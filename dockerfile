# Uses the official Playwright image which has Chromium pre-installed
FROM mcr.microsoft.com/playwright/python:v1.50.0-noble

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright browsers are already in the base image, but run install just in case
RUN playwright install chromium

COPY main.py .

CMD ["python", "main.py"]
