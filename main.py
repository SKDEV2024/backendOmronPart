import io
import logging
import asyncio
import pandas as pd
from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

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
LOGIN_URL = "https://industrial.omron.eu/en/login"
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
    """ปิดแบนเนอร์ Cookie ที่มักจะบังปุ่มต่างๆ"""
    try:
        banner_buttons = page.locator("button#onetrust-accept-btn-handler, .cookie-banner button, button:has-text('Accept All')")
        if await banner_buttons.first.is_visible(timeout=2000):
            await banner_buttons.first.click()
            await page.wait_for_timeout(1000)
    except Exception:
        pass


async def omron_direct_login(page, username, password):
    """ฟังก์ชันจัดการการ Login ตรงๆ ด้วย Playwright Native Actions"""
    logger.info("กำลังเปิดหน้า Login...")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=40000)
    await dismiss_cookie_banner(page)

    logger.info("กำลังกรอกข้อมูลเข้าสู่ระบบ...")
    try:
        # ใช้ Selector ที่ครอบคลุมหลายรูปแบบ (เผื่อเว็บเปลี่ยนโครงสร้าง)
        email_selector = 'input[type="email"], input[name="emailAddress"], input[name*="user"], input[id*="email"]'
        pass_selector = 'input[type="password"], input[name="password"], input[name*="pass"]'
        submit_selector = 'button[type="submit"], input[type="submit"], button.login-btn, button:has-text("Log in"), button:has-text("Sign in")'

        # รอจนกว่าช่องกรอกอีเมลจะโผล่ขึ้นมา (สูงสุด 15 วินาที)
        await page.locator(email_selector).first.wait_for(state="visible", timeout=15000)
        
        # ค่อยๆ พิมพ์เพื่อจำลองพฤติกรรมมนุษย์ (ลดโอกาสโดนบล็อค)
        await page.locator(email_selector).first.fill(username)
        await page.wait_for_timeout(500)
        
        await page.locator(pass_selector).first.fill(password)
        await page.wait_for_timeout(500)

        # กดปุ่มเข้าสู่ระบบ
        await page.locator(submit_selector).first.click()

        # รอให้หน้าเว็บโหลดหลังจากกด Login เสร็จสมบูรณ์
        logger.info("กดปุ่ม Login แล้ว กำลังรอระบบตรวจสอบ...")
        await page.wait_for_load_state("networkidle", timeout=15000)
        
        # เช็คว่ามีข้อความ Error รหัสผ่านผิดหรือไม่
        error_msg = page.locator(".error-message, .alert-danger, .form-error")
        if await error_msg.first.is_visible(timeout=3000):
            err_text = await error_msg.first.text_content()
            raise Exception(f"เข้าสู่ระบบไม่สำเร็จ: {err_text.strip()}")

    except PlaywrightTimeoutError:
        raise Exception("หน้าเว็บโหลดช้าเกินไป หรือหาช่องกรอก Email/Password ไม่พบ (อาจติดระบบป้องกันอัตโนมัติ)")
    except Exception as e:
        raise Exception(str(e))


async def search_omron_parts(page, parts_list: list[str]) -> list[dict]:
    """เข้าหน้าค้นหาและเริ่มค้นหาทีละ Part"""
    all_results = []
    
    logger.info(f"กำลังนำทางไปยังหน้าค้นหา: {SEARCH_BASE_URL}")
    await page.goto(SEARCH_BASE_URL, wait_until="domcontentloaded", timeout=40000)
    await dismiss_cookie_banner(page)
    await page.wait_for_timeout(2000)

    # หาช่องค้นหาบนหน้า Lifecycle (ใช้ Selector ครอบจักรวาล)
    search_input_selector = 'input[type="text"][placeholder*="search"], input[type="search"], input.search-input, input[id*="search"]'

    for idx, part in enumerate(parts_list):
        try:
            logger.info(f"[{part}] กำลังค้นหา... ({idx+1}/{len(parts_list)})")
            
            search_box = page.locator(search_input_selector).first
            await search_box.wait_for(state="visible", timeout=10000)
            
            # ล้างค่าเดิมและกรอกใหม่
            await search_box.fill("")
            await search_box.fill(part)
            await page.wait_for_timeout(500)
            
            # กดปุ่ม Enter เพื่อค้นหา
            await search_box.press("Enter")

            # รอให้ตารางอัปเดตผลลัพธ์
            try:
                # รอให้มี tr ปรากฏในตาราง
                await page.wait_for_selector("table tbody tr", state="visible", timeout=10000)
                await page.wait_for_timeout(1500) # เผื่อเวลาให้ render ข้อมูลเสร็จ
            except PlaywrightTimeoutError:
                # ถ้าเกินเวลาแสดงว่าไม่มีข้อมูล
                all_results.extend(_empty_result(part, SEARCH_BASE_URL, "Not Found (Timeout)"))
                continue

            # ดึงข้อมูลจากตาราง
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

    return all_results


