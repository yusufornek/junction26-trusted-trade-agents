import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
import yaml
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError

from app import audit
from app.api import create_app
from conftest import OPERATOR_HEADERS, agent_headers
from app.mcp_server import create_server
from app.models import PolicyUpdateRequest, Quantity
from app.service import Gateway
from conftest import quote_request

AGENT = "hotel-chain-agent"


@pytest.fixture
def registered_agents():
    # This module verifies the actual committed mandate inventory.
    return ()


@pytest.fixture
def need(need):
    # Spend tests vary prices independently of pack-size reconciliation.
    return need.model_copy(update={"quantity": Quantity(value=100, unit="SALES_UNIT")})


def set_mandate(gateway, *, daily=2500, per_order=None, agent=AGENT):
    body = gateway.policy.get().model_dump(mode="json")
    body["agents"][agent] = {"customer_ids": ["1"], "max_auto_total_eur": per_order,
                              "max_auto_daily_total_eur": daily, "note": "Granted by Ada"}
    return gateway.policy.update(PolicyUpdateRequest(policy=body, updated_by="Ada"))


def fitted_quote(gateway, need, *, qty=100, agent=AGENT):
    lookup = gateway.find_offers(need, actor=f"agent:{agent}")
    return gateway.orders.create_quote(quote_request(need, qty=qty, need_ref=lookup.lookup_id), agent)


def place(gateway, need, key, *, qty=100, agent=AGENT):
    return gateway.orders.place_order(fitted_quote(gateway, need, qty=qty, agent=agent).id, key, agent)


def test_ac_1_registered_agent_within_limits_confirms(gateway, need, upstream):
    defaults = gateway.policy.get()
    assert set(defaults.agents) == {AGENT, "web-console"}
    assert all(m.customer_ids == ["1"] and m.max_auto_daily_total_eur == 5000 for m in defaults.agents.values())
    upstream.price = 14
    order = place(gateway, need, "registered")
    assert order.total == 1400 and order.status == "CONFIRMED"
    assert order.policy.hits == []


def test_ac_2_unregistered_agent_cannot_lookup_quote_or_order(gateway, need, upstream):
    upstream.price = 14
    upstream.model, upstream.amount = "Stock", 1000
    # Provision a quote before revocation so placement exercises the owner's mandate check.
    set_mandate(gateway, agent="external")
    lookup = gateway.find_offers(need, actor="agent:external")
    request = quote_request(need, qty=100, need_ref=lookup.lookup_id)
    quote = gateway.orders.create_quote(request, "external")
    policy = gateway.policy.get().model_dump(mode="json")
    del policy["agents"]["external"]
    gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Ada"))
    calls = len(upstream.price_calls), len(upstream.availability_calls), len(upstream.decision_calls)
    with TestClient(create_app(gateway)) as client:
        for headers, status, code in (({}, 401, "UNAUTHENTICATED"),
                                       (agent_headers("external"), 403, "AGENT_NOT_REGISTERED")):
            for path, body in (("/v1/offers", need.model_dump(mode="json")),
                               ("/v1/quotes", request.model_dump(mode="json")),
                               ("/v1/orders", {"quote_id": quote.id})):
                response = client.post(path, json=body, headers={**headers, "Idempotency-Key": "external"})
                assert response.status_code == status and response.json()["code"] == code
        assert calls == (len(upstream.price_calls), len(upstream.availability_calls), len(upstream.decision_calls))
        assert gateway.orders.get_quote(quote.id).status == "OPEN"
        with gateway.db.connect() as conn:
            assert conn.execute("SELECT count(*) FROM orders").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM reservations").fetchone()[0] == 0
        event = gateway.audit()[-1]
        assert event.action == "PLACE_ORDER_FAILED" and event.actor == "agent:external"
        assert event.quote_id == quote.id and event.data["error"] == "AGENT_NOT_REGISTERED"
        assert "ordering requires a mandate" in event.summary
        assert client.get("/v1/stats", headers=OPERATOR_HEADERS).json()["agents_seen"] == [
            {"actor": "agent:external", "order_count": 0, "registered": False}]
        client.post("/v1/offers", json=need.model_dump(mode="json"), headers={**agent_headers("web-console")})
        assert gateway.audit()[-1].actor == "agent:web-console"
        stats = client.get("/v1/stats", headers=OPERATOR_HEADERS).json()
        assert {entry["actor"]: entry["registered"] for entry in stats["agents_seen"]} == {
            "agent:web-console": True, "agent:external": False}


