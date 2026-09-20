"""查询投影：运营视图与争议复原。"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from helpers import at, close, feed_daily, make_engine  # noqa: E402
from queries import explain_cashflow, operations_view  # noqa: E402


class OperationsViewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.engine = make_engine(self.tmp)
        feed_daily(self.engine, "TST", "2026-01-05", "2026-04-06", "90.00")
        self.engine.ingest_price(close("TST", "2026-04-07", "90.00"),
                                 auto_recompute=False)
        self.engine.set_clock(at("2026-04-07", "18:00"))
        self.engine.recompute()

    def test_view_contains_required_sections(self):
        view = operations_view(self.engine)
        self.assertEqual(view["status"]["status"], "alive")
        self.assertIn("TST", view["daily_evidence"])
        self.assertTrue(view["daily_evidence"]["TST"], "应有逐日证据")
        day = view["daily_evidence"]["TST"][0]
        for field in ("date", "status", "price", "ratio", "ki_barrier_level"):
            self.assertIn(field, day)
        self.assertEqual(view["next_observation"]["scheduled_date"], "2026-07-06")
        keys = {p["key"] for p in view["projected_cashflows"]}
        self.assertIn("coupon:2026-07-06", keys)
        self.assertIn("redemption:maturity", keys)
        self.assertIn("knock_in", view["barrier_levels"]["TST"])

    def test_next_observation_none_after_call(self):
        self.engine.ingest_price(close("TST", "2026-04-07", "106.00", version=2,
                                       published="2026-04-07T16:00:00+08:00",
                                       corrects="P-TST-20260407-1"))
        view = operations_view(self.engine)
        self.assertEqual(view["status"]["status"], "called")
        self.assertIsNone(view["next_observation"])


class ExplainTest(unittest.TestCase):
    def test_explain_reconstructs_payment_and_corrections(self):
        tmp = tempfile.mkdtemp()
        engine = make_engine(tmp)
        feed_daily(engine, "TST", "2026-01-05", "2026-04-06", "90.00")
        engine.ingest_price(close("TST", "2026-04-07", "90.00"),
                            auto_recompute=False)
        engine.set_clock(at("2026-04-07", "18:00"))
        engine.recompute()
        engine.set_clock(at("2026-04-10", "10:00"))
        engine.settle("TEST-1:coupon:2026-04-07", "INS-1", actor="ops")
        # 付款后更正：90 → 79，票息不再应付
        engine.set_clock(at("2026-04-13", "10:00"))
        engine.ingest_price(close("TST", "2026-04-07", "79.00", version=2,
                                  published="2026-04-13T09:00:00+08:00",
                                  corrects="P-TST-20260407-1"))

        explain = explain_cashflow(engine, "TEST-1:coupon:2026-04-07")
        just = explain["justification"]
        self.assertEqual(just["paid_amount"], "30000.00")
        self.assertEqual(just["terms_version"], 1)
        # 付款时固定的行情快照仍为 90.00（不是更正后的 79.00）
        self.assertEqual(just["evidence"]["TST"]["price"]["value"], "90.00")
        self.assertTrue(any("30000.00" in line for line in just["calc_path"]))
        self.assertEqual(just["settled_by"], "INS-1")
        self.assertEqual(len(explain["settlements"]), 1)
        corrections = explain["subsequent_corrections"]
        self.assertEqual(len(corrections), 1,
                         "只有付款之后进入的事件才计入后续更正")
        self.assertEqual(corrections[0]["type"], "price_ingested")
        self.assertEqual(corrections[0]["payload"]["value"], "79.00")
        adjustments = explain["difference_adjustments"]
        self.assertEqual(len(adjustments), 1)
        self.assertEqual(adjustments[0]["amount"], "-30000.00")
        # 历史版本链完整：confirmed → paid → adjusted
        states = [v["state"] for v in explain["history"]]
        self.assertIn("confirmed", states)
        self.assertIn("paid", states)
        self.assertIn("adjusted", states)


if __name__ == "__main__":
    unittest.main()
