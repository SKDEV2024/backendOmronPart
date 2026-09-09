import os
import io
import pandas as pd
from fastapi import FastAPI, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from playwright.async_api import async_playwright

app = FastAPI(title="Omron Part Lifecycle Exporter")

# อนุญาตให้ Frontend (Cloudflare Pages) ดึง API ได้ (แก้ปัญหา CORS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # หรือใส่เฉพาะ "https://omron-part.pages.dev"
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ดึง Username / Password จาก Environment Variable (หากไม่มีจะใช้ค่า Default)
OMRON_USERNAME = os.getenv("OMRON_USER", "sarawut.khumthong@omron.com")
OMRON_PASSWORD = os.getenv("OMRON_PASS", "Wearemce@2026a")

@app.post("/search")
async def search_omron_parts(part_numbers: str = Form(...)):
    # แปลงข้อมูลจาก Textarea บรรทัดละ 1 Part Number
    parts_list = [p.strip() for p in part_numbers.split("\n") if p.strip()]
    if not parts_list:
        raise HTTPException(status_code=400, detail="No part numbers provided")

    results = []

    async with async_playwright() as p:
        # เปิด Chromium แบบ Headless บน Server
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        # --- 1. ขั้นตอน Auto-Login เข้าสู่ระบบ Omron ---
        try:
            await page.goto("https://industrial.omron.eu/en/login", timeout=25000)
            
            # ตรวจสอบว่ามีช่องกรอก Username/Password หรือไม่
            user_input = await page.query_selector('input[type="email"], input[name="username"], #username')
            pass_input = await page.query_selector('input[type="password"], #password')

            if user_input and pass_input:
                await user_input.fill(OMRON_USERNAME)
                await pass_input.fill(OMRON_PASSWORD)
                
                # กดปุ่ม Submit Login
                submit_btn = await page.query_selector('button[type="submit"], input[type="submit"]')
                if submit_btn:
                    await submit_btn.click()
                    await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception as e:
            # หาก Login ไม่สำเร็จหรือข้ามหน้าไป ให้ดำเนินการ Search ต่อไป
            print(f"Login Note/Warning: {e}")

        # --- 2. วนลูป Search Part Numbers ตามรายการ ---
        for part in parts_list:
            target_url = f"https://industrial.omron.eu/en/services-support/support/product-lifecycle-management?search={part}"
            try:
                await page.goto(target_url, wait_until="networkidle", timeout=20000)
                
                # ดึงข้อมูลจากตาราง ผลลัพธ์ Lifecycle Management
                rows = await page.query_selector_all("table tbody tr")
                
                if rows:
                    for row in rows:
                        cols = await row.query_selector_all("td")
                        if len(cols) >= 4:
                            p_num = (await cols[0].text_content()).strip()
                            status = (await cols[1].text_content()).strip()
                            replacement = (await cols[2].text_content()).strip()
                            disco_date = (await cols[3].text_content()).strip()

                            results.append({
                                "Search Input": part,
                                "Part Number": p_num,
                                "Status": status,
                                "Possible Replacement": replacement,
                                "Discontinuation Date": disco_date,
                                "Source URL": target_url
                            })
                else:
                    results.append({
                        "Search Input": part,
                        "Part Number": "Not Found",
                        "Status": "-",
                        "Possible Replacement": "-",
                        "Discontinuation Date": "-",
                        "Source URL": target_url
                    })
            except Exception as e:
                results.append({
                    "Search Input": part,
                    "Part Number": "Error / Timeout",
                    "Status": "-",
                    "Possible Replacement": "-",
                    "Discontinuation Date": "-",
                    "Source URL": target_url
                })

        await browser.close()

    # --- 3. สร้างไฟล์ CSV ส่งกลับให้ Browser ของ User ---
    df = pd.DataFrame(results)
    stream = io.StringIO()
    # encoding utf-8-sig เพื่อรองรับการเปิดไฟล์บน MS Excel ใน Windows
    df.to_csv(stream, index=False, encoding='utf-8-sig')
    
    response = StreamingResponse(
        iter([stream.getvalue()]),
        media_type="text/csv"
    )
    response.headers["Content-Disposition"] = "attachment; filename=omron_lifecycle_report.csv"
    return response

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=10000)