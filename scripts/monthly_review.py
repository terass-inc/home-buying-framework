#!/usr/bin/env python3
"""数値前提（assumptions.yaml）の毎月の見直し。GitHub Actions（.github/workflows/monthly-review.yml）から月1回動かす。

Claude に Web 検索で最新値を調べさせ、次の2つを行う。
1. market_rates（実勢金利の目安）は自動で書き換える。値が一次情報の数字そのもので、判断が要らないため
2. それ以外の「見直し要」の項目（税制・補助金・審査金利・シナリオ金利など）は書き換えず、
   出典つきの確認結果をレポート（Markdown）にして、Pull Request の本文に載せる。人が読んで直す

書き換えたら updated_at と market_rates.as_of も今日の日付にする。dist/lite.md の再生成はワークフロー側で行う。

使い方:
    python3 scripts/monthly_review.py --report review.md          # 調べて書き換え、レポートを書く
    python3 scripts/monthly_review.py --from-json result.json ...  # 調べ済みの JSON から書き換える（試験用）
依存: anthropic（--from-json のときは不要）
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSUMPTIONS = ROOT / "assumptions.yaml"
MODEL = "claude-opus-5-5"

# 自動で書き換える market_rates のキーと、受け取る値の範囲（範囲外は採用しない）
RATE_KEYS = {
    "flat35_most_common_pct": (0.5, 10.0),
    "boj_policy_rate_pct": (-1.0, 10.0),
}
RANGE_KEYS = {"variable_major_bank_min_pct": (0.1, 10.0)}  # "1.2〜1.3" のような幅で持つ

# 人が判断する項目。Claude には現在値と照らして「変わったか」を出典つきで答えさせる
CHECK_ITEMS = [
    ("interest_rate.screening_rate_pct", "審査金利（銀行が返済比率を計算するときの仮の金利）の一般的な水準"),
    ("inflation.scenarios", "medium・high シナリオの金利（1.5%・2.5%）が実勢と比べて妥当か。固定金利の実勢を必ず併記"),
    ("tax_and_programs.mortgage_deduction", "住宅ローン控除の控除率・期間・借入限度額・所得要件に変更・改正の動きがないか"),
    ("tax_and_programs.gift_tax_exemption", "住宅取得等資金の贈与税非課税（期限2026年12月31日）の延長が決まったか"),
    ("tax_and_programs.subsidies", "国の住宅省エネ補助（みらいエコ住宅2026事業など）の受付状況・予算の消化率・次年度事業の発表"),
    ("rent.growth_pct_per_year", "家賃の動向（消費者物価指数の家賃、都市部の募集賃料）"),
    ("loan.default_term_years", "40年・50年ローンの普及状況（最長期間の前提が変わっていないか）"),
]

PROMPT = """あなたは日本の住宅ローン・住宅税制の調査担当です。今日は {today} です。
Web 検索で一次情報（住宅金融支援機構、日本銀行、財務省、国土交通省、各銀行の公式発表）を優先して、次を調べてください。
推測で数字を埋めないこと。確認できなかった値は null にし、note に理由を書いてください。

## A. 実勢金利（数値で答える）
- flat35_most_common_pct: フラット35（返済期間21〜35年・融資率9割以下）の最頻金利。今月適用分（未発表なら先月分と明記）
- variable_major_bank_min_pct: 大手銀行（三菱UFJ・三井住友・みずほ・りそな）の変動金利の最低水準（優遇後）。"1.2〜1.3" のような幅の文字列
- boj_policy_rate_pct: 日本銀行の政策金利（無担保コール翌日物の誘導目標）

## B. 現在の前提との照合（文章で答える）
現在の前提ファイルの該当部分:
```yaml
{current}
```
確認する項目:
{items}

