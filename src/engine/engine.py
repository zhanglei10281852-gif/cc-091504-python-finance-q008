"""结构性票据核算引擎。

核心原则：
- 每个观察点固定当时适用的条款版本与权威行情（证据记录价格版本号）。
- 临时（provisional）价格只能让观察进入待确认（pending_confirmation）。
- 停牌等缺价按条款顺延（postpone），绝不静默跳过。
- 补发/撤销行情通过新版本重算尚未支付的现金流；已付款项目保留原记录，
  以差额调整（adjustment）反映，绝不抹去。
- 结算按幂等键执行，同一指令重试不会重复付款。
"""

from __future__ import annotations

import json
import threading
from datetime import date

from . import pricing
from .store import Store
from .types import (
    DateLike,
    add_business_days,
    business_days_between,
    dec,
    js,
    money,
    new_id,
    next_business_day,
    parse_date,
    px,
    roll_date,
    utc_now,
)

TERMS = "terms"
PRICES = "prices"
CORP_ACTIONS = "corp_actions"
CALENDARS = "calendars"
OBSERVATIONS = "observations"
CASHFLOWS = "cashflows"
SETTLEMENTS = "settlements"
PRODUCT_STATE = "product_state"
SAMPLES = "cashflow_samples"

OBS_AUTOCALL = "autocall"
OBS_FINAL = "final"
OBS_KI_DAILY = "ki_daily"

ST_PROJECTED = "projected"
ST_CONFIRMED = "confirmed"
ST_PAID = "paid"
ST_ADJUSTED = "adjusted"
ST_CANCELLED = "cancelled"

OBS_POSTPONED = "postponed"
OBS_PENDING = "pending_confirmation"
OBS_CONFIRMED = "confirmed"
OBS_UNRESOLVED = "unresolved"

TERMS_DEFAULTS = {
    "currency": "CNY",
    "coupon_memory": True,
    "coupon_on_autocall": "always",
    "delivery": "cash",
    "holiday_calendar": "CN-SSE",
    "roll_convention": "modified_following",
    "payment_lag_days": 2,
    "missing_price_rule": {"action": "postpone", "max_days": 8},
    "ki_observation": "daily_close",
    "day_count": "ACT/365",
}

TERMS_REQUIRED = [
    "notional",
    "issue_date",
    "maturity_date",
    "basket",
    "knock_out_barrier",
    "knock_in_barrier",
    "coupon_barrier",
    "coupon_rate_per_period",
    "put_strike",
    "autocall_dates",
]


class EngineError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def _normalize_terms_body(body: dict) -> dict:
    missing = [k for k in TERMS_REQUIRED if k not in body]
    if missing:
        raise EngineError("terms_incomplete", f"terms missing fields: {', '.join(missing)}")
    out = dict(TERMS_DEFAULTS)
    out.update(body)
    if not out["basket"]:
        raise EngineError("terms_incomplete", "basket must not be empty")
    legs = []
    for leg in out["basket"]:
        if "underlying" not in leg or "initial_price" not in leg:
            raise EngineError("terms_incomplete", "basket legs need underlying and initial_price")
        legs.append(
            {
                "underlying": leg["underlying"],
                "initial_price": js(px(leg["initial_price"])),
                "weight": js(dec(leg.get("weight", "1"))),
            }
        )
    out["basket"] = legs
    out["notional"] = js(money(out["notional"]))
    out["autocall_dates"] = sorted(parse_date(d).isoformat() for d in out["autocall_dates"])
    out["issue_date"] = parse_date(out["issue_date"]).isoformat()
    out["maturity_date"] = parse_date(out["maturity_date"]).isoformat()
    return out


