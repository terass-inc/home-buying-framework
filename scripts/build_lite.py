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
LIMIT = 5000

# 本書第6章「理想の家を見つける4つのステップ」の順に話題を束ねる
STEPS = [
    ("ステップ1 住む年数と購入コンセプト", ["00", "01", "04"]),
    ("ステップ2 資金計画", ["03", "02", "05"]),
    ("ステップ3 条件整理", ["06", "07", "08"]),
    ("ステップ4 物件見学", ["09"]),
]

LITE_ASSUMPTIONS = [
    ("interest_rate", "simulation_default_pct", "比較シミュレーションの標準金利（実勢金利は必ず最新値を確認。見直し要）", "%"),
    ("purchase_costs", "simulation_default_pct", "購入諸費用（物件価格に対する率）", "%"),
    ("ownership_costs", "repair_reserve_growth_pct_per_year", "修繕積立金の上昇率（年）", "%"),
    ("rent", "growth_pct_per_year", "賃料上昇率（年。標準。0%・2%も並べる。本書外）", "%"),
    ("rent", "comparable_rent_ratio_pct_of_price_per_year", "比較賃料の仮置き（物件価格に対する年率。相場が分かればそちら）", "%"),
    ("rent", "initial_cost_months", "賃貸の住み替え時の初期費用（賃料の月数）", "カ月分"),
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
        m = re.search(rf"^{section}:[^\n]*\n((?:(?:[ \t]+.*)?\n)+)", t, re.M)
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
        "AIへ: この文章だけが送られ相談内容がまだ無いときは、要約や復唱をせず、次の1文だけ返してください。「前提を読み込みました。まず、家を買おうと思ったきっかけを教えてください」相談内容が一緒に書かれていれば、そのまま答えてください。",
        "出典: 江口亮介『住宅購入の思考法』（2024）のエッセンスと、TERASS による追加論点。完全版: https://github.com/terass-inc/home-buying-framework",
        "",
        agents_core(),
        "",
        "## 話題別の要点（本書の4ステップ順）",
    ]
    files = {p.name[:2]: p for p in (ROOT / "principles").glob("[0-9][0-9]-*.md")}
    for step, ids in STEPS:
        parts.append(f"### {step}")
        for i in ids:
            parts.append(principle_digest(files[i]))
            parts.append("")
    parts.append("## " + assumptions_digest())
    parts.append("")
    parts.append("最終判断は不動産エージェントや独立系ファイナンシャルプランナーなど専門家と行ってください。")
    out = "\n".join(parts).rstrip() + "\n"
    (ROOT / "dist").mkdir(exist_ok=True)
    (ROOT / "dist" / "lite.md").write_text(out, encoding="utf-8")
    n = len(out)
    print(f"dist/lite.md: {n}字 (上限 {LIMIT}字)")
    inject_readme(out)
    if n > LIMIT:
        sys.exit(1)


README_START = "<!-- lite:embed:start -->"
README_END = "<!-- lite:embed:end -->"


def inject_readme(lite: str) -> None:
    """README.md のマーカー間に lite 本文（5つのルール以降）を埋め込む。

    AIに https://github.com/terass-inc/home-buying-framework を渡す方式では、AIが読むのは
    リポジトリページに描画された README なので、README 自体を AI向けの前提にしておく。
    """
    body = lite[lite.index("## 絶対に守る5つのルール"):].rstrip()
    p = ROOT / "README.md"
    t = p.read_text(encoding="utf-8")
    a, b = t.index(README_START) + len(README_START), t.index(README_END)
    new = t[:a] + "\n" + body + "\n" + t[b:]
    if new != t:
        p.write_text(new, encoding="utf-8")
    print(f"README.md: {len(new)}字（AI向け前提を埋め込み）")


if __name__ == "__main__":
    main()
