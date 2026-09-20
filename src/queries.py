"""查询投影：运营视图与争议复原。

- 运营视图：各标的逐日证据、障碍判定、下一观察日、预计现金流。
- 争议复原：任一历史付款为何成立（付款时的条款/行情/计算路径快照），
  以及后续更正事件与由此产生的差额调整。
"""
from __future__ import annotations

from typing import Any

from models import (
    OBS_KIND_KI, OBS_KIND_SCHEDULED, OBS_PENDING, D, dec_str,
)
from engine import NotFoundError, ProductEngine


def operations_view(engine: ProductEngine) -> dict[str, Any]:
    """运营人员视角：单产品的观察证据、障碍判定、下一观察日与预计现金流。"""
    terms = engine._terms_latest()
    basket = [m["ticker"] for m in terms.payload["basket"]] if terms else []

    # 逐日证据：按标的汇总敲入逐日观察
    daily: dict[str, list[dict[str, Any]]] = {t: [] for t in basket}
    for obs_id, versions in sorted(engine.observations.items()):
        obs = versions[-1]
        if obs.kind != OBS_KIND_KI:
            continue
        for ticker, ev in obs.evidence.items():
            daily.setdefault(ticker, []).append({
                "date": obs.date.isoformat(),
                "obs_version": obs.version,
                "status": obs.status,
                "price": ev.get("price"),
                "indicative_price": ev.get("indicative_price"),
                "price_quality": ev.get("price_quality"),
                "adjusted_initial": ev.get("adjusted_initial"),
                "ratio": ev.get("ratio"),
                "ki_barrier_level": ev.get("barrier_levels", {}).get("knock_in"),
                "ki_breached": ev.get("breached"),
                "pending_reasons": obs.pending_reasons,
            })

    # 预定观察日一览
    observations = []
    for item in engine.schedule:
        obs = engine._latest_obs(item["obs_id"])
        if obs is None:
            status = "cancelled" if engine.status_info.get("status") == "called" else "scheduled"
            observations.append({
                "obs_id": item["obs_id"], "kind": OBS_KIND_SCHEDULED,
                "scheduled_date": item["scheduled_date"].isoformat(),
                "date": item["date"].isoformat(), "roll_path": item["roll_path"],
                "is_final": item["is_final"], "status": status,
            })
        else:
            observations.append({
                "obs_id": obs.obs_id, "kind": obs.kind,
                "scheduled_date": obs.scheduled_date.isoformat(),
                "date": obs.date.isoformat(), "roll_path": obs.roll_path,
                "is_final": item["is_final"], "status": obs.status,
                "version": obs.version, "outcomes": obs.outcomes,
                "pending_reasons": obs.pending_reasons,
            })

    # 下一观察日：产品存续时，第一个未确认的预定观察日
    next_obs = None
    if engine.status_info.get("status") == "alive":
        for entry in observations:
            if entry["status"] in ("scheduled", OBS_PENDING):
                next_obs = entry
                break

    projected = [
        _cf_brief(cf) for cf in engine.latest_cashflows()
        if cf.state == "projected"
    ]
    upcoming = [
        _cf_brief(cf) for cf in engine.latest_cashflows()
        if cf.state in ("confirmed",)
    ]

    barriers = terms.payload["barriers"] if terms else {}
    barrier_levels: dict[str, Any] = {}
    if terms:
        for member in terms.payload["basket"]:
            ticker = member["ticker"]
            adj, path = engine._adjusted_initial(ticker, engine.clock.date())
            barrier_levels[ticker] = {
                "adjusted_initial": dec_str(adj),
                "adjustment_path": path,
                "knock_in": dec_str(adj * D(barriers["knock_in"]["ratio"])),
                "coupon": dec_str(adj * D(barriers["coupon"]["ratio"])),
                "autocall": dec_str(adj * D(barriers["autocall"]["ratio"])),
            }

    return {
        "product_id": engine.product_id,
        "as_of": engine.clock.isoformat(),
        "status": engine.status_info,
        "terms_version": terms.version if terms else None,
        "barriers": barriers,
        "barrier_levels": barrier_levels,
        "next_observation": next_obs,
        "observations": observations,
        "daily_evidence": daily,
        "projected_cashflows": projected,
        "confirmed_cashflows": upcoming,
    }


