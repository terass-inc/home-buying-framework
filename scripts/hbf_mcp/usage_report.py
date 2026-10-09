#!/usr/bin/env python3
"""MCP サーバーの利用状況を Cloud Logging から集計する。

サーバーは呼び出しごとに {"event": "hbf_mcp_request", "method", "tool", "client", "user_agent", "ok", "ms"} を
1行出す（相談の中身は含まない）。これを日別・道具別・AIアプリ別に数える。

使い方:
    gcloud logging read 'jsonPayload.event="hbf_mcp_request"' --project terass-house --freshness 30d --format json \\
      | python3 scripts/hbf_mcp/usage_report.py
"""
import collections
import json
import sys


def app_of(rec: dict) -> str:
    ua = (rec.get("user_agent") or "").lower()
    for key, name in (("claude-user", "Claude"), ("claude", "Claude"), ("chatgpt", "ChatGPT"), ("openai", "ChatGPT"),
                      ("cursor", "Cursor"), ("python-httpx", "その他（プログラム）")):
        if key in ua:
            return name
    return rec.get("client") or "不明"


def main() -> None:
    entries = json.load(sys.stdin)
    recs = []
    for e in entries:
        p = e.get("jsonPayload") or {}
        if p.get("event") == "hbf_mcp_request":
            p["date"] = (e.get("timestamp") or "")[:10]
            recs.append(p)
    calls = [r for r in recs if r.get("method") == "tools/call"]
    starts = [r for r in calls if r.get("tool") == "start_consultation"]
    print(f"# MCP サーバーの利用状況（{min((r['date'] for r in recs), default='-')} 〜 {max((r['date'] for r in recs), default='-')}）\n")
    print(f"- 道具の呼び出し: {len(calls):,}回（失敗 {sum(not r.get('ok') for r in calls):,}回）")
    print(f"- 相談の開始: {len(starts):,}件")
    print(f"- 診断の更新: {sum(r.get('tool') == 'update_facts' for r in calls):,}回、もしも: {sum(r.get('tool') == 'what_if' for r in calls):,}回\n")
    for title, key in (("道具別", lambda r: r.get("tool")), ("AIアプリ別", app_of)):
        print(f"## {title}\n\n| 項目 | 回数 |\n|---|---|")
        for k, n in collections.Counter(map(key, calls)).most_common():
            print(f"| {k} | {n:,} |")
        print()
    print("## 日別（相談の開始）\n\n| 日付 | 件数 |\n|---|---|")
    for d, n in sorted(collections.Counter(r["date"] for r in starts).items()):
        print(f"| {d} | {n:,} |")


if __name__ == "__main__":
    main()
