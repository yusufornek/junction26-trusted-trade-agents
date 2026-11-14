"""Spec 34 AC-7: compare real REST, MCP and SDK A2A procurement results."""
import asyncio

from a2a import types as t

from app.mcp_server import create_server
from conftest import OPERATOR_HEADERS
from test_a2a_service import artifact_data, command, lookup, sdk


def plan_for(offers):
    return next(i for i, plan in enumerate(offers["plans"]) if
                len(plan["lines"]) == 1 and plan["lines"][0]["product_id"] == "1")


def snapshot(gateway, order, before):
    with gateway.db.connect() as conn:
        reserved = conn.execute("SELECT COALESCE(SUM(quantity),0) FROM reservations WHERE product_id='1'").fetchone()[0]
        audit = conn.execute("SELECT action,actor,summary FROM audit WHERE order_id=? OR quote_id=? ORDER BY id",
                             (order["id"], order["quote_id"])).fetchall()
        events = conn.execute("SELECT type,aggregate_revision FROM events WHERE aggregate_type='order' AND aggregate_id=? ORDER BY aggregate_revision", (order["id"],)).fetchall()
    return {"total": order["total"], "currency": order["currency"], "status": order["status"],
            "policy": order["policy"], "stock_delta": reserved - before,
            "lines": [(l["product_id"], l["units_needed"], l["unit_price"]) for l in order["lines"]],
            "audit": [tuple(row) for row in audit], "events": [tuple(row) for row in events]}


def test_ac_7_rest_mcp_a2a_equal_purchase(gateway, need, upstream):
    upstream.model, upstream.amount = "Stock", 2000
    results = []
    async def run():
        async with sdk(gateway) as (a2a, http, _):
            mcp = create_server(gateway, "external")
            for interface in ("rest", "mcp", "a2a"):
                with gateway.db.connect() as conn:
                    before = conn.execute("SELECT COALESCE(SUM(quantity),0) FROM reservations WHERE product_id='1'").fetchone()[0]
                if interface == "rest":
                    response = await http.post("/v1/offers", json=need.model_dump(mode="json"))
                    assert response.status_code == 200, response.text
                    offers = response.json()
                    response = await http.post("/v1/quotes", json={"customer_id": "1", "deliver_by": str(need.deliver_by),
                        "plan_ref": {"lookup_id": offers["lookup_id"], "plan_index": plan_for(offers)}})
                    assert response.status_code == 200, response.text
                    response = await http.post("/v1/orders", json={"quote_id": response.json()["id"]}, headers={"Idempotency-Key": "rest-purchase"})
                    assert response.status_code == 200, response.text
                    order = response.json()
                elif interface == "mcp":
                    offers = (await mcp.call_tool("find_offers", {"need": need.model_dump(mode="json")})).structured_content
                    quote = (await mcp.call_tool("create_quote", {"customer_id": "1", "deliver_by": str(need.deliver_by),
                        "plan_ref": {"lookup_id": offers["lookup_id"], "plan_index": plan_for(offers)}})).structured_content
                    result = await mcp.call_tool("place_order", {"quote_id": quote["id"], "idempotency_key": "mcp-purchase"})
                    assert not result.is_error
                    order = result.structured_content
                else:
                    task = await lookup(a2a, need, "a2a-lookup")
                    offers = artifact_data(task, "offers")
                    task = (await a2a.send_message(command("select_plan", "a2a-select", task, plan_index=plan_for(offers)))).task
                    task = (await a2a.send_message(command("place_order", "a2a-purchase", task))).task
                    assert task.status.state == t.TaskState.TASK_STATE_AUTH_REQUIRED
                    order = artifact_data(task, "order")
                assert order["status"] == "PENDING_APPROVAL" and order["total"] == 2600
                results.append(snapshot(gateway, order, before))
                # All three use exactly the same authenticated, revision-bound review command.
                approved = await http.post(f"/v1/approvals/{order['id']}/approve", headers=OPERATOR_HEADERS,
                    json={"approver": "Ada", "evidence_revision": order["evidence_revision"]})
                assert approved.status_code == 200, approved.text
                if interface == "a2a":
                    assert (await a2a.get_task(t.GetTaskRequest(id=task.id))).status.state == t.TaskState.TASK_STATE_COMPLETED
                results.append(snapshot(gateway, approved.json(), before))
    asyncio.run(run())
    assert results[0] == results[2] == results[4]
    assert results[1] == results[3] == results[5]
    assert results[0]["stock_delta"] == 200
    assert [a[0] for a in results[1]["audit"]] == ["CREATE_QUOTE", "PLACE_ORDER", "APPROVE"]
