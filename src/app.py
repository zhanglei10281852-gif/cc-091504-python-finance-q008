"""HTTP 接口层：结构性票据核算服务。

- 启动时从 reference/ 引导产品条款、日历与行情种子数据（幂等，可重复启动）。
- 业务事件（行情、条款修订、公司行动、结算）经 API 进入，落库到 .runtime/。
- 所有响应为 JSON；错误返回 {"error": ...} 与对应状态码。
"""
from __future__ import annotations

import json
import os
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from calendars import BusinessCalendar
from engine import ConflictError, EngineError, NotFoundError, ProductEngine, ValidationError
from models import dec_str
from queries import explain_cashflow, operations_view
from store import StoreRegistry

SERVICE_NAME = '结构性票据核算服务'

ROOT = Path(__file__).resolve().parents[1]
REFERENCE_DIR = Path(os.getenv("REFERENCE_DIR", ROOT / "reference"))
RUNTIME_DIR = Path(os.getenv("RUNTIME_DIR", ROOT / ".runtime"))


def health_payload() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE_NAME}


# ---------------------------------------------------------------------------
# 引导：reference/ → 引擎
# ---------------------------------------------------------------------------

class Registry:
    """产品引擎注册表：按 product_id 管理引擎与事件存储。"""

    def __init__(self, reference_dir: Path = REFERENCE_DIR,
                 runtime_dir: Path = RUNTIME_DIR):
        self.reference_dir = reference_dir
        self.runtime_dir = runtime_dir
        self.stores = StoreRegistry(runtime_dir)
        self.calendars = self._load_calendars()
        self.precision = self._load_precision()
        self.engines: dict[str, ProductEngine] = {}
        self._bootstrap()

    def _load_calendars(self) -> dict[str, BusinessCalendar]:
        calendars: dict[str, BusinessCalendar] = {}
        cal_dir = self.reference_dir / "calendars"
        if cal_dir.exists():
            for path in sorted(cal_dir.glob("*.json")):
                data = json.loads(path.read_text(encoding="utf-8"))
                cal = BusinessCalendar.from_dict(data)
                calendars[cal.name] = cal
        calendars.setdefault("CN", BusinessCalendar("CN"))
        return calendars

    def _load_precision(self) -> dict[str, Any]:
        domain_path = self.reference_dir / "domain.json"
        if domain_path.exists():
            data = json.loads(domain_path.read_text(encoding="utf-8"))
            return data.get("precision", {})
        return {}

    def _bootstrap(self) -> None:
        products_dir = self.reference_dir / "products"
        if not products_dir.exists():
            return
        for terms_path in sorted(products_dir.glob("*.terms.json")):
            terms = json.loads(terms_path.read_text(encoding="utf-8"))
            product_id = terms["product_id"]
            engine = self.get_or_create_engine(product_id)
            payload = terms.get("payload") or {
                k: v for k, v in terms.items()
                if k not in ("product_id", "terms_version", "effective_from")
            }
            engine.register_terms(
                payload,
                effective_from=date.fromisoformat(terms["effective_from"]),
                version=int(terms.get("terms_version", 1)),
            )
            prices_path = self.reference_dir / "prices" / f"{product_id}.prices.jsonl"
            if prices_path.exists():
                for line in prices_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line:
                        engine.ingest_price(json.loads(line), auto_recompute=False)
            ca_path = self.reference_dir / "corporate-actions" / f"{product_id}.ca.json"
            if ca_path.exists():
                for ca in json.loads(ca_path.read_text(encoding="utf-8")):
                    engine.apply_corporate_action(ca, auto_recompute=False)
            engine.recompute()

    def get_or_create_engine(self, product_id: str,
                             calendar: str = "CN") -> ProductEngine:
        if product_id not in self.engines:
            self.engines[product_id] = ProductEngine(
                product_id,
                self.stores.for_product(product_id),
                self.calendars.get(calendar, self.calendars["CN"]),
                self.precision,
            )
        return self.engines[product_id]

    def get_engine(self, product_id: str) -> ProductEngine:
        if product_id not in self.engines:
            raise NotFoundError(f"产品不存在：{product_id}")
        return self.engines[product_id]

    def expected_cashflows(self, product_id: str) -> Optional[dict[str, Any]]:
        path = self.reference_dir / "cashflows" / f"{product_id}.expected.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return None


REGISTRY: Optional[Registry] = None


def get_registry() -> Registry:
    global REGISTRY
    if REGISTRY is None:
        REGISTRY = Registry()
    return REGISTRY


# ---------------------------------------------------------------------------
# HTTP 路由
# ---------------------------------------------------------------------------