## 回答の形式
最後に、次の形の JSON を ```json のコードブロック1つで出力してください。それ以外の場所に JSON を書かないこと。
{{
  "as_of": "YYYY-MM-DD",
  "market_rates": {{
    "flat35_most_common_pct": 数値 または null,
    "flat35_month": "YYYY-MM",
    "variable_major_bank_min_pct": "幅の文字列" または null,
    "boj_policy_rate_pct": 数値 または null,
    "note": "変動金利の今後の見通しなど、回答時にAIが伝えるべきこと（1〜2文）",
    "sources": ["URL", ...]
  }},
  "checks": [
    {{"key": "項目のキー", "status": "変更なし" | "要修正" | "要注意" | "確認できず",
      "finding": "分かったこと（数字と時点を含める）", "proposal": "直す場合の案（なければ空）", "sources": ["URL", ...]}}
  ]
}}
"""


def section_text(yaml_text: str, section: str) -> str:
    m = re.search(rf"^{section}:[^\n]*\n((?:(?:[ \t]+.*)?\n)+)", yaml_text, re.M)
    return f"{section}:\n{m.group(1)}" if m else ""


def build_prompt(yaml_text: str, today: str) -> str:
    sections = ["interest_rate", "market_rates", "inflation", "tax_and_programs", "rent", "loan"]
    current = "\n".join(section_text(yaml_text, s) for s in sections)
    items = "\n".join(f"- `{k}`: {d}" for k, d in CHECK_ITEMS)
    return PROMPT.format(today=today, current=current, items=items)


def ask_claude(prompt: str) -> str:
    """Web 検索つきで調べさせ、最後のテキストを返す。サーバー側の検索が長引くと pause_turn で止まるので続きを頼む。"""
    import anthropic  # 遅延 import（--from-json と単体テストでは SDK 不要）

    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": prompt}]
    for _ in range(5):
        with client.beta.messages.stream(
            model=MODEL,
            max_tokens=32000,
            output_config={"effort": "high"},
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 20,
                    "user_location": {"type": "approximate", "country": "JP", "timezone": "Asia/Tokyo"}}],
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",  # 安全分類で断られたときは別のモデルで続ける
            messages=messages,
        ) as stream:
            resp = stream.get_final_message()
        if resp.stop_reason == "pause_turn":
            messages = [*messages, {"role": "assistant", "content": resp.content}]
            continue
        if resp.stop_reason == "refusal":
            sys.exit(f"Claude が回答を断りました: {resp.stop_details}")
        return "".join(b.text for b in resp.content if b.type == "text")
    sys.exit("検索が終わりませんでした（pause_turn が5回続いた）")


def extract_json(text: str) -> dict:
    blocks = re.findall(r"```json\s*\n(.*?)\n```", text, re.S)
    if not blocks:
        raise ValueError("回答に ```json のブロックがありません")
    return json.loads(blocks[-1])


def _in_range(v, lo, hi) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi


def _range_ok(v, lo, hi) -> bool:
    if not isinstance(v, str):
        return False
    nums = re.findall(r"\d+(?:\.\d+)?", v)
    return 1 <= len(nums) <= 2 and all(lo <= float(n) <= hi for n in nums)


def _set_scalar(text: str, key: str, value: str) -> str:
    """market_rates 区間の `key: 値  # コメント` の値だけを差し替える（コメントは残す）。"""
    head, sep, rest = text.partition("\nmarket_rates:")
    if not sep:
        raise ValueError("assumptions.yaml に market_rates がありません")
    block_end = re.search(r"\n(?=[^ \t\n#])", rest)
    block, tail = (rest[:block_end.start()], rest[block_end.start():]) if block_end else (rest, "")
    pat = re.compile(rf"^(\s+{key}:\s*)(\"[^\"]*\"|[^\s#]+)", re.M)
    if not pat.search(block):
        raise ValueError(f"market_rates.{key} が見つかりません")
    block = pat.sub(lambda m: m.group(1) + value, block, count=1)
    return head + sep + block + tail


def _set_list(text: str, key: str, values: list[str]) -> str:
    head, sep, rest = text.partition("\nmarket_rates:")
    block_end = re.search(r"\n(?=[^ \t\n#])", rest)
    block, tail = (rest[:block_end.start()], rest[block_end.start():]) if block_end else (rest, "")
    pat = re.compile(rf"^(\s+){key}:\n(?:\1  - .*\n)*", re.M)
    m = pat.search(block + "\n")
    if not m:
        return text
    indent = m.group(1)
    new = f"{indent}{key}:\n" + "".join(f"{indent}  - {json.dumps(v, ensure_ascii=False)}\n" for v in values)
    block = (block + "\n")[:m.start()] + new + (block + "\n")[m.end():]
    return head + sep + block.rstrip("\n") + tail


