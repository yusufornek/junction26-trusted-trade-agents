from datetime import timedelta

import pytest
import httpx
from fastapi.testclient import TestClient

from app import audit
from app.api import create_app
from conftest import OPERATOR_HEADERS
from app.models import PlanRef, PolicyUpdateRequest, QuoteRequest
from conftest import quote_request


@pytest.fixture
def client(gateway):
    with TestClient(create_app(gateway), headers=OPERATOR_HEADERS) as client:
        yield client


def pending(gateway, need, upstream):
    upstream.model, upstream.amount = "Stock", 400
    lookup = gateway.find_offers(need, "agent:hotel-chain-agent")
    quote = gateway.orders.create_quote(quote_request(need, qty=400, need_ref=lookup.lookup_id),
                                         "hotel-chain-agent")
    order = gateway.orders.place_order(quote.id, "evidence", "hotel-chain-agent")
    assert order.status == "PENDING_APPROVAL" and order.evidence_revision == 1
    return order


def approve(client, order, revision=None):
    body = {"approver": "Ada", "comment": "Reviewed the evidence"}
    if revision is not None:
        body["evidence_revision"] = revision
    return client.post(f"/v1/approvals/{order.id}/approve", json=body)


def drop_stock(client, order, upstream):
    upstream.amount = 300
    response = approve(client, order, 1)
    assert response.status_code == 409
    assert response.json()["code"] == "EVIDENCE_CHANGED"
    return response.json()["data"]


def test_ac_1_unchanged_evidence_confirms_at_revision_1(client, gateway, need, upstream):
    order = pending(gateway, need, upstream)
    response = approve(client, order, 1)
    assert response.status_code == 200
    assert response.json()["status"] == "CONFIRMED"
    assert response.json()["evidence_revision"] == 1
    assert response.json()["total"] == order.total
    assert gateway.orders.get_order(order.id).approver == "Ada"
    assert not any(e.action == "EVIDENCE_CHANGED" for e in gateway.audit(order_id=order.id))


def test_ac_2_stock_drop_persists_pending_revision_and_audit(client, gateway, need, upstream):
    order = pending(gateway, need, upstream)
    data = drop_stock(client, order, upstream)
    assert data["evidence_revision"] == 2
    assert "1: covered 400 → 300 of 400" in data["changes"]
    assert "1: new warning PARTIAL_STOCK" in data["changes"]
    stored = gateway.orders.get_order(order.id)
    assert stored.status == "PENDING_APPROVAL" and stored.evidence_revision == 2
    assert stored.approver is None and stored.comment is None
    assert stored.lines[0].availability.available_amount == 300
    assert "PARTIAL_STOCK" in {w.code for w in stored.lines[0].warnings}
    assert "PARTIAL_STOCK" in {h.rule for h in stored.policy.hits}
    event = gateway.audit(order_id=order.id)[-1]
    assert event.action == "EVIDENCE_CHANGED" and event.actor == "human:operator"
    assert event.data["stated_name"] == "Ada"
    assert event.data["changes"] == data["changes"]
    assert event.data["evidence_revision"] == 2
    assert event.data["live_calls"] == {"price": 0, "availability": 1}
    with gateway.db.connect() as conn:
        assert conn.execute("SELECT quantity FROM reservations WHERE order_id=?", (order.id,)).fetchone()[0] == 400


def test_ac_3_second_approval_confirms_reviewed_revision(client, gateway, need, upstream):
    order = pending(gateway, need, upstream)
    drop_stock(client, order, upstream)
    response = approve(client, order, 2)
    assert response.status_code == 200
    assert response.json()["status"] == "CONFIRMED"
    assert response.json()["evidence_revision"] == 2
    assert "PARTIAL_STOCK" in {w["code"] for w in response.json()["lines"][0]["warnings"]}
    assert response.json()["total"] == order.total


def test_ac_4_stale_revision_refused_without_upstream_calls(client, gateway, need, upstream):
    order = pending(gateway, need, upstream)
    drop_stock(client, order, upstream)
    calls = (len(upstream.price_calls), len(upstream.availability_calls), len(upstream.decision_calls))
    response = approve(client, order, 1)
    assert response.status_code == 409 and response.json()["code"] == "EVIDENCE_CHANGED"
    assert response.json()["data"] == {"evidence_revision": 2, "changes": []}
    assert calls == (len(upstream.price_calls), len(upstream.availability_calls), len(upstream.decision_calls))
    assert gateway.orders.get_order(order.id).evidence_revision == 2
    assert gateway.orders.get_order(order.id).status == "PENDING_APPROVAL"
    assert gateway.audit(order_id=order.id)[-1].data["live_calls"] == {"price": 0, "availability": 0}


def test_ac_5_fetch_timestamp_changes_do_not_change_evidence(client, gateway, need, upstream, monkeypatch):
    order = pending(gateway, need, upstream)
    later = audit.utcnow() + timedelta(seconds=5)
    monkeypatch.setattr(audit, "utcnow", lambda: later)
    response = approve(client, order)
    assert response.status_code == 200 and response.json()["status"] == "CONFIRMED"
    stored = gateway.orders.get_order(order.id)
    assert stored.evidence_revision == 1
    assert stored.lines[0].provenance["availability"].fetched_at != order.lines[0].provenance["availability"].fetched_at


