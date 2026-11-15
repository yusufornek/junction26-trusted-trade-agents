"""Exactly one focused offline test for each spec 64 acceptance criterion."""
import asyncio
import json
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from google.protobuf.json_format import MessageToDict

from app import audit
from app.a2a import t, data_part, metadata
from app.api import create_app
from app.auth import Principal, resolve_credential
from app.db import DomainError
from app.foundation import PRINCIPAL
from app.handoff_delegated_authority import Handoff, Delegation
from app.mcp_server import create_server
from app.models import Quantity, PolicyUpdateRequest
from app.purchase_intent_integrity import IntentRequest
from conftest import TEST_KEYS, OPERATOR_HEADERS, agent_headers, quote_request, demo_catalog
from test_webhooks import receiver, subscribe

pytestmark = pytest.mark.parametrize("registered_agents", [("external", "agent-b", "other")], indirect=True)


@contextmanager
def actor(gateway, name="external", secret=None):
    p = resolve_credential(TEST_KEYS, secret or "test-secret-" + name, gateway)
    token = PRINCIPAL.set(p)
    try:
        yield p
    finally:
        PRINCIPAL.reset(token)


def intent_request(need, key="demand", **kwargs):
    return IntentRequest(client_intent_id=key, customer_id=need.customer_id, deliver_by=need.deliver_by,
                         lines=[{"product_id": "1", "quantity_sales_units": 10}], **kwargs)


def expiry(seconds=600):
    return (audit.utcnow() + timedelta(seconds=seconds)).isoformat()


def mcp(server, name, args, context=None):
    result = asyncio.run(server.call_tool(name, args, context))
    return result.structured_content or json.loads(result.content[0].text)


def a2a(gateway, p, command, key, task=None, context_id=None, **fields):
    return gateway.a2a.send(t.SendMessageRequest(message=t.Message(message_id=key, role=t.Role.ROLE_USER,
        task_id=task.id if task else "", context_id=task.context_id if task else context_id or "",
        parts=[data_part({"version": 1, "command": command, **fields})])), p)


def approved(client, url, body):
    preview = client.post(url, json=body, headers=OPERATOR_HEADERS)
    assert preview.status_code == 200, preview.text
    assert preview.json()["confirmation_required"], preview.text
    assert preview.json()["summary"]["changes"]["evidence"]["intent_id"], preview.text
    result = client.post(url, json={**body, "confirmation_token": preview.json()["confirmation_token"]}, headers=OPERATOR_HEADERS)
    assert result.status_code == 200, result.text
    return result.json()


def own_quote(gateway, need, obj, agent="external", secret=None):
    with actor(gateway, agent, secret):
        lookup = gateway.find_offers(need, actor="agent:" + agent)
        body = quote_request(need, qty=10, need_ref=lookup.lookup_id)
        body.intent_id, body.intent_version = obj["id"], obj["version"]
        return gateway.orders.create_quote(body, agent)


