from __future__ import annotations

import json
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from app import create_server  # noqa: E402
from engine import Engine, Store  # noqa: E402

TERMS = {
    "notional": "1000000",
    "issue_date": "2026-01-05",
    "maturity_date": "2027-01-05",
    "basket": [{"underlying": "AAA", "initial_price": "10.0000"}],
    "knock_out_barrier": "1.0000",
    "knock_in_barrier": "0.7500",
    "coupon_barrier": "0.8500",
    "coupon_rate_per_period": "0.0125",
    "put_strike": "0.7500",
    "autocall_dates": ["2026-07-06", "2027-01-05"],
}


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        engine = Engine(Store(cls.tmp.name))
        engine.set_holidays("CN-SSE", [])
        engine.issue_terms("P1", TERMS, "2025-12-28")
        cls.server = create_server("127.0.0.1", 0, engine)
        cls.port = cls.server.server_address[1]
        import threading

        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.tmp.cleanup()

    def _req(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_flow_over_http(self):
        status, payload = self._req("GET", "/health")
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

        status, payload = self._req(
            "POST", "/market-data/prices",
            {"underlying": "AAA", "date": "2026-07-06", "price": "10.3000", "status": "official"},
        )
        self.assertEqual(200, status)
        self.assertEqual("official", payload["status"])

        status, payload = self._req("POST", "/products/P1/observations", {"date": "2026-07-06"})
        self.assertEqual(200, status)
        self.assertEqual("confirmed", payload["state"])
        self.assertTrue(payload["decision"]["knock_out"])

        status, view = self._req("GET", "/products/P1")
        self.assertEqual(200, status)
        self.assertTrue(view["state"]["autocalled"])
        redemption = next(c for c in view["cashflows"]["confirmed"] if c["kind"] == "redemption")
        self.assertEqual("1000000.00", redemption["amount"])

        # 结算幂等：同一指令重试不重复付款
        body = {"idempotency_key": "api-key-1", "cashflow_id": redemption["cashflow_id"]}
        status, first = self._req("POST", "/settlements", body)
        self.assertEqual(200, status)
        status, replay = self._req("POST", "/settlements", body)
        self.assertEqual(200, status)
        self.assertEqual(first["settlement_id"], replay["settlement_id"])

        status, expl = self._req("GET", f"/cashflows/{redemption['cashflow_id']}/explanation")
        self.assertEqual(200, status)
        self.assertIsNotNone(expl["settlement"])
        self.assertEqual("paid", expl["current"]["state"])

        # 发行人更正：撤销收盘价 → 已付赎回出现差额调整
        status, payload = self._req(
            "POST", "/market-data/corrections",
            {"underlying": "AAA", "date": "2026-07-06", "action": "reissue", "price": "9.9000", "reason": "更正"},
        )
        self.assertEqual(200, status)
        status, view = self._req("GET", "/products/P1")
        self.assertFalse(view["state"]["autocalled"])
        self.assertEqual(1, len(view["cashflows"]["adjustments"]))
        self.assertEqual("-1000000.00", view["cashflows"]["adjustments"][0]["amount"])

        status, payload = self._req("GET", "/products/NOPE")
        self.assertEqual(404, status)
        self.assertEqual("not_found", payload["error"])


if __name__ == "__main__":
    unittest.main()
