"""Nine focused acceptance tests for the purchase intent mechanism (offline)."""
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from app import audit
from app.api import create_app
from app.auth import Principal
from app.db import DomainError
from app.foundation import PRINCIPAL
from app.mandates import sign_jwt
from app.mcp_server import create_server
from app.models import Execution, PolicyUpdateRequest
from app.purchase_intent_integrity import IntentRequest, Amendment, Transition, SupplierEvent, Faults
from app.recovery import problem
from app.signing import b64
from conftest import agent_headers, quote_request, demo_catalog, OPERATOR_HEADERS

pytestmark = pytest.mark.parametrize("registered_agents", [("external", "agent-b", "other")], indirect=True)


def request(need, key="purchase-1", products=("1",), quantity=10, **extra):
    return IntentRequest(client_intent_id=key, customer_id=need.customer_id, deliver_by=need.deliver_by,
        lines=[{"product_id": p, "quantity_sales_units": quantity} for p in products], **extra)


def quote(gateway, need, intent, upstream):
    lookup = gateway.find_offers(need, actor="agent:external")
    body = quote_request(need, qty=intent["request"]["lines"][0]["quantity_sales_units"], need_ref=lookup.lookup_id)
    body.lines = [body.lines[0].model_copy(update={"product_id": line["product_id"], "quantity_sales_units": line["quantity_sales_units"]}) for line in intent["request"]["lines"]]
    body.intent_id, body.intent_version = intent["id"], intent["version"]
    return gateway.orders.create_quote(body, "external")


def delayed(gateway, need, upstream, monkeypatch, **kwargs):
    obj = gateway.intents.create(request(need, **kwargs), "external")
    q = quote(gateway, need, obj, upstream)
    monkeypatch.setattr(gateway.settings, "environment", "sandbox")
    gateway.intents.set_faults(obj["id"], Faults(confirmation_delay_seconds=86400))
    order = gateway.orders.place_order(q.id, "placement:" + obj["id"], "external")
    return gateway.intents.get(obj["id"], "external"), order


def test_AC_1_dropped_response_replays_rest_mcp_one_order(gateway, need, upstream, monkeypatch):
    body = request(need)
    with TestClient(create_app(gateway, serve_mcp=False)) as client:
        obj = client.post("/v1/intents", json=body.model_dump(mode="json"), headers=agent_headers("external")).json()
        q = quote(gateway, need, obj, upstream)
        monkeypatch.setattr(gateway.settings, "environment", "sandbox")
        monkeypatch.setattr(gateway.settings.keys, "environment", "sandbox")
        gateway.intents.set_faults(obj["id"], Faults(drop_order_response=True))
        lost = client.post("/v1/orders", json={"quote_id": q.id}, headers={**agent_headers("external"), "Idempotency-Key": "lost"})
        assert lost.status_code == 503, lost.text
        retry = client.post("/v1/intents", json=body.model_dump(mode="json"), headers=agent_headers("external"))
        assert retry.status_code == 200, retry.text
        rest = retry.json()
        mcp = asyncio.run(create_server(gateway, agent_id="external").call_tool("create_intent", {"request": body.model_dump(mode="json")}))
        assert not mcp.is_error, mcp
        mcp_result = mcp.structured_content or json.loads(mcp.content[0].text)
        assert "order" in mcp_result, mcp_result
        assert mcp_result["order"]["id"] == rest["order"]["id"]
        assert rest["state"] == "CONFIRMED"
        assert rest["order"]["intent_id"] == obj["id"]
        assert rest["order"]["policy_version_id"] == q.policy_version_id
        from app.a2a import t, data_part
        from google.protobuf.json_format import MessageToDict
        principal = Principal("external", tenant_id="hotel-aurora", customer_ids=("1",))
        token = PRINCIPAL.set(principal)
        try:
            task = gateway.a2a.send(t.SendMessageRequest(message=t.Message(message_id="intent-a2a", role=t.Role.ROLE_USER,
                parts=[data_part({"version": 1, "command": "create_intent", "arguments": {"request": body.model_dump(mode="json")}})])), principal)
            a2a_result = MessageToDict(task.artifacts[0].parts[0])["data"]
            assert a2a_result["order"]["id"] == rest["order"]["id"]
            assert gateway.a2a.get(task.id, principal).status.state == t.TaskState.TASK_STATE_COMPLETED
        finally:
            PRINCIPAL.reset(token)
        with gateway.db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1


