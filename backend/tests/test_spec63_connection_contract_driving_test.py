"""One focused, offline test per spec 63 acceptance criterion."""
import asyncio
import copy
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from google.protobuf.json_format import MessageToDict, ParseDict
from mcp import Client, types as mt

from app.agent_surface import surface
from app.api import create_app
from app.auth import Principal
from app.connection_contract import CONTRACT_META, ConnectionContract, manager
from app.connection_contract_driving import driving
from app.db import DomainError
from app.foundation import PRINCIPAL
from app.mcp_server import create_server
from app.models import BasketLookupRequest, ConnectionRequest, QuoteRequest
from app.signing import verify_jws, verify_quote
from conftest import agent_headers, demo_catalog


def connect(gateway, agent="contract-agent"):
    m = manager(gateway)
    p = gateway.accounts.bind(gateway.settings.keys.resolve("test-operator-token", gateway.policy.get()))
    connection = gateway.connections.create(ConnectionRequest(agent_id=agent, client="rest", customer_ids=["1"],
        contract_version=m.current()["version"]), p, "http://localhost:8000", True)
    with gateway.db.connect() as conn:
        row = conn.execute("SELECT * FROM connections WHERE connection_id=?", (connection.connection_id,)).fetchone()
    return connection, surface(gateway).connection_principal(row)


async def measured_run(gateway, p, *, wrong_unit=False, optional=False):
    token = PRINCIPAL.set(p)
    try:
        server = create_server(gateway, p.id, full_surface=True)
        opened = await server.call_tool("start_driving_test", {})
        assert not opened.is_error, opened
        rid = opened.structured_content["id"]
        async def step(check, tool, arguments):
            response = await server.call_tool("driving_test_step", {"run_id": rid, "check": check, "tool": tool, "arguments": arguments})
            assert not response.is_error, response
            return response.structured_content
        for check in ["unit", "retry", "approval"] + (["expired_quote", "unknown_argument"] if optional else []):
            description = await step(check, "describe", {})
            challenge = description["challenge"]
            if check == "unit":
                args = copy.deepcopy(challenge["quote_arguments"])
                if wrong_unit:
                    args["lines"][0]["quantity_sales_units"] = challenge["base_units_requested"]
                outcome = await step(check, "create_quote", args)
                assert outcome["status"] == ("failed" if wrong_unit else "passed")
            elif check == "unknown_argument":
                args = {**challenge["quote_arguments"], "delivery_date": challenge["need"]["deliver_by"]}
                assert (await step(check, "create_quote", args))["status"] == "passed"
            elif check == "expired_quote":
                outcome = await step(check, "place_order", {"quote_id": challenge["quote_id"], "idempotency_key": challenge["intent_id"]})
                assert outcome["result"]["next_action"] == "REFRESH_QUOTE"
                assert (await step(check, "create_quote", challenge["quote_arguments"]))["status"] == "passed"
            else:
                args = {"quote_id": challenge["quote_id"], "idempotency_key": challenge["intent_id"]}
                outcome = await step(check, "place_order", args)
                if check == "retry":
                    assert outcome["response_dropped"]
                    assert (await step(check, "place_order", args))["status"] == "passed"
                else:
                    assert outcome["result"]["status"] == "PENDING_APPROVAL"
                    assert (await step(check, "get_order_status", {"order_id": outcome["result"]["id"]}))["status"] == "passed"
        result = await server.call_tool("finish_driving_test", {"run_id": rid})
        assert not result.is_error, result
        return result.structured_content
    finally:
        PRINCIPAL.reset(token)


def test_AC_1_identical_digest_in_cards_meta_resource_and_sdk_initialize(gateway):
    from app.agent_surface_cards import mcp_card
    from app.a2a import agent_card
    from app.discovery import EXTENSION
    async def run():
        mcp = await mcp_card(gateway)
        a2a = MessageToDict(await agent_card(gateway))
        contract = mcp["_meta"][EXTENSION]["contract"]
        extension = next(e for e in a2a["capabilities"]["extensions"] if e["uri"] == "urn:luotsi:connection-contract:v1")
        assert extension["params"]["digest"] == contract["digest"]
        server = create_server(gateway, "external", full_surface=True)
        async with Client(server, mode="legacy") as client:
            assert client.session.initialize_result.meta[CONTRACT_META]["digest"] == contract["digest"]
            resource = await client.read_resource("luotsi://trading-profile")
            assert json.loads(resource.contents[0].text)["contract"]["digest"] == contract["digest"]
        return contract
    contract = asyncio.run(run())
    with TestClient(create_app(gateway, serve_mcp=False)) as http:
        assert http.get("/v1/meta").json()["contract"]["digest"] == contract["digest"]
        profile = http.get("/v1/trading-profile").json()["profile"]
        assert profile["currency"] == "EUR" and profile["time_zone"] == "Europe/Helsinki"
        assert profile["quantity"]["products"]["1"]["base_units_per_sale_unit"] == 10


