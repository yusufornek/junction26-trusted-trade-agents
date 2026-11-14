"""Spec 23: recovery decisions must be data, across transports."""
import ast
import asyncio
import importlib.util
import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import audit
from app.api import create_app
from app.mcp_server import create_server
from app.recovery import RECOVERY
from conftest import OPERATOR_HEADERS, agent_headers
from test_shared_foundation import make_quote


def expire_quote(gateway, quote):
    from app.signing import quote_payload
    quote.expires_at = audit.utcnow() - timedelta(seconds=1)
    quote.signature = gateway.signing.sign(quote_payload(quote))
    with gateway.db.connect(write=True) as conn:
        gateway.orders._save_quote(conn, quote)


def test_ac_1_all_domain_codes_have_explicit_mapping():
    codes = {"UNKNOWN_CODE", "VALIDATION_ERROR"}
    dynamic_sources = {("auth.py", "check_owner", "code"),
                       ("recovery.py", "validation_problem", "code"),
                       ("a2a.py", "invoke", "code")}
    dynamic_sources.add(("governance.py", "check_operation", "code"))
    observed = set()
    for file in (Path(__file__).parents[1] / "app").glob("*.py"):
        tree = ast.parse(file.read_text())
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "DomainError":
                code = node.args[0] if node.args else next((kw.value for kw in node.keywords if kw.arg == "code"), None)
                if isinstance(code, ast.Constant):
                    codes.add(code.value)
                else:
                    owner = parents.get(node)
                    while owner is not None and not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        owner = parents.get(owner)
                    source = (file.name, owner.name if owner else None, ast.unparse(code))
                    assert source in dynamic_sources, source
                    observed.add(source)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "check_owner":
                assert len(node.args) >= 3 and isinstance(node.args[2], ast.Constant)
                codes.add(node.args[2].value)
    assert observed == dynamic_sources
    assert codes <= RECOVERY.keys(), codes - RECOVERY.keys()
    assert set(RECOVERY.values()) <= {(False, action) for action in ("FIX_REQUEST", "STOP", "CONTACT_ADMIN", "REFRESH_QUOTE", "REVIEW_UPDATED_EVIDENCE", "LOOKUP_REQUIRED", "WAIT_FOR_APPROVAL")} | {(True, "RETRY_SAME_COMMAND")}


def test_ac_2_expired_quote_problem_matches_mcp(gateway, need, upstream):
    quote = make_quote(gateway, need)
    expire_quote(gateway, quote)
    with TestClient(create_app(gateway), headers={**agent_headers("external"), "X-Correlation-Id": "expiry"}) as client:
        response = client.post("/v1/orders", json={"quote_id": quote.id}, headers={"Idempotency-Key": "expired"})
    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"
    data = response.json()
    assert data["next_action"] == "REFRESH_QUOTE" and data["retryable"] is False
    assert data["code"] == "EXPIRED_QUOTE" and data["message"]
    assert {"type", "title", "detail", "status", "instance", "violations", "correlation_id", "recovery_version"} <= data.keys()
    result = asyncio.run(create_server(gateway, "external").call_tool("place_order", {"quote_id": quote.id, "idempotency_key": "expired"}))
    assert result.is_error
    for field in ("type", "title", "status", "code", "message", "retryable", "next_action", "retry_after_seconds", "violations", "data", "recovery_version"):
        assert result.structured_content[field] == data[field]


@pytest.mark.parametrize("endpoint", ["quote", "price", "availability"])
def test_ac_3_timeout_is_retryable_with_interval(gateway, need, upstream, respx_mock, endpoint):
    quote = make_quote(gateway, need)
    gateway.valio.cache.clear()
    respx_mock.post("https://valio.test/api/" + ("price" if endpoint == "quote" else endpoint)).mock(side_effect=httpx.ReadTimeout("private upstream detail"))
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        from conftest import quote_request
        response = (client.post("/v1/quotes", json=quote_request(need, need_ref=quote.need_ref).model_dump(mode="json"))
                    if endpoint == "quote" else client.post("/v1/offers", json=need.model_dump(mode="json")))
    assert response.status_code == 502
    data = response.json()
    assert data["retryable"] and data["next_action"] == "RETRY_SAME_COMMAND"
    assert data["retry_after_seconds"] > 0
    assert "private upstream detail" not in response.text


