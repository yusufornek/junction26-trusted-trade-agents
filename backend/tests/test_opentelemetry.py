"""Spec 27 acceptance with SDK in-memory exporters, including auto instrumentation."""
import json
import threading

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from app.api import create_app
from app.telemetry import Telemetry, ALLOWLIST
from app.anchoring import AnchorConfig
from conftest import agent_headers, quote_request
from test_webhooks import receiver, subscribe
from test_audit_anchoring import RPC, RPC_URL, TEST_KEY


@pytest.fixture
def observed(gateway):
    gateway.telemetry.close()
    exporter, reader = InMemorySpanExporter(), InMemoryMetricReader()
    telemetry = Telemetry(exporter=exporter, metric_reader=reader)
    gateway.telemetry = gateway.webhooks.telemetry = gateway.anchoring.telemetry = telemetry
    for client in (gateway.valio.client, gateway.decisions.client, gateway.webhooks.client):
        telemetry.instrument_client(client)
    return telemetry, exporter, reader


def metrics(reader):
    data = reader.get_metrics_data()
    return {m.name: m for resource in data.resource_metrics for scope in resource.scope_metrics for m in scope.metrics}


def test_ac_1_purchase_trace_has_required_spans_and_parent_order(gateway, need, upstream, observed, receiver, respx_mock):
    telemetry, exporter, _ = observed
    subscribe(gateway)
    gateway.anchoring.config = AnchorConfig(RPC_URL, "80002", TEST_KEY, "https://amoy.polygonscan.com/tx/")
    rpc = RPC(gateway, gateway.anchoring)
    respx_mock.post(RPC_URL).mock(side_effect=rpc.respond)
    with gateway.activate(), telemetry.span("purchase"):
        lookup = gateway.find_offers(need)
        quote = gateway.orders.create_quote(quote_request(need, qty=200, need_ref=lookup.lookup_id))
        order = gateway.orders.place_order(quote.id, "otel-workflow")
        assert order.status == "PENDING_APPROVAL"
        assert gateway.orders.approve(order.id, "Reviewer", evidence_revision=1).status == "CONFIRMED"
        gateway.anchoring.anchor_once(current_head=True)
    spans = exporter.get_finished_spans()
    names = {s.name for s in spans}
    required = {"catalog.search", "fit.classify", "supplier.price", "supplier.availability", "plan.build",
                "policy.evaluate", "quote.create", "order.place", "approval.review", "webhook.deliver", "audit.anchor"}
    assert required <= names
    by_id = {s.context.span_id: s for s in spans}
    def ancestor(s, name):
        while s.parent and s.parent.span_id in by_id:
            s = by_id[s.parent.span_id]
            if s.name == name:
                return True
        return False
    for s in spans:
        if s.name in {"catalog.search", "fit.classify", "plan.build"}:
            assert ancestor(s, "command.find_offers") and ancestor(s, "purchase")
        if s.name == "policy.evaluate":
            assert ancestor(s, "plan.build") or ancestor(s, "order.place") or ancestor(s, "approval.review")
        if s.name in {"supplier.price", "supplier.availability"}:
            assert ancestor(s, "purchase")
            assert any(ancestor(s, operation) for operation in ("command.find_offers", "quote.create", "order.place", "approval.review"))
        if s.name in required - {"webhook.deliver", "audit.anchor"}:
            assert s.context.trace_id == next(p for p in spans if p.name == "purchase").context.trace_id
    assert any(s.name == "audit.anchor" and ancestor(s, "purchase") for s in spans)
    assert any(s.name == "POST" for s in spans)  # httpx auto-instrumentation, not a synthetic stand-in.
    placement = next(s for s in spans if s.name == "order.place")
    deliveries = [s for s in spans if s.name == "webhook.deliver" and s.links]
    assert deliveries and all(s.parent is None for s in deliveries)
    assert all(s.links[0].context.span_id == placement.context.span_id for s in deliveries)


def test_ac_2_review_links_to_durable_placing_context(gateway, need, upstream, observed):
    _, exporter, _ = observed
    lookup = gateway.find_offers(need)
    quote = gateway.orders.create_quote(quote_request(need, qty=200, need_ref=lookup.lookup_id))
    order = gateway.orders.place_order(quote.id, "otel-linked-review")
    placement = next(s for s in exporter.get_finished_spans() if s.name == "order.place")
    stored = gateway.orders.get_order(order.id)
    assert stored.links["placement_span"]["span_id"] == format(placement.context.span_id, "016x")
    gateway.orders.approve(order.id, "Reviewer")
    review = next(s for s in exporter.get_finished_spans() if s.name == "approval.review")
    assert review.parent is None
    assert review.links[0].context.trace_id == placement.context.trace_id
    assert review.links[0].context.span_id == placement.context.span_id


