"""查询视图：运营看板与争议复原。

- product_view：各标的逐日证据、障碍判定、下一观察日、预计现金流。
- payment_explanation：复原任一历史付款为何成立（条款版本、观察版本、
  行情版本、计算步骤），以及后续更正造成的差额调整。
"""

from __future__ import annotations

from datetime import date

from .engine import (
    OBS_CONFIRMED,
    ST_ADJUSTED,
    ST_CANCELLED,
    ST_CONFIRMED,
    ST_PAID,
    ST_PROJECTED,
    Engine,
    EngineError,
)
from .types import dec, js, parse_date, roll_date


def _obs_summary(obs):
    return {
        "observation_uid": obs["observation_uid"],
        "sched_date": obs["sched_date"],
        "actual_date": obs["actual_date"],
        "kind": obs["kind"],
        "version": obs["version"],
        "state": obs["state"],
        "terms_version": obs["terms_version"],
        "rolled_from": obs["rolled_from"],
        "roll_reason": obs["roll_reason"],
        "next_attempt": obs["next_attempt"],
        "worst_of": obs.get("worst_of"),
        "decision": obs.get("decision"),
        "evidence": obs["evidence"],
    }


def product_view(engine: Engine, product_id: str, as_of=None) -> dict:
    as_of = parse_date(as_of) if as_of else date.today()
    terms = engine.current_terms(product_id)
    if terms is None:
        raise EngineError("not_found", f"product {product_id} not found", 404)
    body = terms["body"]
    state = engine.product_state(product_id)
    observations = engine.observations(product_id)

    next_obs = None
    if not (state["autocalled"] or state["matured"]):
        holidays = engine.holidays(body["holiday_calendar"])
        for d in body["autocall_dates"]:
            if parse_date(d) > as_of:
                kind = "final" if d == body["autocall_dates"][-1] else "autocall"
                next_obs = {
                    "sched_date": d,
                    "actual_date": roll_date(parse_date(d), body["roll_convention"], holidays).isoformat(),
                    "kind": kind,
                }
                break

    confirmed = [o for o in observations if o["state"] == OBS_CONFIRMED and o.get("worst_of")]
    latest_worst = None
    distances = None
    if confirmed:
        latest = max(confirmed, key=lambda o: o["actual_date"])
        wo = dec(latest["worst_of"]["performance"])
        latest_worst = {
            "actual_date": latest["actual_date"],
            "underlying": latest["worst_of"]["underlying"],
            "performance": latest["worst_of"]["performance"],
        }
        distances = {
            "to_knock_out": js(wo - dec(body["knock_out_barrier"])),
            "to_knock_in": js(wo - dec(body["knock_in_barrier"])),
            "to_coupon": js(wo - dec(body["coupon_barrier"])),
        }

    flows = engine.cashflows(product_id)
    grouped = {ST_PROJECTED: [], ST_CONFIRMED: [], ST_PAID: [], ST_ADJUSTED: [], ST_CANCELLED: [], "adjustments": []}
    for cf in flows:
        entry = {
            "cashflow_id": cf["cashflow_id"],
            "kind": cf["kind"],
            "period": cf.get("period"),
            "amount": cf["amount"],
            "currency": cf["currency"],
            "state": cf["state"],
            "pay_date": cf.get("pay_date"),
            "version": cf["version"],
            "adjusts": cf.get("adjusts"),
            "settlement_id": cf.get("settlement_id"),
        }
        if cf["kind"] == "adjustment":
            grouped["adjustments"].append(entry)
        else:
            grouped[cf["state"]].append(entry)

    return {
        "product_id": product_id,
        "as_of": as_of.isoformat(),
        "terms": {
            "version": terms["version"],
            "effective_from": terms["effective_from"],
            "notional": body["notional"],
            "currency": body["currency"],
            "issue_date": body["issue_date"],
            "maturity_date": body["maturity_date"],
            "basket": body["basket"],
            "knock_out_barrier": body["knock_out_barrier"],
            "knock_in_barrier": body["knock_in_barrier"],
            "coupon_barrier": body["coupon_barrier"],
            "coupon_rate_per_period": body["coupon_rate_per_period"],
            "put_strike": body["put_strike"],
            "delivery": body["delivery"],
        },
        "state": state,
        "next_observation": next_obs,
        "barriers": {
            "latest_worst_of": latest_worst,
            "distances": distances,
        },
        "daily_evidence": [_obs_summary(o) for o in observations],
        "cashflows": grouped,
        "samples": engine.samples(product_id),
    }


