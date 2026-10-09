#!/usr/bin/env python3
"""住宅購入AIフレームワークの MCP サーバー。

一問一答ではなく、相談の状態をサーバーが覚え、分かったことが増えるたびに
関係する計算をまとめた診断レポートを返す。前提を変えた「もしも」も試せる。
計算は scripts/rentbuy（calc/ と principles/ の手順の実装。文章の数値をテストで再現済み）を使う。

起動:
    uvx --from git+https://github.com/terass-inc/home-buying-framework hbf-mcp
    （リポジトリから）uv run --with "mcp>=2,<3" python scripts/hbf_mcp/server.py
"""
from __future__ import annotations

import re
import sys
import uuid
from dataclasses import asdict, fields
from pathlib import Path
from typing import Literal, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

import consult  # noqa: E402
import report  # noqa: E402
from paths import ROOT  # noqa: E402
from report import DISCLAIMER, advice, inputs_from_assumptions, load_assumptions, simulate  # noqa: E402

try:
    from scripts.rentbuy import balance_after_months, interest_paid_months, monthly_payment
except ImportError:
    from rentbuy import balance_after_months, interest_paid_months, monthly_payment  # type: ignore

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
SESSION = ToolAnnotations(readOnlyHint=False, idempotentHint=False, openWorldHint=False)
Topic = Literal[tuple(consult.TOPICS)]  # type: ignore[valid-type]

server = MCPServer(
    name="home-buying-framework",
    title="住宅購入AIフレームワーク",
    version="0.2.0",
    instructions=(
        "住宅購入・住宅ローン・賃貸か購入かの相談では、最初に get_framework を読み、その前提を会話全体に適用する。"
        "相談が始まったら start_consultation で相談を作り、利用者の話から分かったことを update_facts でまとめて渡す"
        "（1つずつ聞き直さない）。返ってくる診断レポートをもとに答え、次に聞くのは『次に分かると精度が上がること』の1つだけにする。"
        "利用者が前提を変えて考えたいときは what_if を使う。計算は道具を使い、暗算しない。"
        "回答を送る前に self_check で NG 判定に当たっていないか確認する。"
        "比較賃料は今の家賃ではなく同等物件の相場。居住年数が分からないうちは結論を出さない。"
        "作成は不動産仲介会社の株式会社TERASSであり、利用者にはその立場を伝える。"
    ),
    website_url="https://terass-inc.github.io/home-buying-framework/",
)


# ---- 相談の状態 ---------------------------------------------------------------

class FactsInput(BaseModel):
    """相談で分かったこと。分かったものだけ渡せばよい（渡さない項目は変えない）。金額は円。"""
    purpose: Optional[str] = Field(None, description="家を買おうと思ったきっかけ・理由（利用者の言葉のまま）")
    purpose_sentence_confirmed: Optional[bool] = Field(None, description="「〇〇のための家」と1文で確認できたか")
    stay_years: Optional[float] = Field(None, description="何年住む予定か（売却・住み替えまでの年数）")
    household_income_yen: Optional[int] = Field(None, description="世帯年収（円）")
    savings_yen: Optional[int] = Field(None, description="購入後も手元に残せる現預金（円）")
    current_rent_yen: Optional[int] = Field(None, description="今の家賃（円/月）。比較の賃料には使わない")
    property_price_yen: Optional[int] = Field(None, description="検討している物件の価格（円）")
    property_age_years: Optional[float] = Field(None, description="築年数")
    comparable_rent_yen: Optional[int] = Field(None, description="検討物件と同等の物件の募集賃料の相場（円/月）")
    rent_moves_known: Optional[bool] = Field(None, description="賃貸を続ける場合の住み替え予定を確認したか")
    rate_type: Optional[Literal["fixed", "variable"]] = Field(None, description="金利タイプ")
    interest_rate_pct: Optional[float] = Field(None, description="適用金利（%）")
    family_plan_known: Optional[bool] = Field(None, description="家族構成の変化・教育や介護の支出を確認したか")
    existing_insurance_known: Optional[bool] = Field(None, description="医療・がん・就業不能保険の加入状況を確認したか")


_FACT_FIELDS = {f.name for f in fields(consult.Facts)} - {"extra"}
_sessions: dict[str, consult.Facts] = {}


def _merge(base: consult.Facts, new: Optional[FactsInput]) -> consult.Facts:
    if new is None:
        return base
    d = asdict(base)
    d.update({k: v for k, v in new.model_dump().items() if v is not None and k in _FACT_FIELDS})
    return consult.Facts(**d)


def _session(consultation_id: str) -> consult.Facts:
    if consultation_id not in _sessions:
        raise ToolError("その consultation_id の相談はありません。start_consultation で作り直してください")
    return _sessions[consultation_id]