def test_ac_3_allowlist_and_secret_redaction_on_auto_spans_and_metrics(gateway, need, upstream, observed):
    _, exporter, reader = observed
    secret = "test-super-private-prompt-and-password"
    request = need.model_copy(update={"intent": secret})
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        response = client.post("/v1/offers?private=" + secret, json=request.model_dump(mode="json"))
        assert response.status_code == 200
    spans = exporter.get_finished_spans()
    assert any(s.kind.name == "SERVER" for s in spans)
    for span in spans:
        assert set(span.attributes) <= ALLOWLIST
        assert not span.events and not span.resource.attributes
        assert secret not in json.dumps(dict(span.attributes)) and secret not in span.name
    for metric in metrics(reader).values():
        for point in metric.data.data_points:
            assert set(point.attributes) <= ALLOWLIST
            assert secret not in json.dumps(dict(point.attributes))


def test_ac_4_one_lookup_metrics_match_actual_calls_and_candidates(gateway, need, upstream, observed):
    _, _, reader = observed
    result = gateway.find_offers(need)
    data = metrics(reader)
    total = lambda name: sum(p.value for p in data[name].data.data_points)
    assert total("upstream.calls") == result.live_calls.price + result.live_calls.availability == 2
    assert total("candidates.screened") == result.screened_count
    assert total("candidates.priced") == result.priced_count
    gateway.find_offers(need)
    data = metrics(reader)
    assert total("upstream.calls") == 2 and total("cache.hits") == 2


def test_ac_5_exporter_failure_never_changes_success(gateway, need, upstream):
    from opentelemetry.sdk.trace.export import SpanExporter
    class Broken(SpanExporter):
        def export(self, spans):
            raise RuntimeError("Collector down")
    gateway.telemetry.close()
    gateway.telemetry = Telemetry(exporter=Broken())
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        lookup = client.post("/v1/offers", json=need.model_dump(mode="json"))
        assert lookup.status_code == 200
        quote = client.post("/v1/quotes", json=quote_request(need, qty=1, need_ref=lookup.json()["lookup_id"]).model_dump(mode="json"))
        order = client.post("/v1/orders", headers={"Idempotency-Key": "otel-broken"}, json={"quote_id": quote.json()["id"]})
    assert order.status_code == 200 and order.json()["status"] == "CONFIRMED"


def test_ac_6_no_otel_variables_no_providers_or_exporter_threads(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_CONSOLE", raising=False)
    before = set(threading.enumerate())
    telemetry = Telemetry()
    assert not telemetry.enabled and telemetry.provider is None and telemetry.meter_provider is None
    assert set(threading.enumerate()) == before
    with telemetry.activate(), telemetry.span("noop"):
        telemetry.metric("upstream.calls")
    telemetry.close()
    assert set(threading.enumerate()) == before


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_review_cached_review_records_one_wait(gateway, need, upstream, observed, decision):
    _, _, reader = observed
    lookup = gateway.find_offers(need)
    quote = gateway.orders.create_quote(quote_request(need, qty=200, need_ref=lookup.lookup_id))
    order = gateway.orders.place_order(quote.id, "wait-placement")
    operation = getattr(gateway.orders, decision)
    first = operation(order.id, "Human", command_key="one-review")
    count = lambda: sum(p.count for p in metrics(reader)["approval.wait"].data.data_points)
    assert count() == 1
    assert operation(order.id, "Human", command_key="one-review").status == first.status
    assert count() == 1


@pytest.mark.parametrize("operation", ["anchor", "verify"])
def test_review_anchor_rpc_client_spans(gateway, need, upstream, observed, respx_mock, operation):
    telemetry, exporter, _ = observed
    gateway.find_offers(need)
    gateway.anchoring.config = AnchorConfig(RPC_URL, "80002", TEST_KEY, "https://amoy.polygonscan.com/tx/")
    rpc = RPC(gateway, gateway.anchoring)
    respx_mock.post(RPC_URL).mock(side_effect=rpc.respond)
    rpc.mined = True
    exporter.clear()
    with gateway.activate():
        gateway.anchoring.anchor_once(current_head=True)
        if operation == "verify":
            exporter.clear()
            assert gateway.anchoring.verify("latest")["anchor_match"]
    assert any(s.kind.name == "CLIENT" for s in exporter.get_finished_spans())
