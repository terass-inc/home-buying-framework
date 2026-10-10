"""MCP 以外の計測用 URL。MCP サーバーと同じ Cloud Run で、HttpGuard から呼ばれる。

1. AI に読ませる前提の配信（既定 /ai/home-buying）
   dist/lite.md を text/plain で返し、読みに来た AI（ChatGPT・Claude・Perplexity など）を User-Agent で見分けて数える。
   CDN にキャッシュされると回数が取れないので no-store で返す。
2. クリック計測の転送（既定 /go/home-buying/<行き先>?src=<媒体>）
   クリックを数えてから、決めておいた行き先（ChatGPT・Claude・LP・GitHub・前提の本文）にだけ転送する。
   任意の URL には転送しない（オープンリダイレクトにしない）。

ログは1回につき1行の JSON（event・行き先・媒体・AI の種類・User-Agent）。IP アドレスは残さない。
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
from typing import Optional

from paths import ROOT

PUBLIC_BASE = os.environ.get("HBF_PUBLIC_BASE", "https://terass.house").rstrip("/")
TEXT_PATH = os.environ.get("HBF_TEXT_PATH", "/ai/home-buying")
GO_PREFIX = os.environ.get("HBF_GO_PREFIX", "/go/home-buying")
LP_URL = "https://terass-inc.github.io/home-buying-framework/"
REPO_URL = "https://github.com/terass-inc/home-buying-framework"

# User-Agent に含まれる名乗り → 集計上の名前。上から順に照合する（利用者の操作で読みに来たものを先に）
AGENTS = [
    ("chatgpt-user", "ChatGPT（利用者の操作）"), ("oai-searchbot", "ChatGPT（検索）"), ("gptbot", "OpenAI（学習用の巡回）"),
    ("claude-user", "Claude（利用者の操作）"), ("claude-searchbot", "Claude（検索）"), ("claudebot", "Anthropic（学習用の巡回）"),
    ("perplexity-user", "Perplexity（利用者の操作）"), ("perplexitybot", "Perplexity（検索）"),
    ("mistralai-user", "Mistral（利用者の操作）"), ("duckassistbot", "DuckDuckGo AI"),
    ("google-extended", "Google（AI）"), ("googleother", "Google（その他）"), ("googlebot", "Google（検索）"),
    ("bingbot", "Bing"), ("applebot", "Apple"), ("meta-externalagent", "Meta"), ("bytespider", "ByteDance"),
]
_SRC_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


def classify(user_agent: str) -> str:
    ua = (user_agent or "").lower()
    for token, name in AGENTS:
        if token in ua:
            return name
    if not ua:
        return "不明"
    if any(b in ua for b in ("bot", "crawler", "spider", "python", "curl", "httpx", "go-http", "java/")):
        return "その他の自動取得"
    return "ブラウザ"


def clean_src(query: bytes) -> Optional[str]:
    src = (urllib.parse.parse_qs(query.decode("latin-1")).get("src") or [""])[0].lower()
    return src if _SRC_RE.match(src) else ("other" if src else None)


def text_url(src: str) -> str:
    return f"{PUBLIC_BASE}{TEXT_PATH}?src={src}"


def _prompt(src: str) -> str:
    """ChatGPT・Claude を開くボタン用の文（scripts/make_button_urls.py の fetch 版と同じ考え方）。"""
    return f"""まず次のURLの前提を読み、その前提で住宅購入の相談に答えてください。
{text_url(src)}

URLを実際に読めた場合だけ「完全版を読み込みました」と言い、読めなかった場合は読めたふりをせず「要点版で進めます」と言ってから、最低限次の5つを守ってください。
1. 結論より先に「なぜ買うのか」を掘り（「家賃がもったいない」は理由ではない）、次に「何年住む予定か（売却時期）」を確認する
2. ローン期間は利息総額でなく、売却時の残債と手残りで考える。原則は組める最長で組み、短縮は繰上返済で行う
3. 「借りられる額」（年収7〜8倍）と「借りるべき額」（ライフプランから逆算）を混同しない
4. 賃貸と購入の比較は、購入側に売却後の手残りと修繕積立金の上昇、賃貸側に更新料と住み替え時の初期費用を入れる
5. 市況の予測とエリアの優劣は断言しない

準備ができたら、要約や復唱をせず「前提を読み込みました。まず、家を買おうと思ったきっかけを教えてください」の1文と選択肢だけを返して会話を始めてください。確認事項は1つずつ、選択肢（最後は自由記述）を添えて聞いてください。
"""


def destination(target: str, src: str) -> Optional[str]:
    """行き先は決め打ち。ここにないものは 404。"""
    q = urllib.parse.quote
    return {
        "chatgpt": "https://chatgpt.com/?q=" + q(_prompt(f"chatgpt-{src}"), safe=""),
        "claude": "https://claude.ai/new?q=" + q(_prompt(f"claude-{src}"), safe=""),
        "lp": f"{LP_URL}?utm_source={q(src)}",
        "github": REPO_URL,
        "text": text_url(src),
    }.get(target)


def _log(event: str, **fields) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


_lite_cache: Optional[bytes] = None


def lite_bytes() -> bytes:
    global _lite_cache
    if _lite_cache is None:
        _lite_cache = (ROOT / "dist" / "lite.md").read_bytes()
    return _lite_cache


def handle(scope, user_agent: str):
    """計測用 URL なら (status, headers, body) を返す。対象外なら None。"""
    path = scope["path"].rstrip("/")
    src = clean_src(scope.get("query_string", b""))
    if path == TEXT_PATH:
        _log("hbf_fetch", agent=classify(user_agent), src=src, user_agent=(user_agent or "")[:200])
        return 200, [(b"content-type", b"text/plain; charset=utf-8")], lite_bytes()
    if path.startswith(GO_PREFIX + "/"):
        target = path[len(GO_PREFIX) + 1:]
        dest = destination(target, src or "direct")
        if dest is None:
            return 404, [(b"content-type", b"application/json")], b'{"error": "not found"}'
        _log("hbf_click", target=target, src=src, agent=classify(user_agent), user_agent=(user_agent or "")[:200])
        return 302, [(b"location", dest.encode("ascii")), (b"content-type", b"text/plain; charset=utf-8")], b""
    return None
