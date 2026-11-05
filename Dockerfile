# 行動記録メモ バックエンド（Python標準ライブラリのみ / SQLite）
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    TRUST_PROXY=1

# root以外のユーザーで実行。/data は名前付きボリュームの初期所有者になる
RUN useradd --system --uid 10001 --no-create-home app \
 && mkdir -p /data && chown app:app /data

WORKDIR /app
COPY server.py auth.py pdf_report.py index.html login.html users.html ./

USER app
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"

# DBは /data/activity_log.db（名前付きボリュームで永続化）
CMD ["python", "server.py", "--db", "/data/activity_log.db"]
