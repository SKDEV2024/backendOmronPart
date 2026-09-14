import os
import io
import logging
import asyncio
import pandas as pd
from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import StreamingResponse, HTMLResponse
from playwright.async_api import async_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("omron-part-search")

app = FastAPI(title="Omron Part Lifecycle Exporter")

SEARCH_BASE_URL = "https://industrial.omron.eu/en/services-support/support/product-lifecycle-management"
COOKIE_DOMAIN = "industrial.omron.eu"

MIN_DELAY_SEC = 2.0
MAX_DELAY_SEC = 4.0


def _empty_result(part: str, target_url: str, status_label: str) -> list[dict]:
    return [{
        "Search Input": part,
        "Part Number": status_label,
        "Status": "-",
        "Possible Replacement": "-",
        "Discontinuation Date": "-",
        "Source URL": target_url,
    }]


def parse_cookie_header(cookie_header: str) -> list[dict]:
    cookies = []
    pairs = [p.strip() for p in cookie_header.split(";") if p.strip()]

    for pair in pairs:
        if "=" not in pair:
            continue
        name, value = pair.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        cookies.append({
            "name": name,
            "value": value,
            "domain": COOKIE_DOMAIN,
            "path": "/",
        })

    return cookies


async def search_single_part(page, part: str) -> list[dict]:
    target_url = SEARCH_BASE_URL

    try:
        await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)

        search_input_selector = "input[type='search'], input[placeholder*='search' i], input.form-control"
        await page.wait_for_selector(search_input_selector, timeout=15000)
        
        search_box = page.locator(search_input_selector).first
        await search_box.fill("")
        await search_box.fill(part)
        await search_box.press("Enter")

        await page.wait_for_timeout(3000)
        
        part_results = []
        page_num = 1

        while True:
            rows = await page.query_selector_all("table tbody tr")

            if not rows and page_num == 1:
                logger.info(f"[{part}] ไม่พบผลลัพธ์ในตาราง")
                return _empty_result(part, target_url, "Not Found")

            for row in rows:
                cols = await row.query_selector_all("td")
                if len(cols) >= 4:
                    p_num = (await cols[0].text_content() or "").strip()
                    status = (await cols[1].text_content() or "").strip()
                    replacement = (await cols[2].text_content() or "").strip()
                    disco_date = (await cols[3].text_content() or "").strip()

                    if "no result" in p_num.lower() or "not found" in p_num.lower():
                        continue

                    part_results.append({
                        "Search Input": part,
                        "Part Number": p_num,
                        "Status": status,
                        "Possible Replacement": replacement,
                        "Discontinuation Date": disco_date,
                        "Source URL": target_url,
                    })

            next_button = await page.query_selector("ul.pagination li.next:not(.disabled) a, a.next-page, button.btn-next")

            if next_button and await next_button.is_visible():
                logger.info(f"[{part}] กำลังดึงข้อมูลหน้า {page_num + 1}...")
                await next_button.click()
                await page.wait_for_timeout(2500)
                page_num += 1
            else:
                break

        if not part_results:
            return _empty_result(part, target_url, "Not Found")

        logger.info(f"[{part}] ดึงข้อมูลสำเร็จ รวม {len(part_results)} รายการ")
        return part_results

    except Exception as e:
        logger.error(f"[{part}] เกิดข้อผิดพลาด: {e}")
        return _empty_result(part, target_url, f"Error: {str(e)[:50]}")