class RequestHandler(BaseHTTPRequestHandler):
    server_version = "NotesEngine/1.0"

    # ---------------- 工具 ----------------
    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValidationError(f"请求体不是合法 JSON：{exc}")

    def _segments(self) -> list[str]:
        return [s for s in urlparse(self.path).path.split("/") if s]

    def _handle(self, method: str) -> None:
        segments = self._segments()
        registry = get_registry()
        try:
            result = self._route(method, segments, registry)
            if result is not None:
                status, payload = result
                self._send_json(status, payload)
        except NotFoundError as exc:
            self._send_json(404, {"error": str(exc)})
        except ConflictError as exc:
            self._send_json(409, {"error": str(exc)})
        except ValidationError as exc:
            self._send_json(400, {"error": str(exc)})
        except EngineError as exc:
            self._send_json(400, {"error": str(exc)})

    def _route(self, method: str, segments: list[str],
               registry: Registry) -> Optional[tuple[int, Any]]:
        # GET /health
        if method == "GET" and segments == ["health"]:
            return 200, health_payload()

        # GET /products
        if method == "GET" and segments == ["products"]:
            return 200, {"products": sorted(registry.engines.keys())}

        if len(segments) >= 2 and segments[0] == "products":
            product_id = segments[1]
            engine = registry.get_engine(product_id)

            # GET /products/{pid}
            if method == "GET" and len(segments) == 2:
                terms = engine._terms_latest()
                return 200, {
                    "product_id": product_id,
                    "status": engine.status_info,
                    "terms_version": terms.version if terms else None,
                    "terms": terms.payload if terms else None,
                }

            # GET /products/{pid}/operations
            if method == "GET" and segments[2:] == ["operations"]:
                return 200, operations_view(engine)

            # GET /products/{pid}/observations
            if method == "GET" and segments[2:] == ["observations"]:
                return 200, {
                    "observations": [
                        versions[-1].to_dict()
                        for _, versions in sorted(engine.observations.items())
                    ]
                }

            # GET /products/{pid}/cashflows
            if method == "GET" and segments[2:] == ["cashflows"]:
                return 200, {
                    "cashflows": [
                        cf.to_dict() for cf in
                        sorted(engine.latest_cashflows(), key=lambda c: c.key)
                    ]
                }

            # GET /products/{pid}/reconcile —— 与 reference 现金流样例对账
            if method == "GET" and segments[2:] == ["reconcile"]:
                expected = registry.expected_cashflows(product_id)
                if expected is None:
                    raise NotFoundError(f"{product_id} 无现金流样例")
                actual = {cf.key: cf for cf in engine.latest_cashflows()}
                report = []
                for sample in expected.get("samples", []):
                    cf = actual.get(sample["key"])
                    report.append({
                        "key": sample["key"],
                        "expected_amount": sample["amount"],
                        "expected_state": sample.get("state"),
                        "actual_amount": dec_str(cf.amount) if cf else None,
                        "actual_state": cf.state if cf else None,
                        "match": bool(cf) and dec_str(cf.amount) == sample["amount"]
                        and (sample.get("state") in (None, cf.state)),
                    })
                return 200, {"product_id": product_id, "report": report,
                             "all_match": all(r["match"] for r in report)}

            # POST /products/{pid}/prices —— 行情进入（临时/官方/更正/撤销）
            if method == "POST" and segments[2:] == ["prices"]:
                rec = engine.ingest_price(self._read_json())
                return 201, rec.to_dict()

            # POST /products/{pid}/terms —— 条款修订（新版本）
            if method == "POST" and segments[2:] == ["terms"]:
                body = self._read_json()
                tv = engine.register_terms(
                    body["payload"],
                    effective_from=date.fromisoformat(body["effective_from"]),
                )
                engine.recompute()
                return 201, tv.to_dict()

            # POST /products/{pid}/corporate-actions
            if method == "POST" and segments[2:] == ["corporate-actions"]:
                ca = engine.apply_corporate_action(self._read_json())
                return 201, ca.to_dict()

            # POST /products/{pid}/recompute —— 手动触发重算
            if method == "POST" and segments[2:] == ["recompute"]:
                engine.recompute()
                return 200, {"status": engine.status_info}

        # /cashflows/{cf_id}/... —— cf_id 形如 {product_id}:{key}，用 ~ 代替 :
        if len(segments) >= 2 and segments[0] == "cashflows":
            cf_ref = segments[1]
            product_id, _, key = cf_ref.partition("~")
            if not key:
                product_id, _, key = cf_ref.partition(":")
            engine = registry.get_engine(product_id)
            cf_id = f"{product_id}:{key}"

            # GET /cashflows/{pid}~{key}/explain
            if method == "GET" and segments[2:] == ["explain"]:
                return 200, explain_cashflow(engine, cf_id)

            # POST /cashflows/{pid}~{key}/settle {"instruction_id": ..., "actor": ...}
            if method == "POST" and segments[2:] == ["settle"]:
                body = self._read_json()
                instruction_id = body.get("instruction_id")
                if not instruction_id:
                    raise ValidationError("缺少 instruction_id（幂等键）")
                settlement = engine.settle(
                    cf_id, instruction_id, actor=body.get("actor", "ops")
                )
                return 200, settlement.to_dict()

        if method == "GET":
            self.send_error(404, "Not Found")
            return None
        self.send_error(404, "Not Found")
        return None

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def log_message(self, format: str, *args: object) -> None:
        return


def create_server(host: str, port: int) -> ThreadingHTTPServer:
    get_registry()  # 提前引导，启动失败尽早暴露
    return ThreadingHTTPServer((host, port), RequestHandler)