@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    """หน้าต่าง UI สำหรับกรอก Email, Password และ Part Number"""
    return """
    <!DOCTYPE html>
    <html lang="th">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Omron Direct Login Search Tool</title>
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; max-width: 650px; margin: 30px auto; padding: 15px; background: #f0f2f5; }
            .card { background: white; padding: 20px; border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); margin-bottom: 15px; }
            h2 { color: #0056b3; margin-top: 0; font-size: 20px; }
            h3 { color: #0056b3; margin-top: 0; font-size: 16px; margin-bottom: 15px;}
            label { font-weight: bold; font-size: 14px; display: block; margin-bottom: 5px; color: #333;}
            textarea { width: 100%; height: 140px; padding: 10px; border: 1px solid #ccc; border-radius: 6px; box-sizing: border-box; font-family: monospace; font-size: 14px; margin-bottom: 10px;}
            input[type="text"], input[type="password"], input[type="email"] { width: 100%; padding: 10px; border: 1px solid #ccc; border-radius: 6px; box-sizing: border-box; font-size: 14px; margin-bottom: 15px; }
            button { width: 100%; background: #0056b3; color: white; padding: 12px; border: none; border-radius: 6px; font-weight: bold; font-size: 16px; margin-top: 5px; cursor: pointer; transition: 0.2s;}
            button:hover { background: #004494; }
            button:disabled { background: #aaa; cursor: not-allowed; }
            .loading { display: none; margin-top: 15px; padding: 10px; background: #e8f4f8; color: #0056b3; border-radius: 6px; font-weight: bold; text-align: center; }
            .error-box { display: none; margin-top: 15px; padding: 10px; background: #fdecea; color: #b71c1c; border-radius: 6px; font-weight: bold; text-align: center; white-space: pre-wrap; }
            .success-box { display: none; margin-top: 15px; padding: 10px; background: #e6f4ea; color: #1e7e34; border-radius: 6px; font-weight: bold; text-align: center; }
        </style>
    </head>
    <body>

    <div class="card">
        <h3>🔒 1. เข้าสู่ระบบ Omron Account</h3>
        <label>Email / Username:</label>
        <input type="email" id="userInput" placeholder="อีเมลบัญชี Omron ของคุณ">
        <label>Password:</label>
        <input type="password" id="passInput" placeholder="รหัสผ่าน">
    </div>

    <div class="card">
        <h2>🔎 2. ค้นหา Part Number</h2>
        <label>ใส่ Part Number ที่ต้องการเช็ค (บรรทัดละ 1 รายการ):</label>
        <textarea id="partsInput" placeholder="CP1E-E10DR-A\nCP1E-E10DR-D\nCP1E-E10DT-D"></textarea>
        <button id="submitBtn" onclick="processSearch()">ล็อกอิน & เริ่มค้นหา</button>

        <div id="loadingBox" class="loading"></div>
        <div id="errorBox" class="error-box"></div>
        <div id="successBox" class="success-box"></div>
    </div>

    <script>
        async function processSearch() {
            const username = document.getElementById('userInput').value.trim();
            const password = document.getElementById('passInput').value.trim();
            const text = document.getElementById('partsInput').value.trim();

            if (!username || !password) return showError('กรุณากรอก Email และ Password ให้ครบถ้วน');
            if (!text) return showError('กรุณากรอก Part Number อย่างน้อย 1 รายการ');

            const btn = document.getElementById('submitBtn');
            const loading = document.getElementById('loadingBox');
            const errorBox = document.getElementById('errorBox');
            const successBox = document.getElementById('successBox');

            const partsCount = text.split('\\n').map(l => l.trim()).filter(l => l.length > 0).length;

            btn.disabled = true;
            errorBox.style.display = 'none';
            successBox.style.display = 'none';
            loading.style.display = 'block';
            loading.textContent = `⏳ กำลังล็อกอินเข้าสู่ระบบ Omron และค้นหา ${partsCount} รายการ... (อาจใช้เวลา 1-3 นาที)`;

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

                    successBox.textContent = '✓ ค้นหาและสร้างไฟล์ CSV สำเร็จ!';
                    successBox.style.display = 'block';
                } else {
                    let detail = `เกิดข้อผิดพลาด (HTTP ${response.status})`;
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


@app.post("/search")
async def process_search_endpoint(
    username: str = Form(...),
    password: str = Form(...),
    part_numbers: str = Form(...),
):
    parts_list = [p.strip() for p in part_numbers.split("\n") if p.strip()]
    if not parts_list:
        raise HTTPException(status_code=400, detail="ไม่พบข้อมูล Part Number")

    logger.info(f"เริ่มการทำงาน: Direct Login สำหรับบัญชี {username} และค้นหา {len(parts_list)} รายการ")

    async with async_playwright() as p:
        # เปิด Browser แบบ Headless
        # (หากในอนาคต Omron ตรวจจับบอทได้ อาจจะต้องเปลี่ยน headless=False แล้วรันบนเครื่อง Local แทนเซิร์ฟเวอร์)
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox", 
                "--disable-setuid-sandbox", 
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled" # ลดโอกาสถูกจับได้ว่าเป็นบอท
            ]
        )

        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800}
        )

        page = await context.new_page()

        try:
            # 1. จัดการการล็อกอิน
            await omron_direct_login(page, username, password)
            
            # 2. ทำการค้นหาพาร์ทและเก็บข้อมูล
            all_results = await search_omron_parts(page, parts_list)

        except Exception as e:
            logger.error(f"กระบวนการล้มเหลว: {e}")
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            await browser.close()

    # แปลงผลลัพธ์เป็นไฟล์ CSV
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
