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


class TestReviewFindings(unittest.TestCase):
    """PR #4 のレビュー指摘への対策のテスト。"""

    @classmethod
    def setUpClass(cls):
        if not HAS_MCP:
            raise unittest.SkipTest("mcp・httpx が無い")
        sys.path.insert(0, str(ROOT / "scripts" / "hbf_mcp"))
        import server  # noqa: PLC0415
        import http_guard  # noqa: PLC0415
        cls.server, cls.http_guard = server, http_guard

    def _token(self, payload: dict) -> str:
        import base64, zlib  # noqa: E401, PLC0415
        raw = zlib.compress(json.dumps(payload).encode())
        return "c1." + base64.urlsafe_b64encode(raw).decode().rstrip("=")

    def test_forged_id_goes_through_input_validation(self):
        """細工した相談IDの中身も、ふつうの入力と同じ範囲・型の検査を通す。"""
        from mcp.server.mcpserver.exceptions import ToolError  # noqa: PLC0415
        for bad in ({"stay_years": 3000}, {"interest_rate_pct": 99}, {"property_price_yen": "abc"},
                    {"purpose": "あ" * 600}):
            with self.subTest(bad=bad), self.assertRaises(ToolError):
                self.server._session(self._token(bad))
        ok = self.server._session(self._token({"stay_years": 10, "property_price_yen": 50_000_000}))
        self.assertEqual(ok.stay_years, 10)

    def test_zero_values_survive_the_id(self):
        """貯金0円・金利0%・築0年は入力済みとして相談IDに残る（0 == False で落とさない）。"""
        f = self.server.consult.Facts(savings_yen=0, interest_rate_pct=0.0, property_age_years=0, stay_years=5)
        back = self.server._session(self.server._encode(f))
        self.assertEqual((back.savings_yen, back.interest_rate_pct, back.property_age_years), (0, 0.0, 0))

    def test_stay_up_to_50_years_reports(self):
        """受け付ける上限の50年まで、診断レポートが出る（以前は40年を超えると失敗した）。"""
        f = self.server.consult.Facts(property_price_yen=50_000_000, stay_years=50)
        r = self.server.report.diagnose(f)
        self.assertIn("50年住んだ場合（購入−賃貸。負なら購入が有利）", r["診断"]["賃貸か購入か"])

    def test_rate_limit_key_ignores_caller_controlled_entries(self):
        """X-Forwarded-For の先頭は利用者が書けるので使わない。末尾から信頼できる段数の値を使う。"""
        ip = self.http_guard._client_ip
        scope = lambda xff: {"headers": [(b"x-forwarded-for", xff.encode())], "client": ("10.0.0.1", 1)}  # noqa: E731
        # Cloud Run に直接（Google のフロントエンドが末尾に本当の接続元を付ける）
        self.assertEqual(ip(scope("1.1.1.1, 198.51.100.7"), 1), "198.51.100.7")
        self.assertEqual(ip(scope("9.9.9.9, 198.51.100.7"), 1), "198.51.100.7")
        # Firebase 経由（利用者, Firebase の順。先頭に偽の値を足されても変わらない）
        self.assertEqual(ip(scope("198.51.100.7, 203.0.113.9"), 2), "198.51.100.7")
        self.assertEqual(ip(scope("6.6.6.6, 198.51.100.7, 203.0.113.9"), 2), "198.51.100.7")
        # 段数より短いときは接続そのものの相手を使う
        self.assertEqual(ip(scope(""), 2), "10.0.0.1")


class TestMeasurementUrls(unittest.TestCase):
    """MCP 以外の計測用 URL: AI に読ませる前提の配信と、クリック計測の転送。"""

    @classmethod
    def setUpClass(cls):
        if not HAS_MCP:
            raise unittest.SkipTest("mcp・httpx が無い")
        sys.path.insert(0, str(ROOT / "scripts" / "hbf_mcp"))
        import server  # noqa: PLC0415
        from starlette.testclient import TestClient  # noqa: PLC0415
        cls.client = TestClient(server.http_app(allowed_hosts=["testserver"]), follow_redirects=False)

    def logs(self, fn):
        import contextlib, io  # noqa: E401, PLC0415
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            r = fn()
        return r, [json.loads(x) for x in buf.getvalue().splitlines() if x.startswith("{")]

    def test_ai_text_is_served_uncached_and_counted_by_agent(self):
        ua = "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; ChatGPT-User/1.0; +https://openai.com/bot"
        r, logs = self.logs(lambda: self.client.get("/ai/home-buying?src=chatgpt-note", headers={"User-Agent": ua}))
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/plain", r.headers["content-type"])
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.assertIn("絶対に守る5つのルール", r.text)
        fetch = [x for x in logs if x["event"] == "hbf_fetch"][0]
        self.assertEqual((fetch["agent"], fetch["src"]), ("ChatGPT（利用者の操作）", "chatgpt-note"))
        self.assertNotIn("ip", json.dumps(fetch).lower().replace("ipad", ""))

    def test_click_redirects_only_to_fixed_destinations(self):
        r, logs = self.logs(lambda: self.client.get("/go/home-buying/chatgpt?src=note"))
        self.assertEqual(r.status_code, 302)
        loc = r.headers["location"]
        self.assertTrue(loc.startswith("https://chatgpt.com/?q="))
        self.assertIn("terass.house%2Fai%2Fhome-buying%3Fsrc%3Dchatgpt-note", loc)  # 読ませる URL も計測用
        click = [x for x in logs if x["event"] == "hbf_click"][0]
        self.assertEqual((click["target"], click["src"]), ("chatgpt", "note"))
        self.assertEqual(self.client.get("/go/home-buying/lp?src=x").headers["location"],
                         "https://terass-inc.github.io/home-buying-framework/?utm_source=x")
        # 決めていない行き先や、URL を渡しての転送はできない（オープンリダイレクトにしない）
        self.assertEqual(self.client.get("/go/home-buying/https://evil.example").status_code, 404)
        self.assertEqual(self.client.get("/go/home-buying/evil").status_code, 404)

    def test_src_is_sanitized_and_methods_limited(self):
        _, logs = self.logs(lambda: self.client.get("/ai/home-buying?src=<script>alert(1)</script>"))
        self.assertEqual([x for x in logs if x["event"] == "hbf_fetch"][0]["src"], "other")
        self.assertEqual(self.client.post("/ai/home-buying").status_code, 405)
        self.assertEqual(self.client.head("/ai/home-buying").status_code, 200)


class TestUsageReport(unittest.TestCase):
    def test_failed_calls_are_not_counted_as_consultations(self):
        import subprocess  # noqa: PLC0415
        e = lambda tool, ok: {"timestamp": "2026-10-20T01:00:00Z", "jsonPayload": {  # noqa: E731
            "event": "hbf_mcp_request", "method": "tools/call", "tool": tool, "ok": ok}}
        data = json.dumps([e("start_consultation", True), e("start_consultation", False), e("update_facts", False)])
        out = subprocess.run([sys.executable, str(ROOT / "scripts" / "hbf_mcp" / "usage_report.py")],
                             input=data, capture_output=True, text=True, check=True).stdout
        self.assertIn("道具の呼び出し: 3回（失敗 2回）", out)
        self.assertIn("相談の開始: 1件", out)
        self.assertIn("診断の更新: 0回", out)


if __name__ == "__main__":
    unittest.main()
