"""Spec 22 acceptance coverage: shared state, durable events and identity."""
import asyncio
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import audit
from app.api import create_app
from app.auth import Credentials
from app.mcp_server import create_server
from app.models import PolicyUpdateRequest
from conftest import agent_headers, quote_request


KEYED_WRITES = ("create_quote", "place_order", "cancel", "approve", "reject", "update", "reset", "revoke", "change", "find_offers")


@pytest.mark.parametrize("command", KEYED_WRITES)
def test_review_all_keyed_commands_reauthorize(gateway, need, upstream, command):
    """Real commands, current scope loss, and same refusal as a newly keyed call."""
    from app.db import DomainError
    from app.foundation import PRINCIPAL
    from conftest import OPERATOR_HEADERS
    from test_graduated_autonomy import organisations, update
    from test_verifiable_mandate import ISSUER
    from app.mandates import sign_jwt
    from app.signing import b64
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    organisations(gateway)
    # Keep the matrix exhaustive when another keyed command is installed.
    assert gateway.keyed_routes == {"orders.create_quote", "orders.place_order", "orders.cancel", "orders.approve",
                                   "orders.reject", "gateway.find_offers", "policy.update", "mandates.revoke", "autonomy.change"}
    actor = "external"
    headers = agent_headers(actor)
    method, url, body = "POST", "", {}
    if command in {"create_quote", "place_order", "cancel", "approve", "reject"}:
        lookup = gateway.find_offers(need)
        request = quote_request(need, need_ref=lookup.lookup_id, qty=200 if command in {"approve", "reject"} else 1)
        if command == "create_quote":
            url, body = "/v1/quotes", request.model_dump(mode="json")
        else:
            quote = gateway.orders.create_quote(request)
            if command == "place_order":
                url, body = "/v1/orders", {"quote_id": quote.id}
            else:
                order = gateway.orders.place_order(quote.id, "setup")
                if command == "cancel":
                    url, body = f"/v1/orders/{order.id}/cancel", {"cancelled_by": "Agent", "reason": "Duplicate"}
                else:
                    actor, headers = "buyer", agent_headers("buyer")
                    url, body = f"/v1/approvals/{order.id}/{command}", {"approver": "Buyer"}
    elif command in {"update", "reset"}:
        actor, headers = "operator", OPERATOR_HEADERS
        if command == "update":
            method, url = "PUT", "/v1/policy"
            body = {"policy": gateway.policy._editable(gateway.policy.get()), "updated_by": "Operator"}
        else:
            url, body = "/v1/policy/reset", {"updated_by": "Operator"}
    elif command == "change":
        actor, headers = "hotel-agent", agent_headers("hotel-agent")
        url, body = "/v1/agents/external/autonomy", {"level": "approval_required", "reason": "Pilot"}
    elif command == "revoke":
        key = Ed25519PrivateKey.generate()
        update(gateway, customers={"1": {"mandate_issuers": {ISSUER: [{"kid": "buyer-1", "kty": "OKP", "crv": "Ed25519",
            "alg": "EdDSA", "x": b64(key.public_key().public_bytes_raw())}]}}})
        token = sign_jwt(key, {"iss": ISSUER, "jti_revoked": ["mandate"], "iat": int(audit.utcnow().timestamp())}, kid="buyer-1")
        url, body = "/v1/mandates/revocations", {"jwt": token}

    with TestClient(create_app(gateway)) as client:
        def call(key):
            if command != "find_offers":
                response = client.request(method, url, json=body, headers={**headers, "Idempotency-Key": key})
                return response.status_code, response.json()
            principal = gateway.settings.keys.resolve(headers["Authorization"].split()[1], gateway.policy.get())
            context = PRINCIPAL.set(principal)
            try:
                return 200, gateway.find_offers(need, command_key=key).model_dump(mode="json")
            except DomainError as error:
                return error.status, {"code": error.code}
            finally:
                PRINCIPAL.reset(context)

        status, first = call("review-key")
        assert status == 200, first
        calls = len(upstream.price_calls), len(upstream.availability_calls), len(upstream.decision_calls)
        assert call("review-key") == (status, first)
        assert calls == (len(upstream.price_calls), len(upstream.availability_calls), len(upstream.decision_calls))
        if command in {"update", "reset"}:
            # Actual credential validity is established again by both transport calls.
            headers = {"Authorization": "Bearer withdrawn-operator-key"}
        elif command == "revoke":
            update(gateway, customers={})
        elif command == "cancel":
            with gateway.db.connect(write=True) as conn:
                conn.execute("UPDATE orders SET data=json_set(data,'$.agent_id','other') WHERE id=?", (order.id,))
        elif command in {"approve", "reject", "change"}:
            policy = gateway.policy._editable(gateway.policy.get())
            policy["identities"]["external"]["organisation_id"] = "other"
            update(gateway, **policy)
        else:
            policy = gateway.policy._editable(gateway.policy.get())
            policy["agents"]["external"]["customer_ids"] = ["2"]
            update(gateway, **policy)
        fresh_status, fresh = call("fresh-key")
        replay_status, replay = call("review-key")
        assert fresh_status in {401, 403, 404, 422}
        assert (replay_status, replay.get("code")) == (fresh_status, fresh["code"])
        assert not {"track_record", "lines", "policy", "offers"} & replay.keys()