def test_AC_1_mcp_to_a2a_exclusive_handoff_one_order(gateway, need, upstream, receiver):
    small = need.model_copy(update={"quantity": Quantity(value=50, unit="L")})
    server = create_server(gateway, agent_id="external")
    obj = mcp(server, "create_intent", {"request": intent_request(small).model_dump(mode="json")})
    q = own_quote(gateway, small, obj)
    with actor(gateway) as p:
        subscribe(gateway, event_types=["INTENT_HANDOVER"], principal=p)
        before = a2a(gateway, p, "get_intent", "before-handoff", context_id="stable-buying-context", arguments={"intent_id": obj["id"]})
    grant = mcp(server, "handoff_intent", {"intent_id": obj["id"], "request": {
        "to_agent_id": "agent-b", "mode": "may_commit", "expires_at": expiry()}})
    assert grant["owner_approval_required"]
    with TestClient(create_app(gateway, serve_mcp=False)) as client:
        refused = client.post(f"/v1/handoffs/{grant['code']}/accept", headers=agent_headers("agent-b"))
        assert refused.status_code == 403 and refused.json()["code"] == "OWNER_REQUIRED"
        approved(client, grant["approval_url"], {})
        with actor(gateway, "agent-b") as p:
            task = a2a(gateway, p, "accept_handoff", "accept", arguments={"code": grant["code"]})
            context_id, task_id = task.context_id, task.id
            task = a2a(gateway, p, "lookup", "b-lookup", task, need=small.model_dump(mode="json"))
            lookup = gateway.lookup.get(metadata(task)["request_id"], p.id)
            index = next(i for i, plan in enumerate(lookup.plans) if len(plan.lines) == 1 and plan.lines[0].product_id == "1")
            task = a2a(gateway, p, "select_plan", "b-quote", task, plan_index=index)
            task = a2a(gateway, p, "place_order", "b-order", task)
            assert task.context_id == context_id == before.context_id == "stable-buying-context"
            assert task.id == task_id == before.id
            result = gateway.intents.get(obj["id"], p.id)
            assert result["state"] == "CONFIRMED" and result["order"]["agent_id"] == "agent-b"
        denied = client.post("/v1/orders", json={"quote_id": q.id}, headers={**agent_headers("external"), "Idempotency-Key": "old-holder"})
        assert denied.status_code == 409 and denied.json()["code"] == "INTENT_HANDED_OVER"
        assert denied.json()["data"]["new_holder"] == "agent-b"
        checkpoint = client.get(f"/v1/intents/{obj['id']}/checkpoint", headers=agent_headers("external"))
        assert checkpoint.status_code == 200 and checkpoint.json()["holder"] == "agent-b"
        evidence = client.get(f"/v1/orders/{result['order']['id']}/evidence?format=json", headers=agent_headers("agent-b"))
        assert evidence.status_code == 200, evidence.text
    with gateway.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
        event = conn.execute("SELECT payload FROM events WHERE type='INTENT_HANDOVER'").fetchone()
        assert json.loads(event[0])["grant"]["id"] == grant["handoff_id"]
    from app.webhooks import verify_webhook
    handovers = [r for r in receiver["calls"] if json.loads(r.content)["type"] == "INTENT_HANDOVER"]
    assert len(handovers) == 1 and verify_webhook(handovers[0].content, handovers[0].headers, gateway.signing.jwks())
    from app.evidence import build_bundle
    with actor(gateway, "agent-b") as p:
        _, files = build_bundle(gateway, result["order"]["id"], p)
        entries = [x["entry"] for x in json.loads(files["audit.json"]) if "entry" in x]
        assert any(e["action"] == "INTENT_HANDOVER" and e["data"]["actor_chain"] == ["external", "agent-b"] for e in entries)
        assert grant["code"].encode() not in files["audit.json"]
    with gateway.db.connect() as conn:
        assert grant["code"] not in "".join(r[0] for r in conn.execute("SELECT data FROM a2a_tasks"))


def test_AC_2_prepare_only_and_recipient_category_intersection(gateway, need, upstream):
    with TestClient(create_app(gateway, serve_mcp=False)) as client:
        obj = client.post("/v1/intents", json=intent_request(need).model_dump(mode="json"), headers=agent_headers("external")).json()
        grant = client.post(f"/v1/intents/{obj['id']}/handoff", json={"to_agent_id": "agent-b", "expires_at": expiry()}, headers=agent_headers("external")).json()
        accepted = client.post(f"/v1/handoffs/{grant['code']}/accept", headers=agent_headers("agent-b"))
        assert accepted.status_code == 200, accepted.text
        current = gateway.intents.get(obj["id"], "agent-b")
        q = own_quote(gateway, need, current, "agent-b")
        denied = client.post("/v1/orders", json={"quote_id": q.id}, headers={**agent_headers("agent-b"), "Idempotency-Key": "prepare-only"})
        assert denied.status_code == 403 and denied.json()["code"] == "HANDOFF_MODE"
        with actor(gateway) as p:
            other = gateway.intents.create(intent_request(need, "second", delivery_point="other"), p.id)
            restricted = gateway.handoff_authority.create_handoff(other["id"], Handoff(to_agent_id="other", expires_at=expiry()), p)
        gateway.governance.change("other", {"allowed_categories": ["99999"]}, "Restricted research category", Principal("operator", operator=True))
        rejected = client.post(f"/v1/handoffs/{restricted['code']}/accept", headers=agent_headers("other"))
        assert rejected.status_code == 403 and rejected.json()["code"] == "CATEGORY_NOT_ALLOWED", rejected.text
        assert gateway.intents.get(other["id"], "external")["active_agent"] == "external"


