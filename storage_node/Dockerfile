FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY node_app ./node_app

# Chunk storage lives on a named volume so node data survives restarts.
RUN mkdir -p /data/chunks
VOLUME ["/data"]

ENV DATA_DIR=/data

EXPOSE 9000

HEALTHCHECK --interval=20s --timeout=4s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9000/health', timeout=3).status == 200 else 1)"

CMD ["uvicorn", "node_app.main:app", "--host", "0.0.0.0", "--port", "9000"]
