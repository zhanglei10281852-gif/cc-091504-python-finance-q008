"""结算幂等与冲突。"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from engine import ConflictError, ValidationError  # noqa: E402
from helpers import at, close, feed_daily, make_engine  # noqa: E402


class SettlementTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.engine = make_engine(self.tmp)
        feed_daily(self.engine, "TST", "2026-01-05", "2026-04-06", "90.00")
        self.engine.ingest_price(close("TST", "2026-04-07", "90.00"),
                                 auto_recompute=False)
        self.engine.set_clock(at("2026-04-07", "18:00"))
        self.engine.recompute()

    def test_same_instruction_replays_without_double_pay(self):
        self.engine.set_clock(at("2026-04-10", "10:00"))
        st1 = self.engine.settle("TEST-1:coupon:2026-04-07", "INS-1")
        st2 = self.engine.settle("TEST-1:coupon:2026-04-07", "INS-1")
        self.assertEqual(st1.instruction_id, st2.instruction_id)
        self.assertEqual(str(st1.amount), "30000.00")
        paid_events = [e for e in self.engine.store.events()
                       if e["type"] == "settlement_recorded"]
        self.assertEqual(len(paid_events), 1, "同一指令重试只记录一次结算")
        cf = self.engine.cashflows["coupon:2026-04-07"][-1]
        self.assertEqual(cf.state, "paid")

    def test_different_instruction_on_paid_conflicts(self):
        self.engine.set_clock(at("2026-04-10", "10:00"))
        self.engine.settle("TEST-1:coupon:2026-04-07", "INS-1")
        with self.assertRaises(ConflictError):
            self.engine.settle("TEST-1:coupon:2026-04-07", "INS-2")

    def test_instruction_cannot_be_reused_for_other_cashflow(self):
        self.engine.set_clock(at("2026-04-10", "10:00"))
        self.engine.settle("TEST-1:coupon:2026-04-07", "INS-1")
        with self.assertRaises(ConflictError):
            self.engine.settle("TEST-1:redemption:maturity", "INS-1")

    def test_settle_requires_confirmed_and_value_date(self):
        with self.assertRaises(ValidationError):
            # 当前为 confirmed，但付款日 2026-04-10 未到（时钟停在 4/7）
            self.engine.settle("TEST-1:coupon:2026-04-07", "INS-1")

    def test_settle_projected_rejected(self):
        engine = make_engine(tempfile.mkdtemp())  # 无行情 → 票息为 projected
        engine.set_clock(at("2026-04-07", "18:00"))
        engine.recompute()
        with self.assertRaises(ValidationError):
            engine.settle("TEST-1:coupon:2026-04-07", "INS-1")


if __name__ == "__main__":
    unittest.main()