def test_AC_3_handoff_single_use_expiry_and_stale_cas(gateway, need, monkeypatch):
    with actor(gateway) as p:
        obj = gateway.intents.create(intent_request(need), p.id)
        first = gateway.handoff_authority.create_handoff(obj["id"], Handoff(to_agent_id="agent-b", expires_at=expiry()), p)
        stale = gateway.handoff_authority.create_handoff(obj["id"], Handoff(to_agent_id="other", expires_at=expiry()), p)
        expiring_obj = gateway.intents.create(intent_request(need, "expires", delivery_point="other"), p.id)
        expiring = gateway.handoff_authority.create_handoff(expiring_obj["id"], Handoff(to_agent_id="agent-b", expires_at=expiry(10)), p)
    with actor(gateway, "agent-b") as p:
        gateway.handoff_authority.accept(first["code"], p)
        with pytest.raises(DomainError, match="single-use") as used:
            gateway.handoff_authority.accept(first["code"], p)
        assert used.value.code == "HANDOFF_USED"
    with actor(gateway, "other") as p:
        with pytest.raises(DomainError) as changed:
            gateway.handoff_authority.accept(stale["code"], p)
        assert changed.value.code == "INTENT_VERSION_CONFLICT"
    now = audit.utcnow()
    monkeypatch.setattr(audit, "utcnow", lambda: now + timedelta(seconds=11))
    with actor(gateway, "agent-b") as p:
        with pytest.raises(DomainError) as expired:
            gateway.handoff_authority.accept(expiring["code"], p)
        assert expired.value.code == "HANDOFF_EXPIRED"


def test_AC_4_fresh_session_open_work_and_duplicate_resume(gateway, need):
    first = create_server(gateway, agent_id="external")
    obj = mcp(first, "create_intent", {"request": intent_request(need).model_dump(mode="json")})
    fresh = create_server(gateway, agent_id="external")
    listed = mcp(fresh, "list_open_intents", {})
    assert [i["intent_id"] for i in listed["intents"]] == [obj["id"]]
    with actor(gateway) as p:
        resource = asyncio.run(fresh.read_resource("luotsi://intents/open"))
        assert obj["id"] in str(resource)
        task = a2a(gateway, p, "get_intent", "reattach", arguments={"intent_id": obj["id"]})
        assert metadata(task)["intent_id"] == obj["id"]
    from app.a2a import Handler
    from a2a.server.context import ServerCallContext
    context = ServerCallContext(state={"gateway": gateway, "keys": TEST_KEYS,
        "headers": {"authorization": "Bearer test-secret-external"}, "source": "test"})
    tasks = asyncio.run(Handler().on_list_tasks(t.ListTasksRequest(), context))
    assert task.id in [item.id for item in tasks.tasks]
    with TestClient(create_app(gateway, serve_mcp=False)) as client:
        open_work = client.get("/v1/intents?state=open", headers=agent_headers("external"))
        assert open_work.status_code == 200 and open_work.json()["intents"][0]["intent_id"] == obj["id"]
        duplicate = client.post("/v1/intents", json=intent_request(need, "new-session-id").model_dump(mode="json"), headers=agent_headers("external"))
        assert duplicate.status_code == 409 and duplicate.json()["code"] == "POSSIBLE_DUPLICATE_DEMAND"
        assert duplicate.json()["data"]["overlaps"][0]["intent_id"] == obj["id"]
    resumed = mcp(fresh, "resume_intent", {"intent_id": obj["id"], "expected_version": obj["version"]})
    assert resumed["id"] == obj["id"] and resumed["active_agent"] == "external"


