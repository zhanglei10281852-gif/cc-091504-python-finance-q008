from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from engine import Engine, EngineError, Store  # noqa: E402
from engine.report import payment_explanation, product_view  # noqa: E402

HOLIDAYS = ["2026-04-06", "2026-10-05", "2026-10-06", "2026-10-07", "2027-01-01"]

TERMS = {
    "notional": "1000000",
    "issue_date": "2026-01-05",
    "maturity_date": "2027-01-05",
    "basket": [
        {"underlying": "AAA", "initial_price": "10.0000"},
        {"underlying": "BBB", "initial_price": "20.0000"},
    ],
    "knock_out_barrier": "1.0000",
    "knock_in_barrier": "0.7500",
    "coupon_barrier": "0.8500",
    "coupon_rate_per_period": "0.0125",
    "put_strike": "0.7500",
    "autocall_dates": ["2026-04-06", "2026-07-06", "2026-10-05", "2027-01-05"],
}


def make_engine(tmp: str, terms: dict | None = None, product_id: str = "P1") -> Engine:
    engine = Engine(Store(tmp))
    engine.set_holidays("CN-SSE", HOLIDAYS)
    engine.issue_terms(product_id, terms or TERMS, "2025-12-28")
    return engine


def feed(engine: Engine, underlying: str, date: str, price: str, status: str = "official"):
    return engine.ingest_price(underlying, date, price=price, status=status, source="test")


