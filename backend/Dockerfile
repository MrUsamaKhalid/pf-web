# Official Playwright Python image ships Chromium + all system libraries it
# needs, which is the reliable way to run Playwright on Render's free tier.
FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Make sure the Chromium build matching the installed Playwright is present.
RUN playwright install chromium

COPY . .

# Render injects $PORT at runtime; default to 10000 for local `docker run`.
ENV PORT=10000
EXPOSE 10000

CMD gunicorn app:app --bind 0.0.0.0:$PORT --timeout 120 --workers 1 --threads 2
