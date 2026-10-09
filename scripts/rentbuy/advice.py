"""住宅相談でよく使う計算（MCP サーバーから呼ぶ前提）。標準ライブラリのみ。

各関数は AdviceResult を返す。AdviceResult は
「数値（figures: 値・単位・意味）＋前提（assumptions）＋注意（notes）＋
利用者に確認が必要な入力（inputs_needed）＋出典（sources）」をまとめたもの。
`to_dict()` で JSON にそのまま出せる。

仕様は文章が正。各関数の docstring に、従った文章を書く。
- loan_term_comparison   … principles/02-loan-term.md、cases/01-loan-term-20-years.md
- wait_cost              … principles/01-exit-first.md、principles/04-rent-vs-buy.md、principles/99-out-of-scope.md
- borrowing_budget       … principles/03-budget.md
- balance_sheet          … calc/balance-sheet.md（principles/01-exit-first.md 判断手順3・4）
- selling_costs_breakdown … assumptions.yaml の selling_costs、model.selling_costs

金額の単位はすべて円、率は %。文章に式がない部分は推測で作らず、inputs_needed に回す。
文章が曖昧で解釈した箇所には【解釈】を付けた。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import loan as _loan
from .assumptions import Assumptions, load_assumptions
from .model import brokerage_fee as _brokerage_fee
from .model import selling_costs as _selling_costs

__all__ = [
    "DISCLAIMER",
    "Figure",
    "AdviceResult",
    "loan_term_comparison",
    "wait_cost",
    "borrowing_budget",
    "balance_sheet",
    "selling_costs_breakdown",
]

MAN = 10_000

# --- assumptions.yaml に未収録で、文章にだけある値（yaml に追加されたら置き換える） ---
# principles/02-loan-term.md 判断手順4「運用益には約20%課税される」（yaml は loan.term_shortening_breakeven の文中のみ）
INVESTMENT_GAIN_TAX_PCT = 20.0
# principles/01・04・99「完済の遅れと健康リスクを加味すると、5%以上の下落がないと得にならない」
WAIT_PRACTICAL_DROP_PCT = 5.0
# principles/01・04「2008年5月→2009年4月」、99・04「見直し要」: データは2023年4月まで
HISTORICAL_MAX_DROP_PERIOD = "2008年5月→2009年4月、リーマンショック時"
HISTORICAL_MAX_DROP_SCOPE = "首都圏中古マンション"
HISTORICAL_MAX_DROP_DATA_UNTIL = "2023年4月"
# principles/02 判断手順5「年収400万円以上で35%」
REPAYMENT_RATIO_INCOME_FLOOR_YEN = 4_000_000
# calc/balance-sheet.md「入力」の既定値
BS_SAVINGS_YEN = 4_000_000
BS_SECURITIES_YEN = 1_000_000
BS_OTHER_ASSETS_YEN = 1_000_000
BS_TERM_YEARS = 50  # 「最長で組む前提（標準の初期値は50年）」
BS_FIRST_HORIZON_YEARS = 10  # 「10年後と、利用者が指定した年（既定20年）」
BS_DEFAULT_TARGET_YEAR = 20


# AGENTS.md「回答の姿勢」: 金額の試算を示したら末尾に必ず添える趣旨
DISCLAIMER = ("この試算は一般的な前提に基づく概算です。金利・税制は変わるため、"
              "最終的な判断は金融機関やファイナンシャルプランナーなどの専門家と行ってください。")


@dataclass(frozen=True)
class Figure:
    """1つの数値と、その意味。"""

    value: Any
    unit: str
    meaning: str

    def to_dict(self) -> dict:
        return {"value": self.value, "unit": self.unit, "meaning": self.meaning}


@dataclass
class AdviceResult:
    topic: str
    summary: str
    figures: Dict[str, Figure] = field(default_factory=dict)
    assumptions: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    inputs_needed: List[Dict[str, str]] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)
    disclaimer: str = DISCLAIMER

    def value(self, key: str):
        return self.figures[key].value

    def add(self, key: str, value: Any, unit: str, meaning: str) -> None:
        if isinstance(value, float):
            value = round(value, 4) if unit in ("%", "倍", "%/年") else round(value)
        self.figures[key] = Figure(value, unit, meaning)

    def to_dict(self) -> dict:
        return {
            "topic": self.topic,
            "summary": self.summary,
            "figures": {k: f.to_dict() for k, f in self.figures.items()},
            "assumptions": list(self.assumptions),
            "notes": list(self.notes),
            "inputs_needed": list(self.inputs_needed),
            "sources": list(self.sources),
            "disclaimer": self.disclaimer,
        }


def _a(assumptions: Optional[Assumptions]) -> Assumptions:
    return assumptions if assumptions is not None else load_assumptions()


def _man(yen: float) -> str:
    return f"{yen / MAN:,.0f}万円"


def _positive(name: str, v: float) -> None:
    if v is None or v <= 0:
        raise ValueError(f"{name} は0より大きい値")


# ---------------------------------------------------------------------------
# 1. 借入期間の比較
# ---------------------------------------------------------------------------

def _paid(principal: float, rate: float, term: int, months: int) -> float:
    return _loan.monthly_payment(principal, rate, term) * max(0, min(months, 12 * term))


def _fv_of_payment_gap(principal: float, rate: float, long_t: int, short_t: int,
                       months: int, after_tax_yield_pct: float) -> float:
    """月ごとの返済差（短期 − 長期）を、長期側が税引後利回りで月複利運用したときの months カ月後の額。

    短期側が完済した後は差が負になる（短期側に余力が出る）。これは長期側の取り崩し
    ＝短期側が同じ利回りで運用するのと同じ扱いにした【解釈】。
    """
    m_long = _loan.monthly_payment(principal, rate, long_t)
    m_short = _loan.monthly_payment(principal, rate, short_t)
    r = after_tax_yield_pct / 100 / 12
    fv = 0.0
    for k in range(1, months + 1):
        pay_l = m_long if k <= 12 * long_t else 0.0
        pay_s = m_short if k <= 12 * short_t else 0.0
        fv = fv * (1 + r) + (pay_s - pay_l)
    return fv


def loan_term_comparison(
    principal_yen: float,
    annual_rate_pct: Optional[float] = None,
    long_term_years: Optional[int] = None,
    short_term_years: int = 20,
    sale_year: Optional[int] = None,
    investment_yield_pct: Optional[float] = None,
    investment_tax_rate_pct: float = INVESTMENT_GAIN_TAX_PCT,
    annual_income_yen: Optional[float] = None,
    screening_rate_pct: Optional[float] = None,
    assumptions: Optional[Assumptions] = None,
) -> AdviceResult:
    """借入期間（例: 35年と20年）を比べる。principles/02-loan-term.md、cases/01-loan-term-20-years.md。

    - 毎月返済額・総利息（完済まで）
    - 売却年を渡せば、その時点の残債・支払総額・支払利息と、残債差・追加支払・正味差（=利息差）
    - 差額の使い道（現金で置く／税引後利回りで運用する）と分岐利回り（=ローン金利）
    - 年収を渡せば、審査金利での年間返済額と返済比率
    """
    a = _a(assumptions)
    rate = a.interest_rate_pct if annual_rate_pct is None else annual_rate_pct
    long_t = a.loan_term_years if long_term_years is None else long_term_years
    short_t = short_term_years
    _positive("principal_yen", principal_yen)
    if rate < 0:
        raise ValueError("annual_rate_pct は0以上")
    if not (0 < short_t < long_t):
        raise ValueError("short_term_years は 0 より大きく long_term_years より短いこと")
    if sale_year is not None and not (1 <= sale_year <= long_t):
        raise ValueError(f"sale_year は 1〜{long_t} 年")
    if not (0 <= investment_tax_rate_pct < 100):
        raise ValueError("investment_tax_rate_pct は 0以上100未満")

    L = principal_yen
    res = AdviceResult(
        topic="loan_term_comparison",
        summary="",
        sources=["principles/02-loan-term.md", "cases/01-loan-term-20-years.md", "assumptions.yaml loan.*"],
    )
    res.assumptions += [
        f"借入額 {_man(L)}、金利 {rate}%（全期間一定）、元利均等・月次返済。",
        f"比較する期間: {long_t}年と{short_t}年。",
        "金利の既定値1.0%（interest_rate.simulation_default_pct）は2023〜2024年のスナップショット。回答時点の実勢金利と並べて示す。",
        "税・繰上返済手数料・団信・住宅ローン控除は数値に含めない。",
    ]

    for t in (long_t, short_t):
        m = _loan.monthly_payment(L, rate, t)
        interest = _loan.interest_paid_months(L, rate, t, 12 * t)
        res.add(f"term_{t}y.monthly_payment_yen", m, "円/月", f"{t}年で組んだときの毎月返済額")
        res.add(f"term_{t}y.total_interest_yen", interest, "円", f"{t}年で組み、完済まで持ったときの総利息")
        res.add(f"term_{t}y.total_payment_yen", m * 12 * t, "円", f"{t}年で組み、完済まで持ったときの支払総額")
    m_long = _loan.monthly_payment(L, rate, long_t)
    m_short = _loan.monthly_payment(L, rate, short_t)
    gap = m_short - m_long
    res.add("monthly_payment_gap_yen", gap, "円/月",
            f"{short_t}年のほうが毎月多く払う額（＝{long_t}年で組んだ場合に毎月浮く差額）")
    total_interest_gap = (_loan.interest_paid_months(L, rate, long_t, 12 * long_t)
                          - _loan.interest_paid_months(L, rate, short_t, 12 * short_t))
    res.add("total_interest_gap_to_maturity_yen", total_interest_gap, "円",
            "完済まで持った場合の総利息の差（長期 − 短期）。売却時期を置かない数字なので、これだけで結論を出さない")

    horizon_years = sale_year if sale_year is not None else long_t
    months = 12 * horizon_years
    if sale_year is not None:
        for t in (long_t, short_t):
            res.add(f"sale.term_{t}y.balance_yen", _loan.balance_after_months(L, rate, t, months), "円",
                    f"{t}年で組み、{sale_year}年目に売却するときの残債")
            res.add(f"sale.term_{t}y.paid_yen", _paid(L, rate, t, months), "円",
                    f"{t}年で組んだ場合の、{sale_year}年目までの返済の支払総額")
            res.add(f"sale.term_{t}y.interest_paid_yen", _loan.interest_paid_months(L, rate, t, months), "円",
                    f"{t}年で組んだ場合の、{sale_year}年目までに払った利息")
        bal_gap = (_loan.balance_after_months(L, rate, long_t, months)
                   - _loan.balance_after_months(L, rate, short_t, months))
        extra_paid = _paid(L, rate, short_t, months) - _paid(L, rate, long_t, months)
        res.add("sale.balance_gap_yen", bal_gap, "円",
                f"売却時の残債の差（{long_t}年 − {short_t}年）。手残り（売却額 − 売却諸費用 − 残債）は{short_t}年のほうがこの額だけ多い")
        res.add("sale.extra_paid_by_short_yen", extra_paid, "円",
                f"{sale_year}年目までに{short_t}年側が多く払った現金。残債差の原資")
        res.add("sale.net_advantage_short_yen", bal_gap - extra_paid, "円",
                f"正味の差（残債差 − 追加支払）＝{sale_year}年目までの利息削減分。純資産（手残り＋手元に残った現金）の差はこれだけ")
        res.notes.append("「売却時の手残りは期間で変わらない」は誤り。残債が違うので手残りは短期のほうが多い。"
                         "期間でほぼ変わらないのは純資産（手残り＋手元に残った現金）で、正味の差は利息削減分だけ。")

    # 差額の使い道
    interest_gap_h = (_loan.interest_paid_months(L, rate, long_t, months)
                      - _loan.interest_paid_months(L, rate, short_t, months))
    res.add("use_of_gap.cash.short_advantage_yen", interest_gap_h, "円",
            f"差額を使う・現金で置くだけなら、{horizon_years}年目時点で{short_t}年のほうが有利な額（＝利息削減分）")
    tax = investment_tax_rate_pct / 100
    res.add("use_of_gap.breakeven_after_tax_yield_pct", rate, "%",
            "差額を運用して長期側が有利になる分岐の利回り（税引後）。必ずローン金利に一致する")
    res.add("use_of_gap.breakeven_pre_tax_yield_pct", rate / (1 - tax), "%",
            f"上の分岐を税引前に直した利回り（運用益に{investment_tax_rate_pct:g}%課税として）")
    if investment_yield_pct is not None:
        after = investment_yield_pct * (1 - tax)
        fv = _fv_of_payment_gap(L, rate, long_t, short_t, months, after)
        bal_gap_h = (_loan.balance_after_months(L, rate, long_t, months)
                     - _loan.balance_after_months(L, rate, short_t, months))
        res.add("use_of_gap.invest.after_tax_yield_pct", after, "%",
                f"税引後の運用利回り（税引前{investment_yield_pct}% × (1 − {investment_tax_rate_pct:g}%)）")
        res.add("use_of_gap.invest.fund_value_yen", fv, "円",
                f"{long_t}年で組み、毎月の差額を税引後利回りで積み立てた場合の{horizon_years}年目の運用残高")
        res.add("use_of_gap.invest.long_minus_short_net_worth_yen", fv - bal_gap_h, "円",
                f"{horizon_years}年目の純資産の差（{long_t}年＋運用 − {short_t}年）。正なら長期＋運用が有利")
        res.assumptions.append(
            "運用は毎月末に差額を積み立て、税引後利回りで月複利。課税は「利回り × (1 − 税率)」で毎期差し引く近似"
            "（実際は売却時にまとめて課税されることが多く、その場合は運用側がわずかに有利になる）【解釈】。")
    res.notes += [
        "利回りは保証されず元本割れもある。特定の運用商品・金融機関は勧めない（investment.scope、principles/99-out-of-scope.md）。判断は利用者に返す。",
        a.term_shortening_deduction_note,
    ]

    # 審査金利での返済比率
    srate = a.screening_rate_pct if screening_rate_pct is None else screening_rate_pct
    if annual_income_yen is not None:
        _positive("annual_income_yen", annual_income_yen)
        for t in (long_t, short_t):
            annual = _loan.monthly_payment(L, srate, t) * 12
            ratio = annual / annual_income_yen * 100
            res.add(f"screening.term_{t}y.annual_payment_yen", annual, "円/年",
                    f"審査金利{srate}%・{t}年で計算した年間返済額（銀行が審査で使う仮の額）")
            res.add(f"screening.term_{t}y.repayment_ratio_pct", ratio, "%",
                    f"上の年間返済額 ÷ 年収（返済比率）。上限の目安は{a.repayment_ratio_limit_pct:g}%")
            res.add(f"screening.term_{t}y.within_limit", ratio <= a.repayment_ratio_limit_pct, "真偽",
                    f"返済比率が上限{a.repayment_ratio_limit_pct:g}%以内か（目安。審査の可否は銀行が決める）")
        res.assumptions.append(
            f"審査金利 {srate}%（interest_rate.screening_rate_pct、3〜4%が一般的）、返済比率の上限 "
            f"{a.repayment_ratio_limit_pct:g}%（年収400万円以上の一般的な基準）。銀行ごとに異なる。")
        if annual_income_yen < REPAYMENT_RATIO_INCOME_FLOOR_YEN:
            res.notes.append("年収400万円未満の返済比率の上限は文章に数値がない。銀行に確認する。")
        res.notes.append("期間短縮は返済比率を押し上げ、審査否決や減額回答につながる。試算は目安で「通ります」とは言わない。")
    else:
        res.inputs_needed.append({"name": "annual_income_yen",
                                  "why": "審査金利での返済比率（期間短縮で審査に通りにくくなるか）を出すため"})
    if sale_year is None:
        res.inputs_needed.append({"name": "sale_year",
                                  "why": "何年住む予定か（売却時期）。売却時の残債と正味差で比べるため。最初に確認する"})

    res.notes += [
        "期間は後から短くできるが、長くはできない（期間延長は条件変更＝返済困難の申し出扱いになり、信用情報に影響しうる）。",
        f"対案: {long_t}年で組んで差額を期間短縮型で繰上返済すれば、{short_t}年とほぼ同じ利息削減ができ、毎月の約束された返済額は低いまま固定される。収入が落ちた年は止められる。",
        a.prepayment_how_to_mention,
        a.prepayment_type_note,
        f"団信は残債がある期間だけ有効。{long_t}年なら保障は最長{long_t}年、{short_t}年なら{short_t}年で終わる。",
        f"{short_t}年が合理的になる条件: 定年までの完済を最優先し、諸費用と予備資金（生活費1年分程度）を払っても手元資金に十分な余裕があり、繰上返済を自律的に実行できない自覚がある人。",
        "確認すること: 何年住む予定か、諸費用・手付金を払った後の現預金、収入変動の見込み、年齢と健康状態、金利上昇への耐性。",
    ]
    res.summary = (
        f"{_man(L)}・金利{rate}%: {long_t}年は月{m_long / MAN:.1f}万円、{short_t}年は月{m_short / MAN:.1f}万円。"
        f"完済まで持てば総利息の差は{_man(total_interest_gap)}だが、"
        + (f"{sale_year}年目に売るなら正味の差は利息削減分の{_man(interest_gap_h)}。" if sale_year else "売却時期で差は縮む。")
        + f"差額をローン金利{rate}%（税引後）より高く運用できれば{long_t}年が有利。"
    )
    return res


# ---------------------------------------------------------------------------
# 2. 待つコスト
# ---------------------------------------------------------------------------

def wait_cost(
    property_price_yen: float,
    monthly_rent_yen: float,
    wait_years: float = 1,
    assumptions: Optional[Assumptions] = None,
) -> AdviceResult:
    """「安くなるまで待つべきか」に、待つコストを数字で返す。

    principles/01-exit-first.md 判断手順6、principles/04-rent-vs-buy.md 判断手順4、
    principles/99-out-of-scope.md 項目5。式は「年間家賃 ÷ 物件価格 ＝ 待って得をするための最低下落率」。
    """
    a = _a(assumptions)
    _positive("property_price_yen", property_price_yen)
    _positive("monthly_rent_yen", monthly_rent_yen)
    _positive("wait_years", wait_years)
    annual_rent = monthly_rent_yen * 12
    breakeven = annual_rent / property_price_yen * 100
    rent_paid = annual_rent * wait_years
    cum_pct = rent_paid / property_price_yen * 100

    res = AdviceResult(
        topic="wait_cost",
        summary="",
        sources=["principles/01-exit-first.md 判断手順6", "principles/04-rent-vs-buy.md 判断手順4",
                 "principles/99-out-of-scope.md 項目5", "assumptions.yaml holding.*"],
    )
    res.add("annual_rent_yen", annual_rent, "円/年", "待っている間に払う年間家賃（掛け捨て）")
    res.add("breakeven_drop_pct_per_year", breakeven, "%/年",
            "待って得をするための最低下落率（年間家賃 ÷ 物件価格）。1年待つなら物件価格がこれ以上下がらないと損")
    res.add("rent_paid_while_waiting_yen", rent_paid, "円", f"{wait_years:g}年待つ間に払う家賃の合計")
    res.add("breakeven_cumulative_drop_pct", cum_pct, "%",
            f"{wait_years:g}年待つ間の家賃合計を取り戻すのに必要な、待つ期間全体での下落率（家賃合計 ÷ 物件価格）")
    res.add("breakeven_cumulative_drop_yen", rent_paid, "円",
            f"{wait_years:g}年後に、物件価格が今より最低これだけ下がっていないと待った分の家賃を取り戻せない額")
    res.add("practical_drop_pct_per_year", WAIT_PRACTICAL_DROP_PCT, "%/年",
            "完済の遅れと健康悪化でローン条件が悪くなるリスクを加味した、得をするための目安（文章の標準ケース 家賃15万円・5,000万円での値）")
    res.add("historical_max_drop_pct", a.historical_max_drop_pct, "%",
            f"過去最大の下落。{HISTORICAL_MAX_DROP_SCOPE}、{HISTORICAL_MAX_DROP_PERIOD}。価格は同程度の期間で戻った")
    res.add("payoff_delay_years", wait_years, "年", "待った年数だけ完済時年齢が遅れる")
    res.add("breakeven_exceeds_practical", breakeven >= WAIT_PRACTICAL_DROP_PCT, "真偽",
            "損益分岐の下落率がすでに5%の目安以上か")
    res.add("breakeven_exceeds_historical_max", breakeven >= a.historical_max_drop_pct, "真偽",
            "損益分岐の下落率が過去最大の下落（6.3%）以上か。真なら、過去に一度も起きていない下落を待つことになる")

    res.assumptions += [
        f"物件価格 {_man(property_price_yen)}、家賃 月{monthly_rent_yen / MAN:g}万円、待つ年数 {wait_years:g}年。",
        "損益分岐は単純比（年間家賃 ÷ 物件価格）。待つ間の家賃上昇・金利変化・諸費用の変化は含めない。",
        f"5%の目安は文章の標準ケース（3.6%）に完済の遅れ・健康リスクを加味したもので、式から出した値ではない。"
        f"利用者の数値で損益分岐が5%を超えるなら、目安はそれより高くなる。",
        f"過去最大の下落6.3%は{HISTORICAL_MAX_DROP_SCOPE}のデータで、期間は{HISTORICAL_MAX_DROP_PERIOD}（約1年）。"
        f"データは{HISTORICAL_MAX_DROP_DATA_UNTIL}までで、2024年以降の金利上昇局面は未検証（見直し要）。",
    ]
    if wait_years > 1:
        res.assumptions.append(
            f"6.3%は約1年間の下落。{wait_years:g}年待つ場合の必要下落率（期間全体で{cum_pct:.1f}%）と直接は比べられない。")
    res.notes += [
        "市況は予測しない。「今は買い時・待ち時」とは言わず、待つコストを示して判断は利用者に返す。",
        "年5%超の下落は過去に1度しか起きていない局面を当てる賭けである。",
        "金利が上がると「家賃と同等の月返済になる物件価格」が下がるので、利用者の数値で計算し直す。",
        f"判断の軸は、{a.holding_min_years}年以上住むか（holding.minimum_years_to_buy）と、ローンを好条件で組めるか。",
        "今の相場の実績は成約ベースの統計（レインズ・東京カンテイ・不動産価格指数）で出典つきで示す。売出価格を成約価格のように扱わない（market_data）。",
    ]
    res.summary = (
        f"家賃 年{_man(annual_rent)} ÷ 物件{_man(property_price_yen)} ＝ 年{breakeven:.1f}%下がらないと待つほうが損。"
        f"完済の遅れ・健康リスクを加味すると目安は年{WAIT_PRACTICAL_DROP_PCT:g}%以上。"
        f"過去最大の下落は{a.historical_max_drop_pct:g}%（{HISTORICAL_MAX_DROP_PERIOD}）。"
    )
    return res


# ---------------------------------------------------------------------------
# 3. 借りられる額と借りるべき額
# ---------------------------------------------------------------------------

def borrowing_budget(
    annual_income_yen: float,
    other_debt_annual_payment_yen: float = 0,
    term_years: Optional[int] = None,
    screening_rate_pct: Optional[float] = None,
    repayment_ratio_limit_pct: Optional[float] = None,
    property_price_yen: Optional[float] = None,
    property_type: Optional[str] = None,
    affordable_monthly_payment_yen: Optional[float] = None,
    stress_rate_pct: Optional[float] = None,
    assumptions: Optional[Assumptions] = None,
) -> AdviceResult:
    """「借りられる額」（銀行の上限）と「借りるべき額」（ライフプランからの逆算）を分けて出す。principles/03-budget.md。

    借りられる額 = 審査金利・期間で、年間返済額が 年収 × 返済比率上限（− 他の借入の年間返済）に収まる借入額。
    借りるべき額の式は文章にない（ライフプランから逆算し、FPに作成を依頼する）。そのため推測では作らず、
    必要な入力を inputs_needed で返す。利用者が「毎月返済に回せる上限」と「悲観シナリオの金利」を
    自分で決めて渡した場合だけ、それを借入額に換算する（ローンの式による換算で、予算の判断ではない）。

    property_type: "new_condo"（新築マンション 4.5%）/ "used_or_house"（新築戸建て・中古 7.5%）/ None（7%）。
    """
    a = _a(assumptions)
    _positive("annual_income_yen", annual_income_yen)
    if other_debt_annual_payment_yen < 0:
        raise ValueError("other_debt_annual_payment_yen は0以上")
    term = a.loan_term_years if term_years is None else term_years
    srate = a.screening_rate_pct if screening_rate_pct is None else screening_rate_pct
    limit = a.repayment_ratio_limit_pct if repayment_ratio_limit_pct is None else repayment_ratio_limit_pct
    _positive("term_years", term)

    res = AdviceResult(
        topic="borrowing_budget",
        summary="",
        sources=["principles/03-budget.md", "principles/02-loan-term.md 判断手順5",
                 "assumptions.yaml loan.*・interest_rate.screening_rate_pct・purchase_costs.*"],
    )
    allowable = max(0.0, annual_income_yen * limit / 100 - other_debt_annual_payment_yen)
    per_yen_annual = _loan.monthly_payment(1.0, srate, term) * 12
    lendable = allowable / per_yen_annual
    res.add("lendable.allowable_annual_payment_yen", allowable, "円/年",
            f"返済比率の上限から出した、住宅ローンに使える年間返済額（年収 × {limit:g}% − 他の借入の年間返済）")
    res.add("lendable.max_principal_yen", lendable, "円",
            f"借りられる額の上限の目安（審査金利{srate}%・{term}年で上の年間返済額になる借入額）。銀行が回収できると見込む上限であり、借りるべき額ではない")
    res.add("lendable.income_multiple", lendable / annual_income_yen, "倍", "上の借入額が年収の何倍か")
    lo, hi, mlo, mhi = a.income_multiple_range
    res.add("lendable.income_multiple_typical_min_yen", annual_income_yen * lo, "円", f"年収倍率の目安 {lo:g}倍の額")
    res.add("lendable.income_multiple_typical_max_yen", annual_income_yen * hi, "円", f"年収倍率の目安 {hi:g}倍の額")
    res.add("lendable.income_multiple_screening_max_yen", annual_income_yen * mhi, "円",
            f"審査上の最大 {mlo:g}〜{mhi:g}倍の上端の額")
    res.assumptions += [
        f"年収 {_man(annual_income_yen)}、他の借入の年間返済 {_man(other_debt_annual_payment_yen)}（車・奨学金・カード分割は返済比率に合算される）。",
        f"審査金利 {srate}%（3〜4%が一般的）、期間 {term}年、返済比率の上限 {limit:g}%（年収400万円以上の一般的な基準）。銀行ごとに異なる（見直し要）。",
        f"年収倍率の目安: {a.income_multiple_lendable}（loan.income_multiple_lendable）。",
    ]
    if annual_income_yen < REPAYMENT_RATIO_INCOME_FLOOR_YEN:
        res.inputs_needed.append({"name": "repayment_ratio_limit_pct",
                                  "why": "年収400万円未満の返済比率の上限は文章に数値がない。銀行に確認した値を入れる"})

    # 借りるべき額
    res.inputs_needed += [
        {"name": "現在の収支", "why": "借りるべき額はライフプランから逆算する（判断手順3）。現在の収入と支出を書き出す"},
        {"name": "将来の収入推移", "why": "悲観シナリオでは収入は大きく上がらない（据え置き）で置く（inflation.income_note）。昇給の確実性、片方が働き方を変える可能性、定年と退職金"},
        {"name": "家族構成の変化と教育費", "why": "子供1人を成人まで育てる費用は最低約2,000万円、中学〜大学（文系）の私立と公立の差は約600万円。未定なら複数パターンで"},
        {"name": "介護費・老後資金", "why": "時間軸に置く支出"},
        {"name": "貯蓄力・支出の見込み", "why": "悲観シナリオでは貯蓄力は自己申告より低め、支出は高めに置く"},
        {"name": "affordable_monthly_payment_yen", "why": "上のライフプランから出した、毎月の住宅ローン返済に回せる上限（利用者・FPが決める値。AIは推測しない）"},
        {"name": "stress_rate_pct", "why": "悲観シナリオの金利。文章は「高め」とだけ書き、数値を定めていない。利用者が決める"},
        {"name": "手元の金融資産と購入に充ててよい額", "why": "諸費用と手付金は現金で必要。緊急予備資金や運用資産を回す前提になっていないか"},
        {"name": "ペアローンの有無と比率", "why": "7:3か8:2が目安（loan.pair_loan_ratio_recommended）。事前審査の前に決める"},
    ]
    if affordable_monthly_payment_yen is not None and stress_rate_pct is not None:
        _positive("affordable_monthly_payment_yen", affordable_monthly_payment_yen)
        should = affordable_monthly_payment_yen / _loan.monthly_payment(1.0, stress_rate_pct, term)
        res.add("should_borrow.principal_from_affordable_payment_yen", should, "円",
                f"利用者が決めた毎月返済の上限 月{affordable_monthly_payment_yen / MAN:g}万円を、悲観金利{stress_rate_pct}%・{term}年で借入額に換算した値")
        res.add("should_borrow.gap_to_lendable_yen", lendable - should, "円",
                "借りられる額との差（借りられる額 − 借りるべき額）。正なら銀行の上限まで借りない余白")
        res.inputs_needed = [x for x in res.inputs_needed
                             if x["name"] not in ("affordable_monthly_payment_yen", "stress_rate_pct")]
        res.assumptions.append("借りるべき額の換算は、利用者が決めた月額と金利をローンの式で借入額に直しただけ。月額そのものの妥当性はライフプラン（FP）で確かめる。")

    if property_price_yen is not None:
        _positive("property_price_yen", property_price_yen)
        if property_type == "new_condo":
            pct, label = a.purchase_new_condo_pct, "新築マンション（4〜5%）"
        elif property_type == "used_or_house":
            pct, label = a.purchase_used_or_house_pct, "新築戸建て・中古（7〜8%）"
        elif property_type is None:
            pct, label = a.purchase_cost_pct, "種別未指定（シミュレーション既定値）"
        else:
            raise ValueError("property_type は 'new_condo' / 'used_or_house' / None")
        costs = property_price_yen * pct / 100
        earnest = property_price_yen * a.earnest_money_pct / 100
        res.add("cash.purchase_costs_yen", costs, "円", f"購入諸費用の目安（物件価格 × {pct:g}%、{label}）。現金で用意する")
        res.add("cash.earnest_money_yen", earnest, "円",
                f"手付金の目安（物件価格 × {a.earnest_money_pct:g}%、文章は5〜10%）。契約時に現金で必要（売買代金に充当される）")
        res.add("cash.total_upfront_yen", costs + earnest, "円", "契約〜引渡しまでに現金で用意する額の目安（諸費用＋手付金）")
        res.add("property_price_to_lendable_ratio", property_price_yen / lendable if lendable else None, "倍",
                "物件価格が借りられる額の上限の何倍か（1を超えれば上限を超える）")

    res.notes += [
        "最初に「借りられる額と借りるべき額は別物」と伝える。年収倍率だけで「買えます」と言わない。",
        a.income_multiple_caution,
        "借りるべき額はライフプランから悲観シナリオ（金利は高め、収入は大きく上がらない、貯蓄力は自己申告より低め、支出は高め）で逆算する。作成は独立系FPに依頼する（不動産会社所属のFPは避ける）。",
        "限度額いっぱい（過大）も、今の家賃ベース（過小）も失敗パターン。",
        "予算が届かないときは価格ではなく条件（広さ・築年数・駅徒歩）を動かす。",
        "物件価格は全額借入れでも構わない。頭金の利息軽減効果は低金利下では薄い（金利0.7%で300万円入れても35年の総支払額の差は約38万円）。",
        "具体的な借入上限は銀行の審査でしか確定しない。複数行（ネット銀行と都市銀行・地方銀行）に事前審査を出す。",
    ]
    res.summary = (
        f"借りられる額（銀行の上限）: 審査金利{srate}%・{term}年・返済比率{limit:g}%で約{_man(lendable)}"
        f"（年収の{lendable / annual_income_yen:.1f}倍）。これは借りるべき額ではない。"
        "借りるべき額はライフプランから悲観シナリオで逆算するので、inputs_needed の値を利用者に確認する。"
    )
    return res


# ---------------------------------------------------------------------------
# 4. 住宅購入バランスシート
# ---------------------------------------------------------------------------

def balance_sheet(
    property_price_yen: float = 50_000_000,
    savings_yen: float = BS_SAVINGS_YEN,
    securities_yen: float = BS_SECURITIES_YEN,
    other_assets_yen: float = BS_OTHER_ASSETS_YEN,
    existing_debt_yen: float = 0,
    loan_ratio_pct: float = 100,
    purchase_cost_pct: Optional[float] = None,
    term_years: int = BS_TERM_YEARS,
    annual_rate_pct: Optional[float] = None,
    depreciation_pct_per_year: Optional[float] = None,
    target_year: int = BS_DEFAULT_TARGET_YEAR,
    assumptions: Optional[Assumptions] = None,
) -> AdviceResult:
    """現在・購入直後・N年後（10年後と target_year）の資産・負債・純資産。calc/balance-sheet.md。

    depreciation_pct_per_year は下落を正の値で渡す（1.5 なら毎年1.5%下落）。
    """
    a = _a(assumptions)
    rate = a.interest_rate_pct if annual_rate_pct is None else annual_rate_pct
    cost_pct = a.purchase_cost_pct if purchase_cost_pct is None else purchase_cost_pct
    dep = a.depreciation_condo_pct if depreciation_pct_per_year is None else depreciation_pct_per_year
    _positive("property_price_yen", property_price_yen)
    if not (0 <= loan_ratio_pct <= 100):
        raise ValueError("loan_ratio_pct は 0〜100")
    if target_year < 1:
        raise ValueError("target_year は1以上")

    P = property_price_yen
    loan = P * loan_ratio_pct / 100
    costs = P * cost_pct / 100
    res = AdviceResult(
        topic="balance_sheet",
        summary="",
        sources=["calc/balance-sheet.md", "principles/01-exit-first.md 判断手順3・4"],
    )
    res.assumptions += [
        f"現在の資産: 貯金{_man(savings_yen)}・有価証券{_man(securities_yen)}・その他{_man(other_assets_yen)}、既存の借入{_man(existing_debt_yen)}。",
        f"物件価格{_man(P)}、借入割合{loan_ratio_pct:g}%、購入諸費用{cost_pct:g}%、{term_years}年・金利{rate}%（元利均等）。",
        f"物件価値は年{dep:g}%下落（市況変化なし＋経年減価。depreciation.condo_pct_per_year）。",
        "N年後の現金・有価証券・その他は購入直後と同じ（家計の貯蓄は別扱い）。既存の借入も据え置き【解釈】。",
        "税・売却費用は含めない。将来の利益や資産を保証しない。",
    ]

    now_assets = savings_yen + securities_yen + other_assets_yen
    res.add("now.assets_yen", now_assets, "円", "現在の資産合計（貯金＋有価証券＋その他）")
    res.add("now.liabilities_yen", existing_debt_yen, "円", "現在の負債（既存の借入）")
    res.add("now.net_worth_yen", now_assets - existing_debt_yen, "円", "現在の純資産")

    cash = savings_yen - costs - (P - loan)
    after_assets = cash + securities_yen + other_assets_yen + P
    after_liab = existing_debt_yen + loan
    res.add("after_purchase.cash_yen", cash, "円", "購入直後の現金（貯金 − 諸費用 − 頭金）")
    res.add("after_purchase.house_yen", P, "円", "購入直後の住宅（物件価格）")
    res.add("after_purchase.assets_yen", after_assets, "円", "購入直後の資産合計")
    res.add("after_purchase.liabilities_yen", after_liab, "円", "購入直後の負債（ローン＋既存の借入）")
    res.add("after_purchase.net_worth_yen", after_assets - after_liab, "円",
            "購入直後の純資産。諸費用の分だけ減る（掛け捨ての家賃を資産に変える入口の費用）")
    res.add("after_purchase.net_worth_change_yen", (after_assets - after_liab) - (now_assets - existing_debt_yen), "円",
            "購入による純資産の変化（＝ −諸費用）")
    if cash < 0:
        res.notes.append(f"購入直後の現金がマイナス（{_man(cash)}）。諸費用・頭金を貯金で払えない。予備資金も残らない前提なので見直す。")

    for n in sorted({BS_FIRST_HORIZON_YEARS, target_year}):
        house = P * (1 - dep / 100) ** n
        bal = _loan.balance_after_months(loan, rate, term_years, 12 * n) if loan > 0 else 0.0
        assets = cash + securities_yen + other_assets_yen + house
        liab = existing_debt_yen + bal
        res.add(f"year_{n}.house_yen", house, "円", f"{n}年後の物件価値（物件価格 × (1 − {dep:g}%)^{n}）")
        res.add(f"year_{n}.loan_balance_yen", bal, "円", f"{n}年後のローン残債")
        res.add(f"year_{n}.assets_yen", assets, "円", f"{n}年後の資産合計")
        res.add(f"year_{n}.liabilities_yen", liab, "円", f"{n}年後の負債合計")
        res.add(f"year_{n}.net_worth_yen", assets - liab, "円", f"{n}年後の純資産")
        res.add(f"year_{n}.housing_equity_yen", house - bal, "円",
                f"{n}年後の住宅由来の純資産（物件価値 − 残債）。売却で手にできる「隠れた自己資本」（税・売却費用は除く）")
        res.add(f"year_{n}.net_worth_change_since_purchase_yen", (assets - liab) - (after_assets - after_liab), "円",
                f"購入直後から{n}年後までの純資産の増減。正ならローン返済のペースが物件価値の下落ペースを上回っている")

    res.notes += [
        "購入直後に現金は諸費用の分だけ減るが、現金が住宅という資産に置き換わっただけで、純資産の減少は諸費用分にとどまる。",
        "N年後に純資産が増えていれば「売って次に移る」自由がある。減っていく物件は価値の下落が返済を上回っている（資産性が低い、または借入期間が長すぎて元本が減っていない）。",
        "購入後は年1回、残債と物件価値を確認し「残債 < 物件価値」を保てているかを見る。",
        "予算はバランスシートではなくライフプランから決める（principles/03-budget.md）。",
        "下落率の置き方で結果が変わるので、数値を引くときは前提を添える。",
    ]
    tgt = max(BS_FIRST_HORIZON_YEARS, target_year)
    res.summary = (
        f"純資産は現在{_man(now_assets - existing_debt_yen)} → 購入直後{_man(after_assets - after_liab)}（諸費用分減少）。"
        f"{BS_FIRST_HORIZON_YEARS}年後の住宅由来の純資産は{_man(res.value(f'year_{BS_FIRST_HORIZON_YEARS}.housing_equity_yen'))}"
        + (f"、{tgt}年後は{_man(res.value(f'year_{tgt}.housing_equity_yen'))}。" if tgt != BS_FIRST_HORIZON_YEARS else "。")
    )
    return res


# ---------------------------------------------------------------------------
# 5. 売却諸費用の内訳
# ---------------------------------------------------------------------------

def selling_costs_breakdown(
    sale_price_yen: float,
    assumptions: Optional[Assumptions] = None,
) -> AdviceResult:
    """売却価格から、仲介手数料・その他費用・合計・率。assumptions.yaml selling_costs、model.selling_costs。"""
    a = _a(assumptions)
    _positive("sale_price_yen", sale_price_yen)
    fee = _brokerage_fee(sale_price_yen)
    total = _selling_costs(sale_price_yen, other_costs_yen=a.selling_other_costs_yen)
    rate = total / sale_price_yen * 100
    approx = _selling_costs(sale_price_yen, method="rate", rate_pct=a.selling_cost_rate_pct)

    res = AdviceResult(
        topic="selling_costs_breakdown",
        summary="",
        sources=["assumptions.yaml selling_costs", "scripts/rentbuy/model.py selling_costs・brokerage_fee",
                 "principles/01-exit-first.md 見直し要"],
    )
    res.add("brokerage_fee_yen", fee, "円", f"仲介手数料の上限（税込）。{a.selling_brokerage_fee_formula}")
    res.add("brokerage_fee_ex_tax_yen", fee / 1.1, "円", "仲介手数料の上限（税抜）")
    res.add("other_costs_yen", a.selling_other_costs_yen, "円",
            "その他費用（抵当権抹消の登記費用・司法書士報酬・印紙、ローンの一括返済手数料など。3〜5万円）")
    res.add("total_yen", total, "円", "売却諸費用の合計（仲介手数料＋その他費用）")
    res.add("total_rate_pct", rate, "%", "売却価格に対する売却諸費用の率")
    res.add("approx_rate_pct", a.selling_cost_rate_pct, "%", "率で概算するときの既定値")
    res.add("approx_total_yen", approx, "円", f"売却価格 × {a.selling_cost_rate_pct:g}% での概算")
    res.add("approx_minus_exact_yen", approx - total, "円", "概算と式による額の差（金額を出せるなら式のほうを使う）")
    res.assumptions += [
        f"売却諸費用 = {a.selling_brokerage_fee_formula} + {a.selling_other_costs_yen / MAN:g}万円（税込、仲介手数料は上限額）。",
        "仲介手数料の速算式は売却価格400万円超の場合。400万円以下は宅建業法の報酬規程の段階料率（200万円以下5%、200万〜400万円4%+2万円、税抜）で計算した【解釈：文章に式がないため】。",
        a.selling_rate_by_price_note,
    ]
    res.notes += [
        f"含まないもの: {a.selling_excluded}",
        "仲介手数料は上限額であり、実際の額は媒介契約で決まる。上限規定と消費税率が変わったら見直す。",
    ]
    res.summary = (
        f"売却価格{_man(sale_price_yen)}: 仲介手数料{_man(fee)}＋その他{a.selling_other_costs_yen / MAN:g}万円"
        f"＝{_man(total)}（{rate:.2f}%）。"
    )
    return res
