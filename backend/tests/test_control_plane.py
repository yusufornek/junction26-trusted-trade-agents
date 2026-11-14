from collections import Counter
from copy import deepcopy
from datetime import timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import audit
from app.api import create_app
from conftest import OPERATOR_HEADERS
from app.lookup import WARNING_TEXT
from app.models import PolicyDocument
from app.service import Gateway
from conftest import quote_request


def test_review_historical_lookup_stats(gateway, need, upstream, monkeypatch):
    from datetime import datetime
    import app.models as models
    lookup = gateway.find_offers(need)
    class FutureDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.combine(need.deliver_by + timedelta(days=1), datetime.min.time(), tzinfo=tz)
    monkeypatch.setattr(models, "datetime", FutureDateTime)
    with TestClient(create_app(gateway), headers=OPERATOR_HEADERS) as client:
        assert client.get(f"/v1/lookups/{lookup.lookup_id}").status_code == 200
        response = client.get("/v1/stats")
        assert response.status_code == 200
        assert response.json()["lookups"] == 1


def test_review_current_policy_edits_survive_reload(gateway):
    from app.policy import Policy
    from app.lookup import warning
    with TestClient(create_app(gateway), headers=OPERATOR_HEADERS) as client:
        body = client.get("/v1/policy").json()
        body["reject"]["on"].remove("NO_STOCK")
        body["approval"]["warnings_need_approval"] = ["NO_STOCK"]
        response = client.put("/v1/policy", json={"policy": body, "updated_by": "Ada"})
        assert response.status_code == 200
        assert response.json()["reject"]["on"] == body["reject"]["on"]
        assert response.json()["approval"]["warnings_need_approval"] == ["NO_STOCK"]
        assert client.get("/v1/policy").json() == response.json()
    reloaded = Policy(gateway.policy.path, gateway.policy.runtime_path, gateway.db).get()
    assert "NO_STOCK" not in reloaded.reject.on
    assert gateway.policy.evaluate(0, [], warnings=[warning("NO_STOCK")]).outcome == "PENDING_APPROVAL"
    diff = gateway.audit()[-1].data["diff"]
    assert "NO_STOCK" not in diff["reject.on"]["after"]


def editable(resource):
    return deepcopy({key: resource[key] for key in PolicyDocument.model_fields})


def test_lookup_fetch_and_not_found(gateway, need, upstream):
    with TestClient(create_app(gateway), headers=OPERATOR_HEADERS) as client:
        result = client.post("/v1/offers", json=need.model_dump(mode="json")).json()
        calls = len(upstream.price_calls), len(upstream.availability_calls), len(upstream.decision_calls)
        assert client.get(f"/v1/lookups/{result['lookup_id']}").json() == result
        missing = client.get("/v1/lookups/missing")
        assert missing.status_code == 404
        assert {k: missing.json()[k] for k in ("code", "message")} == {"code": "LOOKUP_NOT_FOUND", "message": "Lookup was not found"}
        assert calls == (len(upstream.price_calls), len(upstream.availability_calls), len(upstream.decision_calls))