# ---------------------------------------------------------
# Front-end UI (เสิร์ฟผ่านหน้าแรก /)
# ---------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    html_content = """
    <!DOCTYPE html>
    <html lang="th">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Omron Part Search Tool</title>
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; max-width: 650px; margin: 30px auto; padding: 15px; background: #f0f2f5; }
            .card { background: white; padding: 20px; border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); margin-bottom: 15px; }
            h2 { color: #0056b3; margin-top: 0; font-size: 20px; }
            h3 { color: #0056b3; margin-top: 0; font-size: 16px; }
            textarea { width: 100%; height: 140px; padding: 10px; border: 1px solid #ccc; border-radius: 6px; box-sizing: border-box; font-family: monospace; font-size: 14px; }
            input[type="text"] { width: 100%; padding: 10px; border: 1px solid #ccc; border-radius: 6px; box-sizing: border-box; font-family: monospace; font-size: 13px; }
            button { width: 100%; background: #0056b3; color: white; padding: 12px; border: none; border-radius: 6px; font-weight: bold; font-size: 16px; margin-top: 10px; cursor: pointer; }
            button:disabled { background: #aaa; cursor: not-allowed; }
            .loading { display: none; margin-top: 15px; padding: 10px; background: #e8f4f8; color: #0056b3; border-radius: 6px; font-weight: bold; text-align: center; }
            .error-box { display: none; margin-top: 15px; padding: 10px; background: #fdecea; color: #b71c1c; border-radius: 6px; font-weight: bold; text-align: center; white-space: pre-wrap; }
            .success-box { display: none; margin-top: 15px; padding: 10px; background: #e6f4ea; color: #1e7e34; border-radius: 6px; font-weight: bold; text-align: center; }
            .hint { font-size: 12px; color: #666; margin-top: 6px; }
            .steps { font-size: 13px; color: #333; line-height: 1.8; padding-left: 20px; margin: 8px 0; }
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
            <li>เปิด <a class="omron-link" href="https://industrial.omron.eu/en/services-support/support/product-lifecycle-management" target="_blank" rel="noopener">เว็บ Omron Lifecycle</a> แล้ว Login บัญชีของคุณ</li>
            <li>กด <code>F12</code> -> แท็บ <code>Network</code> แล้วกด <code>F5</code> รีเฟรชหน้าเว็บ</li>
            <li>คลิก Request แรก -> ดูหัวข้อ <code>Request Headers</code> -> คัดลอกค่าหลังบรรทัด <code>Cookie:</code></li>
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
        <textarea id="partsInput" placeholder="CP1E&#10;E2E-X3D1-M1G&#10;MY4N DC24"></textarea>
        <div class="hint" id="countHint"></div>
        <button id="submitBtn" onclick="processSearch()">เริ่มค้นหา & โหลด CSV</button>

        <div id="loadingBox" class="loading"></div>
        <div id="errorBox" class="error-box"></div>
        <div id="successBox" class="success-box"></div>
    </div>

    <script>
        const cookieInput = document.getElementById('cookieInput');
        const cookieStatus = document.getElementById('cookieStatus');
        const partsInput = document.getElementById('partsInput');
        const countHint = document.getElementById('countHint');

        cookieInput.addEventListener('input', () => {
            if (cookieInput.value.trim().length > 0) {
                cookieStatus.textContent = '✓ พร้อมใช้งาน';
                cookieStatus.className = 'cookie-status ok';
            } else {
                cookieStatus.textContent = '✗ ยังไม่ได้วาง Cookie';
                cookieStatus.className = 'cookie-status missing';
            }
        });

        async function processSearch() {
            const text = partsInput.value.trim();
            const cookieVal = cookieInput.value.trim();

            if (!text) return showError('กรุณากรอก Part Number อย่างน้อย 1 รายการ');
            if (!cookieVal) return showError('กรุณาวาง Session Cookie ก่อนค้นหา');

            const btn = document.getElementById('submitBtn');
            const loading = document.getElementById('loadingBox');
            const errorBox = document.getElementById('errorBox');
            const successBox = document.getElementById('successBox');

            btn.disabled = true;
            errorBox.style.display = 'none';
            successBox.style.display = 'none';
            loading.style.display = 'block';
            loading.textContent = '⏳ กำลังระบบประมวลผลและค้นหาข้อมูล... (กรุณาลองรอมันดึงข้อมูลสักครู่)';

            try {
                const response = await fetch('/search', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                    body: new URLSearchParams({
                        'part_numbers': text,
                        'omron_cookie': cookieVal,
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

                    successBox.textContent = '✓ ค้นหาสำเร็จ ไฟล์ CSV ถูกดาวน์โหลดเรียบร้อยแล้ว';
                    successBox.style.display = 'block';
                } else {
                    let detail = `เกิดข้อผิดพลาดจากเซิร์ฟเวอร์ (HTTP ${response.status})`;
                    try {
                        const errJson = await response.json();
                        if (errJson && errJson.detail) detail += `\\n${errJson.detail}`;
                    } catch (_) {}
                    showError(detail);
                }
            } catch (err) {
                showError('ไม่สามารถเชื่อมต่อระบบได้: ' + err.message);
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
    return html_content


# ---------------------------------------------------------
# Back-end API Endpoint (/search)
# ---------------------------------------------------------
@app.post("/search")
async def search_omron_parts(
    part_numbers: str = Form(...),
    omron_cookie: str = Form(...),
):
    parts_list = [p.strip() for p in part_numbers.split("\n") if p.strip()]
    if not parts_list:
        raise HTTPException(status_code=400, detail="No part numbers provided")

    if not omron_cookie or not omron_cookie.strip():
        raise HTTPException(status_code=400, detail="Missing Omron session cookie")

    cookies = parse_cookie_header(omron_cookie)
    if not cookies:
        raise HTTPException(status_code=400, detail="รูปแบบ Cookie ไม่ถูกต้อง")

    logger.info(f"เริ่มค้นหา {len(parts_list)} รายการ")
    all_results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )

        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800}
        )

        await context.add_cookies(cookies)
        page = await context.new_page()

        try:
            for idx, part in enumerate(parts_list):
                part_results = await search_single_part(page, part)
                all_results.extend(part_results)

                if idx < len(parts_list) - 1:
                    delay = MIN_DELAY_SEC + (MAX_DELAY_SEC - MIN_DELAY_SEC) * os.urandom(1)[0] / 255
                    await asyncio.sleep(delay)

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