@pytest.mark.parametrize("section", ["policy", "reset", "customers", "agents", "issuers", "autonomy", "rate_limits"])
@pytest.mark.parametrize("failure", ["command", "audit", "commit"])
def test_review_policy_file_waits_for_outer_commit(gateway, monkeypatch, section, failure):
    """Every policy writer shares staging; neither audit nor enclosing failures publish it."""
    from app import db as db_module
    from app.models import AutonomyChange, PolicyResetRequest
    from app.auth import Principal
    from app.foundation import PRINCIPAL
    from test_graduated_autonomy import approval_first

    approval_first(gateway)
    before = gateway.policy.runtime_path.read_bytes()
    before_audit = len(gateway.audit())
    body = gateway.policy._editable(gateway.policy.get())
    if section == "customers":
        body["customers"] = {"1": {"note": "New customer policy"}}
    elif section == "agents":
        body["agents"]["external"]["max_auto_daily_total_eur"] = 1
    elif section == "issuers":
        body["customers"] = {"1": {"mandate_issuers": {"urn:buyer": [{"kid": "test", "kty": "OKP", "crv": "Ed25519", "x": "A" * 43}]}}}
    elif section == "rate_limits":
        body["rate_limits"]["mutation"]["capacity"] = 5
    else:
        body["approval"]["max_auto_total_eur"] = 1

    staged = []
    original_replace = gateway.policy._replace
    def stage(content):
        original_replace(content)
        staged.append(content)
        # A separate reader must still see precisely the old file and old audit.
        assert gateway.policy.runtime_path.read_bytes() == before
        with sqlite3.connect(gateway.db.path) as observer:
            assert observer.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == before_audit
    monkeypatch.setattr(gateway.policy, "_replace", stage)
    if failure != "commit":
        table = "commands" if failure == "command" else "audit"
        with gateway.db.connect(write=True) as conn:
            conn.execute(f"CREATE TRIGGER fail_review BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT,'review failure'); END")
    else:
        original_connect = sqlite3.connect
        class FailingCommit(sqlite3.Connection):
            def commit(self):
                if self.execute("SELECT COUNT(*) FROM commands WHERE key='file-review'").fetchone()[0]:
                    raise sqlite3.OperationalError("review failure")
                return super().commit()
        monkeypatch.setattr(db_module.sqlite3, "connect", lambda *a, **kw: original_connect(*a, **kw, factory=FailingCommit))
    token = PRINCIPAL.set(Principal("web-console", operator=True, kind="operator", role="valio_operator"))
    try:
        with pytest.raises(sqlite3.Error, match="review failure"):
            if section == "autonomy":
                gateway.autonomy.change("external", AutonomyChange(level="autonomous_within_limits", reason="Pilot"),
                                        PRINCIPAL.get(), command_key="file-review")
            else:
                request = PolicyResetRequest(updated_by="Operator") if section == "reset" else PolicyUpdateRequest(policy=body, updated_by="Operator")
                gateway.policy.update(request, reset=section == "reset", command_key="file-review")
    finally:
        PRINCIPAL.reset(token)
    assert gateway.policy.runtime_path.read_bytes() == before
    assert len(gateway.audit()) == before_audit
    with gateway.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM commands WHERE key='file-review'").fetchone()[0] == 0
    assert staged or failure == "audit"


