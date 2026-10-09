"""賃貸 vs 購入の比較計算（calc/rent-vs-buy.md の「賃貸側の計算」「購入側の計算」の実装）。

金額の単位はすべて円・名目。現在価値への割引はしない（原則7、`inflation.discounting`）。
年 k は 1 始まり（k = 1 が入居1年目）。N 年目の比較は「N 年目の末に売却・退去した場合」。

文章の式をそのまま実装し、文章が曖昧で解釈が必要な箇所には【解釈】コメントを付けた。
文章とコードが食い違ったら文章が正（README.md 参照）。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Optional

from . import loan as _loan
from .assumptions import Assumptions

__all__ = [
    "RentMove",
    "RentBuyInputs",
    "YearRow",
    "RentBuyResult",
    "selling_costs",
    "simulate",
    "inputs_from_assumptions",
]

MAN = 10_000  # 1万円


@dataclass(frozen=True)
class RentMove:
    """住み替え。after_years 年目から、今の相場ベースで rent_yen_per_month の部屋に移る。"""

    after_years: int
    rent_yen_per_month: float


@dataclass(frozen=True)
class RentBuyInputs:
    """比較の入力。既定値は calc/rent-vs-buy.md「入力」節の標準の初期値。

    通常は inputs_from_assumptions() で assumptions.yaml から作る。
    """

    # --- 共通 ---
    horizon_years: int = 40                 # 何年目まで表を出すか（N の候補の上限）
    # --- 賃貸側 ---
    rent_yen_per_month: float = 160_000     # R₁ 比較賃料（同等物件の相場）
    moves: tuple = (RentMove(5, 180_000), RentMove(20, 140_000))  # (n₂, R₂), (n₃, R₃)
    rent_growth_pct: float = 1.0            # g_rent
    renewal_fee_months: float = 1.0
    renewal_interval_years: int = 2
    initial_cost_months: float = 4.5
    include_danshin: bool = True            # 団信相当の保険料を賃貸側に立てるか
    danshin_premium_yen_per_month: float = 3_000
    rent_subsidy_yen_per_month: float = 0.0  # 家賃補助・経費化で賃貸側から差し引く額（名目・固定）
    # --- 購入側 ---
    price_yen: float = 50_000_000           # P
    loan_amount_yen: Optional[float] = None  # L。None なら P（フルローン）
    loan_term_years: int = 35               # T
    interest_rate_pct: float = 1.0          # i（固定金利）
    purchase_cost_pct: float = 7.0          # C = P × 7%
    management_fee_yen_per_month: float = 14_000
    management_fee_growth_pct: float = 0.5  # g_mgmt
    repair_reserve_yen_per_month: float = 14_000
    repair_reserve_growth_pct: float = 3.0  # g_rep（= 段階増額2.0% + π）
    property_tax_yen_per_year: float = 120_000
    renovation_yen: float = 1_000_000       # 10年ごとのリフォーム（今の価格）
    renovation_interval_years: int = 10
    deduction_rate_pct: float = 0.7         # 住宅ローン控除の控除率
    deduction_limit_yen: float = 20_000_000  # 借入限度額（中古「その他の住宅」2,000万円）
    deduction_years: int = 10
    inflation_pct: float = 1.0              # π
    price_pass_through_pct: float = 100.0   # α
    depreciation_pct: float = 1.5           # d（経年減価 中）
    include_selling_costs: bool = True
    selling_cost_method: str = "formula"    # "formula"（式）または "rate"（率で概算）
    selling_cost_rate_pct: float = 3.5
    selling_other_costs_yen: float = 40_000
    # --- 文章が一意に決めていない部分の解釈（既定は文章の字面どおり） ---
    renewal_schedule: str = "even_years"    # "even_years" | "since_move"
    rent_growth_from_year: int = 1          # R(k) = R_base × (1+g)^(k - 1 + rent_growth_from_year)
    initial_cost_at_start: bool = False     # 1年目にも賃貸の初期費用を計上するか

    def validate(self) -> None:
        if self.horizon_years < 1:
            raise ValueError("horizon_years は1以上")
        if self.rent_yen_per_month <= 0 or self.price_yen <= 0:
            raise ValueError("賃料と物件価格は正の値")
        if self.loan_term_years < 1:
            raise ValueError("loan_term_years は1以上")
        if not 0 <= self.depreciation_pct < 100:
            raise ValueError("depreciation_pct は0以上100未満")
        if self.selling_cost_method not in ("formula", "rate"):
            raise ValueError("selling_cost_method は 'formula' か 'rate'")
        if self.renewal_schedule not in ("even_years", "since_move"):
            raise ValueError("renewal_schedule は 'even_years' か 'since_move'")
        if self.renewal_interval_years < 1 or self.renovation_interval_years < 1:
            raise ValueError("更新・リフォームの間隔は1年以上")
        years = [m.after_years for m in self.moves]
        if any(y < 1 for y in years) or years != sorted(set(years)):
            raise ValueError("住み替えの年は1以上で、重複なく昇順に並べる")

    @property
    def loan_principal(self) -> float:
        return self.price_yen if self.loan_amount_yen is None else self.loan_amount_yen


@dataclass(frozen=True)
class YearRow:
    """k 年目の値。*_cum は1年目から k 年目までの累計。"""

    year: int
    # 賃貸側
    rent_monthly: float
    rent_annual: float
    renewal_fee: float
    initial_cost: float
    danshin_premium: float
    rent_subsidy: float
    rent_year_total: float
    rent_cum: float                 # Cost_rent(k)
    # 購入側
    loan_payment: float
    management_fee: float
    repair_reserve: float
    property_tax: float
    mortgage_deduction: float       # 正の値（キャッシュアウトから引く額）
    renovation: float
    buy_year_cash: float
    cashout_cum: float              # CashOut(k)（購入諸費用 C を含む）
    property_value: float           # V_k
    loan_balance: float             # B_k
    selling_cost: float             # S_k
    equity_on_sale: float           # E_k = V_k − S_k − B_k
    buy_net: float                  # Cost_buy(k) = CashOut(k) − E_k
    buy_minus_rent: float           # 負なら購入が有利


@dataclass(frozen=True)
class RentBuyResult:
    inputs: RentBuyInputs
    purchase_cost: float
    monthly_payment: float
    rows: tuple
    breakeven_year: Optional[int]   # Cost_buy < Cost_rent となる最初の年。範囲内に無ければ None

    def row(self, year: int) -> YearRow:
        return self.rows[year - 1]

    def to_dict(self) -> dict:
        d = {
            "inputs": asdict(self.inputs),
            "purchase_cost": self.purchase_cost,
            "monthly_payment": self.monthly_payment,
            "breakeven_year": self.breakeven_year,
            "rows": [asdict(r) for r in self.rows],
        }
        return d


def brokerage_fee(sale_price: float) -> float:
    """仲介手数料の上限（税込）。

    文章の式 `(売却価格 × 3% + 6万円) × 1.1` は売却価格400万円超の速算式
    （`selling_costs.brokerage_fee_formula` の注記どおり）。400万円以下は
    宅建業法の報酬規程の段階料率（200万円以下5%、200万〜400万円4%+2万円）で出す。
    【解釈】400万円以下は文章に式がないため、法定の上限の段階料率を使った。
    """
    if sale_price <= 0:
        return 0.0
    if sale_price <= 2_000_000:
        base = sale_price * 0.05
    elif sale_price <= 4_000_000:
        base = sale_price * 0.04 + 20_000
    else:
        base = sale_price * 0.03 + 60_000
    return base * 1.1


def selling_costs(sale_price: float, method: str = "formula", rate_pct: float = 3.5,
                  other_costs_yen: float = 40_000) -> float:
    """売却諸費用 S = (売却価格 × 3% + 6万円) × 1.1 + 4万円（`selling_costs`）。

    method="rate" なら 売却価格 × rate_pct（文章の「率で概算するなら3.5%」）。
    """
    if method == "rate":
        return sale_price * rate_pct / 100
    if method != "formula":
        raise ValueError("method は 'formula' か 'rate'")
    return brokerage_fee(sale_price) + other_costs_yen


def _rent_base(inp: RentBuyInputs, k: int) -> float:
    """R_base(k) = R₁（k < n₂）/ R₂（n₂ ≤ k < n₃）/ R₃（n₃ ≤ k）。"""
    base = inp.rent_yen_per_month
    for mv in inp.moves:
        if k >= mv.after_years:
            base = mv.rent_yen_per_month
    return base


def _is_move_year(inp: RentBuyInputs, k: int) -> bool:
    return any(mv.after_years == k for mv in inp.moves)


def _is_renewal_year(inp: RentBuyInputs, k: int) -> bool:
    """更新料を払う年。

    文章: 「偶数年に R(k) の1カ月分（`rent.renewal_interval_months` = 24）」。
    既定（even_years）は字面どおり k が更新間隔の倍数の年。住み替えの年と重なっても払う
    （例: 20年目は住み替えの初期費用と更新料の両方が立つ）。
    【解釈】since_move は「契約開始（入居・住み替え）から2年ごと」と読む別解。感度確認用。
    """
    step = inp.renewal_interval_years
    if inp.renewal_schedule == "even_years":
        return k % step == 0
    start = 0
    for mv in inp.moves:
        if mv.after_years <= k:
            start = mv.after_years
    return k > start and (k - start) % step == 0


def simulate(inp: RentBuyInputs) -> RentBuyResult:
    """年ごとの賃貸累計・購入の純負担・損益分岐年を計算する。"""
    inp.validate()
    L = inp.loan_principal
    T = inp.loan_term_years
    i = inp.interest_rate_pct
    pi = inp.inflation_pct / 100
    alpha = inp.price_pass_through_pct / 100
    g_rent = inp.rent_growth_pct / 100
    g_mgmt = inp.management_fee_growth_pct / 100
    g_rep = inp.repair_reserve_growth_pct / 100
    d = inp.depreciation_pct / 100

    C = inp.price_yen * inp.purchase_cost_pct / 100
    M = _loan.monthly_payment(L, i, T) if L > 0 else 0.0

    rows = []
    rent_cum = 0.0
    cash_cum = C
    breakeven = None
    for k in range(1, inp.horizon_years + 1):
        # ---- 賃貸側 ----
        # 文章: R(k) = R_base(k) × (1 + g_rent)^k。1年目から1年分上がった賃料になる。
        # 【解釈】購入側の保有コストは (k-1) 乗なので両側で起点が1年ずれるが、文章どおり k 乗を既定にした
        # （rent_growth_from_year=0 で (k-1) 乗にできる）。
        rent_m = _rent_base(inp, k) * (1 + g_rent) ** (k - 1 + inp.rent_growth_from_year)
        rent_annual = 12 * rent_m
        renewal = inp.renewal_fee_months * rent_m if _is_renewal_year(inp, k) else 0.0
        # 【解釈】初期費用は「住み替えの年」だけ。1年目（今の賃貸に住み続ける出発点）には立てない。
        # 購入側の諸費用 C に対応する入居費用を賃貸側に立てるかは文章に書かれていないので、
        # initial_cost_at_start=True で別解を確かめられるようにした。
        move = _is_move_year(inp, k) or (k == 1 and inp.initial_cost_at_start)
        initial = inp.initial_cost_months * rent_m if move else 0.0
        danshin = 12 * inp.danshin_premium_yen_per_month if inp.include_danshin else 0.0
        subsidy = 12 * inp.rent_subsidy_yen_per_month
        rent_year = rent_annual + renewal + initial + danshin - subsidy
        rent_cum += rent_year

        # ---- 購入側 ----
        payment = 12 * M if k <= T else 0.0
        mgmt = 12 * inp.management_fee_yen_per_month * (1 + g_mgmt) ** (k - 1)
        repair = 12 * inp.repair_reserve_yen_per_month * (1 + g_rep) ** (k - 1)
        tax = inp.property_tax_yen_per_year * (1 + pi) ** (k - 1)
        balance = _loan.balance_after_months(L, i, T, 12 * k) if L > 0 else 0.0
        # 控除 = min(年末残高, 借入限度額) × 0.7%。制度額は名目で固定（物価で伸ばさない）。
        deduction = (min(balance, inp.deduction_limit_yen) * inp.deduction_rate_pct / 100
                     if k <= inp.deduction_years else 0.0)
        # 【解釈】「10年ごと」は 10・20・30 年目の年末に実施と読んだ（入居直後の0年目には立てない）。
        renovation = (inp.renovation_yen * (1 + pi) ** (k - 1)
                      if k % inp.renovation_interval_years == 0 else 0.0)
        buy_year = payment + mgmt + repair + tax - deduction + renovation
        cash_cum += buy_year

        value = inp.price_yen * (1 - d) ** k * (1 + pi * alpha) ** k
        sell = (selling_costs(value, inp.selling_cost_method, inp.selling_cost_rate_pct,
                              inp.selling_other_costs_yen)
                if inp.include_selling_costs else 0.0)
        equity = value - sell - balance
        buy_net = cash_cum - equity

        diff = buy_net - rent_cum
        if breakeven is None and diff < 0:
            breakeven = k
        rows.append(YearRow(
            year=k, rent_monthly=rent_m, rent_annual=rent_annual, renewal_fee=renewal,
            initial_cost=initial, danshin_premium=danshin, rent_subsidy=subsidy,
            rent_year_total=rent_year, rent_cum=rent_cum,
            loan_payment=payment, management_fee=mgmt, repair_reserve=repair, property_tax=tax,
            mortgage_deduction=deduction, renovation=renovation, buy_year_cash=buy_year,
            cashout_cum=cash_cum, property_value=value, loan_balance=balance, selling_cost=sell,
            equity_on_sale=equity, buy_net=buy_net, buy_minus_rent=diff,
        ))
    return RentBuyResult(inputs=inp, purchase_cost=C, monthly_payment=M, rows=tuple(rows),
                         breakeven_year=breakeven)


SCENARIOS = ("low", "medium", "high")
DEPRECIATION = ("small", "medium", "large")


def inputs_from_assumptions(a: Assumptions, scenario: Optional[str] = "medium",
                            depreciation: str = "medium", **overrides) -> RentBuyInputs:
    """assumptions.yaml の既定値から入力を作る。

    scenario: "low" / "medium" / "high" なら inflation.scenarios のセット（物価・金利・賃料・
      管理費・修繕積立金の上昇率）をまとめて入れる。None なら「入力」節の単独の既定値
      （物価1%・金利1%・賃料1%・管理費0.5%・修繕積立金3%）を使う。
    depreciation: "small" / "medium" / "large"（経年減価 1% / 1.5% / 4%）。
    overrides: RentBuyInputs のフィールドを個別に上書きする。
      inflation_pct だけを上書きし repair_reserve_growth_pct を指定しなかった場合は、
      文章の「g_rep = 段階増額2.0% + π」に合わせて修繕積立金の上昇率も連動させる。
    """
    if depreciation not in DEPRECIATION:
        raise ValueError(f"depreciation は {DEPRECIATION} のどれか")
    base = dict(
        rent_yen_per_month=160_000,  # 文章「入力」節の既定 R₁（P×3.8%÷12 の仮置きとは別）
        moves=tuple(RentMove(y, r) for y, r in a.example_moves),
        rent_growth_pct=a.rent_growth_pct,
        renewal_fee_months=a.renewal_fee_months,
        renewal_interval_years=_months_to_years(a.renewal_interval_months),
        initial_cost_months=a.initial_cost_months,
        danshin_premium_yen_per_month=a.danshin_premium_yen_per_month,
        loan_term_years=a.loan_term_years,
        interest_rate_pct=a.interest_rate_pct,
        purchase_cost_pct=a.purchase_cost_pct,
        management_fee_yen_per_month=a.management_fee_yen_per_month,
        management_fee_growth_pct=a.management_fee_growth_pct,
        repair_reserve_yen_per_month=a.repair_reserve_yen_per_month,
        repair_reserve_growth_pct=a.repair_reserve_growth_pct,
        property_tax_yen_per_year=a.property_tax_yen_per_year,
        renovation_yen=a.renovation_yen_per_10years,
        renovation_interval_years=10,
        deduction_rate_pct=a.deduction_rate_pct,
        deduction_limit_yen=a.deduction_limit_other_used_man_yen * MAN,
        deduction_years=a.deduction_default_years,
        inflation_pct=a.inflation_pct,
        price_pass_through_pct=a.price_pass_through_pct,
        depreciation_pct=a.depreciation_scenarios_pct[depreciation],
        selling_cost_rate_pct=a.selling_cost_rate_pct,
        selling_other_costs_yen=a.selling_other_costs_yen,
    )
    if scenario is not None:
        if scenario not in a.scenarios:
            raise ValueError(f"scenario は {SCENARIOS} のどれか")
        s = a.scenarios[scenario]
        base.update(
            inflation_pct=s.inflation_pct,
            interest_rate_pct=s.interest_rate_pct,
            rent_growth_pct=s.rent_growth_pct,
            management_fee_growth_pct=s.management_fee_growth_pct,
            repair_reserve_growth_pct=s.repair_reserve_growth_pct,
        )
    if "inflation_pct" in overrides and "repair_reserve_growth_pct" not in overrides:
        overrides["repair_reserve_growth_pct"] = a.repair_reserve_real_growth_pct + overrides["inflation_pct"]
    base.update(overrides)
    return RentBuyInputs(**base)


def _months_to_years(months: int) -> int:
    if months % 12:
        raise ValueError(f"更新間隔 {months} カ月は年単位で割り切れない（年次モデルでは扱えない）")
    return months // 12
