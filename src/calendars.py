"""交易日历：周末 + 节假日，支持顺延并记录计算路径。

顺延路径必须可读：每一次滚动都留下“原日期 → 原因 → 新日期”的文字记录，
供运营与争议处理复核。
"""
from __future__ import annotations

from datetime import date, timedelta

_WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


class BusinessCalendar:
    def __init__(self, name: str, holidays: set[date] | None = None,
                 weekend: tuple[int, ...] = (5, 6)):
        self.name = name
        self.holidays: set[date] = set(holidays or set())
        self.weekend = weekend

    # ------------------------------------------------------------------
    def is_business_day(self, day: date) -> bool:
        return day.weekday() not in self.weekend and day not in self.holidays

    def non_business_reason(self, day: date) -> str | None:
        if day.weekday() in self.weekend:
            return f"{day.isoformat()} 为{_WEEKDAY_CN[day.weekday()]}（周末）"
        if day in self.holidays:
            return f"{day.isoformat()} 为节假日（{self.name} 日历）"
        return None

    # ------------------------------------------------------------------
    def roll(self, day: date, convention: str = "following") -> tuple[date, list[str]]:
        """按约定顺延，返回 (调整后日期, 计算路径)。"""
        if convention == "modified_following":
            return self._roll_modified_following(day)
        return self._roll_following(day)

    def _roll_following(self, day: date) -> tuple[date, list[str]]:
        path: list[str] = []
        current = day
        while True:
            reason = self.non_business_reason(current)
            if reason is None:
                if current != day:
                    path.append(f"{current.isoformat()} 为交易日，定为调整日")
                return current, path
            path.append(f"{reason}，顺延至下一交易日")
            current += timedelta(days=1)

    def _roll_modified_following(self, day: date) -> tuple[date, list[str]]:
        rolled, path = self._roll_following(day)
        if rolled.month != day.month:
            path.append(
                f"顺延结果 {rolled.isoformat()} 跨月，按 modified_following 回退"
            )
            current = day
            while True:
                current -= timedelta(days=1)
                reason = self.non_business_reason(current)
                if reason is None:
                    path.append(f"{current.isoformat()} 为交易日，定为调整日")
                    return current, path
                path.append(f"{reason}，向前回退")
        return rolled, path

    # ------------------------------------------------------------------
    def add_business_days(self, day: date, n: int) -> tuple[date, list[str]]:
        """从 day 起加 n 个交易日（不含 day 本身），返回 (日期, 计算路径)。"""
        path: list[str] = []
        current = day
        remaining = n
        while remaining > 0:
            current += timedelta(days=1)
            reason = self.non_business_reason(current)
            if reason is None:
                remaining -= 1
                path.append(f"第 {n - remaining} 个交易日：{current.isoformat()}")
            else:
                path.append(f"{reason}，不计入")
        return current, path

    def business_days_between(self, start: date, end: date) -> list[date]:
        """[start, end] 闭区间内的交易日列表。"""
        days: list[date] = []
        current = start
        while current <= end:
            if self.is_business_day(current):
                days.append(current)
            current += timedelta(days=1)
        return days

    # ------------------------------------------------------------------
    @staticmethod
    def from_dict(data: dict) -> "BusinessCalendar":
        holidays = {date.fromisoformat(h) for h in data.get("holidays", [])}
        weekend = tuple(data.get("weekend", [5, 6]))
        return BusinessCalendar(data.get("name", "CN"), holidays, weekend)
