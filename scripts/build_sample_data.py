"""生成 NOTE-A001 的示例行情与现金流样例（确定性，可重复执行）。

场景设定（与运营争议对应）：
- 600519.SH：1800 起步温和下行，全程未触敲入/敲出。
- 300750.SZ：220 起步；2026-06-19 盘中停牌 —— 种子数据故意缺失当日行情，
  临时价、补发官方价、更正价均由 scripts/demo_scenario.py 作为实时事件注入；
  2026-08-10 除权 1 拆 2，价格序列减半。
- 种子数据只含“正常”官方收盘价（2026-03-19 .. 2026-09-18 各交易日）。

输出：
- reference/prices/NOTE-A001.prices.jsonl
- reference/cashflows/NOTE-A001.expected.json
"""
from __future__ import annotations

import json
import random
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from calendars import BusinessCalendar  # noqa: E402

PRODUCT_ID = "NOTE-A001"
TZ = "+08:00"
START = date(2026, 3, 19)
END = date(2026, 9, 18)
SPLIT_EX = date(2026, 8, 10)
SUSPENDED = date(2026, 6, 19)  # 300750.SZ 当日停牌，种子不含官方收盘

PINS_A = {
    date(2026, 3, 19): "1800.00",
    date(2026, 6, 19): "1765.00",
    date(2026, 9, 16): "1730.00",
    date(2026, 9, 18): "1720.00",
}
PINS_B = {
    date(2026, 3, 19): "220.00",
    date(2026, 6, 18): "196.00",
    date(2026, 6, 22): "195.50",
    date(2026, 8, 7): "190.00",
    date(2026, 8, 10): "95.00",
    date(2026, 9, 18): "95.50",
}


def walk(seed: str, start: float, days: list[date], lo: float, hi: float,
         pins: dict[date, str], split_ex: date | None = None,
         split_lo: float = 0.0, split_hi: float = 0.0) -> dict[date, str]:
    rng = random.Random(seed)
    out: dict[date, str] = {}
    px = start
    halved = False
    for d in days:
        if d in pins:
            px = float(pins[d])
        else:
            if split_ex and d >= split_ex and not halved:
                px = round(px / 2, 2)
                halved = True
            r = rng.uniform(-0.012, 0.011)
            lo_d, hi_d = (split_lo, split_hi) if (split_ex and d >= split_ex) else (lo, hi)
            px = min(max(round(px * (1 + r), 2), lo_d), hi_d)
        out[d] = f"{px:.2f}"
    return out


def main() -> None:
    calendar = BusinessCalendar.from_dict(json.loads(
        (ROOT / "reference" / "calendars" / "CN.json").read_text(encoding="utf-8")
    ))
    days = calendar.business_days_between(START, END)

    prices_a = walk("600519.SH", 1800.00, days, 1700.00, 1795.00, PINS_A)
    prices_b = walk("300750.SZ", 220.00, days, 188.00, 208.00, PINS_B,
                    split_ex=SPLIT_EX, split_lo=94.00, split_hi=104.00)
    prices_b.pop(SUSPENDED, None)  # 停牌日：无官方收盘

    records = []
    for ticker, series in (("600519.SH", prices_a), ("300750.SZ", prices_b)):
        for d in sorted(series):
            records.append({
                "price_id": f"P-{ticker}-{d:%Y%m%d}-1",
                "underlying": ticker,
                "date": d.isoformat(),
                "value": series[d],
                "price_type": "official_close",
                "status": "official",
                "version": 1,
                "published_at": f"{d.isoformat()}T15:30:00{TZ}",
                "source": "exchange",
                "corrects": None,
            })

    prices_path = ROOT / "reference" / "prices" / f"{PRODUCT_ID}.prices.jsonl"
    prices_path.parent.mkdir(parents=True, exist_ok=True)
    with prices_path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    expected = {
        "product_id": PRODUCT_ID,
        "as_of": "2026-09-20",
        "note": "demo_scenario.py 回放完整时间线后的终态现金流样例",
        "samples": [
            {
                "key": "coupon:2026-06-19", "type": "coupon",
                "state": "adjusted", "amount": "20000.00",
                "note": "Q1 票息已于 2026-06-26 支付；2026-07-02 行情更正后不再应付，"
                        "原付款保留并标记 adjusted",
            },
            {
                "key": "adj:coupon:2026-06-19:1", "type": "adjustment",
                "state": "confirmed", "amount": "-20000.00",
                "note": "更正后应付 0.00 - 已付 20000.00 = 差额 -20000.00，待结算收回",
            },
            {
                "key": "coupon:2026-09-19", "type": "coupon",
                "state": "projected", "amount": "40000.00",
                "note": "Q2 票息预计：当期 20000.00 + 记忆补付 Q1 的 20000.00",
            },
            {
                "key": "redemption:maturity", "type": "redemption",
                "state": "projected", "amount": "1000000.00",
                "note": "未敲入，按保护比例 1.00 预计到期兑付",
            },
        ],
    }
    expected_path = ROOT / "reference" / "cashflows" / f"{PRODUCT_ID}.expected.json"
    expected_path.parent.mkdir(parents=True, exist_ok=True)
    expected_path.write_text(
        json.dumps(expected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    # 公司行动种子：300750.SZ 于 2026-08-10 除权 1 拆 2（与价格序列一致）
    corp_actions = [
        {
            "ca_id": "CA-300750-20260810",
            "underlying": "300750.SZ",
            "ex_date": "2026-08-10",
            "kind": "split",
            "factor": "0.5",
            "payload": {"ratio": "1:2", "note": "每 1 股拆为 2 股"},
            "recorded_at": "2026-08-03T09:00:00+08:00",
        }
    ]
    ca_path = ROOT / "reference" / "corporate-actions" / f"{PRODUCT_ID}.ca.json"
    ca_path.parent.mkdir(parents=True, exist_ok=True)
    ca_path.write_text(
        json.dumps(corp_actions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(f"已生成 {len(records)} 条行情 → {prices_path}")
    print(f"已生成现金流样例 → {expected_path}")
    print(f"已生成公司行动 → {ca_path}")


if __name__ == "__main__":
    main()
