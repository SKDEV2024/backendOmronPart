import os
import io
import logging
import asyncio
import pandas as pd
from fastapi import FastAPI, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from playwright.async_api import async_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("omron-part-search")

app = FastAPI(title="Omron Part Lifecycle Exporter")

# อนุญาตให้ Frontend (Cloudflare Pages) ดึง API ได้ (แก้ปัญหา CORS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://omron-part.pages.dev"],  # ระบุ origin ตรงๆ แทน "*" เพื่อความปลอดภัย
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# credential ต้องมาจาก Environment Variable เท่านั้น ไม่มีค่า default
# ตั้งค่าที่ Render Dashboard -> Settings -> Environment
OMRON_USERNAME = os.getenv("OMRON_USER")
OMRON_PASSWORD = os.getenv("OMRON_PASS")

LOGIN_URL = "https://industrial.omron.eu/en/login"
SEARCH_BASE_URL = "https://industrial.omron.eu/en/services-support/support/product-lifecycle-management"

# ปรับได้ตามความเหมาะสม - ดีเลย์ระหว่าง request แต่ละ part number (วินาที)
MIN_DELAY_SEC = 2.0
MAX_DELAY_SEC = 4.0


def _empty_result(part: str, target_url: str, status_label: str) -> dict:
    """สร้าง dict ผลลัพธ์ที่ไม่พบข้อมูล/error ให้ shape เดียวกับผลลัพธ์ที่พบ"""
    return {
        "Search Input": part,
        "Part Number": status_label,
        "Status": "-",
        "Possible Replacement": "-",
        "Discontinuation Date": "-",
        "Source URL": target_url,
    }


async def login_omron(page) -> bool:
    """
    Login เข้าเว็บ Omron ก่อนเริ่ม search
    คืนค่า True หาก login สำเร็จ (หรือไม่จำเป็นต้อง login ซ้ำ), False หากผิดพลาดชัดเจน
    """
    if not OMRON_USERNAME or not OMRON_PASSWORD:
        logger.error("OMRON_USER / OMRON_PASS ไม่ได้ตั้งค่าใน environment variables")
        raise HTTPException(
            status_code=500,
            detail="Server misconfiguration: missing Omron credentials in environment"
        )

    try:
        await page.goto(LOGIN_URL, timeout=25000)

        user_input = await page.query_selector(
            'input[type="email"], input[name="username"], #username'
        )
        pass_input = await page.query_selector('input[type="password"], #password')

        if not user_input or not pass_input:
            logger.warning("ไม่พบช่อง login form - อาจ redirect ไปหน้าอื่น หรือ login ค้างอยู่แล้ว")
            return False

        await user_input.fill(OMRON_USERNAME)
        await pass_input.fill(OMRON_PASSWORD)

        submit_btn = await page.query_selector('button[type="submit"], input[type="submit"]')
        if not submit_btn:
            logger.warning("ไม่พบปุ่ม submit บนฟอร์ม login")
            return False

        await submit_btn.click()
        await page.wait_for_load_state("networkidle", timeout=15000)
        logger.info("Login สำเร็จ (หรืออย่างน้อยฟอร์ม submit ผ่านแล้ว)")
        return True

    except Exception as e:
        logger.warning(f"Login ล้มเหลว/ข้ามขั้นตอน: {e}")
        return False


async def search_single_part(page, part: str) -> dict:
    """ค้นหา part number เดียว คืนค่าผลลัพธ์เป็น dict เดียว (แถวแรกที่พบ)"""
    target_url = f"{SEARCH_BASE_URL}?search={part}"

    try:
        await page.goto(target_url, wait_until="networkidle", timeout=20000)

        rows = await page.query_selector_all("table tbody tr")

        if not rows:
            logger.info(f"[{part}] ไม่พบผลลัพธ์ในตาราง")
            return _empty_result(part, target_url, "Not Found")

        row = rows[0]
        cols = await row.query_selector_all("td")

        if len(cols) < 4:
            logger.warning(f"[{part}] พบแถวผลลัพธ์ แต่จำนวนคอลัมน์ไม่ตรงตามที่คาด ({len(cols)} คอลัมน์)")
            return _empty_result(part, target_url, "Unexpected Table Format")

        p_num = (await cols[0].text_content() or "").strip()
        status = (await cols[1].text_content() or "").strip()
        replacement = (await cols[2].text_content() or "").strip()
        disco_date = (await cols[3].text_content() or "").strip()

        logger.info(f"[{part}] พบข้อมูล: {p_num} / {status}")
        return {
            "Search Input": part,
            "Part Number": p_num,
            "Status": status,
            "Possible Replacement": replacement,
            "Discontinuation Date": disco_date,
            "Source URL": target_url,
        }

    except Exception as e:
        logger.error(f"[{part}] เกิดข้อผิดพลาด: {e}")
        return _empty_result(part, target_url, "Error / Timeout")


@app.post("/search")
async def search_omron_parts(part_numbers: str = Form(...)):
    parts_list = [p.strip() for p in part_numbers.split("\n") if p.strip()]
    if not parts_list:
        raise HTTPException(status_code=400, detail="No part numbers provided")

    logger.info(f"เริ่มค้นหา {len(parts_list)} part numbers")

    results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()

        try:
            await login_omron(page)

            for idx, part in enumerate(parts_list):
                result = await search_single_part(page, part)
                results.append(result)

                # หน่วงเวลาแบบสุ่มระหว่าง request เพื่อลด pattern ที่ดูเป็นบอทเกินไป
                # ข้ามการหน่วงหลัง part number สุดท้าย
                if idx < len(parts_list) - 1:
                    delay = MIN_DELAY_SEC + (MAX_DELAY_SEC - MIN_DELAY_SEC) * os.urandom(1)[0] / 255
                    await asyncio.sleep(delay)

        finally:
            await browser.close()

    logger.info(f"ค้นหาเสร็จสิ้น: {len(results)} รายการ")

    df = pd.DataFrame(results)
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
    """เอาไว้เช็คว่า service รันอยู่ และ credential ถูกตั้งค่าไว้หรือยัง (ไม่เปิดเผยค่าจริง)"""
    return {
        "status": "ok",
        "credentials_configured": bool(OMRON_USERNAME and OMRON_PASSWORD),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=10000)
