import os
import io
import logging
import asyncio
import pandas as pd
from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from playwright.async_api import async_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("omron-part-search")

app = FastAPI(title="Omron Part Lifecycle Exporter")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SEARCH_BASE_URL = "https://industrial.omron.eu/en/services-support/support/product-lifecycle-management"
MIN_DELAY_SEC = 1.0


def _empty_result(part: str, target_url: str, status_label: str) -> list[dict]:
    return [{
        "Search Input": part,
        "Part Number": status_label,
        "Status": "-",
        "Possible Replacement": "-",
        "Discontinuation Date": "-",
        "Source URL": target_url,
    }]


async def dismiss_cookie_banner(page):
    """ปิดแบนเนอร์ Cookie ของหน้าเว็บ"""
    try:
        banner_buttons = page.locator("button#onetrust-accept-btn-handler, .cookie-banner button, button:has-text('Accept All')")
        if await banner_buttons.first.is_visible(timeout=2000):
            await banner_buttons.first.click()
            await page.wait_for_timeout(1000)
    except Exception:
        pass


@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    return r"""
    <!DOCTYPE html>
    <html lang="th">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Omron Part Lifecycle Checker</title>
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; max-width: 600px; margin: 40px auto; padding: 20px; background: #f0f2f5; }
            .card { background: white; padding: 25px; border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
            h2 { color: #0056b3; margin-top: 0; font-size: 22px; }
            label { font-weight: bold; font-size: 14px; display: block; margin-bottom: 8px; color: #333; }
            textarea { width: 100%; height: 180px; padding: 12px; border: 1px solid #ccc; border-radius: 6px; box-sizing: border-box; font-family: monospace; font-size: 14px; margin-bottom: 12px; }
            button { width: 100%; background: #0056b3; color: white; padding: 14px; border: none; border-radius: 6px; font-weight: bold; font-size: 16px; cursor: pointer; transition: 0.2s; }
            button:hover { background: #004494; }
            button:disabled { background: #aaa; cursor: not-allowed; }
            .loading { display: none; margin-top: 15px; padding: 12px; background: #e8f4f8; color: #0056b3; border-radius: 6px; font-weight: bold; text-align: center; }
            .error-box { display: none; margin-top: 15px; padding: 12px; background: #fdecea; color: #b71c1c; border-radius: 6px; font-weight: bold; text-align: center; white-space: pre-wrap; }
            .success-box { display: none; margin-top: 15px; padding: 12px; background: #e6f4ea; color: #1e7e34; border-radius: 6px; font-weight: bold; text-align: center; }
            .hint { font-size: 13px; color: #666; margin-bottom: 15px; }
        </style>
    </head>
    <body>

    <div class="card">
        <h2>🔎 Omron Part Lifecycle Search</h2>
        <div class="hint">ระบบตรวจสอบสถานะและรุ่นทดแทนอุปกรณ์ Omron (กรอกรายชื่อพาร์ทเพื่อดึงรายงาน CSV)</div>
        
        <label>ใส่ Part Number (บรรทัดละ 1 รายการ):</label>
        <textarea id="partsInput" placeholder="CP1E-E10DR-A&#10;CP1E-E14DR-A&#10;E2E-X3D1-M1G"></textarea>
        
        <button id="submitBtn" onclick="processSearch()">เริ่มค้นหา & ดาวน์โหลด CSV</button>

        <div id="loadingBox" class="loading"></div>
        <div id="errorBox" class="error-box"></div>
        <div id="successBox" class="success-box"></div>
    </div>

    <script>
        async function processSearch() {
            const text = document.getElementById('partsInput').value.trim();
            if (!text) return showError('กรุณากรอก Part Number อย่างน้อย 1 รายการ');

            const btn = document.getElementById('submitBtn');
            const loading = document.getElementById('loadingBox');
            const errorBox = document.getElementById('errorBox');
            const successBox = document.getElementById('successBox');

            const partsCount = text.split(/\r?\n/).map(l => l.trim()).filter(l => l.length > 0).length;

            btn.disabled = true;
            errorBox.style.display = 'none';
            successBox.style.display = 'none';
            loading.style.display = 'block';
            loading.textContent = `⏳ กำลังค้นหา ${partsCount} รายการ... (กรุณารอสักครู่)`;

            try {
                const response = await fetch('/search', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                    body: new URLSearchParams({ 'part_numbers': text })
                });

                if (response.ok) {
                    const blob = await response.blob();
                    const url = window.URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    a.download = `Omron_Lifecycle_Report_${new Date().toISOString().slice(0, 10)}.csv`;
                    document.body.appendChild(a);
                    a.click();
                    a.remove();
                    window.URL.revokeObjectURL(url);

                    successBox.textContent = '✓ ค้นหาสำเร็จ ไฟล์ CSV ถูกดาวน์โหลดแล้ว!';
                    successBox.style.display = 'block';
                } else {
                    let detail = `เกิดข้อผิดพลาด (HTTP ${response.status})`;
                    try {
                        const errJson = await response.json();
                        if (errJson && errJson.detail) detail += '\n' + errJson.detail;
                    } catch (_) {}
                    showError(detail);
                }
            } catch (err) {
                showError('ไม่สามารถเชื่อมต่อเซิร์ฟเวอร์ได้: ' + err.message);
            } finally {
                btn.disabled = false;
                loading.style.display = 'none';
            }
        }

        function showError(msg) {
            document.getElementById('successBox').style.display = 'none';
            const errorBox = document.getElementById('errorBox');
            errorBox.textContent = '⚠️ ' + msg;
            errorBox.style.display = 'block';
        }
    </script>
    </body>
    </html>
    """