def test_AC_5_research_credential_scope_depth_and_chain(gateway, need, upstream):
    with actor(gateway) as p:
        obj = gateway.intents.create(intent_request(need), p.id)
        grant = gateway.handoff_authority.delegate(Delegation(to_agent_id="agent-b", intent_id=obj["id"], expires_at=expiry()), p)
    q = own_quote(gateway, need, obj)
    headers = {"Authorization": "Bearer " + grant["credential"]}
    with actor(gateway, secret=grant["credential"]):
        specialist = create_server(gateway, agent_id=None)
        detail_mcp = mcp(specialist, "get_product", {"id": "1"})
        assert detail_mcp["id"] == "1", detail_mcp
        assert mcp(specialist, "list_open_intents", {})["intents"][0]["intent_id"] == obj["id"]
    with TestClient(create_app(gateway, serve_mcp=False)) as client:
        detail = client.get("/v1/products/1", headers=headers)
        assert detail.status_code == 200 and detail.json()["units"], detail.text
        checkpoint = client.get(f"/v1/intents/{obj['id']}/checkpoint", headers=headers)
        assert checkpoint.status_code == 200 and grant["credential"] not in checkpoint.text
        denied = client.post("/v1/orders", json={"quote_id": q.id}, headers={**headers, "Idempotency-Key": "specialist-order"})
        assert denied.status_code == 403 and denied.json()["code"] == "OUTSIDE_DELEGATION", denied.text
        deeper = client.post("/v1/delegations", json={"to_agent_id": "other", "intent_id": obj["id"], "expires_at": expiry(300)}, headers=headers)
        assert deeper.status_code == 403 and deeper.json()["code"] == "DELEGATION_DEPTH", deeper.text
    with actor(gateway) as p:
        order = gateway.orders.place_order(q.id, "holder-order", p.id)
        timeline = gateway.orders.timeline(order.id, p.id)
        assert any(e.data.get("actor_chain") == ["external", "agent-b"] for e in timeline.steps)
        next_intent = gateway.intents.create(intent_request(need, "owner-approved-helper", delivery_point="second"), p.id)
    with TestClient(create_app(gateway, serve_mcp=False)) as client:
        explicit = approved(client, "/v1/delegations", {"to_agent_id": "agent-b", "intent_id": next_intent["id"],
            "operations": ["lookup", "quote", "order", "rfq_read"], "expires_at": expiry()})
        with actor(gateway, secret=explicit["credential"]) as p:
            quote = own_quote(gateway, need, next_intent, p.id, secret=explicit["credential"])
            helper_order = gateway.orders.place_order(quote.id, "approved-helper-order", p.id)
            assert helper_order.status == "CONFIRMED" and helper_order.agent_id == "agent-b"
    with gateway.db.connect() as conn:
        assert grant["credential"] not in "".join(r[0] for r in conn.execute("SELECT data FROM audit"))