def test_ac_3_daily_limit_serializes_orders_and_uses_rolling_window(gateway, need, upstream, monkeypatch):
    set_mandate(gateway)
    upstream.price = 14
    first = place(gateway, need, "first")
    assert first.total == 1400 and first.status == "CONFIRMED"
    second = place(gateway, need, "second")
    assert second.status == "PENDING_APPROVAL"
    hit, = second.policy.hits
    assert hit.rule == "max_auto_daily_total_eur"
    assert "1400.00 + 1400.00 exceeds 2500.00" in hit.detail
    assert gateway.orders.place_order(first.quote_id, "first", AGENT).id == first.id
    # Pending orders and idempotent replays do not inflate autonomous spend.
    upstream.price = 11
    boundary = place(gateway, need, "equal-ceiling")
    assert boundary.total == 1100 and boundary.status == "CONFIRMED"
    upstream.price = 14
    # Another agent has an independent 24-hour budget.
    assert place(gateway, need, "other-agent", agent="web-console").status == "CONFIRMED"

    # Concurrent attempts share the same writer lock, even across gateway instances.
    set_mandate(gateway, agent="concurrent")
    quotes = [fitted_quote(gateway, need, agent="concurrent") for _ in range(2)]
    peer = Gateway(gateway.settings)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(service.orders.place_order, quote.id, f"parallel-{i}", "concurrent")
                       for i, (service, quote) in enumerate(zip((gateway, peer), quotes))]
            assert sorted(f.result().status for f in futures) == ["CONFIRMED", "PENDING_APPROVAL"]
    finally:
        peer.close()

    # A fresh quote remains usable while the original confirmed order crosses the rolling boundary.
    later = first.created_at + timedelta(hours=24)
    upstream.cutoff = later + timedelta(hours=1)
    monkeypatch.setattr(audit, "utcnow", lambda: later)
    at_boundary = place(gateway, need, "exact-24h")
    assert at_boundary.status == "PENDING_APPROVAL"  # Inclusive boundary.
    monkeypatch.setattr(audit, "utcnow", lambda: later + timedelta(seconds=1))
    assert place(gateway, need, "after-24h").status == "CONFIRMED"


def test_ac_4_cancelled_orders_leave_autonomous_daily_total(gateway, need, upstream):
    set_mandate(gateway)
    upstream.price = 14
    first = place(gateway, need, "first")
    assert first.status == "CONFIRMED"
    assert place(gateway, need, "blocked").status == "PENDING_APPROVAL"
    gateway.orders.cancel(first.id, actor=f"agent:{AGENT}")
    assert place(gateway, need, "after-cancel").status == "CONFIRMED"


def test_ac_5_human_approved_orders_do_not_consume_daily_budget(gateway, need, upstream):
    set_mandate(gateway)
    upstream.price = 14
    upstream.fit = 0.7
    pending = place(gateway, need, "human")
    assert pending.status == "PENDING_APPROVAL"
    assert gateway.orders.approve(pending.id, "Ada").status == "CONFIRMED"
    upstream.fit = 0.91
    assert place(gateway, need, "autonomous").status == "CONFIRMED"
    assert place(gateway, need, "over-budget").status == "PENDING_APPROVAL"


