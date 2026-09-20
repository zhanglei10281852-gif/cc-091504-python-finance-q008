"""种子数据加载：从 reference/seed 读取条款、日历、公司行动、行情与现金流样例。

价格网格（price_grid.json）是模拟行情源的锚点，加载器在锚点间线性插值
生成每个营业日的官方收盘价；prices.json 中的 steps 按时间线编排停牌、
补发收盘价、发行人更正与结算动作，重现争议批次的全过程。
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from engine import Engine
from engine.types import dec, is_business_day, js, parse_date, px


def _interp(anchors: list[tuple], d) -> Decimal:
    if d <= anchors[0][0]:
        return anchors[0][1]
    if d >= anchors[-1][0]:
        return anchors[-1][1]
    for (d0, p0), (d1, p1) in zip(anchors, anchors[1:]):
        if d0 <= d <= d1:
            span = (d1 - d0).days
            if span == 0:
                return p1
            frac = Decimal((d - d0).days) / Decimal(span)
            return p0 + (p1 - p0) * frac
    return anchors[-1][1]


def _ingest_grid(engine: Engine, grid: dict, skips: set[tuple]) -> None:
    holidays = engine.holidays("CN-SSE")
    for underlying, spec in grid.items():
        anchors = sorted((parse_date(d), dec(p)) for d, p in spec["anchors"])
        start, end = anchors[0][0], anchors[-1][0]
        d = start
        while d <= end:
            if is_business_day(d, holidays) and (underlying, d.isoformat()) not in skips:
                engine.ingest_price(
                    underlying,
                    d,
                    price=js(px(_interp(anchors, d))),
                    status="official",
                    source=spec.get("source", "simulated-feed"),
                    recorded_at=f"{d.isoformat()}T16:00:00+00:00",
                )
            d += timedelta(days=1)


def _settle_due(engine: Engine, product_id: str, key_prefix: str) -> None:
    for cf in engine.cashflows(product_id):
        if cf["state"] == "confirmed":
            engine.settle(f"{key_prefix}-{cf['cashflow_id']}", cf["cashflow_id"], actor="seed")


def load_seed(engine: Engine, seed_dir) -> None:
    seed_dir = Path(seed_dir)

    holidays = json.loads((seed_dir / "holidays.json").read_text(encoding="utf-8"))
    for calendar, dates in holidays.items():
        engine.set_holidays(calendar, dates)

    products = json.loads((seed_dir / "products.json").read_text(encoding="utf-8"))
    for p in products:
        engine.issue_terms(
            p["product_id"],
            p["terms"],
            p["effective_from"],
            reason=p.get("reason", "terms_issued"),
            recorded_at=p.get("recorded_at"),
        )

    actions = json.loads((seed_dir / "corporate_actions.json").read_text(encoding="utf-8"))
    for ca in actions:
        engine.apply_corporate_action(
            ca["underlying"],
            ca["ex_date"],
            ca["kind"],
            ca["factor"],
            note=ca.get("note"),
            recorded_at=ca.get("recorded_at"),
        )

    grid = json.loads((seed_dir / "price_grid.json").read_text(encoding="utf-8"))
    notable = json.loads((seed_dir / "prices.json").read_text(encoding="utf-8"))
    skips = {tuple(x) for x in notable.get("grid_skip", [])}
    _ingest_grid(engine, grid, skips)

    samples = json.loads((seed_dir / "cashflow_samples.json").read_text(encoding="utf-8"))
    engine.load_samples(samples)

    for step in notable.get("steps", []):
        if "process_due" in step:
            product_id, as_of = step["process_due"]
            engine.process_due(product_id, as_of, actor="seed")
        elif "suspension" in step:
            s = step["suspension"]
            engine.record_suspension(
                s["underlying"], s["date"], note=s.get("note"), recorded_at=s.get("recorded_at"), actor="seed"
            )
        elif "price" in step:
            p = step["price"]
            engine.ingest_price(
                p["underlying"],
                p["date"],
                price=p.get("price"),
                status=p.get("status", "official"),
                source=p.get("source", "exchange"),
                note=p.get("note"),
                recorded_at=p.get("recorded_at"),
                actor="seed",
            )
        elif "correction" in step:
            c = step["correction"]
            engine.correct_price(
                c["underlying"],
                c["date"],
                c["action"],
                new_price=c.get("price"),
                reason=c.get("reason"),
                recorded_at=c.get("recorded_at"),
                actor="seed",
            )
        elif "settle_due" in step:
            s = step["settle_due"]
            _settle_due(engine, s["product"], s["key_prefix"])
        else:  # pragma: no cover - 数据文件错误
            raise ValueError(f"unknown seed step: {step}")
