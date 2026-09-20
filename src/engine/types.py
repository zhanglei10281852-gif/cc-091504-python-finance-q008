"""基础类型：精度约定、日期与营业日工具。

精度约定与 reference/domain.json 中 ``precision`` 一节保持一致：
金额 2 位、价格 4 位、比率 6 位，ROUND_HALF_UP。JSON 中十进制一律存字符串。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Union

MONEY_DP = 2
PRICE_DP = 4
RATIO_DP = 6
ROUNDING = ROUND_HALF_UP

DateLike = Union[str, date]
NumLike = Union[str, int, float, Decimal]


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_date(value: DateLike) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def dec(value: NumLike) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _quant(dp: int) -> Decimal:
    return Decimal(1).scaleb(-dp)


def money(value: NumLike) -> Decimal:
    return dec(value).quantize(_quant(MONEY_DP), rounding=ROUNDING)


def px(value: NumLike) -> Decimal:
    return dec(value).quantize(_quant(PRICE_DP), rounding=ROUNDING)


def ratio(value: NumLike) -> Decimal:
    return dec(value).quantize(_quant(RATIO_DP), rounding=ROUNDING)


def js(value: NumLike) -> str:
    """JSON 安全的十进制字符串。"""
    return format(dec(value), "f")


def is_business_day(d: date, holidays: set[date]) -> bool:
    return d.weekday() < 5 and d not in holidays


def next_business_day(d: date, holidays: set[date]) -> date:
    d = d + timedelta(days=1)
    while not is_business_day(d, holidays):
        d += timedelta(days=1)
    return d


def prev_business_day(d: date, holidays: set[date]) -> date:
    d = d - timedelta(days=1)
    while not is_business_day(d, holidays):
        d -= timedelta(days=1)
    return d


def roll_date(d: date, convention: str, holidays: set[date]) -> date:
    """节假日顺延。支持 following / preceding / modified_following。"""
    if is_business_day(d, holidays):
        return d
    if convention == "following":
        return next_business_day(d, holidays)
    if convention == "preceding":
        return prev_business_day(d, holidays)
    nxt = next_business_day(d, holidays)
    if nxt.month == d.month:
        return nxt
    return prev_business_day(d, holidays)


def add_business_days(d: date, n: int, holidays: set[date]) -> date:
    for _ in range(n):
        d = next_business_day(d, holidays)
    return d


def business_days_between(start: date, end: date, holidays: set[date]) -> Iterable[date]:
    d = start
    while d <= end:
        if is_business_day(d, holidays):
            yield d
        d += timedelta(days=1)
