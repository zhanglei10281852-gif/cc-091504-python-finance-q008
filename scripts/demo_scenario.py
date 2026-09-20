"""端到端演示：停牌补价 → 确认付款 → 行情更正 → 差额调整 → 公司行动 → 撤销补发。

复现运营争议场景：标的当天先停牌后补出官方收盘价。旧系统按缺价跳过观察，
本引擎将观察挂起为 pending，待官方价补发后回填确认；已付款项在更正后
以差额调整承接，原付款记录永不抹除。

运行：python3 scripts/demo_scenario.py [--runtime-dir .runtime/demo]
退出码：0 = 全部断言通过；1 = 存在不一致。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from app import Registry  # noqa: E402
from engine import ConflictError  # noqa: E402

TZ8 = timezone(timedelta(hours=8))
PRODUCT = "NOTE-A001"

CHECKS: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((label, ok, detail))
    mark = "✓" if ok else "✗"
    print(f"  [{mark}] {label}" + (f" —— {detail}" if detail else ""))


def ts(day: str, hm: str = "09:00") -> datetime:
    return datetime.fromisoformat(f"{day}T{hm}:00+08:00")


def price(pid: str, underlying: str, day: str, value, status: str, version: int,
          published: str, source: str = "exchange", corrects=None) -> dict:
    return {
        "price_id": pid, "underlying": underlying, "date": day,
        "value": value, "price_type": "official_close", "status": status,
        "version": version, "published_at": published, "source": source,
        "corrects": corrects,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-dir", default=str(ROOT / ".runtime" / "demo"))
    args = parser.parse_args()
    runtime_dir = Path(args.runtime_dir)
    if runtime_dir.exists():
        shutil.rmtree(runtime_dir)

    registry = Registry(runtime_dir=runtime_dir)
    engine = registry.get_engine(PRODUCT)

    print("=" * 72)
    print("第 1 幕  2026-06-19 观察日：300750.SZ 盘中停牌，仅有临时价")
    print("=" * 72)
    engine.set_clock(ts("2026-06-19", "18:05"))
    engine.ingest_price(price(
        "P-300750.SZ-20260619-prov1", "300750.SZ", "2026-06-19", "194.50",
        "provisional", 1, "2026-06-19T16:35:00+08:00", source="vendor"))
    obs = engine._latest_obs(f"{PRODUCT}:scheduled:2026-06-19")
    check("观察未被跳过，状态为 pending（旧系统按缺价跳过）",
          obs is not None and obs.status == "pending",
          "；".join(obs.pending_reasons) if obs else "无观察记录")
    coupon = engine.cashflows["coupon:2026-06-19"][-1]
    check("票息现金流为 projected（临时价只能进入待确认）",
          coupon.state == "projected" and str(coupon.amount) == "20000.00",
          f"state={coupon.state} amount={coupon.amount}")

    print()
    print("=" * 72)
    print("第 2 幕  2026-06-22：交易所补发 6/19 官方收盘价 195.00")
    print("=" * 72)
    engine.set_clock(ts("2026-06-22", "09:35"))
    engine.ingest_price(price(
        "P-300750.SZ-20260619-2", "300750.SZ", "2026-06-19", "195.00",
        "official", 2, "2026-06-22T09:05:00+08:00"))
    obs = engine._latest_obs(f"{PRODUCT}:scheduled:2026-06-19")
    check("观察确认（官方价补发后重算为新版本），worst_of = 195/220 = 0.88636364 ≥ 0.85",
          obs.status == "confirmed" and obs.outcomes["coupon_due"] is True
          and obs.outcomes["worst_of"]["ratio"] == "0.88636364",
          f"version={obs.version} worst={obs.outcomes['worst_of']}")
    coupon = engine.cashflows["coupon:2026-06-19"][-1]
    check("票息确认 20000.00，付款日 2026-06-26",
          coupon.state == "confirmed" and str(coupon.amount) == "20000.00"
          and coupon.value_date.isoformat() == "2026-06-26")

    print()
    print("=" * 72)
    print("第 3 幕  2026-06-26：票息结算（幂等指令）")
    print("=" * 72)
    engine.set_clock(ts("2026-06-26", "10:00"))
    st1 = engine.settle(f"{PRODUCT}:coupon:2026-06-19", "PAY-A001-Q1-001", actor="ops")
    st2 = engine.settle(f"{PRODUCT}:coupon:2026-06-19", "PAY-A001-Q1-001", actor="ops")
    check("同一结算指令重试返回同一结果，不重复付款",
          st1.instruction_id == st2.instruction_id and str(st2.amount) == "20000.00"
          and engine.cashflows["coupon:2026-06-19"][-1].state == "paid")
    try:
        engine.settle(f"{PRODUCT}:coupon:2026-06-19", "PAY-A001-Q1-002", actor="ops")
        check("不同指令号对已付款现金流结算被拒绝", False, "未抛出冲突")
    except ConflictError as exc:
        check("不同指令号对已付款现金流结算被拒绝", True, str(exc))

    print()
    print("=" * 72)
    print("第 4 幕  2026-07-02：交易所更正 6/19 官方收盘价 195.00 → 184.00")
    print("=" * 72)
    engine.set_clock(ts("2026-07-02", "10:30"))
    engine.ingest_price(price(
        "P-300750.SZ-20260619-3", "300750.SZ", "2026-06-19", "184.00",
        "official", 3, "2026-07-02T10:00:00+08:00",
        corrects="P-300750.SZ-20260619-2"))
    obs = engine._latest_obs(f"{PRODUCT}:scheduled:2026-06-19")
    check("观察重算为新版本，worst_of = 184/220 = 0.83636364 < 0.85，票息不再应付",
          obs.status == "confirmed" and obs.outcomes["coupon_due"] is False
          and obs.outcomes["worst_of"]["ratio"] == "0.83636364",
          f"version={obs.version}")
    orig = engine.cashflows["coupon:2026-06-19"][-1]
    adj = engine.cashflows["adj:coupon:2026-06-19:1"][-1]
    check("原付款 20000.00 保留并标记 adjusted（不抹除）",
          orig.state == "adjusted" and str(orig.amount) == "20000.00")
    check("生成差额调整 -20000.00（confirmed，待结算收回）",
          adj.state == "confirmed" and str(adj.amount) == "-20000.00",
          f"key={adj.key}")
    proj = engine.cashflows["coupon:2026-09-19"][-1]
    check("Q2 票息预计含记忆补付：20000 × 2 = 40000.00",
          proj.state == "projected" and str(proj.amount) == "40000.00")

    print()
    print("=" * 72)
    print("第 5 幕  2026-08-10：300750.SZ 除权 1 拆 2（公司行动留痕）")
    print("=" * 72)
    engine.set_clock(ts("2026-08-10", "09:00"))
    # 与 reference/corporate-actions 种子一致：幂等回放，重复公告不产生第二份事件
    engine.apply_corporate_action({
        "ca_id": "CA-300750-20260810", "underlying": "300750.SZ",
        "ex_date": "2026-08-10", "kind": "split", "factor": "0.5",
        "payload": {"ratio": "1:2", "note": "每 1 股拆为 2 股"},
        "recorded_at": "2026-08-03T09:00:00+08:00",
    })
    from decimal import Decimal
    adj_init, path = engine._adjusted_initial("300750.SZ", engine.clock.date())
    check("除权后初始价 220 → 110，计算路径留痕",
          adj_init == Decimal("110"), "；".join(path))
    ki_obs = engine._latest_obs(f"{PRODUCT}:ki_daily:2026-08-10")
    level = Decimal(ki_obs.evidence["300750.SZ"]["barrier_levels"]["knock_in"])
    check("8/10 敲入位同步调整为 110 × 0.70 = 77", level == Decimal("77"),
          f"level={level}")

    print()
    print("=" * 72)
    print("第 6 幕  2026-09-16/18：行情撤销与补发（未付现金流随新版本重算）")
    print("=" * 72)
    engine.set_clock(ts("2026-09-16", "20:10"))
    engine.ingest_price(price(
        "P-600519.SH-20260916-rev", "600519.SH", "2026-09-16", None,
        "revoked", 2, "2026-09-16T20:05:00+08:00",
        corrects="P-600519.SH-20260916-1"))
    ki_obs = engine._latest_obs(f"{PRODUCT}:ki_daily:2026-09-16")
    check("9/16 官方价被撤销 → 当日敲入观察回到 pending",
          ki_obs.status == "pending", "；".join(ki_obs.pending_reasons))
    engine.set_clock(ts("2026-09-18", "09:35"))
    engine.ingest_price(price(
        "P-600519.SH-20260916-3", "600519.SH", "2026-09-16", "1730.00",
        "official", 3, "2026-09-18T09:05:00+08:00"))
    ki_obs = engine._latest_obs(f"{PRODUCT}:ki_daily:2026-09-16")
    check("9/18 补发官方价 → 当日观察重新确认",
          ki_obs.status == "confirmed" and ki_obs.version >= 3)

    print()
    print("=" * 72)
    print("第 7 幕  2026-09-20（今日）：运营视图与争议复原")
    print("=" * 72)
    engine.set_clock(ts("2026-09-20", "09:00"))
    engine.recompute()

    from queries import operations_view, explain_cashflow
    view = operations_view(engine)
    nxt = view["next_observation"]
    check("下一观察日：原定 2026-09-19（周六）顺延至 2026-09-21",
          nxt is not None and nxt["scheduled_date"] == "2026-09-19"
          and nxt["date"] == "2026-09-21" and nxt["status"] == "scheduled",
          "；".join(nxt["roll_path"]) if nxt else "无")
    check("敲入未触发，无待确认交易日",
          view["status"]["ki"]["triggered"] is False
          and view["status"]["ki"]["pending_days"] == [])
    projected = {p["key"]: p for p in view["projected_cashflows"]}
    check("预计现金流：Q2 票息 40000.00、到期兑付 1000000.00",
          projected.get("coupon:2026-09-19", {}).get("amount") == "40000.00"
          and projected.get("redemption:maturity", {}).get("amount") == "1000000.00")

    explain = explain_cashflow(engine, f"{PRODUCT}:coupon:2026-06-19")
    just = explain["justification"]
    pinned_b = (just or {}).get("evidence", {}).get("300750.SZ", {}).get("price", {})
    check("争议复原：6/26 付款 20000.00 时的依据快照完整"
          "（条款 v1 + 观察版本 + 当时行情 195.00 + 计算路径）",
          just is not None and just["paid_amount"] == "20000.00"
          and just["terms_version"] == 1
          and pinned_b.get("value") == "195.00"
          and len(just["calc_path"]) > 0)
    check("后续更正与差额调整可见：更正事件 ≥1，调整 -20000.00",
          len(explain["subsequent_corrections"]) >= 1
          and explain["difference_adjustments"]
          and explain["difference_adjustments"][0]["amount"] == "-20000.00")

    print()
    print("=" * 72)
    print("与 reference/cashflows 样例对账")
    print("=" * 72)
    expected = registry.expected_cashflows(PRODUCT)
    actual = {cf.key: cf for cf in engine.latest_cashflows()}
    all_match = True
    for sample in expected["samples"]:
        cf = actual.get(sample["key"])
        ok = (cf is not None and f"{cf.amount}" == sample["amount"]
              and cf.state == sample["state"])
        all_match = all_match and ok
        check(f"{sample['key']} = {sample['amount']}（{sample['state']}）", ok,
              f"实际 {cf.amount if cf else '缺失'}/{cf.state if cf else '-'}")

    failed = [c for c in CHECKS if not c[1]]
    print()
    print(f"合计 {len(CHECKS)} 项断言，失败 {len(failed)} 项")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
