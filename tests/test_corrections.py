"""行情更正 / 撤销 / 补发：未付换版、已付差额调整、复核标记。"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from helpers import at, close, feed_daily, make_engine  # noqa: E402


def _confirmed_coupon(engine):
    feed_daily(engine, "TST", "2026-01-05", "2026-04-06", "90.00")
    engine.ingest_price(close("TST", "2026-04-07", "90.00"), auto_recompute=False)
    engine.set_clock(at("2026-04-07", "18:00"))
    engine.recompute()
    cf = engine.cashflows["coupon:2026-04-07"][-1]
    assert cf.state == "confirmed" and str(cf.amount) == "30000.00"
    return cf


class CorrectionUnpaidTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_correction_before_payment_replaces_version(self):
        engine = make_engine(self.tmp)
        _confirmed_coupon(engine)
        engine.set_clock(at("2026-04-08", "10:00"))
        engine.ingest_price(close("TST", "2026-04-07", "79.00", version=2,
                                  published="2026-04-08T09:00:00+08:00",
                                  corrects="P-TST-20260407-1"))
        obs = engine._latest_obs("TEST-1:scheduled:2026-04-07")
        self.assertEqual(obs.status, "confirmed")
        self.assertFalse(obs.outcomes["coupon_due"])
        cf = engine.cashflows["coupon:2026-04-07"][-1]
        self.assertEqual(cf.state, "cancelled", "未付现金流重算后不再应答应取消")
        adj_keys = [k for k in engine.cashflows if k.startswith("adj:")]
        self.assertEqual(adj_keys, [], "未付款项不得产生差额调整")

    def test_correction_same_outcome_keeps_amount(self):
        engine = make_engine(self.tmp)
        _confirmed_coupon(engine)
        engine.set_clock(at("2026-04-08", "10:00"))
        engine.ingest_price(close("TST", "2026-04-07", "84.00", version=2,
                                  published="2026-04-08T09:00:00+08:00",
                                  corrects="P-TST-20260407-1"))
        cf = engine.cashflows["coupon:2026-04-07"][-1]
        self.assertEqual(cf.state, "confirmed")
        self.assertEqual(str(cf.amount), "30000.00")
        self.assertEqual(cf.version, 2, "证据版本更新但金额不变")


class CorrectionPaidTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _paid_coupon(self, engine):
        _confirmed_coupon(engine)
        engine.set_clock(at("2026-04-10", "10:00"))
        engine.settle("TEST-1:coupon:2026-04-07", "PAY-1", actor="ops")

    def test_correction_after_payment_creates_difference_adjustment(self):
        engine = make_engine(self.tmp)
        self._paid_coupon(engine)
        engine.set_clock(at("2026-04-13", "10:00"))
        engine.ingest_price(close("TST", "2026-04-07", "79.00", version=2,
                                  published="2026-04-13T09:00:00+08:00",
                                  corrects="P-TST-20260407-1"))
        orig = engine.cashflows["coupon:2026-04-07"][-1]
        self.assertEqual(orig.state, "adjusted")
        self.assertEqual(str(orig.amount), "30000.00", "原付款金额永不改写")
        adj = engine.cashflows["adj:coupon:2026-04-07:1"][-1]
        self.assertEqual(adj.state, "confirmed")
        self.assertEqual(str(adj.amount), "-30000.00")
        self.assertEqual(adj.lineage["adjusts"], "coupon:2026-04-07")

    def test_adjustment_settles_independently(self):
        engine = make_engine(self.tmp)
        self._paid_coupon(engine)
        engine.set_clock(at("2026-04-13", "10:00"))
        engine.ingest_price(close("TST", "2026-04-07", "79.00", version=2,
                                  published="2026-04-13T09:00:00+08:00",
                                  corrects="P-TST-20260407-1"))
        st = engine.settle("TEST-1:adj:coupon:2026-04-07:1", "PAY-ADJ-1", actor="ops")
        self.assertEqual(str(st.amount), "-30000.00")
        orig = engine.cashflows["coupon:2026-04-07"][-1]
        self.assertEqual(orig.state, "adjusted", "调整链存续期间原记录保持 adjusted")


class RevocationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_revocation_returns_observation_to_pending(self):
        engine = make_engine(self.tmp)
        _confirmed_coupon(engine)
        engine.set_clock(at("2026-04-08", "10:00"))
        engine.ingest_price(close("TST", "2026-04-07", None, version=2,
                                  status="revoked",
                                  published="2026-04-08T09:00:00+08:00",
                                  corrects="P-TST-20260407-1"))
        obs = engine._latest_obs("TEST-1:scheduled:2026-04-07")
        self.assertEqual(obs.status, "pending")
        cf = engine.cashflows["coupon:2026-04-07"][-1]
        self.assertEqual(cf.state, "projected", "未付现金流随行情撤销回到待确认")
        # 补发官方价后重新确认
        engine.set_clock(at("2026-04-09", "10:00"))
        engine.ingest_price(close("TST", "2026-04-07", "90.00", version=3,
                                  published="2026-04-09T09:00:00+08:00"))
        obs = engine._latest_obs("TEST-1:scheduled:2026-04-07")
        self.assertEqual(obs.status, "confirmed")
        cf = engine.cashflows["coupon:2026-04-07"][-1]
        self.assertEqual(cf.state, "confirmed")
        self.assertEqual(str(cf.amount), "30000.00")

    def test_revocation_after_payment_flags_under_review(self):
        engine = make_engine(self.tmp)
        _confirmed_coupon(engine)
        engine.set_clock(at("2026-04-10", "10:00"))
        engine.settle("TEST-1:coupon:2026-04-07", "PAY-1", actor="ops")
        engine.set_clock(at("2026-04-13", "10:00"))
        engine.ingest_price(close("TST", "2026-04-07", None, version=2,
                                  status="revoked",
                                  published="2026-04-13T09:00:00+08:00",
                                  corrects="P-TST-20260407-1"))
        cf = engine.cashflows["coupon:2026-04-07"][-1]
        self.assertEqual(cf.state, "paid", "依据待复核期间不得改动已付状态")
        self.assertTrue(cf.lineage.get("under_review"))
        adj_keys = [k for k in engine.cashflows if k.startswith("adj:")]
        self.assertEqual(adj_keys, [], "依据未确认前不得产生差额调整")
        # 补发新官方价（结果不变）→ 复核解除且无调整
        engine.set_clock(at("2026-04-14", "10:00"))
        engine.ingest_price(close("TST", "2026-04-07", "90.00", version=3,
                                  published="2026-04-14T09:00:00+08:00"))
        cf = engine.cashflows["coupon:2026-04-07"][-1]
        self.assertFalse(cf.lineage.get("under_review"))
        self.assertEqual([k for k in engine.cashflows if k.startswith("adj:")], [])


if __name__ == "__main__":
    unittest.main()