def test_ac_6_effective_per_order_limit_reports_source(gateway, need, upstream):
    set_mandate(gateway, per_order=500)
    upstream.price = 8
    order = place(gateway, need, "mandate-limit")
    assert order.total == 800 and order.status == "PENDING_APPROVAL"
    hit, = order.policy.hits
    assert hit.rule == "max_auto_total_eur"
    assert "500.00" in hit.detail and "mandate" in hit.detail
    set_mandate(gateway, per_order=2000)
    upstream.price = 16
    global_order = place(gateway, need, "global-limit")
    assert global_order.status == "PENDING_APPROVAL"
    assert "1500.00" in global_order.policy.hits[0].detail
    assert "global policy" in global_order.policy.hits[0].detail


def test_ac_7_customer_scope_rejects_quotes_and_orders_with_audit(gateway, need, upstream):
    other_need = need.model_copy(update={"customer_id": "2"})
    # Create owned evidence while authorised, then revoke that customer's scope.
    policy = gateway.policy.get().model_dump(mode="json")
    policy["agents"][AGENT]["customer_ids"] = ["1", "2"]
    gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Ada"))
    lookup = gateway.find_offers(other_need, actor=f"agent:{AGENT}")
    request = quote_request(other_need, qty=1, need_ref=lookup.lookup_id).model_dump(mode="json")
    quote = gateway.orders.create_quote(quote_request(other_need, qty=1, need_ref=lookup.lookup_id), AGENT)
    policy["agents"][AGENT]["customer_ids"] = ["1"]
    gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Ada"))
    with TestClient(create_app(gateway), headers=OPERATOR_HEADERS) as client:
        calls = len(upstream.price_calls), len(upstream.availability_calls)
        denied = client.post("/v1/quotes", json=request, headers={**agent_headers(AGENT)})
        assert denied.status_code == 403 and denied.json()["code"] == "CUSTOMER_NOT_IN_MANDATE"
        assert calls == (len(upstream.price_calls), len(upstream.availability_calls))
        event = gateway.audit()[-1]
        assert event.action == "CREATE_QUOTE_FAILED" and event.actor == f"agent:{AGENT}"
        assert event.data["error"] == "CUSTOMER_NOT_IN_MANDATE"
        denied = client.post("/v1/orders", json={"quote_id": quote.id}, headers={
            **agent_headers(AGENT), "Idempotency-Key": "scope"})
        assert denied.status_code == 403 and denied.json()["code"] == "CUSTOMER_NOT_IN_MANDATE"
        event = gateway.audit()[-1]
        assert event.action == "PLACE_ORDER_FAILED" and event.quote_id == quote.id
        assert event.data["error"] == "CUSTOMER_NOT_IN_MANDATE"
        assert calls == (len(upstream.price_calls), len(upstream.availability_calls))
        assert gateway.orders.get_quote(quote.id).status == "OPEN"
        assert gateway.orders.list_orders() == []
        # Revoking scope after quote creation also takes effect at placement.
        quote = fitted_quote(gateway, need)
        body = gateway.policy.get().model_dump(mode="json")
        body["agents"][AGENT]["customer_ids"] = ["2"]
        gateway.policy.update(PolicyUpdateRequest(policy=body, updated_by="Ada"))
        assert client.post("/v1/orders", json={"quote_id": quote.id}, headers={
            **agent_headers(AGENT), "Idempotency-Key": "revoked"}).status_code == 403


