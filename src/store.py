"""事件存储：每产品一个 JSONL 追加式日志，持久化到 .runtime/。

- 只追加、不改写：所有更正都以新事件（新版本）落地，历史可完整回放。
- 每条事件带单调递增 seq 与记录时间，支持按序重建任意状态。
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class EventStore:
    """单产品事件日志。线程安全追加；启动时全量回放。"""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._events: list[dict[str, Any]] = []
        if path.exists():
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        self._events.append(json.loads(line))

    # ------------------------------------------------------------------
    def append(self, event_type: str, payload: dict[str, Any],
               recorded_at: datetime | None = None) -> dict[str, Any]:
        with self._lock:
            seq = len(self._events) + 1
            event = {
                "seq": seq,
                "type": event_type,
                "recorded_at": (recorded_at or datetime.now(timezone.utc)).isoformat(),
                "payload": payload,
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            self._events.append(event)
            return event

    # ------------------------------------------------------------------
    def events(self) -> list[dict[str, Any]]:
        return list(self._events)

    def __len__(self) -> int:
        return len(self._events)


class StoreRegistry:
    """管理 .runtime/ 下所有产品的事件日志。"""

    def __init__(self, root: Path):
        self.root = root
        self._stores: dict[str, EventStore] = {}

    def for_product(self, product_id: str) -> EventStore:
        if product_id not in self._stores:
            self._stores[product_id] = EventStore(
                self.root / "products" / product_id / "events.jsonl"
            )
        return self._stores[product_id]

    def known_products(self) -> list[str]:
        base = self.root / "products"
        if not base.exists():
            return []
        return sorted(p.name for p in base.iterdir() if p.is_dir())