def test_AC_2_strict_unknown_and_required_units_rest_mcp_a2a(gateway, need):
    from a2a import types as at
    quote = {"customer_id": "1", "delivery_date": str(need.deliver_by), "lines": [{"product_id": "1", "quantity_sales_units": 1}]}
    missing = need.model_dump(mode="json")
    del missing["quantity"]["unit"]
    with TestClient(create_app(gateway, serve_mcp=False)) as http:
        for endpoint, body, code in [("/v1/quotes", quote, "UNKNOWN_ARGUMENT"), ("/v1/offers", missing, "ARGUMENT_REQUIRED")]:
            response = http.post(endpoint, headers=agent_headers("external"), json=body)
            assert response.status_code == 422 and response.json()["code"] == code
            if code == "UNKNOWN_ARGUMENT":
                assert response.json()["data"]["nearest_known_field"] == "deliver_by"
    async def run():
        from fastapi import Depends
        from app.auth import resolve_principal
        s = surface(gateway)
        @s.rest_app().get("/v1/contract-null-probe", response_model=dict)
        def forward_contract_null(marker: str | None = "default", p=Depends(resolve_principal)):
            return {"marker": marker}
        s.app.openapi_schema, s.registry = None, None
        server = create_server(gateway, "external", full_surface=True)
        async with Client(server) as client:
            for tool, arguments, code in [("create_quote", quote, "UNKNOWN_ARGUMENT"), ("find_offers", {"need": missing}, "ARGUMENT_REQUIRED")]:
                result = await client.call_tool(tool, arguments)
                assert result.is_error and result.structured_content["code"] == code
                if code == "UNKNOWN_ARGUMENT":
                    assert result.structured_content["data"]["nearest_known_field"] == "deliver_by"
            forwarded = await client.call_tool("forward_contract_null", {"marker": None})
            assert not forwarded.is_error and forwarded.structured_content == {"marker": None}
    asyncio.run(run())
    p = gateway.accounts.bind(Principal("external", customer_ids=("1",)))
    for tool, arguments, code in [("create_quote", quote, "UNKNOWN_ARGUMENT"), ("find_offers", {"need": missing}, "ARGUMENT_REQUIRED")]:
        params = at.SendMessageRequest()
        ParseDict({"message": {"messageId": uuid4().hex, "role": "ROLE_USER", "parts": [{"data": {
            "version": 1, "command": "tool", "skill": tool, "arguments": arguments}}]}}, params)
        with pytest.raises(DomainError) as caught:
            gateway.a2a.send(params, p)
        assert caught.value.code == code and caught.value.status == 422
        if code == "UNKNOWN_ARGUMENT":
            assert caught.value.data["nearest_known_field"] == "deliver_by"


