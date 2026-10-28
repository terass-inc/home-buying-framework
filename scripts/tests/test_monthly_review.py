"""毎月の見直し（scripts/monthly_review.py）: 範囲内の値だけを market_rates に書き、ほかの前提には触れない。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # scripts/ を import パスに入れる

import monthly_review as mr  # noqa: E402
from rentbuy.yamlmini import loads  # noqa: E402

TEXT = mr.ASSUMPTIONS.read_text(encoding="utf-8")
RESULT = {
    "as_of": "2026-11-02",
    "market_rates": {
        "flat35_most_common_pct": 3.9, "flat35_month": "2026-11",
        "variable_major_bank_min_pct": "1.4〜1.5", "boj_policy_rate_pct": 1.25,
        "note": "変動金利は11月から上がった", "sources": ["https://www.flat35.com/", "http://insecure.example"],
    },
    "checks": [{"key": "tax_and_programs.gift_tax_exemption", "status": "要注意",
                "finding": "延長は未定 | 12月の大綱で判明", "proposal": "", "sources": ["https://www.mof.go.jp/"]}],
}


class TestMonthlyReview(unittest.TestCase):
    def test_updates_only_market_rates_and_dates(self):
        new, changes = mr.apply_market_rates(TEXT, RESULT, "2026-11-02")
        self.assertEqual(len(changes), 3)
        d, old = loads(new), loads(TEXT)
        m = d["market_rates"]
        self.assertEqual((m["flat35_most_common_pct"], m["variable_major_bank_min_pct"], m["boj_policy_rate_pct"]),
                         (3.9, "1.4〜1.5", 1.25))
        self.assertEqual(str(m["as_of"]), "2026-11-02")
        self.assertEqual(str(d["updated_at"]), "2026-11-02")
        self.assertIn("見直し要", m["note"])
        self.assertEqual(m["sources"], ["https://www.flat35.com/"])  # https 以外は採用しない
        for k in d:  # market_rates と updated_at 以外は1文字も変えない
            if k not in ("market_rates", "updated_at"):
                self.assertEqual(d[k], old[k], k)
        self.assertIn("# フラット35（返済期間21〜35年", new)  # コメントは残す
        self.assertEqual(new.count("\n\n"), TEXT.count("\n\n"))  # 区切りの空行も消さない

    def test_rejects_out_of_range_and_missing_values(self):
        bad = {"market_rates": {"flat35_most_common_pct": 38.3, "variable_major_bank_min_pct": None,
                                "boj_policy_rate_pct": "1.25"}}
        new, changes = mr.apply_market_rates(TEXT, bad, "2026-11-02")
        self.assertEqual(changes, [])
        self.assertEqual(new, TEXT)  # 何も確認できなければ日付も変えない

    def test_extract_json_takes_last_block_and_report_escapes_pipes(self):
        text = '途中の例\n```json\n{"a": 1}\n```\n最終結果\n```json\n{"checks": []}\n```'
        self.assertEqual(mr.extract_json(text), {"checks": []})
        with self.assertRaises(ValueError):
            mr.extract_json("JSON なし")
        report = mr.render_report(RESULT, ["market_rates.flat35_most_common_pct = 3.9"], "2026-11-02")
        self.assertIn("延長は未定 ／ 12月の大綱で判明", report)
        self.assertIn("2026-11", report)

    def test_prompt_contains_current_values_and_items(self):
        p = mr.build_prompt(TEXT, "2026-11-02")
        self.assertIn("flat35_most_common_pct: 3.83", p)
        self.assertIn("tax_and_programs.gift_tax_exemption", p)


if __name__ == "__main__":
    unittest.main()
