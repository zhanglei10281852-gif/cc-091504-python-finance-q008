"""HTTP 接口冒烟测试：引导、查询、结算幂等、争议复原。"""
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import app  # noqa: E402
from helpers import at  # noqa: E402


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = tempfile.mkdtemp()
        app.REGISTRY = app.Registry(runtime_dir=Path(cls.runtime))
        engine = app.REGISTRY.get_engine("NOTE-A001")
        engine.set_clock(at("2026-09-20", "09:00"))
        engine.recompute()
        cls.server = app.create_server("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        app.REGISTRY = None

    def _req(self, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, {"raw": raw}

    def test_health(self):
        status, payload = self._req("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")

    def test_unknown_path_404(self):
        status, _ = self._req("GET", "/nope")
        self.assertEqual(status, 404)

    def test_products_and_operations(self):
        status, payload = self._req("GET", "/products")
        self.assertEqual(status, 200)
        self.assertIn("NOTE-A001", payload["products"])

        status, product = self._req("GET", "/products/NOTE-A001")
        self.assertEqual(status, 200)
        self.assertEqual(product["terms_version"], 1)

        status, view = self._req("GET", "/products/NOTE-A001/operations")
        self.assertEqual(status, 200)
        self.assertEqual(view["next_observation"]["scheduled_date"], "2026-09-19")
        self.assertEqual(view["next_observation"]["date"], "2026-09-21")
        self.assertIn("600519.SH", view["daily_evidence"])
        self.assertTrue(view["projected_cashflows"])

    def test_price_ingest_settle_explain_flow(self):
        # 补发 6/19 官方收盘价 → 观察确认 → 票息 confirmed
        status, _ = self._req("POST", "/products/NOTE-A001/prices", {
            "price_id": "P-300750.SZ-20260619-2", "underlying": "300750.SZ",
            "date": "2026-06-19", "value": "195.00", "price_type": "official_close",
            "status": "official", "version": 2,
            "published_at": "2026-06-22T09:05:00+08:00", "source": "exchange",
            "corrects": None,
        })
        self.assertEqual(status, 201)

        status, payload = self._req("GET", "/products/NOTE-A001/cashflows")
        self.assertEqual(status, 200)
        coupon = next(c for c in payload["cashflows"]
                      if c["key"] == "coupon:2026-06-19")
        self.assertEqual(coupon["state"], "confirmed")
        self.assertEqual(coupon["amount"], "20000.00")

        # 结算：同一指令重试幂等，不同指令冲突
        status, st1 = self._req("POST", "/cashflows/NOTE-A001~coupon:2026-06-19/settle",
                                {"instruction_id": "API-INS-1", "actor": "ops"})
        self.assertEqual(status, 200)
        self.assertEqual(st1["amount"], "20000.00")
        status, st2 = self._req("POST", "/cashflows/NOTE-A001~coupon:2026-06-19/settle",
                                {"instruction_id": "API-INS-1", "actor": "ops"})
        self.assertEqual(status, 200)
        self.assertEqual(st1, st2)
        status, err = self._req("POST", "/cashflows/NOTE-A001~coupon:2026-06-19/settle",
                                {"instruction_id": "API-INS-2", "actor": "ops"})
        self.assertEqual(status, 409)

        # 争议复原
        status, explain = self._req(
            "GET", "/cashflows/NOTE-A001~coupon:2026-06-19/explain")
        self.assertEqual(status, 200)
        self.assertEqual(explain["justification"]["paid_amount"], "20000.00")
        self.assertEqual(explain["justification"]["terms_version"], 1)

    def test_reconcile_endpoint(self):
        status, payload = self._req("GET", "/products/NOTE-A001/reconcile")
        self.assertEqual(status, 200)
        self.assertIn("report", payload)
        self.assertTrue(all("key" in r for r in payload["report"]))


if __name__ == "__main__":
    unittest.main()
