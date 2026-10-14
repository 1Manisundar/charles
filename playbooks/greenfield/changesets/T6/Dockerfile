FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    URLSHORTENER_DB_PATH=/data/urlshortener.db

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY urlshortener ./urlshortener

# Run unprivileged; only /data is writable.
RUN useradd --system --uid 10001 app && mkdir /data && chown app /data
USER app
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"

CMD ["python", "-m", "uvicorn", "urlshortener.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
