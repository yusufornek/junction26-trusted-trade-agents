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
    assert nordfood[1].catalog.count() == 60
    nordfood[1].catalog.ingest([nordfood[1].catalog.get("NF-001")])
    small = gateway.find_offers(reference(need, quantity=need.quantity.model_copy(update={"value": 1})))
    assert small.offers[0].units_needed == 2
    assert small.plans and all(p.lines[0].units == 2 for p in small.plans)


def test_ac_3_nordfood_order_has_actual_service_acknowledgement(gateway, need, upstream, nordfood):
    quote = prepare(gateway, reference(need))
    order = gateway.orders.place_order(quote.id, "nf-ack")
    assert order.status == "PENDING_APPROVAL"  # Case-only price cannot independently confirm catalog litres.
    order = gateway.orders.approve(order.id, "Human")
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
    order = gateway.orders.place_order(quote.id, "nf-lost-ack")
    assert order.status == "PENDING_APPROVAL"
    nordfood[2].lose_ack = True
    pending = gateway.orders.approve(order.id, "Human")
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
    assert rows["nordfood"]["capabilities"]["execution"] == {"mode": "supplier_backed", "atomicity": "atomic", "partial_acceptance": False,
                                                           "consistency_window_seconds": 0}


@pytest.mark.parametrize("requested,expected", [(1, 2), (25, 3)])
def test_review_minimum_is_lower_bound(gateway, need, upstream, nordfood, requested, expected):
    request = reference(need, quantity=need.quantity.model_copy(update={"value": requested}))
    product = nordfood[1].catalog.get("NF-001")
    nordfood[1].catalog.ingest([product])
    result = gateway.find_offers(request)
    assert (result.offers[0].units_needed, result.offers[0].purchase_total) == (expected, expected * 12)
    assert result.plans and result.plans[0].lines[0].units == expected
    quote = gateway.orders.create_quote(quote_request(request, qty=expected, product_id="NF-001", need_ref=result.lookup_id))
    assert quote.lines[0].units_needed == expected
    with TestClient(nordfood[0]) as client:
        ack = client.post("/orders", headers={"Idempotency-Key": "minimum"}, json={"buyer": "1", "currency_code": "EUR",
            "items": [{"article": "NF-001", "cases": expected, "case_price": 12}]}).json()
    assert ack["state"] == "accepted"


def test_review_supplier_size_requires_independent_evidence(gateway, need, upstream, nordfood):
    request = reference(need, quantity=need.quantity.model_copy(update={"value": 25}))
    result = gateway.find_offers(request)
    offer = next(o for o in result.offers if o.product_id == "NF-001")
    assert not offer.size_confirmed_by_price
    assert "SIZE_UNCONFIRMED" in {w.code for w in offer.warnings}
    quote = gateway.orders.create_quote(quote_request(request, qty=3, product_id="NF-001", need_ref=result.lookup_id))
    order = gateway.orders.place_order(quote.id, "unconfirmed")
    assert order.status == "PENDING_APPROVAL" and not nordfood[0].state.orders


def executable_quote(gateway, need):
    return prepare(gateway, reference(need, quantity=need.quantity.model_copy(update={"value": 2, "unit": "SALES_UNIT"})))


