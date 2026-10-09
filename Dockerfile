# MCP サーバーを URL で公開するためのコンテナ（Cloud Run など）。
# 起動時に HBF_ALLOWED_HOSTS へ公開するドメインを入れる（例: mcp.example.com）。
FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir . && rm -rf /root/.cache
ENV PORT=8080
CMD ["sh", "-c", "hbf-mcp --http --host 0.0.0.0 --port ${PORT}"]
