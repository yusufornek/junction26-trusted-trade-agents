"""Observed sandbox challenges, signed graduation, and governed overrides (63)."""
import asyncio
import json
import math
import tempfile
import threading
from datetime import timedelta
from pathlib import Path
from uuid import uuid4
from cryptography.exceptions import InvalidSignature

from . import audit
from .connection_contract import DEMO_AGENTS, manager
from .db import DomainError
from .foundation import PRINCIPAL

CHECKS = ("unit", "expired_quote", "retry", "unknown_argument", "approval", "elicitation")
REQUIRED = ("unit", "retry", "approval")


def driving(gateway):
    if not hasattr(gateway, "connection_contract_driving"):
        gateway.connection_contract_driving = DrivingTests(gateway)
    return gateway.connection_contract_driving


class DrivingTests:
    def __init__(self, gateway):
        self.gateway = gateway
        self.sandboxes = {}
        self.servers = {}
        self.lock = threading.RLock()
        self.step_lock = asyncio.Lock()

    def connection(self, p, *, allow_inactive=False):
        cid = p.connection_id
        if not cid:
            raise DomainError("CONNECTION_REQUIRED", "Use an issued connection credential", 403)
        with self.gateway.db.connect() as conn:
            row = conn.execute("SELECT * FROM connections WHERE connection_id=? AND agent_id=?", (cid, p.id)).fetchone()
        if not row or row["audience"] != "buyer" or not allow_inactive and (row["revoked_at"] or row["expires_at"] and row["expires_at"] <= audit.utcnow().isoformat()):
            raise DomainError("CONNECTION_NOT_FOUND", "Active buyer connection was not found", 404)
        return dict(row)

    def save(self, run):
        with self.gateway.db.connect(write=True, notify=False) as conn:
            conn.execute("INSERT OR REPLACE INTO connection_contract_runs VALUES(?,?,?)", (run["id"], run["connection_id"], json.dumps(run)))

    def load(self, rid, p):
        row = self.connection(p)
        with self.gateway.db.connect() as conn:
            run = conn.execute("SELECT data FROM connection_contract_runs WHERE id=? AND connection_id=?", (rid, row["connection_id"])).fetchone()
        if not run:
            raise DomainError("DRIVING_TEST_NOT_FOUND", "Driving test was not found", 404)
        value = json.loads(run[0])
        if value["client"] != self.client(row) or value["agent_id"] != p.id:
            raise DomainError("DRIVING_TEST_REQUIRED", "Driving result belongs to another client", 403,
                {"checks": list(REQUIRED), "tool": "start_driving_test"})
        if any(c["material"] for c in manager(self.gateway).changes_since(value["contract_version"])):
            raise DomainError("CONTRACT_STALE", "Driving challenge expired after a material contract change", 409,
                {"changes": manager(self.gateway).changes_since(value["contract_version"])})
        return value

    def client(self, row):
        return manager(self.gateway).state(row["connection_id"])["client"] or row["client"]

    def start(self, p):
        row = self.connection(p)
        m = manager(self.gateway)
        m.enforce(p)
        run = {"id": "drive_" + uuid4().hex, "connection_id": row["connection_id"], "client": self.client(row),
            "agent_id": p.id, "contract_version": m.current()["version"], "environment": "sandbox",
            "started_at": audit.utcnow().isoformat(), "checks": {k: {"status": "not_attempted"} for k in CHECKS},
            "sandbox_path": tempfile.mkdtemp(prefix="luotsi-driving-"), "finished": False}
        self.save(run)
        return self.view(run)

    def view(self, run):
        return {k: v for k, v in run.items() if k != "sandbox_path"} | {"instructions": {
            "unit": "Call describe, then create_quote for the stated base quantity. A case is not a carton.",
            "expired_quote": "Call describe, place_order on the expired quote, then follow REFRESH_QUOTE with create_quote.",
            "retry": "Call describe, place_order with the supplied intent_id; if response is dropped retry exactly the same call.",
            "unknown_argument": "Call create_quote with an invented field, inspect UNKNOWN_ARGUMENT/FIX_REQUEST.",
            "approval": "Call describe, place_order, then get_order_status and WAIT_FOR_APPROVAL without confirming the purchase.",
            "elicitation": "Call probe to answer a typed SDK elicitation; timeout retains declared status."},
            "step_tool": "driving_test_step", "finish_tool": "finish_driving_test"}

    def sandbox(self, run, check):
        key = (run["id"], check)
        if key in self.sandboxes:
            return self.sandboxes[key]
        from .auth import Credentials
        from .config import Settings
        from .service import Gateway
        from .sandbox_workspace import FixtureSupplierAdapter
        from .suppliers import SupplierCatalog
        path = Path(run["sandbox_path"]) / check
        keys = Credentials("sandbox-driving-admin", "driving-agent:sandbox-driving-key", environment="sandbox")
        g = Gateway(Settings(db_path=path / "app.db", environment="sandbox", keys=keys))
        # This adapter adds a declared case conversion to the ordinary fixture
        # evidence. All purchase/policy/signing/idempotency code remains shared.
        class CaseFixture(FixtureSupplierAdapter):
            def ingest_catalog(self):
                # Called only after the stock fixture products exist.
                return None

            def _fetch(self, endpoint, customer, deliver_by, ids, counter, *, fresh=False):
                values = super()._fetch(endpoint, customer, deliver_by, ids, counter, fresh=fresh)
                for sku, value in values.items():
                    product = self.catalog.get(sku)
                    if endpoint == "availability":
                        for line in value["scheduleLines"]:
                            line["salesUnit"] = product.sales_unit
                    elif endpoint == "price":
                        factor = product.units.get(product.sales_unit, 1)
                        value["comparisonPrice"]["value"] = value["catalogPrice"]["value"] / (product.base_volume_l * factor)
                return values
        adapter = CaseFixture(g.sandbox)
        products = [g.catalog.get(sku) for sku in ("1", "2", "3")]
        products[0] = products[0].model_copy(update={"sales_unit": "KI", "units": {"KI": 12, "ST": 1},
            "name_en": "Synthetic milk 12 x 1 L cartons per case"})
        SupplierCatalog(g.db, "valio").ingest(products)
        g.suppliers.adapters["valio"] = adapter
        g.catalog = adapter.catalog
        g.lookup.catalog = g.orders.catalog = g.catalog
        self.sandboxes[key] = g
        return g

    def prepare(self, run, check, g):
        state = run["checks"][check]
        if "challenge" in state:
            return state["challenge"]
        from .models import NeedSpec, QuoteRequest
        with g.activate():
            need = NeedSpec(customer_id="sandbox-customer", intent="milk", search_terms=["milk"],
                required_claims=["FREE_FROM_LACTOSE"], quantity={"value": 12, "unit": "L"},
                deliver_by=g.sandbox.now().date() + timedelta(days=3))
            lookup = g.find_offers(need, actor="agent:driving-agent")
            product = g.catalog.get("1")
            contents = product.base_volume_l * product.units[product.sales_unit]
            expected = math.ceil(need.quantity.value / contents)
            request = QuoteRequest(customer_id=need.customer_id, deliver_by=need.deliver_by,
                need_ref=lookup.lookup_id, lines=[{"product_id": product.id, "quantity_sales_units": expected}])
            quote = g.orders.create_quote(request, "driving-agent")
            if check == "expired_quote":
                g.sandbox.advance((quote.expires_at - g.sandbox.now()).total_seconds() + 1)
            challenge = {"need": need.model_dump(mode="json"), "quote_arguments": request.model_dump(mode="json", exclude_none=True),
                "quote_id": quote.id, "intent_id": "intent_" + uuid4().hex, "product_id": product.id,
                "base_unit": "carton", "base_units_requested": need.quantity.value,
                "base_units_per_sale_unit": contents, "sale_unit": product.sales_unit}
            state["expected_sale_units"] = expected
            state["challenge"] = challenge
            self.save(run)
            return challenge

    async def step(self, rid, p, check, tool, arguments, ctx=None):
        # Serialize observation and progression; two concurrent retries may not
        # overwrite each other's measured transitions.
        async with self.step_lock:
            return await self._step(rid, p, check, tool, arguments, ctx)

    async def _step(self, rid, p, check, tool, arguments, ctx):
        run = self.load(rid, p)
        if run["finished"] or check not in CHECKS:
            raise DomainError("INVALID_TRANSITION", "Choose an unfinished driving test and a named check", 409)
        state = run["checks"][check]
        if check == "elicitation":
            from .connection_contract_surface import question
            from .agent_surface_interpret import ConfirmAnswer
            from .mcp_server import create_server
            if tool != "probe" or arguments:
                raise DomainError("VALIDATION_ERROR", "Use probe with empty arguments", 422)
            server = create_server(self.gateway, p.id, full_surface=True)
            answer = await question(server, ctx, ConfirmAnswer, "Confirm you can answer a human question")
            state["status"] = "passed" if answer and answer.confirm else "failed"
            state["attempted"] = True
            self.save(run)
            return {"check": check, **state}
        # Never let the live connection principal/contract/mandate enter sandbox.
        token = PRINCIPAL.set(None)
        try:
            g = await asyncio.to_thread(self.sandbox, run, check)
            challenge = await asyncio.to_thread(self.prepare, run, check, g)
            if tool == "describe":
                if arguments:
                    raise DomainError("UNKNOWN_ARGUMENT", "describe has no arguments", 422)
                return {"check": check, "environment": "sandbox", "challenge": challenge}
            state["attempted"] = True
            allowed = {"unit": {"create_quote"}, "expired_quote": {"place_order", "create_quote"},
                "retry": {"place_order"}, "unknown_argument": {"create_quote"},
                "approval": {"place_order", "get_order_status"}}
            if tool not in allowed[check]:
                state["status"] = "failed"
                self.save(run)
                raise DomainError("INVALID_TRANSITION", "This challenge permits only " + ", ".join(sorted(allowed[check])), 409)
            from .mcp_server import create_server
            from .connection_contract import ECHO, SIGNED_QUOTE
            echo, signed = ECHO.set(None), SIGNED_QUOTE.set(False)
            try:
                # Keep tool registration local to this run/check. Re-registering
                # tools on each step can mutate schemas retained by the registry.
                key = (run["id"], check)
                if key not in self.servers:
                    self.servers[key] = create_server(g, "driving-agent", full_surface=True)
                result = await self.servers[key].call_tool(tool, arguments)
            finally:
                ECHO.reset(echo)
                SIGNED_QUOTE.reset(signed)
            value = result.structured_content or {}
            code = value.get("code") if result.is_error else None
            if check == "unit":
                lines = value.get("lines", [])
                state["status"] = "passed" if not result.is_error and len(lines) == 1 and lines[0]["product_id"] == challenge["product_id"] and lines[0]["units_needed"] == state["expected_sale_units"] else "failed"
            elif check == "unknown_argument":
                state["status"] = "passed" if code == "UNKNOWN_ARGUMENT" and value.get("next_action") == "FIX_REQUEST" else "failed"
            elif check == "expired_quote":
                if code == "EXPIRED_QUOTE" and value.get("next_action") == "REFRESH_QUOTE" and arguments.get("quote_id") == challenge["quote_id"]:
                    state["saw_expired"] = True
                elif tool == "create_quote" and state.get("saw_expired") and not result.is_error and value.get("need_ref") == challenge["quote_arguments"]["need_ref"]:
                    state["status"] = "passed"
                else:
                    state["status"] = "failed"
            elif check == "retry":
                if arguments != {"quote_id": challenge["quote_id"], "idempotency_key": challenge["intent_id"]} or result.is_error:
                    state["status"] = "failed"
                elif "dropped_order_id" not in state:
                    state["dropped_order_id"] = value["id"]
                    self.save(run)
                    return {"check": check, "environment": "sandbox", "response_dropped": True,
                        "next_action": "RETRY_SAME_COMMAND", "intent_id": challenge["intent_id"]}
                else:
                    state["status"] = "passed" if value.get("id") == state["dropped_order_id"] else "failed"
            elif check == "approval":
                if tool == "place_order" and not result.is_error and value.get("status") == "PENDING_APPROVAL" and arguments.get("quote_id") == challenge["quote_id"]:
                    state["pending_order_id"] = value["id"]
                elif tool == "get_order_status" and state.get("pending_order_id") == arguments.get("order_id") and value.get("next_action") == "WAIT_FOR_APPROVAL" and value.get("status") == "PENDING_APPROVAL":
                    state["status"] = "passed"
                else:
                    state["status"] = "failed"
            state["last_code"] = code
            self.save(run)
            return {"check": check, "status": state["status"], "environment": "sandbox", "result": value}
        finally:
            PRINCIPAL.reset(token)

    def finish(self, rid, p):
        with self.lock:
            run = self.load(rid, p)
            if run["finished"]:
                return run["result"]
            for state in run["checks"].values():
                if state["status"] == "not_attempted" and state.get("attempted"):
                    state["status"] = "failed"
            payload = {"type": "LuotsiDrivingTest", "run_id": rid, "connection_id": p.connection_id,
                "agent_id": p.id, "client": run["client"], "contract_version": run["contract_version"],
                "checks": {k: v["status"] for k, v in run["checks"].items()},
                "environment": "sandbox", "iat": int(audit.utcnow().timestamp()),
                "exp": int((audit.utcnow() + timedelta(days=30)).timestamp())}
            result = {"payload": payload, "signature": self.gateway.signing.sign(payload)}
            run["finished"], run["result"] = True, result
            self.save(run)
            return result

    def seeded(self, cid, agent, client):
        m = manager(self.gateway)
        rid = "seeded_" + cid
        with self.gateway.db.connect() as conn:
            row = conn.execute("SELECT data FROM connection_contract_runs WHERE id=?", (rid,)).fetchone()
        if row:
            return json.loads(row[0])["result"]
        payload = {"type": "LuotsiDrivingTest", "run_id": rid, "connection_id": cid, "agent_id": agent,
            "client": client, "contract_version": m.current()["version"], "checks": {k: "passed" for k in CHECKS},
            "environment": "sandbox", "seeded_demo": True, "iat": int(audit.utcnow().timestamp()),
            "exp": int((audit.utcnow() + timedelta(days=30)).timestamp())}
        result = {"payload": payload, "signature": self.gateway.signing.sign(payload)}
        with self.gateway.db.connect(write=True, notify=False) as conn:
            conn.execute("INSERT OR IGNORE INTO connection_contract_runs VALUES(?,?,?)", (rid, cid, json.dumps({"result": result})))
        return result

    def status(self, p):
        from .signing import verify_jws
        m = manager(self.gateway)
        cid = p.connection_id or "legacy:" + p.id
        state = m.state(cid)
        client = state["client"]
        if p.connection_id:
            row = self.connection(p, allow_inactive=True)
            client = client or row["client"]
        inactive = bool(p.connection_id and (row["revoked_at"] or row["expires_at"] and row["expires_at"] <= audit.utcnow().isoformat()))
        with self.gateway.db.connect() as conn:
            legacy = conn.execute("SELECT 1 FROM connection_contract_legacy_connections WHERE connection_id=?", (cid,)).fetchone()
        seeded = bool(legacy) or p.id in DEMO_AGENTS
        result = None
        with self.gateway.db.connect() as conn:
            rows = conn.execute("SELECT data FROM connection_contract_runs WHERE connection_id=? ORDER BY rowid DESC", (cid,)).fetchall()
        if seeded and not rows:
            result = self.seeded(cid, p.id, client)
        for row in rows:
            candidate = json.loads(row[0]).get("result")
            if candidate:
                result = candidate
                break
        failed, valid = list(REQUIRED), False
        if result:
            payload = result["payload"]
            try:
                verify_jws(result["signature"], payload, self.gateway.signing.jwks(include_retired=True))
                # Server-issued demo/migration proofs survive transport negotiation.
                # Measured results retain their exact client binding.
                seeded_client = payload.get("seeded_demo") is True and (seeded or p.id in DEMO_AGENTS)
                valid = (payload["connection_id"] == cid and payload["agent_id"] == p.id and (seeded_client or payload["client"] == client)
                    and payload["exp"] > audit.utcnow().timestamp() and not any(c["material"] for c in m.changes_since(payload["contract_version"])))
            except (ValueError, KeyError, TypeError, InvalidSignature):
                valid = False
            if valid:
                failed = [k for k in REQUIRED if payload["checks"].get(k) != "passed"]
        override = self.override(p, m.current()["version"]) if not inactive else False
        enabled = not failed or override
        quote_blockers = [k for k in ("expired_quote", "unknown_argument") if valid and result["payload"]["checks"].get(k) == "failed"]
        return {"result": result, "valid": valid, "required_checks": failed, "override": override,
                "blocked_operations": {"quote": quote_blockers if not override else [], "order": failed if not enabled else []},
                "enabled_operations": [] if inactive else ["lookup"] + (["quote"] if not quote_blockers or override else []) + (["order"] if enabled else []),
                "contract": m.public(), "negotiation": state}

    def require_quote(self, p):
        if not p or p.kind != "agent" or not p.connection_id:
            return
        state = self.status(p)
        if "quote" not in state["enabled_operations"]:
            raise DomainError("DRIVING_TEST_REQUIRED", "Repeat failed quote checks in the sandbox", 403,
                {"checks": state["blocked_operations"]["quote"], "operation": "quote", "connection_id": p.connection_id,
                 "tool": "start_driving_test"})

    def require_order(self, p, signed_quote=None):
        # Internal domain jobs and pre-connection local callers preserve their
        # existing policy guards. Every issued external connection is graduated.
        if not p or p.kind != "agent" or not p.connection_id:
            return
        status = self.status(p)
        if signed_quote and status["result"]:
            payload = status["result"]["payload"]
            if (all(payload["checks"].get(k) == "passed" for k in REQUIRED)
                    and payload["connection_id"] == p.connection_id and payload["agent_id"] == p.id
                    and (payload["client"] == self.client(self.connection(p)) or status["valid"] and payload.get("seeded_demo") is True)
                    and payload["exp"] > audit.utcnow().timestamp()
                    and manager(self.gateway).signed_pin(signed_quote, p.id, payload["contract_version"])):
                from .signing import verify_jws
                verify_jws(status["result"]["signature"], payload, self.gateway.signing.jwks(include_retired=True))
                return
        if "order" not in status["enabled_operations"]:
            raise DomainError("DRIVING_TEST_REQUIRED", "Pass driving checks before ordering", 403,
                {"checks": status["required_checks"], "connection_id": p.connection_id,
                 "tool": "start_driving_test",
                 "start_url": f"/v1/connections/{p.connection_id}/driving-test"})

    def override(self, p, version):
        if not p.connection_id:
            return False
        client = self.client(self.connection(p))
        with self.gateway.db.connect() as conn:
            rows = conn.execute("SELECT data FROM governance_requests WHERE agent_id=? AND status='approved'", (p.id,)).fetchall()
        return any((r := json.loads(row[0])).get("kind") == "driving_test_override" and
            r.get("connection_id") == p.connection_id and r.get("client") == client
            and not any(c["material"] for c in manager(self.gateway).changes_since(r.get("contract_version"))) for row in rows)

    def request_override(self, cid, reason, p):
        current = manager(self.gateway).current()["version"]
        with self.gateway.db.connect(write=True) as conn:
            row = conn.execute("SELECT * FROM connections WHERE connection_id=?", (cid,)).fetchone()
            if not row:
                raise DomainError("CONNECTION_NOT_FOUND", "Connection was not found", 404)
            agent = self.gateway.governance.target(conn, row["agent_id"], p)
            request = {"id": "pcr_" + uuid4().hex, "kind": "driving_test_override", "agent_id": row["agent_id"],
                "connection_id": cid, "client": self.client(dict(row)), "contract_version": current, "requester": p.user_id or p.id, "reason": reason,
                "status": "pending", "tenant_id": agent["tenant_id"], "base_version": current}
            conn.execute("INSERT INTO governance_requests(id,agent_id,requester,base_version,status,data,tenant_id) VALUES(?,?,?,?,?,?,?)", (request["id"], row["agent_id"], request["requester"], current, "pending", json.dumps(request), agent["tenant_id"]))
            audit.append(conn, request["requester"], "POLICY_CHANGE_REQUESTED", reason,
                data={"request_id": request["id"], "agent_id": row["agent_id"], "kind": request["kind"]})
            return request