def apply_market_rates(text: str, result: dict, today: str) -> tuple[str, list[str]]:
    """範囲内の値だけを書き換え、書き換えた内容を返す。"""
    rates = result.get("market_rates") or {}
    changes: list[str] = []
    for key, (lo, hi) in RATE_KEYS.items():
        v = rates.get(key)
        if _in_range(v, lo, hi):
            text = _set_scalar(text, key, f"{v:g}")
            changes.append(f"market_rates.{key} = {v:g}")
    for key, (lo, hi) in RANGE_KEYS.items():
        v = rates.get(key)
        if _range_ok(v, lo, hi):
            text = _set_scalar(text, key, json.dumps(v, ensure_ascii=False))
            changes.append(f"market_rates.{key} = {v}")
    if changes:
        note = rates.get("note")
        if isinstance(note, str) and note.strip():
            note = note.strip().replace('"', "”")
            if "見直し要" not in note:
                note += "（見直し要）"
            text = _set_scalar(text, "note", json.dumps(note, ensure_ascii=False))
        srcs = [s for s in rates.get("sources") or [] if isinstance(s, str) and s.startswith("https://")]
        if srcs:
            text = _set_list(text, "sources", srcs[:6])
        text = _set_scalar(text, "as_of", today)
        text = re.sub(r"^updated_at:\s*\S+", f"updated_at: {today}", text, count=1, flags=re.M)
    return text, changes


def render_report(result: dict, changes: list[str], today: str) -> str:
    rates = result.get("market_rates") or {}
    lines = [f"# 数値前提の毎月の見直し（{today}）", "",
             "`scripts/monthly_review.py` が Claude の Web 検索で調べた結果です。**数字と出典を確認してからマージしてください。**", "",
             "## 自動で書き換えたもの（market_rates）", ""]
    lines += [f"- {c}" for c in changes] or ["- なし（値を確認できなかったか、範囲外だった）"]
    if rates.get("flat35_month"):
        lines.append(f"- フラット35の適用月: {rates['flat35_month']}")
    if rates.get("note"):
        lines.append(f"- 補足: {rates['note']}")
    lines += ["", "出典: " + "、".join(rates.get("sources") or ["なし"]), "",
              "## 人が判断する項目", "", "| 項目 | 判定 | 分かったこと | 直す場合の案 |", "|---|---|---|---|"]
    for c in result.get("checks") or []:
        cell = lambda s: str(s or "").replace("|", "／").replace("\n", " ")
        src = " ".join(f"[出典]({u})" for u in c.get("sources") or [] if str(u).startswith("https://"))
        lines.append(f"| `{cell(c.get('key'))}` | {cell(c.get('status'))} | {cell(c.get('finding'))} {src} | {cell(c.get('proposal'))} |")
    lines += ["", "「要修正」の項目は、このブランチに追加のコミットで直すか、別の Pull Request にしてください。",
              "直したら `python3 scripts/build_lite.py` で `dist/lite.md` と `README.md` を再生成します。"]
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", default="review.md", help="レポート（Pull Request の本文）の出力先")
    ap.add_argument("--from-json", help="調べ済みの結果（JSON）を使う。Claude を呼ばない")
    ap.add_argument("--today", default=datetime.date.today().isoformat())
    args = ap.parse_args()

    text = ASSUMPTIONS.read_text(encoding="utf-8")
    if args.from_json:
        result = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
    else:
        answer = ask_claude(build_prompt(text, args.today))
        result = extract_json(answer)
        Path(args.report).with_suffix(".json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    new_text, changes = apply_market_rates(text, result, args.today)
    if new_text != text:
        ASSUMPTIONS.write_text(new_text, encoding="utf-8")
    Path(args.report).write_text(render_report(result, changes, args.today), encoding="utf-8")
    print(f"書き換え {len(changes)}件、照合 {len(result.get('checks') or [])}件 → {args.report}")


if __name__ == "__main__":
    main()
