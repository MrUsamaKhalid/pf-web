# Plain slim Python: this service is a JSON reader and a ZIP writer, with no
# browser on any code path. It previously used the Playwright image (~1.7 GB)
# for a fallback that never fired and that could not have fitted in Render
# Starter's 512 MB alongside gunicorn anyway.
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render injects $PORT at runtime; default to 10000 for local `docker run`.
ENV PORT=10000
ENV PYTHONUNBUFFERED=1
EXPOSE 10000

# One worker keeps the download-token store and the rate limiter in a single
# process; threads cover the streaming responses.
CMD gunicorn app:app --bind 0.0.0.0:$PORT --timeout 180 --workers 1 --threads 4