def test_AC_3_material_change_stale_refresh_pinned_basket_and_signed_quote(gateway, need, upstream):
    connection, p = connect(gateway)
    async def negotiate():
        token = PRINCIPAL.set(p)
        try:
            async with Client(create_server(gateway, p.id, full_surface=True), mode="legacy"):
                pass
        finally:
            PRINCIPAL.reset(token)
    asyncio.run(negotiate())
    asyncio.run(measured_run(gateway, p))
    m, d = manager(gateway), driving(gateway)
    token = PRINCIPAL.set(p)
    try:
        lookup = gateway.find_offers(need, actor="agent:" + p.id)
        quote = gateway.orders.create_quote(QuoteRequest(customer_id="1", deliver_by=need.deliver_by,
            need_ref=lookup.lookup_id, lines=[{"product_id": "1", "quantity_sales_units": 100}]), p.id)
        signed_quote = gateway.orders.create_quote(QuoteRequest(customer_id="1", deliver_by=need.deliver_by,
            need_ref=lookup.lookup_id, lines=[{"product_id": "1", "quantity_sales_units": 100}]), p.id)
        basket = gateway.baskets.find_basket_offers(BasketLookupRequest(customer_id="1", deliver_by=need.deliver_by,
            manifest={"version": 1, "recipe_name": "Breakfast milk", "servings": 1, "ingredients": [
                {"name": "Milk", "search_terms": ["milk"], "quantity": {"value": 12, "unit": "L"}}]}), actor="agent:" + p.id)
        old = m.current()["version"]
        def change_case():
            with gateway.db.connect(write=True) as conn:
                conn.execute("UPDATE products SET units=? WHERE id='1'", (json.dumps({"KI": 12}),))
            return m.current()["version"]
        async def notification_check():
            from mcp.shared.subscriptions import ToolsListChanged
            changed = asyncio.Event()
            async def message(value):
                if isinstance(value, mt.ToolListChangedNotification):
                    changed.set()
            async with Client(create_server(gateway, p.id, full_surface=True), message_handler=message, mode="legacy"):
                async with Client(create_server(gateway, p.id, full_surface=True)) as modern:
                    async with modern.listen(tools_list_changed=True) as updates:
                        await asyncio.to_thread(change_case)
                        assert isinstance(await asyncio.wait_for(anext(updates), 3), ToolsListChanged)
                        await asyncio.wait_for(changed.wait(), 3)
        asyncio.run(notification_check())
        new = m.current()["version"]
        assert new != old and not d.status(p)["valid"]
        assert m.intent(lookup.lookup_id, p.id)["status"] == "REEVALUATION_REQUIRED"
        with TestClient(create_app(gateway, serve_mcp=False)) as http:
            read = http.get("/v1/basket-lookups/" + basket.basket_lookup_id, headers={"Authorization": "Bearer " + connection.key})
            assert read.status_code == 200 and read.json()["connection_contract"]["status"] == "REEVALUATION_REQUIRED"
        with pytest.raises(DomainError) as caught:
            gateway.orders.place_order(quote.id, "stale-order", p.id)
        assert caught.value.code == "CONTRACT_STALE"
        assert any("base_units_per_sale_unit" in c["field"] for c in caught.value.data["changes"])
        assert m.signed_pin(quote.id, p.id, old)
        from app.connection_contract import SIGNED_QUOTE
        pin_token = SIGNED_QUOTE.set(True)
        try:
            assert gateway.orders.place_order(signed_quote.id, "signed-old-contract", p.id).status == "PENDING_APPROVAL"
        finally:
            SIGNED_QUOTE.reset(pin_token)
        m.acknowledge(p.connection_id, new, client="rest")
        # Contract refresh does not silently reinstate an expired driving result.
        asyncio.run(measured_run(gateway, p))
        with pytest.raises(DomainError, match="REEVALUATION_REQUIRED"):
            gateway.orders.place_order(quote.id, "needs-reevaluation", p.id)
        refreshed = gateway.find_offers(need, actor="agent:" + p.id)
        fresh_quote = gateway.orders.create_quote(QuoteRequest(customer_id="1", deliver_by=need.deliver_by,
            need_ref=refreshed.lookup_id, lines=[{"product_id": "1", "quantity_sales_units": 100}]), p.id)
        assert gateway.orders.place_order(fresh_quote.id, "refreshed-order", p.id).status == "PENDING_APPROVAL"
        # Editorial additions retain graduation and are safe on the old echo.
        with gateway.db.connect(write=True) as conn:
            conn.execute("INSERT OR REPLACE INTO workspace VALUES('connection_contract_profile',?)", (json.dumps({"documentation": "Clarified examples"}),))
        assert m.current()["version"] != new
        m.enforce(p, new)
        assert d.status(p)["valid"]
    finally:
        PRINCIPAL.reset(token)


