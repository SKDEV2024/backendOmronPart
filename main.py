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

# อนุญาตให้ Frontend ยิง API เข้ามาได้
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # เปิดรับทุก Domain เพื่อป้องกันปัญหา CORS
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SEARCH_BASE_URL = "https://industrial.omron.eu/en/services-support/support/product-lifecycle-management"
COOKIE_DOMAIN = "industrial.omron.eu"

MIN_DELAY_SEC = 2.0
MAX_DELAY_SEC = 4.0


def _empty_result(part: str, target_url: str, status_label: str) -> list[dict]:
    """สร้าง list ของ dict ผลลัพธ์กรณีไม่พบข้อมูลหรือเกิด Error"""
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
    """ค้นหา part number และดึงข้อมูลทุกแถวที่พบกลับมา"""
    target_url = SEARCH_BASE_URL

    try:
        # เปิดไปยังหน้า Lifecycle Search
        await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)

        # รอช่อง Search Box แสดงผล
        search_input_selector = "input[type='search'], input[placeholder*='search' i], input.form-control"
        await page.wait_for_selector(search_input_selector, timeout=15000)
        
        # ล้างข้อมูลเก่าและพิมพ์คำค้นหาใหม่
        search_box = page.locator(search_input_selector).first
        await search_box.fill("")
        await search_box.fill(part)
        await search_box.press("Enter")

        # รอให้ตารางผลลัพธ์โหลดข้อมูล (รออย่างน้อย 3 วินาทีเพื่อให้ AJAX ทำงานเสร็จ)
        await page.wait_for_timeout(3000)
        
        # ดึงแถวข้อมูลทั้งหมดในตาราง
        rows = await page.query_selector_all("table tbody tr")

        if not rows:
            logger.info(f"[{part}] ไม่พบผลลัพธ์ในตาราง")
            return _empty_result(part, target_url, "Not Found")

        part_results = []
        for row in rows:
            cols = await row.query_selector_all("td")
            if len(cols) >= 4:
                p_num = (await cols[0].text_content() or "").strip()
                status = (await cols[1].text_content() or "").strip()
                replacement = (await cols[2].text_content() or "").strip()
                disco_date = (await cols[3].text_content() or "").strip()

                # ข้ามแถวที่เป็นข้อความแจ้งเตือน "No results found"
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

        if not part_results:
            return _empty_result(part, target_url, "Not Found")

        logger.info(f"[{part}] พบข้อมูลจำนวน {len(part_results)} รายการ")
        return part_results

    except Exception as e:
        logger.error(f"[{part}] เกิดข้อผิดพลาด: {e}")
        return _empty_result(part, target_url, f"Error: {str(e)[:50]}")


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

    logger.info(f"ค้นหาเสร็จสิ้น ได้รับรวม {len(all_results)} แถว")

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