"""GA4 への送信（scripts/hbf_mcp/ga4.py）: 設定がなければ送らない・User-Agent や IP を送らない。"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "hbf_mcp"))
import ga4  # noqa: E402
import measure  # noqa: E402


class TestGa4(unittest.TestCase):
    def test_no_config_sends_nothing(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(ga4.threading, "Thread") as th:
            ga4.send("hbf_fetch", src="note")
        th.assert_not_called()

    def test_sends_with_config(self):
        env = {"GA4_MEASUREMENT_ID": "G-TEST", "GA4_API_SECRET": "s"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(ga4.threading, "Thread") as th:
            ga4.send("hbf_click", target="lp", src="x")
        url, body = th.call_args.kwargs["args"]
        self.assertIn("measurement_id=G-TEST", url)
        self.assertIn(b'"name": "hbf_click"', body)

    def test_payload_drops_empty_and_truncates(self):
        p = ga4.payload("hbf_fetch", {"src": None, "doc": "lite", "agent": "あ" * 300})
        params = p["events"][0]["params"]
        self.assertNotIn("src", params)
        self.assertEqual(len(params["agent"]), 100)
        self.assertNotIn("ip", str(p).lower())

    def test_measure_log_does_not_send_user_agent(self):
        with mock.patch.object(measure.ga4, "send") as send:
            measure.handle({"path": "/ai/home-buying", "query_string": b"src=note"}, "ChatGPT-User/1.0")
        name, kwargs = send.call_args.args[0], send.call_args.kwargs
        self.assertEqual((name, kwargs["src"], kwargs["agent"]), ("hbf_fetch", "note", "ChatGPT（利用者の操作）"))
        self.assertNotIn("user_agent", kwargs)


if __name__ == "__main__":
    unittest.main()
