"""纯计算函数：表现、票息、到期交付。不触碰存储。"""

from __future__ import annotations

from decimal import Decimal

from .types import NumLike, dec, money, ratio


def performance(price: NumLike, initial_adj: NumLike) -> Decimal:
    """标的相对期初（经公司行动调整）的表现。"""
    return ratio(dec(price) / dec(initial_adj))


def coupon_amount(notional: NumLike, rate: NumLike, periods: int) -> Decimal:
    """票息 = 名义本金 × 每期利率 × 期数（含记忆补付）。"""
    return money(dec(notional) * dec(rate) * periods)


def maturity_cash(notional: NumLike, worst_of: NumLike, put_strike: NumLike, knocked_in: bool) -> Decimal:
    """到期现金交付：未敲入或期末不低于行权价 → 面值；否则按比例承担损失。"""
    if not knocked_in or dec(worst_of) >= dec(put_strike):
        return money(notional)
    return money(dec(notional) * dec(worst_of) / dec(put_strike))


def physical_delivery(notional: NumLike, put_strike: NumLike, initial_adj: NumLike) -> tuple[int, Decimal]:
    """实物交付：按行权价折算最差标的股数，零股以现金找零。"""
    strike = dec(put_strike) * dec(initial_adj)
    qty = int(dec(notional) // strike)
    residual = money(dec(notional) - qty * strike)
    return qty, residual
