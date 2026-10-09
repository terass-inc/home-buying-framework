"""公開サーバーとしての守りのテスト（入力の範囲・回数制限・パスとメソッドの限定・ヘッダー・本文の大きさ）。

mcp パッケージがない環境では飛ばす。実行: python3 -m unittest discover -s scripts/tests
"""
import asyncio
import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HAS_MCP = importlib.util.find_spec("mcp") is not None and importlib.util.find_spec("httpx") is not None


@unittest.skipUnless(HAS_MCP, "mcp・httpx が無い")
class TestSecurity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "scripts" / "hbf_mcp"))
        import server  # noqa: PLC0415
        from mcp import Client  # noqa: PLC0415
        from starlette.testclient import TestClient  # noqa: PLC0415
        from http_guard import HttpGuard  # noqa: PLC0415
        cls.server, cls.Client, cls.TestClient, cls.HttpGuard = server, Client, TestClient, HttpGuard
        cls.h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}

    def call(self, name, args):
        async def run():
            async with self.Client(self.server.server) as c:
                return await c.call_tool(name, args)
        return asyncio.run(run())

    def test_out_of_range_inputs_are_rejected_before_computing(self):
        """現実にありえない値（居住3,000年、負の金額、金利50%）は計算せずに断る。"""
        for name, args in [
            ("compare_rent_vs_buy", {"price_yen": 50_000_000, "rent_yen_per_month": 160_000, "stay_years": 3000}),
            ("loan_schedule", {"principal_yen": -5, "interest_rate_pct": 1.0, "term_years": 35}),
            ("loan_schedule", {"principal_yen": 50_000_000, "interest_rate_pct": 50, "term_years": 35}),
            ("wait_cost", {"property_price_yen": 50_000_000, "monthly_rent_yen": 150_000, "wait_years": 1e9}),
            ("start_consultation", {"facts": {"stay_years": 1e9}}),
            ("start_consultation", {"facts": {"purpose": "あ" * 501}}),
        ]:
            with self.subTest(name=name, args=args):
                self.assertTrue(self.call(name, args).is_error)
        self.assertFalse(self.call("compare_rent_vs_buy", {
            "price_yen": 50_000_000, "rent_yen_per_month": 160_000, "stay_years": 50}).is_error)

    def _client(self, **kw):
        return self.TestClient(self.server.http_app(allowed_hosts=["testserver"]), **kw)

    def test_paths_methods_headers_and_body_size(self):
        with self._client() as c:
            self.assertEqual(c.get("/").status_code, 404)
            self.assertEqual(c.get("/admin").status_code, 404)
            self.assertEqual(c.put("/mcp", json={}).status_code, 405)
            r = c.post("/mcp", headers=self.h, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.headers["cache-control"], "no-store")
            self.assertEqual(r.headers["x-content-type-options"], "nosniff")
            big = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"pad": "x" * 100_000}}
            self.assertEqual(c.post("/mcp", headers=self.h, content=json.dumps(big)).status_code, 413)

    def test_rate_limit_per_client(self):
        """接続元ごとに1分あたりの回数を超えたら 429。別の接続元や、1分たった後は通る。"""
        now = [0.0]

        async def ok_app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

        guard = self.HttpGuard(ok_app, "/mcp", rate_per_min=3, clock=lambda: now[0])
        c = self.TestClient(guard)  # 起動・終了の処理（lifespan）は不要なので with を使わない
        a = {"X-Forwarded-For": "203.0.113.1"}
        self.assertEqual([c.post("/mcp", headers=a).status_code for _ in range(4)], [200, 200, 200, 429])
        self.assertEqual(c.post("/mcp", headers={"X-Forwarded-For": "203.0.113.2"}).status_code, 200)
        now[0] = 61
        self.assertEqual(c.post("/mcp", headers=a).status_code, 200)


if __name__ == "__main__":
    unittest.main()