@pytest.mark.parametrize("registered_agents", [("external",)], indirect=True)
def test_policy_update_reset_audit_and_reload(gateway, need, upstream):
    defaults_bytes = gateway.settings.policy_path.read_bytes()
    with TestClient(create_app(gateway), headers=OPERATOR_HEADERS) as client:
        defaults = client.get("/v1/policy").json()
        assert defaults["approval"]["max_auto_total_eur"] == 1500
        assert defaults["shortlist"]["catalog_pool"] == 40
        assert defaults["known_warning_codes"] == sorted(WARNING_TEXT)
        assert defaults["updated_at"] is None and defaults["updated_by"] is None
        lookup = gateway.find_offers(need)
        request = quote_request(need, qty=1, need_ref=lookup.lookup_id)
        first_quote = gateway.orders.create_quote(request)
        assert gateway.orders.place_order(first_quote.id, "before").status == "CONFIRMED"
        # The next quote exists before the update: placement must use the current policy.
        second_quote = gateway.orders.create_quote(request)
        peer = Gateway(gateway.settings)
        try:
            changed = {**defaults, "approval": {**defaults["approval"], "max_auto_total_eur": 10}}
            response = client.put("/v1/policy", json={"policy": changed, "updated_by": " Ada ", "comment": "Lower budget"})
            assert response.status_code == 200, response.text
            resource = response.json()
            assert resource["updated_by"] == "Ada" and resource["updated_at"]
            assert peer.policy.get().approval.max_auto_total_eur == 10
            assert gateway.orders.place_order(second_quote.id, "after").status == "PENDING_APPROVAL"
            event = [e for e in gateway.audit() if e.action == "POLICY_UPDATED"][-1]
            assert event.actor == "human:operator"
            assert len(event.data["trace_id"]) == 32
            assert event.data["version_id"].startswith("pol_")
            assert {k: v for k, v in event.data.items() if k not in {"correlation_id", "trace_id", "version_id"}} == {"diff": {"approval.max_auto_total_eur": {"before": 1500, "after": 10}},
                                  "comment": "Lower budget", "reset": False, "stated_name": "Ada"}
            assert gateway.policy.runtime_path == gateway.settings.db_path.parent / "policy.yaml"
            assert gateway.policy.runtime_path.is_file()
            reloaded = Gateway(gateway.settings)
            try:
                assert reloaded.policy.get().model_dump(mode="json") == resource
            finally:
                reloaded.close()
            reset = client.post("/v1/policy/reset", json={"updated_by": "Grace"})
            assert reset.status_code == 200, reset.text
            assert editable(reset.json()) == editable(defaults)
            assert reset.json()["updated_by"] == "Grace"
            assert peer.policy.get().approval.max_auto_total_eur == 1500
            reset_event = [e for e in gateway.audit() if e.action == "POLICY_UPDATED"][-1]
            assert reset_event.actor == "human:operator"
            assert reset_event.data["stated_name"] == "Grace"
            assert reset_event.data["diff"] == {"approval.max_auto_total_eur": {"before": 10, "after": 1500}}
            assert reset_event.data["reset"] is True
            assert gateway.orders.place_order(gateway.orders.create_quote(request).id, "reset").status == "CONFIRMED"
        finally:
            peer.close()
        assert gateway.settings.policy_path.read_bytes() == defaults_bytes


@pytest.mark.parametrize("section,field,value,message", [
    ("approval", "max_auto_total_eur", -1, "greater than or equal to 0"),
    ("approval", "min_fit_probability", -0.1, "greater than or equal to 0"),
    ("approval", "min_fit_probability", 1.1, "less than or equal to 1"),
    ("ranking_weights", "price", -1, "greater than or equal to 0"),
    ("shortlist", "catalog_pool", 9, "greater than or equal to 10"),
    ("shortlist", "catalog_pool", 101, "less than or equal to 100"),
    ("shortlist", "catalog_pool", 10.5, "valid integer"),
    ("approval", "warnings_need_approval", ["TYPO"], "Unknown warning codes: TYPO"),
    ("reject", "on", ["TYPO"], "Unknown warning codes: TYPO"),
])
def test_policy_validation_no_side_effects(gateway, section, field, value, message):
    with TestClient(create_app(gateway), headers=OPERATOR_HEADERS) as client:
        before = client.get("/v1/policy").json()
        body = editable(before)
        body[section][field] = value
        response = client.put("/v1/policy", json={"policy": body, "updated_by": "Ada"})
        assert response.status_code == 422
        assert response.json()["code"] == "VALIDATION_ERROR"
        assert message in response.json()["message"]
        assert client.get("/v1/policy").json() == before
        assert gateway.audit() == []
        assert not gateway.policy.runtime_path.exists()


def test_policy_zero_weights_and_blank_human(gateway):
    with TestClient(create_app(gateway), headers=OPERATOR_HEADERS) as client:
        body = editable(client.get("/v1/policy").json())
        for key in body["ranking_weights"]:
            body["ranking_weights"][key] = 0
        response = client.put("/v1/policy", json={"policy": body, "updated_by": "Ada"})
        assert response.status_code == 422
        assert "sum greater than zero" in response.json()["message"]
        body["ranking_weights"]["price"] = 1
        assert client.put("/v1/policy", json={"policy": body, "updated_by": " "}).status_code == 422
        assert client.post("/v1/policy/reset", json={"updated_by": " "}).status_code == 422
        body["approval"]["max_auto_total_eur"] = "NaN"
        assert client.put("/v1/policy", json={"policy": body, "updated_by": "Ada"}).status_code == 422
        assert gateway.audit() == []