def test_AC_2_changed_payload_requires_explicit_amendment(gateway, need, upstream):
    original = gateway.intents.create(request(need), "external")
    changed = request(need, quantity=20)
    with pytest.raises(DomainError) as caught:
        gateway.intents.create(changed, "external")
    assert caught.value.code == "INTENT_AMENDMENT_REQUIRED" and caught.value.status == 409
    assert {"field": "lines[0].quantity_sales_units", "before": 10, "after": 20} in caught.value.data["difference"]
    amended = gateway.intents.amend(original["id"], Amendment(expected_version=1, request=changed), "external")
    assert amended["version"] == 2 and amended["digest"] != original["digest"]
    assert gateway.intents.create(changed, "external")["id"] == original["id"]
    with gateway.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE type='INTENT_AMENDED'").fetchone()[0] == 1
    q = quote(gateway, need, amended, upstream)
    original_order = gateway.orders.place_order(q.id, "before-amendment", "external")
    latest = gateway.intents.get(original["id"], "external")
    change = request(need, quantity=30)
    staged = gateway.intents.amend(original["id"], Amendment(expected_version=latest["version"], request=change), "external")
    assert staged["next_action"] == "WAIT_FOR_APPROVAL" and staged["order"]["id"] == original_order.id
    lookup = gateway.find_offers(need, actor="agent:external")
    replacement = gateway.orders.create_quote(quote_request(need, qty=30, need_ref=lookup.lookup_id), "external")
    with TestClient(create_app(gateway, serve_mcp=False)) as client:
        review = {"expected_version": staged["version"], "quote_id": replacement.id, "approver": "Ada"}
        url = f"/v1/intents/{original['id']}/amendments/approve"
        preview = client.post(url, json=review, headers=OPERATOR_HEADERS)
        assert preview.status_code == 200 and preview.json()["confirmation_required"]
        assert preview.json()["summary"]["order"]["id"] == original_order.id
        assert preview.json()["summary"]["changes"]["replacement_quote"]["id"] == replacement.id
        response = client.post(url, json={**review, "confirmation_token": preview.json()["confirmation_token"]}, headers=OPERATOR_HEADERS)
        assert response.status_code == 200, response.text
        approved = response.json()
    assert approved["order"]["lines"][0]["units_needed"] == 30
    assert approved["order"]["status"] == "CONFIRMED"
    assert gateway.orders.get_order(original_order.id).status == "CANCELLED"


def test_AC_3_concurrent_cancel_confirm_has_one_winner(gateway, need, upstream, monkeypatch):
    obj, order = delayed(gateway, need, upstream, monkeypatch)
    barrier = Barrier(2)

    def run(state):
        token = PRINCIPAL.set(Principal("external", operator=True))
        try:
            barrier.wait()
            try:
                return gateway.intents.transition(obj["id"], Transition(expected_version=obj["version"], state=state), "external")
            except DomainError as error:
                return error
        finally:
            PRINCIPAL.reset(token)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ["CANCELLED", "CONFIRMED"]))
    errors = [r for r in results if isinstance(r, DomainError)]
    assert len(errors) == 1 and errors[0].code == "INTENT_VERSION_CONFLICT"
    assert problem(errors[0])["next_action"] == "REVIEW_UPDATED_EVIDENCE"
    final = gateway.intents.get(obj["id"], "external")
    assert final["state"] in {"CANCELLED", "CONFIRMED"}
    assert final["order"]["status"] == final["state"]
    assert errors[0].data["winning_state"] == final["state"]


