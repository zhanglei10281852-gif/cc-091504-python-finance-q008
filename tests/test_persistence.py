"""事件日志持久化：重启回放幂等，历史版本链完整。"""
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from engine import ProductEngine  # noqa: E402
from helpers import PRECISION, at, close, feed_daily, make_terms  # noqa: E402
from calendars import BusinessCalendar  # noqa: E402
from store import EventStore  # noqa: E402


def _build(runtime: str) -> ProductEngine:
    engine = ProductEngine("TEST-1", EventStore(Path(runtime) / "events.jsonl"),
                           BusinessCalendar("CN"), PRECISION)
    engine.register_terms(make_terms(), effective_from=date(2026, 1, 5))
    return engine


class PersistenceTest(unittest.TestCase):
    def test_restart_replay_is_idempotent(self):
        runtime = tempfile.mkdtemp()
        engine = _build(runtime)
        feed_daily(engine, "TST", "2026-01-05", "2026-04-06", "90.00")
        engine.ingest_price(close("TST", "2026-04-07", "90.00"),
                            auto_recompute=False)
        engine.set_clock(at("2026-04-07", "18:00"))
        engine.recompute()
        engine.set_clock(at("2026-04-10", "10:00"))
        engine.settle("TEST-1:coupon:2026-04-07", "INS-1")
        engine.set_clock(at("2026-04-13", "10:00"))
        engine.ingest_price(close("TST", "2026-04-07", "79.00", version=2,
                                  published="2026-04-13T09:00:00+08:00",
                                  corrects="P-TST-20260407-1"))
        n_events = len(engine.store)

        # 重启：同一事件日志重建，重算不得追加新事件
        reloaded = _build(runtime)
        reloaded.set_clock(at("2026-04-13", "10:00"))
        reloaded.recompute()
        self.assertEqual(len(reloaded.store), n_events)

        cf = reloaded.cashflows["coupon:2026-04-07"][-1]
        self.assertEqual(cf.state, "adjusted")
        self.assertEqual(str(cf.amount), "30000.00")
        adj = reloaded.cashflows["adj:coupon:2026-04-07:1"][-1]
        self.assertEqual(str(adj.amount), "-30000.00")
        states = [v.state for v in reloaded.cashflow_history("TEST-1:coupon:2026-04-07")]
        self.assertEqual(states, ["confirmed", "paid", "adjusted"],
                         "版本链在重启后完整保留")
        # 结算记录也在回放中恢复，重复指令仍返回原结果
        st = reloaded.settle("TEST-1:coupon:2026-04-07", "INS-1")
        self.assertEqual(st.result, "paid")


if __name__ == "__main__":
    unittest.main()