def test_policy_applies_shortlist_and_ranking(gateway, need, upstream, monkeypatch):
    with TestClient(create_app(gateway), headers=OPERATOR_HEADERS) as client:
        body = editable(client.get("/v1/policy").json())
        body["shortlist"]["catalog_pool"] = 10
        body["ranking_weights"] = {key: int(key == "reliability") for key in body["ranking_weights"]}
        assert client.put("/v1/policy", json={"policy": body, "updated_by": "Ada"}).status_code == 200
    assert gateway.policy.catalog_pool == 10
    original_search = gateway.catalog.search
    searches = []

    def search(*args, **kwargs):
        searches.append((args, kwargs))
        return original_search(*args, **kwargs)

    monkeypatch.setattr(gateway.catalog, "search", search)
    result = gateway.find_offers(need)
    assert searches[0][0][-1] == searches[0][1]["max_limit"] == 10
    assert result.offers[0].product_id == "1"
    assert result.offers[0].score == .97
    assert all(o.score == .97 for o in result.offers[1:])  # Missing reliability uses the global median.


@pytest.mark.parametrize("registered_agents", [("hotel", "observer")], indirect=True)
def test_stats_small_flow_and_since(gateway, need, upstream):
    with TestClient(create_app(gateway), headers=OPERATOR_HEADERS) as client:
        empty = client.get("/v1/stats").json()
        assert empty["lookups"] == 0
        assert empty["average_live_calls_per_lookup"] == {"price": 0, "availability": 0}
        assert empty["agents_seen"] == []
        result = gateway.find_offers(need, "agent:hotel")
        first = gateway.orders.create_quote(quote_request(need, qty=1, need_ref=result.lookup_id), "hotel")
        gateway.orders.place_order(first.id, "auto", "hotel")
        second = gateway.orders.create_quote(quote_request(need, qty=200, need_ref=result.lookup_id), "hotel")
        pending = gateway.orders.place_order(second.id, "pending", "hotel")
        gateway.orders.place_order(second.id, "pending", "hotel")  # No extra placement.
        decision_time = audit.utcnow()
        gateway.orders.approve(pending.id, "Ada")
        # Seen agents need not have placed an order. Cached calls still count as zero actual calls.
        second_lookup = gateway.find_offers(need, "agent:observer")
        # Revoke after lookup: the global view still reports historical, now-unregistered identities.
        from app.models import PolicyUpdateRequest
        body = gateway.policy.get().model_dump(mode="json")
        del body["agents"]["observer"]
        gateway.policy.update(PolicyUpdateRequest(policy=body, updated_by="Fixture"))
        calls = len(upstream.price_calls), len(upstream.availability_calls), len(upstream.decision_calls)
        stats = client.get("/v1/stats").json()
        assert stats["lookups"] == 2
        assert stats["orders_by_status"] == {"CONFIRMED": 2, "PENDING_APPROVAL": 0, "REJECTED": 0, "EXPIRED": 0, "CANCELLED": 0}
        assert stats["placement_outcomes"] == {"CONFIRMED": 1, "PENDING_APPROVAL": 1, "REJECTED": 0}
        assert stats["human_decisions"] == {"approved": 1, "rejected": 0}
        assert stats["confirmed_spend_total_eur"] == 2613
        assert stats["average_live_calls_per_lookup"] == {"price": 0.5, "availability": 0.5}
        assert stats["total_decisions_calls"] == len(upstream.decision_calls) == 2
        warning_counts = Counter(w.code for lookup in [result, second_lookup] for o in lookup.offers for w in o.warnings)
        assert stats["top_warning_codes"] == [{"code": k, "count": v} for k, v in sorted(warning_counts.items(), key=lambda x: (-x[1], x[0]))[:5]]
        assert stats["top_rule_hits"] == [{"rule": "max_auto_total_eur", "count": 1}]
        assert stats["agents_seen"] == [{"actor": "agent:hotel", "order_count": 2, "registered": True},
                                        {"actor": "agent:observer", "order_count": 0, "registered": False}]
        assert calls == (len(upstream.price_calls), len(upstream.availability_calls), len(upstream.decision_calls))
        window = client.get("/v1/stats", params={"since": decision_time.isoformat()}).json()
        assert window["human_decisions"] == {"approved": 1, "rejected": 0}
        assert sum(window["orders_by_status"].values()) == 0
        assert sum(window["placement_outcomes"].values()) == 0
        assert window["confirmed_spend_total_eur"] == 0
        assert window["lookups"] == 1
        future = (audit.utcnow() + timedelta(days=1)).isoformat()
        assert client.get("/v1/stats", params={"since": future}).json() == {**empty, "since": future.replace("+00:00", "Z")}
        assert client.get("/v1/stats", params={"since": "bad"}).status_code == 422


