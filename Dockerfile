FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

# asyncpg and pillow ship wheels for most platforms, but fall back to building
# from source on any that don't — without a compiler present that fails the
# whole deploy, and the error is a long way from the cause.
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# drop the toolchain again so it isn't carried into the running image
RUN apt-get purge -y gcc libc6-dev && apt-get autoremove -y

COPY . .

# /data is where the SQLite file lives when DATABASE_URL isn't set. On a host
# without a persistent disk, set DATABASE_URL to Postgres instead — otherwise
# the catalogue and balances reset on every redeploy.
RUN mkdir -p /data
ENV DB_PATH=/data/shop.db \
    WEBAPP_HOST=0.0.0.0 \
    WEBAPP_PORT=8080 \
    BOT_MODE=auto

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"PORT\",8080)}/health')"

CMD ["python", "bot.py"]
