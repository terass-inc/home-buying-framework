"""文章（calc/・principles/・cases/・assumptions.yaml）に書かれた数値を、計算エンジンで再現するテスト。

実行: python3 -m unittest discover -s scripts/tests
各テストの docstring に出典（ファイルと節）を書く。文章の値をコードで再現できなかったものは
@unittest.expectedFailure を付け、差と原因の仮説をコメントに残す（実装を文章に合わせてゆがめない）。
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # scripts/ を import パスに入れる

from rentbuy import (  # noqa: E402
    AssumptionsError,
    balance_after_months,
    inputs_from_assumptions,
    interest_paid_months,
    load_assumptions,
    monthly_payment,
    selling_costs,
    simulate,
)
from rentbuy import yamlmini  # noqa: E402
from rentbuy.assumptions import DEFAULT_PATH  # noqa: E402
from rentbuy.cli import main as cli_main  # noqa: E402

MAN = 10_000
L = 50_000_000  # 5,000万円
A = load_assumptions()


def breakeven(scenario="low", depreciation="medium", **ov):
    return simulate(inputs_from_assumptions(A, scenario=scenario, depreciation=depreciation, **ov)).breakeven_year


def man(x):
    return x / MAN


# 検算用の標準ケース（calc/rent-vs-buy.md「検算用の標準ケース」）。
# 低セット相当: 物価0%・金利1.0%・賃料上昇0%・管理費据え置き、修繕積立金は初期値の 3%/年。
KENSAN = dict(scenario="low", repair_reserve_growth_pct=3.0)


class TestAssumptionsLoader(unittest.TestCase):
    def test_reads_required_values(self):
        self.assertEqual(A.interest_rate_pct, 1.0)
        self.assertEqual(A.scenarios["high"].interest_rate_pct, 2.5)
        self.assertEqual(A.scenarios["low"].repair_reserve_growth_pct, 2.0)
        self.assertEqual(A.example_moves, ((5, 180000.0), (20, 140000.0)))
        self.assertEqual(A.selling_other_costs_yen, 40000)
        self.assertEqual(A.deduction_default_man_yen_per_year, 14)

    def test_scenario_repair_growth_equals_real_plus_inflation(self):
        """calc「金利・物価…セットで置く」: 修繕積立金の上昇 = 段階増額2.0% + 物価上昇率。"""
        for s in A.scenarios.values():
            self.assertAlmostEqual(s.repair_reserve_growth_pct, A.repair_reserve_real_growth_pct + s.inflation_pct)

    def test_management_fee_growth_is_half_of_inflation(self):
        """calc「金利・物価…」: 管理費は物価上昇率の半分で置く。"""
        for s in A.scenarios.values():
            self.assertAlmostEqual(s.management_fee_growth_pct, s.inflation_pct / 2)

    def test_deduction_default_is_limit_times_rate(self):
        """calc「入力」: 控除14万円 = 限度2,000万円 × 0.7%。"""
        self.assertAlmostEqual(A.deduction_limit_other_used_man_yen * A.deduction_rate_pct / 100,
                               A.deduction_default_man_yen_per_year)

    def _broken(self, text):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.yaml"
            p.write_text(text, encoding="utf-8")
            with self.assertRaises(AssumptionsError):
                load_assumptions(p)

    def test_missing_key_fails(self):
        text = DEFAULT_PATH.read_text(encoding="utf-8").replace("  initial_cost_months: 4.5", "  initial_cost_mo: 4.5")
        self._broken(text)

    def test_wrong_type_fails(self):
        text = DEFAULT_PATH.read_text(encoding="utf-8").replace(
            "property_tax_yen_per_year: 120000", 'property_tax_yen_per_year: "12万円"')
        self._broken(text)

    def test_changed_selling_formula_fails(self):
        text = DEFAULT_PATH.read_text(encoding="utf-8").replace("(売却価格 × 3% + 6万円) × 1.1", "(売却価格 × 4%) × 1.1")
        self._broken(text)

    def test_bad_indent_and_missing_file_fail(self):
        self._broken("a:\n  b: 1\n   c: 2\n")
        with self.assertRaises(AssumptionsError):
            load_assumptions("/nonexistent/assumptions.yaml")

    def test_parser_rejects_unsupported_syntax(self):
        for bad in ("a: |\n  x\n", "a: [1, 2]\n", "a: 1\na: 2\n", "a: \"x\n", "a: {b: {c: 1}}\n"):
            with self.assertRaises(yamlmini.YamlError, msg=bad):
                yamlmini.loads(bad)


class TestLoanMath(unittest.TestCase):
    """元利均等（月次）の数値。"""

    def test_monthly_payment_35_and_20_years(self):
        """principles/02 判断手順2: 5,000万円・1%で35年は月14.1万円、20年は月23.0万円。"""
        self.assertEqual(round(man(monthly_payment(L, 1.0, 35)), 1), 14.1)
        self.assertEqual(round(man(monthly_payment(L, 1.0, 20)), 1), 23.0)

    def test_total_interest(self):
        """principles/02 判断手順2: 総利息 35年約928万円・20年約519万円、差409万円。"""
        i35 = man(interest_paid_months(L, 1.0, 35, 420))
        i20 = man(interest_paid_months(L, 1.0, 20, 240))
        self.assertEqual(round(i35), 928)
        self.assertEqual(round(i20), 519)
        self.assertEqual(round(i35 - i20), 409)

    def test_interest_gap_when_sold_early(self):
        """principles/02 判断手順2: 10年で売るなら利息差約55万円、5年なら約13万円。"""
        for years, expected in ((10, 55), (5, 13)):
            gap = interest_paid_months(L, 1.0, 35, 12 * years) - interest_paid_months(L, 1.0, 20, 12 * years)
            self.assertEqual(round(man(gap)), expected)

    def test_case01_balances_at_year_15(self):
        """cases/01・principles/02: 15年目売却で残債は20年1,345万円・35年3,069万円。"""
        self.assertEqual(round(man(balance_after_months(L, 1.0, 20, 180))), 1345)
        self.assertEqual(round(man(balance_after_months(L, 1.0, 35, 180))), 3069)

    def test_case01_net_difference(self):
        """cases/01・principles/02・loan.term_shortening_breakeven:
        残債差1,724万円、追加支払1,598万円（月8.88万円×180カ月）、正味125万円。"""
        extra_m = monthly_payment(L, 1.0, 20) - monthly_payment(L, 1.0, 35)
        self.assertEqual(round(man(extra_m), 2), 8.88)
        bal_gap = balance_after_months(L, 1.0, 35, 180) - balance_after_months(L, 1.0, 20, 180)
        self.assertEqual(round(man(bal_gap)), 1724)
        self.assertEqual(round(man(extra_m * 180)), 1598)
        self.assertEqual(round(man(bal_gap - extra_m * 180)), 125)

    def test_balance_after_10_years_and_hidden_equity(self):
        """principles/01 判断手順4: 35年・1%で10年後残債3,745万円、年1%下落で4,522万円、差777万円。"""
        b = man(balance_after_months(L, 1.0, 35, 120))
        v = man(L * 0.99 ** 10)
        self.assertEqual(round(b), 3745)
        self.assertEqual(round(v), 4522)
        self.assertEqual(round(v) - round(b), 777)

    def test_screening_rate_annual_payment(self):
        """principles/02 判断手順5: 審査金利3%で35年は年231万円（年収650万円の35.5%）、20年は年333万円（同51%）。"""
        a35 = 12 * monthly_payment(L, 3.0, 35)
        a20 = 12 * monthly_payment(L, 3.0, 20)
        self.assertEqual(round(man(a35)), 231)
        self.assertEqual(round(man(a20)), 333)
        self.assertEqual(round(a35 / 6_500_000 * 100, 1), 35.5)
        self.assertEqual(round(a20 / 6_500_000 * 100), 51)

    def test_variable_rate_stress(self):
        """principles/02 原則: 35年で金利0.5%なら月13.0万円、3%なら月19.2万円（約1.5倍）。"""
        m05, m3 = monthly_payment(L, 0.5, 35), monthly_payment(L, 3.0, 35)
        self.assertEqual(round(man(m05), 1), 13.0)
        self.assertEqual(round(man(m3), 1), 19.2)
        self.assertAlmostEqual(m3 / m05, 1.5, delta=0.05)

    def test_case09_payment_at_2_5_percent(self):
        """cases/09（NG例の数字）: 金利1.0%で月14.1万円、2.5%で17.9万円、月3.8万円増、総返済の差1,500万円以上。"""
        m1, m25 = monthly_payment(L, 1.0, 35), monthly_payment(L, 2.5, 35)
        self.assertEqual(round(man(m25), 1), 17.9)
        self.assertEqual(round(man(m25 - m1), 1), 3.8)
        self.assertGreaterEqual(man(420 * (m25 - m1)), 1500)

    def test_cancer_danshin_interest_equivalent(self):
        """danshin.special_value_example: 金利0.5%の総利息451万円、0.7%との差188万円。"""
        i05 = interest_paid_months(L, 0.5, 35, 420)
        i07 = interest_paid_months(L, 0.7, 35, 420)
        self.assertEqual(round(man(i05)), 451)
        self.assertEqual(round(man(i07 - i05)), 188)

    def test_danshin_early_vs_late_balance(self):
        """danshin.early_vs_late: 5,000万円・35年で10年後約3,650万円、30年後約750万円。
        金利の記載がない。0.5%（special_value_example と同じ）で 3,660万円・769万円となり、
        50万円単位に丸めると一致する（1.0%なら 3,745万円・826万円で合わない）。"""
        b10 = man(balance_after_months(L, 0.5, 35, 120))
        b30 = man(balance_after_months(L, 0.5, 35, 360))
        self.assertEqual(round(b10 / 10) * 10, 3660)
        self.assertEqual(round(b30 / 10) * 10, 770)

    def test_rate_gap_0_2_percent_over_35_years(self):
        """principles/02 原則: 「5000万円で金利0.2%の差は35年で約200万円（金利1.0%と1.2%の比較）」。
        以前の文章は「約250万円」だったが、35年の総利息の差は1.0%基準で198万円のため、このテストで見つけて修正した。"""
        gap = interest_paid_months(L, 1.2, 35, 420) - interest_paid_months(L, 1.0, 35, 420)
        self.assertAlmostEqual(man(gap), 200, delta=5)   # 文章は 2026-10 に「約250万円」から「約200万円」に修正

    def test_origination_fee_breakeven_about_7_years(self):
        """principles/02 原則: 定率2.2%（110万円）と定額33万円の差77万円を金利差0.25%で埋めると約7年。
        各年の利息差の累計（1.25% − 1.0%）が77万円を超えるのは7年目。"""
        fee_gap = L * 0.022 - 330_000
        self.assertEqual(round(man(fee_gap)), 77)
        year = next(y for y in range(1, 36)
                    if interest_paid_months(L, 1.25, 35, 12 * y) - interest_paid_months(L, 1.0, 35, 12 * y) >= fee_gap)
        self.assertEqual(year, 7)

    def test_deduction_gap_35_vs_20_years(self):
        """loan.term_shortening_deduction_note: 新築認定住宅（限度4,500万円）で10年間に約39万円35年のほうが多い。
        中古「その他の住宅」（限度2,000万円）では両者とも上限に張り付き差は出ない。"""
        def total(term, limit):
            return sum(min(balance_after_months(L, 1.0, term, 12 * k), limit) * 0.007 for k in range(1, 11))
        self.assertEqual(round(man(total(35, 45_000_000) - total(20, 45_000_000))), 39)
        self.assertAlmostEqual(total(35, 20_000_000), total(20, 20_000_000))
        self.assertAlmostEqual(man(total(35, 20_000_000)), 140)


class TestSellingCosts(unittest.TestCase):
    def test_formula(self):
        """selling_costs: (売却価格 × 3% + 6万円) × 1.1 + 4万円。5,000万円で 175.6万円。"""
        self.assertAlmostEqual(selling_costs(50_000_000), (50_000_000 * 0.03 + 60_000) * 1.1 + 40_000)
        self.assertAlmostEqual(selling_costs(50_000_000, method="rate"), 1_750_000)

    def test_rate_by_price(self):
        """selling_costs.rate_by_price_note: 3,000万円で3.65%、5,000万円で3.51%、1.0億円で3.41%。"""
        for price, pct in ((30_000_000, 3.65), (50_000_000, 3.51), (100_000_000, 3.41)):
            self.assertEqual(round(selling_costs(price) / price * 100, 2), pct)

    def test_brokerage_tiers_continuous_below_4m(self):
        """400万円以下は法定の段階料率（文章に式がないため実装上の解釈）。境界で連続すること。"""
        self.assertAlmostEqual(selling_costs(4_000_000), (4_000_000 * 0.04 + 20_000) * 1.1 + 40_000)
        self.assertAlmostEqual(selling_costs(4_000_000), (4_000_000 * 0.03 + 60_000) * 1.1 + 40_000)
        self.assertAlmostEqual(selling_costs(2_000_000), 2_000_000 * 0.05 * 1.1 + 40_000)


class TestStandardCaseBreakeven(unittest.TestCase):
    """calc/rent-vs-buy.md「検算用の標準ケース」と「出力の形式」の分岐年。"""

    def test_low_with_selling_costs_only_is_year_8(self):
        """参考値: 低セット＋売却諸費用のみで分岐8年目。修繕積立金 3%（検算ケース）でも 2%（低セット）でも同じ。"""
        self.assertEqual(breakeven(**KENSAN, include_danshin=False), 8)
        self.assertEqual(breakeven("low", include_danshin=False), 8)

    def test_low_with_selling_costs_and_danshin_is_year_7(self):
        """参考値: 低セット＋売却諸費用＋団信相当で7年目（「出力の形式」の低セット7年目も同じ）。"""
        self.assertEqual(breakeven(**KENSAN), 7)
        self.assertEqual(breakeven("low"), 7)

    def test_medium_is_year_6(self):
        """参考値: 中セットで6年目。"""
        self.assertEqual(breakeven("medium"), 6)

    def test_high_is_year_5(self):
        """「出力の形式」: 高セットで5年目。"""
        self.assertEqual(breakeven("high"), 5)

    def test_kensan_base_without_extras(self):
        """検算ケースそのもの（売却諸費用なし・団信相当なし）の分岐。文章に数値はないが、
        売却諸費用を引かないと購入が有利に出る（原則1）ことの確認として 8年目より早いことを見る。"""
        base = breakeven(**KENSAN, include_selling_costs=False, include_danshin=False)
        self.assertEqual(base, 6)
        self.assertLess(base, 8)

    def test_holding_recommended_min_years_matches_low_set(self):
        """holding.recommended_min_years（7年）= 標準セットで購入の純負担が賃貸を下回る年。"""
        self.assertEqual(breakeven("low"), A.holding_recommended_min_years)

    def test_case02_no_rent_growth_around_year_8(self):
        """cases/02 期待する回答5: 標準前提での損益分岐（賃料上昇なしで8年目前後）。"""
        self.assertIn(breakeven("low", include_danshin=False), (7, 8, 9))


class TestInflationSets(unittest.TestCase):
    """calc「金利・物価・賃料・価格はセットで置く」・cases/09 の例（5,000万円・金利2.5%・物価2.0%・賃料2.0%）。"""

    def test_pass_through_100_is_year_5(self):
        self.assertEqual(breakeven("high", price_pass_through_pct=100.0), 5)

    def test_pass_through_0_is_year_14(self):
        self.assertEqual(breakeven("high", price_pass_through_pct=0.0), 14)

    def test_pass_through_0_and_rent_0_5_is_year_35(self):
        self.assertEqual(breakeven("high", price_pass_through_pct=0.0, rent_growth_pct=0.5), 35)

    def test_case09_rent_0_5_with_full_pass_through_is_year_6(self):
        """cases/09 期待する回答5: 物価2.0%・賃料0.5%でも分岐は6年目。"""
        self.assertEqual(breakeven("high", rent_growth_pct=0.5), 6)

    def test_rate_only_raised_to_2_5_is_year_35(self):
        """principles/04 誤解表: 金利2.5%で物価も賃料も据え置くと分岐は35年目。
        【解釈】検算ケース（物価0%・賃料0%・管理費据え置き・修繕積立金3%）に売却諸費用と団信相当を入れ、
        金利だけ2.5%に上げたもの。低セットの修繕積立金2%で計算すると32年目になる（差は報告に記載）。"""
        self.assertEqual(breakeven(**KENSAN, interest_rate_pct=2.5), 35)
        self.assertEqual(breakeven("low", interest_rate_pct=2.5), 32)

    def test_higher_rate_set_breaks_even_earlier(self):
        """cases/09: 金利が高いセットのほうが分岐が早い（賃料と物件価格も上がるため）。"""
        self.assertGreaterEqual(breakeven("low"), breakeven("medium"))
        self.assertGreaterEqual(breakeven("medium"), breakeven("high"))


class TestOtherFigures(unittest.TestCase):
    def test_wait_breakeven_drop(self):
        """holding.wait_breakeven_drop_pct_per_year・principles/01: 家賃180万円/年 ÷ 5,000万円 = 3.6%/年。"""
        self.assertAlmostEqual(1_800_000 / L * 100, A.wait_breakeven_drop_pct)

    def test_placeholder_rent(self):
        """calc「比較賃料 R₁ の決め方」3: P × 3.8% ÷ 12。5,000万円なら月15.8万円。"""
        self.assertEqual(round(man(L * A.comparable_rent_ratio_pct / 100 / 12), 1), 15.8)

    def test_rent_growth_multipliers(self):
        """principles/04 原則: 年1%の上昇で20年後約1.2倍、30年後約1.35倍。"""
        self.assertEqual(round(1.01 ** 20, 2), 1.22)
        self.assertEqual(round(1.01 ** 30, 2), 1.35)

    def test_nominal_rent_in_30_years_is_about_19man(self):
        """calc 原則7: 30年後の賃料が月19万円（名目）。中セット・既定の住み替え（20年後14万円）で R(30)。"""
        res = simulate(inputs_from_assumptions(A, scenario="medium"))
        self.assertEqual(round(man(res.row(30).rent_monthly)), 19)

    def test_naive_totals_in_wrong_answer_examples(self):
        """cases/02・cases/08（NG例の数字）: 賃貸は16万円×12×35年 = 6,720万円、購入は累計で約8,000万円（超）。
        エンジンの累計キャッシュアウト（手残りを引く前）が8,000万円を超えることを確かめる。"""
        self.assertEqual(16 * 12 * 35, 6720)
        res = simulate(inputs_from_assumptions(A, **KENSAN, include_selling_costs=False, include_danshin=False))
        self.assertGreater(man(res.row(35).cashout_cum), 8000)
        self.assertLess(man(res.row(35).cashout_cum), 8500)

    def test_principles04_year8_and_40y_gap_about_19m(self):
        """principles/04 原則: 「標準ケース（低セット・売却諸費用込み）では8年目で追い抜き、40年で約1,900万円の差」。
        以前の文章は「40年で2000万円以上」だったが、8年目になる前提では1,918万円のため、このテストで見つけて修正した。"""
        res = simulate(inputs_from_assumptions(A, scenario="low", include_danshin=False))
        self.assertEqual(res.breakeven_year, 8)
        self.assertAlmostEqual(-res.row(40).buy_minus_rent / 10_000, 1_900, delta=50)   # 文章は「2000万円以上」から「約1,900万円」に修正

    def test_balance_sheet_50_year_term(self):
        """calc/balance-sheet.md: 5,000万円・金利1%・50年・年1.5%下落で、10年後に住宅由来の純資産がプラス。
        値は 4,299万円 − 4,189万円 = 約110万円（文章は「数百万円のプラス」から「約100万円のプラス」に修正）。"""
        v = L * 0.985 ** 10
        b = balance_after_months(L, 1.0, 50, 120)
        self.assertGreater(v, b)
        self.assertEqual(round(man(v - b)), 109)

    def test_balance_sheet_net_worth_after_purchase(self):
        """principles/01 判断手順3: 「諸費用350万円を払うと純資産は600万円から250万円に減る」。
        calc/balance-sheet.md の既定値（貯金400万円＋有価証券100万円＋その他100万円）と一致させた。
        以前は「500万円から150万円」で、2つの文書の初期資産が食い違っていた。"""
        net_now = 400 + 100 + 100
        self.assertEqual(net_now, 600)   # 文章は「500万円から150万円」から「600万円から250万円」に修正
        self.assertEqual(net_now - 350, 250)


class TestModelMechanics(unittest.TestCase):
    """式どおりに積み上がっているかの単体確認（calc「賃貸側の計算」「購入側の計算」）。"""

    def setUp(self):
        self.res = simulate(inputs_from_assumptions(A, scenario="medium"))

    def test_rent_side_items(self):
        r1, r2, r5, r20 = (self.res.row(k) for k in (1, 2, 5, 20))
        self.assertAlmostEqual(r1.rent_monthly, 160_000 * 1.01)          # R(k) = R_base × (1+g)^k
        self.assertEqual(r1.renewal_fee, 0)
        self.assertAlmostEqual(r2.renewal_fee, r2.rent_monthly)           # 偶数年に1カ月分
        self.assertAlmostEqual(r5.rent_monthly, 180_000 * 1.01 ** 5)      # 住み替え後は R₂
        self.assertAlmostEqual(r5.initial_cost, 4.5 * r5.rent_monthly)    # 住み替え年に4.5カ月分
        self.assertAlmostEqual(r20.initial_cost, 4.5 * r20.rent_monthly)
        self.assertAlmostEqual(r20.renewal_fee, r20.rent_monthly)         # 偶数年なので更新料も立つ（解釈）
        self.assertEqual(r1.danshin_premium, 36_000)

    def test_buy_side_items(self):
        r1, r10, r11, r36 = (self.res.row(k) for k in (1, 10, 11, 36))
        self.assertAlmostEqual(self.res.purchase_cost, 3_500_000)
        self.assertAlmostEqual(r1.management_fee, 168_000)
        self.assertAlmostEqual(r10.repair_reserve, 168_000 * 1.03 ** 9)
        self.assertAlmostEqual(r10.property_tax, 120_000 * 1.01 ** 9)
        self.assertAlmostEqual(r1.mortgage_deduction, 140_000)
        self.assertAlmostEqual(r10.mortgage_deduction, 140_000)
        self.assertEqual(r11.mortgage_deduction, 0)
        self.assertAlmostEqual(r10.renovation, 1_000_000 * 1.01 ** 9)
        self.assertEqual(r36.loan_payment, 0)
        self.assertEqual(r36.loan_balance, 0)
        self.assertAlmostEqual(r10.property_value, L * 0.985 ** 10 * 1.01 ** 10)

    def test_identities(self):
        for r in self.res.rows:
            self.assertAlmostEqual(r.equity_on_sale, r.property_value - r.selling_cost - r.loan_balance, places=4)
            self.assertAlmostEqual(r.buy_net, r.cashout_cum - r.equity_on_sale, places=4)
            self.assertAlmostEqual(r.buy_minus_rent, r.buy_net - r.rent_cum, places=4)
        be = self.res.breakeven_year
        self.assertGreaterEqual(self.res.row(be - 1).buy_minus_rent, 0)
        self.assertLess(self.res.row(be).buy_minus_rent, 0)

    def test_deduction_capped_by_balance(self):
        """控除 = min(年末残高, 限度額) × 0.7%。借入が限度額より小さければ残高で決まる。"""
        res = simulate(inputs_from_assumptions(A, scenario="low", loan_amount_yen=10_000_000))
        self.assertAlmostEqual(res.row(1).mortgage_deduction, res.row(1).loan_balance * 0.007)

    def test_inflation_override_moves_repair_growth(self):
        inp = inputs_from_assumptions(A, scenario=None, inflation_pct=2.0)
        self.assertAlmostEqual(inp.repair_reserve_growth_pct, 4.0)

    def test_subsidy_reduces_rent_side(self):
        base = simulate(inputs_from_assumptions(A, scenario="low"))
        sub = simulate(inputs_from_assumptions(A, scenario="low", rent_subsidy_yen_per_month=30_000))
        self.assertAlmostEqual(base.row(10).rent_cum - sub.row(10).rent_cum, 30_000 * 12 * 10)

    def test_invalid_inputs(self):
        with self.assertRaises(ValueError):
            simulate(inputs_from_assumptions(A, horizon_years=0))
        with self.assertRaises(ValueError):
            simulate(inputs_from_assumptions(A, selling_cost_method="x"))


class TestCli(unittest.TestCase):
    def run_cli(self, *argv):
        out = io.StringIO()
        code = cli_main(list(argv), out=out)
        return code, out.getvalue()

    def test_json_output(self):
        code, text = self.run_cli("--scenario", "high", "--pass-through", "0", "--json")
        self.assertEqual(code, 0)
        d = json.loads(text)
        self.assertEqual(d["breakeven_year"], 14)
        self.assertEqual(len(d["rows"]), 40)

    def test_table_output(self):
        code, text = self.run_cli("--scenario", "low", "--stay", "10", "--years", "12")
        self.assertEqual(code, 0)
        self.assertIn("7年目", text)
        self.assertIn("N = 10年", text)

    def test_summary_json(self):
        code, text = self.run_cli("--summary", "--json")
        self.assertEqual(code, 0)
        rows = json.loads(text)["summary"]
        got = {(r["scenario"], r["depreciation"], r["sensitivity"]): r["breakeven_year"] for r in rows}
        self.assertEqual(got[("low", "medium", None)], 7)
        self.assertEqual(got[("medium", "medium", None)], 6)
        self.assertEqual(got[("high", "medium", None)], 5)
        self.assertEqual(got[("high", "medium", "pass_through_0")], 14)
        self.assertEqual(got[("high", "medium", "pass_through_0_and_rent_growth_0.5")], 35)


if __name__ == "__main__":
    unittest.main()
