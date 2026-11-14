"""Synthetic Nordfood HTTP service. All prices, products and acknowledgements are fixtures."""
import json
import threading
from pathlib import Path
from uuid import uuid4
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


class Query(BaseModel):
    buyer: str
    delivery_day: str
    articles: list[str]


class OrderBody(BaseModel):
    buyer: str
    currency_code: str
    items: list[dict] = Field(min_length=1)


def create_app():
    app = FastAPI(title="Nordfood — SYNTHETIC supplier")
    articles = {p["article"]: p for p in json.loads(Path(__file__).with_name("catalog.json").read_text())}
    app.state.orders, app.state.keys = {}, {}
    app.state.stock = {sku: 5000 for sku in articles}
    lock = threading.Lock()

    @app.post("/case-prices")
    def prices(query: Query):
        now = datetime.now(timezone.utc)
        return {"synthetic": True, "observed_at": now.isoformat(), "items": [
            {"article": sku, "net_per_case": articles[sku]["net_per_case"], "currency_code": articles[sku]["currency"],
             "from": now.isoformat(), "until": (now + timedelta(days=1)).isoformat()} for sku in query.articles if sku in articles]}

    @app.post("/inventory")
    def inventory(query: Query):
        now = datetime.now(timezone.utc)
        return {"synthetic": True, "observed_at": now.isoformat(), "cutoff": (now + timedelta(hours=2)).isoformat(),
                "items": [{"article": sku, "cases_left": app.state.stock[sku], "delivery_day": query.delivery_day}
                          for sku in query.articles if sku in articles]}

    @app.post("/orders")
    def submit(body: OrderBody, idempotency_key: str = Header(min_length=1)):
        with lock:
            digest = body.model_dump_json()
            if idempotency_key in app.state.keys:
                ref, previous = app.state.keys[idempotency_key]
                if previous != digest:
                    return JSONResponse({"synthetic": True, "error": "IDEMPOTENCY_CONFLICT"}, status_code=409)
                return app.state.orders[ref]
            quantities = {}
            valid = True
            for line in body.items:
                p, qty = articles.get(line.get("article")), line.get("cases")
                valid &= bool(p and isinstance(qty, (int, float)) and not isinstance(qty, bool) and qty > 0
                    and qty >= p["minimum_cases"] and qty % p.get("order_multiple", 1) == 0 and qty % 1 == 0 and line.get("case_price") == p["net_per_case"]
                    and body.currency_code == p["currency"])
                if p and isinstance(qty, (int, float)):
                    quantities[p["article"]] = quantities.get(p["article"], 0) + qty
            valid &= all(qty <= app.state.stock[sku] for sku, qty in quantities.items())
            ref = "synthetic-nf-" + uuid4().hex
            result = {"synthetic": True, "reference": ref, "state": "accepted" if valid else "rejected",
                      "received_at": datetime.now(timezone.utc).isoformat()}
            if valid:
                for sku, qty in quantities.items():
                    app.state.stock[sku] -= qty
            app.state.orders[ref] = result
            app.state.keys[idempotency_key] = (ref, digest)
            result["reserved_cases"] = quantities if valid else {}
            return result

    @app.get("/orders/by-key/{key}")
    def reconcile(key: str):
        with lock:
            previous = app.state.keys.get(key)
            return app.state.orders[previous[0]] if previous else JSONResponse({"error": "NOT_FOUND"}, status_code=404)

    @app.get("/orders/{ref}")
    def status(ref: str):
        return app.state.orders.get(ref) or JSONResponse({"synthetic": True, "error": "NOT_FOUND"}, status_code=404)

    @app.post("/orders/{ref}/cancel")
    def cancel(ref: str):
        with lock:
            order = app.state.orders.get(ref)
            if not order:
                return JSONResponse({"synthetic": True, "error": "NOT_FOUND"}, status_code=404)
            if order["state"] == "accepted":
                for sku, qty in order["reserved_cases"].items():
                    app.state.stock[sku] += qty
                order["state"] = "cancelled"
            return order
    return app


app = create_app()