def test_AC_4_unanswered_declared_elicitation_falls_back_with_approval_link(gateway, need, upstream):
    connection, p = connect(gateway)
    m = manager(gateway)
    m.acknowledge(p.connection_id, m.current()["version"], "2025-11-25", "rest", {"elicitation": {}})
    m.elicitation_timeout_seconds = .03
    async def run():
        token = PRINCIPAL.set(p)
        try:
            lookup = gateway.find_offers(need, actor="agent:" + p.id)
            async def unanswered(ctx, params):
                await asyncio.Event().wait()
            notifications = []
            async def message(value):
                if isinstance(value, mt.ResourceUpdatedNotification):
                    notifications.append(value)
            async with Client(create_server(gateway, p.id, full_surface=True), elicitation_callback=unanswered, message_handler=message, mode="legacy") as client:
                result = await client.session.call_tool("choose_offer", {"lookup_id": lookup.lookup_id}, allow_input_required=True)
                assert isinstance(result, mt.CallToolResult) and result.structured_content["status"] == "input_required"
                assert result.meta["approval_link"] and result.meta["notification"]["type"] == "approval_link"
                assert any(n.params.meta.get("approval_link") == result.meta["approval_link"] for n in notifications)
                return result
        finally:
            PRINCIPAL.reset(token)
    result = asyncio.run(run())
    health = surface(gateway).health(p.connection_id, p)["connection_contract"]
    assert health["negotiation"]["capabilities"]["elicitation"]["status"] == "declared"
    assert "elicitation" in health["negotiation"]["fallback_removed"]
    with TestClient(create_app(gateway, serve_mcp=False)) as http:
        response = http.get(result.meta["approval_link"], headers={"Authorization": "Bearer " + connection.key})
        assert response.status_code == 200 and response.json()["purpose"] == "tier"


def test_AC_5_measured_failed_and_passing_runs_signatures_and_order_gate(gateway, need, upstream):
    connection, p = connect(gateway)
    failed = asyncio.run(measured_run(gateway, p, wrong_unit=True))
    assert failed["payload"]["checks"]["unit"] == "failed"
    with TestClient(create_app(gateway, serve_mcp=False)) as http:
        headers = {"Authorization": "Bearer " + connection.key}
        context = http.get("/v1/me", headers=headers).json()
        assert "create_quote" in context["supported_operations"] and "place_order" not in context["supported_operations"]
        response = http.post("/v1/orders", headers={**headers, "Idempotency-Key": "denied"}, json={"quote_id": "unused"})
        assert response.status_code == 403 and response.json()["code"] == "DRIVING_TEST_REQUIRED"
        assert response.json()["data"]["checks"] == ["unit"]
    owner = gateway.accounts.bind(Principal("aurora-director", kind="tenant_user", user_id="aurora-director",
        role="tenant_admin", tenant_id="hotel-aurora", customer_ids=("1",)))
    override = driving(gateway).request_override(p.connection_id, "Supervised pilot exception", owner)
    with pytest.raises(DomainError, match="SELF_APPROVAL_FORBIDDEN"):
        gateway.governance.decide(override["id"], "approve", "Owner cannot self-approve", owner)
    reviewer = gateway.accounts.bind(gateway.settings.keys.resolve("test-operator-token", gateway.policy.get()))
    from app.governance_api import invoke
    arguments = {"request_id": override["id"], "reason": "Independent review of the supervised pilot"}
    preview = invoke(gateway, reviewer, "approve_change", arguments)
    reviewed = invoke(gateway, reviewer, "approve_change", arguments, preview.confirmation_token)
    assert reviewed["status"] == "approved" and driving(gateway).status(p)["override"]
    passed = asyncio.run(measured_run(gateway, p, optional=True))
    verify_jws(passed["signature"], passed["payload"], gateway.signing.jwks())
    assert passed["payload"]["connection_id"] == p.connection_id
    assert passed["payload"]["checks"]["elicitation"] == "not_attempted"
    token = PRINCIPAL.set(p)
    try:
        lookup = gateway.find_offers(need, actor="agent:" + p.id)
        quote = gateway.orders.create_quote(QuoteRequest(customer_id="1", deliver_by=need.deliver_by,
            need_ref=lookup.lookup_id, lines=[{"product_id": "1", "quantity_sales_units": 100}]), p.id)
        verify_quote(quote, gateway.signing.jwks())
        assert gateway.orders.place_order(quote.id, "graduated-order", p.id).status == "PENDING_APPROVAL"
        assert "order" in surface(gateway).health(p.connection_id, p)["connection_contract"]["enabled_operations"]
        registered = next(a for a in gateway.governance.register(reviewer) if a["agent_id"] == p.id)
        assert registered["connections"][0]["connection_contract"]["result"]["payload"]["checks"]["unit"] == "passed"
    finally:
        PRINCIPAL.reset(token)


