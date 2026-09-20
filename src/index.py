from __future__ import annotations

import argparse
import os
from pathlib import Path

from app import create_server
from engine import Engine, Store
from seed import load_seed


def build_engine(runtime_dir: str, seed_dir: str | None) -> Engine:
    store = Store(runtime_dir)
    engine = Engine(store)
    if seed_dir and not store.col("terms"):
        load_seed(engine, Path(seed_dir))
    return engine


def main() -> None:
    parser = argparse.ArgumentParser(description="结构性票据核算服务")
    parser.add_argument("--host", default=os.getenv("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    parser.add_argument("--runtime", default=os.getenv("RUNTIME_DIR", ".runtime"))
    parser.add_argument("--seed", default=os.getenv("SEED_DIR"), help="首次启动时加载的种子数据目录")
    args = parser.parse_args()

    engine = build_engine(args.runtime, args.seed)
    server = create_server(args.host, args.port, engine)
    server.serve_forever()


if __name__ == "__main__":
    main()
