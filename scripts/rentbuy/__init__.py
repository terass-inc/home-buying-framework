"""賃貸 vs 購入の比較計算エンジン（calc/rent-vs-buy.md の実装）。標準ライブラリのみ。"""
from .assumptions import Assumptions, AssumptionsError, ScenarioSet, load_assumptions
from .loan import balance_after_months, interest_paid_months, monthly_payment
from .model import (
    RentBuyInputs,
    RentBuyResult,
    RentMove,
    YearRow,
    brokerage_fee,
    inputs_from_assumptions,
    selling_costs,
    simulate,
)

__all__ = [
    "Assumptions", "AssumptionsError", "ScenarioSet", "load_assumptions",
    "balance_after_months", "interest_paid_months", "monthly_payment",
    "RentBuyInputs", "RentBuyResult", "RentMove", "YearRow",
    "brokerage_fee", "inputs_from_assumptions", "selling_costs", "simulate",
]