def test_AC_4_handover_preserves_pending_approval_and_rechecks_category(gateway, need, upstream, monkeypatch):
    obj = gateway.intents.create(request(need, quantity=2000), "external")
    q = quote(gateway, need, obj, upstream)
    order = gateway.orders.place_order(q.id, "pending", "external")
    assert order.status == "PENDING_APPROVAL"
    current = gateway.intents.get(obj["id"], "external")
    resumed = gateway.intents.resume(obj["id"], current["version"], "agent-b")
    assert resumed["order"]["id"] == order.id and resumed["next_action"] == "WAIT_FOR_APPROVAL"
    submissions = []

    class Supplier:
        mode = "supplier_backed"
        consistency_window_seconds = 0

        def reconcile(self, order):
            return None

        def submit(self, order):
            submissions.append(order.id)
            return Execution(mode=self.mode, status="accepted", supplier_ref="supplier-" + order.id,
                             acknowledged_at=audit.utcnow())

    monkeypatch.setattr(gateway.suppliers.get("valio"), "execution", Supplier())
    approved = gateway.orders.approve(order.id, "Ada", evidence_revision=order.evidence_revision)
    assert approved.status == "PENDING_EXECUTION"
    gateway.intents.dispatch_pending()
    assert gateway.intents.get(obj["id"], "agent-b")["state"] == "CONFIRMED"
    assert submissions == [order.id]
    assert gateway.intents.create(request(need, quantity=2000), "agent-b")["order"]["id"] == order.id
    key = Ed25519PrivateKey.generate()
    policy = gateway.policy.get().model_dump(mode="json")
    policy["customers"]["1"] = {"mandate_issuers": {"urn:test:buyer": [{"kid": "buyer", "kty": "OKP", "crv": "Ed25519", "x": b64(key.public_key().public_bytes_raw())}]}}
    gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Ada"))
    now = int(audit.utcnow().timestamp())
    mandate = sign_jwt(key, {"iss": "urn:test:buyer", "sub": "other", "jti": "category-excluded", "nbf": now-1, "exp": now+3600,
        "vc": {"@context": ["https://www.w3.org/ns/credentials/v2"], "type": ["VerifiableCredential", "PurchasingMandate"],
               "credentialSubject": {"agent_id": "other", "customer_id": "1", "categories": ["excluded"], "max_auto_total_eur": 50000, "max_auto_daily_total_eur": 50000}}}, kid="buyer")
    with pytest.raises(DomainError) as denied:
        current = gateway.intents.get(obj["id"], "external")
        gateway.intents.resume(obj["id"], current["version"], "other", mandate)
    assert denied.value.code == "MANDATE_INVALID" and denied.value.data["check"] == "category"
    with gateway.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM events WHERE type='INTENT_HANDOVER'").fetchone()[0] == 1


def test_AC_5_partial_policy_commits_only_allowed_lines(gateway, need, upstream, monkeypatch):
    # Five distinct saleable products; hard constraints still go through the core.
    from conftest import raw_product, rebuild_catalog
    rebuild_catalog(gateway, gateway.settings.db_path.parent, [raw_product(str(i), f"Milk {i} lactose free") for i in range(1, 6)] + [raw_product("egg", "Egg vocabulary", egg=True)])
    ids = tuple(str(i) for i in range(1, 6))
    for key, policy in [("partial", {"mode": "allow_partial", "minimum_share": .7}),
                        ("atomic", {"mode": "all_or_nothing"}),
                        ("essential", {"mode": "allow_partial", "minimum_share": .7})]:
        body = request(need, key=key, products=ids, fulfilment_policy=policy, delivery_point=key)
        body.lines[-1].essential = key == "essential"
        obj = gateway.intents.create(body, "external")
        q = quote(gateway, need, obj, upstream)
        monkeypatch.setattr(gateway.settings, "environment", "sandbox")
        gateway.intents.set_faults(obj["id"], Faults(confirmation_delay_seconds=86400))
        order = gateway.orders.place_order(q.id, key, "external")
        result = gateway.intents.event(obj["id"], SupplierEvent(event_id=key+":accepted", sequence=1, kind="accepted", confirmed_lines={str(i): 10 for i in range(1, 5)}))
        if key == "partial":
            assert result["state"] == "PARTIALLY_CONFIRMED" and len(result["committed_lines"]) == 4
            assert result["remainder"][0]["product_id"] == "5" and result["remainder"][0]["state"] == "OPEN"
            assert len(result["order"]["lines"]) == 4
            assert result["next_actions"][0]["action"] == "LOOKUP_REQUIRED"
        else:
            assert result["committed_lines"] == {} and len(result["remainder"]) == 5
            assert result["state"] == "PENDING_APPROVAL" and result["next_action"] == "WAIT_FOR_APPROVAL"
    with gateway.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE type='INTENT_PARTIALLY_CONFIRMED'").fetchone()[0] == 1


