"""URL で公開するときの HTTP の守り。MCP の ASGI アプリの外側に巻く。

- 接続元ごとの回数制限（1分あたり RATE_PER_MIN 回。超えたら 429）。インスタンスごとの近似値だが、
  台数の上限（Cloud Run の max-instances）と組み合わせて、全体の負荷と費用の上限を決める
- 受け付けるメソッドとパスを限定する（/mcp の POST と、仕様上の GET・DELETE だけ）
- 応答にキャッシュ禁止とセキュリティ系のヘッダーを付ける（CDN や中継にも相談の中身を残させない）
"""
from __future__ import annotations

import json
import os
import time
from collections import defaultdict, deque

RATE_PER_MIN = int(os.environ.get("HBF_RATE_PER_MIN", "60"))
_SECURITY_HEADERS = [
    (b"cache-control", b"no-store"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
    (b"strict-transport-security", b"max-age=31536000"),
]


def _client_ip(scope) -> str:
    headers = dict(scope.get("headers") or [])
    # Firebase Hosting・Cloud Run を経由すると、元の接続元は X-Forwarded-For の先頭に入る
    fwd = headers.get(b"x-forwarded-for", b"").decode("latin-1").split(",")[0].strip()
    if fwd:
        return fwd
    client = scope.get("client")
    return client[0] if client else "unknown"


class HttpGuard:
    def __init__(self, app, path: str, rate_per_min: int = RATE_PER_MIN, clock=time.monotonic):
        self.app, self.path, self.rate, self.clock = app, path.rstrip("/") or "/", rate_per_min, clock
        self.hits: dict[str, deque] = defaultdict(deque)

    async def _reject(self, send, status: int, message: str, extra=()):
        body = json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json; charset=utf-8"), *extra, *_SECURITY_HEADERS]})
        await send({"type": "http.response.body", "body": body})

    def _limited(self, ip: str) -> bool:
        now, q = self.clock(), self.hits[ip]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= self.rate:
            return True
        q.append(now)
        if len(self.hits) > 50_000:  # 記録が増えすぎたら古い接続元から捨てる
            for k in list(self.hits)[:10_000]:
                del self.hits[k]
        return False

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope["path"].rstrip("/") != self.path:
            return await self._reject(send, 404, "not found")
        if scope["method"] not in ("POST", "GET", "DELETE"):
            return await self._reject(send, 405, "method not allowed", [(b"allow", b"POST, GET, DELETE")])
        if self._limited(_client_ip(scope)):
            return await self._reject(send, 429, "too many requests", [(b"retry-after", b"60")])

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), *_SECURITY_HEADERS]}
            await send(message)

        return await self.app(scope, receive, send_with_headers)
