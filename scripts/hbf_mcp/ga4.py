"""計測ログを GA4 にも送る（Measurement Protocol）。

AI が前提を読みに来たときはブラウザを通らないので、GTM・gtag では数えられない。
そこでサーバーが記録するたびに、同じ中身を GA4 のイベントとして送る。

- 環境変数 GA4_MEASUREMENT_ID（G-…）と GA4_API_SECRET が両方あるときだけ送る。なければ何もしない
- 送るのはイベント名と、行き先・媒体・AI の種類・道具の名前だけ。IP アドレス・User-Agent・相談の中身は送らない
- client_id は毎回ランダム（人を追いかけない）。GA4 の「ユーザー数」は意味を持たないので、イベント数で見る
- 応答を遅らせないよう別スレッドで送り、失敗しても無視する（計測の失敗で相談を止めない）
"""
from __future__ import annotations

import json
import os
import threading
import urllib.parse
import urllib.request
import uuid

ENDPOINT = "https://www.google-analytics.com/mp/collect"
# GA4 の制限: パラメータ値は100文字まで、名前は40文字まで
_MAX_VALUE = 100


def _config() -> tuple[str, str] | None:
    mid, secret = os.environ.get("GA4_MEASUREMENT_ID", ""), os.environ.get("GA4_API_SECRET", "")
    return (mid, secret) if mid and secret else None


def payload(event: str, params: dict) -> dict:
    clean = {k[:40]: str(v)[:_MAX_VALUE] for k, v in params.items() if v is not None and v != ""}
    clean["engagement_time_msec"] = 1  # これがないと GA4 の一部レポートに出ない
    return {"client_id": f"{uuid.uuid4().int % 10**10}.{uuid.uuid4().int % 10**10}",
            "non_personalized_ads": True,
            "events": [{"name": event, "params": clean}]}


def _post(url: str, body: bytes) -> None:
    try:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=3).close()
    except Exception:  # noqa: BLE001
        pass


def send(event: str, **params) -> None:
    cfg = _config()
    if cfg is None:
        return
    mid, secret = cfg
    url = f"{ENDPOINT}?{urllib.parse.urlencode({'measurement_id': mid, 'api_secret': secret})}"
    body = json.dumps(payload(event, params), ensure_ascii=False).encode("utf-8")
    threading.Thread(target=_post, args=(url, body), daemon=True).start()
