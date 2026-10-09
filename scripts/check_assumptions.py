#!/usr/bin/env python3
"""assumptions.yaml の整合性を検査する。CI で毎回実行する。

検査すること:
1. 文章（AGENTS.md・principles/・calc/・cases/）が `section.key` の形で参照しているキーが、
   assumptions.yaml に実在すること。キー名を変えたのに文章を直し忘れると、AIが存在しない前提を探すことになる
2. 値の型と範囲。名前が `_pct` で終わる数値は -100〜100、`_yen` で終わる数値は 0 以上
3. scripts/build_lite.py がライト版に取り込むキー（LITE_ASSUMPTIONS）が実在すること。
   build_lite.py は見つからないキーを黙って飛ばすので、ここで検出する
4. updated_at の鮮度。90日を超えたら警告、180日を超えたら失敗

使い方: python3 scripts/check_assumptions.py [--today YYYY-MM-DD]
依存: PyYAML
"""
import argparse
import datetime
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DOC_GLOBS = ["AGENTS.md", "principles/*.md", "calc/*.md", "cases/*.md"]
REF = re.compile(r"`([a-z_]+(?:\.[a-z0-9_]+)+)`")
WARN_DAYS, FAIL_DAYS = 90, 180

errors: list[str] = []
warnings: list[str] = []


def resolve(data, dotted: str) -> bool:
    node = data
    for part in dotted.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return False
    return True


def check_refs(data) -> int:
    n = 0
    for pattern in DOC_GLOBS:
        for path in sorted(ROOT.glob(pattern)):
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for ref in REF.findall(line):
                    if ref.split(".")[0] not in data:
                        continue  # ファイル名（dist/lite.md など）や別の識別子は対象外
                    n += 1
                    if not resolve(data, ref):
                        errors.append(f"{path.relative_to(ROOT)}:{lineno}: `{ref}` は assumptions.yaml にありません")
    return n


def check_values(node, path="") -> int:
    n = 0
    if isinstance(node, dict):
        for k, v in node.items():
            n += check_values(v, f"{path}.{k}" if path else str(k))
        return n
    if isinstance(node, list):
        for i, v in enumerate(node):
            n += check_values(v, f"{path}.{i}")
        return n
    key = path.rsplit(".", 1)[-1]
    if isinstance(node, bool) or not isinstance(node, (int, float)):
        return 0
    n += 1
    if key.endswith("_pct") and not -100 <= node <= 100:
        errors.append(f"{path}: 割合 {node} が -100〜100 の範囲外です")
    if key.endswith("_yen") and node < 0:
        errors.append(f"{path}: 金額 {node} が負です")
    return n


def check_lite_keys(data) -> int:
    src = (ROOT / "scripts" / "build_lite.py").read_text(encoding="utf-8")
    keys = re.findall(r'\(\s*"([a-z_]+)",\s*"([a-z0-9_]+)",', src)
    for section, key in keys:
        if not resolve(data, f"{section}.{key}"):
            errors.append(f"build_lite.py の LITE_ASSUMPTIONS: `{section}.{key}` は assumptions.yaml にありません")
    return len(keys)


def check_freshness(data, today: datetime.date) -> int:
    updated = data.get("updated_at")
    if not isinstance(updated, datetime.date):
        errors.append("updated_at が YYYY-MM-DD の日付ではありません")
        return 0
    age = (today - updated).days
    if age > FAIL_DAYS:
        errors.append(f"updated_at（{updated}）から{age}日経過しています。数値前提を見直してください")
    elif age > WARN_DAYS:
        warnings.append(f"updated_at（{updated}）から{age}日経過しています。次の見直しで更新してください")
    return age


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--today", type=datetime.date.fromisoformat, default=datetime.date.today())
    args = p.parse_args()

    data = yaml.safe_load((ROOT / "assumptions.yaml").read_text(encoding="utf-8"))
    refs = check_refs(data)
    values = check_values(data)
    lite = check_lite_keys(data)
    age = check_freshness(data, args.today)

    for w in warnings:
        print(f"::warning file=assumptions.yaml::{w}")
    for e in errors:
        print(f"::error::{e}")
    print(f"文章からの参照 {refs}件、数値 {values}件、ライト版のキー {lite}件、updated_at から {age}日")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
