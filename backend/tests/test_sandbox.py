"""Spec 26 acceptance: isolated, reproducible requests through the production rules."""
from datetime import timedelta
import json

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.auth import Credentials
from app.config import Settings
from app.db import DomainError
from app.models import NeedSpec, PolicyUpdateRequest
from app.service import Gateway
from app.signing import quote_payload, receipt_payload, verify_jws, verify_quote
from conftest import agent_headers, OPERATOR_HEADERS, quote_request

SANDBOX_KEY = "sandbox-scoped-test-secret"
SANDBOX_ADMIN = "sandbox-scoped-admin"
SANDBOX_HEADERS = {"Authorization": "Bearer " + SANDBOX_KEY}
ADMIN_HEADERS = {"Authorization": "Bearer " + SANDBOX_ADMIN}


@pytest.fixture
def sandbox_gateway(tmp_path, gateway):
    settings = Settings(db_path=tmp_path / "sandbox/app.db", environment="sandbox", policy_path=gateway.settings.policy_path,
                        keys=Credentials(SANDBOX_ADMIN, "external:" + SANDBOX_KEY, environment="sandbox"))
    service = Gateway(settings)
    yield service
    service.close()


def sandbox_need(service, value=1000):
    with service.activate():
        return NeedSpec(customer_id="1", intent="milk", search_terms=["milk"], required_claims=["FREE_FROM_LACTOSE"],
                        exclude_allergens=["AE"], quantity={"value": value, "unit": "L"},
                        deliver_by=service.sandbox.now().date() + timedelta(days=3))


def purchase(service, *, key="sandbox-order", units=100):
    need = sandbox_need(service)
    lookup = service.find_offers(need)
    with service.activate():
        request = quote_request(need, qty=units, need_ref=lookup.lookup_id)
    quote = service.orders.create_quote(request)
    return quote, service.orders.place_order(quote.id, key)


def test_ac_1_split_stock_reset_repeats_offers_plans_and_totals(sandbox_gateway):
    service = sandbox_gateway
    outputs = []
    with TestClient(create_app(service), headers=ADMIN_HEADERS) as client:
        assert len(client.get("/v1/sandbox/scenarios").json()) == 7
        for _ in range(2):
            assert client.post("/v1/sandbox/reset", json={"scenario": "split_stock"}).status_code == 200
            result = client.post("/v1/offers", headers=SANDBOX_HEADERS, json=sandbox_need(service).model_dump(mode="json"))
            assert result.status_code == 200 and result.headers["X-Environment"] == "sandbox"
            body = result.json()
            outputs.append((body["offers"], body["plans"]))
    assert outputs[0] == outputs[1]
    splits = [p for p in outputs[0][1] if p["kind"] == "split"]
    assert splits and splits[0]["total"] == 1120 and splits[0]["uncovered"]["value"] == 0
    assert all(o["provenance"]["price"]["source"].startswith("fixture:split_stock:v1") for o in outputs[0][0])


def test_ac_2_approval_time_stock_change_uses_evidence_revision(sandbox_gateway):
    service = sandbox_gateway
    policy = service.policy.get().model_dump(mode="json")
    policy["agents"]["external"]["autonomy"] = "approval_required"
    service.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Sandbox operator"))
    service.sandbox.reset("approval_time_stock_change")
    quote, order = purchase(service)
    assert order.status == "PENDING_APPROVAL" and service.sandbox.changed
    with pytest.raises(DomainError, match="EVIDENCE_CHANGED") as error:
        service.orders.approve(order.id, "Reviewer", evidence_revision=1)
    assert error.value.data["evidence_revision"] == 2
    refreshed = service.orders.get_order(order.id)
    assert refreshed.status == "PENDING_APPROVAL" and refreshed.lines[0].availability.available_amount == 10


def test_ac_3_supplier_timeout_has_retryable_recovery(sandbox_gateway):
    service = sandbox_gateway
    service.sandbox.reset("supplier_timeout")
    with TestClient(create_app(service), headers=SANDBOX_HEADERS) as client:
        response = client.post("/v1/offers", json=sandbox_need(service).model_dump(mode="json"))
    body = response.json()
    assert response.status_code == 502 and body["code"] == "SUPPLIER_UNAVAILABLE"
    assert body["retryable"] and body["next_action"] == "RETRY_SAME_COMMAND"
    assert body["environment"] == "sandbox" and response.headers["Retry-After"] == "1"


