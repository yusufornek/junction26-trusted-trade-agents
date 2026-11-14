"""Spec 25 acceptance: the second supplier crosses a real, differently shaped HTTP API."""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.db import DomainError
from app.models import PolicyUpdateRequest
from app.suppliers import SupplierAdapter, OrderExecution
from conftest import agent_headers, quote_request, replay_recorded_demo
from suppliers.nordfood import create_app as nordfood_app


class ASGIBridge(httpx.BaseTransport):
    """Sync gateway -> async in-process supplier; no sockets, server or mock supplier payloads."""
    def __init__(self, app):
        self.app = app
        self.lose_ack = False

    def handle_request(self, request):
        async def send():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app)) as client:
                return await client.request(request.method, str(request.url), headers=request.headers, content=request.content)
        response = asyncio.run(send())
        if self.lose_ack and request.url.path == "/orders":
            self.lose_ack = False
            raise httpx.ReadTimeout("Synthetic lost acknowledgement", request=request)
        return httpx.Response(response.status_code, headers=response.headers, content=response.content)


@pytest.fixture
def nordfood(gateway):
    app = nordfood_app()
    adapter = gateway.suppliers.get("nordfood")
    adapter.client.close()
    bridge = ASGIBridge(app)
    adapter.client = httpx.Client(transport=bridge)
    return app, adapter, bridge


def reference(need, **updates):
    return need.model_copy(update={"supplier_id": "nordfood", **updates})


def prepare(gateway, need, units=2):
    lookup = gateway.find_offers(need)
    return gateway.orders.create_quote(quote_request(need, qty=units, product_id="NF-001", need_ref=lookup.lookup_id))


@pytest.mark.parametrize("registered_agents", [("external", "web-console")], indirect=True)
def test_ac_1_valio_adapter_keeps_recorded_demo_best_and_total(gateway, need, respx_mock):
    adapter = gateway.suppliers.get("valio")
    assert isinstance(adapter, SupplierAdapter) and isinstance(adapter.execution, OrderExecution)
    result = replay_recorded_demo(gateway, need, respx_mock)
    assert (result.offers[0].product_id, result.offers[0].purchase_total) == ("464306", 1010)


def test_ac_2_nordfood_ranks_case_prices_and_respects_minimum(gateway, need, upstream, nordfood):
    result = gateway.find_offers(reference(need))
    best = result.offers[0]
    assert (best.product_id, best.sales_unit, best.size_per_sales_unit) == ("NF-001", "CASE", 12)
    assert (best.unit_price, best.price_per_l_or_kg, best.units_needed, best.purchase_total) == (12, 1, 84, 1008)
    assert best.rank_label == "best" and result.supplier_id == "nordfood" and result.synthetic
    small = gateway.find_offers(reference(need, quantity=need.quantity.model_copy(update={"value": 13})))
    assert next(o for o in small.offers if o.product_id == "NF-001").units_needed == 2
    assert all(p.lines[0].units >= 2 for p in small.plans if len(p.lines) == 1 and p.lines[0].product_id == "NF-001")
    assert nordfood[1].catalog.count() == 60


def test_ac_3_nordfood_order_has_actual_service_acknowledgement(gateway, need, upstream, nordfood):
    quote = prepare(gateway, reference(need))
    order = gateway.orders.place_order(quote.id, "nf-ack")
    assert order.status == "CONFIRMED" and order.commitment == "supplier-acknowledged"
    assert order.execution.mode == "supplier_backed" and order.execution.status == "accepted"
    assert nordfood[0].state.orders[order.execution.supplier_ref]["state"] == "accepted"
    assert order.execution.acknowledged_at and order.synthetic and order.supplier_id == "nordfood"
    refreshed = gateway.find_offers(reference(need))
    assert next(o for o in refreshed.offers if o.product_id == "NF-001").availability.available_amount == 4998
    assert nordfood[1].execution.status(order.execution.supplier_ref).status == "accepted"
    cancelled = gateway.orders.cancel(order.id, "agent:external")
    assert cancelled.execution.status == "cancelled" and nordfood[0].state.stock["NF-001"] == 5000


