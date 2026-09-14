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

# อนุญาตให้เชื่อมต่อข้าม Origin (CORS) สำหรับ Frontend ทุกที่
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SEARCH_BASE_URL = "https://industrial.omron.eu/en/services-support/support/product-lifecycle-management"
MIN_DELAY_SEC = 0.8


def _empty_result(part: str, target_url: str, status_label: str) -> list[dict]:
    return [{
        "Search Input": part,
        "Part Number": status_label,
        "Status": "-",
        "Possible Replacement": "-",
        "Discontinuation Date": "-",
        "Source URL": target_url,
    }]


async def search_omron_parts_with_cookie(page, parts_list: list[str]) -> list[dict]:
    """เข้าหน้า Lifecycle Management และค้นหาพาร์ททีละรายการผ่านช่องกรอกบนหน้าเว็บ"""
    all_results = []
    target_url = SEARCH_BASE_URL

    logger.info(f"กำลังนำทางไปยัง: {target_url}")
    try:
        await page.goto(target_url, wait_until="domcontentloaded", timeout=40000)
    except Exception as err:
        logger.warning(f"การโหลดหน้าเว็บใช้เวลานาน: {err}")

    await page.wait_for_timeout(3000)

    # เคลียร์ Cookie Banner ที่อาจบังหน้าจอ
    await page.evaluate("""
        () => {
            const elements = document.querySelectorAll('#onetrust-consent-sdk, #onetrust-banner-sdk, .cookie-banner');
            elements.forEach(el => el.remove());
        }
    """)

    for idx, part in enumerate(parts_list):
        try:
            logger.info(f"[{part}] กำลังค้นหา... ({idx+1}/{len(parts_list)})")
            
            # ค้นหาช่อง input ผ่าน JS และกรอกคำค้นหา
            search_triggered = await page.evaluate("""
                (partText) => {
                    const inputs = Array.from(document.querySelectorAll('input'));
                    const searchInput = inputs.find(el => {
                        const ph = (el.placeholder || '').toLowerCase();
                        const name = (el.name || '').toLowerCase();
                        const id = (el.id || '').toLowerCase();
                        const cls = (el.className || '').toLowerCase();
                        return ph.includes('search') || ph.includes('part') || ph.includes('model') || 
                               name.includes('query') || name.includes('search') || name.includes('keyword') ||
                               id.includes('search') || cls.includes('search');
                    }) || document.querySelector('input[type="search"]') || document.querySelector('input[type="text"]') || document.querySelector('input');

                    if (searchInput) {
                        searchInput.focus();
                        searchInput.value = '';
                        searchInput.value = partText;
                        searchInput.dispatchEvent(new Event('input', { bubbles: true }));
                        searchInput.dispatchEvent(new Event('change', { bubbles: true }));
                        
                        const form = searchInput.closest('form');
                        if (form) {
                            const submitBtn = form.querySelector('button[type="submit"], input[type="submit"], button, .search-btn');
                            if (submitBtn) {
                                submitBtn.click();
                            } else {
                                form.submit();
                            }
                        } else {
                            searchInput.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true }));
                        }
                        return true;
                    }
                    return false;
                }
            """, part)

            if not search_triggered:
                all_results.extend(_empty_result(part, target_url, "Search Input Not Found"))
                continue

            # รอผลลัพธ์แสดงในตาราง
            await page.wait_for_timeout(3000)

            page_num = 1
            part_found = False

            while True:
                rows = await page.query_selector_all("table tbody tr")
                
                if not rows and page_num == 1:
                    break

                for row in rows:
                    cols = await row.query_selector_all("td")
                    if len(cols) >= 4:
                        p_num = (await cols[0].text_content() or "").strip()
                        status = (await cols[1].text_content() or "").strip()
                        replacement = (await cols[2].text_content() or "").strip()
                        disco_date = (await cols[3].text_content() or "").strip()

                        if "no result" in p_num.lower() or "not found" in p_num.lower():
                            continue

                        all_results.append({
                            "Search Input": part,
                            "Part Number": p_num,
                            "Status": status,
                            "Possible Replacement": replacement,
                            "Discontinuation Date": disco_date,
                            "Source URL": target_url,
                        })
                        part_found = True

                next_button = await page.query_selector("ul.pagination li.next:not(.disabled) a, a.next-page, button.btn-next")
                if next_button and await next_button.is_visible():
                    page_num += 1
                    await next_button.click(force=True)
                    await page.wait_for_timeout(1500)
                else:
                    break

            if not part_found:
                all_results.extend(_empty_result(part, target_url, "Not Found"))

        except Exception as e:
            logger.error(f"[{part}] เกิดข้อผิดพลาด: {e}")
            all_results.extend(_empty_result(part, target_url, f"Error: {str(e)[:30]}"))

        if idx < len(parts_list) - 1:
            await asyncio.sleep(MIN_DELAY_SEC)

    return all_results