def test_review_policy_projection_recovers_after_commit(gateway, monkeypatch, tmp_path):
    from app import db as db_module
    from app.db import Database
    from app.policy import Policy
    from app.foundation import PRINCIPAL
    from app.auth import Principal

    before = gateway.policy.get()
    body = gateway.policy._editable(before)
    body["approval"]["max_auto_total_eur"] = 17
    replace = db_module.os.replace
    published = []
    def disk_unavailable(source, destination):
        # Publication must follow the enclosing command/audit commit.
        with sqlite3.connect(gateway.db.path) as observer:
            assert observer.execute("SELECT COUNT(*) FROM commands WHERE key='committed-file'").fetchone()[0] == 1
            assert observer.execute("SELECT COUNT(*) FROM audit WHERE action='POLICY_UPDATED'").fetchone()[0] == 1
        published.append(destination)
        raise OSError("Temporarily unavailable projection")
    monkeypatch.setattr(db_module.os, "replace", disk_unavailable)
    token = PRINCIPAL.set(Principal("web-console", operator=True, kind="operator", role="valio_operator"))
    try:
        result = gateway.policy.update(PolicyUpdateRequest(policy=body, updated_by="Operator"), command_key="committed-file")
    finally:
        PRINCIPAL.reset(token)
    assert published and result.approval.max_auto_total_eur == 17
    assert not gateway.policy.runtime_path.exists()
    assert gateway.policy.get().approval.max_auto_total_eur == 17
    monkeypatch.setattr(db_module.os, "replace", replace)
    # Copying a committed DB must recover beside the copy, never the original path.
    restored = tmp_path / "restored" / "app.db"
    restored.parent.mkdir()
    with sqlite3.connect(gateway.db.path) as source, sqlite3.connect(restored) as destination:
        source.backup(destination)
    db = Database(restored)
    recovered = Policy(gateway.policy.path, restored.parent / "policy.yaml", db)
    assert recovered.get().approval.max_auto_total_eur == 17
    assert (restored.parent / "policy.yaml").exists()
    assert not gateway.policy.runtime_path.exists()
    assert not list(gateway.db.path.parent.glob(".policy-*"))


def test_review_policy_nested_rollback_discards_only_its_file(gateway):
    from app.models import PolicyUpdateRequest
    body = gateway.policy._editable(gateway.policy.get())
    assert not gateway.policy.runtime_path.exists()
    with gateway.db.connect(write=True):
        body["approval"]["max_auto_total_eur"] = 17
        gateway.policy.update(PolicyUpdateRequest(policy=body, updated_by="Outer"))
        with pytest.raises(RuntimeError, match="nested failure"):
            with gateway.db.connect(write=True):
                body["approval"]["max_auto_total_eur"] = 99
                gateway.policy.update(PolicyUpdateRequest(policy=body, updated_by="Inner"))
                raise RuntimeError("nested failure")
        assert gateway.policy.get().approval.max_auto_total_eur == 17
        assert not gateway.policy.runtime_path.exists()
    assert gateway.policy._load(gateway.policy.runtime_path).approval.max_auto_total_eur == 17
    assert len(gateway.audit()) == 1
    assert not list(gateway.db.path.parent.glob(".policy-*"))


