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

MIN_DELAY_SEC = 0.5


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
    """จัดการ Session ของ Playwright: ใช้ Session เดิม หากไม่มีค่อยล็อกอินใหม่"""
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

    if not username or not password:
        raise HTTPException(
            status_code=400,
            detail="ยังไม่มี Session กรุณากรอก Email และ Password เพื่อล็อกอิน"
        )

    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        viewport={"width": 1280, "height": 800}
    )
    page = await context.new_page()

    logger.info("กำลังเปิดหน้าเว็บ Omron เพื่อทำการ ล็อกอิน...")
    try:
        # กำหนด timeout 15 วินาทีเพื่อป้องกัน HTTP 502
        await page.goto(SEARCH_BASE_URL, wait_until="domcontentloaded", timeout=15000)

        # ปิด Cookie Banner ถ้ามี
        try:
            cookie_accept = page.locator("#onetrust-accept-btn-handler, button:has-text('Accept')").first
            if await cookie_accept.is_visible(timeout=2000):
                await cookie_accept.click()
        except Exception:
            pass

        email_field = page.get_by_placeholder("Email address").first
        
        if not await email_field.is_visible():
            logger.info("คลิกปุ่ม Login or register...")
            trigger_btn = page.get_by_role("button", name="Login or register").or_(
                page.locator("a:has-text('Login or register')")
            ).first
            await trigger_btn.click(force=True)
            await email_field.wait_for(state="visible", timeout=5000)

        # กรอกข้อมูลล็อกอิน
        await email_field.fill(username)
        pass_field = page.get_by_placeholder("Password").first
        await pass_field.fill(password)

        login_submit_btn = page.get_by_role("button", name="Log in", exact=True).or_(
            page.locator("button:has-text('Log in')")
        ).first
        await login_submit_btn.click(force=True)

        await page.wait_for_timeout(2000)

        # บันทึก Session เก็บไว้ใช้ซ้ำ
        await context.storage_state(path=STATE_FILE)
        logger.info("ล็อกอินสำเร็จและบันทึก Session เรียบร้อยแล้ว")
        return context

    except Exception as e:
        logger.error(f"การล็อกอินล้มเหลว: {e}")
        raise HTTPException(
            status_code=400,
            detail=f"ล็อกอินไม่สำเร็จ ตรวจสอบ Email/Password หรือลองอีกครั้ง: {str(e)[:150]}"
        )


async def search_omron_parts_fast(page, parts_list: list[str]) -> list[dict]:
    """ค้นหาข้อมูล Part Number แบบรวดเร็ว"""
    all_results = []
    target_url = SEARCH_BASE_URL

    await page.goto(target_url, wait_until="domcontentloaded", timeout=15000)
    
    search_input = page.get_by_placeholder("Search by part number, short item code or EAN code").or_(
        page.locator("input[type='search'], input.form-control")
    ).first

    await search_input.wait_for(state="visible", timeout=10000)

    for idx, part in enumerate(parts_list):
        try:
            logger.info(f"[{part}] กำลังค้นหา... ({idx+1}/{len(parts_list)})")
            
            await search_input.fill("")
            await search_input.fill(part)
            await search_input.press("Enter")

            await page.wait_for_timeout(1500)

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

                # Pagination
                next_button = await page.query_selector("ul.pagination li.next:not(.disabled) a, a.next-page, button.btn-next")
                if next_button and await next_button.is_visible():
                    page_num += 1
                    await next_button.click()
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
        <h3>🔑 เข้าสู่ระบบ Omron Account</h3>
        <label>Email / Username:</label>
        <input type="email" id="userInput" placeholder="อีเมลบัญชี Omron ของคุณ">
        <label>Password:</label>
        <input type="password" id="passInput" placeholder="รหัสผ่าน Omron">
        <div class="hint">* ล็อกอินเพียงครั้งแรก ครั้งถัดไประบบจะใช้ Session เดิมอัตโนมัติ</div>
    </div>

    <div class="card">
        <h2>🔎 ค้นหา Part Number</h2>
        <p>ใส่ Part Number ที่ต้องการเช็ค (แนะนำครั้งละไม่เกิน 5-10 รายการเพื่อป้องกัน Timeout):</p>
        <textarea id="partsInput" placeholder="CP1E-E10DR-A&#10;CP1E-E10DR-D&#10;CP1E-E10DT-D"></textarea>
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
            loading.textContent = '⏳ กำลังประมวลผลค้นหาและสร้างไฟล์ CSV... กรุณารอสักครู่';

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


@app.post("/search")
async def search_omron_parts(
    username: str = Form(""),
    password: str = Form(""),
    part_numbers: str = Form(...),
):
    parts_list = [p.strip() for p in part_numbers.split("\n") if p.strip()]
    if not parts_list:
        raise HTTPException(status_code=400, detail="No part numbers provided")

    logger.info(f"เริ่มค้นหาจำนวน {len(parts_list)} รายการ")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )

        context = await get_authenticated_context(browser, username, password)
        page = await context.new_page()

        try:
            all_results = await search_omron_parts_fast(page, parts_list)
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