@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    """ฝังหน้า UI (index_2.html) ให้อัตโนมัติเมื่อเปิด Root URL"""
    return """
    <!DOCTYPE html>
    <html lang="th">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Omron Part Search Tool - For Sales</title>
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; max-width: 600px; margin: 20px auto; padding: 15px; background: #f0f2f5; }
            .card { background: white; padding: 20px; border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); margin-bottom: 15px; }
            h2 { color: #0056b3; margin-top: 0; font-size: 20px; }
            h3 { color: #0056b3; margin-top: 0; font-size: 16px; }
            textarea { width: 100%; height: 150px; padding: 10px; border: 1px solid #ccc; border-radius: 6px; box-sizing: border-box; font-family: monospace; font-size: 14px; }
            input[type="text"] { width: 100%; padding: 10px; border: 1px solid #ccc; border-radius: 6px; box-sizing: border-box; font-family: monospace; font-size: 13px; }
            button { width: 100%; background: #0056b3; color: white; padding: 12px; border: none; border-radius: 6px; font-weight: bold; font-size: 16px; margin-top: 10px; cursor: pointer; }
            button:disabled { background: #aaa; cursor: not-allowed; }
            .loading { display: none; margin-top: 15px; padding: 10px; background: #e8f4f8; color: #0056b3; border-radius: 6px; font-weight: bold; text-align: center; }
            .error-box { display: none; margin-top: 15px; padding: 10px; background: #fdecea; color: #b71c1c; border-radius: 6px; font-weight: bold; text-align: center; white-space: pre-wrap; }
            .success-box { display: none; margin-top: 15px; padding: 10px; background: #e6f4ea; color: #1e7e34; border-radius: 6px; font-weight: bold; text-align: center; }
            .hint { font-size: 12px; color: #666; margin-top: 6px; }
            .steps { font-size: 13px; color: #333; line-height: 1.8; padding-left: 20px; margin: 8px 0; }
            .steps li { margin-bottom: 4px; }
            .steps code { background: #f0f2f5; padding: 1px 5px; border-radius: 4px; font-size: 12px; }
            .cookie-status { font-size: 12px; margin-top: 6px; font-weight: bold; }
            .cookie-status.ok { color: #1e7e34; }
            .cookie-status.missing { color: #b71c1c; }
            a.omron-link { color: #0056b3; font-weight: bold; }
        </style>
    </head>
    <body>

    <div class="card">
        <h3>ขั้นตอนที่ 1: เตรียม Session จาก Omron</h3>
        <ol class="steps">
            <li>เปิด <a class="omron-link" href="https://industrial.omron.eu/en/services-support/support/product-lifecycle-management" target="_blank" rel="noopener">เว็บ Omron</a> ในแท็บใหม่ แล้ว Login ด้วยบัญชีของคุณตามปกติ</li>
            <li>Login สำเร็จแล้ว กด <code>F12</code> เพื่อเปิด Developer Tools</li>
            <li>ไปที่แท็บ <code>Network</code> แล้วรีเฟรชหน้าเว็บ (F5)</li>
            <li>คลิกที่ request แรกในลิสต์ (ชื่อเดียวกับหน้าเว็บ) แล้วหาหัวข้อ <code>Request Headers</code></li>
            <li>หาบรรทัด <code>Cookie:</code> คลิกขวา copy ค่าทั้งหมดหลังเครื่องหมาย <code>:</code></li>
            <li>กลับมาที่นี่ วางค่าที่ copy มาในช่องด้านล่าง</li>
        </ol>
    </div>

    <div class="card">
        <h3>ขั้นตอนที่ 2: วาง Session Cookie</h3>
        <input type="text" id="cookieInput" placeholder="วาง Cookie ที่ copy มาจาก Omron ที่นี่">
        <div class="cookie-status" id="cookieStatus"></div>
    </div>

    <div class="card">
        <h2>🔎 ขั้นตอนที่ 3: ค้นหา Part Number</h2>
        <p>ใส่ Part Number ที่ต้องการเช็ค (บรรทัดละ 1 รายการ):</p>
        <textarea id="partsInput" placeholder="DX100-0010&#10;E2E-X3D1-M1G&#10;MY4N DC24"></textarea>
        <div class="hint" id="countHint"></div>
        <button id="submitBtn" onclick="processSearch()">เริ่มค้นหา & โหลด CSV</button>

        <div id="loadingBox" class="loading"></div>
        <div id="errorBox" class="error-box"></div>
        <div id="successBox" class="success-box"></div>
    </div>

    <script>
        const BACKEND_URL = "/search";
        const AVG_SECONDS_PER_PART = 3.5;

        const partsInput = document.getElementById('partsInput');
        const countHint = document.getElementById('countHint');
        const cookieInput = document.getElementById('cookieInput');
        const cookieStatus = document.getElementById('cookieStatus');

        window.omronCookieValue = '';

        cookieInput.addEventListener('input', () => {
            window.omronCookieValue = cookieInput.value.trim();
            updateCookieStatus();
        });

        function updateCookieStatus() {
            if (window.omronCookieValue.length > 0) {
                cookieStatus.textContent = '✓ พร้อมใช้งาน';
                cookieStatus.className = 'cookie-status ok';
            } else {
                cookieStatus.textContent = '✗ ยังไม่ได้วาง Cookie';
                cookieStatus.className = 'cookie-status missing';
            }
        }

        partsInput.addEventListener('input', updateCountHint);

        function updateCountHint() {
            const lines = partsInput.value.split('\\n').map(l => l.trim()).filter(l => l.length > 0);
            if (lines.length === 0) {
                countHint.textContent = '';
                return;
            }
            const estSeconds = Math.round(lines.length * AVG_SECONDS_PER_PART);
            const estMinutes = Math.ceil(estSeconds / 60);
            countHint.textContent = `${lines.length} part number • ใช้เวลาโดยประมาณ ${estMinutes} นาที`;
        }

        async function processSearch() {
            const text = partsInput.value.trim();
            if (!text) {
                showError('กรุณากรอก Part Number อย่างน้อย 1 รายการ');
                return;
            }

            if (!window.omronCookieValue) {
                showError('กรุณาวาง Session Cookie จาก Omron ก่อนค้นหา (ดูขั้นตอนที่ 1-2 ด้านบน)');
                return;
            }

            const btn = document.getElementById('submitBtn');
            const loading = document.getElementById('loadingBox');
            const errorBox = document.getElementById('errorBox');
            const successBox = document.getElementById('successBox');

            const lineCount = text.split('\\n').map(l => l.trim()).filter(l => l.length > 0).length;
            const estMinutes = Math.max(1, Math.ceil((lineCount * AVG_SECONDS_PER_PART) / 60));

            btn.disabled = true;
            errorBox.style.display = 'none';
            successBox.style.display = 'none';
            loading.style.display = 'block';
            loading.textContent = `⏳ กำลังค้นหา ${lineCount} part number... (โดยประมาณ ${estMinutes} นาที กรุณาอย่าปิดหน้านี้)`;

            try {
                const response = await fetch(BACKEND_URL, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                    body: new URLSearchParams({
                        'part_numbers': text,
                        'omron_cookie': window.omronCookieValue,
                    })
                });

                if (response.ok) {
                    const blob = await response.blob();
                    const url = window.URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    a.download = `Omron_Search_${new Date().toISOString().slice(0, 10)}.csv`;
                    document.body.appendChild(a);
                    a.click();
                    a.remove();
                    window.URL.revokeObjectURL(url);

                    successBox.textContent = '✓ ค้นหาสำเร็จ ไฟล์ CSV ถูกดาวน์โหลดแล้ว';
                    successBox.style.display = 'block';
                } else {
                    let detail = `เกิดข้อผิดพลาดจากเซิร์ฟเวอร์ (HTTP ${response.status})`;
                    try {
                        const errJson = await response.json();
                        if (errJson && errJson.detail) {
                            detail += `\\n${errJson.detail}`;
                        }
                    } catch (_) {}

                    if (response.status === 401) {
                        detail += '\\n\\nกรุณากลับไปทำขั้นตอนที่ 1-2 ใหม่ เพื่อวาง Cookie ที่ยังไม่หมดอายุ';
                    }

                    showError(detail);
                }
            } catch (err) {
                showError('ไม่สามารถเชื่อมต่อ Backend ได้: ' + err.message);
            } finally {
                btn.disabled = false;
                loading.style.display = 'none';
            }
        }

        function showError(message) {
            const errorBox = document.getElementById('errorBox');
            const successBox = document.getElementById('successBox');
            successBox.style.display = 'none';
            errorBox.textContent = '⚠️ ' + message;
            errorBox.style.display = 'block';
        }

        updateCookieStatus();
    </script>
    </body>
    </html>
    """


@app.post("/search")
async def search_omron_parts(
    part_numbers: str = Form(...),
    omron_cookie: str = Form(...)
):
    parts_list = [p.strip() for p in part_numbers.split("\n") if p.strip()]
    if not parts_list:
        raise HTTPException(status_code=400, detail="No part numbers provided")

    if not omron_cookie.strip():
        raise HTTPException(status_code=400, detail="กรุณาระบุ Session Cookie")

    logger.info(f"เริ่มค้นหาจำนวน {len(parts_list)} รายการด้วย Cookie ที่ได้รับ")

    # แปลง Cookie string ที่คัดลอกมาให้เป็นรูปแบบ list ของ dictionary สำหรับ Playwright context
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

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )

        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800}
        )

        if cookies_list:
            try:
                await context.add_cookies(cookies_list)
            except Exception as e:
                logger.warning(f"เพิ่ม Cookie ไม่สำเร็จบางตัว: {e}")

        page = await context.new_page()

        try:
            all_results = await search_omron_parts_with_cookie(page, parts_list)
        except Exception as e:
            logger.error(f"Search Process Exception: {e}")
            raise HTTPException(status_code=500, detail=f"เกิดข้อผิดพลาดในการประมวลผล: {str(e)[:150]}")
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