def test_review_policy_writers_keep_both_autonomy_changes(gateway, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from app.auth import Principal
    from app.models import AutonomyChange
    from app.service import Gateway

    peer = Gateway(gateway.settings)
    entered, release = threading.Event(), threading.Event()
    stage = gateway.policy._replace
    def held(content):
        stage(content)
        entered.set()
        assert release.wait(5)
    monkeypatch.setattr(gateway.policy, "_replace", held)
    operator = Principal("web-console", operator=True, kind="operator", role="valio_operator")
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(gateway.autonomy.change, "external",
                AutonomyChange(level="approval_required", reason="Review external"), operator, command_key="first-writer")
            assert entered.wait(5)
            second = pool.submit(peer.autonomy.change, "hotel-chain-agent",
                AutonomyChange(level="approval_required", reason="Review hotel"), operator, command_key="second-writer")
            release.set()
            assert first.result().agent_id == "external"
            assert second.result().agent_id == "hotel-chain-agent"
        assert gateway.policy.get().agents["external"].autonomy == "approval_required"
        assert gateway.policy.get().agents["hotel-chain-agent"].autonomy == "approval_required"
        assert len([e for e in gateway.audit() if e.action == "AUTONOMY_CHANGED"]) == 2
        assert gateway.policy._load(gateway.policy.runtime_path).agents == peer.policy.get().agents
    finally:
        release.set()
        peer.close()


def test_review_first_policy_write_failure_keeps_file_absent(gateway):
    body = gateway.policy._editable(gateway.policy.get())
    body["approval"]["max_auto_total_eur"] = 1
    with gateway.db.connect(write=True) as conn:
        conn.execute("CREATE TRIGGER fail_first BEFORE INSERT ON commands BEGIN SELECT RAISE(ABORT,'first failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="first failure"):
        gateway.policy.update(PolicyUpdateRequest(policy=body, updated_by="Operator"), command_key="first-file")
    assert not gateway.policy.runtime_path.exists()
    assert not gateway.audit()
    assert not list(gateway.db.path.parent.glob(".policy-*"))
    with gateway.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM runtime_files").fetchone()[0] == 0


def test_review_legacy_order_replay_rechecks_customer_scope(gateway, need, upstream):
    from app.db import DomainError
    quote = make_quote(gateway, need)
    gateway.orders.place_order(quote.id, "legacy-order")
    with gateway.db.connect(write=True) as conn:
        conn.execute("DELETE FROM commands WHERE key='legacy-order'")
    body = gateway.policy._editable(gateway.policy.get())
    body["agents"]["external"]["customer_ids"] = ["2"]
    gateway.policy.update(PolicyUpdateRequest(policy=body, updated_by="Operator"))
    with pytest.raises(DomainError, match="CUSTOMER_NOT_IN_MANDATE"):
        gateway.orders.place_order(quote.id, "legacy-order")


@pytest.mark.parametrize("command", ["create_quote", "change"])
def test_review_legacy_command_envelopes_keep_loading(gateway, need, upstream, command):
    from dataclasses import asdict
    from hashlib import sha256
    from app.canonical import canonical_json
    from app.foundation import PRINCIPAL
    from app.models import AutonomyChange
    from conftest import OPERATOR_HEADERS

    principal = gateway.settings.keys.resolve(OPERATOR_HEADERS["Authorization"].split()[1], gateway.policy.get())
    token = PRINCIPAL.set(principal)
    try:
        if command == "change":
            request = AutonomyChange(level="approval_required", reason="Pilot review")
            arguments = {"request": request.model_dump(mode="json"), "principal": json.loads(json.dumps(asdict(principal)))}
            def call(target="external"):
                return gateway.autonomy.change(target, request, principal, command_key="legacy")
        else:
            lookup = gateway.find_offers(need, actor="agent:web-console")
            request = quote_request(need, qty=1, need_ref=lookup.lookup_id)
            arguments = {"request": request.model_dump(mode="json")}
            def call():
                return gateway.orders.create_quote(request, "web-console", operator=True, command_key="legacy")
        first = call()
        legacy_digest = sha256(canonical_json({"command": command, "arguments": arguments})).hexdigest()
        with gateway.db.connect(write=True) as conn:
            conn.execute("UPDATE commands SET digest=?,result=json_remove(result,'$.fingerprint_version') WHERE key='legacy'", (legacy_digest,))
        assert call().model_dump(mode="json") == first.model_dump(mode="json")
        if command == "change":
            from app.db import DomainError
            with pytest.raises(DomainError, match="IDEMPOTENCY_CONFLICT"):
                call("hotel-chain-agent")
            assert gateway.policy.get().agents["hotel-chain-agent"].autonomy == "autonomous_within_limits"
    finally:
        PRINCIPAL.reset(token)


def make_quote(gateway, need, agent="external", qty=1):
    lookup = gateway.find_offers(need, actor=f"agent:{agent}")
    return gateway.orders.create_quote(quote_request(need, need_ref=lookup.lookup_id, qty=qty), agent)


def event_rows(gateway, aggregate_id):
    with gateway.db.connect() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM events WHERE aggregate_id=? ORDER BY aggregate_revision", (aggregate_id,))]


def test_ac_1_rest_mcp_same_policy_and_single_events(gateway, need, upstream):
    rest_quote = make_quote(gateway, need)
    mcp_quote = make_quote(gateway, need)
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        response = client.post("/v1/orders", json={"quote_id": rest_quote.id}, headers={"Idempotency-Key": "rest"})
        assert response.status_code == 200, response.text
    remote = asyncio.run(create_server(gateway, "external").call_tool("place_order", {"quote_id": mcp_quote.id, "idempotency_key": "mcp"}))
    assert not remote.is_error
    first, second = response.json(), remote.structured_content
    for field in ("status", "policy", "total", "currency", "customer_id", "agent_id", "commitment", "next_action"):
        assert first[field] == second[field]
    assert {k: v for k, v in first["context"].items() if k != "request_ref"} == {k: v for k, v in second["context"].items() if k != "request_ref"}
    for order in (first, second):
        entries = [e for e in gateway.audit(order_id=order["id"]) if e.action == "PLACE_ORDER"]
        assert len(entries) == 1
        assert len(event_rows(gateway, order["id"])) == 1
        assert event_rows(gateway, order["id"])[0]["type"] == "order.placed"


@pytest.mark.parametrize("registered_agents", [("external", "other")], indirect=True)
def test_ac_2_cross_transport_principal_scoped_payload_binding(gateway, need, upstream):
    quote = make_quote(gateway, need)
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        first = client.post("/v1/orders", json={"quote_id": quote.id}, headers={"Idempotency-Key": "shared"}).json()
        server = create_server(gateway, "external")
        replay = asyncio.run(server.call_tool("place_order", {"quote_id": quote.id, "idempotency_key": "shared"}))
        assert replay.structured_content["id"] == first["id"]
        changed = make_quote(gateway, need)
        conflict = client.post("/v1/orders", json={"quote_id": changed.id}, headers={"Idempotency-Key": "shared"})
        assert conflict.status_code == 409 and conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
    other = make_quote(gateway, need, "other")
    assert gateway.orders.place_order(other.id, "shared", "other").id != first["id"]
    with gateway.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 2


def test_ac_3_audit_failure_rolls_back_state_events_and_command(gateway, need, upstream):
    quote = make_quote(gateway, need)
    with gateway.db.connect(write=True) as conn:
        conn.execute("CREATE TRIGGER fail_order_audit BEFORE INSERT ON audit WHEN NEW.action='PLACE_ORDER' BEGIN SELECT RAISE(ABORT,'audit failed'); END")
    with pytest.raises(sqlite3.IntegrityError, match="audit failed"):
        gateway.orders.place_order(quote.id, "rollback")
    with gateway.db.connect() as conn:
        for table in ("orders", "reservations", "commands"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert conn.execute("SELECT status FROM quotes WHERE id=?", (quote.id,)).fetchone()[0] == "OPEN"
        assert conn.execute("SELECT COUNT(*) FROM events WHERE aggregate_type='order'").fetchone()[0] == 0
    assert audit.verify(gateway.db)["valid"]


def test_ac_4_revisions_outbox_delivery_and_retry(gateway, need, upstream):
    received = []
    def unavailable(event):
        raise RuntimeError("offline")
    gateway.outbox.register("webhooks", unavailable)
    gateway.outbox.register("activity", unavailable)
    quote = make_quote(gateway, need)
    order = gateway.orders.place_order(quote.id, "revision")
    gateway.orders.cancel(order.id, actor="agent:external")
    events = event_rows(gateway, order.id)
    assert [e["aggregate_revision"] for e in events] == [1, 2]
    with gateway.db.connect() as conn:
        for event in events:
            rows = conn.execute("SELECT consumer,status FROM outbox WHERE event_id=?", (event["id"],)).fetchall()
            assert {(r[0], r[1]) for r in rows} == {("webhooks", "pending"), ("activity", "pending")}
    gateway.outbox.register("webhooks", lambda event: received.append(event))
    with gateway.db.connect(write=True) as conn:
        conn.execute("UPDATE outbox SET next_attempt_at='2000-01-01T00:00:00+00:00'")
    gateway.outbox.dispatch()
    assert {e["id"] for e in events} <= {e["id"] for e in received}
    assert all(e["payload"]["links"]["event_id"] == e["id"] for e in received)


def test_ac_5_correlation_and_linked_identifiers(gateway, need, upstream):
    quote = make_quote(gateway, need)
    with TestClient(create_app(gateway), headers={**agent_headers("external"), "X-Correlation-Id": "procurement-123"}) as client:
        response = client.post("/v1/orders", json={"quote_id": quote.id}, headers={"Idempotency-Key": "correlated"})
        assert response.headers["X-Correlation-Id"] == "procurement-123"
        order = response.json()
        assert order["correlation_id"] == "procurement-123"
        event = event_rows(gateway, order["id"])[0]
        assert event["correlation_id"] == "procurement-123"
        assert event["causation_id"] == "correlated"
        assert gateway.audit(order_id=order["id"])[0].data["correlation_id"] == "procurement-123"
        detail = client.get("/v1/orders/" + order["id"]).json()
        assert detail["links"]["request_id"] == quote.need_ref
        assert detail["links"]["event_ids"] == [event["id"]]
        assert detail["links"]["receipt_id"] == order["id"]
        assert client.get("/v1/orders/missing").headers["X-Correlation-Id"] == "procurement-123"


def test_ac_6_public_pinned_compatibility_manifest(gateway):
    with TestClient(create_app(gateway)) as client:
        response = client.get("/v1/meta")
    assert response.status_code == 200
    manifest = response.json()
    assert set(manifest["specifications"]) == {"openapi", "mcp_protocol", "mcp_server_card", "a2a", "ubl", "vc_data_model", "vc_securing_profile", "problem_details", "standard_webhooks"}
    assert all(value for value in manifest["specifications"].values())
    for value in manifest["specifications"].values():
        if isinstance(value, dict):
            assert value.get("version") or value.get("draft_commit") or value.get("commit")
    assert manifest["specifications"]["mcp_server_card"]["draft_commit"] == "526201bbc80231daa40ffcdecfc9da4e54e5dc93"
    assert manifest["specifications"]["standard_webhooks"]["commit"] == "36afae792227eae8a182a7a1a5711dbdc08a4334"
    assert "fastapi" in manifest["packages"] and "fastapi" not in manifest["specifications"]
    assert manifest["limiter_scope"] == "in-process, single instance"


def test_ac_7_buyer_and_supplier_principals_and_key_provisioning(gateway, tmp_path):
    from scripts.gen_keys import provision
    policy = gateway.policy.get().model_dump(mode="json")
    policy["organisations"] = {"hotel": {"customer_ids": ["1", "42"]}}
    policy["identities"] = {"viewer": {"kind": "buyer_user", "role": "buyer_viewer", "organisation_id": "hotel"},
                            "supplier": {"kind": "supplier_agent", "role": "supplier_agent"}}
    gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Ada"))
    keys = Credentials("operator-secret", "viewer:viewer-secret,supplier:supplier-secret")
    viewer = keys.resolve("viewer-secret", gateway.policy.get())
    supplier = keys.resolve("supplier-secret", gateway.policy.get())
    assert (viewer.kind, viewer.role, viewer.organisation_id, viewer.customer_ids) == ("buyer_user", "buyer_viewer", "hotel", ("1", "42"))
    assert supplier.kind == supplier.role == "supplier_agent"
    assert viewer.credential_ref == "agent:viewer:key:1"
    assert supplier.credential_ref == "agent:supplier:key:1"
    assert keys.resolve("viewer-secret").credential_ref == viewer.credential_ref
    env = tmp_path / ".env.local"
    provision(env, set(gateway.policy.get().identities))
    assert "viewer:" in env.read_text() and "supplier:" in env.read_text()


def test_quote_and_cancel_idempotency_across_transports(gateway, need, upstream):
    lookup = gateway.find_offers(need)
    body = quote_request(need, need_ref=lookup.lookup_id, qty=1).model_dump(mode="json")
    server = create_server(gateway, "external")
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        first = client.post("/v1/quotes", json=body, headers={"Idempotency-Key": "quote-key"})
        assert first.status_code == 200
        before = len(upstream.price_calls), len(upstream.availability_calls)
        replay = asyncio.run(server.call_tool("create_quote", {**body, "idempotency_key": "quote-key"}))
        assert replay.structured_content == first.json()
        assert before == (len(upstream.price_calls), len(upstream.availability_calls))
        changed = {**body, "lines": [{"product_id": "1", "quantity_sales_units": 2}]}
        assert client.post("/v1/quotes", json=changed, headers={"Idempotency-Key": "quote-key"}).status_code == 409
        order = client.post("/v1/orders", json={"quote_id": first.json()["id"]}, headers={"Idempotency-Key": "order-key"}).json()
        cancelled = client.post(f"/v1/orders/{order['id']}/cancel", json={"cancelled_by": "ignored for agents", "reason": "correction"}, headers={"Idempotency-Key": "cancel-key"})
        assert cancelled.status_code == 200
        again = asyncio.run(server.call_tool("cancel_order", {"order_id": order["id"], "reason": "correction", "idempotency_key": "cancel-key"}))
        assert not again.is_error and again.structured_content == cancelled.json()
    assert len([e for e in gateway.audit(order_id=order["id"]) if e.action == "CANCEL"]) == 1


def test_approval_replay_and_conflict(gateway, need, upstream):
    from conftest import OPERATOR_HEADERS
    quote = make_quote(gateway, need, qty=200)
    order = gateway.orders.place_order(quote.id, "needs-human")
    with TestClient(create_app(gateway), headers=OPERATOR_HEADERS) as client:
        url = f"/v1/approvals/{order.id}/approve"
        body = {"approver": "Ada", "comment": "reviewed", "evidence_revision": 1}
        first = client.post(url, json=body, headers={"Idempotency-Key": "review-key"})
        assert first.status_code == 200
        before = len(upstream.availability_calls)
        assert client.post(url, json=body, headers={"Idempotency-Key": "review-key"}).json() == first.json()
        assert len(upstream.availability_calls) == before
        assert client.post(url, json={**body, "comment": "changed"}, headers={"Idempotency-Key": "review-key"}).status_code == 409


@pytest.mark.parametrize("scenario", ["replay", "conflict", "stale_promotion"])
def test_review_autonomy_idempotency(gateway, scenario):
    from conftest import OPERATOR_HEADERS
    url = "/v1/agents/external/autonomy"
    promotion = {"level": "autonomous_within_limits", "reason": "Pilot reviewed"}
    demotion = {"level": "approval_required", "reason": "Human review required"}
    with TestClient(create_app(gateway), headers=OPERATOR_HEADERS) as client:
        first = client.post(url, json=promotion, headers={"Idempotency-Key": "promote"})
        assert first.status_code == 200, first.text
        if scenario == "conflict":
            response = client.post(url, json=demotion, headers={"Idempotency-Key": "promote"})
            assert response.status_code == 409
            assert response.json()["code"] == "IDEMPOTENCY_CONFLICT"
        else:
            if scenario == "stale_promotion":
                assert client.post(url, json=demotion, headers={"Idempotency-Key": "demote"}).status_code == 200
            response = client.post(url, json=promotion, headers={"Idempotency-Key": "promote"})
            assert response.status_code == 200 and response.json() == first.json()
            expected = "approval_required" if scenario == "stale_promotion" else "autonomous_within_limits"
            assert gateway.policy.get().agents["external"].autonomy == expected
    entries = [e for e in gateway.audit() if e.action == "AUTONOMY_CHANGED"]
    assert len(entries) == (2 if scenario == "stale_promotion" else 1)


def test_mcp_wire_correlation_metadata(gateway, need, upstream):
    server = create_server(gateway, "external")
    app = server.streamable_http_app(stateless_http=True, json_response=True)
    with TestClient(app, base_url="http://localhost:8001", headers={**agent_headers("external"), "Accept": "application/json, text/event-stream"}) as client:
        response = client.post("/mcp", headers={"X-Correlation-Id": "http-correlation"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "find_offers", "arguments": {"need": need.model_dump(mode="json")}, "_meta": {"correlation_id": "metadata-correlation"}}})
        assert response.status_code == 200, response.text
        assert response.headers["X-Correlation-Id"] == "http-correlation"
        payload = response.json()["result"]["structuredContent"]
        assert payload["correlation_id"] == "metadata-correlation"
        event = event_rows(gateway, payload["lookup_id"])[0]
        assert event["correlation_id"] == "metadata-correlation"
