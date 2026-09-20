"""领域模型：结构性票据核算引擎的不可变记录与值对象。

约定：
- 金额与比率一律使用 Decimal，JSON 序列化为字符串，避免浮点误差。
- 每条记录都携带业务时间（date/published_at）与版本号，系统接收时间由事件日志补充。
- 观察、现金流的“版本”通过 (id, version) 唯一定位；历史版本永不改写。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional


# ---------------------------------------------------------------------------
# 基础值类型
# ---------------------------------------------------------------------------

def D(value: Any) -> Decimal:
    """统一构造 Decimal，字符串直通，整数/浮点先转字符串。"""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def dec_str(value: Decimal) -> str:
    return format(value, "f")


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


# 价格记录状态
PRICE_PROVISIONAL = "provisional"   # 临时价：只能进入待确认
PRICE_OFFICIAL = "official"         # 官方收盘价：可作为权威行情
PRICE_REVOKED = "revoked"           # 已撤销：该版本作废

# 观察状态
OBS_SCHEDULED = "scheduled"
OBS_PENDING = "pending"             # 待确认（缺价 / 临时价 / 行情被撤销）
OBS_CONFIRMED = "confirmed"
OBS_CANCELLED = "cancelled"

# 现金流状态
CF_PROJECTED = "projected"
CF_CONFIRMED = "confirmed"
CF_PAID = "paid"
CF_ADJUSTED = "adjusted"            # 已付款项被后续更正，差额由调整现金流承接
CF_CANCELLED = "cancelled"

# 现金流类型
CF_COUPON = "coupon"
CF_REDEMPTION = "redemption"        # 自动赎回 / 到期本金
CF_DELIVERY = "delivery"            # 到期实物交付（股票 + 现金找零）
CF_ADJUSTMENT = "adjustment"        # 差额调整

# 观察类型
OBS_KIND_KI = "ki_daily"            # 敲入逐日观察
OBS_KIND_SCHEDULED = "scheduled"    # 票息 / 敲出 / 到期观察


# ---------------------------------------------------------------------------
# 产品条款（版本化）
# ---------------------------------------------------------------------------

@dataclass
class TermsVersion:
    """条款版本。effective_from 起生效，下一版本生效时本版本自然失效。"""
    product_id: str
    version: int
    effective_from: date
    payload: dict[str, Any]
    registered_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "product_id": self.product_id,
            "version": self.version,
            "effective_from": self.effective_from.isoformat(),
            "payload": self.payload,
            "registered_at": self.registered_at.isoformat(),
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "TermsVersion":
        return TermsVersion(
            product_id=data["product_id"],
            version=int(data["version"]),
            effective_from=parse_date(data["effective_from"]),
            payload=data["payload"],
            registered_at=parse_dt(data["registered_at"]),
        )


# ---------------------------------------------------------------------------
# 行情（版本化，支持临时价 / 官方价 / 撤销 / 更正）
# ---------------------------------------------------------------------------

@dataclass
class PriceRecord:
    price_id: str
    underlying: str
    date: date                    # 行情所属交易日
    value: Optional[Decimal]      # 撤销版本为 None
    price_type: str               # official_close / adjusted_close ...
    status: str                   # provisional / official / revoked
    version: int                  # 同一 (underlying, date) 下递增
    published_at: datetime        # 行情发布时间（知识时间）
    source: str
    corrects: Optional[str] = None   # 指向被更正的 price_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "price_id": self.price_id,
            "underlying": self.underlying,
            "date": self.date.isoformat(),
            "value": None if self.value is None else dec_str(self.value),
            "price_type": self.price_type,
            "status": self.status,
            "version": self.version,
            "published_at": self.published_at.isoformat(),
            "source": self.source,
            "corrects": self.corrects,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "PriceRecord":
        value = data.get("value")
        return PriceRecord(
            price_id=data["price_id"],
            underlying=data["underlying"],
            date=parse_date(data["date"]),
            value=None if value is None else D(value),
            price_type=data["price_type"],
            status=data["status"],
            version=int(data["version"]),
            published_at=parse_dt(data["published_at"]),
            source=data.get("source", "unknown"),
            corrects=data.get("corrects"),
        )


# ---------------------------------------------------------------------------
# 公司行动 / 篮子变更
# ---------------------------------------------------------------------------

@dataclass
class CorporateAction:
    ca_id: str
    underlying: str
    ex_date: date
    kind: str                     # split / cash_dividend / basket_change ...
    factor: Decimal               # 对初始价的调整因子（如 1拆2 → 0.5）
    payload: dict[str, Any]
    recorded_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "ca_id": self.ca_id,
            "underlying": self.underlying,
            "ex_date": self.ex_date.isoformat(),
            "kind": self.kind,
            "factor": dec_str(self.factor),
            "payload": self.payload,
            "recorded_at": self.recorded_at.isoformat(),
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "CorporateAction":
        return CorporateAction(
            ca_id=data["ca_id"],
            underlying=data["underlying"],
            ex_date=parse_date(data["ex_date"]),
            kind=data["kind"],
            factor=D(data["factor"]),
            payload=data.get("payload", {}),
            recorded_at=parse_dt(data["recorded_at"]),
        )


# ---------------------------------------------------------------------------
# 观察（逐日证据 + 障碍判定）
# ---------------------------------------------------------------------------

@dataclass
class Observation:
    """一次观察的一个版本。evidence 固定当时适用的条款版本与行情版本。"""
    obs_id: str                   # {product}:{kind}:{原定日期}
    product_id: str
    kind: str                     # ki_daily / scheduled
    scheduled_date: date          # 条款原定观察日
    date: date                    # 实际观察日（节假日顺延后的）
    roll_path: list[str]          # 顺延计算路径
    terms_version: int
    status: str                   # pending / confirmed / cancelled
    evidence: dict[str, Any]      # {ticker: {price 版本、调整后初始价、障碍位、比率、公司行动路径}}
    outcomes: dict[str, Any]      # 障碍判定结果
    calc_path: list[str]          # 人类可读的计算路径
    version: int
    computed_at: datetime
    pending_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "obs_id": self.obs_id,
            "product_id": self.product_id,
            "kind": self.kind,
            "scheduled_date": self.scheduled_date.isoformat(),
            "date": self.date.isoformat(),
            "roll_path": list(self.roll_path),
            "terms_version": self.terms_version,
            "status": self.status,
            "evidence": self.evidence,
            "outcomes": self.outcomes,
            "calc_path": list(self.calc_path),
            "version": self.version,
            "computed_at": self.computed_at.isoformat(),
            "pending_reasons": list(self.pending_reasons),
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Observation":
        return Observation(
            obs_id=data["obs_id"],
            product_id=data["product_id"],
            kind=data["kind"],
            scheduled_date=parse_date(data["scheduled_date"]),
            date=parse_date(data["date"]),
            roll_path=list(data.get("roll_path", [])),
            terms_version=int(data["terms_version"]),
            status=data["status"],
            evidence=data.get("evidence", {}),
            outcomes=data.get("outcomes", {}),
            calc_path=list(data.get("calc_path", [])),
            version=int(data["version"]),
            computed_at=parse_dt(data["computed_at"]),
            pending_reasons=list(data.get("pending_reasons", [])),
        )


# ---------------------------------------------------------------------------
# 现金流（projected → confirmed → paid / adjusted）
# ---------------------------------------------------------------------------

@dataclass
class Cashflow:
    cf_id: str                    # {product}:{key}
    key: str                      # coupon:2026-06-19 / redemption:autocall / adj:...:1
    product_id: str
    type: str                     # coupon / redemption / delivery / adjustment
    state: str
    amount: Decimal               # 现金部分（实物交付时为找零现金）
    currency: str
    value_date: date
    version: int
    lineage: dict[str, Any]       # 条款版本、观察版本、证据快照、计算路径
    updated_at: datetime
    delivery: Optional[dict[str, Any]] = None   # 实物交付明细
    links: dict[str, Any] = field(default_factory=dict)  # adjusts / adjusted_by 等

    def to_dict(self) -> dict[str, Any]:
        return {
            "cf_id": self.cf_id,
            "key": self.key,
            "product_id": self.product_id,
            "type": self.type,
            "state": self.state,
            "amount": dec_str(self.amount),
            "currency": self.currency,
            "value_date": self.value_date.isoformat(),
            "version": self.version,
            "lineage": self.lineage,
            "updated_at": self.updated_at.isoformat(),
            "delivery": self.delivery,
            "links": self.links,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Cashflow":
        return Cashflow(
            cf_id=data["cf_id"],
            key=data["key"],
            product_id=data["product_id"],
            type=data["type"],
            state=data["state"],
            amount=D(data["amount"]),
            currency=data["currency"],
            value_date=parse_date(data["value_date"]),
            version=int(data["version"]),
            lineage=data.get("lineage", {}),
            updated_at=parse_dt(data["updated_at"]),
            delivery=data.get("delivery"),
            links=data.get("links", {}),
        )


# ---------------------------------------------------------------------------
# 结算指令（幂等）
# ---------------------------------------------------------------------------

@dataclass
class Settlement:
    instruction_id: str           # 幂等键：同一指令重试返回同一结果
    cf_id: str
    amount: Decimal
    currency: str
    settled_at: datetime
    actor: str
    result: str                   # paid / replayed

    def to_dict(self) -> dict[str, Any]:
        return {
            "instruction_id": self.instruction_id,
            "cf_id": self.cf_id,
            "amount": dec_str(self.amount),
            "currency": self.currency,
            "settled_at": self.settled_at.isoformat(),
            "actor": self.actor,
            "result": self.result,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Settlement":
        return Settlement(
            instruction_id=data["instruction_id"],
            cf_id=data["cf_id"],
            amount=D(data["amount"]),
            currency=data["currency"],
            settled_at=parse_dt(data["settled_at"]),
            actor=data.get("actor", "system"),
            result=data.get("result", "paid"),
        )
