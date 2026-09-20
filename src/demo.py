"""演示：停牌后补价引发的自动赎回争议，引擎如何收口。

运行：python3 src/demo.py
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from engine import Engine, Store
from engine.report import payment_explanation, product_view
from seed import load_seed


def show(title: str, payload) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        engine = Engine(Store(Path(tmp) / ".runtime"))
        load_seed(engine, Path(__file__).resolve().parents[1] / "reference" / "seed")

        versions = engine.observation_versions("ACN-2026-001", "2026-07-06", "autocall")
        show(
            "1. 争议观察点 2026-07-06 的版本链：顺延 → 补价确认 → 更正重算",
            [
                {
                    "version": v["version"],
                    "state": v["state"],
                    "actual_date": v["actual_date"],
                    "worst_of": v.get("worst_of"),
                    "evidence": [
                        {"underlying": e["underlying"], "price": e["price"], "status": e["price_status"]}
                        for e in v["evidence"]
                    ],
                }
                for v in versions
            ],
        )

        flows = engine.cashflows("ACN-2026-001")
        show(
            "2. ACN-2026-001 现金流现状（已付保留 + 差额调整）",
            [
                {
                    "cashflow_id": c["cashflow_id"],
                    "kind": c["kind"],
                    "amount": c["amount"],
                    "state": c["state"],
                    "adjusts": c.get("adjusts"),
                }
                for c in flows
            ],
        )

        view = product_view(engine, "ACN-2026-001")
        show(
            "3. 运营视图摘要：状态 / 下一观察日 / 预计现金流",
            {
                "state": view["state"],
                "next_observation": view["next_observation"],
                "barriers": view["barriers"],
                "projected": view["cashflows"]["projected"],
                "adjustments": view["cashflows"]["adjustments"],
            },
        )

        show(
            "4. 争议复原：赎回款为何成立、更正后差额为何产生",
            payment_explanation(engine, "cf-ACN-2026-001-redemption-2"),
        )


if __name__ == "__main__":
    main()
