# Ebay Watcher – Docker-Image
#
# Gebaut wird vom Projekt-Hauptordner aus (dort liegt build/):
#   docker build -f docker/Dockerfile .
# Ins Image kommt NUR der von CodeKit erzeugte Ordner build/app (nicht source/).
# Vorher also in CodeKit bauen und build/ mit committen.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    TZ=Europe/Berlin

WORKDIR /srv
COPY docker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY build/app ./app
COPY docker/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
 && test -f app/main.py \
 && test -f app/static/css/app-min.css \
 && test -f app/static/js/app-min.js \
 && ! grep -q '@import "tailwindcss"' app/static/css/app-min.css \
 || (echo "build/app ist unvollständig oder das CSS ist nicht übersetzt. Bitte in CodeKit bauen und build/ committen." && exit 1)

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
