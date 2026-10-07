#!/usr/bin/env python3
"""GitHub のトラフィック（閲覧数・クローン数・流入元・よく見られたページ）を CSV に積み上げる。

GitHub はトラフィックを直近14日分しか保持しないため、毎日取得して履歴を残す。
.github/workflows/traffic.yml から毎日実行し、結果は traffic-data ブランチに保存する。

出力（--out で指定したディレクトリ）:
- views.csv      日別の閲覧数（date,count,uniques）。同じ日付は最新の取得値で上書きする
- clones.csv     日別のクローン数（同上）
- referrers.csv  取得日時点の直近14日の流入元（date,referrer,count,uniques）
- paths.csv      取得日時点の直近14日のよく見られたページ（date,path,count,uniques）
- repo.csv       取得日のスター数・フォーク数・ウォッチ数
- SUMMARY.md     累計と直近の内訳

トラフィック API にはリポジトリの Administration: read 権限が必要で、GITHUB_TOKEN では取れない。
Fine-grained PAT を環境変数 TRAFFIC_TOKEN で渡す。
使い方: TRAFFIC_TOKEN=... python3 scripts/collect_traffic.py --out traffic
"""
import argparse
import csv
import datetime
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.github.com/repos/"


def get(repo, path, token):
    req = urllib.request.Request(
        API + repo + path,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        # 403 はトークンの権限不足・組織の承認待ち・期限切れのいずれか。GitHub の説明文を出して切り分ける
        sys.exit(f"{e.code} {API + repo + path}: {e.read().decode(errors='replace')}")


def read_csv(path):
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path, fields, rows):
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def upsert_daily(path, api_rows):
    """日別の値を日付キーで上書きマージする。直近の日は後の取得で値が確定していくため上書きする。"""
    by_date = {r["date"]: r for r in read_csv(path)}
    for r in api_rows:
        date = r["timestamp"][:10]
        by_date[date] = {"date": date, "count": r["count"], "uniques": r["uniques"]}
    rows = [by_date[d] for d in sorted(by_date)]
    write_csv(path, ["date", "count", "uniques"], rows)
    return rows


def replace_snapshot(path, key, today, api_rows):
    """14日集計のスナップショットを取得日ごとに残す。同じ日に再実行したらその日の分を置き換える。"""
    rows = [r for r in read_csv(path) if r["date"] != today]
    rows += [{"date": today, key: r[key], "count": r["count"], "uniques": r["uniques"]} for r in api_rows]
    write_csv(path, ["date", key, "count", "uniques"], rows)


def summary(repo, today, views, clones, info, referrers, paths):
    total = lambda rows, k: sum(int(r[k]) for r in rows)
    since = views[0]["date"] if views else today
    lines = [
        f"# {repo} のトラフィック",
        "",
        f"最終取得: {today}（UTC）。GitHub が保持する直近14日分を毎日取り込み、{since} から積み上げています。",
        "",
        "| 指標 | 累計 |",
        "|---|---|",
        f"| 閲覧数 | {total(views, 'count'):,}回 |",
        f"| 閲覧数（日ごとのユニーク訪問者の合計） | {total(views, 'uniques'):,}人 |",
        f"| クローン数 | {total(clones, 'count'):,}回 |",
        f"| スター数（現在） | {info['stargazers_count']:,}件 |",
        f"| フォーク数（現在） | {info['forks_count']:,}件 |",
        "",
        "ユニーク訪問者は日ごとの値の合計のため、同じ人が別の日に来ると重複して数えています。",
        "",
        "## 直近14日の流入元",
        "",
        "| 流入元 | 閲覧数 | ユニーク訪問者 |",
        "|---|---|---|",
    ]
    lines += [f"| {r['referrer']} | {r['count']:,}回 | {r['uniques']:,}人 |" for r in referrers] or ["| （なし） | | |"]
    lines += ["", "## 直近14日によく見られたページ", "", "| ページ | 閲覧数 | ユニーク訪問者 |", "|---|---|---|"]
    lines += [f"| {r['path']} | {r['count']:,}回 | {r['uniques']:,}人 |" for r in paths] or ["| （なし） | | |"]
    return "\n".join(lines) + "\n"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="traffic")
    p.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "terass-inc/home-buying-framework"))
    args = p.parse_args()
    token = os.environ.get("TRAFFIC_TOKEN")
    if not token:
        sys.exit("TRAFFIC_TOKEN が設定されていません")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()

    views = upsert_daily(out / "views.csv", get(args.repo, "/traffic/views", token)["views"])
    clones = upsert_daily(out / "clones.csv", get(args.repo, "/traffic/clones", token)["clones"])
    referrers = get(args.repo, "/traffic/popular/referrers", token)
    paths = get(args.repo, "/traffic/popular/paths", token)
    replace_snapshot(out / "referrers.csv", "referrer", today, referrers)
    replace_snapshot(out / "paths.csv", "path", today, paths)

    info = get(args.repo, "", token)
    repo_rows = [r for r in read_csv(out / "repo.csv") if r["date"] != today]
    repo_rows.append({
        "date": today,
        "stars": info["stargazers_count"],
        "forks": info["forks_count"],
        "watchers": info["subscribers_count"],
    })
    write_csv(out / "repo.csv", ["date", "stars", "forks", "watchers"], repo_rows)

    (out / "SUMMARY.md").write_text(summary(args.repo, today, views, clones, info, referrers, paths))
    print(f"views {len(views)} days, clones {len(clones)} days, referrers {len(referrers)}, paths {len(paths)}")


if __name__ == "__main__":
    main()