def _report(cid: str, f: consult.Facts) -> dict:
    r = report.diagnose(f)
    known = {k: v for k, v in asdict(f).items() if k != "extra" and v not in (None, False, "")}
    return {"consultation_id": cid, "分かっていること": known, "report": r, "report_markdown": report.to_markdown(r)}


@server.tool(annotations=SESSION, description=(
    "相談を始める。分かっていることがあれば facts に入れる（なくてもよい）。"
    "相談ID と、今の前提での診断レポート（賃貸か購入か・ローン期間・待つコスト・借りるべき額・購入直後の純資産・"
    "結論が変わる条件・次に分かると精度が上がること）を返す。"))
def start_consultation(facts: Optional[FactsInput] = None) -> dict:
    cid = uuid.uuid4().hex[:8]
    _sessions[cid] = _merge(consult.Facts(), facts)
    return _report(cid, _sessions[cid])


@server.tool(annotations=SESSION, description=(
    "相談に、新しく分かったことをまとめて足し、診断レポートを更新して返す。"
    "利用者が1回の発言で複数のことを話したら、全部まとめて渡す。"))
def update_facts(consultation_id: str, facts: FactsInput) -> dict:
    _sessions[consultation_id] = _merge(_session(consultation_id), facts)
    return _report(consultation_id, _sessions[consultation_id])


@server.tool(annotations=READ_ONLY, description="相談の今の診断レポートを返す。")
def diagnose(consultation_id: str) -> dict:
    return _report(consultation_id, _session(consultation_id))


@server.tool(annotations=READ_ONLY, description=(
    "前提を一時的に変えたらどうなるかを試す（保存しない）。例: 居住年数を5年にしたら、変動金利なら、"
    "比較賃料が18万円なら。主な数字（損益分岐年・居住年数時点の差・毎月返済）の変化と、変更後の結論が変わる条件を返す。"))
def what_if(consultation_id: str, changes: FactsInput) -> dict:
    f = _session(consultation_id)
    ch = {k: v for k, v in changes.model_dump().items() if v is not None and k in _FACT_FIELDS}
    if not ch:
        raise ToolError("changes に変えたい項目を1つ以上入れてください")
    return report.what_if(f, ch)


@server.tool(annotations=READ_ONLY, description=(
    "話題と分かっていることから、相談がどのステップにいるか、次に聞く質問（1つだけ）、まだ出せない結論を返す。"
    "相談IDがあればその状態を使う。"))
def next_step(topic: Topic, consultation_id: Optional[str] = None, facts: Optional[FactsInput] = None) -> dict:
    f = _merge(_session(consultation_id) if consultation_id else consult.Facts(), facts)
    return consult.next_step(topic, f)


@server.tool(annotations=READ_ONLY, description=(
    "回答を送る前に照らす NG 判定の一覧（全ケース共通＋話題別のテストケース）と、必ず含める要点。"
    "金額の試算を示すなら shows_numbers=true（免責文の要件が加わる）。"))
def self_check(topic: Topic, shows_numbers: bool = False) -> dict:
    return consult.self_check(topic, shows_numbers)


# ---- 前提・原則・数値 ---------------------------------------------------------------

def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _principles() -> dict[str, Path]:
    return {p.name[:2]: p for p in sorted((ROOT / "principles").glob("[0-9][0-9]-*.md"))}


@server.tool(annotations=READ_ONLY, description="AIへの前提の全文（5つのルール・確認事項・4ステップ・対応しないこと・数値前提）。相談に答える前に必ず読む。")
def get_framework() -> str:
    return _read("dist/lite.md")


@server.tool(annotations=READ_ONLY, description="話題別の原則の一覧（ID・題名・一文要約）。")
def list_principles() -> list[dict]:
    out = []
    for pid, path in _principles().items():
        t = path.read_text(encoding="utf-8")
        title = re.sub(r"（AIが回答するときの原則）$", "", re.search(r"^# (.+)$", t, re.M).group(1)).strip()
        quote = re.search(r"^> (.+)$", t, re.M)
        out.append({"id": pid, "title": title, "summary": quote.group(1).strip() if quote else ""})
    return out


@server.tool(annotations=READ_ONLY, description="話題別の原則の本文（判断手順・アンチパターン・断り方）。id は list_principles の ID（例: '02'）。")
def get_principle(id: str) -> str:
    files = _principles()
    if id not in files:
        raise ToolError(f"id は {sorted(files)} のどれか")
    return files[id].read_text(encoding="utf-8")


