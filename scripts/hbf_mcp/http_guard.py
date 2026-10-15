"""URL で公開するときの HTTP の守り。MCP の ASGI アプリの外側に巻く。

- 接続元ごとの回数制限（1分あたり RATE_PER_MIN 回。超えたら 429）。インスタンスごとの近似値だが、
  台数の上限（Cloud Run の max-instances）と組み合わせて、全体の負荷と費用の上限を決める
- 受け付けるメソッドとパスを限定する（MCP は POST と仕様上の GET・DELETE、計測用 URL は GET・HEAD だけ）
- 応答にキャッシュ禁止とセキュリティ系のヘッダーを付ける（CDN や中継にも相談の中身を残させない）
"""
from __future__ import annotations

import json
import os
import time
from collections import defaultdict, deque
from typing import Optional

import measure

RATE_PER_MIN = int(os.environ.get("HBF_RATE_PER_MIN", "60"))
# X-Forwarded-For の末尾から何番目を接続元とみなすか。末尾側は経由した基盤が付け足すので利用者には書き換えられない。
# 先頭側は利用者が自由に書けるので使わない。Cloud Run に直接: 1（Google のフロントエンドが付けた値）、
# Firebase Hosting 経由: 2（Firebase が付けた利用者の値。その後ろに Google のフロントエンドが Firebase の値を付ける）
TRUSTED_PROXY_HOPS = int(os.environ.get("HBF_TRUSTED_PROXY_HOPS", "1"))
# 経由した基盤（Firebase Hosting など）ごとの上限。末尾の値は Google のフロントエンドが付けるので偽装できない。
# 利用者ごとの上限（先頭側の値）は偽装されうるので、こちらで直接アクセスの総量を抑える。
PROXY_RATE_PER_MIN = int(os.environ.get("HBF_PROXY_RATE_PER_MIN", "600"))
# Firebase Hosting の転送では Host が *.run.app になり、元のドメインは X-Forwarded-Host に入る（2026-10-14 に確認）。
# 1 にすると、Host が *.run.app で X-Forwarded-Host が許可したドメインのときに、元のドメインとして扱う。
TRUST_FORWARDED_HOST = os.environ.get("HBF_TRUST_FORWARDED_HOST") == "1"
# 初回デプロイの確認用。転送の形（Host・X-Forwarded-Host・X-Forwarded-For の件数）だけをログに出す
DEBUG_FORWARDING = os.environ.get("HBF_DEBUG_FORWARDING") == "1"
_SECURITY_HEADERS = [
    (b"cache-control", b"no-store"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
    (b"strict-transport-security", b"max-age=31536000"),
]


def _client_ip(scope, hops: int = TRUSTED_PROXY_HOPS) -> str:
    headers = dict(scope.get("headers") or [])
    parts = [p.strip() for p in headers.get(b"x-forwarded-for", b"").decode("latin-1").split(",") if p.strip()]
    if hops > 0 and len(parts) >= hops:
        return parts[-hops]
    client = scope.get("client")
    return client[0] if client else "unknown"


class HttpGuard:
    def __init__(self, app, path: str, rate_per_min: int = RATE_PER_MIN, clock=time.monotonic,
                 trusted_proxy_hops: int = TRUSTED_PROXY_HOPS, allowed_hosts: Optional[list[str]] = None,
                 trust_forwarded_host: bool = TRUST_FORWARDED_HOST, proxy_rate_per_min: int = PROXY_RATE_PER_MIN):
        self.app, self.path, self.rate, self.clock = app, path.rstrip("/") or "/", rate_per_min, clock
        self.hops = trusted_proxy_hops
        self.allowed_hosts = allowed_hosts  # None なら検査しない（MCP 側は SDK も検査する）
        self.hits: dict[str, deque] = defaultdict(deque)
        self.trust_forwarded_host, self.proxy_rate = trust_forwarded_host, proxy_rate_per_min

    def _host_ok(self, scope) -> bool:
        """計測用 URL も MCP と同じく、許可したホスト（terass.house 系）経由だけを受け付ける。
        *.run.app に直接来たアクセスを断ることで、回数制限の接続元の決め方と計測の数字を Firebase 経由に揃える。"""
        if not self.allowed_hosts:
            return True
        host = self._effective_host(scope)
        for pat in self.allowed_hosts:
            if host == pat or (pat.endswith(":*") and host.split(":")[0] == pat[:-2]):
                return True
        return False

    async def _reject(self, send, status: int, message: str, extra=()):
        body = json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json; charset=utf-8"), *extra, *_SECURITY_HEADERS]})
        await send({"type": "http.response.body", "body": body})

    def _effective_host(self, scope) -> str:
        h = dict(scope.get("headers") or [])
        host = h.get(b"host", b"").decode("latin-1")
        fwd = h.get(b"x-forwarded-host", b"").decode("latin-1").split(",")[0].strip()
        if self.trust_forwarded_host and fwd and host.split(":")[0].endswith(".run.app"):
            return fwd
        return host

    def _limited(self, ip: str, limit: Optional[int] = None) -> bool:
        now, q = self.clock(), self.hits[ip]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= (limit or self.rate):
            return True
        q.append(now)
        if len(self.hits) > 50_000:  # 記録が増えすぎたら古い接続元から捨てる
            for k in list(self.hits)[:10_000]:
                del self.hits[k]
        return False

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"].rstrip("/")
        is_mcp = path == self.path
        is_measure = path == measure.TEXT_PATH or path.startswith(measure.GO_PREFIX + "/")
        if not (is_mcp or is_measure):
            return await self._reject(send, 404, "not found")
        if is_measure and not self._host_ok(scope):
            return await self._reject(send, 421, "misdirected request")
        allowed = ("POST", "GET", "DELETE") if is_mcp else ("GET", "HEAD")
        if scope["method"] not in allowed:
            return await self._reject(send, 405, "method not allowed", [(b"allow", ", ".join(allowed).encode())])
        if DEBUG_FORWARDING:
            h = dict(scope.get("headers") or [])
            xff = [x for x in h.get(b"x-forwarded-for", b"").split(b",") if x.strip()]
            print(json.dumps({"event": "hbf_forwarding_debug", "host": h.get(b"host", b"").decode("latin-1"),
                              "x_forwarded_host": h.get(b"x-forwarded-host", b"").decode("latin-1") or None,
                              "x_forwarded_for_entries": len(xff)}), flush=True)  # IP の値そのものは出さない
        last_hop = "hop:" + _client_ip(scope, 1)  # 末尾は偽装できない（直接アクセスなら本人、転送なら基盤）
        if self._limited(_client_ip(scope, self.hops)) or (self.hops > 1 and self._limited(last_hop, self.proxy_rate)):
            return await self._reject(send, 429, "too many requests", [(b"retry-after", b"60")])
        if self.trust_forwarded_host:  # 以降（MCP の SDK のホスト検査を含む）は元のドメインとして扱う
            eff = self._effective_host(scope).encode("latin-1")
            scope = {**scope, "headers": [(k, eff if k == b"host" else v) for k, v in scope.get("headers") or []]}

        if is_measure:
            ua = dict(scope.get("headers") or []).get(b"user-agent", b"").decode("latin-1")
            status, headers, body = measure.handle(scope, ua)
            await send({"type": "http.response.start", "status": status, "headers": [*headers, *_SECURITY_HEADERS]})
            await send({"type": "http.response.body", "body": b"" if scope["method"] == "HEAD" else body})
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), *_SECURITY_HEADERS]}
            await send(message)

        return await self.app(scope, receive, send_with_headers)