def test_AC_6_declared_chain_intersects_limits_and_parent_pause_cascades(gateway, need, upstream, monkeypatch):
    gateway.governance.change("external", {"per_order_ceiling_eur": 25}, "Restrict original requester", Principal("operator", operator=True))
    with TestClient(create_app(gateway, serve_mcp=False)) as client:
        headers = {**agent_headers("agent-b"), "On-Behalf-Of": "external"}
        lookup = client.post("/v1/offers", json=need.model_dump(mode="json"), headers=headers)
        assert lookup.status_code == 200, lookup.text
        q = client.post("/v1/quotes", json=quote_request(need, qty=10, need_ref=lookup.json()["lookup_id"]).model_dump(mode="json"), headers=headers)
        assert q.status_code == 200, q.text
        order = client.post("/v1/orders", json={"quote_id": q.json()["id"]}, headers={**agent_headers("agent-b"), "Idempotency-Key": "chain-limit"})
        assert order.status_code == 200 and order.json()["status"] == "PENDING_APPROVAL", order.text
        policy = gateway.policy.get().model_dump(mode="json")
        policy["agents"]["other"]["max_auto_daily_total_eur"] = 200
        gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Budget owner"))
        for index, expected in enumerate(("CONFIRMED", "PENDING_APPROVAL")):
            ancestor = {**agent_headers("agent-b"), "On-Behalf-Of": "other"}
            found = client.post("/v1/offers", json=need.model_dump(mode="json"), headers=ancestor).json()
            quoted = client.post("/v1/quotes", json=quote_request(need, qty=10, need_ref=found["lookup_id"]).model_dump(mode="json"), headers=ancestor).json()
            placed = client.post("/v1/orders", json={"quote_id": quoted["id"]}, headers={**agent_headers("agent-b"), "Idempotency-Key": "ancestor-budget-" + str(index)})
            assert placed.status_code == 200 and placed.json()["status"] == expected, placed.text
        assert any(h["rule"] == "max_auto_daily_total_eur" for h in placed.json()["policy"]["hits"])
        unknown = client.get("/v1/products/1", headers={**agent_headers("agent-b"), "On-Behalf-Of": "nonexistent"})
        assert unknown.status_code == 403 and unknown.json()["code"] == "UNKNOWN_PRINCIPAL_IN_CHAIN"
        with actor(gateway, "agent-b"):
            error = mcp(create_server(gateway, agent_id="agent-b"), "get_product", {"id": "1"}, SimpleNamespace(meta={"on_behalf_of": "nonexistent"}))
            assert error["code"] == "UNKNOWN_PRINCIPAL_IN_CHAIN"
        obj = client.post("/v1/intents", json=intent_request(need, delivery_point="specialist").model_dump(mode="json"), headers=agent_headers("external")).json()
        grant = client.post("/v1/delegations", json={"to_agent_id": "agent-b", "intent_id": obj["id"], "expires_at": expiry(), "max_depth": 2}, headers=agent_headers("external")).json()
        child_headers = {"Authorization": "Bearer " + grant["credential"]}
        child = client.post("/v1/delegations", json={"to_agent_id": "other", "intent_id": obj["id"], "expires_at": expiry(300)}, headers=child_headers)
        assert child.status_code == 200, child.text
        grandchild_headers = {"Authorization": "Bearer " + child.json()["credential"]}
        assert client.get("/v1/products/1", headers=grandchild_headers).status_code == 200
        gateway.governance.pause("external", True, "Owner paused requester", Principal("operator", operator=True))
        paused = client.get("/v1/products/1", headers=grandchild_headers)
        assert paused.status_code == 423 and paused.json()["code"] == "AGENT_PAUSED", paused.text
        gateway.governance.pause("external", False, "Resume requester", Principal("operator", operator=True))
        assert client.get("/v1/products/1", headers=grandchild_headers).status_code == 200
        with actor(gateway) as p:
            gateway.handoff_authority.revoke(grant["delegation_id"], p)
        revoked = client.get("/v1/products/1", headers=grandchild_headers)
        assert revoked.status_code == 403 and revoked.json()["code"] == "DELEGATION_REVOKED"
        with actor(gateway) as p:
            expiring = gateway.handoff_authority.delegate(Delegation(to_agent_id="agent-b", intent_id=obj["id"], expires_at=expiry(1)), p)
        now = audit.utcnow()
        monkeypatch.setattr(audit, "utcnow", lambda: now + timedelta(seconds=2))
        expired = client.get("/v1/products/1", headers={"Authorization": "Bearer " + expiring["credential"]})
        assert expired.status_code == 403 and expired.json()["code"] == "DELEGATION_EXPIRED"


def test_AC_7_demo_464306_1010_confirmed_unchanged(gateway, need, upstream):
    demo_catalog(gateway, gateway.settings.db_path.parent, upstream)
    with actor(gateway) as p:
        lookup = gateway.find_offers(need, actor="agent:" + p.id)
        q = gateway.orders.create_quote(quote_request(need, qty=1000, product_id="464306", need_ref=lookup.lookup_id), p.id)
        order = gateway.orders.place_order(q.id, "demo", p.id)
    assert (order.lines[0].product_id, order.total, order.status) == ("464306", 1010, "CONFIRMED")
    assert "intent_id" not in order.model_dump(mode="json")
