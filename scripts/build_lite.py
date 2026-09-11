#!/usr/bin/env python3
"""AGENTS.md と principles/ から、コピペ用のライト版 dist/lite.md を生成する。

取り込むもの:
- AGENTS.md の <!-- lite:start --> 〜 <!-- lite:end --> の区間
- 各 principles/NN-*.md の H1 タイトルと、先頭の引用ブロック（一文要約）。
  確認質問は AGENTS.md の区間と重複するため取り込まない
- assumptions.yaml の主要な数値（キーは LITE_ASSUMPTIONS で指定）
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIMIT = 4500

LITE_ASSUMPTIONS = [
    ("purchase_costs", "simulation_default_pct", "購入諸費用（物件価格に対する率）", "%"),
    ("depreciation", "condo_pct_per_year", "マンション価格の年間下落率（市況変化なし）", "%"),
    ("depreciation", "house_pct_per_year", "戸建て価格の年間下落率（市況変化なし）", "%"),
    ("holding", "minimum_years_to_buy", "購入を勧める最低居住年数", "年"),
    ("holding", "wait_breakeven_drop_pct_per_year", "1年待つ場合の損益分岐となる下落率（得をするには5%以上が必要、本書第3章）", "%"),
    ("holding", "historical_max_drop_pct", "首都圏中古マンションの過去最大下落（リーマンショック時）", "%"),
    ("loan", "default_term_years", "ローン期間の原則", "年"),
    ("loan", "income_multiple_lendable", "借りられる額の目安（年収倍率）", ""),
    ("loan", "pair_loan_ratio_recommended", "ペアローンの比率の目安", ""),
    ("rent", "renewal_fee_months", "賃貸更新料（2年ごと、賃料の月数）", "カ月分"),
]


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def agents_core() -> str:
    t = read(ROOT / "AGENTS.md")
    m = re.search(r"<!-- lite:start -->\n(.*?)<!-- lite:end -->", t, re.S)
    if not m:
        sys.exit("AGENTS.md に lite マーカーがありません")
    return m.group(1).strip()


def principle_digest(p: Path) -> str:
    t = read(p)
    title = re.search(r"^# (.+)$", t, re.M).group(1).strip()
    title = re.sub(r"（AIが回答するときの原則）$", "", title)
    quote = re.search(r"^> (.+)$", t, re.M)
    lines = [f"**{title}**"]
    if quote:
        lines.append(quote.group(1).strip())
    return "\n".join(lines)


def assumptions_digest() -> str:
    t = read(ROOT / "assumptions.yaml")
    updated = re.search(r"^updated_at:\s*(\S+)", t, re.M).group(1)
    rows = [f"数値前提（{updated} 時点。金利・税制は必ず最新値を確認）"]
    for section, key, label, unit in LITE_ASSUMPTIONS:
        m = re.search(rf"^{section}:[^\n]*\n((?:[ \t]+.*\n)+)", t, re.M)
        if not m:
            continue
        v = re.search(rf"^\s+{key}:\s*(.+?)\s*(?:#.*)?$", m.group(1), re.M)
        if v:
            val = v.group(1).strip().strip('"')
            rows.append(f"- {label}: {val}{unit}")
    return "\n".join(rows)


def main() -> None:
    parts = [
        "# 住宅購入をAIに相談する前に読み込ませる前提（ライト版）",
        "この文章を貼り付けたあとに、相談したいことを書いてください。AIはこの前提を会話全体に適用します。",
        "出典: 江口亮介『住宅購入の思考法』（2024）のエッセンスと、TERASS による追加論点。完全版: https://github.com/terass-inc/home-buying-framework",
        "",
        agents_core(),
        "",
        "## 話題別の要点",
    ]
    for p in sorted((ROOT / "principles").glob("[0-9][0-9]-*.md")):
        if p.name.startswith("99-"):
            continue  # 対応しないことは上の区間で既に書いている
        parts.append(principle_digest(p))
        parts.append("")
    parts.append("## " + assumptions_digest())
    parts.append("")
    parts.append("最終判断は不動産エージェントや独立系ファイナンシャルプランナーなど専門家と行ってください。この前提は不動産仲介会社 TERASS が作成しており、私たちにもポジションがあります。")
    out = "\n".join(parts).rstrip() + "\n"
    (ROOT / "dist").mkdir(exist_ok=True)
    (ROOT / "dist" / "lite.md").write_text(out, encoding="utf-8")
    n = len(out)
    print(f"dist/lite.md: {n}字 (上限 {LIMIT}字)")
    if n > LIMIT:
        sys.exit(1)


if __name__ == "__main__":
    main()
