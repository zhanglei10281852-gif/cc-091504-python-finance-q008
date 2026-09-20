"""JSON 文档存储：每个集合一个文件，原子写；事件追加写入 events.jsonl。

业务记录区分三个时间维度：业务发生时间（记录字段）、系统接收时间
``recorded_at``、以及版本号 ``version``。历史版本永不删除。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path


class Store:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._cols: dict[str, list[dict]] = {}
        self._lock = threading.RLock()

    def _path(self, name: str) -> Path:
        return self.root / f"{name}.json"

    def col(self, name: str) -> list[dict]:
        with self._lock:
            if name not in self._cols:
                path = self._path(name)
                if path.exists():
                    self._cols[name] = json.loads(path.read_text(encoding="utf-8"))
                else:
                    self._cols[name] = []
            return self._cols[name]

    def add(self, name: str, record: dict) -> dict:
        with self._lock:
            self.col(name).append(record)
            self._save(name)
            return record

    def save(self, name: str) -> None:
        with self._lock:
            self._save(name)

    def _save(self, name: str) -> None:
        path = self._path(name)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self._cols.get(name, []), ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        os.replace(tmp, path)

    def audit(self, event: dict) -> None:
        with self._lock:
            line = json.dumps(event, ensure_ascii=False)
            with (self.root / "events.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def audit_trail(self, product_id: str | None = None) -> list[dict]:
        path = self.root / "events.jsonl"
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if product_id is None or event.get("product_id") == product_id:
                out.append(event)
        return out