@pytest.mark.parametrize("registered_agents", [("external",)], indirect=True)
def test_stats_rejections_legacy_placements_and_timezone(gateway, need, upstream):
    result = gateway.find_offers(need)
    first = gateway.orders.create_quote(quote_request(need, qty=200, need_ref=result.lookup_id))
    pending = gateway.orders.place_order(first.id, "human-reject")
    gateway.orders.reject(pending.id, "Ada")
    upstream.sales_block = True
    second = gateway.orders.create_quote(quote_request(need, qty=1, need_ref=result.lookup_id))
    rejected = gateway.orders.place_order(second.id, "policy-reject")
    assert rejected.status == "REJECTED"
    # Existing audit events do not have the new explicit outcome field.
    with gateway.db.connect(write=True) as conn:
        audit.append(conn, "agent:legacy", "PLACE_ORDER", "Order CONFIRMED at the locked total of 13.00 EUR.",
                     data={"rule_hits": []})
    with TestClient(create_app(gateway), headers=OPERATOR_HEADERS) as client:
        stats = client.get("/v1/stats").json()
        assert stats["orders_by_status"]["REJECTED"] == 2
        assert stats["placement_outcomes"] == {"CONFIRMED": 1, "PENDING_APPROVAL": 1, "REJECTED": 1}
        assert stats["human_decisions"] == {"approved": 0, "rejected": 1}
        assert stats["confirmed_spend_total_eur"] == 0
        assert {x["rule"] for x in stats["top_rule_hits"]} >= {"SALES_BLOCK", "max_auto_total_eur"}
        bound = audit.utcnow() - timedelta(days=1)
        aware = bound.astimezone(timezone(timedelta(hours=2))).isoformat()
        assert client.get("/v1/stats", params={"since": aware}).json() == {**stats, "since": bound.isoformat().replace("+00:00", "Z")}
        naive = bound.replace(tzinfo=None).isoformat()
        assert client.get("/v1/stats", params={"since": naive}).json() == client.get("/v1/stats", params={"since": aware}).json()


@pytest.mark.parametrize("registered_agents", [("external",)], indirect=True)
def test_stats_top_five_warnings_and_rules(gateway, need, upstream, respx_mock):
    upstream.shift = 1
    upstream.sales_block = True
    upstream.cutoff = audit.utcnow() - timedelta(minutes=1)
    upstream.price_error = True
    respx_mock.post("https://api.openai.com/v1/decisions").respond(503)
    lookup = gateway.find_offers(need)
    warnings = Counter(w.code for o in lookup.offers for w in o.warnings)
    assert len(warnings) > 5
    # Valid prices still allow a quote for an order with several independent rule hits.
    upstream.price_error = False
    quote = gateway.orders.create_quote(quote_request(need, qty=200, need_ref=lookup.lookup_id))
    order = gateway.orders.place_order(quote.id, "many-rules")
    assert len(order.policy.hits) > 5
    with TestClient(create_app(gateway), headers=OPERATOR_HEADERS) as client:
        stats = client.get("/v1/stats").json()
    assert stats["top_warning_codes"] == [{"code": k, "count": v} for k, v in sorted(warnings.items(), key=lambda x: (-x[1], x[0]))[:5]]
    hits = Counter(h.rule for h in order.policy.hits)
    assert stats["top_rule_hits"] == [{"rule": k, "count": v} for k, v in sorted(hits.items(), key=lambda x: (-x[1], x[0]))[:5]]


def test_policy_persistence_failure_rolls_back_audit(gateway, monkeypatch):
    with TestClient(create_app(gateway), headers=OPERATOR_HEADERS) as client:
        before = client.get("/v1/policy").json()
        body = editable(before)
        body["approval"]["max_auto_total_eur"] = 10

        def fail_write(content):
            raise OSError("Simulated disk failure")

        monkeypatch.setattr(gateway.policy, "_replace", fail_write)
        with pytest.raises(OSError, match="Simulated disk failure"):
            client.put("/v1/policy", json={"policy": body, "updated_by": "Ada"})
        assert client.get("/v1/policy").json() == before
        assert gateway.audit() == []