@server.tool(annotations=READ_ONLY, description="数値前提（assumptions.yaml）。section を指定するとその節だけ。コメントの注意も読む。「見直し要」は最新値の確認が必要。")
def get_assumptions(section: Optional[str] = None) -> str:
    text = _read("assumptions.yaml")
    if not section:
        return text
    m = re.search(rf"^{re.escape(section)}:.*?(?=^[a-z_]+:|\Z)", text, re.M | re.S)
    if not m:
        raise ToolError(f"section は {re.findall(r'^([a-z_]+):', text, re.M)} のどれか")
    return m.group(0).rstrip()


# ---- 個別の計算 ---------------------------------------------------------------

@server.tool(annotations=READ_ONLY, description=(
    "賃貸か購入かを、売却時の手残りまで含めた純負担で比べる（calc/rent-vs-buy.md）。"
    "rent_yen_per_month は今の家賃ではなく同等物件の相場。低・中・高セットと波及率0%の分岐年も返す。"))
def compare_rent_vs_buy(price_yen: int, rent_yen_per_month: int, stay_years: int,
                        scenario: Literal["low", "medium", "high"] = "medium",
                        interest_rate_pct: Optional[float] = None, price_pass_through_pct: float = 100.0,
                        depreciation: Literal["small", "medium", "large"] = "medium",
                        include_danshin: bool = True) -> dict:
    a = load_assumptions(ROOT / "assumptions.yaml")

    def run(sc, **extra):
        kw = dict(price_yen=price_yen, rent_yen_per_month=rent_yen_per_month, horizon_years=max(40, stay_years),
                  price_pass_through_pct=price_pass_through_pct, include_danshin=include_danshin)
        if interest_rate_pct is not None and sc == scenario:
            kw["interest_rate_pct"] = interest_rate_pct
        kw.update(extra)
        return simulate(inputs_from_assumptions(a, scenario=sc, depreciation=depreciation, **kw))

    m = run(scenario)
    r = m.row(stay_years)
    man = lambda y: round(y / 10_000)  # noqa: E731
    return {
        "毎月のローン返済_万円": round(m.monthly_payment / 10_000, 2),
        f"{stay_years}年目": {"賃貸の累計負担_万円": man(r.rent_cum), "購入の累計支出_万円": man(r.cashout_cum),
                             "物件価値_万円": man(r.property_value), "残債_万円": man(r.loan_balance),
                             "売却諸費用_万円": man(r.selling_cost), "手残り_万円": man(r.equity_on_sale),
                             "購入−賃貸_万円（負なら購入が有利）": man(r.buy_minus_rent)},
        "損益分岐年": m.breakeven_year,
        "感度": {f"{sc}セット": run(sc).breakeven_year for sc in ("low", "medium", "high")}
        | {"波及率0%": run(scenario, price_pass_through_pct=0.0).breakeven_year},
        "注意": DISCLAIMER,
    }


@server.tool(annotations=READ_ONLY, description="元利均等の毎月返済額、完済までの利息、sell_after_years 年後の残債とそれまでの利息。")
def loan_schedule(principal_yen: int, interest_rate_pct: float, term_years: int, sell_after_years: Optional[int] = None) -> dict:
    out = {"毎月の返済_円": round(monthly_payment(principal_yen, interest_rate_pct, term_years)),
           "完済までの利息総額_万円": round(interest_paid_months(principal_yen, interest_rate_pct, term_years, term_years * 12) / 10_000)}
    if sell_after_years:
        mths = sell_after_years * 12
        out[f"{sell_after_years}年後の残債_万円"] = round(balance_after_months(principal_yen, interest_rate_pct, term_years, mths) / 10_000)
        out[f"{sell_after_years}年間に払う利息_万円"] = round(interest_paid_months(principal_yen, interest_rate_pct, term_years, mths) / 10_000)
    out["注意"] = DISCLAIMER
    return out


@server.tool(annotations=READ_ONLY, description=(
    "ローン期間の比較（principles/02）。毎月返済・総利息・売却時点の残債と正味の差・差額の使い道・審査金利での返済比率。"))
def loan_term_comparison(principal_yen: int, annual_rate_pct: Optional[float] = None, short_term_years: int = 20,
                         long_term_years: Optional[int] = None, sale_year: Optional[int] = None,
                         investment_yield_pct: Optional[float] = None, annual_income_yen: Optional[int] = None) -> dict:
    return advice.loan_term_comparison(principal_yen, annual_rate_pct=annual_rate_pct, short_term_years=short_term_years,
                                       long_term_years=long_term_years, sale_year=sale_year,
                                       investment_yield_pct=investment_yield_pct, annual_income_yen=annual_income_yen).to_dict()