def test_AC_6_existing_demo_464306_1010_confirmed_unchanged(gateway, need, upstream):
    demo_catalog(gateway, gateway.settings.db_path.parent, upstream)
    lookup = gateway.find_offers(need, actor="agent:external")
    q = gateway.orders.create_quote(quote_request(need, qty=1000, product_id="464306", need_ref=lookup.lookup_id), "external")
    order = gateway.orders.place_order(q.id, "demo", "external")
    assert (order.lines[0].product_id, order.total, order.status) == ("464306", 1010, "CONFIRMED")
    assert "intent_id" not in order.model_dump(mode="json")


def test_AC_7_unknown_outcome_keeps_stable_reference_through_window(gateway, need, upstream, monkeypatch):
    obj = gateway.intents.create(request(need), "external")
    q = quote(gateway, need, obj, upstream)
    policy = gateway.policy.get().model_dump(mode="json")
    policy["agents"]["external"]["max_auto_daily_total_eur"] = q.total
    gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Ada"))

    class EventuallyConsistent:
        mode = "supplier_backed"
        consistency_window_seconds = 60

        def __init__(self):
            self.orders, self.submits, self.queries = {}, [], []

        def reconcile(self, order):
            self.queries.append(order.id)
            return None

        def submit(self, order):
            self.submits.append(order.id)
            self.orders.setdefault(order.id, object())
            raise TimeoutError("Acknowledgement lost after supplier accepted")

    adapter = gateway.suppliers.get("valio")
    supplier = EventuallyConsistent()
    monkeypatch.setattr(adapter, "execution", supplier)
    order = gateway.orders.place_order(q.id, "supplier-placement", "external")
    assert order.status == "PENDING_EXECUTION" and not supplier.submits
    first = gateway.intents.recover(obj["id"], "external")
    assert first["state"] == "SUBMISSION_UNKNOWN" and len(supplier.submits) == 1
    # A restart must not also enqueue the legacy supplier dispatcher, which
    # would treat an eventually-consistent 404 as permission to submit again.
    from app.service import Gateway
    restarted = Gateway(gateway.settings)
    try:
        monkeypatch.setattr(restarted.suppliers.get("valio"), "execution", supplier)
        with restarted.db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM supplier_execution_outbox").fetchone()[0] == 0
        inside = restarted.intents.recover(obj["id"], "external")
        assert inside["state"] == "SUBMISSION_UNKNOWN" and len(supplier.submits) == 1
        from app.runtime import CLOCK
        later = audit.utcnow() + timedelta(seconds=61)
        clock = CLOCK.set(lambda: later)
        try:
            second = restarted.intents.recover(obj["id"], "external")
        finally:
            CLOCK.reset(clock)
    finally:
        restarted.close()
    assert supplier.submits == [order.id, order.id] and set(supplier.queries) == {order.id}
    assert len(supplier.orders) == 1
    assert second["state"] == "SUBMISSION_UNKNOWN" and second["next_action"] == "WAIT_FOR_APPROVAL"


