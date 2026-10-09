"""rentbuy.advice（住宅相談の計算）が文章の数字を再現するかのテスト。

実行: python3 -m unittest discover -s scripts/tests
各テストの docstring に出典（ファイルと節）を書く。文章の数字は「約」「万円単位」なので、
万円単位の丸め（±0.5万円）か、文章の桁に合わせた許容差で比べる。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # scripts/ を import パスに入れる

from rentbuy import (  # noqa: E402
    AdviceResult,
    balance_sheet,
    borrowing_budget,
    load_assumptions,
    loan_term_comparison,
    selling_costs_breakdown,
    wait_cost,
)

MAN = 10_000
L = 50_000_000
A = load_assumptions()


def man(yen: float) -> int:
    """万円単位に四捨五入。"""
    return round(yen / MAN)


class TestAssumptionsForAdvice(unittest.TestCase):
    def test_new_keys_loaded(self):
        """assumptions.yaml: 審査金利3%・返済比率35%・年収倍率「7〜8倍（最大9〜10倍）」。"""
        self.assertEqual(A.screening_rate_pct, 3.0)
        self.assertEqual(A.repayment_ratio_limit_pct, 35)
        self.assertEqual(A.income_multiple_range, (7.0, 8.0, 9.0, 10.0))
        self.assertEqual(A.earnest_money_pct, 5.0)


class TestLoanTermComparison(unittest.TestCase):
    def setUp(self):
        self.r = loan_term_comparison(L, sale_year=15, investment_yield_pct=1.25, annual_income_yen=6_500_000,
                                      assumptions=A)

    def test_monthly_and_total_interest(self):
        """principles/02 判断手順2: 5000万円・1%で35年は月14.1万円・総利息約928万円、20年は月23.0万円・約519万円、差409万円。"""
        r = self.r
        self.assertAlmostEqual(r.value("term_35y.monthly_payment_yen") / MAN, 14.1, places=1)
        self.assertAlmostEqual(r.value("term_20y.monthly_payment_yen") / MAN, 23.0, places=1)
        self.assertEqual(man(r.value("term_35y.total_interest_yen")), 928)
        self.assertEqual(man(r.value("term_20y.total_interest_yen")), 519)
        self.assertEqual(man(r.value("total_interest_gap_to_maturity_yen")), 409)

    def test_sale_year_15_balances_and_net(self):
        """principles/02 判断手順2・cases/01 要点3: 15年目の残債 35年3,069万円・20年1,345万円、差1,724万円、追加支払1,598万円（月8.88万円×180カ月）、正味125万円。"""
        r = self.r
        self.assertEqual(man(r.value("sale.term_35y.balance_yen")), 3069)
        self.assertEqual(man(r.value("sale.term_20y.balance_yen")), 1345)
        self.assertEqual(man(r.value("sale.balance_gap_yen")), 1724)
        self.assertAlmostEqual(r.value("monthly_payment_gap_yen") / MAN, 8.88, places=2)
        self.assertEqual(man(r.value("sale.extra_paid_by_short_yen")), 1598)
        self.assertEqual(man(r.value("sale.net_advantage_short_yen")), 125)
        self.assertEqual(man(r.value("use_of_gap.cash.short_advantage_yen")), 125)

    def test_interest_gap_shrinks_with_earlier_sale(self):
        """principles/02 判断手順2: 10年で売るなら支払利息の差は約55万円、5年なら約13万円。"""
        for year, expected in ((10, 55), (5, 13)):
            r = loan_term_comparison(L, sale_year=year, assumptions=A)
            self.assertEqual(man(r.value("sale.net_advantage_short_yen")), expected)

    def test_breakeven_yield_equals_loan_rate(self):
        """principles/02 判断手順4・loan.term_shortening_breakeven: 分岐は必ずローン金利に一致（0.5%なら0.50%、2.5%なら2.50%）。税引前では約1.25%。"""
        self.assertAlmostEqual(self.r.value("use_of_gap.breakeven_pre_tax_yield_pct"), 1.25)
        # 税引前1.25% = 税引後1.0% = ローン金利 → 純資産の差はゼロ
        self.assertAlmostEqual(self.r.value("use_of_gap.invest.long_minus_short_net_worth_yen"), 0, delta=1)
        for rate in (0.5, 1.0, 2.5):
            for sale in (5, 15, 30):  # 30年は20年側が完済した後
                r = loan_term_comparison(L, annual_rate_pct=rate, sale_year=sale,
                                         investment_yield_pct=rate / 0.8, assumptions=A)
                self.assertEqual(r.value("use_of_gap.breakeven_after_tax_yield_pct"), rate)
                self.assertAlmostEqual(r.value("use_of_gap.invest.long_minus_short_net_worth_yen"), 0, delta=2)
                above = loan_term_comparison(L, annual_rate_pct=rate, sale_year=sale,
                                             investment_yield_pct=rate / 0.8 + 0.5, assumptions=A)
                below = loan_term_comparison(L, annual_rate_pct=rate, sale_year=sale,
                                             investment_yield_pct=max(0.0, rate / 0.8 - 0.5), assumptions=A)
                self.assertGreater(above.value("use_of_gap.invest.long_minus_short_net_worth_yen"), 0)
                self.assertLess(below.value("use_of_gap.invest.long_minus_short_net_worth_yen"), 0)

    def test_cash_only_equals_zero_yield(self):
        """principles/02 判断手順4: 現金で置くだけなら20年が125万円有利（運用利回り0%と同じ）。"""
        r = loan_term_comparison(L, sale_year=15, investment_yield_pct=0.0, assumptions=A)
        self.assertEqual(man(-r.value("use_of_gap.invest.long_minus_short_net_worth_yen")), 125)

    def test_screening_ratio(self):
        """principles/02 判断手順5: 審査金利3%なら35年で年231万円（年収650万円の35.5%）、20年で年333万円（同51%）。"""
        r = self.r
        self.assertEqual(man(r.value("screening.term_35y.annual_payment_yen")), 231)
        self.assertAlmostEqual(r.value("screening.term_35y.repayment_ratio_pct"), 35.5, delta=0.05)
        self.assertEqual(man(r.value("screening.term_20y.annual_payment_yen")), 333)
        self.assertEqual(round(r.value("screening.term_20y.repayment_ratio_pct")), 51)

    def test_notes_and_inputs(self):
        """principles/02・cases/01: 「短くはできるが長くはできない」、繰上返済の手数料の一言、売却時期の確認。"""
        joined = "\n".join(self.r.notes)
        self.assertIn("後から短くできるが、長くはできない", joined)
        self.assertIn("繰上返済の条件は銀行で異なる", joined)
        r = loan_term_comparison(L, assumptions=A)
        self.assertIn("sale_year", [x["name"] for x in r.inputs_needed])
        self.assertIn("annual_income_yen", [x["name"] for x in r.inputs_needed])

    def test_validation_and_json(self):
        with self.assertRaises(ValueError):
            loan_term_comparison(L, short_term_years=35, assumptions=A)
        with self.assertRaises(ValueError):
            loan_term_comparison(L, sale_year=40, assumptions=A)
        d = self.r.to_dict()
        json.dumps(d, ensure_ascii=False)
        self.assertIn("disclaimer", d)
        self.assertEqual(set(d["figures"]["term_35y.monthly_payment_yen"]), {"value", "unit", "meaning"})


class TestWaitCost(unittest.TestCase):
    def test_breakeven_3_6(self):
        """principles/01 判断手順6・04 判断手順4・99 項目5: 家賃15万円/月（年180万円）・5000万円なら3.6%/年。yaml holding.wait_breakeven_drop_pct_per_year と一致。"""
        r = wait_cost(L, 150_000, assumptions=A)
        self.assertEqual(man(r.value("annual_rent_yen")), 180)
        self.assertAlmostEqual(r.value("breakeven_drop_pct_per_year"), 3.6)
        self.assertAlmostEqual(r.value("breakeven_drop_pct_per_year"), A.wait_breakeven_drop_pct)

    def test_thresholds(self):
        """principles/01・04・99: 得をするには5%以上、過去最大の下落6.3%（2008年5月→2009年4月）、データは2023年4月まで。"""
        r = wait_cost(L, 150_000, assumptions=A)
        self.assertEqual(r.value("practical_drop_pct_per_year"), 5.0)
        self.assertEqual(r.value("historical_max_drop_pct"), 6.3)
        self.assertFalse(r.value("breakeven_exceeds_practical"))
        self.assertFalse(r.value("breakeven_exceeds_historical_max"))
        text = "\n".join(r.assumptions) + r.figures["historical_max_drop_pct"].meaning
        self.assertIn("2008年5月→2009年4月", text)
        self.assertIn("2023年4月", text)
        self.assertIn("首都圏中古マンション", text)

    def test_multi_year(self):
        """principles/01 判断手順6 の式を待つ年数に広げた値: 3年待てば家賃540万円＝物件価格の10.8%。"""
        r = wait_cost(L, 150_000, wait_years=3, assumptions=A)
        self.assertEqual(man(r.value("rent_paid_while_waiting_yen")), 540)
        self.assertAlmostEqual(r.value("breakeven_cumulative_drop_pct"), 10.8)
        self.assertEqual(r.value("breakeven_drop_pct_per_year"), 3.6)  # 年率は待つ年数によらない


class TestBorrowingBudget(unittest.TestCase):
    def test_lendable_1100(self):
        """principles/03 判断手順2: 世帯年収1100万円なら年385万円、審査金利3%・35年で借入上限は約8300万円（約7.6倍）。"""
        r = borrowing_budget(11_000_000, assumptions=A)
        self.assertEqual(man(r.value("lendable.allowable_annual_payment_yen")), 385)
        self.assertAlmostEqual(r.value("lendable.max_principal_yen") / 1e7, 8.3, delta=0.05)
        self.assertAlmostEqual(r.value("lendable.income_multiple"), 7.6, delta=0.05)

    def test_income_multiple_range(self):
        """principles/03「よくある誤解」: 世帯年収1100万円なら7700万〜8800万円（年収の7〜8倍）。"""
        r = borrowing_budget(11_000_000, assumptions=A)
        self.assertEqual(man(r.value("lendable.income_multiple_typical_min_yen")), 7700)
        self.assertEqual(man(r.value("lendable.income_multiple_typical_max_yen")), 8800)

    def test_should_borrow_is_not_invented(self):
        """principles/03 判断手順3: 借りるべき額の式は文章にない → 入力が揃わなければ数値を出さず、確認が必要な値として返す。"""
        r = borrowing_budget(11_000_000, assumptions=A)
        self.assertFalse(any(k.startswith("should_borrow.") for k in r.figures))
        names = [x["name"] for x in r.inputs_needed]
        self.assertIn("affordable_monthly_payment_yen", names)
        self.assertIn("stress_rate_pct", names)
        r2 = borrowing_budget(11_000_000, affordable_monthly_payment_yen=200_000, stress_rate_pct=2.5, assumptions=A)
        self.assertIn("should_borrow.principal_from_affordable_payment_yen", r2.figures)
        self.assertNotIn("stress_rate_pct", [x["name"] for x in r2.inputs_needed])

    def test_other_debt_reduces_lendable(self):
        """principles/03「確認すること」: 他の借入れは返済比率に合算される。"""
        base = borrowing_budget(11_000_000, assumptions=A).value("lendable.max_principal_yen")
        less = borrowing_budget(11_000_000, other_debt_annual_payment_yen=600_000, assumptions=A)
        self.assertLess(less.value("lendable.max_principal_yen"), base)
        self.assertEqual(man(less.value("lendable.allowable_annual_payment_yen")), 325)

    def test_cash_upfront(self):
        """principles/03 判断手順5: 諸費用（新築マンション4〜5%、中古・戸建て7〜8%）と手付金（5〜10%）は現金で用意する。"""
        r = borrowing_budget(11_000_000, property_price_yen=L, property_type="new_condo", assumptions=A)
        self.assertEqual(man(r.value("cash.purchase_costs_yen")), 225)
        self.assertEqual(man(r.value("cash.earnest_money_yen")), 250)
        r = borrowing_budget(11_000_000, property_price_yen=L, property_type="used_or_house", assumptions=A)
        self.assertEqual(man(r.value("cash.purchase_costs_yen")), 375)

    def test_low_income_flags_ratio(self):
        """principles/02 判断手順5: 返済比率35%は年収400万円以上の基準。未満は銀行に確認。"""
        r = borrowing_budget(3_500_000, assumptions=A)
        self.assertIn("repayment_ratio_limit_pct", [x["name"] for x in r.inputs_needed])


class TestBalanceSheet(unittest.TestCase):
    def test_defaults_after_purchase(self):
        """principles/01 判断手順3・calc/balance-sheet.md: 5000万円をフルローン・諸費用350万円で純資産は600万円から250万円に。"""
        r = balance_sheet(assumptions=A)
        self.assertEqual(man(r.value("now.net_worth_yen")), 600)
        self.assertEqual(man(-r.value("after_purchase.net_worth_change_yen")), 350)
        self.assertEqual(man(r.value("after_purchase.net_worth_yen")), 250)

    def test_10_years_50y_loan(self):
        """calc/balance-sheet.md「読み方」: 標準ケース（5,000万円・1%・年1.5%下落・50年）で10年後の住宅由来の純資産は約100万円のプラス。"""
        r = balance_sheet(assumptions=A)
        eq = r.value("year_10.housing_equity_yen")
        self.assertGreater(eq, 0)
        self.assertAlmostEqual(eq / MAN, 100, delta=15)  # 計算値は約109万円
        self.assertIn("year_20.net_worth_yen", r.figures)  # 既定の指定年20年

    def test_exit_first_step4_example(self):
        """principles/01 判断手順4: 5000万円・1%・35年で10年後の残債3745万円、年1%下落で物件4522万円、差額777万円。"""
        r = balance_sheet(term_years=35, depreciation_pct_per_year=1.0, assumptions=A)
        self.assertEqual(man(r.value("year_10.loan_balance_yen")), 3745)
        self.assertEqual(man(r.value("year_10.house_yen")), 4522)
        self.assertEqual(man(r.value("year_10.housing_equity_yen")), 777)

    def test_negative_cash_warns(self):
        r = balance_sheet(savings_yen=1_000_000, assumptions=A)
        self.assertLess(r.value("after_purchase.cash_yen"), 0)
        self.assertTrue(any("マイナス" in n for n in r.notes))


class TestSellingCosts(unittest.TestCase):
    def test_rates_by_price(self):
        """assumptions.yaml selling_costs.rate_by_price_note: 3,000万円で3.65%、5,000万円で3.51%、1.0億円で3.41%。"""
        for price, pct in ((30_000_000, 3.65), (50_000_000, 3.51), (100_000_000, 3.41)):
            r = selling_costs_breakdown(price, assumptions=A)
            self.assertAlmostEqual(r.value("total_rate_pct"), pct, delta=0.005)

    def test_breakdown_5000(self):
        """selling_costs: (5000万円 × 3% + 6万円) × 1.1 = 171.6万円、+4万円で175.6万円。"""
        r = selling_costs_breakdown(L, assumptions=A)
        self.assertEqual(r.value("brokerage_fee_yen"), 1_716_000)
        self.assertEqual(r.value("other_costs_yen"), 40_000)
        self.assertEqual(r.value("total_yen"), 1_756_000)
        self.assertEqual(r.value("approx_total_yen"), 1_750_000)
        self.assertIsInstance(r, AdviceResult)


if __name__ == "__main__":
    unittest.main()
