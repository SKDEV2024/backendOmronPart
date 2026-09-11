# ใช้ official Playwright image ที่มี Chromium + system dependencies ครบอยู่แล้ว
# เวอร์ชัน Python image ต้องตรงกับเวอร์ชัน playwright ใน requirements.txt
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ไม่ต้อง apt-get install libraries เองแล้ว เพราะ base image มีครบ
# ไม่ต้อง playwright install-deps แล้ว เพราะ base image ติดตั้งไว้ให้แล้ว
RUN playwright install chromium

COPY . .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "10000"]