def test_AC_8_out_of_order_and_duplicate_supplier_events_are_terminal(gateway, need, upstream, monkeypatch):
    obj, order = delayed(gateway, need, upstream, monkeypatch)
    # Supplier-backed cancellation stays a request until its sequence-2 ack.
    with gateway.db.connect(write=True) as conn:
        saved = gateway.orders._order(conn, order.id)
        saved.execution = Execution(mode="supplier_backed", status="unknown", supplier_ref=order.id)
        gateway.orders._save_order(conn, saved)
    current = gateway.intents.get(obj["id"], "external")
    pending = gateway.intents.transition(obj["id"], Transition(expected_version=current["version"], state="CANCELLED"), "external")
    assert pending["state"] == "CANCEL_REQUESTED"
    ack = SupplierEvent(event_id="cancel-ack", sequence=2, kind="cancelled")
    waiting = gateway.intents.event(obj["id"], ack)
    assert waiting["state"] == "CANCEL_REQUESTED" and waiting["supplier_sequence"] == 0
    final = gateway.intents.event(obj["id"], SupplierEvent(event_id="accept", sequence=1, kind="accepted"))
    duplicate = gateway.intents.event(obj["id"], ack)
    assert final["state"] == duplicate["state"] == final["order"]["status"] == "CANCELLED"
    assert final["version"] == duplicate["version"] and final["supplier_sequence"] == 2
    with gateway.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM reservations WHERE order_id=?", (order.id,)).fetchone()[0] == 0
    # A live acceptance after the request exposes the kitchen warning while
    # waiting for the supplier's later cancellation acknowledgement.
    other, _ = delayed(gateway, need, upstream, monkeypatch, key="disputed", delivery_point="second-site")
    with gateway.db.connect(write=True) as conn:
        saved = gateway.orders._order(conn, other["order_id"])
        saved.execution = Execution(mode="supplier_backed", status="unknown", supplier_ref=saved.id)
        gateway.orders._save_order(conn, saved)
    pending = gateway.intents.transition(other["id"], Transition(expected_version=other["version"], state="CANCELLED"), "external")
    disputed = gateway.intents.event(other["id"], SupplierEvent(event_id="second-accept", sequence=1, kind="accepted"))
    assert disputed["state"] == "CANCEL_DISPUTED"
    assert disputed["kitchen_message"] == disputed["order"]["delivery_notice"] == "delivery may still arrive"
    acknowledged = gateway.intents.event(other["id"], SupplierEvent(event_id="second-cancel", sequence=2, kind="cancelled"))
    assert acknowledged["state"] == acknowledged["order"]["status"] == "CANCELLED"


def test_AC_9_new_id_and_second_agent_detect_same_demand(gateway, need, upstream):
    original = gateway.intents.create(request(need), "external")
    for agent, key in [("external", "retry-new-id"), ("agent-b", "another-agent")]:
        with pytest.raises(DomainError) as caught:
            gateway.intents.create(request(need, key=key), agent)
        assert caught.value.code == "POSSIBLE_DUPLICATE_DEMAND"
        assert caught.value.data["overlaps"][0]["intent_id"] == original["id"]
    additional = gateway.intents.create(request(need, key="extra", additional=True), "external")
    q = quote(gateway, need, additional, upstream)
    order = gateway.orders.place_order(q.id, "extra-demand", "external")
    assert order.status == "PENDING_APPROVAL"
    # The same approval requirement survives an amendment from unrelated demand.
    separate = gateway.intents.create(request(need, key="amended-extra", products=("3",)), "external")
    extra = request(need, key="amended-extra", additional=True)
    amended = gateway.intents.amend(separate["id"], Amendment(expected_version=separate["version"], request=extra), "external")
    amended_quote = quote(gateway, need, amended, upstream)
    assert gateway.orders.place_order(amended_quote.id, "amended-extra-demand", "external").status == "PENDING_APPROVAL"
    # Tenant-scoped client ids and fingerprints cannot alias another buyer's
    # intent, even when the product, delivery date and client id are identical.
    hospital_agent = "hospital-kitchen-agent"
    token = PRINCIPAL.set(gateway.accounts.bind(Principal(hospital_agent, tenant_id="kaari-hospital", customer_ids=("2",))))
    try:
        hospital = gateway.intents.create(request(need.model_copy(update={"customer_id": "2"})), hospital_agent)
        assert hospital["id"] != original["id"] and hospital["tenant_id"] == "kaari-hospital"
        with pytest.raises(DomainError) as hidden:
            gateway.intents.get(original["id"], hospital_agent)
        assert hidden.value.code == "INTENT_NOT_FOUND"
    finally:
        PRINCIPAL.reset(token)
