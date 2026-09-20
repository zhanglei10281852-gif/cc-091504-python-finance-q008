"""核算引擎核心：观察、障碍判定、现金流推导、更正重算与幂等结算。

核心原则：
- 每个观察版本固定当时适用的条款版本与行情版本（证据快照），历史版本不可改写。
- 缺价 / 临时价（provisional）只能让观察进入 pending，绝不跳过观察；
  只有官方价（official）能确认观察。
- 行情补发、撤销、更正以及条款修订都以新版本进入，触发重算：
  未付现金流直接换版；已付现金流保留原记录，差额以 adjustment 现金流承接。
- 结算按 instruction_id 幂等：同一指令重试返回原结果，绝不重复付款。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from calendars import BusinessCalendar
from models import (
    CF_ADJUSTED, CF_ADJUSTMENT, CF_CANCELLED, CF_CONFIRMED, CF_COUPON, CF_DELIVERY,
    CF_PAID, CF_PROJECTED, CF_REDEMPTION,
    OBS_CANCELLED, OBS_CONFIRMED, OBS_KIND_KI, OBS_KIND_SCHEDULED, OBS_PENDING,
    PRICE_OFFICIAL, PRICE_PROVISIONAL, PRICE_REVOKED,
    Cashflow, CorporateAction, D, Observation, PriceRecord, Settlement, TermsVersion,
    dec_str,
)
from store import EventStore


class EngineError(Exception):
    pass


class NotFoundError(EngineError):
    pass


class ConflictError(EngineError):
    pass


class ValidationError(EngineError):
    pass


_FREQUENCIES = {"monthly": 12, "quarterly": 4, "semiannual": 2, "annual": 1}
_QUALITY_CN = {
    "missing": "缺失（未收到行情）",
    "provisional": "仅有临时价，待官方确认",
    "revoked": "官方价已被撤销，等待补发",
}


@dataclass
class _DesiredCF:
    """一次重算期望存在的现金流（未落库前的目标态）。"""
    key: str
    type: str
    state: str
    amount: Decimal
    value_date: date
    lineage: dict[str, Any]
    delivery: Optional[dict[str, Any]] = None


class ProductEngine:
    def __init__(self, product_id: str, store: EventStore,
                 calendar: BusinessCalendar, precision: dict[str, Any]):
        self.product_id = product_id
        self.store = store
        self.calendar = calendar
        self.precision = precision
        self.clock: datetime = datetime.now(timezone.utc)

        self.terms_versions: list[TermsVersion] = []
        self.prices: dict[tuple[str, str], list[PriceRecord]] = {}
        self.corp_actions: list[CorporateAction] = []
        self.settlements: dict[str, Settlement] = {}
        self.observations: dict[str, list[Observation]] = {}
        self.cashflows: dict[str, list[Cashflow]] = {}
        self.status_info: dict[str, Any] = {"status": "alive"}
        self.schedule: list[dict[str, Any]] = []

        for event in store.events():
            self._fold(event)

    # ------------------------------------------------------------------
    # 事件折叠
    # ------------------------------------------------------------------
    def _fold(self, event: dict[str, Any]) -> None:
        payload = event["payload"]
        etype = event["type"]
        if etype == "terms_registered":
            self.terms_versions.append(TermsVersion.from_dict(payload))
            self.terms_versions.sort(key=lambda t: (t.effective_from, t.version))
        elif etype == "price_ingested":
            rec = PriceRecord.from_dict(payload)
            self.prices.setdefault((rec.underlying, rec.date.isoformat()), []).append(rec)
        elif etype == "corporate_action_applied":
            self.corp_actions.append(CorporateAction.from_dict(payload))
        elif etype == "observation_recorded":
            obs = Observation.from_dict(payload)
            self.observations.setdefault(obs.obs_id, []).append(obs)
        elif etype == "cashflow_recorded":
            cf = Cashflow.from_dict(payload)
            self.cashflows.setdefault(cf.key, []).append(cf)
        elif etype == "settlement_recorded":
            st = Settlement.from_dict(payload)
            self.settlements[st.instruction_id] = st

    # ------------------------------------------------------------------
    # 时钟
    # ------------------------------------------------------------------
    def set_clock(self, at: datetime) -> None:
        self.clock = at

    # ------------------------------------------------------------------
    # 条款
    # ------------------------------------------------------------------
    def register_terms(self, payload: dict[str, Any], effective_from: date,
                       version: Optional[int] = None,
                       registered_at: Optional[datetime] = None) -> TermsVersion:
        registered_at = registered_at or self.clock
        # 幂等：相同内容 + 相同生效日的重复登记直接返回已有版本
        for t in self.terms_versions:
            if t.payload == payload and t.effective_from == effective_from:
                if version is None or t.version == version:
                    return t
                raise ConflictError(
                    f"相同条款已登记为 v{t.version}，与请求版本 v{version} 不符"
                )
        if version is None:
            version = max((t.version for t in self.terms_versions), default=0) + 1
        elif any(t.version == version for t in self.terms_versions):
            raise ConflictError(f"条款版本 v{version} 已存在且内容不同")
        self._validate_terms(payload)
        tv = TermsVersion(self.product_id, version, effective_from, payload, registered_at)
        self.store.append("terms_registered", tv.to_dict())
        self._fold({"type": "terms_registered", "payload": tv.to_dict()})
        return tv

    @staticmethod
    def _validate_terms(payload: dict[str, Any]) -> None:
        for key in ("notional", "currency", "issue_date", "maturity_date",
                    "basket", "barriers", "coupon", "schedule"):
            if key not in payload:
                raise ValidationError(f"条款缺少必填字段 {key}")
        if not payload["basket"]:
            raise ValidationError("篮子不能为空")

    def _terms_on(self, day: date) -> TermsVersion:
        candidates = [t for t in self.terms_versions if t.effective_from <= day]
        if not candidates:
            raise ValidationError(f"{day.isoformat()} 之前无生效条款")
        return candidates[-1]

    def _terms_latest(self) -> Optional[TermsVersion]:
        return self.terms_versions[-1] if self.terms_versions else None

    # ------------------------------------------------------------------
    # 行情
    # ------------------------------------------------------------------
    def ingest_price(self, data: dict[str, Any],
                     auto_recompute: bool = True) -> PriceRecord:
        rec = PriceRecord.from_dict(data)
        for existing in self.prices.get((rec.underlying, rec.date.isoformat()), []):
            if existing.price_id == rec.price_id:
                if existing.to_dict() == rec.to_dict():
                    return existing  # 幂等：同一记录重复推送
                raise ConflictError(f"price_id {rec.price_id} 已存在且内容不同")
        if rec.status not in (PRICE_PROVISIONAL, PRICE_OFFICIAL, PRICE_REVOKED):
            raise ValidationError(f"未知行情状态 {rec.status}")
        if rec.status == PRICE_REVOKED and not rec.corrects:
            raise ValidationError("撤销行情必须指明 corrects（被撤销的 price_id）")
        self.store.append("price_ingested", rec.to_dict())
        self._fold({"type": "price_ingested", "payload": rec.to_dict()})
        if auto_recompute:
            self.recompute()
        return rec

    def _authoritative_price(self, ticker: str, day: date,
                             as_of: datetime) -> tuple[Optional[PriceRecord], str]:
        """按知识时间 as_of 选取权威行情：最新有效官方价优先，其次临时价。"""
        records = [r for r in self.prices.get((ticker, day.isoformat()), [])
                   if r.published_at <= as_of]
        officials = []
        for r in records:
            if r.status != PRICE_OFFICIAL:
                continue
            superseded = any(
                o.corrects == r.price_id and o.published_at <= as_of
                for o in records if o.status in (PRICE_OFFICIAL, PRICE_REVOKED)
            )
            if not superseded:
                officials.append(r)
        if officials:
            return max(officials, key=lambda r: r.version), "official"
        provisionals = [r for r in records if r.status == PRICE_PROVISIONAL]
        if provisionals:
            return max(provisionals, key=lambda r: r.version), "provisional"
        if records:
            return None, "revoked"
        return None, "missing"

    def _latest_level(self, ticker: str, as_of: datetime) -> Optional[dict[str, Any]]:
        """最新可得价位（官方优先），用于预计现金流。"""
        best: Optional[PriceRecord] = None
        best_quality = "missing"
        for (tk, _), records in self.prices.items():
            if tk != ticker:
                continue
            for r in records:
                if r.published_at > as_of:
                    continue
                rec, quality = self._authoritative_price(ticker, r.date, as_of)
                if rec is None or rec.price_id != r.price_id:
                    continue
                if best is None or r.date > best.date or (
                    r.date == best.date and quality == "official" and best_quality != "official"
                ):
                    best, best_quality = rec, quality
        if best is None:
            return None
        adj, _ = self._adjusted_initial(ticker, as_of.date())
        return {
            "ticker": ticker, "date": best.date.isoformat(), "value": best.value,
            "quality": best_quality, "price_id": best.price_id,
            "adjusted_initial": adj,
            "ratio": (best.value / adj) if best.value is not None else None,
        }

    # ------------------------------------------------------------------
    # 公司行动
    # ------------------------------------------------------------------
    def apply_corporate_action(self, data: dict[str, Any],
                               auto_recompute: bool = True) -> CorporateAction:
        ca = CorporateAction.from_dict(data)
        for existing in self.corp_actions:
            if existing.ca_id == ca.ca_id:
                if existing.to_dict() == ca.to_dict():
                    return existing
                raise ConflictError(f"公司行动 {ca.ca_id} 已存在且内容不同")
        self.store.append("corporate_action_applied", ca.to_dict())
        self._fold({"type": "corporate_action_applied", "payload": ca.to_dict()})
        if auto_recompute:
            self.recompute()
        return ca

    def _adjusted_initial(self, ticker: str, obs_day: date) -> tuple[Decimal, list[str]]:
        """obs_day 当日适用的调整后初始价，以及公司行动计算路径。"""
        terms = self._terms_on(obs_day)
        member = next((m for m in terms.payload["basket"] if m["ticker"] == ticker), None)
        if member is None:
            raise ValidationError(f"{ticker} 不在 {obs_day.isoformat()} 适用的篮子中")
        base = D(member["initial_price"])
        current = base
        path: list[str] = []
        relevant = sorted(
            (ca for ca in self.corp_actions
             if ca.underlying == ticker and ca.ex_date <= obs_day and ca.factor != 1),
            key=lambda ca: ca.ex_date,
        )
        for ca in relevant:
            new_value = current * ca.factor
            path.append(
                f"{ca.ex_date.isoformat()} {ca.kind}（因子 {dec_str(ca.factor)}）："
                f"初始价 {dec_str(current)} → {dec_str(new_value)}"
            )
            current = new_value
        return current, path

    # ------------------------------------------------------------------
    # 观察日程
    # ------------------------------------------------------------------
    def _build_schedule(self, terms: TermsVersion) -> list[dict[str, Any]]:
        payload = terms.payload
        scheduled_dates = sorted({
            date.fromisoformat(d) for d in payload["schedule"]["observation_dates"]
        } | {date.fromisoformat(payload["maturity_date"])})
        maturity = date.fromisoformat(payload["maturity_date"])
        schedule = []
        for sched in scheduled_dates:
            actual, roll_path = self.calendar.roll(sched, "following")
            schedule.append({
                "scheduled_date": sched,
                "date": actual,
                "roll_path": roll_path,
                "is_final": sched == maturity,
                "obs_id": f"{self.product_id}:{OBS_KIND_SCHEDULED}:{sched.isoformat()}",
            })
        return schedule

    # ------------------------------------------------------------------
    # 重算主流程
    # ------------------------------------------------------------------
    def recompute(self, as_of: Optional[datetime] = None) -> None:
        as_of = as_of or self.clock
        terms_latest = self._terms_latest()
        if terms_latest is None:
            return
        self.schedule = self._build_schedule(terms_latest)

        # 1) 逐日敲入观察（若此前已确认敲出，则窗口截断至敲出日）
        ki_state = self._eval_ki_window(as_of, cap=self._current_call_date())

        # 2) 预定观察日：评估票息 / 敲出 / 到期
        called = False
        call_item: Optional[dict[str, Any]] = None
        confirmed: list[Observation] = []
        for item in self.schedule:
            if called:
                self._cancel_scheduled(item, as_of,
                                       f"产品已于 {call_item['date'].isoformat()} 自动赎回")
                continue
            if item["date"] > as_of.date():
                continue  # 未来观察日：不建档，查询时显示 scheduled
            obs = self._eval_scheduled(item, ki_state, as_of)
            self._record_observation(obs)
            if obs.status == OBS_CONFIRMED:
                confirmed.append(obs)
                if obs.outcomes.get("autocall"):
                    called = True
                    call_item = item

        # 敲出日之后的逐日观察一次性取消（后续重算窗口已被 cap 截断，不会反复）
        if called and call_item is not None:
            self._cancel_ki_after(call_item["date"], as_of)

        # 3) 推导现金流（含预计、确认、差额调整）
        self._derive_cashflows(confirmed, ki_state, as_of)

        # 4) 产品状态
        final_item = self.schedule[-1]
        final_obs = self._latest_obs(final_item["obs_id"])
        if called:
            self.status_info = {
                "status": "called",
                "call_date": call_item["date"].isoformat(),
                "ki": ki_state,
            }
        elif final_obs is not None and final_obs.status == OBS_CONFIRMED:
            self.status_info = {"status": "matured", "ki": ki_state}
        else:
            self.status_info = {"status": "alive", "ki": ki_state}

    def _current_call_date(self) -> Optional[date]:
        """已确认观察中最早的敲出日（用于截断敲入窗口，保证重算幂等）。"""
        dates = [
            versions[-1].date for versions in self.observations.values()
            if versions[-1].kind == OBS_KIND_SCHEDULED
            and versions[-1].status == OBS_CONFIRMED
            and versions[-1].outcomes.get("autocall")
        ]
        return min(dates) if dates else None

    def _cancel_ki_after(self, call_date: date, as_of: datetime) -> None:
        for obs_id, versions in sorted(self.observations.items()):
            obs = versions[-1]
            if obs.kind == OBS_KIND_KI and obs.date > call_date \
                    and obs.status != OBS_CANCELLED:
                self._record_observation(Observation(
                    obs_id=obs.obs_id, product_id=obs.product_id, kind=obs.kind,
                    scheduled_date=obs.scheduled_date, date=obs.date,
                    roll_path=obs.roll_path, terms_version=obs.terms_version,
                    status=OBS_CANCELLED, evidence=obs.evidence,
                    outcomes={"cancel_reason": f"产品已于 {call_date.isoformat()} 自动赎回"},
                    calc_path=[], version=obs.version + 1, computed_at=as_of,
                ))

    # ------------------------------------------------------------------
    def _eval_ki_window(self, as_of: datetime, cap: Optional[date] = None) -> dict[str, Any]:
        terms_latest = self._terms_latest()
        window = terms_latest.payload["schedule"]["ki_window"]
        start = date.fromisoformat(window["start"])
        end = min(date.fromisoformat(window["end"]), as_of.date())
        if cap is not None:
            end = min(end, cap)
        triggered = False
        first_date: Optional[str] = None
        pending_days: list[str] = []
        for day in self.calendar.business_days_between(start, end):
            obs = self._eval_ki_day(day, as_of)
            self._record_observation(obs)
            if obs.status == OBS_CONFIRMED:
                if obs.outcomes.get("breached") and not triggered:
                    triggered = True
                    first_date = day.isoformat()
            else:
                pending_days.append(day.isoformat())
        return {"triggered": triggered, "first_breach_date": first_date,
                "pending_days": pending_days}

    def _eval_ki_day(self, day: date, as_of: datetime) -> Observation:
        terms = self._terms_on(day)
        ki_ratio = D(terms.payload["barriers"]["knock_in"]["ratio"])
        evidence: dict[str, Any] = {}
        breached: list[str] = []
        pending: list[str] = []
        calc: list[str] = []
        for member in terms.payload["basket"]:
            ticker = member["ticker"]
            adj_init, adj_path = self._adjusted_initial(ticker, day)
            level = adj_init * ki_ratio
            rec, quality = self._authoritative_price(ticker, day, as_of)
            entry: dict[str, Any] = {
                "price_quality": quality,
                "adjusted_initial": dec_str(adj_init),
                "adjustment_path": adj_path,
                "barrier_levels": {"knock_in": dec_str(level)},
                "terms_version": terms.version,
            }
            if quality == "official" and rec is not None:
                ratio = rec.value / adj_init
                hit = rec.value < level
                entry["price"] = self._price_snapshot(rec)
                entry["ratio"] = self._fmt_ratio(ratio)
                entry["breached"] = hit
                calc.append(
                    f"{ticker} {day.isoformat()} 官方收盘 {dec_str(rec.value)}"
                    f"（{rec.price_id} v{rec.version}）/ 调整后初始价 {dec_str(adj_init)}"
                    f" = {self._fmt_ratio(ratio)}；敲入位 {dec_str(level)}"
                    f" → {'触发敲入' if hit else '未触发'}"
                )
                if hit:
                    breached.append(ticker)
            else:
                if rec is not None:
                    entry["indicative_price"] = self._price_snapshot(rec)
                pending.append(f"{ticker} {day.isoformat()}：{_QUALITY_CN[quality]}")
            evidence[ticker] = entry
        status = OBS_CONFIRMED if not pending else OBS_PENDING
        return Observation(
            obs_id=f"{self.product_id}:{OBS_KIND_KI}:{day.isoformat()}",
            product_id=self.product_id, kind=OBS_KIND_KI,
            scheduled_date=day, date=day, roll_path=[],
            terms_version=terms.version, status=status,
            evidence=evidence,
            outcomes={"breached": bool(breached), "breach_tickers": breached},
            calc_path=calc, version=1, computed_at=as_of,
            pending_reasons=pending,
        )

    def _eval_scheduled(self, item: dict[str, Any], ki_state: dict[str, Any],
                        as_of: datetime) -> Observation:
        terms = self._terms_on(item["date"])
        payload = terms.payload
        barriers = payload["barriers"]
        coupon_ratio = D(barriers["coupon"]["ratio"])
        autocall_ratio = D(barriers["autocall"]["ratio"])
        ki_ratio = D(barriers["knock_in"]["ratio"])
        evidence: dict[str, Any] = {}
        ratios: dict[str, Decimal] = {}
        pending: list[str] = []
        calc: list[str] = [
            f"观察日 {item['date'].isoformat()}（原定 {item['scheduled_date'].isoformat()}）",
            *item["roll_path"],
            f"适用条款版本 v{terms.version}（{terms.effective_from.isoformat()} 起生效）",
        ]
        for member in payload["basket"]:
            ticker = member["ticker"]
            adj_init, adj_path = self._adjusted_initial(ticker, item["date"])
            rec, quality = self._authoritative_price(ticker, item["date"], as_of)
            entry = {
                "price_quality": quality,
                "adjusted_initial": dec_str(adj_init),
                "adjustment_path": adj_path,
                "barrier_levels": {
                    "knock_in": dec_str(adj_init * ki_ratio),
                    "coupon": dec_str(adj_init * coupon_ratio),
                    "autocall": dec_str(adj_init * autocall_ratio),
                },
                "terms_version": terms.version,
            }
            if quality == "official" and rec is not None:
                ratio = rec.value / adj_init
                ratios[ticker] = ratio
                entry["price"] = self._price_snapshot(rec)
                entry["ratio"] = self._fmt_ratio(ratio)
                calc.append(
                    f"{ticker} 官方收盘 {dec_str(rec.value)}"
                    f"（{rec.price_id} v{rec.version}）/ 调整后初始价 {dec_str(adj_init)}"
                    f" = {self._fmt_ratio(ratio)}"
                )
            else:
                if rec is not None:
                    entry["indicative_price"] = self._price_snapshot(rec)
                    calc.append(
                        f"{ticker} 暂无官方收盘价（{_QUALITY_CN[quality]}），观察待确认"
                    )
                else:
                    calc.append(f"{ticker} {_QUALITY_CN[quality]}，观察待确认")
                pending.append(f"{ticker} {item['date'].isoformat()}：{_QUALITY_CN[quality]}")
            evidence[ticker] = entry

        outcomes: dict[str, Any] = {"is_final": item["is_final"]}
        if not pending:
            worst_ticker = min(ratios, key=lambda t: (ratios[t], t))
            worst = ratios[worst_ticker]
            coupon_due = worst >= coupon_ratio
            autocall = worst >= autocall_ratio
            outcomes.update({
                "ratios": {t: self._fmt_ratio(r) for t, r in ratios.items()},
                "worst_of": {"ticker": worst_ticker, "ratio": self._fmt_ratio(worst)},
                "coupon_due": coupon_due,
                "autocall": autocall,
                "ki_triggered_as_of": ki_state["triggered"],
                "first_ki_date": ki_state["first_breach_date"],
            })
            calc.append(f"最差标的 {worst_ticker}，比率 {self._fmt_ratio(worst)}")
            calc.append(
                f"票息障碍 {dec_str(coupon_ratio)} → {self._fmt_ratio(worst)} "
                f"{'≥' if coupon_due else '<'} {dec_str(coupon_ratio)}，"
                f"{'票息应付' if coupon_due else '票息不付（计入记忆）'}"
            )
            calc.append(
                f"敲出障碍 {dec_str(autocall_ratio)} → {self._fmt_ratio(worst)} "
                f"{'≥' if autocall else '<'} {dec_str(autocall_ratio)}，"
                f"{'触发自动赎回' if autocall else '未敲出'}"
            )
        status = OBS_CONFIRMED if not pending else OBS_PENDING
        return Observation(
            obs_id=item["obs_id"], product_id=self.product_id,
            kind=OBS_KIND_SCHEDULED,
            scheduled_date=item["scheduled_date"], date=item["date"],
            roll_path=item["roll_path"], terms_version=terms.version,
            status=status, evidence=evidence, outcomes=outcomes,
            calc_path=calc, version=1, computed_at=as_of,
            pending_reasons=pending,
        )

    def _cancel_scheduled(self, item: dict[str, Any], as_of: datetime,
                          reason: str) -> None:
        latest = self._latest_obs(item["obs_id"])
        if latest is not None and latest.status == OBS_CANCELLED:
            return
        obs = Observation(
            obs_id=item["obs_id"], product_id=self.product_id,
            kind=OBS_KIND_SCHEDULED,
            scheduled_date=item["scheduled_date"], date=item["date"],
            roll_path=item["roll_path"],
            terms_version=latest.terms_version if latest else self._terms_latest().version,
            status=OBS_CANCELLED, evidence={}, outcomes={"cancel_reason": reason},
            calc_path=[reason], version=(latest.version + 1) if latest else 1,
            computed_at=as_of,
        )
        self._record_observation(obs)

    # ------------------------------------------------------------------
    # 现金流推导
    # ------------------------------------------------------------------
    def _derive_cashflows(self, confirmed: list[Observation], ki_state: dict[str, Any],
                          as_of: datetime) -> None:
        desired: dict[str, _DesiredCF] = {}
        backlog = 0
        stopped = False
        for obs in confirmed:
            if stopped:
                break
            terms = self._terms_on(obs.date)
            payload = terms.payload
            notional = D(payload["notional"])
            rate_per = D(payload["coupon"]["annual_rate"]) / _FREQUENCIES[payload["coupon"]["frequency"]]
            lag = int(payload.get("settlement_lag_days", 5))
            value_date, vd_path = self.calendar.add_business_days(obs.date, lag)

            if obs.outcomes.get("coupon_due"):
                periods = 1 + backlog
                amount = self._round(notional * rate_per * periods)
                calc = list(obs.calc_path) + [
                    f"票息 = 本金 {dec_str(notional)} × 单期利率 {dec_str(rate_per)}"
                    f" × {periods} 期（含记忆补付 {backlog} 期）= {dec_str(amount)}",
                    f"付款日：观察日 {obs.date.isoformat()} 起 {lag} 个交易日"
                    f" → {value_date.isoformat()}",
                    *vd_path,
                ]
                desired[f"coupon:{obs.scheduled_date.isoformat()}"] = _DesiredCF(
                    key=f"coupon:{obs.scheduled_date.isoformat()}",
                    type=CF_COUPON, state=CF_CONFIRMED, amount=amount,
                    value_date=value_date,
                    lineage=self._lineage(obs, terms, calc,
                                          {"memory_backlog": backlog, "periods_paid": periods}),
                )
                backlog = 0
            else:
                backlog += 1

            if obs.outcomes.get("autocall"):
                redemption_ratio = D(payload["autocall"]["redemption_ratio"])
                amount = self._round(notional * redemption_ratio)
                calc = list(obs.calc_path) + [
                    f"自动赎回 = 本金 {dec_str(notional)} × 赎回比例 {dec_str(redemption_ratio)}"
                    f" = {dec_str(amount)}",
                    f"付款日：观察日 {obs.date.isoformat()} 起 {lag} 个交易日"
                    f" → {value_date.isoformat()}",
                ]
                desired["redemption:autocall"] = _DesiredCF(
                    key="redemption:autocall", type=CF_REDEMPTION, state=CF_CONFIRMED,
                    amount=amount, value_date=value_date,
                    lineage=self._lineage(obs, terms, calc, {}),
                )
                stopped = True
            elif obs.outcomes.get("is_final"):
                self._derive_maturity(desired, obs, terms, ki_state, value_date, vd_path)

        # 预计现金流：产品存续期间，对未确认的观察日按当前可得行情估计
        if not stopped:
            final_obs = self._latest_obs(self.schedule[-1]["obs_id"])
            matured = final_obs is not None and final_obs.status == OBS_CONFIRMED
            if not matured:
                self._derive_projected(desired, backlog, ki_state, as_of)

        self._apply_desired(desired, as_of)

    def _derive_maturity(self, desired: dict[str, _DesiredCF], obs: Observation,
                         terms: TermsVersion, ki_state: dict[str, Any],
                         value_date: date, vd_path: list[str]) -> None:
        payload = terms.payload
        notional = D(payload["notional"])
        protection = D(payload["maturity"].get("protection_ratio", "1"))
        worst = D(obs.outcomes["worst_of"]["ratio"])
        worst_ticker = obs.outcomes["worst_of"]["ticker"]
        knocked_in = ki_state["triggered"] and worst < protection
        calc = list(obs.calc_path)
        if knocked_in:
            settlement = payload["maturity"].get("knocked_in_settlement", "cash")
            if settlement == "physical":
                adj_init, adj_path = self._adjusted_initial(worst_ticker, obs.date)
                close = D(obs.evidence[worst_ticker]["price"]["value"])
                lot = int(payload["maturity"].get("lot_size", 1))
                exact = notional / adj_init
                units = (exact // lot) * lot
                fraction = exact - units
                cash_in_lieu = self._round(fraction * close)
                calc += [
                    f"已敲入且期末比率 {dec_str(worst)} < 保护比例 {dec_str(protection)}，实物交付",
                    f"理论股数 = 本金 {dec_str(notional)} / 调整后初始价 {dec_str(adj_init)}"
                    f" = {dec_str(exact)}",
                    f"整股 {int(units)} 股（手数 {lot}），零股 {dec_str(fraction)} 股"
                    f" 按期末收盘 {dec_str(close)} 折现 = {dec_str(cash_in_lieu)}",
                    *adj_path,
                ]
                desired["delivery:maturity"] = _DesiredCF(
                    key="delivery:maturity", type=CF_DELIVERY, state=CF_CONFIRMED,
                    amount=cash_in_lieu, value_date=value_date,
                    lineage=self._lineage(obs, terms, calc, {}),
                    delivery={
                        "ticker": worst_ticker, "shares": int(units),
                        "fractional_shares": dec_str(fraction),
                        "cash_in_lieu": dec_str(cash_in_lieu),
                        "close": dec_str(close), "adjusted_initial": dec_str(adj_init),
                    },
                )
            else:
                amount = self._round(notional * worst)
                calc.append(
                    f"已敲入且期末比率 {dec_str(worst)} < 保护比例 {dec_str(protection)}，"
                    f"现金结算 = 本金 {dec_str(notional)} × {dec_str(worst)} = {dec_str(amount)}"
                )
                desired["redemption:maturity"] = _DesiredCF(
                    key="redemption:maturity", type=CF_REDEMPTION, state=CF_CONFIRMED,
                    amount=amount, value_date=value_date,
                    lineage=self._lineage(obs, terms, calc, {}),
                )
        else:
            amount = self._round(notional * protection)
            reason = (f"未敲入" if not ki_state["triggered"]
                      else f"已敲入但期末比率 {dec_str(worst)} ≥ 保护比例 {dec_str(protection)}")
            calc.append(
                f"{reason}，按保护比例兑付 = 本金 {dec_str(notional)}"
                f" × {dec_str(protection)} = {dec_str(amount)}"
            )
            desired["redemption:maturity"] = _DesiredCF(
                key="redemption:maturity", type=CF_REDEMPTION, state=CF_CONFIRMED,
                amount=amount, value_date=value_date,
                lineage=self._lineage(obs, terms, calc, {}),
            )

    def _derive_projected(self, desired: dict[str, _DesiredCF], backlog: int,
                          ki_state: dict[str, Any], as_of: datetime) -> None:
        terms = self._terms_latest()
        payload = terms.payload
        notional = D(payload["notional"])
        rate_per = D(payload["coupon"]["annual_rate"]) / _FREQUENCIES[payload["coupon"]["frequency"]]
        lag = int(payload.get("settlement_lag_days", 5))
        coupon_ratio = D(payload["barriers"]["coupon"]["ratio"])
        autocall_ratio = D(payload["barriers"]["autocall"]["ratio"])
        protection = D(payload["maturity"].get("protection_ratio", "1"))

        levels = {m["ticker"]: self._latest_level(m["ticker"], as_of)
                  for m in payload["basket"]}
        known = [lv for lv in levels.values() if lv and lv["ratio"] is not None]
        worst = min((lv["ratio"] for lv in known), default=None)
        assumptions = [
            f"{t}：{lv['date']} {lv['quality']} 价 {dec_str(lv['value'])}"
            f"（比率 {self._fmt_ratio(lv['ratio'])}）" if lv else f"{t}：无可用行情"
            for t, lv in levels.items()
        ]

        for item in self.schedule:
            obs = self._latest_obs(item["obs_id"])
            if obs is not None and obs.status == OBS_CONFIRMED:
                continue  # 已确认的期间由确认现金流覆盖
            value_date, _ = self.calendar.add_business_days(item["date"], lag)
            est_due = worst is None or worst >= coupon_ratio
            periods = 1 + backlog
            amount = self._round(notional * rate_per * periods) if est_due else Decimal("0.00")
            desired[f"coupon:{item['scheduled_date'].isoformat()}"] = _DesiredCF(
                key=f"coupon:{item['scheduled_date'].isoformat()}",
                type=CF_COUPON, state=CF_PROJECTED, amount=amount,
                value_date=value_date,
                lineage={
                    "terms_version": terms.version,
                    "assumptions": assumptions + [
                        f"按当前最差比率 "
                        f"{self._fmt_ratio(worst) if worst is not None else '未知'}"
                        f" 与票息障碍 {dec_str(coupon_ratio)} 估计，"
                        f"记忆待补 {backlog} 期，实际以观察日官方收盘为准"
                    ],
                    "calc_path": [],
                    "observations": [],
                },
            )
            if worst is not None and worst >= autocall_ratio:
                amount_r = self._round(notional * D(payload["autocall"]["redemption_ratio"]))
                desired["redemption:autocall"] = _DesiredCF(
                    key="redemption:autocall", type=CF_REDEMPTION, state=CF_PROJECTED,
                    amount=amount_r, value_date=value_date,
                    lineage={"terms_version": terms.version,
                             "assumptions": assumptions, "calc_path": [], "observations": []},
                )
                return  # 预计敲出后不再预计更后期现金流

        # 到期预计（未预计敲出时）
        final_item = self.schedule[-1]
        value_date, _ = self.calendar.add_business_days(final_item["date"], lag)
        if ki_state["triggered"] and worst is not None and worst < protection:
            amount_m = self._round(notional * worst)
            note = f"已敲入且当前最差比率 {self._fmt_ratio(worst)} < 保护比例，按比率预计"
        else:
            amount_m = self._round(notional * protection)
            note = "未敲入（或比率不低于保护比例），按保护比例预计"
        desired["redemption:maturity"] = _DesiredCF(
            key="redemption:maturity", type=CF_REDEMPTION, state=CF_PROJECTED,
            amount=amount_m, value_date=value_date,
            lineage={"terms_version": terms.version,
                     "assumptions": assumptions + [note], "calc_path": [], "observations": []},
        )

    # ------------------------------------------------------------------
    def _apply_desired(self, desired: dict[str, _DesiredCF], as_of: datetime) -> None:
        # 1) 不再应付的现金流：未付取消，已付调整到零
        for key, versions in list(self.cashflows.items()):
            latest = versions[-1]
            if latest.type == CF_ADJUSTMENT or key in desired:
                continue
            if latest.state in (CF_PROJECTED, CF_CONFIRMED):
                self._append_cf(replace(
                    latest, state=CF_CANCELLED, version=latest.version + 1,
                    updated_at=as_of,
                    lineage={**latest.lineage, "cancel_reason": "重算后不再应付"},
                ))
            elif latest.state in (CF_PAID, CF_ADJUSTED):
                if self._basis_pending(latest):
                    self._flag_under_review(latest, as_of)
                else:
                    if latest.lineage.get("under_review"):
                        latest = self._append_cf(replace(
                            latest, version=latest.version + 1, updated_at=as_of,
                            lineage={k: v for k, v in latest.lineage.items()
                                     if k != "under_review"},
                        ))
                    self._ensure_adjustment(latest, Decimal("0.00"), as_of,
                                            "重算后该笔不再应付，差额调整至零")
        # 2) 新建 / 更新期望现金流
        for key, d in desired.items():
            versions = self.cashflows.get(key)
            if not versions:
                self._append_cf(Cashflow(
                    cf_id=f"{self.product_id}:{key}", key=key,
                    product_id=self.product_id, type=d.type, state=d.state,
                    amount=d.amount, currency=self._currency(),
                    value_date=d.value_date, version=1, lineage=d.lineage,
                    updated_at=as_of, delivery=d.delivery,
                ))
                continue
            latest = versions[-1]
            if latest.state in (CF_PAID, CF_ADJUSTED):
                if self._basis_pending(latest):
                    self._flag_under_review(latest, as_of)
                else:
                    if latest.lineage.get("under_review"):
                        latest = self._append_cf(replace(
                            latest, version=latest.version + 1, updated_at=as_of,
                            lineage={k: v for k, v in latest.lineage.items()
                                     if k != "under_review"},
                        ))
                    self._ensure_adjustment(latest, d.amount, as_of,
                                            "条款/行情更正后重算，差额调整")
                continue
            if latest.state == CF_CANCELLED:
                self._append_cf(Cashflow(  # 取消后重新需要 → 新开版本链
                    cf_id=f"{self.product_id}:{key}", key=key,
                    product_id=self.product_id, type=d.type, state=d.state,
                    amount=d.amount, currency=self._currency(),
                    value_date=d.value_date, version=latest.version + 1,
                    lineage=d.lineage, updated_at=as_of, delivery=d.delivery,
                ))
                continue
            if self._cf_signature(latest) != self._desired_signature(d):
                self._append_cf(replace(
                    latest, state=d.state, amount=d.amount, value_date=d.value_date,
                    version=latest.version + 1, lineage=d.lineage,
                    updated_at=as_of, delivery=d.delivery,
                ))

    def _ensure_adjustment(self, orig: Cashflow, desired_amount: Decimal,
                           as_of: datetime, reason: str) -> None:
        """已付款项金额变化时，以差额调整承接；原记录永不抹除。"""
        paid_total = orig.amount
        open_adj: Optional[Cashflow] = None
        seq = 0
        for key, versions in self.cashflows.items():
            if not key.startswith(f"adj:{orig.key}:"):
                continue
            adj = versions[-1]
            seq = max(seq, int(key.rsplit(":", 1)[1]))
            if adj.state == CF_PAID:
                paid_total += adj.amount
            elif adj.state in (CF_PROJECTED, CF_CONFIRMED):
                open_adj = adj
        net = self._round(desired_amount - paid_total)
        calc = [
            f"{reason}",
            f"重算应付 {dec_str(desired_amount)} - 已付合计 {dec_str(paid_total)}"
            f" = 差额 {dec_str(net)}",
        ]
        if open_adj is not None:
            if net == Decimal("0.00"):
                self._append_cf(replace(
                    open_adj, state=CF_CANCELLED, version=open_adj.version + 1,
                    updated_at=as_of,
                    lineage={**open_adj.lineage, "cancel_reason": "差额已归零"},
                ))
            elif open_adj.amount != net:
                self._append_cf(replace(
                    open_adj, amount=net, version=open_adj.version + 1,
                    updated_at=as_of,
                    lineage={**open_adj.lineage, "calc_path": calc},
                ))
        elif net != Decimal("0.00"):
            key = f"adj:{orig.key}:{seq + 1}"
            value_date, _ = self.calendar.roll(as_of.date(), "following")
            self._append_cf(Cashflow(
                cf_id=f"{self.product_id}:{key}", key=key,
                product_id=self.product_id, type=CF_ADJUSTMENT, state=CF_CONFIRMED,
                amount=net, currency=self._currency(), value_date=value_date,
                version=1, updated_at=as_of,
                lineage={
                    "adjusts": orig.key,
                    "reason": reason,
                    "terms_version": self._terms_latest().version,
                    "calc_path": calc,
                    "observations": [],
                },
                links={"adjusts": orig.key},
            ))
        # 原始现金流状态联动：存在有效调整 → adjusted；调整全部冲销 → 回到 paid
        has_live_adj = any(
            versions[-1].state in (CF_CONFIRMED, CF_PAID)
            for key, versions in self.cashflows.items()
            if key.startswith(f"adj:{orig.key}:")
        )
        current = self.cashflows[orig.key][-1]
        if current.state == CF_PAID and has_live_adj:
            self._append_cf(replace(current, state=CF_ADJUSTED,
                                    version=current.version + 1, updated_at=as_of))
        elif current.state == CF_ADJUSTED and not has_live_adj:
            self._append_cf(replace(current, state=CF_PAID,
                                    version=current.version + 1, updated_at=as_of))

    def _basis_pending(self, cf: Cashflow) -> bool:
        """已付现金流的观察依据是否因行情撤销等原因回到待确认。"""
        for ref in cf.lineage.get("observations", []):
            obs = self._latest_obs(ref["obs_id"])
            if obs is not None and obs.status == OBS_PENDING:
                return True
        return False

    def _flag_under_review(self, cf: Cashflow, as_of: datetime) -> None:
        if cf.lineage.get("under_review"):
            return
        self._append_cf(replace(
            cf, version=cf.version + 1, updated_at=as_of,
            lineage={**cf.lineage,
                     "under_review": "观察依据的行情被撤销/更正，复核中，暂不调整"},
        ))

    # ------------------------------------------------------------------
    def _lineage(self, obs: Observation, terms: TermsVersion,
                 calc_path: list[str], extra: dict[str, Any]) -> dict[str, Any]:
        return {
            "terms_version": terms.version,
            "observations": [{"obs_id": obs.obs_id, "version": obs.version}],
            "evidence": obs.evidence,
            "calc_path": calc_path,
            **extra,
        }

    @staticmethod
    def _price_snapshot(rec: PriceRecord) -> dict[str, Any]:
        return {
            "price_id": rec.price_id, "version": rec.version,
            "status": rec.status, "value": dec_str(rec.value) if rec.value is not None else None,
            "price_type": rec.price_type, "published_at": rec.published_at.isoformat(),
            "source": rec.source, "corrects": rec.corrects,
        }

    def _round(self, amount: Decimal) -> Decimal:
        quantum = Decimal("1").scaleb(-int(self.precision.get("amount_decimals", 2)))
        return amount.quantize(quantum, rounding=ROUND_HALF_UP)

    def _fmt_ratio(self, ratio: Decimal) -> str:
        """比率序列化精度（domain.json precision.ratio_decimals），比较仍用精确值。"""
        quantum = Decimal("1").scaleb(-int(self.precision.get("ratio_decimals", 8)))
        return dec_str(ratio.quantize(quantum, rounding=ROUND_HALF_UP))

    def _currency(self) -> str:
        terms = self._terms_latest()
        return terms.payload["currency"] if terms else "CNY"

    # ------------------------------------------------------------------
    # 观察 / 现金流落库（差异才追加新版本）
    # ------------------------------------------------------------------
    def _record_observation(self, obs: Observation) -> Observation:
        versions = self.observations.setdefault(obs.obs_id, [])
        if versions:
            latest = versions[-1]
            if self._obs_signature(latest) == self._obs_signature(obs):
                return latest
            obs.version = latest.version + 1
        self.store.append("observation_recorded", obs.to_dict())
        versions.append(obs)
        return obs

    def _append_cf(self, cf: Cashflow) -> Cashflow:
        self.store.append("cashflow_recorded", cf.to_dict())
        self.cashflows.setdefault(cf.key, []).append(cf)
        return cf

    @staticmethod
    def _obs_signature(obs: Observation) -> tuple:
        return (
            obs.status, obs.terms_version,
            json.dumps(obs.evidence, sort_keys=True, ensure_ascii=False),
            json.dumps(obs.outcomes, sort_keys=True, ensure_ascii=False),
        )

    def _cf_signature(self, cf: Cashflow) -> tuple:
        return (
            cf.type, cf.state, dec_str(cf.amount), cf.value_date.isoformat(),
            cf.lineage.get("terms_version"),
            json.dumps(cf.delivery, sort_keys=True, ensure_ascii=False),
            json.dumps(
                [(o["obs_id"], o["version"]) for o in cf.lineage.get("observations", [])],
                ensure_ascii=False,
            ),
        )

    def _desired_signature(self, d: _DesiredCF) -> tuple:
        return (
            d.type, d.state, dec_str(d.amount), d.value_date.isoformat(),
            d.lineage.get("terms_version"),
            json.dumps(d.delivery, sort_keys=True, ensure_ascii=False),
            json.dumps(
                [(o["obs_id"], o["version"]) for o in d.lineage.get("observations", [])],
                ensure_ascii=False,
            ),
        )

    # ------------------------------------------------------------------
    # 结算（幂等）
    # ------------------------------------------------------------------
    def settle(self, cf_id: str, instruction_id: str, actor: str = "ops",
               at: Optional[datetime] = None) -> Settlement:
        at = at or self.clock
        if instruction_id in self.settlements:
            existing = self.settlements[instruction_id]
            if existing.cf_id != cf_id:
                raise ConflictError(
                    f"结算指令 {instruction_id} 已用于 {existing.cf_id}，不能挪作他用"
                )
            return existing  # 幂等重放：同一指令重试返回原结果
        key = cf_id.split(":", 1)[1] if cf_id.startswith(self.product_id) else cf_id
        versions = self.cashflows.get(key)
        if not versions:
            raise NotFoundError(f"现金流不存在：{cf_id}")
        latest = versions[-1]
        if latest.state in (CF_PAID, CF_ADJUSTED):
            raise ConflictError(f"{cf_id} 已付款，不能重复支付")
        if latest.state != CF_CONFIRMED:
            raise ValidationError(f"{cf_id} 状态为 {latest.state}，仅 confirmed 可结算")
        if at.date() < latest.value_date:
            raise ValidationError(
                f"{cf_id} 付款日 {latest.value_date.isoformat()} 未到，不能结算"
            )
        settlement = Settlement(
            instruction_id=instruction_id, cf_id=latest.cf_id,
            amount=latest.amount, currency=latest.currency,
            settled_at=at, actor=actor, result="paid",
        )
        self.store.append("settlement_recorded", settlement.to_dict())
        self.settlements[instruction_id] = settlement
        self._append_cf(replace(
            latest, state=CF_PAID, version=latest.version + 1, updated_at=at,
            lineage={**latest.lineage, "settled_by": instruction_id},
        ))
        return settlement

    # ------------------------------------------------------------------
    # 查询辅助
    # ------------------------------------------------------------------
    def _latest_obs(self, obs_id: str) -> Optional[Observation]:
        versions = self.observations.get(obs_id)
        return versions[-1] if versions else None

    def latest_cashflows(self) -> list[Cashflow]:
        return [versions[-1] for versions in self.cashflows.values()]

    def get_cashflow(self, cf_id: str) -> Cashflow:
        key = cf_id.split(":", 1)[1] if cf_id.startswith(self.product_id) else cf_id
        versions = self.cashflows.get(key)
        if not versions:
            raise NotFoundError(f"现金流不存在：{cf_id}")
        return versions[-1]

    def cashflow_history(self, cf_id: str) -> list[Cashflow]:
        key = cf_id.split(":", 1)[1] if cf_id.startswith(self.product_id) else cf_id
        return list(self.cashflows.get(key, []))
