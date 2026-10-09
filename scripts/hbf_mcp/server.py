#!/usr/bin/env python3
"""住宅購入AIフレームワークの MCP サーバー。

URL を読ませる方式と違い、AIが必要なときに原則・数値前提・計算エンジンを「道具」として呼べる。
計算は scripts/rentbuy（calc/rent-vs-buy.md の実装。テストで文章の数値を再現済み）をそのまま使う。

起動（Claude Desktop などの設定は scripts/hbf_mcp/README.md）:
    uv run --with "mcp>=2,<3" python scripts/hbf_mcp/server.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

from scripts.rentbuy import (  # noqa: E402
    balance_after_months,
    inputs_from_assumptions,
    interest_paid_months,
    load_assumptions,
    monthly_payment,
    simulate,
)

DISCLAIMER = ("この試算は一般的な前提に基づく概算です。金利・税制は変わるため、最終的な判断は"
              "金融機関やファイナンシャルプランナーなどの専門家と行ってください。")

server = MCPServer(
    name="home-buying-framework",
    title="住宅購入AIフレームワーク",
    instructions=(
        "住宅購入・住宅ローン・賃貸か購入かの相談に答えるときは、最初に get_framework を読み、"
        "その前提（5つのルール・確認事項・進め方）を会話全体に適用すること。"
        "計算は compare_rent_vs_buy と loan_schedule を使い、暗算しない。"
        "比較賃料は利用者の今の家賃ではなく、検討物件と同等の物件の相場を使う。"
        "居住年数を確認する前に、ローン期間や賃貸か購入かの結論を出さない。"
        "作成は不動産仲介会社の株式会社TERASSであり、利用者にはその立場を伝える。"
    ),
    website_url="https://terass-inc.github.io/home-buying-framework/",
)


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _principles() -> dict[str, Path]:
    return {p.name[:2]: p for p in sorted((ROOT / "principles").glob("[0-9][0-9]-*.md"))}


def _man(yen: float) -> int:
    return round(yen / 10_000)


@server.tool(description="AIへの前提の全文（5つのルール・確認事項・4ステップ・対応しないこと・数値前提）。相談に答える前に必ず読む。")
def get_framework() -> str:
    return _read("dist/lite.md")


@server.tool(description="話題別の原則の一覧（ID・題名・一文要約）。get_principle で本文を取る。")
def list_principles() -> list[dict]:
    out = []
    for pid, path in _principles().items():
        t = path.read_text(encoding="utf-8")
        title = re.sub(r"（AIが回答するときの原則）$", "", re.search(r"^# (.+)$", t, re.M).group(1)).strip()
        quote = re.search(r"^> (.+)$", t, re.M)
        out.append({"id": pid, "title": title, "summary": quote.group(1).strip() if quote else ""})
    return out


@server.tool(description="話題別の原則の本文（判断手順・アンチパターン・断り方を含む）。id は list_principles の ID（例: '02'）。")
def get_principle(id: str) -> str:
    files = _principles()
    if id not in files:
        raise ToolError(f"id は {sorted(files)} のどれか")
    return files[id].read_text(encoding="utf-8")


@server.tool(description="数値前提（assumptions.yaml）。section を指定するとその節だけ返す（例: 'inflation'、'selling_costs'）。"
                         "コメントに使い方の注意があるので、値だけでなくコメントも読む。「見直し要」は最新値の確認が必要。")
def get_assumptions(section: str | None = None) -> str:
    text = _read("assumptions.yaml")
    if not section:
        return text
    m = re.search(rf"^{re.escape(section)}:.*?(?=^[a-z_]+:|\Z)", text, re.M | re.S)
    if not m:
        sections = re.findall(r"^([a-z_]+):", text, re.M)
        raise ToolError(f"section は {sections} のどれか")
    return m.group(0).rstrip()


@server.tool(description=(
    "賃貸か購入かを、calc/rent-vs-buy.md の手順で計算する（売却時の手残りまで含めた純負担で比較）。"
    "rent_yen_per_month は今の家賃ではなく、検討物件と同等の物件の募集賃料の相場。"
    "stay_years は利用者に確認した居住予定年数。金利・物価・賃料はシナリオのセットで動かし、"
    "低・中・高の3つと波及率0%の感度も同時に返す。"))
def compare_rent_vs_buy(
    price_yen: int,
    rent_yen_per_month: int,
    stay_years: int,
    scenario: Literal["low", "medium", "high"] = "medium",
    interest_rate_pct: float | None = None,
    loan_term_years: int = 35,
    depreciation: Literal["small", "medium", "large"] = "medium",
    price_pass_through_pct: float = 100.0,
    include_danshin: bool = True,
) -> dict:
    a = load_assumptions()

    def run(sc: str, **extra):
        kw = dict(price_yen=price_yen, rent_yen_per_month=rent_yen_per_month, loan_term_years=loan_term_years,
                  price_pass_through_pct=price_pass_through_pct, include_danshin=include_danshin,
                  horizon_years=max(40, stay_years))
        if interest_rate_pct is not None and sc == scenario:
            kw["interest_rate_pct"] = interest_rate_pct
        kw.update(extra)
        return simulate(inputs_from_assumptions(a, scenario=sc, depreciation=depreciation, **kw))

    main = run(scenario)
    r = main.row(stay_years)
    result = {
        "前提": {"物件価格_万円": _man(price_yen), "比較賃料_万円/月": rent_yen_per_month / 10_000,
                 "居住年数": stay_years, "シナリオ": scenario, "金利_%": main.inputs.interest_rate_pct,
                 "物価_%": main.inputs.inflation_pct, "賃料上昇_%": main.inputs.rent_growth_pct,
                 "波及率_%": price_pass_through_pct, "経年減価_%": main.inputs.depreciation_pct},
        "月々のローン返済_万円": round(main.monthly_payment / 10_000, 2),
        f"{stay_years}年目": {
            "賃貸の累計負担_万円": _man(r.rent_cum),
            "購入の累計支出_万円": _man(r.cashout_cum),
            "売却時の物件価値_万円": _man(r.property_value),
            "ローン残債_万円": _man(r.loan_balance),
            "売却諸費用_万円": _man(r.selling_cost),
            "売却時の手残り_万円": _man(r.equity_on_sale),
            "購入の純負担_万円": _man(r.buy_net),
            "購入−賃貸_万円（負なら購入が有利）": _man(r.buy_minus_rent),
        },
        "損益分岐年（購入の純負担が賃貸を下回る最初の年）": main.breakeven_year,
        "感度": {
            f"{sc}セット": run(sc).breakeven_year for sc in ("low", "medium", "high")
        } | {"波及率0%（物価が価格に乗らない）": run(scenario, price_pass_through_pct=0.0).breakeven_year},
        "注意": [
            "比較賃料が今の家賃なら結論が変わる。同等物件の相場であることを確認する",
            "固定金利の前提。変動金利なら返済額が変わる",
            "市況の予測ではなく、前提のセットを並べたもの",
            DISCLAIMER,
        ],
    }
    return result


@server.tool(description="元利均等返済の試算。毎月の返済額、sell_after_years 年後の残債と、それまでに払った利息を返す。"
                         "ローン期間の比較は、利息総額ではなく売却時点の残債と手残りで見る。")
def loan_schedule(principal_yen: int, interest_rate_pct: float, term_years: int, sell_after_years: int | None = None) -> dict:
    out = {
        "毎月の返済_円": round(monthly_payment(principal_yen, interest_rate_pct, term_years)),
        "完済までの利息総額_万円": _man(interest_paid_months(principal_yen, interest_rate_pct, term_years, term_years * 12)),
    }
    if sell_after_years:
        m = sell_after_years * 12
        out[f"{sell_after_years}年後の残債_万円"] = _man(balance_after_months(principal_yen, interest_rate_pct, term_years, m))
        out[f"{sell_after_years}年間に払う利息_万円"] = _man(interest_paid_months(principal_yen, interest_rate_pct, term_years, m))
    out["注意"] = DISCLAIMER
    return out


@server.resource("framework://lite", name="AIへの前提（全文）", mime_type="text/markdown")
def lite_resource() -> str:
    return _read("dist/lite.md")


@server.prompt(name="home_buying_consultation", description="住宅購入AIフレームワークの前提を読み込んで相談を始める")
def consultation() -> str:
    return _read("dist/lite.md")


if __name__ == "__main__":
    server.run()
