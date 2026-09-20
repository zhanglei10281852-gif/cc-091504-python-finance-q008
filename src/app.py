from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from engine import EngineError
from engine.report import payment_explanation, product_view

SERVICE_NAME = '结构性票据核算服务'


def health_payload() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE_NAME}


class Api:
    """引擎的 JSON 接口层。所有响应均为 JSON。"""

    def __init__(self, engine):
        self.engine = engine

    def handle(self, method: str, raw_path: str, body: dict | None) -> tuple[int, object]:
        parsed = urlparse(raw_path)
        segments = [s for s in parsed.path.split("/") if s]
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        body = body or {}
        try:
            return 200, self._route(method, segments, query, body)
        except EngineError as exc:
            return exc.status, {"error": exc.code, "message": str(exc)}

    def _route(self, method: str, seg: list[str], query: dict, body: dict):
        eng = self.engine
        if method == "GET" and seg == ["health"]:
            return health_payload()
        if method == "GET" and seg == ["products"]:
            return {"products": eng.list_products()}
        if method == "GET" and len(seg) == 2 and seg[0] == "products":
            return product_view(eng, seg[1], as_of=query.get("as_of"))
        if method == "GET" and len(seg) == 3 and seg[0] == "products" and seg[2] == "observations":
            return {"observations": eng.observations(seg[1])}
        if method == "GET" and len(seg) == 3 and seg[0] == "products" and seg[2] == "cashflows":
            return {"cashflows": eng.cashflows(seg[1])}
        if method == "GET" and len(seg) == 3 and seg[0] == "products" and seg[2] == "audit":
            return {"events": eng.audit_trail(seg[1])}
        if method == "GET" and len(seg) == 3 and seg[0] == "cashflows" and seg[2] == "explanation":
            return payment_explanation(eng, seg[1])
        if method == "POST" and len(seg) == 3 and seg[0] == "terms" and seg[2] == "versions":
            return eng.issue_terms(
                seg[1],
                body.get("terms", {}),
                body["effective_from"],
                actor=body.get("actor", "api"),
                reason=body.get("reason", "terms_issued"),
            )
        if method == "POST" and seg == ["corporate-actions"]:
            return eng.apply_corporate_action(
                body["underlying"],
                body["ex_date"],
                body.get("kind", "split"),
                body["factor"],
                actor=body.get("actor", "api"),
                note=body.get("note"),
            )
        if method == "POST" and seg == ["market-data", "prices"]:
            if body.get("status") == "suspended":
                return eng.record_suspension(
                    body["underlying"], body["date"], actor=body.get("actor", "api"), note=body.get("note")
                )
            return eng.ingest_price(
                body["underlying"],
                body["date"],
                price=body.get("price"),
                price_type=body.get("price_type", "official_close"),
                status=body.get("status", "official"),
                source=body.get("source", "api"),
                actor=body.get("actor", "api"),
                note=body.get("note"),
            )
        if method == "POST" and seg == ["market-data", "corrections"]:
            return eng.correct_price(
                body["underlying"],
                body["date"],
                body["action"],
                actor=body.get("actor", "api"),
                new_price=body.get("price"),
                reason=body.get("reason"),
            )
        if method == "POST" and len(seg) == 3 and seg[0] == "products" and seg[2] == "observations":
            if "as_of" in body:
                return {"ran": eng.process_due(seg[1], body["as_of"], actor=body.get("actor", "api"))}
            return eng.run_observation(
                seg[1],
                body["date"],
                kind=body.get("kind"),
                actor=body.get("actor", "api"),
                as_of=body.get("as_of"),
            )
        if method == "POST" and seg == ["settlements"]:
            return eng.settle(body["idempotency_key"], body["cashflow_id"], actor=body.get("actor", "api"))
        raise EngineError("not_found", "route not found", 404)


class RequestHandler(BaseHTTPRequestHandler):
    api: Api | None = None

    def _respond(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.api is None:
            if self.path == "/health":
                self._respond(200, health_payload())
            else:
                self.send_error(404, "Not Found")
            return
        status, payload = self.api.handle("GET", self.path, None)
        self._respond(status, payload)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            self._respond(400, {"error": "bad_json", "message": "request body must be JSON"})
            return
        if self.api is None:
            self.send_error(404, "Not Found")
            return
        status, payload = self.api.handle("POST", self.path, body)
        self._respond(status, payload)

    def log_message(self, format: str, *args: object) -> None:
        return


def create_server(host: str, port: int, engine=None) -> ThreadingHTTPServer:
    handler = type("BoundRequestHandler", (RequestHandler,), {})
    handler.api = Api(engine) if engine is not None else None
    return ThreadingHTTPServer((host, port), handler)