def decide_override(gateway, request_id, decision, reason, p):
    """Delegate only this request kind; the existing spec-44 decision endpoint remains authoritative."""
    with gateway.db.connect(write=True) as conn:
        row = conn.execute("SELECT * FROM governance_requests WHERE id=?", (request_id,)).fetchone()
        if not row or json.loads(row["data"]).get("kind") != "driving_test_override":
            return None
        gateway.accounts.manage(p)
        gateway.governance.target(conn, row["agent_id"], p)
        if row["requester"] == (p.user_id or p.id):
            raise DomainError("SELF_APPROVAL_FORBIDDEN", "A requester cannot approve its own override", 403)
        if row["status"] != "pending":
            raise DomainError("INVALID_TRANSITION", "Change request is already decided", 409)
        request = json.loads(row["data"])
        if any(c["material"] for c in manager(gateway).changes_since(request["contract_version"])):
            raise DomainError("CONTRACT_STALE", "Submit an override for the current contract", 409)
        request.update(status="approved" if decision == "approve" else "rejected", decided_by=p.user_id or p.id,
                       decided_at=audit.utcnow().isoformat(), decision_reason=reason)
        conn.execute("UPDATE governance_requests SET status=?,data=? WHERE id=?", (request["status"], json.dumps(request), request_id))
        audit.append(conn, p.user_id or p.id, "POLICY_CHANGED", reason, data=request)
        return {"status": request["status"], "change_request": request}
