"""测试公共构造：合成条款、引擎与行情记录。"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from calendars import BusinessCalendar  # noqa: E402
from engine import ProductEngine  # noqa: E402
from store import EventStore  # noqa: E402

TZ8 = timezone(timedelta(hours=8))
PRECISION = {"amount_decimals": 2, "rounding": "ROUND_HALF_UP", "ratio_decimals": 8}


def at(day: str, hm: str = "09:00") -> datetime:
    return datetime.fromisoformat(f"{day}T{hm}:00+08:00")


def make_terms(**overrides):
    terms = {
        "notional": "1000000.00",
        "currency": "CNY",
        "issue_date": "2026-01-05",
        "maturity_date": "2026-07-06",
        "calendar": "CN",
        "settlement_lag_days": 3,
        "basket": [
            {"ticker": "TST", "name": "测试标的", "weight": "1", "initial_price": "100.00"},
        ],
        "barriers": {
            "knock_in": {"ratio": "0.70", "observation": "daily_close"},
            "autocall": {"ratio": "1.05", "observation": "scheduled"},
            "coupon": {"ratio": "0.80", "observation": "scheduled"},
        },
        "coupon": {"annual_rate": "0.12", "frequency": "quarterly", "memory": True},
        "autocall": {"redemption_ratio": "1.00"},
        "maturity": {
            "knocked_in_settlement": "cash",
            "lot_size": 100,
            "protection_ratio": "1.00",
        },
        "schedule": {
            "observation_dates": ["2026-04-07", "2026-07-06"],
            "ki_window": {"start": "2026-01-05", "end": "2026-07-06"},
        },
    }
    terms.update(overrides)
    return terms


def make_engine(tmp_path, terms=None, product_id="TEST-1") -> ProductEngine:
    store = EventStore(Path(tmp_path) / "events.jsonl")
    engine = ProductEngine(product_id, store, BusinessCalendar("CN"), PRECISION)
    engine.register_terms(terms or make_terms(), effective_from=date(2026, 1, 5))
    return engine


def close(ticker: str, day: str, value, version: int = 1,
          published: str | None = None, status: str = "official",
          corrects: str | None = None, pid: str | None = None) -> dict:
    return {
        "price_id": pid or f"P-{ticker}-{day.replace('-', '')}-{version}",
        "underlying": ticker,
        "date": day,
        "value": value,
        "price_type": "official_close",
        "status": status,
        "version": version,
        "published_at": published or f"{day}T15:30:00+08:00",
        "source": "exchange",
        "corrects": corrects,
    }


def feed_daily(engine: ProductEngine, ticker: str, start: str, end: str,
               value) -> None:
    """为 [start, end] 内每个工作日补一条官方收盘，避免敲入日缺价挂起。"""
    day = date.fromisoformat(start)
    last = date.fromisoformat(end)
    while day <= last:
        if day.weekday() < 5:
            engine.ingest_price(close(ticker, day.isoformat(), value),
                                auto_recompute=False)
        day += timedelta(days=1)
