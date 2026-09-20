"""条款修订（版本化）、篮子成分变更、公司行动计算路径。"""
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from helpers import at, close, feed_daily, make_engine, make_terms  # noqa: E402


class TermsAmendmentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_observation_pins_terms_version(self):
        engine = make_engine(self.tmp)
        # 条款 v2：2026-04-07 起票息障碍 0.80 → 0.95
        v2 = make_terms()
        v2["barriers"] = {**v2["barriers"],
                          "coupon": {"ratio": "0.95", "observation": "scheduled"}}
        engine.register_terms(v2, effective_from=date(2026, 4, 7))
        feed_daily(engine, "TST", "2026-01-05", "2026-04-06", "90.00")
        engine.ingest_price(close("TST", "2026-04-07", "90.00"),
                            auto_recompute=False)
        engine.set_clock(at("2026-04-07", "18:00"))
        engine.recompute()
        obs = engine._latest_obs("TEST-1:scheduled:2026-04-07")
        self.assertEqual(obs.terms_version, 2, "观察日适用生效中的条款版本")
        self.assertFalse(obs.outcomes["coupon_due"], "0.90 < 0.95，票息不付")
        ki_day = engine._latest_obs("TEST-1:ki_daily:2026-04-06")
        self.assertEqual(ki_day.terms_version, 1, "4/6 的逐日观察仍适用 v1")

    def test_duplicate_registration_idempotent(self):
        engine = make_engine(self.tmp)
        terms = make_terms()
        again = engine.register_terms(terms, effective_from=date(2026, 1, 5), version=1)
        self.assertEqual(again.version, 1)
        self.assertEqual(len(engine.terms_versions), 1)


class BasketChangeTest(unittest.TestCase):
    def test_basket_change_leaves_calculation_path(self):
        tmp = tempfile.mkdtemp()
        engine = make_engine(tmp)
        # 条款 v2：2026-04-01 起篮子由 TST 换为 NEW（初始价 50）
        v2 = make_terms()
        v2["basket"] = [
            {"ticker": "NEW", "name": "新成分", "weight": "1", "initial_price": "50.00"},
        ]
        engine.register_terms(v2, effective_from=date(2026, 4, 1))
        feed_daily(engine, "TST", "2026-01-05", "2026-03-31", "90.00")
        feed_daily(engine, "NEW", "2026-04-01", "2026-04-06", "55.00")
        engine.ingest_price(close("NEW", "2026-04-07", "60.00"),
                            auto_recompute=False)
        engine.set_clock(at("2026-04-07", "18:00"))
        engine.recompute()
        old_day = engine._latest_obs("TEST-1:ki_daily:2026-03-31")
        self.assertIn("TST", old_day.evidence)
        self.assertEqual(old_day.terms_version, 1)
        new_day = engine._latest_obs("TEST-1:ki_daily:2026-04-01")
        self.assertIn("NEW", new_day.evidence)
        self.assertEqual(new_day.terms_version, 2)
        obs = engine._latest_obs("TEST-1:scheduled:2026-04-07")
        self.assertIn("NEW", obs.evidence)
        self.assertTrue(obs.outcomes["autocall"], "60/50 = 1.20 ≥ 1.05，应敲出")
        self.assertEqual(engine.status_info["status"], "called")


class CorporateActionTest(unittest.TestCase):
    def test_split_adjusts_initial_with_path(self):
        tmp = tempfile.mkdtemp()
        engine = make_engine(tmp)
        engine.apply_corporate_action({
            "ca_id": "CA-1", "underlying": "TST", "ex_date": "2026-03-10",
            "kind": "split", "factor": "0.5",
            "payload": {"ratio": "1:2"},
            "recorded_at": "2026-03-10T09:00:00+08:00",
        }, auto_recompute=False)
        before, path_before = engine._adjusted_initial("TST", date(2026, 3, 9))
        after, path_after = engine._adjusted_initial("TST", date(2026, 3, 10))
        self.assertEqual(str(before), "100.00")
        self.assertEqual(str(after), "50.000")
        self.assertEqual(path_before, [])
        self.assertEqual(len(path_after), 1)
        self.assertIn("split", path_after[0])
        self.assertIn("100.00" , path_after[0])


if __name__ == "__main__":
    unittest.main()