def test_ac_4_scoped_credentials_and_storage_are_disjoint(gateway, sandbox_gateway, need, upstream, tmp_path):
    from scripts.gen_keys import provision
    file = tmp_path / "sandbox.env"
    assert provision(file, {"new-agent"}, sandbox=True)
    assert "SANDBOX_AGENT_KEYS=new-agent:" in file.read_text() and "\nAGENT_KEYS=" not in file.read_text()
    quote, order = purchase(sandbox_gateway)
    live_lookup = gateway.find_offers(need)
    live_quote = gateway.orders.create_quote(quote_request(need, qty=10, need_ref=live_lookup.lookup_id))
    with TestClient(create_app(gateway)) as live, TestClient(create_app(sandbox_gateway)) as sandbox:
        assert live.get("/v1/orders/" + order.id, headers=SANDBOX_HEADERS).status_code == 401
        assert sandbox.get("/v1/quotes/" + live_quote.id, headers=agent_headers("external")).status_code == 401
        assert live.get("/v1/quotes/" + quote.id, headers=agent_headers("external")).status_code == 404
        assert sandbox.get("/v1/quotes/" + live_quote.id, headers=SANDBOX_HEADERS).status_code == 404
        assert live.post("/v1/offers", headers=SANDBOX_HEADERS, json=need.model_dump(mode="json")).status_code == 401
        assert sandbox.post("/v1/offers", headers=agent_headers("external"), json=sandbox_need(sandbox_gateway).model_dump(mode="json")).status_code == 401
        assert live.post("/v1/sandbox/reset", headers=OPERATOR_HEADERS, json={"scenario": "full_stock"}).status_code == 403
        assert sandbox.post("/v1/sandbox/reset", headers=SANDBOX_HEADERS, json={"scenario": "full_stock"}).status_code == 403
    # Same DB path is rejected even if an operator accidentally misconfigures a deployment.
    with pytest.raises(ValueError, match="cannot be shared"):
        Gateway(Settings(db_path=gateway.db.path, policy_path=gateway.settings.policy_path, environment="sandbox"))


def test_ac_5_reset_expires_promises_and_preserves_explicit_reason(sandbox_gateway):
    service = sandbox_gateway
    need = sandbox_need(service)
    lookup = service.find_offers(need)
    with service.activate():
        quote = service.orders.create_quote(quote_request(need, qty=100, need_ref=lookup.lookup_id))
        pending_quote = service.orders.create_quote(quote_request(need, qty=1800, need_ref=lookup.lookup_id))
    pending = service.orders.place_order(pending_quote.id, "pending-reset")
    assert pending.status == "PENDING_APPROVAL"
    service.sandbox.reset("full_stock")
    with pytest.raises(DomainError, match="sandbox_reset") as error:
        service.orders.place_order(quote.id, "after-reset")
    assert error.value.code == "EXPIRED_QUOTE" and error.value.data["reason"] == "sandbox_reset"
    assert verify_quote(service.orders.get_quote(quote.id), service.signing.jwks())
    expired = service.orders.get_order(pending.id)
    assert expired.status == "EXPIRED" and expired.expiration_reason == "sandbox_reset"
    with service.db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM reservations").fetchone()[0] == 0


def test_ac_6_signed_quote_receipt_and_ubl_label_sandbox(sandbox_gateway):
    service = sandbox_gateway
    quote, order = purchase(service)
    payload = quote_payload(quote)
    assert payload["environment"] == "sandbox" and payload["synthetic"] is True
    assert verify_jws(quote.signature, payload, service.signing.jwks())
    assert receipt_payload(order)["environment"] == "sandbox"
    assert verify_jws(order.receipt, receipt_payload(order), service.signing.jwks())
    from app import audit
    with service.activate():
        head = audit.head(service.db, service.signing)
    assert head["environment"] == "sandbox"
    assert verify_jws(head["jws"], {k: v for k, v in head.items() if k != "jws"}, service.signing.jwks())
    for document in ("order", "response"):
        xml = service.orders.get_order_ubl(order.id, document)
        assert "environment: sandbox" in xml and "synthetic: true" in xml
        from test_ubl_export import schema
        # Existing schema fixture is exercised by the regression suite; validate directly here too.
        import xmlschema
        from app.config import BACKEND
        kind = "Order" if document == "order" else "OrderResponse"
        xmlschema.XMLSchema(BACKEND / f"vendor/ubl-2.1/xsd/maindoc/UBL-{kind}-2.1.xsd").validate(xml)