@app.post("/search")
async def process_search_endpoint(part_numbers: str = Form(...)):
    parts_list = [p.strip() for p in part_numbers.split("\n") if p.strip()]
    if not parts_list:
        raise HTTPException(status_code=400, detail="ไม่พบข้อมูล Part Number")

    omron_cookie = os.environ.get("OMRON_COOKIE", "").strip()
    if not omron_cookie:
        raise HTTPException(
            status_code=500, 
            detail="ระบบยังไม่ได้ตั้งค่าคุกกี้กลาง (OMRON_COOKIE) บน Render"
        )

    cookies_list = []
    for item in omron_cookie.split(";"):
        if "=" in item:
            parts = item.split("=", 1)
            c_name = parts[0].strip()
            c_value = parts[1].strip()
            if c_name:
                cookies_list.append({
                    "name": c_name,
                    "value": c_value,
                    "domain": ".omron.eu",
                    "path": "/"
                })

    logger.info(f"เริ่มการค้นหาพาร์ทจำนวน {len(parts_list)} รายการให้ผู้ใช้")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox", 
                "--disable-setuid-sandbox", 
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled"
            ]
        )

        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800}
        )

        if cookies_list:
            try:
                await context.add_cookies(cookies_list)
            except Exception as e:
                logger.warning(f"เพิ่ม Cookie บางตัวไม่สำเร็จ: {e}")

        page = await context.new_page()
        all_results = []

        try:
            logger.info(f"กำลังนำทางไปยัง: {SEARCH_BASE_URL}")
            await page.goto(SEARCH_BASE_URL, wait_until="domcontentloaded", timeout=40000)
            await dismiss_cookie_banner(page)
            await page.wait_for_timeout(2000)

            # ปรับปรุง Selector ให้ตรงกับช่องค้นหาของ Omron จากภาพจริง
            search_input_selector = 'input[placeholder*="part number" i], input[type="text"], input.search-input'

            for idx, part in enumerate(parts_list):
                try:
                    logger.info(f"[{part}] กำลังค้นหา...")
                    
                    search_box = page.locator(search_input_selector).first
                    await search_box.wait_for(state="visible", timeout=10000)
                    
                    await search_box.click()
                    await page.keyboard.press("Control+A")
                    await page.keyboard.press("Backspace")
                    await search_box.fill(part)
                    await page.wait_for_timeout(800)
                    
                    # คลิกปุ่มแว่นขยาย หรือกด Enter เพื่อค้นหา
                    search_icon = page.locator('button:has(svg), .search-btn, button[type="submit"]')
                    if await search_icon.first.is_visible(timeout=1500):
                        await search_icon.first.click()
                    else:
                        await search_box.press("Enter")

                    # รอให้ตารางผลลัพธ์โหลดขึ้นมา
                    try:
                        await page.wait_for_selector("table tbody tr", state="visible", timeout=10000)
                        await page.wait_for_timeout(1500)
                    except Exception:
                        all_results.extend(_empty_result(part, SEARCH_BASE_URL, "Not Found (Timeout)"))
                        continue

                    rows = await page.locator("table tbody tr").all()
                    part_found = False

                    for row in rows:
                        cols = await row.locator("td").all()
                        if len(cols) >= 4:
                            p_num = (await cols[0].text_content() or "").strip()
                            status = (await cols[1].text_content() or "").strip()
                            replacement = (await cols[2].text_content() or "").strip()
                            disco_date = (await cols[3].text_content() or "").strip()

                            if "no result" in p_num.lower() or "not found" in p_num.lower() or not p_num:
                                continue

                            all_results.append({
                                "Search Input": part,
                                "Part Number": p_num,
                                "Status": status,
                                "Possible Replacement": replacement,
                                "Discontinuation Date": disco_date,
                                "Source URL": SEARCH_BASE_URL,
                            })
                            part_found = True

                    if not part_found:
                        all_results.extend(_empty_result(part, SEARCH_BASE_URL, "Not Found"))

                except Exception as e:
                    logger.error(f"[{part}] เกิดข้อผิดพลาด: {e}")
                    all_results.extend(_empty_result(part, SEARCH_BASE_URL, f"Error: {str(e)[:30]}"))

                if idx < len(parts_list) - 1:
                    await asyncio.sleep(MIN_DELAY_SEC)

        except Exception as e:
            logger.error(f"กระบวนการล้มเหลว: {e}")
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            await browser.close()

    df = pd.DataFrame(all_results)
    stream = io.StringIO()
    df.to_csv(stream, index=False, encoding="utf-8-sig")

    response = StreamingResponse(
        iter([stream.getvalue()]),
        media_type="text/csv"
    )
    response.headers["Content-Disposition"] = "attachment; filename=omron_lifecycle_report.csv"
    return response


@app.get("/health")
async def health_check():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=10000)