def test_ac_6_timeline_orders_evidence_change_before_approval(client, gateway, need, upstream):
    order = pending(gateway, need, upstream)
    data = drop_stock(client, order, upstream)
    assert approve(client, order, 2).status_code == 200
    response = client.get(f"/v1/orders/{order.id}/timeline")
    assert response.status_code == 200
    timeline = response.json()
    assert [s["action"] for s in timeline["steps"]] == [
        "LOOKUP", "CREATE_QUOTE", "PLACE_ORDER", "EVIDENCE_CHANGED", "APPROVE",
    ]
    changed = timeline["steps"][3]
    assert changed["step"] == "decision" and changed["data"]["changes"] == data["changes"]
    assert timeline["total_live_calls"] == {"price": 2, "availability": 5}
    assert timeline["total_decision_calls"] == 1


def test_policy_rule_change_requires_review_without_stated_revision(client, gateway, need, upstream):
    order = pending(gateway, need, upstream)
    policy = gateway.policy.get().model_dump(mode="json")
    policy["approval"]["min_fit_probability"] = 0.95
    gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Ada"))
    response = approve(client, order)
    assert response.status_code == 409
    assert response.json()["data"]["changes"] == ["Order: new policy rule min_fit_probability"]
    assert gateway.orders.get_order(order.id).policy != order.policy
    assert approve(client, order).status_code == 200


def test_warning_and_policy_text_are_not_decision_changes(client, gateway, need, upstream):
    order = pending(gateway, need, upstream)
    drop_stock(client, order, upstream)
    # Change rule detail while keeping exactly the same rule names.
    policy = gateway.policy.get().model_dump(mode="json")
    policy["approval"]["max_auto_total_eur"] = 1000
    gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Ada"))
    with gateway.db.connect(write=True) as conn:
        stored = gateway.orders._order(conn, order.id)
        for warning in stored.lines[0].warnings:
            warning.text = "Different explanatory wording"
        gateway.orders._save_order(conn, stored)
    assert approve(client, order, 2).status_code == 200


@pytest.mark.parametrize("shift", [-1, 1])
def test_delivery_date_change_requires_new_review(client, gateway, need, upstream, shift):
    order = pending(gateway, need, upstream)
    upstream.shift = shift
    response = approve(client, order)
    assert response.status_code == 409 and response.json()["code"] == "EVIDENCE_CHANGED"
    expected = f"1: delivery date {need.deliver_by} → {need.deliver_by + timedelta(days=shift)}"
    assert expected in response.json()["data"]["changes"]
    assert gateway.orders.get_order(order.id).evidence_revision == 2


def test_order_level_plan_warnings_survive_placement_and_approval(client, gateway, need, upstream):
    upstream.model, upstream.amount = "Stock", 1
    lookup = gateway.find_offers(need, "agent:hotel-chain-agent")
    plan_index = next(i for i, plan in enumerate(lookup.plans) if plan.kind == "partial")
    quote = gateway.orders.create_quote(QuoteRequest(
        customer_id=need.customer_id, deliver_by=need.deliver_by,
        plan_ref=PlanRef(lookup_id=lookup.lookup_id, plan_index=plan_index)), "hotel-chain-agent")
    order = gateway.orders.place_order(quote.id, "partial-plan", "hotel-chain-agent")
    assert order.status == "PENDING_APPROVAL" and order.warnings == quote.warnings
    assert "PARTIAL_STOCK" in {w.code for w in order.warnings}
    response = approve(client, order, 1)
    assert response.status_code == 200
    assert response.json()["warnings"] == [w.model_dump() for w in order.warnings]
    assert response.json()["evidence_revision"] == 1


def test_excess_stock_change_keeps_fully_covered_revision(client, gateway, need, upstream):
    order = pending(gateway, need, upstream)
    upstream.amount = 500
    response = approve(client, order)
    assert response.status_code == 200 and response.json()["evidence_revision"] == 1


@pytest.mark.parametrize("failure", ["sales_block", "unverifiable", "unknown_product", "availability_error"])
def test_hard_failures_do_not_persist_or_confirm_new_evidence(client, gateway, need, upstream, respx_mock, failure):
    order = pending(gateway, need, upstream)
    if failure == "sales_block":
        upstream.sales_block = True
        expected_status, code = 409, "HARD_CONSTRAINT_VIOLATION"
    elif failure == "unknown_product":
        upstream.unknown = True
        expected_status, code = 409, "HARD_CONSTRAINT_VIOLATION"
    elif failure == "availability_error":
        def availability_error(request):
            body = upstream.availability_response(request).json()
            for product in body["products"]:
                product["error"] = True
            return httpx.Response(200, json=body)
        respx_mock.post("https://valio.test/api/availability").mock(side_effect=availability_error)
        expected_status, code = 502, "AVAILABILITY_ERROR"
    else:
        respx_mock.post("https://valio.test/api/availability").respond(503)
        expected_status, code = 502, "VALIO_UNAVAILABLE"
    response = approve(client, order, 1)
    assert response.status_code == expected_status and response.json()["code"] == code
    assert gateway.orders.get_order(order.id) == order
    assert not any(e.action == "EVIDENCE_CHANGED" for e in gateway.audit(order_id=order.id))
