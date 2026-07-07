FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Stamp the service worker cache version so every image build refreshes the
# PWA shell on installed clients (sw.js itself is served no-cache).
RUN sed -i "s/__BUILD_REV__/$(date +%s)/" static/sw.js

# Non-root user. /data is created and chowned in the image so a fresh named
# volume mounted at /data inherits this ownership (Docker seeds an empty
# named volume from the image path). A host bind mount does NOT inherit it:
# use a named volume, or chown the bind dir to uid 10001.
RUN useradd -m -u 10001 aeolus \
    && mkdir -p /data \
    && chown -R aeolus:aeolus /data /app

USER aeolus

ENV DB_PATH=/data/aeolus.db

EXPOSE 8080

# Do NOT add --preload: poller threads must live inside a worker process.
# One worker wins the flock next to the DB and runs the pollers.
# 8 threads/worker: request handling is IO-bound and the radar tile proxy
# holds a slot for up to a few seconds on cache misses.
# No access log (house convention): scrapes, healthchecks and PWA polling
# would generate tens of thousands of noise lines a day.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--threads", "8", \
     "--timeout", "60", "wsgi:app"]
