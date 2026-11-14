"""REST pause/resume parity with the recurring MCP service and authority checks."""
from fastapi.testclient import TestClient

from app.api import create_app
from app.auth import Principal
from app.models import PolicyUpdateRequest
from conftest import OPERATOR_HEADERS, agent_headers
from test_recurring_drift import create, product_rows, recurring_setup  # noqa: F401


def assert_permissions(client, gateway, row, path, key, status):
    assert client.post(path, headers={"Authorization": ""}).status_code == 401
    assert client.post(path, headers=agent_headers("other")).status_code == 404
    assert client.post(path, headers=OPERATOR_HEADERS).status_code == 404
    assert client.post(path, headers=agent_headers("buyer")).status_code == 403
    assert client.post(path.replace(row["id"], "missing")).status_code == 404
    policy = gateway.policy._editable(gateway.policy.get())
    policy["agents"]["external"]["recurring"] = False
    gateway.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Withdraw recurring permission"))
    for headers in ({}, {"Idempotency-Key": key}):
        denied = client.post(path, headers=headers)
        assert denied.status_code == 403, denied.text
        assert denied.json()["code"] == "RECURRING_NOT_AUTHORIZED"
    assert gateway.recurring.get(row["id"])["status"] == status


def test_ac_1_pause_rest_stops_runs_and_rechecks_authority(gateway, recurring_setup):
    row = create(gateway, recurring_setup)
    path = f"/v1/recurring-orders/{row['id']}/pause"
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        response = client.post(path, headers={"Idempotency-Key": "pause-once"})
        assert response.status_code == 200, response.text
        assert response.json() == {**row, "status": "PAUSED"}
        assert client.post(path, headers={"Idempotency-Key": "pause-once"}).json() == response.json()
        stopped = client.post(f"/v1/recurring-orders/{row['id']}/run-now")
        assert stopped.status_code == 409, stopped.text
        assert stopped.json()["code"] == "INVALID_TRANSITION"
        assert gateway.recurring.runs(row["id"]) == []
        with gateway.db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM audit WHERE action='RECURRING_STOPPED'").fetchone()[0] == 1
        assert_permissions(client, gateway, row, path, "pause-once", "PAUSED")


def test_ac_2_resume_rest_restores_schedule_and_rechecks_authority(gateway, recurring_setup):
    row = create(gateway, recurring_setup)
    gateway.recurring.pause(row["id"], Principal("external", customer_ids=("1",)))
    path = f"/v1/recurring-orders/{row['id']}/resume"
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        response = client.post(path, headers={"Idempotency-Key": "resume-once"})
        assert response.status_code == 200, response.text
        assert response.json() == row
        assert gateway.recurring.get(row["id"])["status"] == "ACTIVE"
        assert client.post(path, headers={"Idempotency-Key": "resume-once"}).json() == response.json()
        assert gateway.recurring.runs(row["id"]) == []
        with gateway.db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM audit WHERE action='RECURRING_RESUMED'").fetchone()[0] == 1
        run = client.post(f"/v1/recurring-orders/{row['id']}/run-now")
        assert run.status_code == 200, run.text
        assert run.json()["status"] == "CONFIRMED"
        assert len(gateway.recurring.runs(row["id"])) == 1
        assert_permissions(client, gateway, row, path, "resume-once", "ACTIVE")