@server.tool(annotations=READ_ONLY, description="待つコスト（principles/01・04）。待って得をするための損益分岐下落率と、過去の下落実績との比較。")
def wait_cost(property_price_yen: int, monthly_rent_yen: int, wait_years: float = 1) -> dict:
    return advice.wait_cost(property_price_yen, monthly_rent_yen, wait_years=wait_years).to_dict()


@server.tool(annotations=READ_ONLY, description=(
    "借りられる額（銀行の上限）と借りるべき額（principles/03）を分けて返す。借りるべき額は式で決めず、確認すべき値を返す。"))
def borrowing_budget(annual_income_yen: int, property_price_yen: Optional[int] = None,
                     property_type: Optional[Literal["new_condo", "used_or_house"]] = None,
                     other_debt_annual_payment_yen: int = 0, affordable_monthly_payment_yen: Optional[int] = None,
                     stress_rate_pct: Optional[float] = None) -> dict:
    return advice.borrowing_budget(annual_income_yen, property_price_yen=property_price_yen, property_type=property_type,
                                   other_debt_annual_payment_yen=other_debt_annual_payment_yen,
                                   affordable_monthly_payment_yen=affordable_monthly_payment_yen,
                                   stress_rate_pct=stress_rate_pct).to_dict()


@server.tool(annotations=READ_ONLY, description="住宅購入のバランスシート（calc/balance-sheet.md）。購入直後と N 年後の資産・負債・純資産。")
def balance_sheet(property_price_yen: int, savings_yen: int = 4_000_000, securities_yen: int = 1_000_000,
                  other_assets_yen: int = 1_000_000, term_years: int = 35, annual_rate_pct: Optional[float] = None,
                  target_year: int = 20) -> dict:
    return advice.balance_sheet(property_price_yen=property_price_yen, savings_yen=savings_yen, securities_yen=securities_yen,
                                other_assets_yen=other_assets_yen, term_years=term_years, annual_rate_pct=annual_rate_pct,
                                target_year=target_year).to_dict()


@server.tool(annotations=READ_ONLY, description="売却諸費用の内訳（仲介手数料・その他費用・合計・率）。")
def selling_costs_breakdown(sale_price_yen: int) -> dict:
    return advice.selling_costs_breakdown(sale_price_yen).to_dict()


# ---- リソース・プロンプト ---------------------------------------------------------------

@server.resource("framework://lite", name="AIへの前提（全文）", mime_type="text/markdown")
def lite_resource() -> str:
    return _read("dist/lite.md")


@server.resource("framework://assumptions", name="数値前提（assumptions.yaml）", mime_type="application/yaml")
def assumptions_resource() -> str:
    return _read("assumptions.yaml")


@server.resource("framework://principles/{pid}", name="話題別の原則", mime_type="text/markdown")
def principle_resource(pid: str) -> str:
    return get_principle(pid)


@server.resource("framework://cases/{cid}", name="テストケース", mime_type="text/markdown")
def case_resource(cid: str) -> str:
    hits = sorted((ROOT / "cases").glob(f"{cid}-*.md"))
    if not hits:
        raise ToolError(f"ケース {cid} はありません")
    return hits[0].read_text(encoding="utf-8")


@server.prompt(name="home_buying_consultation", description="住宅購入AIフレームワークの前提で相談を始める")
def consultation_prompt() -> str:
    return _read("dist/lite.md") + "\n\n相談が始まったら start_consultation を使い、分かったことは update_facts でまとめて渡すこと。"


@server.prompt(name="rent_vs_buy_check", description="賃貸か購入かを、前提をそろえて診断する")
def rent_vs_buy_prompt(property_price: str = "", comparable_rent: str = "", stay_years: str = "") -> str:
    known = "、".join(x for x in (f"物件価格 {property_price}" if property_price else "",
                                  f"同等物件の家賃 {comparable_rent}" if comparable_rent else "",
                                  f"居住年数 {stay_years}" if stay_years else "") if x)
    return (f"住宅購入AIフレームワークの前提で、賃貸か購入かを考えたい。分かっていること: {known or 'まだない'}。"
            "get_framework を読み、start_consultation で診断し、足りない前提は1つずつではなく、次に必要なものから聞いてください。")


@server.prompt(name="loan_term_check", description="ローン期間（20年か35年か）を、売却時期から考える")
def loan_term_prompt(loan_amount: str = "", sale_after_years: str = "") -> str:
    return (f"住宅ローンの期間を決めたい。借入額 {loan_amount or '未定'}、売却予定 {sale_after_years or '未定'}。"
            "get_framework を読み、利息総額ではなく売却時の残債と手残りで比べてください（loan_term_comparison を使う）。")


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
