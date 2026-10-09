"""assumptions.yaml から、比較計算に必要な既定値だけを取り出す。

必要なキーが欠けている・型が違う・式の文字列が想定と変わった場合は
AssumptionsError で止める。黙って既定値に落とすと「文章の数字がコードで
保証されている」状態が崩れるため。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .yamlmini import YamlError, loads

__all__ = ["AssumptionsError", "Assumptions", "ScenarioSet", "load_assumptions", "DEFAULT_PATH"]

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "assumptions.yaml"

# selling_costs.brokerage_fee_formula の想定文字列。式を変えたらエンジン側も直す必要があるので照合する
_BROKERAGE_FORMULA_RE = re.compile(r"^\(売却価格\s*×\s*3%\s*\+\s*6万円\)\s*×\s*1\.1$")


class AssumptionsError(ValueError):
    """assumptions.yaml が読めない、または必要な値が欠けている・壊れている。"""


@dataclass(frozen=True)
class ScenarioSet:
    """inflation.scenarios の1セット（単位はすべて %/年）。"""

    name: str
    inflation_pct: float
    interest_rate_pct: float
    rent_growth_pct: float
    management_fee_growth_pct: float
    repair_reserve_growth_pct: float


@dataclass(frozen=True)
class Assumptions:
    updated_at: str
    interest_rate_pct: float
    inflation_pct: float
    price_pass_through_pct: float
    scenarios: Mapping[str, ScenarioSet]
    loan_term_years: int
    purchase_cost_pct: float
    selling_other_costs_yen: float
    selling_cost_rate_pct: float
    depreciation_scenarios_pct: Mapping[str, float]  # small / medium / large
    depreciation_condo_pct: float
    depreciation_house_pct: float
    management_fee_yen_per_month: float
    repair_reserve_yen_per_month: float
    management_fee_growth_pct: float
    repair_reserve_growth_pct: float
    repair_reserve_real_growth_pct: float
    property_tax_yen_per_year: float
    renovation_yen_per_10years: float
    renewal_fee_months: float
    renewal_interval_months: int
    initial_cost_months: float
    rent_growth_pct: float
    comparable_rent_ratio_pct: float
    example_moves: tuple  # ((after_years, rent_yen_per_month), ...)
    danshin_premium_yen_per_month: float
    deduction_rate_pct: float
    deduction_limit_other_used_man_yen: float
    deduction_default_man_yen_per_year: float
    deduction_default_years: int
    deduction_period_years: int
    holding_min_years: int
    holding_recommended_min_years: int
    wait_breakeven_drop_pct: float
    historical_max_drop_pct: float


def _get(tree: Mapping[str, Any], dotted: str):
    node: Any = tree
    for part in dotted.split("."):
        if not isinstance(node, Mapping) or part not in node:
            raise AssumptionsError(f"assumptions.yaml に必要なキーがない: {dotted}")
        node = node[part]
    return node


def _num(tree, dotted: str) -> float:
    val = _get(tree, dotted)
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise AssumptionsError(f"assumptions.yaml の {dotted} が数値ではない: {val!r}")
    return float(val)


def _int(tree, dotted: str) -> int:
    val = _get(tree, dotted)
    if isinstance(val, bool) or not isinstance(val, int):
        raise AssumptionsError(f"assumptions.yaml の {dotted} が整数ではない: {val!r}")
    return val


def parse_assumptions(tree: Mapping[str, Any]) -> Assumptions:
    if not isinstance(tree, Mapping):
        raise AssumptionsError("assumptions.yaml の最上位がマッピングではない")

    raw_sc = _get(tree, "inflation.scenarios")
    if not isinstance(raw_sc, Mapping):
        raise AssumptionsError("inflation.scenarios がマッピングではない")
    scenarios = {}
    for name in ("low", "medium", "high"):
        prefix = f"inflation.scenarios.{name}"
        scenarios[name] = ScenarioSet(
            name=name,
            inflation_pct=_num(tree, f"{prefix}.inflation_pct"),
            interest_rate_pct=_num(tree, f"{prefix}.interest_rate_pct"),
            rent_growth_pct=_num(tree, f"{prefix}.rent_growth_pct"),
            management_fee_growth_pct=_num(tree, f"{prefix}.management_fee_growth_pct"),
            repair_reserve_growth_pct=_num(tree, f"{prefix}.repair_reserve_growth_pct"),
        )

    formula = _get(tree, "selling_costs.brokerage_fee_formula")
    if not isinstance(formula, str) or not _BROKERAGE_FORMULA_RE.match(formula.strip()):
        raise AssumptionsError(
            "selling_costs.brokerage_fee_formula が想定の式 '(売却価格 × 3% + 6万円) × 1.1' と違う: "
            f"{formula!r}。エンジン（model.selling_costs）も合わせて直すこと"
        )

    moves_raw = _get(tree, "rent.example_moves")
    if not isinstance(moves_raw, list) or not moves_raw:
        raise AssumptionsError("rent.example_moves が空、またはリストではない")
    moves = []
    for i, mv in enumerate(moves_raw):
        if not isinstance(mv, Mapping):
            raise AssumptionsError(f"rent.example_moves[{i}] がマッピングではない")
        moves.append((_int({"m": mv}, "m.after_years"), _num({"m": mv}, "m.rent_yen_per_month")))

    return Assumptions(
        updated_at=str(_get(tree, "updated_at")),
        interest_rate_pct=_num(tree, "interest_rate.simulation_default_pct"),
        inflation_pct=_num(tree, "inflation.simulation_default_pct"),
        price_pass_through_pct=_num(tree, "inflation.price_pass_through_pct"),
        scenarios=scenarios,
        loan_term_years=_int(tree, "loan.default_term_years"),
        purchase_cost_pct=_num(tree, "purchase_costs.simulation_default_pct"),
        selling_other_costs_yen=_num(tree, "selling_costs.other_costs_yen"),
        selling_cost_rate_pct=_num(tree, "selling_costs.simulation_default_pct"),
        depreciation_scenarios_pct={
            "small": _num(tree, "depreciation.scenarios.small_pct_per_year"),
            "medium": _num(tree, "depreciation.scenarios.medium_pct_per_year"),
            "large": _num(tree, "depreciation.scenarios.large_pct_per_year"),
        },
        depreciation_condo_pct=_num(tree, "depreciation.condo_pct_per_year"),
        depreciation_house_pct=_num(tree, "depreciation.house_pct_per_year"),
        management_fee_yen_per_month=_num(tree, "ownership_costs.condo_management_fee_yen_per_month"),
        repair_reserve_yen_per_month=_num(tree, "ownership_costs.condo_repair_reserve_yen_per_month"),
        management_fee_growth_pct=_num(tree, "ownership_costs.management_fee_growth_pct_per_year"),
        repair_reserve_growth_pct=_num(tree, "ownership_costs.repair_reserve_growth_pct_per_year"),
        repair_reserve_real_growth_pct=_num(tree, "ownership_costs.repair_reserve_real_growth_pct_per_year"),
        property_tax_yen_per_year=_num(tree, "ownership_costs.property_tax_yen_per_year"),
        renovation_yen_per_10years=_num(tree, "ownership_costs.renovation_yen_per_10years"),
        renewal_fee_months=_num(tree, "rent.renewal_fee_months"),
        renewal_interval_months=_int(tree, "rent.renewal_interval_months"),
        initial_cost_months=_num(tree, "rent.initial_cost_months"),
        rent_growth_pct=_num(tree, "rent.growth_pct_per_year"),
        comparable_rent_ratio_pct=_num(tree, "rent.comparable_rent_ratio_pct_of_price_per_year"),
        example_moves=tuple(moves),
        danshin_premium_yen_per_month=_num(tree, "danshin.equivalent_premium_yen_per_month"),
        deduction_rate_pct=_num(tree, "tax_and_programs.mortgage_deduction.rate_pct"),
        deduction_limit_other_used_man_yen=_num(tree, "tax_and_programs.mortgage_deduction.limit_used_man_yen.other"),
        deduction_default_man_yen_per_year=_num(tree, "tax_and_programs.mortgage_deduction.simulation_default_man_yen_per_year"),
        deduction_default_years=_int(tree, "tax_and_programs.mortgage_deduction.simulation_default_years"),
        deduction_period_years=_int(tree, "tax_and_programs.mortgage_deduction.period_years"),
        holding_min_years=_int(tree, "holding.minimum_years_to_buy"),
        holding_recommended_min_years=_int(tree, "holding.recommended_min_years"),
        wait_breakeven_drop_pct=_num(tree, "holding.wait_breakeven_drop_pct_per_year"),
        historical_max_drop_pct=_num(tree, "holding.historical_max_drop_pct"),
    )


def load_assumptions(path: str | Path | None = None) -> Assumptions:
    """assumptions.yaml を読み、計算に使う値を返す。壊れていれば AssumptionsError。"""
    p = Path(path) if path is not None else DEFAULT_PATH
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise AssumptionsError(f"assumptions.yaml を開けない: {p}: {exc}") from exc
    try:
        tree = loads(text)
    except YamlError as exc:
        raise AssumptionsError(f"assumptions.yaml を解釈できない: {p}: {exc}") from exc
    return parse_assumptions(tree)