def explain_cashflow(engine: ProductEngine, cf_id: str) -> dict[str, Any]:
    """争议处理视角：复原一笔现金流为何成立，以及后续更正造成的差额。"""
    history = engine.cashflow_history(cf_id)
    if not history:
        raise NotFoundError(f"现金流不存在：{cf_id}")
    latest = history[-1]

    # 付款版本（含付款时的完整证据快照）
    paid_version = next((v for v in reversed(history)
                         if v.state in ("paid", "adjusted")), None)
    justification = None
    if paid_version is not None:
        lineage = paid_version.lineage
        justification = {
            "paid_amount": dec_str(paid_version.amount),
            "paid_version": paid_version.version,
            "terms_version": lineage.get("terms_version"),
            "observations": [
                _obs_snapshot(engine, ref) for ref in lineage.get("observations", [])
            ],
            "evidence": lineage.get("evidence"),
            "calc_path": lineage.get("calc_path", []),
            "settled_by": lineage.get("settled_by"),
        }

    # 结算记录
    settlements = [st.to_dict() for st in engine.settlements.values()
                   if st.cf_id == latest.cf_id]

    # 后续更正：在该笔付款落账（事件序号）之后进入系统的条款/行情/公司行动事件
    corrections: list[dict[str, Any]] = []
    if paid_version is not None:
        paid_seq = None
        for event in engine.store.events():
            payload = event["payload"]
            if event["type"] == "cashflow_recorded" \
                    and payload.get("key") == latest.key \
                    and payload.get("state") == "paid":
                paid_seq = event["seq"]
                break
            if event["type"] == "settlement_recorded" \
                    and payload.get("cf_id") == latest.cf_id:
                paid_seq = event["seq"]
                break
        if paid_seq is not None:
            for event in engine.store.events():
                if event["seq"] > paid_seq and event["type"] in (
                        "price_ingested", "terms_registered", "corporate_action_applied"):
                    corrections.append({"seq": event["seq"], "type": event["type"],
                                        "payload": event["payload"]})

    # 差额调整链
    adjustments = []
    for key, versions in engine.cashflows.items():
        if key.startswith(f"adj:{latest.key}:"):
            adj = versions[-1]
            adjustments.append({
                "cf_id": adj.cf_id, "state": adj.state,
                "amount": dec_str(adj.amount), "value_date": adj.value_date.isoformat(),
                "reason": adj.lineage.get("reason"),
                "calc_path": adj.lineage.get("calc_path", []),
            })

    return {
        "cashflow": latest.to_dict(),
        "history": [v.to_dict() for v in history],
        "justification": justification,
        "settlements": settlements,
        "subsequent_corrections": corrections,
        "difference_adjustments": adjustments,
        "under_review": latest.lineage.get("under_review"),
    }


def _obs_snapshot(engine: ProductEngine, ref: dict[str, Any]) -> dict[str, Any]:
    versions = engine.observations.get(ref["obs_id"], [])
    pinned = next((v for v in versions if v.version == ref["version"]), None)
    obs = pinned or (versions[-1] if versions else None)
    if obs is None:
        return {"obs_id": ref["obs_id"], "missing": True}
    return {
        "obs_id": obs.obs_id, "version": obs.version, "status": obs.status,
        "date": obs.date.isoformat(), "terms_version": obs.terms_version,
        "outcomes": obs.outcomes, "calc_path": obs.calc_path,
        "pinned_as_paid": pinned is not None,
    }


def _cf_brief(cf) -> dict[str, Any]:
    return {
        "cf_id": cf.cf_id, "key": cf.key, "type": cf.type, "state": cf.state,
        "amount": dec_str(cf.amount), "currency": cf.currency,
        "value_date": cf.value_date.isoformat(),
        "assumptions": cf.lineage.get("assumptions"),
        "delivery": cf.delivery,
    }