def test_ac_8_policy_merges_legacy_fields_validates_and_audits_mandates(gateway):
    with TestClient(create_app(gateway), headers=OPERATOR_HEADERS) as client:
        body = client.get("/v1/policy").json()
        body["agents"][AGENT]["max_auto_daily_total_eur"] = 2500
        body["shortlist"]["live_max"] = 12
        response = client.put("/v1/policy", json={"policy": body, "updated_by": "Ada"})
        assert response.status_code == 200
        assert gateway.audit()[-1].data["diff"][f"agents.{AGENT}.max_auto_daily_total_eur"] == {"before": 5000, "after": 2500}
        # Older frontend PUTs must retain both fields, across process reloads as well.
        del body["agents"], body["shortlist"]["live_max"]
        response = client.put("/v1/policy", json={"policy": body, "updated_by": "Ada"})
        assert response.status_code == 200
        assert response.json()["agents"][AGENT]["max_auto_daily_total_eur"] == 2500
        assert response.json()["shortlist"]["live_max"] == 12
        peer = Gateway(gateway.settings)
        try:
            assert peer.policy.get() == gateway.policy.get()
        finally:
            peer.close()

        valid = {"customer_ids": ["1"], "max_auto_daily_total_eur": 0, "max_auto_total_eur": 0}
        invalid_maps = [{"": valid}, {" ": valid}, {AGENT: {**valid, "customer_ids": []}},
                        {AGENT: {**valid, "customer_ids": [""]}}, {AGENT: {**valid, "customer_ids": [" "]}},
                        {AGENT: {**valid, "max_auto_total_eur": -1}},
                        {AGENT: {**valid, "max_auto_daily_total_eur": -1}},
                        {AGENT: {**valid, "max_auto_daily_total_eur": "NaN"}}]
        before, events = client.get("/v1/policy").json(), len(gateway.audit())
        for agents in invalid_maps:
            assert client.put("/v1/policy", json={"policy": {**body, "agents": agents}, "updated_by": "Ada"}).status_code == 422
        assert client.get("/v1/policy").json() == before and len(gateway.audit()) == events
        # Explicit additions and removals are governed, not treated as omitted fields.
        assert client.put("/v1/policy", json={"policy": {**body, "agents": {"new-agent": valid}}, "updated_by": "Ada"}).status_code == 200
        diff = gateway.audit()[-1].data["diff"]
        assert diff["agents.new-agent"]["before"] is None
        assert diff[f"agents.{AGENT}"]["after"] is None
        assert client.put("/v1/policy", json={"policy": {**body, "agents": {}}, "updated_by": "Ada"}).json()["agents"] == {}
        reset = client.post("/v1/policy/reset", json={"updated_by": "Ada"}).json()
        assert set(reset["agents"]) == {AGENT, "web-console"}
        # An old runtime file falls back to the actual committed defaults, preserving other edits.
        body["approval"]["max_auto_total_eur"] = 1200
        gateway.policy.runtime_path.write_text(yaml.safe_dump(body))
        peer = Gateway(gateway.settings)
        try:
            assert peer.policy.get().agents == gateway.policy.get().agents
            assert peer.policy.get().agents[AGENT].max_auto_daily_total_eur == 5000
            assert peer.policy.get().shortlist.live_max == 25
            assert peer.policy.get().approval.max_auto_total_eur == 1200
        finally:
            peer.close()


