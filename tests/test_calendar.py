import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from calendars import BusinessCalendar  # noqa: E402


class CalendarTest(unittest.TestCase):
    def setUp(self):
        self.cal = BusinessCalendar("CN", holidays={date(2026, 4, 6)})

    def test_weekend_roll_leaves_path(self):
        rolled, path = self.cal.roll(date(2026, 9, 19))  # 周六
        self.assertEqual(rolled, date(2026, 9, 21))
        self.assertTrue(any("周六" in step for step in path))
        self.assertTrue(any("定为调整日" in step for step in path))

    def test_holiday_roll(self):
        rolled, path = self.cal.roll(date(2026, 4, 4))  # 周六，且 4/6 为节假日
        self.assertEqual(rolled, date(2026, 4, 7))
        self.assertTrue(any("节假日" in step for step in path))

    def test_modified_following_crosses_month(self):
        rolled, path = self.cal.roll(date(2026, 5, 31), "modified_following")
        self.assertEqual(rolled, date(2026, 5, 29))  # 顺延跨月 → 回退到 5 月内
        self.assertTrue(any("跨月" in step for step in path))

    def test_add_business_days(self):
        rolled, _ = self.cal.add_business_days(date(2026, 6, 19), 5)
        self.assertEqual(rolled, date(2026, 6, 26))

    def test_business_days_between(self):
        days = self.cal.business_days_between(date(2026, 9, 18), date(2026, 9, 22))
        self.assertEqual(days, [date(2026, 9, 18), date(2026, 9, 21), date(2026, 9, 22)])


if __name__ == "__main__":
    unittest.main()
