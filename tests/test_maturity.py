"""到期交付：现金结算（敲入损失 / 保本）与实物交付。"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from helpers import at, close, feed_daily, make_engine, make_terms  # noqa: E402


class MaturityCashTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_knocked_in_cash_loss(self):
        engine = make_engine(self.tmp)
        feed_daily(engine, "TST", "2026-01-05", "2026-02-09", "90.00")
        engine.ingest_price(close("TST", "2026-02-10", "69.00"),
                            auto_recompute=False)  # 敲入
        feed_daily(engine, "TST", "2026-02-11", "2026-07-03", "85.00")
        engine.ingest_price(close("TST", "2026-07-06", "80.00"),
                            auto_recompute=False)  # 期末 80 < 100
        engine.set_clock(at("2026-07-06", "18:00"))
        engine.recompute()
        self.assertEqual(engine.status_info["status"], "matured")
        redemption = engine.cashflows["redemption:maturity"][-1]
        self.assertEqual(redemption.state, "confirmed")
        self.assertEqual(str(redemption.amount), "800000.00",
                         "敲入且期末低于初始价：本金 × 0.80")
        self.assertTrue(any("现金结算" in line
                            for line in redemption.lineage["calc_path"]))

    def test_not_knocked_in_full_protection(self):
        engine = make_engine(self.tmp)
        feed_daily(engine, "TST", "2026-01-05", "2026-07-03", "85.00")
        engine.ingest_price(close("TST", "2026-07-06", "80.00"),
                            auto_recompute=False)
        engine.set_clock(at("2026-07-06", "18:00"))
        engine.recompute()
        redemption = engine.cashflows["redemption:maturity"][-1]
        self.assertEqual(str(redemption.amount), "1000000.00",
                         "未敲入：按保护比例兑付全部本金")

    def test_knocked_in_but_recovered_still_protected(self):
        engine = make_engine(self.tmp)
        feed_daily(engine, "TST", "2026-01-05", "2026-02-09", "90.00")
        engine.ingest_price(close("TST", "2026-02-10", "69.00"),
                            auto_recompute=False)  # 敲入
        feed_daily(engine, "TST", "2026-02-11", "2026-07-03", "95.00")
        engine.ingest_price(close("TST", "2026-07-06", "102.00"),
                            auto_recompute=False)  # 期末回到初始价之上
        engine.set_clock(at("2026-07-06", "18:00"))
        engine.recompute()
        redemption = engine.cashflows["redemption:maturity"][-1]
        self.assertEqual(str(redemption.amount), "1000000.00")


class MaturityPhysicalTest(unittest.TestCase):
    def test_physical_delivery_with_cash_in_lieu(self):
        tmp = tempfile.mkdtemp()
        terms = make_terms(
            notional="990050.00",
            maturity={
                "knocked_in_settlement": "physical",
                "lot_size": 100,
                "protection_ratio": "1.00",
            },
        )
        engine = make_engine(tmp, terms=terms)
        feed_daily(engine, "TST", "2026-01-05", "2026-02-09", "90.00")
        engine.ingest_price(close("TST", "2026-02-10", "69.00"),
                            auto_recompute=False)  # 敲入
        feed_daily(engine, "TST", "2026-02-11", "2026-07-03", "85.00")
        engine.ingest_price(close("TST", "2026-07-06", "80.00"),
                            auto_recompute=False)
        engine.set_clock(at("2026-07-06", "18:00"))
        engine.recompute()
        delivery = engine.cashflows["delivery:maturity"][-1]
        self.assertEqual(delivery.type, "delivery")
        # 理论股数 990050/100 = 9900.5 → 整股 9900，零股 0.5 × 80 = 40.00
        self.assertEqual(delivery.delivery["shares"], 9900)
        self.assertEqual(delivery.delivery["ticker"], "TST")
        self.assertEqual(str(delivery.amount), "40.00")
        self.assertTrue(any("实物交付" in line
                            for line in delivery.lineage["calc_path"]))


if __name__ == "__main__":
    unittest.main()
