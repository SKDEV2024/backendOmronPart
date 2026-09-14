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

SEARCH_BASE_URL = "https://industrial.omron.eu/en/services-support/support/product-lifecycle-management"
COOKIE_DOMAIN = "industrial.omron.eu"

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


def parse_cookie_header(cookie_header: str) -> list[dict]:
    """
    แปลง Cookie header string ที่ sale copy มาจาก DevTools
    (รูปแบบ "name1=value1; name2=value2; ...")
    ให้เป็น list ของ dict ที่ Playwright context.add_cookies() ใช้ได้

    ทุก cookie ถูกผูกกับ domain/path ของเว็บ Omron เท่านั้น
    ไม่มีการเก็บ cookie นี้ไว้ที่ backend เกินอายุของ request เดียว
    """
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
async def search_omron_parts(
    part_numbers: str = Form(...),
    omron_cookie: str = Form(...),
):
    parts_list = [p.strip() for p in part_numbers.split("\n") if p.strip()]
    if not parts_list:
        raise HTTPException(status_code=400, detail="No part numbers provided")

    if not omron_cookie or not omron_cookie.strip():
        raise HTTPException(
            status_code=400,
            detail="Missing Omron session cookie - กรุณา login เข้า Omron แล้ว copy cookie มาวางก่อนค้นหา"
        )

    cookies = parse_cookie_header(omron_cookie)
    if not cookies:
        raise HTTPException(
            status_code=400,
            detail="รูปแบบ cookie ไม่ถูกต้อง - ตรวจสอบว่า copy มาจาก Network tab -> Cookie header ครบถ้วน"
        )

    logger.info(f"เริ่มค้นหา {len(parts_list)} part numbers ด้วย session ของ sale")

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

        # ใส่ cookie ของ sale เข้า context ก่อนเริ่ม browse
        # แทนที่การ login ด้วย form ซึ่งจะโดน reCAPTCHA บล็อก
        await context.add_cookies(cookies)

        page = await context.new_page()

        try:
            # เช็คก่อนว่า session ที่ได้รับมายังใช้งานได้จริง (ยังไม่หมดอายุ)
            await page.goto(SEARCH_BASE_URL, wait_until="networkidle", timeout=20000)
            login_prompt = await page.query_selector("text=Please log in or register")
            if login_prompt:
                logger.warning("Cookie ที่ได้รับมาหมดอายุ หรือไม่ถูกต้อง - session ไม่ผ่าน")
                raise HTTPException(
                    status_code=401,
                    detail="Session หมดอายุหรือไม่ถูกต้อง กรุณา login เข้า Omron ใหม่แล้ว copy cookie อีกครั้ง"
                )

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
    """เอาไว้เช็คว่า service รันอยู่หรือไม่"""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=10000)
