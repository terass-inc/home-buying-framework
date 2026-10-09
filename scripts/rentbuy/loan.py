"""元利均等返済の計算（月次）。

calc/rent-vs-buy.md は `M = PMT(i/12, 12T, L)` と書いており月次の元利均等が前提。
残債の式 `B_N = L − Σ_{k=1..N} PPMT(i, k, T, L)` は年単位の記法だが、
返済は月次なので、ここでは「12N カ月返済後の残高」を閉じた式で出す
（月次 PPMT を 12N カ月分足したものと数学的に同じ）。
"""
from __future__ import annotations

__all__ = ["monthly_payment", "balance_after_months", "interest_paid_months"]


def _check(principal: float, annual_rate_pct: float, term_years: int) -> None:
    if principal < 0:
        raise ValueError("借入額は0以上")
    if annual_rate_pct < 0:
        raise ValueError("金利は0以上")
    if term_years <= 0:
        raise ValueError("借入期間は1年以上")


def monthly_payment(principal: float, annual_rate_pct: float, term_years: int) -> float:
    """毎月の返済額 M = PMT(i/12, 12T, L)。"""
    _check(principal, annual_rate_pct, term_years)
    n = 12 * term_years
    r = annual_rate_pct / 100 / 12
    if r == 0:
        return principal / n
    return principal * r / (1 - (1 + r) ** -n)


def balance_after_months(principal: float, annual_rate_pct: float, term_years: int, months: int) -> float:
    """months カ月返済した直後の残債。完済後は0。"""
    _check(principal, annual_rate_pct, term_years)
    n = 12 * term_years
    if months <= 0:
        return principal
    if months >= n:
        return 0.0
    r = annual_rate_pct / 100 / 12
    if r == 0:
        return principal * (1 - months / n)
    m = monthly_payment(principal, annual_rate_pct, term_years)
    growth = (1 + r) ** months
    return max(0.0, principal * growth - m * (growth - 1) / r)


def interest_paid_months(principal: float, annual_rate_pct: float, term_years: int, months: int) -> float:
    """months カ月までに払った利息の合計（= 支払総額 − 元本の減少分）。"""
    months = max(0, min(months, 12 * term_years))
    paid = monthly_payment(principal, annual_rate_pct, term_years) * months
    return paid - (principal - balance_after_months(principal, annual_rate_pct, term_years, months))