def test_ac_4_pending_guidance_clears_after_approval(gateway, need, upstream):
    quote = make_quote(gateway, need, qty=200)
    order = gateway.orders.place_order(quote.id, "pending")
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        pending = client.get(f"/v1/orders/{order.id}").json()
        assert pending["next_action"] == "WAIT_FOR_APPROVAL" and pending["poll_after_seconds"] > 0
        approved = client.post(f"/v1/approvals/{order.id}/approve", json={"approver": "Ada"}, headers=OPERATOR_HEADERS)
        assert approved.status_code == 200
        confirmed = client.get(f"/v1/orders/{order.id}").json()
        assert confirmed["next_action"] is None and confirmed["poll_after_seconds"] is None


def test_ac_5_unknown_allergen_has_valid_values(gateway, need):
    data = need.model_dump(mode="json")
    data["exclude_allergens"] = ["NOT_AN_ALLERGEN"]
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        response = client.post("/v1/offers", json=data)
    assert response.status_code == 422
    body = response.json()
    assert body["next_action"] == "FIX_REQUEST"
    assert "AE" in body["violations"][0]["valid_values"]
    assert body["violations"][0]["field"] == "exclude_allergens"


def test_ac_6_no_unauthorised_resource_references(gateway, need, upstream):
    quote = make_quote(gateway, need)
    order = gateway.orders.place_order(quote.id, "private")
    with TestClient(create_app(gateway)) as client:
        response = client.get(f"/v1/orders/{order.id}")
        assert response.status_code == 401 and "resource_ref" not in response.json()
        response = client.get(f"/v1/orders/{order.id}", headers=agent_headers("other"))
        assert response.status_code == 404 and "resource_ref" not in response.json()
        assert quote.id not in response.text and order.id not in json.dumps(response.json().get("data"))


def test_ac_7_reference_client_recovers_using_actions_only(gateway, need, upstream):
    file = Path(__file__).parents[2] / "clients/python/reference_buyer.py"
    spec = importlib.util.spec_from_file_location("reference_buyer", file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    server = create_server(gateway, "external")
    state = {"expired": False, "throttle": False, "lost": False}
    waits = []
    async def call(name, arguments):
        if name == "place_order" and not state["expired"]:
            expire_quote(gateway, gateway.orders.get_quote(arguments["quote_id"]))
            state["expired"] = True
        elif name == "place_order" and not state["throttle"]:
            from app.db import DomainError
            from app.recovery import problem
            from mcp.types import CallToolResult
            state["throttle"] = True
            payload = problem(DomainError("RATE_LIMITED", "opaque", 429, {"retry_after_seconds": 3}))
            payload["code"], payload["message"] = "OPAQUE", "opaque"
            return CallToolResult(is_error=True, structured_content=payload, content=[])
        result = await server.call_tool(name, arguments)
        if name == "place_order" and not result.is_error and not state["lost"]:
            state["lost"] = True
            # Real SDK read timeout on an in-memory connection after the purchase committed.
            import anyio
            from mcp import ClientSession
            incoming_send, incoming = anyio.create_memory_object_stream(10)
            outgoing, outgoing_receive = anyio.create_memory_object_stream(10)
            from mcp.shared.exceptions import MCPError
            timeout = None
            async with incoming_send, incoming, outgoing, outgoing_receive:
                async with ClientSession(incoming, outgoing, read_timeout_seconds=0.01) as session:
                    try:
                        await session.call_tool("place_order", arguments)
                    except MCPError as error:
                        timeout = error
            assert timeout is not None
            raise timeout
        return result
    async def sleep(seconds):
        waits.append(seconds)
    result = asyncio.run(module.ReferenceBuyer(call, sleep=sleep).purchase(need.model_dump(mode="json"), product_id="1", units=1))
    assert result["status"] == "CONFIRMED" and all(state.values())
    assert waits == [3, 1]
    with gateway.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
