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
            "start_consultation", "update_facts", "diagnose", "what_if", "next_step", "self_check",
            "get_framework", "list_principles", "get_principle", "get_assumptions",
            "compare_rent_vs_buy", "loan_schedule", "loan_term_comparison", "wait_cost",
            "borrowing_budget", "balance_sheet", "selling_costs_breakdown"})

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

    def test_consultation_session_flow(self):
        """相談を作り、まとめて事実を足すと診断が更新され、what_if は保存しない。"""
        async def run():
            async with self.Client(self.server) as c:
                d = self.data(await c.call_tool("start_consultation", {"facts": {"property_price_yen": 50_000_000}}))
                cid = d["consultation_id"]
                self.assertIn("居住年数が未確認", " ".join(d["report"]["まだ出せない結論"]))
                self.assertIn("仮置き", " ".join(d["report"]["仮置きしている前提"]) + "仮置き")
                d = self.data(await c.call_tool("update_facts", {"consultation_id": cid, "facts": {
                    "purpose": "学区を決めて落ち着きたい", "purpose_sentence_confirmed": True,
                    "stay_years": 10, "comparable_rent_yen": 160_000}}))
                self.assertNotEqual(d["consultation_id"], cid)  # 状態を詰めた ID なので更新で変わる
                cid = d["consultation_id"]
                self.assertIn("10年住んだ場合（購入−賃貸。負なら購入が有利）", d["report"]["診断"]["賃貸か購入か"])
                self.assertEqual(d["report"]["仮置きしている前提"], [])
                w = self.data(await c.call_tool("what_if", {"consultation_id": cid, "changes": {"stay_years": 4}}))
                diff = w["主な数字の変化"]["居住年数時点の購入−賃貸（中）"]
                self.assertLess(diff["今"], 0)        # 10年なら購入が有利
                self.assertGreater(diff["変更後"], 0)  # 4年なら賃貸が有利
                d = self.data(await c.call_tool("diagnose", {"consultation_id": cid}))
                self.assertEqual(d["分かっていること"]["stay_years"], 10)  # what_if は保存しない
        asyncio.run(run())

    def test_broken_consultation_id_is_tool_error(self):
        r = self.call("diagnose", {"consultation_id": "c1.broken"})
        self.assertTrue(r.is_error)

    def test_http_mode_is_stateless(self):
        """URL で公開する HTTP モード: セッションなしの JSON 応答で、相談IDだけで相談を続けられる。"""
        from starlette.testclient import TestClient  # noqa: PLC0415
        import server  # noqa: PLC0415
        h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}

        def rpc(client, i, method, params):
            r = client.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": i, "method": method, "params": params})
            self.assertEqual(r.status_code, 200, r.text)
            res = r.json()["result"]
            if "content" not in res:
                return res
            return res.get("structuredContent") or json.loads(res["content"][0]["text"])

        with TestClient(server.http_app(allowed_hosts=["testserver"])) as c1:
            rpc(c1, 1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "t", "version": "0"}})
            res = rpc(c1, 2, "tools/call", {"name": "start_consultation",
                                            "arguments": {"facts": {"property_price_yen": 50_000_000, "stay_years": 10}}})
            cid = res["consultation_id"]
        with TestClient(server.http_app(allowed_hosts=["testserver"])) as c2:  # 別のサーバーが応答しても続けられる
            rpc(c2, 1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "t", "version": "0"}})
            res = rpc(c2, 2, "tools/call", {"name": "diagnose", "arguments": {"consultation_id": cid}})
            self.assertEqual(res["分かっていること"]["stay_years"], 10)
        with TestClient(server.http_app(allowed_hosts=["example.com"])) as c3:  # 許可していないホストは拒否
            r = c3.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
            self.assertEqual(r.status_code, 421)

    def test_usage_log_has_no_consultation_content(self):
        """計測ログ: 道具の名前と成否は残すが、相談の中身（年収などの値、consultation_id）は残さない。"""
        import contextlib, io  # noqa: E401, PLC0415
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ok = self.call("start_consultation", {"facts": {"household_income_yen": 12_345_678, "property_price_yen": 50_000_000}})
            self.call("get_principle", {"id": "zz"})
        lines = [json.loads(x) for x in buf.getvalue().splitlines() if '"hbf_mcp_request"' in x]
        calls = {x["tool"]: x for x in lines if x["method"] == "tools/call"}
        self.assertTrue(calls["start_consultation"]["ok"])
        self.assertFalse(calls["get_principle"]["ok"])
        cid = self.data(ok)["consultation_id"]
        self.assertNotIn("12345678", buf.getvalue())
        self.assertNotIn(cid, buf.getvalue())

    def test_next_step_asks_one_question(self):
        d = self.data(self.call("next_step", {"topic": "loan_term", "facts": {"purpose": "家賃がもったいない"}}))
        self.assertEqual(d["next_question"]["field"], "purpose_sentence_confirmed")
        self.assertFalse(d["can_conclude"])

    def test_bad_principle_id_is_tool_error(self):
        r = self.call("get_principle", {"id": "zz"})
        self.assertTrue(r.is_error)
        self.assertIn("00", r.content[0].text)


if __name__ == "__main__":
    unittest.main()