@pytest.mark.parametrize("withdrawal", ["removed", "approval", "limit", "suspended", "credential", "rotated"])
def test_review_dispatch_rechecks_authority(gateway, need, upstream, nordfood, monkeypatch, withdrawal):
    quote = executable_quote(gateway, need)
    original = nordfood[1].client.request
    def unavailable(method, url, **kwargs):
        if method == "POST" and str(url).endswith("/orders"):
            raise httpx.ConnectTimeout("Before delivery")
        return original(method, url, **kwargs)
    monkeypatch.setattr(nordfood[1].client, "request", unavailable)
    from app.foundation import PRINCIPAL
    principal = gateway.settings.keys.resolve(agent_headers("external")["Authorization"].split()[1], gateway.policy.get())
    token = PRINCIPAL.set(principal)
    try:
        pending = gateway.orders.place_order(quote.id, "dispatch-authority")
        assert pending.status == "PENDING_EXECUTION" and not nordfood[0].state.orders
        policy = gateway.policy.get().model_dump(mode="json")
        if withdrawal == "removed":
            del policy["agents"]["external"]
        elif withdrawal == "approval":
            policy["agents"]["external"]["autonomy"] = "approval_required"
        elif withdrawal == "limit":
            policy["agents"]["external"]["max_auto_total_eur"] = 0
        elif withdrawal == "suspended":
            with gateway.db.connect(write=True) as conn:
                conn.execute("INSERT OR REPLACE INTO buyer_suspensions VALUES(?,?)", ("external", json.dumps({"suspended": True})))
        else:
            from app.auth import Credentials
            gateway.settings.keys = Credentials("operator", "external:replacement" if withdrawal == "rotated" else "other:replacement")
        if withdrawal != "suspended":
            # The operator changes authority independently of the buyer's command.
            operator_token = PRINCIPAL.set(None)
            try:
                gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Operator"))
            finally:
                PRINCIPAL.reset(operator_token)
        monkeypatch.setattr(nordfood[1].client, "request", original)
        try:
            final = gateway.orders.place_order(quote.id, "dispatch-authority")
            assert final.status != "CONFIRMED"
        except DomainError as error:
            assert error.status in {401, 403, 409}
        assert not nordfood[0].state.orders
        assert any(e.action.endswith("FAILED") for e in gateway.audit())
    finally:
        PRINCIPAL.reset(token)