def test_ac_7_sandbox_spend_is_absent_from_live_daily_budget(gateway, sandbox_gateway, need, upstream):
    _, sandbox_order = purchase(sandbox_gateway, units=1000)
    assert sandbox_order.status == "CONFIRMED"
    with sandbox_gateway.activate(), sandbox_gateway.db.connect() as conn:
        assert sandbox_gateway.policy.autonomous_total(conn, "external") == 1000
    with gateway.db.connect() as conn:
        assert gateway.policy.autonomous_total(conn, "external") == 0
    lookup = gateway.find_offers(need)
    quote = gateway.orders.create_quote(quote_request(need, qty=1, need_ref=lookup.lookup_id))
    assert gateway.orders.place_order(quote.id, "independent-live").status == "CONFIRMED"
    sandbox_gateway.sandbox.reset("full_stock")
    with sandbox_gateway.activate(), sandbox_gateway.db.connect() as conn:
        assert sandbox_gateway.policy.autonomous_total(conn, "external") == 0


@pytest.mark.parametrize("scenario", ["missing_evidence", "shifted_date", "expired_quote"])
def test_versioned_scenarios_exercise_real_rules(sandbox_gateway, scenario):
    service = sandbox_gateway
    service.sandbox.reset(scenario)
    lookup = service.find_offers(sandbox_need(service))
    if scenario == "missing_evidence":
        assert "1" not in {o.product_id for o in lookup.offers} and lookup.excluded_count == 1
    elif scenario == "shifted_date":
        assert all(any(w.code == "DATE_SHIFT" for w in o.warnings) for o in lookup.offers)
    else:
        with service.activate():
            quote = service.orders.create_quote(quote_request(lookup.need, qty=10, need_ref=lookup.lookup_id))
        with pytest.raises(DomainError, match="EXPIRED_QUOTE"):
            service.orders.place_order(quote.id, "fixture-expiry")


@pytest.mark.parametrize("approval", [False, True])
def test_review_nordfood_simulated_execution_can_confirm(sandbox_gateway, approval):
    service = sandbox_gateway
    policy = service.policy.get().model_dump(mode="json")
    policy["agents"]["external"]["autonomy"] = "approval_required" if approval else "autonomous_within_limits"
    service.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Fixture"))
    need = sandbox_need(service, 10).model_copy(update={"supplier_id": "nordfood"})
    lookup = service.find_offers(need)
    with service.activate():
        quote = service.orders.create_quote(quote_request(need, qty=2, need_ref=lookup.lookup_id))
    order = service.orders.place_order(quote.id, "sandbox-nf")
    if approval:
        order = service.orders.approve(order.id, "Human")
    assert order.status == "CONFIRMED" and order.commitment == "gateway-simulated"
    assert order.execution.mode == "simulated"


def test_review_mcp_catalog_has_sandbox_metadata(sandbox_gateway):
    import asyncio
    from app.mcp_server import create_server
    result = asyncio.run(create_server(sandbox_gateway, "external").call_tool("search_products", {"query": "milk"}))
    assert not result.is_error and result.meta["environment"] == "sandbox"


def test_review_sandbox_policy_event_environment(sandbox_gateway):
    with TestClient(create_app(sandbox_gateway), headers=ADMIN_HEADERS) as client:
        policy = sandbox_gateway.policy.get().model_dump(mode="json")
        result = client.put("/v1/policy", json={"policy": policy, "updated_by": "Human"})
        assert result.status_code == 200
    with sandbox_gateway.db.connect() as conn:
        events = conn.execute("SELECT environment FROM events WHERE type='policy.updated'").fetchall()
    assert events and all(r[0] == "sandbox" for r in events)


def test_review_sandbox_procurement_environment(sandbox_gateway):
    service = sandbox_gateway
    quote, order = purchase(service, units=2)
    lookup = service.lookup.get(quote.need_ref)
    for resource in (lookup, quote, order, service.orders.get_order(order.id)):
        assert resource.context["environment"] == resource.environment == "sandbox"
    from app.auth import Principal
    from app.evidence import build_bundle
    assert build_bundle(service, order.id, Principal("external", customer_ids=("1",)))[0]["environment"] == "sandbox"
    with service.db.connect() as conn:
        assert all(r[0] == "sandbox" for r in conn.execute("SELECT environment FROM events"))
