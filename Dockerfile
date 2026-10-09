# MCP サーバーを URL で公開するためのコンテナ（Cloud Run など）。
# 起動時に HBF_ALLOWED_HOSTS へ公開するドメインを入れる（例: terass.house）。
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PORT=8080
WORKDIR /app
# 依存はバージョンを固定したロックファイルから入れる（ビルドのたびに中身が変わらないように）
COPY scripts/hbf_mcp/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt
COPY . .
RUN pip install --no-deps --no-build-isolation . && rm -rf /app
# root で動かさない
RUN useradd --system --uid 10001 --no-create-home hbf
USER 10001
WORKDIR /tmp
CMD ["sh", "-c", "exec hbf-mcp --http --host 0.0.0.0 --port ${PORT}"]