@pytest.mark.parametrize("lost_ack", [False, True])
def test_review_local_failure_never_orphans_supplier_purchase(gateway, need, upstream, nordfood, lost_ack):
    import sqlite3
    quote = executable_quote(gateway, need)
    nordfood[0].state.stock["NF-001"] = 2
    nordfood[2].lose_ack = lost_ack
    with gateway.db.connect(write=True) as conn:
        conn.execute("CREATE TRIGGER review_fail_audit BEFORE INSERT ON audit WHEN NEW.action='PLACE_ORDER' BEGIN SELECT RAISE(ABORT,'review failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        gateway.orders.place_order(quote.id, "durable-intent")
    # Outbound dispatch must happen only after its local intent and audit commit.
    assert not nordfood[0].state.orders
    with gateway.db.connect(write=True) as conn:
        conn.execute("DROP TRIGGER review_fail_audit")
    order = gateway.orders.place_order(quote.id, "durable-intent")
    if order.status == "PENDING_EXECUTION":
        order = gateway.orders.place_order(quote.id, "durable-intent")
    assert order.status == "CONFIRMED" and len(nordfood[0].state.orders) == 1
    assert gateway.orders.get_order(order.id).execution.supplier_ref == order.execution.supplier_ref


@pytest.mark.parametrize("recovery", ["placement", "background"])
def test_review_approval_lost_ack_returns_current_state_and_events(gateway, need, upstream, nordfood, recovery):
    quote = prepare(gateway, reference(need))
    policy = gateway.policy.get().model_dump(mode="json")
    policy["agents"]["external"]["autonomy"] = "approval_required"
    gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Operator"))
    order = gateway.orders.place_order(quote.id, "approval-lost-ack")
    assert order.status == "PENDING_APPROVAL"
    nordfood[2].lose_ack = True
    pending = gateway.orders.approve(order.id, "Human")
    assert pending.status == "PENDING_EXECUTION"
    with gateway.db.connect() as conn:
        confirmations = conn.execute("SELECT payload FROM events WHERE aggregate_id=? AND type='order.confirmed'", (order.id,)).fetchall()
    assert not confirmations
    if recovery == "background":
        gateway.orders.dispatch_pending()
    replay = gateway.orders.place_order(quote.id, "approval-lost-ack")
    assert replay.status == gateway.orders.get_order(order.id).status == "CONFIRMED"
    assert replay.execution.status == "accepted" and len(nordfood[0].state.orders) == 1
    assert gateway.orders.place_order(quote.id, "approval-lost-ack").model_dump(mode="json") == replay.model_dump(mode="json")
    with gateway.db.connect() as conn:
        confirmations = conn.execute("SELECT payload FROM events WHERE aggregate_id=? AND type='order.confirmed'", (order.id,)).fetchall()
    assert len(confirmations) == 1 and json.loads(confirmations[0][0])["status"] == "CONFIRMED"


def test_review_supplier_ranking_statistics(gateway, nordfood):
    products = nordfood[1].catalog.rows()[:3]
    for p, co2, reliability in zip(products, [2, 10, None], [.8, 1, None]):
        p.co2_kg_per_kg = co2
        p.delivery_reliability = reliability
    nordfood[1].catalog.ingest(products)
    stats = nordfood[1].catalog.metadata("ranking")
    assert stats["co2"] == {"min": 2, "max": 10, "median": 6}
    assert stats["reliability"]["median"] == .9


def test_review_nordfood_only_signed_mandate_can_approve(gateway, need, upstream, nordfood):
    from test_verifiable_mandate import issue, ISSUER
    from app.signing import b64
    from app.mandates import decode, sign_jwt
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.generate()
    policy = gateway.policy.get().model_dump(mode="json")
    policy["customers"]["1"] = {"mandate_issuers": {ISSUER: [{"kid": "buyer-1", "x": b64(key.public_key().public_bytes_raw())}]}}
    gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Buyer"))
    claims = decode(issue(key))[1]
    claims["vc"]["credentialSubject"]["supplier_ids"] = ["nordfood"]
    claims["vc"]["credentialSubject"]["autonomy"] = "approval_required"
    token = sign_jwt(key, claims, kid="buyer-1")
    request = reference(need, quantity=need.quantity.model_copy(update={"value": 25}))
    result = gateway.find_offers(request, "agent:outsider", mandate=token)
    quote = gateway.orders.create_quote(quote_request(request, qty=3, product_id="NF-001", need_ref=result.lookup_id), "outsider", mandate=token)
    order = gateway.orders.place_order(quote.id, "nf-only", "outsider", mandate=token)
    assert order.status == "PENDING_APPROVAL"
    assert gateway.orders.approve(order.id, "Human").status == "CONFIRMED"


@pytest.mark.parametrize("qty,accepted", [(1, False), (2, True), (3, True), (3.5, False)])
def test_review_supplier_whole_case_minimum_validation(gateway, nordfood, qty, accepted):
    with TestClient(nordfood[0]) as client:
        response = client.post("/orders", headers={"Idempotency-Key": "qty"}, json={"buyer": "1", "currency_code": "EUR",
            "items": [{"article": "NF-001", "cases": qty, "case_price": 12}]}).json()
    assert (response["state"] == "accepted") == accepted


@pytest.mark.parametrize("lost_ack", [False, True])
def test_review_ack_persistence_failure_reconciles_after_restart(gateway, need, upstream, nordfood, lost_ack):
    import sqlite3
    from app.service import Gateway
    quote = executable_quote(gateway, need)
    nordfood[0].state.stock["NF-001"] = 2
    nordfood[2].lose_ack = lost_ack
    with gateway.db.connect(write=True) as conn:
        conn.execute("CREATE TRIGGER review_ack_failure BEFORE INSERT ON audit WHEN NEW.action='SUPPLIER_ACKNOWLEDGED' BEGIN SELECT RAISE(ABORT,'ack audit'); END")
    if lost_ack:
        assert gateway.orders.place_order(quote.id, "restart-intent").status == "PENDING_EXECUTION"
        with pytest.raises(sqlite3.IntegrityError):
            gateway.orders.place_order(quote.id, "restart-intent")
    else:
        with pytest.raises(sqlite3.IntegrityError):
            gateway.orders.place_order(quote.id, "restart-intent")
    assert len(nordfood[0].state.orders) == 1 and nordfood[0].state.stock["NF-001"] == 0
    with gateway.db.connect(write=True) as conn:
        conn.execute("DROP TRIGGER review_ack_failure")
        assert conn.execute("SELECT count(*) FROM supplier_execution_outbox WHERE status='pending'").fetchone()[0] == 1
    restarted = Gateway(gateway.settings)
    try:
        adapter = restarted.suppliers.get("nordfood")
        adapter.client.close()
        adapter.client = httpx.Client(transport=nordfood[2])
        restarted.orders.dispatch_pending()
        with restarted.db.connect() as conn:
            row = conn.execute("SELECT data FROM orders").fetchone()
        order = json.loads(row[0])
        assert order["status"] == "CONFIRMED" and order["execution"]["supplier_ref"] in nordfood[0].state.orders
        assert len(nordfood[0].state.orders) == 1
    finally:
        restarted.close()


def test_review_supplier_missing_catalog_facts_do_not_become_proof(gateway, need, upstream, nordfood, monkeypatch):
    from pathlib import Path
    article = dict(nordfood[1].articles[0])
    for field in ["bottles_per_case", "litres_per_bottle", "allergen_status"]:
        article.pop(field, None)
    control = {**nordfood[1].articles[1], "description": "Control product", "allergen_status": {"AE": ["contains"]}}
    read = Path.read_text
    def sparse(path, *args, **kwargs):
        if path.name == "catalog.json" and path.parent.name == "nordfood":
            return json.dumps([article, control])
        return read(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", sparse)
    nordfood[1].ingest_catalog()
    product = nordfood[1].catalog.get("NF-001")
    assert not product.has_allergen_declaration and not product.allergen_status
    assert not product.units and product.base_volume_l is None
    result = gateway.find_offers(reference(need))
    assert not result.offers
    unchecked = reference(need, exclude_allergens=[])
    result = gateway.find_offers(unchecked)
    assert result.offers and "SIZE_UNKNOWN" in {w.code for w in result.offers[0].warnings}


def test_review_operator_dispatch_keeps_operator_authority(gateway, need, upstream, nordfood):
    from conftest import OPERATOR_HEADERS
    request = reference(need, quantity=need.quantity.model_copy(update={"value": 2, "unit": "SALES_UNIT"}))
    lookup = gateway.find_offers(request, "agent:web-console")
    quote = gateway.orders.create_quote(quote_request(request, qty=2, product_id="NF-001", need_ref=lookup.lookup_id), "web-console")
    with TestClient(create_app(gateway), headers=OPERATOR_HEADERS) as client:
        result = client.post("/v1/orders", headers={"Idempotency-Key": "operator-nf"}, json={"quote_id": quote.id})
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "CONFIRMED" and len(nordfood[0].state.orders) == 1


def test_review_reconciliation_records_purchase_after_authority_removed(gateway, need, upstream, nordfood, monkeypatch):
    quote = executable_quote(gateway, need)
    nordfood[2].lose_ack = True
    order = gateway.orders.place_order(quote.id, "accepted-before-removal")
    assert order.status == "PENDING_EXECUTION" and len(nordfood[0].state.orders) == 1
    policy = gateway.policy.get().model_dump(mode="json")
    del policy["agents"]["external"]
    gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Operator"))
    original = nordfood[1].client.request
    def read_only(method, url, **kwargs):
        assert method == "GET", "Reconciliation must not submit another purchase after revocation"
        return original(method, url, **kwargs)
    monkeypatch.setattr(nordfood[1].client, "request", read_only)
    gateway.orders.dispatch_pending()
    recovered = gateway.orders.get_order(order.id)
    assert recovered.status == "CONFIRMED" and recovered.execution.status == "accepted"
    assert len(nordfood[0].state.orders) == 1


def test_review_execution_guard_refusal_stays_retryable(gateway, need, upstream, nordfood, monkeypatch):
    from app import limits
    quote = executable_quote(gateway, need)
    original = nordfood[1].client.request
    def unavailable(method, url, **kwargs):
        if method == "POST" and str(url).endswith("/orders"):
            raise httpx.ConnectTimeout("Before delivery")
        return original(method, url, **kwargs)
    monkeypatch.setattr(nordfood[1].client, "request", unavailable)
    order = gateway.orders.place_order(quote.id, "execution-guard")
    assert order.status == "PENDING_EXECUTION" and not nordfood[0].state.orders
    monkeypatch.setattr(nordfood[1].client, "request", original)
    guard = limits.UpstreamGuard(concurrency=1, queue_size=0)
    monkeypatch.setattr(limits, "SUPPLIER_GUARD", guard)
    with guard.slot(), pytest.raises(DomainError, match="UPSTREAM_BUSY"):
        gateway.orders.place_order(quote.id, "execution-guard")
    assert gateway.orders.place_order(quote.id, "execution-guard").status == "CONFIRMED"
    assert len(nordfood[0].state.orders) == 1
