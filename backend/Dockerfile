FROM python:3.11.10-slim

# Pas de .pyc, sortie non bufferisée (logs visibles tout de suite)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd -r app -g 10001 \
 && useradd -r -u 10001 -g app -m -d /home/app app \
 && mkdir -p /app/data /app/backend \
 && chown -R app:app /app

WORKDIR /app/backend

# Installs root pour pip global
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip==24.3.1 \
 && pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app . .

ENV FRONTEND_DIR=/dev/null \
    DB_PATH=/app/data/workshop.db \
    CHEATSHEET_PATH=/app/CHEATSHEET.md \
    HOST=0.0.0.0

VOLUME ["/app/data"]

EXPOSE 8000 8001

USER app

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; \
                 sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status==200 else 1)" \
  || exit 1

CMD ["python", "server.py"]