class ObservationTest(unittest.TestCase):
    def test_suspension_postpones_then_late_close_confirms_autocall(self):
        """停牌不静默跳过：先顺延留痕，补出官方收盘价后确认敲出。"""
        with tempfile.TemporaryDirectory() as tmp:
            engine = make_engine(tmp)
            # 第一期票息观察（04-06 节假日顺延到 04-07），票息条件不满足 → 记忆 1
            feed(engine, "AAA", "2026-04-07", "9.8000")
            feed(engine, "BBB", "2026-04-07", "16.4000")
            obs1 = engine.run_observation("P1", "2026-04-06", as_of="2026-04-07")
            self.assertEqual("confirmed", obs1["state"])
            self.assertEqual("2026-04-07", obs1["actual_date"])
            self.assertEqual("2026-04-06", obs1["rolled_from"])
            self.assertFalse(obs1["decision"]["coupon_ok"])
            self.assertEqual(1, engine.product_state("P1")["memory_count"])

            # 07-06：AAA 正常，BBB 停牌 → 观察不顺延跳过，进入顺延待办
            feed(engine, "AAA", "2026-07-06", "10.3000")
            engine.record_suspension("BBB", "2026-07-06", note="盘中停牌")
            obs2 = engine.run_observation("P1", "2026-07-06", as_of="2026-07-06")
            self.assertEqual("postponed", obs2["state"])
            self.assertEqual("2026-07-07", obs2["next_attempt"])
            bbb_ev = next(e for e in obs2["evidence"] if e["underlying"] == "BBB")
            self.assertEqual("suspended", bbb_ev["price_status"])
            self.assertFalse(engine.product_state("P1")["autocalled"])

            # 当晚补出官方收盘价 → 自动重算确认，敲出
            feed(engine, "BBB", "2026-07-06", "20.6000")
            obs3 = engine.observations("P1")
            auto = next(o for o in obs3 if o["sched_date"] == "2026-07-06" and o["kind"] == "autocall")
            self.assertEqual("confirmed", auto["state"])
            self.assertEqual(2, auto["version"])
            self.assertEqual("2026-07-06", auto["actual_date"])
            self.assertTrue(auto["decision"]["knock_out"])
            self.assertEqual("1.030000", auto["worst_of"]["performance"])
            self.assertTrue(engine.product_state("P1")["autocalled"])

            # 赎回 1,000,000 + 票息 2 期（含记忆）25,000
            flows = {c["cashflow_id"]: c for c in engine.cashflows("P1")}
            self.assertEqual("1000000.00", flows["cf-P1-redemption-2"]["amount"])
            self.assertEqual("confirmed", flows["cf-P1-redemption-2"]["state"])
            self.assertEqual("25000.00", flows["cf-P1-coupon-2"]["amount"])

            # 历史版本保留，可复原顺延过程
            versions = engine.observation_versions("P1", "2026-07-06", "autocall")
            self.assertEqual(["postponed", "confirmed"], [v["state"] for v in versions])

    def test_provisional_price_only_pending(self):
        """临时价格只能进入待确认，不产生现金流。"""
        with tempfile.TemporaryDirectory() as tmp:
            engine = make_engine(tmp)
            feed(engine, "AAA", "2026-07-06", "10.3000")
            feed(engine, "BBB", "2026-07-06", "20.6000", status="provisional")
            obs = engine.run_observation("P1", "2026-07-06", as_of="2026-07-06")
            self.assertEqual("pending_confirmation", obs["state"])
            self.assertTrue(obs["decision"]["tentative"])
            self.assertFalse(engine.product_state("P1")["autocalled"])
            states = {c["cashflow_id"]: c["state"] for c in engine.cashflows("P1")}
            self.assertEqual("projected", states["cf-P1-redemption-4"])

            feed(engine, "BBB", "2026-07-06", "20.6000", status="official")
            auto = next(
                o for o in engine.observations("P1") if o["sched_date"] == "2026-07-06" and o["kind"] == "autocall"
            )
            self.assertEqual("confirmed", auto["state"])
            self.assertTrue(engine.product_state("P1")["autocalled"])

    def test_missing_price_beyond_max_days_unresolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = make_engine(tmp)
            feed(engine, "AAA", "2026-07-06", "10.3000")
            obs = engine.run_observation("P1", "2026-07-06", as_of="2026-08-31")
            self.assertEqual("unresolved", obs["state"])

    def test_process_due_idempotent_no_version_spam(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = make_engine(tmp)
            feed(engine, "AAA", "2026-07-06", "10.3000")
            feed(engine, "BBB", "2026-07-06", "20.6000")
            engine.process_due("P1", "2026-07-06")
            count = len(engine.store.col("observations"))
            engine.process_due("P1", "2026-07-06")
            self.assertEqual(count, len(engine.store.col("observations")))


class CorrectionTest(unittest.TestCase):
    def _autocalled_engine(self, tmp: str) -> Engine:
        engine = make_engine(tmp)
        feed(engine, "AAA", "2026-04-07", "9.8000")
        feed(engine, "BBB", "2026-04-07", "16.4000")
        engine.run_observation("P1", "2026-04-06", as_of="2026-04-07")
        feed(engine, "AAA", "2026-07-06", "10.3000")
        feed(engine, "BBB", "2026-07-06", "20.6000")
        engine.run_observation("P1", "2026-07-06", as_of="2026-07-06")
        return engine

    def test_correction_recomputes_unpaid_cashflows(self):
        """发行人更正 → 未支付现金流以新版本重算，旧版本保留。"""
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._autocalled_engine(tmp)
            self.assertTrue(engine.product_state("P1")["autocalled"])
            # 更正：BBB 收盘价 20.60 → 19.90（0.995 < 敲出 1.00）→ 赎回不成立
            engine.correct_price("BBB", "2026-07-06", "reissue", new_price="19.9000", reason="交易所更正")
            self.assertFalse(engine.product_state("P1")["autocalled"])
            flows = {c["cashflow_id"]: c for c in engine.cashflows("P1")}
            self.assertEqual("cancelled", flows["cf-P1-redemption-2"]["state"])
            # 票息仍成立（0.995 ≥ 0.85），金额不变
            self.assertEqual("25000.00", flows["cf-P1-coupon-2"]["amount"])
            self.assertEqual("confirmed", flows["cf-P1-coupon-2"]["state"])
            # 产品恢复存续，重新生成预计现金流
            projected = [c for c in engine.cashflows("P1") if c["state"] == "projected"]
            self.assertTrue(any(c["cashflow_id"] == "cf-P1-coupon-3" for c in projected))
            # 赎回旧版本链保留
            versions = engine.cashflow_versions("cf-P1-redemption-2")
            self.assertEqual(["confirmed", "cancelled"], [v["state"] for v in versions])

    def test_correction_on_paid_cashflow_creates_difference_adjustment(self):
        """已付款项目不抹去：原记录保留，差额以调整单体现。"""
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._autocalled_engine(tmp)
            st1 = engine.settle("key-redemption", "cf-P1-redemption-2")
            st2 = engine.settle("key-coupon", "cf-P1-coupon-2")
            self.assertEqual("1000000.00", st1["amount"])
            self.assertEqual("25000.00", st2["amount"])

            engine.correct_price("BBB", "2026-07-06", "reissue", new_price="19.9000", reason="发行人更正")

            flows = {c["cashflow_id"]: c for c in engine.cashflows("P1")}
            redemption = flows["cf-P1-redemption-2"]
            self.assertEqual("adjusted", redemption["state"])
            self.assertEqual("1000000.00", redemption["amount"])  # 原付款金额不抹去
            adjustments = [c for c in engine.cashflows("P1") if c["kind"] == "adjustment"]
            self.assertEqual(1, len(adjustments))
            adj = adjustments[0]
            self.assertEqual("-1000000.00", adj["amount"])
            self.assertEqual("cf-P1-redemption-2", adj["adjusts"])
            self.assertEqual("confirmed", adj["state"])
            # 票息已付且金额不变 → 无调整
            self.assertEqual("paid", flows["cf-P1-coupon-2"]["state"])

            # 差额调整本身也可结算（追回方向）
            st3 = engine.settle("key-adj", adj["cashflow_id"])
            self.assertEqual("collect", st3["direction"])
            self.assertEqual("-1000000.00", st3["amount"])

    def test_repeated_refresh_keeps_single_adjustment(self):
        """差额调整对账幂等：后续观察刷新不产生调整单抖动。"""
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._autocalled_engine(tmp)
            engine.settle("key-redemption", "cf-P1-redemption-2")
            engine.correct_price("BBB", "2026-07-06", "reissue", new_price="19.9000", reason="更正")
            adjs1 = [c for c in engine.cashflows("P1") if c["kind"] == "adjustment"]
            self.assertEqual(1, len(adjs1))
            feed(engine, "AAA", "2026-07-07", "10.3000")
            feed(engine, "BBB", "2026-07-07", "19.9500")
            engine.run_observation("P1", "2026-07-07", kind="ki_daily", as_of="2026-07-07")
            adjs2 = [c for c in engine.cashflows("P1") if c["kind"] == "adjustment"]
            self.assertEqual(1, len(adjs2))
            self.assertEqual(adjs1[0]["cashflow_id"], adjs2[0]["cashflow_id"])
            self.assertEqual("-1000000.00", adjs2[0]["amount"])
            all_versions = [c for c in engine.store.col("cashflows") if c["kind"] == "adjustment"]
            self.assertEqual(1, len(all_versions))

    def test_cancel_price_downgrades_unpaid_cashflow(self):
        """撤销行情 → 证据失效，未付确认现金流退回预计。"""
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._autocalled_engine(tmp)
            engine.correct_price("BBB", "2026-07-06", "cancel", reason="行情撤销")
            self.assertFalse(engine.product_state("P1")["autocalled"])
            auto = next(
                o for o in engine.observations("P1") if o["sched_date"] == "2026-07-06" and o["kind"] == "autocall"
            )
            self.assertEqual("unresolved", auto["state"])
            flows = {c["cashflow_id"]: c for c in engine.cashflows("P1")}
            self.assertEqual("projected", flows["cf-P1-coupon-2"]["state"])
            self.assertEqual("cancelled", flows["cf-P1-redemption-2"]["state"])


class SettlementTest(unittest.TestCase):
    def test_settlement_retry_never_double_pays(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = make_engine(tmp)
            feed(engine, "AAA", "2026-07-06", "10.3000")
            feed(engine, "BBB", "2026-07-06", "20.6000")
            engine.run_observation("P1", "2026-07-06", as_of="2026-07-06")

            first = engine.settle("same-key", "cf-P1-redemption-2")
            replay = engine.settle("same-key", "cf-P1-redemption-2")
            self.assertEqual(first["settlement_id"], replay["settlement_id"])
            self.assertEqual(1, len(engine.settlements("P1")))
            self.assertEqual("paid", engine.cashflow_versions("cf-P1-redemption-2")[-1]["state"])

            with self.assertRaises(EngineError) as ctx:
                engine.settle("same-key", "cf-P1-coupon-1")
            self.assertEqual("settlement_conflict", ctx.exception.code)
            self.assertEqual(409, ctx.exception.status)

    def test_unconfirmed_cashflow_not_settleable(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = make_engine(tmp)
            with self.assertRaises(EngineError) as ctx:
                engine.settle("k", "cf-P1-redemption-4")
            self.assertEqual("not_settleable", ctx.exception.code)


class TermsAndCalendarTest(unittest.TestCase):
    def test_corporate_action_adjusts_performance(self):
        """公司行动留下计算路径：证据含调整因子与调整后期初价。"""
        with tempfile.TemporaryDirectory() as tmp:
            engine = make_engine(tmp)
            engine.apply_corporate_action("AAA", "2026-05-20", "split", "0.5", note="2拆1")
            feed(engine, "AAA", "2026-07-06", "5.1500")
            feed(engine, "BBB", "2026-07-06", "20.6000")
            obs = engine.run_observation("P1", "2026-07-06", as_of="2026-07-06")
            aaa = next(e for e in obs["evidence"] if e["underlying"] == "AAA")
            self.assertEqual("0.5", aaa["factor"])
            self.assertEqual("5.0000", aaa["initial_adj"])
            self.assertEqual("1.030000", aaa["performance"])
            self.assertTrue(obs["decision"]["knock_out"])

    def test_basket_change_pins_terms_by_observation_date(self):
        """篮子变更：变更前后观察各自固定适用条款版本。"""
        with tempfile.TemporaryDirectory() as tmp:
            engine = make_engine(tmp)
            feed(engine, "AAA", "2026-04-07", "9.8000")
            feed(engine, "BBB", "2026-04-07", "17.0000")
            obs1 = engine.run_observation("P1", "2026-04-06", as_of="2026-04-07")
            self.assertEqual({"AAA", "BBB"}, {e["underlying"] for e in obs1["evidence"]})

            new_terms = dict(TERMS)
            new_terms["basket"] = [
                {"underlying": "AAA", "initial_price": "10.0000"},
                {"underlying": "CCC", "initial_price": "25.0000"},
            ]
            engine.issue_terms("P1", new_terms, "2026-06-01", reason="basket_change")

            feed(engine, "AAA", "2026-07-06", "10.3000")
            feed(engine, "CCC", "2026-07-06", "25.7500")
            obs2 = engine.run_observation("P1", "2026-07-06", as_of="2026-07-06")
            self.assertEqual({"AAA", "CCC"}, {e["underlying"] for e in obs2["evidence"]})
            self.assertEqual(2, obs2["terms_version"])

            # 重算旧观察点仍固定旧条款版本
            engine.correct_price("BBB", "2026-04-07", "reissue", new_price="17.0000", reason="重发同价")
            obs1_versions = engine.observation_versions("P1", "2026-04-06", "autocall")
            self.assertEqual(2, len(obs1_versions))
            self.assertTrue(all(v["terms_version"] == 1 for v in obs1_versions))

    def test_holiday_roll_records_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = make_engine(tmp)
            feed(engine, "AAA", "2026-10-08", "10.3000")
            feed(engine, "BBB", "2026-10-08", "20.6000")
            obs = engine.run_observation("P1", "2026-10-05", as_of="2026-10-08")
            self.assertEqual("2026-10-08", obs["actual_date"])
            self.assertEqual("2026-10-05", obs["rolled_from"])
            self.assertEqual("holiday", obs["roll_reason"])


class LifecycleTest(unittest.TestCase):
    def test_memory_coupon_accumulates_and_pays(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = make_engine(tmp)
            # 第 1 期：worst 0.82 < 0.85 → 记忆
            feed(engine, "AAA", "2026-04-07", "9.8000")
            feed(engine, "BBB", "2026-04-07", "16.4000")
            engine.run_observation("P1", "2026-04-06", as_of="2026-04-07")
            self.assertEqual(1, engine.product_state("P1")["memory_count"])
            self.assertIsNone(
                next((c for c in engine.cashflows("P1") if c["cashflow_id"] == "cf-P1-coupon-1" and c["state"] == "confirmed"), None)
            )
            # 第 2 期：worst 1.03 ≥ 0.85 但未敲出？1.03 ≥ 1.00 会敲出——用低于敲出的价格
            feed(engine, "AAA", "2026-07-06", "9.9000")
            feed(engine, "BBB", "2026-07-06", "17.6000")  # worst 0.88
            engine.run_observation("P1", "2026-07-06", as_of="2026-07-06")
            self.assertEqual(0, engine.product_state("P1")["memory_count"])
            coupon = next(c for c in engine.cashflows("P1") if c["cashflow_id"] == "cf-P1-coupon-2")
            self.assertEqual("25000.00", coupon["amount"])  # 2 期一并支付
            self.assertEqual("confirmed", coupon["state"])
            self.assertFalse(engine.product_state("P1")["autocalled"])

    def test_knock_in_latches_and_maturity_cash_loss(self):
        terms = dict(TERMS)
        terms["autocall_dates"] = ["2026-04-06", "2026-07-06"]
        terms["maturity_date"] = "2026-07-06"
        with tempfile.TemporaryDirectory() as tmp:
            engine = make_engine(tmp, terms=terms)
            # 逐日敲入观察：BBB 跌破 0.75
            feed(engine, "AAA", "2026-03-10", "9.9000")
            feed(engine, "BBB", "2026-03-10", "14.4000")  # 0.72
            ki = engine.run_observation("P1", "2026-03-10", kind="ki_daily", as_of="2026-03-10")
            self.assertTrue(ki["decision"]["knock_in"])
            self.assertTrue(engine.product_state("P1")["knocked_in"])
            self.assertEqual("2026-03-10", engine.product_state("P1")["knock_in_date"])
            # 到期：worst 0.70 < 0.75 → 现金损失 1,000,000 × 0.70/0.75
            feed(engine, "AAA", "2026-07-06", "9.6000")
            feed(engine, "BBB", "2026-07-06", "14.0000")
            final = engine.run_observation("P1", "2026-07-06", as_of="2026-07-06")
            self.assertEqual("final", final["kind"])
            self.assertTrue(engine.product_state("P1")["matured"])
            redemption = next(c for c in engine.cashflows("P1") if c["cashflow_id"] == "cf-P1-redemption-2")
            self.assertEqual("933333.33", redemption["amount"])
            self.assertEqual("confirmed", redemption["state"])

    def test_physical_delivery_at_maturity(self):
        terms = dict(TERMS)
        terms["autocall_dates"] = ["2026-07-06"]
        terms["maturity_date"] = "2026-07-06"
        terms["delivery"] = "physical"
        with tempfile.TemporaryDirectory() as tmp:
            engine = make_engine(tmp, terms=terms)
            feed(engine, "AAA", "2026-03-10", "9.9000")
            feed(engine, "BBB", "2026-03-10", "14.0000")  # 敲入
            engine.run_observation("P1", "2026-03-10", kind="ki_daily", as_of="2026-03-10")
            feed(engine, "AAA", "2026-07-06", "9.6000")
            feed(engine, "BBB", "2026-07-06", "12.0000")  # worst 0.60
            engine.run_observation("P1", "2026-07-06", as_of="2026-07-06")
            delivery = next(c for c in engine.cashflows("P1") if c["kind"] == "delivery")
            self.assertEqual("BBB", delivery["underlying"])
            self.assertEqual(66666, delivery["quantity"])
            self.assertEqual("10.00", delivery["amount"])  # 零股现金找零


class ReportTest(unittest.TestCase):
    def test_product_view_and_payment_explanation(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = make_engine(tmp)
            feed(engine, "AAA", "2026-04-07", "9.8000")
            feed(engine, "BBB", "2026-04-07", "16.4000")
            engine.run_observation("P1", "2026-04-06", as_of="2026-04-07")
            feed(engine, "AAA", "2026-07-06", "10.3000")
            engine.record_suspension("BBB", "2026-07-06")
            engine.run_observation("P1", "2026-07-06", as_of="2026-07-06")
            feed(engine, "BBB", "2026-07-06", "20.6000")
            engine.settle("k1", "cf-P1-redemption-2")
            engine.correct_price("BBB", "2026-07-06", "reissue", new_price="19.9000", reason="更正")

            view = product_view(engine, "P1", as_of="2026-09-20")
            self.assertEqual("P1", view["product_id"])
            self.assertFalse(view["state"]["autocalled"])
            self.assertEqual("2026-10-05", view["next_observation"]["sched_date"])
            self.assertEqual("2026-10-08", view["next_observation"]["actual_date"])
            self.assertTrue(view["daily_evidence"])
            auto_ev = next(
                o for o in view["daily_evidence"] if o["sched_date"] == "2026-07-06" and o["kind"] == "autocall"
            )
            self.assertEqual("confirmed", auto_ev["state"])
            self.assertEqual("0.995000", auto_ev["worst_of"]["performance"])
            self.assertIsNotNone(view["barriers"]["latest_worst_of"])
            self.assertTrue(view["cashflows"]["projected"])
            self.assertEqual(1, len(view["cashflows"]["adjustments"]))

            expl = payment_explanation(engine, "cf-P1-redemption-2")
            self.assertEqual("adjusted", expl["current"]["state"])
            self.assertIsNotNone(expl["settlement"])
            self.assertEqual("1000000.00", expl["settlement"]["amount"])
            self.assertIsNotNone(expl["observation"])
            self.assertEqual(2, expl["observation"]["version"])  # 付款依据：20.60 那一版
            self.assertTrue(expl["basis"]["steps"])
            self.assertEqual(1, len(expl["corrections"]))  # 更正后的第三版
            self.assertEqual(1, len(expl["adjustments"]))
            self.assertEqual("-1000000.00", expl["adjustments"][0]["amount"])


if __name__ == "__main__":
    unittest.main()