def payment_explanation(engine: Engine, cashflow_id: str) -> dict:
    versions = engine.cashflow_versions(cashflow_id)
    if not versions:
        raise EngineError("not_found", f"cashflow {cashflow_id} not found", 404)
    latest = versions[-1]
    product_id = latest["product_id"]

    settlement = None
    paid_version = None
    for s in engine.settlements(product_id):
        if s["cashflow_id"] == cashflow_id:
            settlement = s
            paid_version = s["cashflow_version"]
    basis_source = latest
    if paid_version is not None:
        basis_source = next((v for v in versions if v["version"] == paid_version), latest)

    basis = basis_source.get("basis") or {}
    observation = None
    corrections = []
    obs_uid = basis.get("observation_uid")
    if obs_uid:
        obs = engine.observation_by_uid(obs_uid)
        if obs:
            observation = _obs_summary(obs)
            later = engine.observation_versions(product_id, obs["sched_date"], obs["kind"])
            for v in later:
                if v["version"] > obs["version"]:
                    corrections.append(
                        {
                            "observation_uid": v["observation_uid"],
                            "version": v["version"],
                            "state": v["state"],
                            "actual_date": v["actual_date"],
                            "worst_of": v.get("worst_of"),
                            "recorded_at": v["recorded_at"],
                            "evidence": v["evidence"],
                        }
                    )

    adjustments = []
    for cf in engine.cashflows(product_id):
        if cf["kind"] == "adjustment" and cf.get("adjusts") == cashflow_id:
            adj_settlement = next(
                (s for s in engine.settlements(product_id) if s["cashflow_id"] == cf["cashflow_id"]), None
            )
            adjustments.append(
                {
                    "cashflow_id": cf["cashflow_id"],
                    "amount": cf["amount"],
                    "state": cf["state"],
                    "version": cf["version"],
                    "basis": cf.get("basis"),
                    "settlement": adj_settlement,
                }
            )

    original = None
    if latest["kind"] == "adjustment" and latest.get("adjusts"):
        orig_versions = engine.cashflow_versions(latest["adjusts"])
        if orig_versions:
            orig_latest = orig_versions[-1]
            original = {
                "cashflow_id": orig_latest["cashflow_id"],
                "kind": orig_latest["kind"],
                "amount": orig_latest["amount"],
                "state": orig_latest["state"],
            }

    return {
        "cashflow_id": cashflow_id,
        "product_id": product_id,
        "kind": latest["kind"],
        "current": {
            "version": latest["version"],
            "state": latest["state"],
            "amount": latest["amount"],
            "currency": latest["currency"],
            "pay_date": latest.get("pay_date"),
        },
        "history": [
            {
                "version": v["version"],
                "state": v["state"],
                "amount": v["amount"],
                "recorded_at": v["recorded_at"],
                "actor": v.get("actor"),
            }
            for v in versions
        ],
        "basis": {
            "terms_version": basis.get("terms_version"),
            "inputs": basis.get("inputs"),
            "steps": basis.get("steps"),
            "assumption": basis.get("assumption"),
            "reason": basis.get("reason"),
            "based_on_version": basis_source["version"],
        },
        "observation": observation,
        "settlement": settlement,
        "corrections": corrections,
        "adjustments": adjustments,
        "adjusts_original": original,
    }