class Engine:
    def __init__(self, store: Store):
        self.store = store
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ utils
    def _audit(self, action, product_id=None, refs=None, detail=None, actor="system", at=None):
        self.store.audit(
            {
                "event_id": new_id("ev"),
                "at": at or utc_now(),
                "actor": actor,
                "action": action,
                "product_id": product_id,
                "refs": refs or {},
                "detail": detail or {},
            }
        )

    # ------------------------------------------------------------------ terms
    def issue_terms(self, product_id, body, effective_from, actor="system", reason="terms_issued", recorded_at=None):
        """发布条款版本。篮子成分变更即一个 reason=basket_change 的新版本。"""
        with self._lock:
            effective_from = parse_date(effective_from).isoformat()
            versions = [t for t in self.store.col(TERMS) if t["product_id"] == product_id]
            version = max([t["version"] for t in versions], default=0) + 1
            rec = {
                "product_id": product_id,
                "version": version,
                "effective_from": effective_from,
                "recorded_at": recorded_at or utc_now(),
                "actor": actor,
                "reason": reason,
                "body": _normalize_terms_body(body),
            }
            self.store.add(TERMS, rec)
            self._ensure_state(product_id)
            self._audit(
                "terms_issued",
                product_id,
                {"terms_version": version},
                {"effective_from": effective_from, "reason": reason},
                actor,
            )
            self._refresh(product_id, actor)
            return rec

    def terms_at(self, product_id, on_date):
        """业务日期 on_date 当天适用的条款版本。"""
        on = parse_date(on_date).isoformat()
        cands = [t for t in self.store.col(TERMS) if t["product_id"] == product_id and t["effective_from"] <= on]
        if not cands:
            return None
        return max(cands, key=lambda t: (t["effective_from"], t["version"]))

    def current_terms(self, product_id):
        cands = [t for t in self.store.col(TERMS) if t["product_id"] == product_id]
        return max(cands, key=lambda t: t["version"], default=None)

    def _terms_version(self, product_id, version):
        for t in self.store.col(TERMS):
            if t["product_id"] == product_id and t["version"] == version:
                return t
        return None

    # -------------------------------------------------------------- calendars
    def set_holidays(self, calendar, dates, actor="system"):
        with self._lock:
            normalized = sorted({parse_date(d).isoformat() for d in dates})
            recs = [c for c in self.store.col(CALENDARS) if c["calendar"] == calendar]
            if recs:
                recs[0]["dates"] = normalized
                self.store.save(CALENDARS)
            else:
                self.store.add(CALENDARS, {"calendar": calendar, "dates": normalized})
            self._audit("calendar_set", None, {"calendar": calendar}, {"days": len(normalized)}, actor)

    def holidays(self, calendar) -> set[date]:
        for c in self.store.col(CALENDARS):
            if c["calendar"] == calendar:
                return {parse_date(d) for d in c["dates"]}
        return set()

    # -------------------------------------------------------- corporate actions
    def apply_corporate_action(self, underlying, ex_date, kind, factor, actor="system", note=None, recorded_at=None):
        """登记公司行动（拆分、股息等），factor 作用于 ex_date 之后的观察。"""
        with self._lock:
            rec = {
                "ca_id": new_id("ca"),
                "underlying": underlying,
                "ex_date": parse_date(ex_date).isoformat(),
                "kind": kind,
                "factor": js(dec(factor)),
                "note": note,
                "recorded_at": recorded_at or utc_now(),
                "actor": actor,
            }
            self.store.add(CORP_ACTIONS, rec)
            self._audit(
                "corporate_action",
                None,
                {"ca_id": rec["ca_id"], "underlying": underlying},
                {"kind": kind, "factor": rec["factor"], "ex_date": rec["ex_date"]},
                actor,
            )
            for pid in self._products_ever_holding(underlying):
                self._refresh(pid, actor)
            return rec

    def _factor(self, underlying, issue_date, on_date):
        """期初价调整因子：发行日之后、观察日（含）之前生效的公司行动累积。"""
        issue = parse_date(issue_date)
        on = parse_date(on_date)
        factor = dec(1)
        cas = [c for c in self.store.col(CORP_ACTIONS) if c["underlying"] == underlying]
        cas.sort(key=lambda c: (c["ex_date"], c["recorded_at"]))
        for ca in cas:
            if issue < parse_date(ca["ex_date"]) <= on:
                factor *= dec(ca["factor"])
        return factor

    # ------------------------------------------------------------ market data
    def ingest_price(
        self,
        underlying,
        price_date,
        price=None,
        price_type="official_close",
        status="official",
        source="unknown",
        actor="system",
        note=None,
        recorded_at=None,
    ):
        """登记行情。同一 (标的, 日期, 类型) 重复登记产生递增版本，最新版本为准。"""
        with self._lock:
            price_date = parse_date(price_date).isoformat()
            if status in ("official", "provisional") and price is None:
                raise EngineError("price_required", f"{status} price requires a value")
            versions = [
                p
                for p in self.store.col(PRICES)
                if p["underlying"] == underlying and p["price_date"] == price_date and p["price_type"] == price_type
            ]
            version = max([p["version"] for p in versions], default=0) + 1
            rec = {
                "price_id": new_id("px"),
                "underlying": underlying,
                "price_date": price_date,
                "price_type": price_type,
                "price": js(px(price)) if price is not None else None,
                "status": status,
                "source": source,
                "version": version,
                "supersedes": (max(versions, key=lambda p: p["version"])["price_id"] if versions else None),
                "note": note,
                "recorded_at": recorded_at or utc_now(),
                "actor": actor,
            }
            self.store.add(PRICES, rec)
            self._audit(
                "price_ingested",
                None,
                {"price_id": rec["price_id"], "underlying": underlying},
                {"date": price_date, "status": status, "price": rec["price"], "version": version},
                actor,
            )
            as_of = parse_date(rec["recorded_at"]) if recorded_at else date.today()
            self._reprocess_market_change(underlying, price_date, actor, as_of)
            return rec

    def record_suspension(self, underlying, price_date, actor="system", note=None, recorded_at=None):
        rec = self.ingest_price(
            underlying,
            price_date,
            price=None,
            status="suspended",
            source="exchange",
            actor=actor,
            note=note or "trading suspended",
            recorded_at=recorded_at,
        )
        self._audit(
            "suspension_recorded",
            None,
            {"underlying": underlying, "price_id": rec["price_id"]},
            {"date": parse_date(price_date).isoformat()},
            actor,
        )
        return rec

    def correct_price(
        self,
        underlying,
        price_date,
        action,
        actor="system",
        new_price=None,
        reason=None,
        price_type="official_close",
        recorded_at=None,
    ):
        """发行人/交易所更正：reissue 补发新版本，cancel 撤销。触发未付重算与已付差额调整。"""
        with self._lock:
            if action == "reissue":
                rec = self.ingest_price(
                    underlying,
                    price_date,
                    price=new_price,
                    price_type=price_type,
                    status="official",
                    source="correction",
                    actor=actor,
                    note=reason,
                    recorded_at=recorded_at,
                )
            elif action == "cancel":
                rec = self.ingest_price(
                    underlying,
                    price_date,
                    price=None,
                    price_type=price_type,
                    status="cancelled",
                    source="correction",
                    actor=actor,
                    note=reason,
                    recorded_at=recorded_at,
                )
            else:
                raise EngineError("bad_action", "action must be 'reissue' or 'cancel'")
            self._audit(
                "price_corrected",
                None,
                {"price_id": rec["price_id"], "underlying": underlying},
                {"date": parse_date(price_date).isoformat(), "action": action, "reason": reason},
                actor,
            )
            return rec

    def _latest_price(self, underlying, price_date, price_type="official_close"):
        cands = [
            p
            for p in self.store.col(PRICES)
            if p["underlying"] == underlying and p["price_date"] == price_date and p["price_type"] == price_type
        ]
        if not cands:
            return None
        return max(cands, key=lambda p: p["version"])

    def _products_ever_holding(self, underlying):
        return {
            t["product_id"]
            for t in self.store.col(TERMS)
            if any(leg["underlying"] == underlying for leg in t["body"]["basket"])
        }

    def _reprocess_market_change(self, underlying, price_date, actor, as_of):
        """行情变化后，重算受影响的观察（新版本），进而刷新现金流。"""
        for pid in sorted(self._products_ever_holding(underlying)):
            for obs in self._latest_observations(pid):
                if obs["actual_date"] == price_date or obs["state"] in (OBS_POSTPONED, OBS_PENDING, OBS_UNRESOLVED):
                    self._observe(pid, obs["sched_date"], obs["kind"], actor, as_of=as_of)

    # ------------------------------------------------------------ observations
    def _latest_observations(self, product_id):
        best = {}
        for o in self.store.col(OBSERVATIONS):
            if o["product_id"] != product_id:
                continue
            key = (o["sched_date"], o["kind"])
            if key not in best or o["version"] > best[key]["version"]:
                best[key] = o
        return sorted(best.values(), key=lambda o: (o["sched_date"], o["kind"]))

    def _latest_observation(self, product_id, sched_date, kind):
        for o in self._latest_observations(product_id):
            if o["sched_date"] == sched_date and o["kind"] == kind:
                return o
        return None

    def observation_by_uid(self, uid):
        for o in self.store.col(OBSERVATIONS):
            if o["observation_uid"] == uid:
                return o
        return None

    def observation_versions(self, product_id, sched_date, kind):
        recs = [
            o
            for o in self.store.col(OBSERVATIONS)
            if o["product_id"] == product_id and o["sched_date"] == sched_date and o["kind"] == kind
        ]
        return sorted(recs, key=lambda o: o["version"])

    def _kind_for(self, terms, sched_date):
        dates = terms["body"]["autocall_dates"]
        if sched_date in dates:
            return OBS_FINAL if sched_date == dates[-1] else OBS_AUTOCALL
        return OBS_KI_DAILY

    def run_observation(self, product_id, sched_date, kind=None, actor="system", as_of=None):
        with self._lock:
            sched_date = parse_date(sched_date).isoformat()
            as_of = parse_date(as_of) if as_of else date.today()
            terms = self.terms_at(product_id, sched_date)
            if terms is None:
                raise EngineError("no_terms", f"no terms for {product_id} effective on {sched_date}", 404)
            if parse_date(sched_date) > as_of:
                raise EngineError("not_due", f"observation {sched_date} is after as_of {as_of}", 409)
            kind = kind or self._kind_for(terms, sched_date)
            return self._observe(product_id, sched_date, kind, actor, as_of=as_of)

    def process_due(self, product_id, as_of, actor="system"):
        """运行截至 as_of 全部到期观察：自动赎回/期末观察 + 逐日敲入观察。"""
        with self._lock:
            as_of = parse_date(as_of)
            terms = self.current_terms(product_id)
            if terms is None:
                raise EngineError("no_terms", f"no terms for {product_id}", 404)
            state = self._ensure_state(product_id)
            if state.get("autocalled") or state.get("matured"):
                return []
            body = terms["body"]
            holidays = self.holidays(body["holiday_calendar"])
            existing = {(o["sched_date"], o["kind"]): o for o in self._latest_observations(product_id)}
            ran = []
            for d in body["autocall_dates"]:
                if parse_date(d) > as_of:
                    continue
                kind = self._kind_for(terms, d)
                obs = existing.get((d, kind))
                if obs and obs["state"] == OBS_CONFIRMED:
                    continue
                if obs and obs["state"] == OBS_POSTPONED and parse_date(obs["next_attempt"]) > as_of:
                    continue
                rec = self._observe(product_id, d, kind, actor, as_of=as_of)
                if rec is not None:
                    ran.append(rec)
            state = self._ensure_state(product_id)
            if not (state.get("autocalled") or state.get("matured")):
                start = parse_date(body["issue_date"])
                end = min(as_of, parse_date(body["maturity_date"]))
                sched = set(body["autocall_dates"])
                for d in business_days_between(start, end, holidays):
                    ds = d.isoformat()
                    if ds in sched:
                        continue
                    obs = existing.get((ds, OBS_KI_DAILY))
                    if obs and obs["state"] == OBS_CONFIRMED:
                        continue
                    rec = self._observe(product_id, ds, OBS_KI_DAILY, actor, as_of=as_of)
                    if rec is not None:
                        ran.append(rec)
            return ran

    def _build_evidence(self, leg, body, actual):
        underlying = leg["underlying"]
        factor = self._factor(underlying, body["issue_date"], actual)
        initial_adj = px(dec(leg["initial_price"]) * factor)
        rec = self._latest_price(underlying, actual.isoformat())
        entry = {
            "underlying": underlying,
            "initial_price": leg["initial_price"],
            "factor": js(factor),
            "initial_adj": js(initial_adj),
            "price_id": rec["price_id"] if rec else None,
            "price_version": rec["version"] if rec else None,
            "price": rec["price"] if rec else None,
            "price_status": rec["status"] if rec else "no_record",
            "performance": None,
            "note": (rec.get("note") if rec else "no price record"),
        }
        if rec and rec["status"] in ("official", "provisional") and rec["price"] is not None:
            entry["performance"] = js(pricing.performance(rec["price"], initial_adj))
        return entry

    def _observe(self, product_id, sched_date, kind, actor, as_of):
        terms = self.terms_at(product_id, sched_date)
        if terms is None:
            raise EngineError("no_terms", f"no terms for {product_id} effective on {sched_date}", 404)
        body = terms["body"]
        holidays = self.holidays(body["holiday_calendar"])
        prev = self._latest_observation(product_id, sched_date, kind)
        state = self._ensure_state(product_id)
        if (state.get("autocalled") or state.get("matured")) and prev is None:
            return None

        rolled = roll_date(parse_date(sched_date), body["roll_convention"], holidays)
        rolled_from = sched_date if rolled.isoformat() != sched_date else None
        rule = body.get("missing_price_rule", {"action": "postpone", "max_days": 8})
        max_days = int(rule.get("max_days", 8))

        actual = rolled
        evidence = None
        obs_state = None
        next_attempt = None
        tentative = False
        while True:
            evidence = [self._build_evidence(leg, body, actual) for leg in body["basket"]]
            missing = [e for e in evidence if e["price_status"] not in ("official", "provisional")]
            provisional = [e for e in evidence if e["price_status"] == "provisional"]
            if not missing and not provisional:
                obs_state = OBS_CONFIRMED
                break
            if provisional:
                # 临时价格只能进入待确认
                obs_state = OBS_PENDING
                tentative = not missing
                break
            if kind == OBS_KI_DAILY or rule.get("action") != "postpone":
                obs_state = OBS_PENDING
                break
            nxt = next_business_day(actual, holidays)
            if (nxt - rolled).days > max_days:
                obs_state = OBS_UNRESOLVED
                break
            if nxt > as_of:
                obs_state = OBS_POSTPONED
                next_attempt = nxt.isoformat()
                break
            actual = nxt

        decision = None
        worst = None
        if obs_state == OBS_CONFIRMED or tentative:
            perfs = {e["underlying"]: dec(e["performance"]) for e in evidence}
            worst_u = min(perfs, key=lambda u: (perfs[u], u))
            worst = {"underlying": worst_u, "performance": js(perfs[worst_u])}
            wo = perfs[worst_u]
            decision = {
                "worst_of": worst,
                "knock_in": bool(wo <= dec(body["knock_in_barrier"])),
                "coupon_ok": bool(wo >= dec(body["coupon_barrier"])),
                "knock_out": bool(kind == OBS_AUTOCALL and wo >= dec(body["knock_out_barrier"])),
                "tentative": tentative,
            }

        fingerprint = json.dumps(
            {
                "terms_version": terms["version"],
                "actual": actual.isoformat(),
                "state": obs_state,
                "next": next_attempt,
                "ev": [
                    (e["underlying"], e["price_id"], e["price_status"], e["factor"], e["performance"])
                    for e in evidence
                ],
            },
            sort_keys=True,
        )
        if prev and prev.get("fingerprint") == fingerprint:
            return prev

        uid = new_id("obs")
        rec = {
            "observation_uid": uid,
            "product_id": product_id,
            "sched_date": sched_date,
            "kind": kind,
            "version": (prev["version"] + 1) if prev else 1,
            "state": obs_state,
            "terms_version": terms["version"],
            "actual_date": actual.isoformat(),
            "rolled_from": rolled_from,
            "roll_reason": "holiday" if rolled_from else None,
            "next_attempt": next_attempt,
            "evidence": evidence,
            "worst_of": worst,
            "decision": decision,
            "fingerprint": fingerprint,
            "superseded_by": None,
            "recorded_at": utc_now(),
            "actor": actor,
        }
        if prev:
            prev["superseded_by"] = uid
        self.store.add(OBSERVATIONS, rec)
        self._audit(
            "observation_confirmed" if obs_state == OBS_CONFIRMED else "observation_recorded",
            product_id,
            {"observation_uid": uid, "sched_date": sched_date, "kind": kind},
            {"state": obs_state, "actual_date": rec["actual_date"], "version": rec["version"]},
            actor,
        )
        self._refresh(product_id, actor)
        return rec

    # ---------------------------------------------------------- state & flows
    def _ensure_state(self, product_id):
        for s in self.store.col(PRODUCT_STATE):
            if s["product_id"] == product_id:
                return s
        s = {
            "product_id": product_id,
            "knocked_in": False,
            "knock_in_date": None,
            "autocalled": False,
            "autocall_date": None,
            "matured": False,
            "maturity_date": None,
            "memory_count": 0,
        }
        self.store.add(PRODUCT_STATE, s)
        return s

    def _rebuild_state(self, product_id, actor):
        state = self._ensure_state(product_id)
        confirmed = [
            o
            for o in self._latest_observations(product_id)
            if o["state"] == OBS_CONFIRMED and o.get("decision")
        ]
        confirmed.sort(key=lambda o: (o["actual_date"], o["kind"]))
        new = {
            "knocked_in": False,
            "knock_in_date": None,
            "autocalled": False,
            "autocall_date": None,
            "matured": False,
            "maturity_date": None,
        }
        for o in confirmed:
            d = o["decision"]
            if d.get("knock_in") and not new["knocked_in"]:
                new["knocked_in"] = True
                new["knock_in_date"] = o["actual_date"]
            if o["kind"] == OBS_AUTOCALL and d.get("knock_out") and not new["autocalled"]:
                new["autocalled"] = True
                new["autocall_date"] = o["actual_date"]
            if o["kind"] == OBS_FINAL:
                new["matured"] = True
                new["maturity_date"] = o["actual_date"]
        transitions = []
        if state.get("knocked_in") != new["knocked_in"]:
            transitions.append("knock_in" if new["knocked_in"] else "knock_in_revoked")
        if state.get("autocalled") != new["autocalled"]:
            transitions.append("autocall_triggered" if new["autocalled"] else "autocall_revoked")
        if state.get("matured") != new["matured"]:
            transitions.append("matured" if new["matured"] else "maturity_revoked")
        changed = any(state.get(k) != v for k, v in new.items())
        if changed:
            state.update(new)
            self.store.save(PRODUCT_STATE)
            for action in transitions:
                self._audit(
                    action,
                    product_id,
                    {},
                    {
                        "knock_in_date": new["knock_in_date"],
                        "autocall_date": new["autocall_date"],
                        "maturity_date": new["maturity_date"],
                    },
                    actor,
                )

    def _refresh(self, product_id, actor):
        if not any(t["product_id"] == product_id for t in self.store.col(TERMS)):
            return
        self._rebuild_state(product_id, actor)
        desired, memory = self._desired_cashflows(product_id)
        self._diff_cashflows(product_id, desired, actor)
        state = self._ensure_state(product_id)
        if state.get("memory_count") != memory:
            state["memory_count"] = memory
            self.store.save(PRODUCT_STATE)

    def _latest_confirmed_worst(self, product_id):
        confirmed = [
            o
            for o in self._latest_observations(product_id)
            if o["state"] == OBS_CONFIRMED and o.get("worst_of")
        ]
        if not confirmed:
            return None
        latest = max(confirmed, key=lambda o: o["actual_date"])
        return dec(latest["worst_of"]["performance"])

    def _desired_cashflows(self, product_id):
        """由条款与已确认观察确定性重算全部应有现金流（含预计）。"""
        desired = {}
        terms_now = self.current_terms(product_id)
        if terms_now is None:
            return desired, 0
        state = self._ensure_state(product_id)
        confirmed = [
            o
            for o in self._latest_observations(product_id)
            if o["state"] == OBS_CONFIRMED and o.get("decision") and o["kind"] in (OBS_AUTOCALL, OBS_FINAL)
        ]
        confirmed.sort(key=lambda o: o["actual_date"])
        pending = 0
        terminated = False
        last_sched = None
        for obs in confirmed:
            terms = self._terms_version(product_id, obs["terms_version"])
            body = terms["body"]
            dates = body["autocall_dates"]
            if obs["sched_date"] not in dates:
                continue
            period = dates.index(obs["sched_date"]) + 1
            notional = dec(body["notional"])
            rate = dec(body["coupon_rate_per_period"])
            holidays = self.holidays(body["holiday_calendar"])
            pay_date = add_business_days(
                parse_date(obs["actual_date"]), int(body.get("payment_lag_days", 2)), holidays
            ).isoformat()
            d = obs["decision"]
            wo_txt = d["worst_of"]["performance"]
            coupon_due = d["coupon_ok"] or (
                obs["kind"] == OBS_AUTOCALL
                and d["knock_out"]
                and body.get("coupon_on_autocall", "always") == "always"
            )
            if coupon_due:
                cnt = pending + 1
                amount = pricing.coupon_amount(notional, rate, cnt)
                if d["coupon_ok"]:
                    step1 = f"worst_of {wo_txt} ({d['worst_of']['underlying']}) >= coupon_barrier {body['coupon_barrier']} → 当期票息应付"
                else:
                    step1 = "自动赎回触发，按 coupon_on_autocall=always 支付当期票息"
                steps = [
                    step1,
                    f"coupon = {js(notional)} × {js(rate)} × {cnt} 期（含记忆补付 {pending} 期）= {js(amount)}",
                ]
                desired[f"cf-{product_id}-coupon-{period}"] = {
                    "kind": "coupon",
                    "period": period,
                    "amount": js(amount),
                    "state": ST_CONFIRMED,
                    "pay_date": pay_date,
                    "basis": {
                        "observation_uid": obs["observation_uid"],
                        "terms_version": terms["version"],
                        "inputs": {
                            "notional": js(notional),
                            "rate": js(rate),
                            "periods_paid": cnt,
                            "memory_used": pending,
                        },
                        "steps": steps,
                    },
                }
                pending = 0
            else:
                pending += 1
            if obs["kind"] == OBS_AUTOCALL and d["knock_out"]:
                steps = [
                    f"worst_of {wo_txt} >= knock_out_barrier {body['knock_out_barrier']} → 自动赎回",
                    f"redemption = notional {js(notional)}",
                ]
                desired[f"cf-{product_id}-redemption-{period}"] = {
                    "kind": "redemption",
                    "period": None,
                    "amount": js(money(notional)),
                    "state": ST_CONFIRMED,
                    "pay_date": pay_date,
                    "basis": {
                        "observation_uid": obs["observation_uid"],
                        "terms_version": terms["version"],
                        "inputs": {"notional": js(notional)},
                        "steps": steps,
                    },
                }
                terminated = True
            if obs["kind"] == OBS_FINAL:
                wo = dec(wo_txt)
                ki = bool(state.get("knocked_in"))
                if body.get("delivery") == "physical" and ki and wo < dec(body["put_strike"]):
                    worst_leg = next(
                        leg for leg in body["basket"] if leg["underlying"] == d["worst_of"]["underlying"]
                    )
                    factor = self._factor(worst_leg["underlying"], body["issue_date"], obs["actual_date"])
                    initial_adj = px(dec(worst_leg["initial_price"]) * factor)
                    qty, residual = pricing.physical_delivery(notional, body["put_strike"], initial_adj)
                    steps = [
                        f"已敲入且 worst_of {wo_txt} < put_strike {body['put_strike']} → 实物交付",
                        f"quantity = floor({js(notional)} / ({body['put_strike']} × {js(initial_adj)})) = {qty} 股 {worst_leg['underlying']}",
                        f"residual cash = {js(residual)}",
                    ]
                    desired[f"cf-{product_id}-delivery"] = {
                        "kind": "delivery",
                        "period": None,
                        "amount": js(residual),
                        "quantity": qty,
                        "underlying": worst_leg["underlying"],
                        "state": ST_CONFIRMED,
                        "pay_date": pay_date,
                        "basis": {
                            "observation_uid": obs["observation_uid"],
                            "terms_version": terms["version"],
                            "inputs": {"notional": js(notional), "put_strike": body["put_strike"]},
                            "steps": steps,
                        },
                    }
                else:
                    amt = pricing.maturity_cash(notional, wo, body["put_strike"], ki)
                    if ki and wo < dec(body["put_strike"]):
                        steps = [
                            f"已敲入且 worst_of {wo_txt} < put_strike {body['put_strike']} → 现金结算损失",
                            f"redemption = {js(notional)} × {wo_txt} / {body['put_strike']} = {js(amt)}",
                        ]
                    else:
                        steps = [
                            f"knocked_in={ki}, worst_of {wo_txt} vs put_strike {body['put_strike']} → 面值兑付",
                            f"redemption = {js(amt)}",
                        ]
                    desired[f"cf-{product_id}-redemption-{period}"] = {
                        "kind": "redemption",
                        "period": None,
                        "amount": js(amt),
                        "state": ST_CONFIRMED,
                        "pay_date": pay_date,
                        "basis": {
                            "observation_uid": obs["observation_uid"],
                            "terms_version": terms["version"],
                            "inputs": {"notional": js(notional), "put_strike": body["put_strike"]},
                            "steps": steps,
                        },
                    }
                terminated = True
            if terminated:
                break
            last_sched = obs["sched_date"]

        if not terminated:
            body = terms_now["body"]
            holidays = self.holidays(body["holiday_calendar"])
            notional = dec(body["notional"])
            rate = dec(body["coupon_rate_per_period"])
            lag = int(body.get("payment_lag_days", 2))
            future = [d for d in body["autocall_dates"] if last_sched is None or d > last_sched]
            first = True
            for d in future:
                period = body["autocall_dates"].index(d) + 1
                cnt = pending + 1 if first else 1
                amount = pricing.coupon_amount(notional, rate, cnt)
                pay_date = add_business_days(
                    roll_date(parse_date(d), body["roll_convention"], holidays), lag, holidays
                ).isoformat()
                desired[f"cf-{product_id}-coupon-{period}"] = {
                    "kind": "coupon",
                    "period": period,
                    "amount": js(amount),
                    "state": ST_PROJECTED,
                    "pay_date": pay_date,
                    "basis": {
                        "assumption": "假设未来各期票息条件均满足且未提前赎回",
                        "terms_version": terms_now["version"],
                        "inputs": {
                            "notional": js(notional),
                            "rate": js(rate),
                            "periods_paid": cnt,
                            "memory_used": pending if first else 0,
                        },
                        "steps": [f"projected coupon = {js(notional)} × {js(rate)} × {cnt} 期 = {js(amount)}"],
                    },
                }
                first = False
            proj_amount = money(notional)
            note = "假设到期面值兑付"
            if state.get("knocked_in"):
                wo = self._latest_confirmed_worst(product_id)
                if wo is not None:
                    proj_amount = pricing.maturity_cash(notional, wo, body["put_strike"], True)
                    note = f"已敲入；按最近确认 worst_of {js(wo)} 与 put_strike {body['put_strike']} 指示性估算"
            pay_date = add_business_days(
                roll_date(parse_date(body["maturity_date"]), body["roll_convention"], holidays), lag, holidays
            ).isoformat()
            desired[f"cf-{product_id}-redemption-{len(body['autocall_dates'])}"] = {
                "kind": "redemption",
                "period": None,
                "amount": js(proj_amount),
                "state": ST_PROJECTED,
                "pay_date": pay_date,
                "basis": {
                    "assumption": note,
                    "terms_version": terms_now["version"],
                    "inputs": {"notional": js(notional)},
                    "steps": [f"projected redemption = {js(proj_amount)}"],
                },
            }
        return desired, pending

    # ---------------------------------------------------------- cashflow diff
    def _latest_cashflows(self, product_id):
        best = {}
        for c in self.store.col(CASHFLOWS):
            if c["product_id"] != product_id or c["kind"] == "adjustment":
                continue
            cur = best.get(c["cashflow_id"])
            if cur is None or c["version"] > cur["version"]:
                best[c["cashflow_id"]] = c
        return best

    def _latest_cashflow(self, cashflow_id):
        cands = [c for c in self.store.col(CASHFLOWS) if c["cashflow_id"] == cashflow_id]
        if not cands:
            return None
        return max(cands, key=lambda c: c["version"])

    def _active_adjustments(self, product_id, cashflow_id):
        best = {}
        for c in self.store.col(CASHFLOWS):
            if c["product_id"] != product_id or c["kind"] != "adjustment" or c.get("adjusts") != cashflow_id:
                continue
            cur = best.get(c["cashflow_id"])
            if cur is None or c["version"] > cur["version"]:
                best[c["cashflow_id"]] = c
        return [c for c in best.values() if c["state"] != ST_CANCELLED]

    def _new_cf_version(self, cur, actor, **changes):
        rec = dict(cur)
        rec["version"] = cur["version"] + 1
        rec["recorded_at"] = utc_now()
        rec["actor"] = actor
        rec["superseded_by"] = None
        rec.update(changes)
        cur["superseded_by"] = rec["version"]
        self.store.add(CASHFLOWS, rec)
        return rec

    def _new_cashflow(self, product_id, cf_id, spec, actor):
        terms = self.current_terms(product_id)
        rec = {
            "cashflow_id": cf_id,
            "version": 1,
            "product_id": product_id,
            "kind": spec["kind"],
            "period": spec.get("period"),
            "amount": spec["amount"],
            "currency": (terms["body"]["currency"] if terms else "CNY"),
            "state": spec["state"],
            "pay_date": spec.get("pay_date"),
            "quantity": spec.get("quantity"),
            "underlying": spec.get("underlying"),
            "adjusts": spec.get("adjusts"),
            "basis": spec.get("basis"),
            "settlement_id": None,
            "superseded_by": None,
            "recorded_at": utc_now(),
            "actor": actor,
        }
        self.store.add(CASHFLOWS, rec)
        self._audit(
            "cashflow_created",
            product_id,
            {"cashflow_id": cf_id},
            {"kind": spec["kind"], "amount": spec["amount"], "state": spec["state"]},
            actor,
        )
        return rec

    def _cashflow_changed(self, cur, spec):
        if cur["amount"] != spec["amount"] or cur["state"] != spec["state"]:
            return True
        if cur.get("pay_date") != spec.get("pay_date"):
            return True
        cur_obs = (cur.get("basis") or {}).get("observation_uid")
        new_obs = (spec.get("basis") or {}).get("observation_uid")
        return cur_obs != new_obs

    def _diff_cashflows(self, product_id, desired, actor):
        existing = self._latest_cashflows(product_id)
        for cf_id, spec in desired.items():
            cur = existing.get(cf_id)
            if cur is None:
                self._new_cashflow(product_id, cf_id, spec, actor)
            elif cur["state"] in (ST_PROJECTED, ST_CONFIRMED, ST_CANCELLED):
                if self._cashflow_changed(cur, spec):
                    new = self._new_cf_version(
                        cur,
                        actor,
                        amount=spec["amount"],
                        state=spec["state"],
                        pay_date=spec.get("pay_date"),
                        quantity=spec.get("quantity"),
                        underlying=spec.get("underlying"),
                        basis=spec.get("basis"),
                        settlement_id=None,
                    )
                    self._audit(
                        "cashflow_recomputed",
                        product_id,
                        {"cashflow_id": cf_id},
                        {"version": new["version"], "amount": new["amount"], "state": new["state"]},
                        actor,
                    )
            elif cur["state"] in (ST_PAID, ST_ADJUSTED):
                self._reconcile_paid(cur, dec(spec["amount"]), spec, actor)
        for cf_id, cur in existing.items():
            if cf_id in desired:
                continue
            if cur["state"] in (ST_PROJECTED, ST_CONFIRMED):
                new = self._new_cf_version(cur, actor, state=ST_CANCELLED)
                self._audit(
                    "cashflow_cancelled",
                    product_id,
                    {"cashflow_id": cf_id},
                    {"version": new["version"], "reason": "条款或行情更正后不再适用"},
                    actor,
                )
            elif cur["state"] in (ST_PAID, ST_ADJUSTED):
                self._reconcile_paid(cur, dec(0), None, actor)

    def _reconcile_paid(self, cur, desired_amount, spec, actor):
        """已付款项目不抹去：按差额生成/重算调整现金流。

        total_needed = 应有 − 原付款；活跃调整合计应等于它。
        未付调整按残差改版本，已付调整不再动，残差由新调整单补足。
        """
        product_id = cur["product_id"]
        total_needed = desired_amount - dec(cur["amount"])
        active = self._active_adjustments(product_id, cur["cashflow_id"])
        residual = total_needed - sum((dec(a["amount"]) for a in active), dec(0))
        unpaid = [a for a in active if a["state"] in (ST_PROJECTED, ST_CONFIRMED)]
        if unpaid:
            adj = unpaid[0]
            new_amount = dec(adj["amount"]) + residual
            if new_amount == 0:
                self._new_cf_version(adj, actor, state=ST_CANCELLED)
                self._audit(
                    "adjustment_cancelled",
                    product_id,
                    {"cashflow_id": adj["cashflow_id"], "adjusts": cur["cashflow_id"]},
                    {},
                    actor,
                )
            elif residual != 0:
                basis = dict(adj.get("basis") or {})
                basis["steps"] = [
                    f"应有 {js(money(desired_amount))} − 已付 {cur['amount']} − 其他调整 = 差额 {js(money(new_amount))}"
                ]
                self._new_cf_version(adj, actor, amount=js(money(new_amount)), basis=basis)
                self._audit(
                    "adjustment_recomputed",
                    product_id,
                    {"cashflow_id": adj["cashflow_id"], "adjusts": cur["cashflow_id"]},
                    {"amount": js(money(new_amount))},
                    actor,
                )
        elif residual != 0:
            reason = "发行人更正导致已付现金流差额调整"
            steps = [f"应有 {js(money(desired_amount))} − 已付 {cur['amount']} = 差额 {js(money(residual))}"]
            if spec and spec.get("basis"):
                steps = (spec["basis"].get("steps") or []) + steps
            self._new_cashflow(
                product_id,
                new_id("cf-adj"),
                {
                    "kind": "adjustment",
                    "period": cur.get("period"),
                    "amount": js(money(residual)),
                    "state": ST_CONFIRMED,
                    "pay_date": cur.get("pay_date"),
                    "adjusts": cur["cashflow_id"],
                    "basis": {
                        "adjusts": cur["cashflow_id"],
                        "reason": reason,
                        "observation_uid": (spec or {}).get("basis", {}).get("observation_uid"),
                        "steps": steps,
                    },
                },
                actor,
            )
            self._audit(
                "adjustment_created",
                product_id,
                {"adjusts": cur["cashflow_id"]},
                {"amount": js(money(residual)), "reason": reason},
                actor,
            )
        has_active = bool(self._active_adjustments(product_id, cur["cashflow_id"]))
        target_state = ST_ADJUSTED if has_active else ST_PAID
        if cur["state"] != target_state:
            self._new_cf_version(cur, actor, state=target_state)

    # -------------------------------------------------------------- settlement
    def settle(self, idempotency_key, cashflow_id, actor="system"):
        """执行结算。同一 idempotency_key 重试返回原结果，绝不重复付款。"""
        with self._lock:
            for s in self.store.col(SETTLEMENTS):
                if s["idempotency_key"] == idempotency_key:
                    if s["cashflow_id"] != cashflow_id:
                        raise EngineError(
                            "settlement_conflict",
                            f"idempotency key {idempotency_key} already used for {s['cashflow_id']}",
                            409,
                        )
                    self._audit(
                        "settlement_replayed",
                        s["product_id"],
                        {"settlement_id": s["settlement_id"]},
                        {"key": idempotency_key},
                        actor,
                    )
                    return s
            cf = self._latest_cashflow(cashflow_id)
            if cf is None:
                raise EngineError("not_found", f"cashflow {cashflow_id} not found", 404)
            if cf["state"] != ST_CONFIRMED:
                raise EngineError(
                    "not_settleable",
                    f"cashflow {cashflow_id} is {cf['state']}; only confirmed cashflows settle",
                    409,
                )
            st = {
                "settlement_id": new_id("st"),
                "idempotency_key": idempotency_key,
                "product_id": cf["product_id"],
                "cashflow_id": cashflow_id,
                "cashflow_version": cf["version"],
                "amount": cf["amount"],
                "currency": cf["currency"],
                "direction": "pay" if dec(cf["amount"]) >= 0 else "collect",
                "paid_at": utc_now(),
                "actor": actor,
                "status": "paid",
            }
            self.store.add(SETTLEMENTS, st)
            self._new_cf_version(cf, actor, state=ST_PAID, settlement_id=st["settlement_id"])
            self._audit(
                "settlement_paid",
                cf["product_id"],
                {"settlement_id": st["settlement_id"], "cashflow_id": cashflow_id},
                {"amount": cf["amount"], "direction": st["direction"]},
                actor,
            )
            return st

    # ------------------------------------------------------------------ reads
    def list_products(self):
        out = []
        for pid in sorted({t["product_id"] for t in self.store.col(TERMS)}):
            cur = self.current_terms(pid)
            state = self._ensure_state(pid)
            out.append(
                {
                    "product_id": pid,
                    "terms_version": cur["version"],
                    "issue_date": cur["body"]["issue_date"],
                    "maturity_date": cur["body"]["maturity_date"],
                    "knocked_in": state["knocked_in"],
                    "autocalled": state["autocalled"],
                    "matured": state["matured"],
                }
            )
        return out

    def product_state(self, product_id):
        return dict(self._ensure_state(product_id))

    def observations(self, product_id):
        return self._latest_observations(product_id)

    def cashflows(self, product_id):
        latest = self._latest_cashflows(product_id)
        adjs = {}
        for c in self.store.col(CASHFLOWS):
            if c["product_id"] != product_id or c["kind"] != "adjustment":
                continue
            cur = adjs.get(c["cashflow_id"])
            if cur is None or c["version"] > cur["version"]:
                adjs[c["cashflow_id"]] = c
        return sorted(latest.values(), key=lambda c: c["cashflow_id"]) + sorted(
            adjs.values(), key=lambda c: c["cashflow_id"]
        )

    def cashflow_versions(self, cashflow_id):
        recs = [c for c in self.store.col(CASHFLOWS) if c["cashflow_id"] == cashflow_id]
        return sorted(recs, key=lambda c: c["version"])

    def settlements(self, product_id=None):
        recs = self.store.col(SETTLEMENTS)
        if product_id is None:
            return list(recs)
        return [s for s in recs if s["product_id"] == product_id]

    def load_samples(self, samples_by_product, actor="system"):
        with self._lock:
            for pid, samples in samples_by_product.items():
                self.store.add(SAMPLES, {"product_id": pid, "samples": samples, "recorded_at": utc_now()})
            self._audit("samples_loaded", None, {}, {"products": sorted(samples_by_product)}, actor)

    def samples(self, product_id):
        for s in self.store.col(SAMPLES):
            if s["product_id"] == product_id:
                return s["samples"]
        return []

    def audit_trail(self, product_id=None):
        return self.store.audit_trail(product_id)