def test_AC_6_demo_464306_1010_confirmed_and_seeded_passing_results(gateway, need, upstream, tmp_path):
    demo_catalog(gateway, tmp_path, upstream)
    connection, p = connect(gateway, "hotel-chain-agent")
    _, existing_other = connect(gateway, "pre-feature-agent")
    _, attempted = connect(gateway, "attempted-agent")
    driving(gateway).start(attempted)
    assert not driving(gateway).status(p)['valid']  # A new demo-named connection must test too.
    # Simulate the pre-feature database at upgrade, without touching live storage.
    with gateway.db.connect(write=True, notify=False) as conn:
        conn.execute("DELETE FROM workspace WHERE key='connection_contract_legacy_seed_v1'")
    gateway.connection_contract = ConnectionContract(gateway)
    assert driving(gateway).status(existing_other)['valid']
    assert not driving(gateway).status(attempted)['valid']  # An existing attempt is never overwritten.
    _, new_demo = connect(gateway, "hotel-chain-agent")
    gateway.connection_contract = ConnectionContract(gateway)  # Restart cannot grandfather new keys.
    assert not driving(gateway).status(new_demo)['valid']
    assert 'order' not in driving(gateway).status(new_demo)['enabled_operations']
    owner = gateway.accounts.bind(gateway.settings.keys.resolve("test-operator-token", gateway.policy.get()))
    gateway.connection_trust.confirm(connection.connection_id, owner)
    token = PRINCIPAL.set(p)
    try:
        lookup = gateway.find_offers(need, actor="agent:" + p.id)
        assert lookup.offers[0].product_id == "464306" and lookup.offers[0].purchase_total == 1010
        quote = gateway.orders.create_quote(QuoteRequest(customer_id="1", deliver_by=need.deliver_by, need_ref=lookup.lookup_id,
            lines=[{"product_id": "464306", "quantity_sales_units": 1000}]), p.id)
        for agent in (p, gateway.accounts.bind(Principal("web-console", customer_ids=("1",))),
                      gateway.accounts.bind(Principal("hospital-kitchen-agent", customer_ids=("2",)))):
            status = driving(gateway).status(agent)
            assert status["valid"] and "order" in status["enabled_operations"]
            assert set(status["result"]["payload"]["checks"].values()) == {"passed"}
            verify_jws(status["result"]["signature"], status["result"]["payload"], gateway.signing.jwks())
    finally:
        PRINCIPAL.reset(token)
    # One existing issued key negotiates a different client value, then places
    # a fresh demo order through each actual HTTP adapter.
    from test_spec43_oauth_audiences import tool
    from test_spec62_connection_trust_lifecycle import a2a_tool, mcp_client
    key = {'Authorization': 'Bearer ' + connection.key}
    with TestClient(create_app(gateway), base_url='http://localhost') as http:
        changed = http.post(f'/v1/connections/{p.connection_id}/contract', headers=key, json={
            'contract_version': manager(gateway).current()['version'], 'protocol_revision': '2026-07-28',
            'client': json.dumps({'name': 'demo-runtime', 'version': '1.0'})})
        assert changed.status_code == 200, changed.text
        mcp_client(http, key, 'demo-runtime')
        context = http.get('/v1/me', headers=key)
        assert context.status_code == 200 and context.json()['connection_contract']['valid'], context.text
        rest = http.post('/v1/orders', headers={**key, 'Idempotency-Key': 'contract-demo'}, json={'quote_id': quote.id})
        assert rest.status_code == 200, rest.text
        def mcp_call(name, arguments):
            value = tool(http, key, name, arguments)
            assert not value.get('isError'), value
            return value['structuredContent']
        results = [rest.json()]
        for protocol, call in [('mcp', mcp_call), ('a2a', lambda name, args: a2a_tool(http, key, name, args))]:
            request = need.model_dump(mode='json', exclude={'max_candidates'})
            offers = call('find_offers', {'need': request})
            assert offers['offers'][0]['product_id'] == '464306'
            fresh = call('create_quote', {'customer_id': '1', 'deliver_by': str(need.deliver_by),
                'need_ref': offers['lookup_id'], 'lines': [{'product_id': '464306', 'quantity_sales_units': 1000}]})
            results.append(call('place_order', {'quote_id': fresh['id'], 'idempotency_key': 'contract-demo-' + protocol}))
        assert len({r['id'] for r in results}) == 3
        for result in results:
            assert (result['total'], result['status']) == (1010, 'CONFIRMED')
        # Cross-protocol exact retries still return the original commitment.
        for call in (mcp_call, lambda name, args: a2a_tool(http, key, name, args)):
            replay = call('place_order', {'quote_id': quote.id, 'idempotency_key': 'contract-demo'})
            assert replay['id'] == rest.json()['id']
