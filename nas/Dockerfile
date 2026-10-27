# 빌드 위치(context)는 저장소 최상위 폴더입니다. (docker-compose.yml 이 알아서 지정)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/tools/kidsnote_fetch:/app/nas \
    TZ=Asia/Seoul \
    DATA_DIR=/data \
    PORT=8765

RUN pip install --no-cache-dir "requests>=2.31" tzdata

WORKDIR /app
COPY tools/kidsnote_fetch/ /app/tools/kidsnote_fetch/
COPY nas/collector.py nas/server.py /app/nas/

VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=5m --timeout=10s --start-period=30s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8765/health',timeout=5)" || exit 1

CMD ["python", "/app/nas/server.py"]
