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
STATE_FILE = "omron_auth_state.json"

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


async def get_authenticated_context(browser, username: str = "", password: str = ""):
    """จัดการ Session ของ Playwright: ใช้ Session เดิม หรือทำ Auto-Login ผ่าน Popup"""
    # 1. โหลด Session เดิมหากมีบันทึกไว้แล้ว
    if os.path.exists(STATE_FILE):
        try:
            context = await browser.new_context(
                storage_state=STATE_FILE,
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800}
            )
            logger.info("ใช้งาน Session เดิมจาก storage_state")
            return context
        except Exception as e:
            logger.warning(f"Session เดิมใช้ไม่ได้: {e}")

    # 2. กรณีไม่มี Session ต้องระบุ Username / Password เพื่อล็อกอินผ่าน Popup
    if not username or not password:
        raise HTTPException(
            status_code=400,
            detail="ยังไม่มี Session หรือหมดอายุ กรุณากรอก Email และ Password เพื่อล็อกอิน"
        )

    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        viewport={"width": 1280, "height": 800}
    )
    page = await context.new_page()

    logger.info("กำลังเปิดหน้าหลักเพื่อทำการ Login ผ่าน Popup...")
    try:
        await page.goto(SEARCH_BASE_URL, wait_until="domcontentloaded", timeout=30000)

        # กดปุ่มยอมรับ Cookie/Privacy Notice หากมีขึ้นมาบังหน้าจอ
        try:
            accept_cookie_btn = page.locator("#onetrust-accept-btn-handler, button:has-text('Accept'), button:has-text('Allow')")
            if await accept_cookie_btn.is_visible(timeout=3000):
                await accept_cookie_btn.click()
        except Exception:
            pass

        # คลิกปุ่ม/ลิงก์ Login เพื่อเปิด Popup
        login_trigger = page.locator("a:has-text('Log in'), button:has-text('Log in'), .user-login-link, a[href*='login']")
        await login_trigger.first.click()

        # รอให้ Popup ล็อกอินแสดงผล
        email_selector = "input[type='email'], input[name='email'], input[name='username'], input[id*='email' i]"
        pass_selector = "input[type='password'], input[name='password'], input[id*='password' i]"

        await page.wait_for_selector(email_selector, timeout=15000)
        await page.fill(email_selector, username)
        await page.fill(pass_selector, password)

        # กดปุ่ม Submit ภายใน Popup
        submit_btn = page.locator("form button[type='submit'], form input[type='submit'], .modal-body button[type='submit']")
        await submit_btn.first.click()

        # รอให้กระบวนการ Login ใน Popup เสร็จสิ้น
        await page.wait_for_timeout(4000)

        # บันทึก Session เก็บไว้ใช้ครั้งถัดไป
        await context.storage_state(path=STATE_FILE)
        logger.info("ล็อกอินผ่าน Popup สำเร็จ และบันทึก Session เรียบร้อย")
        return context

    except Exception as e:
        logger.error(f"การล็อกอินผ่าน Popup ล้มเหลว: {e}")
        raise HTTPException(
            status_code=400,
            detail=f"ล็อกอินไม่สำเร็จ ตรวจสอบ Email/Password หรือลองอีกครั้ง: {str(e)[:100]}"
        )


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
            input[type="text"], input[type="password"], input[type="email"] { width: 100%; padding: 10px; border: 1px solid #ccc; border-radius: 6px; box-sizing: border-box; font-size: 14px; margin-bottom: 10px; }
            button { width: 100%; background: #0056b3; color: white; padding: 12px; border: none; border-radius: 6px; font-weight: bold; font-size: 16px; margin-top: 10px; cursor: pointer; }
            button:disabled { background: #aaa; cursor: not-allowed; }
            .loading { display: none; margin-top: 15px; padding: 10px; background: #e8f4f8; color: #0056b3; border-radius: 6px; font-weight: bold; text-align: center; }
            .error-box { display: none; margin-top: 15px; padding: 10px; background: #fdecea; color: #b71c1c; border-radius: 6px; font-weight: bold; text-align: center; white-space: pre-wrap; }
            .success-box { display: none; margin-top: 15px; padding: 10px; background: #e6f4ea; color: #1e7e34; border-radius: 6px; font-weight: bold; text-align: center; }
            .hint { font-size: 12px; color: #666; margin-top: 2px; }
        </style>
    </head>
    <body>

    <div class="card">
        <h3>🔑 ล็อกอิน Omron Account</h3>
        <label>Email / Username:</label>
        <input type="email" id="userInput" placeholder="อีเมลบัญชี Omron ของคุณ">
        <label>Password:</label>
        <input type="password" id="passInput" placeholder="รหัสผ่าน Omron">
        <div class="hint">* หากเคยล็อกอินสำเร็จแล้ว ระบบจะใช้ Session เดิมอัตโนมัติ</div>
    </div>

    <div class="card">
        <h2>🔎 ค้นหา Part Number</h2>
        <p>ใส่ Part Number ที่ต้องการเช็ค (บรรทัดละ 1 รายการ):</p>
        <textarea id="partsInput" placeholder="CP1E&#10;E2E-X3D1-M1G&#10;MY4N DC24"></textarea>
        <button id="submitBtn" onclick="processSearch()">เริ่มค้นหา & โหลด CSV</button>

        <div id="loadingBox" class="loading"></div>
        <div id="errorBox" class="error-box"></div>
        <div id="successBox" class="success-box"></div>
    </div>

    <script>
        async function processSearch() {
            const username = document.getElementById('userInput').value.trim();
            const password = document.getElementById('passInput').value.trim();
            const text = document.getElementById('partsInput').value.trim();

            if (!text) return showError('กรุณากรอก Part Number อย่างน้อย 1 รายการ');

            const btn = document.getElementById('submitBtn');
            const loading = document.getElementById('loadingBox');
            const errorBox = document.getElementById('errorBox');
            const successBox = document.getElementById('successBox');

            btn.disabled = true;
            errorBox.style.display = 'none';
            successBox.style.display = 'none';
            loading.style.display = 'block';
            loading.textContent = '⏳ กำลังเปิดระบบ ค้นหา และดึงข้อมูล... กรุณารอสักครู่';

            try {
                const response = await fetch('/search', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                    body: new URLSearchParams({
                        'username': username,
                        'password': password,
                        'part_numbers': text,
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
    username: str = Form(""),
    password: str = Form(""),
    part_numbers: str = Form(...),
):
    parts_list = [p.strip() for p in part_numbers.split("\n") if p.strip()]
    if not parts_list:
        raise HTTPException(status_code=400, detail="No part numbers provided")

    logger.info(f"เริ่มค้นหา {len(parts_list)} รายการ")
    all_results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )

        context = await get_authenticated_context(browser, username, password)
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
