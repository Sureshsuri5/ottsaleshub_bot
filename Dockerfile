FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# /data is where the SQLite file lives. Mount a persistent volume here in
# production; without one the catalogue and orders reset on every redeploy.
RUN mkdir -p /data
ENV DB_PATH=/data/shop.db \
    WEBAPP_HOST=0.0.0.0 \
    WEBAPP_PORT=8080 \
    BOT_MODE=auto

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"PORT\",8080)}/health')"

CMD ["python", "bot.py"]
