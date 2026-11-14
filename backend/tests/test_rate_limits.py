"""Spec 24: per-principal buckets shared across transports, upstream admission."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.limits import UpstreamGuard
from app.mcp_server import create_server
from app.models import PolicyUpdateRequest
from conftest import OPERATOR_HEADERS, agent_headers
from test_shared_foundation import make_quote


def set_limits(gateway, category, capacity=1, refill=1, agent=None):
    policy = gateway.policy.get().model_dump(mode="json")
    limits = policy["rate_limits"]
    limits[category] = {"capacity": capacity, "refill_per_minute": refill}
    if agent:
        policy["agents"][agent]["rate_limits"] = limits
    gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Ada"))


def test_ac_1_lookup_throttle_before_external_calls(gateway, need, upstream):
    set_limits(gateway, "lookup")
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        assert client.post("/v1/offers", json=need.model_dump(mode="json")).status_code == 200
        before = (len(upstream.price_calls), len(upstream.availability_calls), len(upstream.decision_calls))
        refused = client.post("/v1/offers", json=need.model_dump(mode="json"))
        assert refused.status_code == 429 and int(refused.headers["Retry-After"]) > 0
        body = refused.json()
        assert body["code"] == "RATE_LIMITED" and body["next_action"] == "RETRY_SAME_COMMAND"
        assert body["retry_after_seconds"] == int(refused.headers["Retry-After"])
        assert before == (len(upstream.price_calls), len(upstream.availability_calls), len(upstream.decision_calls))
        stats = client.get("/v1/stats", headers=OPERATOR_HEADERS).json()
        assert stats["throttles"] == 1
    assert any(e.action == "RATE_LIMITED" for e in gateway.audit())


@pytest.mark.parametrize("registered_agents", [("external", "other")], indirect=True)
def test_ac_2_read_and_other_principal_unaffected(gateway, need, upstream):
    quote = make_quote(gateway, need)
    order = gateway.orders.place_order(quote.id, "read")
    set_limits(gateway, "lookup", agent="external")
    # The first lookup already consumed a token; changing its limit caps remaining tokens.
    gateway.limiter.buckets.pop(("external", "lookup"))
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        assert client.post("/v1/offers", json=need.model_dump(mode="json")).status_code == 200
        assert client.post("/v1/offers", json=need.model_dump(mode="json")).status_code == 429
        assert client.get(f"/v1/orders/{order.id}").status_code == 200
        assert client.post("/v1/offers", json=need.model_dump(mode="json"), headers=agent_headers("other")).status_code == 200
        me = client.get("/v1/me").json()
        assert me["rate_limits"]["lookup"]["remaining"] < 1
        assert me["rate_limits"]["read"]["remaining"] > 0


def test_ac_3_rest_mcp_share_bucket(gateway, need, upstream):
    set_limits(gateway, "lookup")
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        assert client.post("/v1/offers", json=need.model_dump(mode="json")).status_code == 200
    before = len(upstream.decision_calls)
    result = asyncio.run(create_server(gateway, "external").call_tool("find_offers", {"need": need.model_dump(mode="json")}))
    assert result.is_error and result.structured_content["status"] == 429
    assert result.structured_content["next_action"] == "RETRY_SAME_COMMAND"
    assert len(upstream.decision_calls) == before


def test_ac_4_replay_uses_read_bucket(gateway, need, upstream):
    quote = make_quote(gateway, need)
    set_limits(gateway, "mutation", refill=0.001)
    gateway.limiter.buckets.pop(("external", "mutation"))
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        args = {"json": {"quote_id": quote.id}, "headers": {"Idempotency-Key": "cheap"}}
        first = client.post("/v1/orders", **args)
        assert first.status_code == 200
        before = len(upstream.availability_calls)
        replay = asyncio.run(create_server(gateway, "external").call_tool("place_order", {"quote_id": quote.id, "idempotency_key": "cheap"}))
        assert not replay.is_error and replay.structured_content["id"] == first.json()["id"]
        assert len(upstream.availability_calls) == before
        refused = client.post("/v1/orders", json={"quote_id": quote.id}, headers={"Idempotency-Key": "new-key"})
        assert refused.status_code == 429


def test_ac_5_refill_allows_same_request(gateway, need, upstream):
    now = [1000.0]
    gateway.limiter.clock = lambda: now[0]
    set_limits(gateway, "lookup", refill=1)
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        assert client.post("/v1/offers", json=need.model_dump(mode="json")).status_code == 200
        assert client.post("/v1/offers", json=need.model_dump(mode="json")).status_code == 429
        now[0] += 60
        assert client.post("/v1/offers", json=need.model_dump(mode="json")).status_code == 200


def test_ac_6_policy_limit_effective_next_request_and_audited(gateway, need, upstream):
    set_limits(gateway, "lookup", capacity=3)
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        assert client.post("/v1/offers", json=need.model_dump(mode="json")).status_code == 200
        set_limits(gateway, "lookup", capacity=1, refill=0.001)
        assert client.post("/v1/offers", json=need.model_dump(mode="json")).status_code == 200
        assert client.post("/v1/offers", json=need.model_dump(mode="json")).status_code == 429
    changes = [e.data["diff"] for e in gateway.audit() if e.action == "POLICY_UPDATED"]
    assert any("rate_limits.lookup.capacity" in diff for diff in changes)


def test_ac_7_bounded_global_supplier_concurrency(gateway, need, upstream, monkeypatch):
    from app import limits
    guard = UpstreamGuard(concurrency=1, queue_size=1, wait_seconds=2)
    monkeypatch.setattr(limits, "SUPPLIER_GUARD", guard)
    entered, release, queued, second_entered = (threading.Event() for _ in range(4))
    active, maximum = [0], [0]
    lock = threading.Lock()
    original_acquire = guard.admitted.acquire
    def acquire(*args, **kwargs):
        result = original_acquire(*args, **kwargs)
        if result and entered.is_set():
            queued.set()
        return result
    monkeypatch.setattr(guard.admitted, "acquire", acquire)
    def held(second=False):
        with guard.slot():
            with lock:
                active[0] += 1
                maximum[0] = max(maximum[0], active[0])
            (second_entered if second else entered).set()
            try:
                assert release.wait(5)
            finally:
                with lock:
                    active[0] -= 1
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(held)
        assert entered.wait(5)
        second = pool.submit(held, True)
        try:
            assert queued.wait(5)
            assert not second_entered.wait(0.1), "queued supplier call entered while active slot was occupied"
            with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
                refused = client.post("/v1/offers", json=need.model_dump(mode="json"))
            assert refused.status_code == 503
            assert refused.json()["code"] == "UPSTREAM_BUSY" and refused.json()["retryable"]
            assert refused.json()["next_action"] == "RETRY_SAME_COMMAND"
            assert not upstream.price_calls and not upstream.availability_calls
        finally:
            release.set()
        first.result()
        second.result()
    assert second_entered.is_set() and maximum[0] == 1


def test_unknown_credentials_limited_before_authentication(gateway, monkeypatch):
    from app.limits import SourceLimiter
    gateway.source_limiter = SourceLimiter(capacity=1, refill_per_minute=0.001)
    keys = gateway.settings.keys
    original, calls = keys.resolve, []
    def resolve(secret, policy=None):
        calls.append(1)
        return original(secret, policy)
    monkeypatch.setattr(keys, "resolve", resolve)
    with TestClient(create_app(gateway)) as client:
        assert client.get("/v1/me", headers={"Authorization": "Bearer unknown"}).status_code == 401
        assert client.get("/v1/me", headers={"Authorization": "Bearer unknown"}).status_code == 429
    assert len(calls) == 1


def test_review_legacy_replay_charged_as_read(gateway, need, upstream):
    quote = make_quote(gateway, need)
    # Simulate a pre-command-boundary record, including the original raw key format.
    order = gateway.orders.place_order.__wrapped__(quote.id, 'legacy')
    with gateway.db.connect(write=True) as conn:
        conn.execute("UPDATE orders SET idempotency_key='legacy' WHERE id=?", (order.id,))
        assert conn.execute('SELECT count(*) FROM commands').fetchone()[0] == 0
    set_limits(gateway, 'mutation', refill=0.001)
    gateway.limiter.clock = lambda: 1000
    gateway.limiter.buckets.pop(('external', 'mutation'), None)
    gateway.limiter.consume('external', 'mutation')
    with TestClient(create_app(gateway), headers=agent_headers('external')) as client:
        result = client.post('/v1/orders', json={'quote_id': quote.id}, headers={'Idempotency-Key': 'legacy'})
    assert result.status_code == 200, result.text
    assert result.json()['id'] == order.id
    assert gateway.limiter.snapshot('external')['mutation']['remaining'] == 0


def test_review_inbox_costs_one_read(gateway, need, upstream):
    gateway.orders.place_order(make_quote(gateway, need, qty=200).id, 'inbox')
    set_limits(gateway, 'read', refill=0.001)
    gateway.limiter.clock = lambda: 1000
    with TestClient(create_app(gateway), headers=OPERATOR_HEADERS) as client:
        result = client.get('/v1/approvals')
        assert result.status_code == 200 and len(result.json()) == 1, result.text
        # Standalone quote reads still consume their own token.
        quote_id = result.json()[0]['quote']['id']
        assert client.get(f'/v1/quotes/{quote_id}').status_code == 429


@pytest.mark.parametrize('transport', ['rest-agent', 'rest-operator', 'mcp'])
def test_review_known_credentials_survive_source_exhaustion(gateway, transport):
    from app.limits import SourceLimiter
    gateway.source_limiter = SourceLimiter(capacity=1, refill_per_minute=0.001, clock=lambda: 1000)
    if transport == 'mcp':
        app = create_server(gateway, 'external').streamable_http_app(stateless_http=True, json_response=True)
        url = '/mcp'
        valid = agent_headers('external')
    else:
        app, url = create_app(gateway), '/v1/me'
        valid = OPERATOR_HEADERS if transport == 'rest-operator' else agent_headers('external')
    with TestClient(app) as client:
        assert client.get(url, headers={'Authorization': 'Bearer invalid'}).status_code == 401
        assert client.get(url, headers={'Authorization': 'Bearer invalid'}).status_code == 429
        response = client.get(url, headers={**valid, 'Accept': 'application/json, text/event-stream'})
        assert response.status_code not in (401, 429), response.text


def test_review_conflicting_commands_pay_read_cost(gateway, need, upstream):
    quote = make_quote(gateway, need)
    gateway.orders.place_order(quote.id, 'conflict-cost')
    set_limits(gateway, 'read', refill=0.001)
    gateway.limiter.clock = lambda: 1000
    with TestClient(create_app(gateway), headers=agent_headers('external')) as client:
        for status in (409, 429):
            result = client.post('/v1/orders', json={'quote_id': 'changed'}, headers={'Idempotency-Key': 'conflict-cost'})
            assert result.status_code == status, result.text
    assert any(e.action == 'RATE_LIMITED' and e.data['category'] == 'read' for e in gateway.audit())
