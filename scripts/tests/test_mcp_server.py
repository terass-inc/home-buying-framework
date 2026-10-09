"""MCP サーバー（scripts/hbf_mcp/server.py）のテスト。mcp パッケージがない環境では飛ばす。

実行: python3 -m unittest discover -s scripts/tests
"""
import asyncio
import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HAS_MCP = importlib.util.find_spec("mcp") is not None


@unittest.skipUnless(HAS_MCP, "mcp パッケージが無い（pip install 'mcp>=2,<3'）")
class TestMcpServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "scripts" / "hbf_mcp"))
        import server  # noqa: PLC0415
        from mcp import Client  # noqa: PLC0415
        cls.server, cls.Client = server.server, Client

    def call(self, name, args):
        async def run():
            async with self.Client(self.server) as c:
                return await c.call_tool(name, args)
        return asyncio.run(run())

    def data(self, result):
        return result.structured_content or json.loads(result.content[0].text)

    def test_tools_listed(self):
        async def run():
            async with self.Client(self.server) as c:
                return [t.name for t in (await c.list_tools()).tools]
        self.assertEqual(set(asyncio.run(run())), {
            "get_framework", "list_principles", "get_principle", "get_assumptions",
            "compare_rent_vs_buy", "loan_schedule"})

    def test_loan_schedule_matches_article_numbers(self):
        """20年ローンを3年で売る例（記事の数字）: 月約23万円、3年間の利息約140万円、残債約4,312万円。"""
        d = self.data(self.call("loan_schedule", {
            "principal_yen": 50_000_000, "interest_rate_pct": 1.0, "term_years": 20, "sell_after_years": 3}))
        self.assertEqual(d["毎月の返済_円"], 229_947)
        self.assertEqual(d["3年間に払う利息_万円"], 140)
        self.assertEqual(d["3年後の残債_万円"], 4_312)

    def test_compare_breakeven_by_scenario(self):
        """calc/rent-vs-buy.md の参考値: 低セット（売却諸費用＋団信相当）7年目、中セット6年目、高セット5年目。"""
        d = self.data(self.call("compare_rent_vs_buy", {
            "price_yen": 50_000_000, "rent_yen_per_month": 160_000, "stay_years": 10, "scenario": "low"}))
        self.assertEqual(d["感度"]["lowセット"], 7)
        self.assertEqual(d["感度"]["mediumセット"], 6)
        self.assertEqual(d["感度"]["highセット"], 5)

    def test_bad_principle_id_is_tool_error(self):
        r = self.call("get_principle", {"id": "zz"})
        self.assertTrue(r.is_error)
        self.assertIn("00", r.content[0].text)


if __name__ == "__main__":
    unittest.main()
