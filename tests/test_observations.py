"""观察与障碍判定：缺价挂起、临时价、补发确认、敲入、敲出。"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from helpers import at, close, feed_daily, make_engine, make_terms  # noqa: E402


class ObservationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_missing_price_pends_not_skips(self):
        engine = make_engine(self.tmp)
        engine.set_clock(at("2026-04-07", "18:00"))
        engine.recompute()
        obs = engine._latest_obs("TEST-1:scheduled:2026-04-07")
        self.assertIsNotNone(obs, "缺价时观察被跳过（应建档为 pending）")
        self.assertEqual(obs.status, "pending")
        self.assertTrue(obs.pending_reasons)
        coupon = engine.cashflows["coupon:2026-04-07"][-1]
        self.assertEqual(coupon.state, "projected")

    def test_provisional_only_pends(self):
        engine = make_engine(self.tmp)
        engine.set_clock(at("2026-04-07", "18:00"))
        engine.ingest_price(close("TST", "2026-04-07", "90.00",
                                  status="provisional", pid="P-TST-prov"))
        obs = engine._latest_obs("TEST-1:scheduled:2026-04-07")
        self.assertEqual(obs.status, "pending")
        self.assertIn("临时价", "；".join(obs.pending_reasons))
        coupon = engine.cashflows["coupon:2026-04-07"][-1]
        self.assertEqual(coupon.state, "projected",
                         "临时价只能进入待确认，不得确认现金流")

    def test_late_official_confirms(self):
        engine = make_engine(self.tmp)
        engine.set_clock(at("2026-04-07", "18:00"))
        engine.recompute()
        engine.set_clock(at("2026-04-08", "09:30"))
        engine.ingest_price(close("TST", "2026-04-07", "90.00", version=2,
                                  published="2026-04-08T09:00:00+08:00"))
        obs = engine._latest_obs("TEST-1:scheduled:2026-04-07")
        self.assertEqual(obs.status, "confirmed")
        self.assertTrue(obs.outcomes["coupon_due"])
        self.assertFalse(obs.outcomes["autocall"])
        coupon = engine.cashflows["coupon:2026-04-07"][-1]
        self.assertEqual(coupon.state, "confirmed")
        self.assertEqual(str(coupon.amount), "30000.00")
        self.assertEqual(coupon.value_date.isoformat(), "2026-04-10")

    def test_knock_in_daily(self):
        engine = make_engine(self.tmp)
        feed_daily(engine, "TST", "2026-01-05", "2026-02-09", "90.00")
        engine.ingest_price(close("TST", "2026-02-10", "69.00"),
                            auto_recompute=False)
        engine.set_clock(at("2026-02-10", "18:00"))
        engine.recompute()
        ki = engine.status_info["ki"]
        self.assertTrue(ki["triggered"])
        self.assertEqual(ki["first_breach_date"], "2026-02-10")
        obs = engine._latest_obs("TEST-1:ki_daily:2026-02-10")
        self.assertEqual(obs.status, "confirmed")
        self.assertEqual(obs.outcomes["breach_tickers"], ["TST"])
        self.assertTrue(any("触发敲入" in line for line in obs.calc_path))

    def test_autocall_cancels_later_observations(self):
        engine = make_engine(self.tmp)
        feed_daily(engine, "TST", "2026-01-05", "2026-04-06", "90.00")
        engine.ingest_price(close("TST", "2026-04-07", "106.00"),
                            auto_recompute=False)
        feed_daily(engine, "TST", "2026-04-08", "2026-04-10", "107.00")
        engine.set_clock(at("2026-04-10", "18:00"))  # 一次重算覆盖敲出日之后的交易日
        engine.recompute()
        self.assertEqual(engine.status_info["status"], "called")
        self.assertEqual(engine.status_info["call_date"], "2026-04-07")
        redemption = engine.cashflows["redemption:autocall"][-1]
        self.assertEqual(redemption.state, "confirmed")
        self.assertEqual(str(redemption.amount), "1000000.00")
        final_obs = engine._latest_obs("TEST-1:scheduled:2026-07-06")
        self.assertEqual(final_obs.status, "cancelled")
        ki_after = engine._latest_obs("TEST-1:ki_daily:2026-04-08")
        self.assertIsNotNone(ki_after)
        self.assertEqual(ki_after.status, "cancelled",
                         "敲出日之后的逐日观察应被取消")
        # 重算幂等：再次重算不产生新版本
        versions_before = {k: len(v) for k, v in engine.observations.items()}
        engine.recompute()
        engine.recompute()
        versions_after = {k: len(v) for k, v in engine.observations.items()}
        self.assertEqual(versions_before, versions_after)


class CouponMemoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_memory_accrues_and_recovers(self):
        engine = make_engine(self.tmp)
        feed_daily(engine, "TST", "2026-01-05", "2026-04-06", "90.00")
        engine.ingest_price(close("TST", "2026-04-07", "75.00"),
                            auto_recompute=False)  # < 0.80 票息障碍，不付
        feed_daily(engine, "TST", "2026-04-08", "2026-07-03", "90.00")
        engine.ingest_price(close("TST", "2026-07-06", "85.00"),
                            auto_recompute=False)  # ≥ 0.80，补付两期
        engine.set_clock(at("2026-07-06", "18:00"))
        engine.recompute()
        q1 = engine.cashflows.get("coupon:2026-04-07")
        self.assertTrue(q1 is None or q1[-1].state == "cancelled",
                        "Q1 不满足票息条件，不应存在应付现金流")
        q2 = engine.cashflows["coupon:2026-07-06"][-1]
        self.assertEqual(q2.state, "confirmed")
        self.assertEqual(str(q2.amount), "60000.00",
                         "记忆特性：Q2 应付 = 当期 30000 + 补付 Q1 30000")
        self.assertEqual(q2.lineage["memory_backlog"], 1)


if __name__ == "__main__":
    unittest.main()