def test_ac_4_lost_ack_retry_creates_one_supplier_order(gateway, need, upstream, nordfood):
    quote = prepare(gateway, reference(need))
    nordfood[2].lose_ack = True
    pending = gateway.orders.place_order(quote.id, "nf-lost-ack")
    assert pending.status == "PENDING_EXECUTION" and pending.execution.status == "unknown"
    assert pending.next_action == "RETRY_SAME_COMMAND"
    assert pending.commitment == "gateway-simulated"
    with gateway.db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM purchase_memory WHERE json_extract(data,'$.order_id')=?", (pending.id,)).fetchone()[0] == 0
    final = gateway.orders.place_order(quote.id, "nf-lost-ack")
    assert final.id == pending.id and final.status == "CONFIRMED"
    assert final.commitment == "supplier-acknowledged"
    assert len(nordfood[0].state.orders) == 1 and nordfood[0].state.stock["NF-001"] == 4998
    with gateway.db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM events WHERE aggregate_id=? AND type='order.confirmed'", (final.id,)).fetchone()[0] == 1
        purchase = json.loads(conn.execute("SELECT data FROM purchase_memory WHERE json_extract(data,'$.order_id')=?", (final.id,)).fetchone()[0])
        assert purchase["quantity"] == 24 and purchase["unit"] == "L"
        assert purchase["total_paid"] == final.total
    assert gateway.orders.place_order(quote.id, "nf-lost-ack").id == final.id
    with gateway.db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM purchase_memory WHERE json_extract(data,'$.order_id')=?", (final.id,)).fetchone()[0] == 1


def test_ac_5_sek_is_refused_without_named_fx_source(gateway, need, upstream, nordfood):
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        response = client.post("/v1/offers", json=reference(need, search_terms=["Swedish"]).model_dump(mode="json"))
    assert response.status_code == 422 and response.json()["code"] == "UNSUPPORTED_CURRENCY"
    assert "FX rate source" in response.json()["message"]


def test_ac_6_shared_gtin_keeps_supplier_packaging(gateway, need, upstream, nordfood, tmp_path):
    from conftest import raw_product, rebuild_catalog
    raw = raw_product("same-sku", "Milk 1 L lactose free", sales="ST", volume=1, multiplier=1)
    raw["salesUnitGtin"] = "6408430000487"
    rebuild_catalog(gateway, tmp_path, [raw])
    valio = gateway.suppliers.get("valio").catalog.get("same-sku")
    nf = nordfood[1].catalog.get("NF-001")
    assert valio.gtin == nf.gtin
    assert (valio.sales_unit, valio.units["ST"]) == ("ST", 1)
    assert (nf.sales_unit, nf.units["CASE"]) == ("CASE", 12)
    with gateway.db.connect() as conn:
        assert conn.execute("SELECT supplier_sku FROM supplier_products WHERE supplier_id='nordfood' AND supplier_sku='NF-001'").fetchone()


def test_ac_7_operator_and_buyer_mandates_restrict_supplier(gateway, need, upstream, nordfood):
    policy = gateway.policy.get().model_dump(mode="json")
    policy["agents"]["external"]["supplier_ids"] = ["valio"]
    gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Operator"))
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        refused = client.post("/v1/offers", json=reference(need).model_dump(mode="json"))
    assert refused.status_code == 403 and refused.json()["code"] == "SUPPLIER_NOT_IN_MANDATE"
    # Buyer VC restriction is independent of operator policy.
    from test_verifiable_mandate import issue, ISSUER
    from app.signing import b64
    from app.mandates import decode, sign_jwt
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.generate()
    policy["customers"]["1"] = {"mandate_issuers": {ISSUER: [{"kid": "buyer-1", "x": b64(key.public_key().public_bytes_raw())}]}}
    gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Buyer"))
    claims = decode(issue(key))[1]
    claims["vc"]["credentialSubject"]["supplier_ids"] = ["valio"]
    with pytest.raises(DomainError, match="SUPPLIER_NOT_IN_MANDATE"):
        gateway.find_offers(reference(need), "agent:outsider", mandate=sign_jwt(key, claims, kid="buyer-1"))


def test_ac_8_registry_labels_synthetic_nordfood(gateway):
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        response = client.get("/v1/suppliers")
    rows = {r["supplier_id"]: r for r in response.json()}
    assert response.status_code == 200 and rows["nordfood"]["synthetic"] is True
    assert rows["valio"]["synthetic"] is False
    assert rows["nordfood"]["capabilities"]["execution"] == {"mode": "supplier_backed", "atomicity": "atomic", "partial_acceptance": False}