def test_ac_9_mcp_uses_fixed_server_identity_for_mandate(gateway, need, upstream):
    registered = create_server(gateway, agent_id=AGENT)
    unregistered = create_server(gateway, agent_id="outsider")

    async def run():
        for server in (registered, unregistered):
            tools = await server.list_tools()
            assert all("agent_id" not in t.input_schema["properties"] for t in tools)
            assert "unregistered" in next(t.description for t in tools if t.name == "place_order").lower()

        offers = await registered.call_tool("find_offers", {"need": need.model_dump(mode="json")})
        assert not offers.is_error
        assert gateway.audit()[-1].actor == f"agent:{AGENT}"
        quote = await registered.call_tool("create_quote", {"customer_id": "1", "deliver_by": str(need.deliver_by),
            "need_ref": offers.structured_content["lookup_id"], "lines": [{"product_id": "1", "quantity_sales_units": 1}]})
        assert not quote.is_error
        placed = await registered.call_tool("place_order", {
            "quote_id": quote.structured_content["id"], "idempotency_key": AGENT})
        assert not placed.is_error
        assert placed.structured_content["agent_id"] == AGENT
        assert placed.structured_content["status"] == "CONFIRMED"

        calls = len(upstream.price_calls), len(upstream.availability_calls), len(upstream.decision_calls)
        for tool, action, args in (
            ("find_offers", "LOOKUP_FAILED", {"need": need.model_dump(mode="json")}),
            ("create_quote", "CREATE_QUOTE_FAILED", {"customer_id": "1", "deliver_by": str(need.deliver_by),
                "need_ref": offers.structured_content["lookup_id"], "lines": [{"product_id": "1", "quantity_sales_units": 1}]}),
            ("place_order", "PLACE_ORDER_FAILED", {"quote_id": quote.structured_content["id"], "idempotency_key": "outsider"}),
        ):
            with pytest.raises(ToolError, match="AGENT_NOT_REGISTERED"):
                await unregistered.call_tool(tool, args)
            event = gateway.audit()[-1]
            assert event.action == action and event.actor == "agent:outsider"
            assert event.data["error"] == "AGENT_NOT_REGISTERED"
        assert calls == (len(upstream.price_calls), len(upstream.availability_calls), len(upstream.decision_calls))
        with pytest.raises(ToolError, match="CUSTOMER_NOT_IN_MANDATE"):
            await registered.call_tool("create_quote", {"customer_id": "2", "deliver_by": str(need.deliver_by),
                "lines": [{"product_id": "1", "quantity_sales_units": 1}]})

    asyncio.run(run())


def test_ac_11_revoked_mandate_blocks_approval_but_allows_replay_and_rejection(gateway, need, upstream):
    set_mandate(gateway, per_order=500)
    upstream.price = 14
    upstream.model, upstream.amount = "Stock", 1000
    order = place(gateway, need, "revoke-pending")
    assert order.status == "PENDING_APPROVAL"
    with gateway.db.connect() as conn:
        reservations = [tuple(row) for row in conn.execute("SELECT * FROM reservations")]
    assert reservations == [(order.id, "1", 100)]

    with TestClient(create_app(gateway), headers=OPERATOR_HEADERS) as client:
        body = gateway.policy.get().model_dump(mode="json")
        del body["agents"][AGENT]
        assert client.put("/v1/policy", json={"policy": body, "updated_by": "Ada"}).status_code == 200
        # Placement replays still return the existing order after the mandate is removed.
        calls = len(upstream.availability_calls)
        replay = client.post("/v1/orders", json={"quote_id": order.quote_id}, headers={
            "Idempotency-Key": "revoke-pending", **agent_headers(AGENT)})
        assert replay.status_code == 200 and replay.json()["id"] == order.id
        assert len(upstream.availability_calls) == calls

        denied = client.post(f"/v1/approvals/{order.id}/approve", json={"approver": "Ada"})
        assert denied.status_code == 403 and denied.json()["code"] == "AGENT_NOT_REGISTERED"
        hits = denied.json()["data"]["rule_hits"]
        assert "unregistered_agent" in [hit["rule"] for hit in hits]
        event = gateway.audit(order_id=order.id)[-1]
        assert event.action == "APPROVE_FAILED" and event.data["rule_hits"] == hits
        pending = client.get(f"/v1/orders/{order.id}").json()
        assert pending["status"] == "PENDING_APPROVAL" and pending["approver"] is None
        with gateway.db.connect() as conn:
            assert [tuple(row) for row in conn.execute("SELECT * FROM reservations")] == reservations
        rejected = client.post(f"/v1/approvals/{order.id}/reject", json={"approver": "Ada"})
        assert rejected.status_code == 200 and rejected.json()["status"] == "REJECTED"
        with gateway.db.connect() as conn:
            assert conn.execute("SELECT count(*) FROM reservations").fetchone()[0] == 0
