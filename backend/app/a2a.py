"""A2A 1.0 procurement tasks over the existing authenticated gateway commands.

SDK protobuf types and server routing own the wire protocol. Task snapshots are
durable domain events in the same outbox used for webhook push and SSE replay.
"""
import asyncio
import json
import threading
from contextlib import contextmanager
from hashlib import sha256
from uuid import uuid4

from a2a import types as t
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import RequestHandler
from a2a.server.routes.common import DefaultServerCallContextBuilder
from a2a.server.routes.fastapi_routes import add_a2a_routes_to_fastapi
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.utils.errors import InvalidParamsError, TaskNotFoundError, UnsupportedOperationError
from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from google.protobuf.json_format import MessageToDict, ParseDict
from pydantic import ValidationError

from . import audit
from .auth import check_source, admit_credential
from .canonical import canonical_json
from .db import DomainError
from .foundation import CAUSATION, CORRELATION, PRINCIPAL, TASK, correlation_id
from .models import NeedSpec, QuoteRequest
from .recovery import problem, validation_problem
from .webhooks import EVENT_TYPES, SubscriptionCreate

TERMINAL = {t.TaskState.TASK_STATE_COMPLETED, t.TaskState.TASK_STATE_REJECTED, t.TaskState.TASK_STATE_FAILED, t.TaskState.TASK_STATE_CANCELED}
ORDER_STATES = {"PENDING_EXECUTION": t.TaskState.TASK_STATE_WORKING, "PENDING_APPROVAL": t.TaskState.TASK_STATE_AUTH_REQUIRED, "CONFIRMED": t.TaskState.TASK_STATE_COMPLETED,
                "REJECTED": t.TaskState.TASK_STATE_REJECTED, "EXPIRED": t.TaskState.TASK_STATE_FAILED, "CANCELLED": t.TaskState.TASK_STATE_CANCELED}


def proto(cls, data):
    return ParseDict(data, cls())


def command_data(value):
    # protobuf Value represents every JSON number as a double. Restore integral
    # values before the shared strict integer validators (never coerce strings or bools).
    if isinstance(value, dict):
        return {key: command_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [command_data(item) for item in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def data_part(data):
    return proto(t.Part, {"data": data, "mediaType": "application/json"})


def status_message(task_id, summary, data=None):
    return t.Message(message_id=uuid4().hex, task_id=task_id, role=t.Role.ROLE_AGENT,
                     parts=[t.Part(text=summary), *([data_part(data)] if data is not None else [])])


def artifact(task, name, data):
    value = t.Artifact(artifact_id=task.id + ":" + name, name=name,
                       parts=[data_part(data), t.Part(text=f"Procurement {name}")])
    for index, old in enumerate(task.artifacts):
        if old.name == name:
            task.artifacts[index].CopyFrom(value)
            return
    task.artifacts.append(value)


def metadata(task):
    return MessageToDict(task).get("metadata", {})


def shared_tool(gateway, principal, name, arguments, mandate=None):
    """Use adjacent feature adapters through their published MCP schema boundary.

This keeps the basket/evidence integrations additive across parallel lanes and
does not reproduce their business rules in the A2A transport.
"""
    async def invoke():
        from .mcp_server import create_server
        server = create_server(gateway, principal.id, mandate=mandate)
        if name not in {tool.name for tool in await server.list_tools()}:
            return None
        result = await server.call_tool(name, arguments)
        value = result.structured_content
        if result.is_error:
            code = value["code"]
            raise DomainError(code, value["message"], value["status"], value.get("data"))
        return value
    return asyncio.run(invoke())


class ProcurementA2A:
    def __init__(self, gateway):
        self.gateway, self.db = gateway, gateway.db
        self.command_lock = threading.RLock()
        with self.db.connect() as conn:
            existing = conn.execute("SELECT 1 FROM a2a_tasks LIMIT 1").fetchone()
        if existing:
            self._ensure_consumers()

    def _ensure_consumers(self):
        gateway = self.gateway
        if {"a2a:project", "a2a:stream"} <= gateway.outbox.handlers.keys():
            with self.db.connect() as conn:
                if conn.execute("SELECT COUNT(*) FROM event_consumers WHERE name IN ('a2a:project','a2a:stream') AND enabled=1").fetchone()[0] == 2:
                    return
        gateway.outbox.register("a2a:project", self._project, replay=True)
        # Queue consumer intentionally has no generic dispatcher handler. Its rows
        # are acknowledged by stream readers; snapshots remain replayable.
        gateway.outbox.register("a2a:stream", lambda event: None)

    def _load(self, conn, task_id, principal=None):
        row = conn.execute("SELECT * FROM a2a_tasks WHERE id=?", (task_id,)).fetchone()
        if row is None or (principal is not None and row["owner"] != f"{principal.kind}:{principal.id}"):
            raise DomainError("TASK_NOT_FOUND", "Task was not found", 404)
        return proto(t.Task, json.loads(row["data"]))

    def _save(self, conn, task, owner):
        conn.execute("INSERT INTO a2a_tasks VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                     (task.id, metadata(task).get("owner_scope", "agent:" + owner), json.dumps(MessageToDict(task))))

    def _update(self, conn, task, state, summary, data=None):
        task.status.CopyFrom(t.TaskStatus(state=state, message=status_message(task.id, summary, data)))
        task.status.timestamp.FromDatetime(audit.utcnow())
        meta = metadata(task)
        revision = conn.execute("SELECT COALESCE(MAX(aggregate_revision),0)+1 FROM events WHERE aggregate_type='task' AND aggregate_id=?",
                                (task.id,)).fetchone()[0]
        event_id = uuid4().hex
        meta.update(event_id=event_id, aggregate_revision=revision)
        task.metadata.update(meta)
        self._save(conn, task, meta["owner"])
        # Lifecycle records are additive; purchasing audit actions still come only
        # from the same lookup/quote/order methods used by REST and MCP.
        audit_id = audit.append(conn, f"agent:{meta['owner']}", "A2A_TASK_UPDATED", summary,
                                data={"task_id": task.id, "state": t.TaskState.Name(state), "revision": revision})
        if audit_id is None:
            audit_id = conn.execute("SELECT MAX(id) FROM audit").fetchone()[0]
        context = meta.get("context", {})
        conn.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, 1, "task.status_updated", context.get("customer_id"), context.get("environment", "live"),
             "task", task.id, revision, f"agent:{meta['owner']}", audit.utcnow().isoformat(),
             CORRELATION.get() or meta["correlation_id"], CAUSATION.get(),
             json.dumps({"task": MessageToDict(task), "links": {"task_id": task.id, "event_id": event_id}}), audit_id))
        conn.execute("INSERT INTO outbox(event_id,consumer,status,attempts,next_attempt_at) "
                     "SELECT ?,name,'pending',0,? FROM event_consumers WHERE enabled=1", (event_id, audit.utcnow().isoformat()))

    def _order_result(self, conn, task, order, *, state=None):
        meta = metadata(task)
        meta["order_id"] = order.id
        meta["quote_id"] = order.quote_id
        meta["context"] = order.context or meta.get("context", {})
        task.metadata.update(meta)
        order.links["task_id"] = task.id
        if task.id not in order.links.setdefault("task_ids", []):
            order.links["task_ids"].append(task.id)
        self.gateway.orders._save_order(conn, order)
        artifact(task, "order", order.model_dump(mode="json"))
        if order.receipt:
            artifact(task, "receipt", {"order_id": order.id, "receipt": order.receipt,
                                      "jwks_url": "/.well-known/jwks.json", "status": order.status})
        self._update(conn, task, state if state is not None else ORDER_STATES[order.status], f"Order {order.status}",
                     {"next_action": order.next_action, "poll_after_seconds": order.poll_after_seconds,
                      "evidence_revision": order.evidence_revision})

    def _project(self, event):
        if event["aggregate_type"] != "order":
            return
        with self.db.connect(write=True) as conn:
            rows = conn.execute("SELECT * FROM a2a_tasks WHERE json_extract(data,'$.metadata.order_id')=?", (event["aggregate_id"],)).fetchall()
            for row in rows:
                task = proto(t.Task, json.loads(row["data"]))
                if task.status.state in TERMINAL:
                    continue  # A later order cancellation belongs to its new task.
                order = self.gateway.orders._order(conn, event["aggregate_id"])
                old = next((MessageToDict(a.parts[0])["data"] for a in task.artifacts if a.name == "order"), {})
                if old.get("status") != order.status or old.get("evidence_revision") != order.evidence_revision:
                    self._order_result(conn, task, order)

    def send(self, params, principal, mandate=None):
        with self.command_lock:
            if params.configuration.accepted_output_modes and not set(params.configuration.accepted_output_modes) & {"application/json", "text/plain"}:
                from a2a.utils.errors import ContentTypeNotSupportedError
                raise ContentTypeNotSupportedError()
            from .commands import ADMISSION, failure_audit
            admission_token = ADMISSION.set(None)
            try:
                from .connection_contract_surface import a2a_scope
                with a2a_scope(self.gateway, params, principal):
                    task = self._send(params, principal, mandate)
                from .agent_surface_interpret import note_a2a
                note_a2a(self.gateway, params, principal)
            except DomainError as error:
                from .agent_surface_interpret import note_a2a
                note_a2a(self.gateway, params, principal, error)
                parts = [MessageToDict(part).get("data", {}) for part in params.message.parts if part.WhichOneof("content") == "data"]
                name = parts[0].get("command") if parts else None
                route = {"lookup": "gateway.find_offers", "select_plan": "orders.create_quote",
                         "place_order": "orders.place_order", "cancel": "orders.cancel"}.get(name, "a2a.command")
                failure_audit(self.gateway, route, {}, principal.id, error)
                raise
            finally:
                ADMISSION.reset(admission_token)
            if params.configuration.HasField("task_push_notification_config"):
                config = t.TaskPushNotificationConfig()
                config.CopyFrom(params.configuration.task_push_notification_config)
                config.task_id = task.id
                config.id = config.id or "message:" + params.message.message_id
                self.create_push(config, principal)
            if params.configuration.history_length > 0:
                history = list(task.history)[-params.configuration.history_length:]
                del task.history[:]
                task.history.extend(history)
            return task

    def _reauthorize(self, conn, command, key, task, principal, mandate):
        """Use the original command result, not a later task's mutable selection.

        Old messages need no schema migration: shared command envelopes already
        contain their immutable result. Lifecycle-only messages use owned bindings.
        """
        from .commands import replay_authorization
        from .buyer import check_suspension
        check_suspension(self.db, principal.id)
        previous = conn.execute("SELECT * FROM commands WHERE principal=? AND key=?",
                                (f"{principal.kind}:{principal.id}", key)).fetchone() if key is not None else None
        if previous:
            saved = json.loads(previous["result"])
            value, name = saved["value"], previous["command"]
            arguments = {"mandate": mandate, "agent_id": principal.id, "operator": principal.operator}
            if name == "find_offers":
                arguments["need"] = NeedSpec.model_validate(value["need"], context={"stored": True})
                route = "gateway.find_offers"
            elif name == "create_quote":
                arguments["request"] = QuoteRequest.model_validate({"customer_id": value["customer_id"],
                    "deliver_by": value["deliver_by"], "need_ref": value["need_ref"],
                    "lines": [{"product_id": line["product_id"], "quantity_sales_units": line["units_needed"]} for line in value["lines"]]},
                    context={"stored": True})
                route = "orders.create_quote"
            elif name == "place_order":
                arguments.update(quote_id=value["quote_id"], idempotency_key=key)
                route = "orders.place_order"
            elif name == "cancel":
                arguments.update(order_id=value["id"], actor=f"agent:{principal.id}")
                route = "orders.cancel"
            else:
                raise DomainError("VALIDATION_ERROR", "Unsupported original command binding", 422)
            replay_authorization(self.gateway, route, conn, arguments, principal, saved)
            return
        # Historical/lifecycle-only messages still recheck the recorded resource.
        meta = metadata(task)
        # Pre-adapter messages may lack a shared command row. Their immutable
        # event snapshot retains the original binding after subsequent lookups.
        snapshot = conn.execute("SELECT payload FROM events WHERE aggregate_type='task' AND aggregate_id=? AND causation_id=? ORDER BY aggregate_revision DESC LIMIT 1",
                                (task.id, key)).fetchone() if key is not None else None
        if snapshot:
            meta = json.loads(snapshot[0])["task"].get("metadata", meta)
        if command["command"] == "lookup" and "need" in command:
            customer = command["need"].get("customer_id")
        else:
            customer = (meta.get("context") or {}).get("customer_id")
        kind, object_id, products = None, None, []
        for field, table, candidate in (("order_id", "orders", "order"), ("quote_id", "quotes", "quote"), ("request_id", "lookups", "lookup")):
            if meta.get(field):
                row = conn.execute(f"SELECT data FROM {table} WHERE id=?", (meta[field],)).fetchone()
                if not row:
                    continue
                obj = json.loads(row[0])
                if obj.get("agent_id") != principal.id:
                    raise DomainError("TASK_NOT_FOUND", "Task was not found", 404)
                if command["command"] != "lookup":
                    customer = obj.get("customer_id") or obj.get("need", {}).get("customer_id")
                kind, object_id = candidate, meta[field]
                products = [line["product_id"] for line in obj.get("lines", obj.get("offers", []))]
                break
        if customer:
            self.gateway.mandates.authorize(conn, mandate, principal.id, customer, products=products,
                bound=(kind, object_id) if kind else None)

    def _send(self, params, principal, mandate=None):
        from .agent_surface_interpret import a2a_text
        interpreted = a2a_text(self, params, principal, mandate)
        if interpreted is not None:
            return interpreted
        message = params.message
        if not message.message_id or message.role != t.Role.ROLE_USER:
            raise InvalidParamsError("A user message with a nonempty messageId is required")
        parts = [MessageToDict(part)["data"] for part in message.parts if part.WhichOneof("content") == "data"]
        if len(parts) != 1 or not isinstance(parts[0], dict):
            raise InvalidParamsError("Supply one versioned structured command data part")
        command = command_data(parts[0])
        if type(command.get("version")) is not int or command["version"] != 1 or command.get("command") not in {"lookup", "select_plan", "place_order", "cancel", "get_evidence"}:
            raise InvalidParamsError("Command version must be 1; command must be lookup, select_plan, place_order, cancel or get_evidence")
        digest = sha256(canonical_json(MessageToDict(message))).hexdigest()
        error = None
        scope = f"{principal.kind}:{principal.id}"
        with self.db.connect() as conn:
            previous = conn.execute("SELECT * FROM a2a_messages WHERE owner=? AND message_id=?", (scope, message.message_id)).fetchone()
            old_task = self._load(conn, previous["task_id"], principal) if previous else None
        complete_replay = old_task is not None and metadata(old_task).get("retry_message_id") != message.message_id
        category = "lookup" if command["command"] == "lookup" else "read" if command["command"] == "get_evidence" else "mutation"
        self.gateway.limiter.consume(principal.id, "read" if complete_replay else category)
        if previous:
            if previous["digest"] != digest:
                raise DomainError("IDEMPOTENCY_CONFLICT", "Message id belongs to a changed command", 409)
            replay_error = None
            with self.db.connect(write=True) as conn:
                try:
                    self._reauthorize(conn, command, message.message_id, old_task, principal, mandate)
                except DomainError as caught:
                    replay_error = caught
            if replay_error:
                raise replay_error
            if complete_replay:
                return old_task
        from .buyer import check_suspension
        check_suspension(self.db, principal.id)
        from .commands import ADMISSION
        ADMISSION.set({"identity": principal.id, "category": category, "used": False})
        self._ensure_consumers()
        with self.db.connect(write=True) as conn:
            previous = conn.execute("SELECT * FROM a2a_messages WHERE owner=? AND message_id=?", (scope, message.message_id)).fetchone()
            retry_task = None
            if previous:
                if previous["digest"] != digest:
                    raise DomainError("IDEMPOTENCY_CONFLICT", "Message id belongs to a changed command", 409)
                retry_task = self._load(conn, previous["task_id"], principal)
                if metadata(retry_task).get("retry_message_id") != message.message_id:
                    return retry_task
            if retry_task is not None:
                task = retry_task
                task.metadata.update({"retry_message_id": None})
            elif message.task_id:
                task = self._load(conn, message.task_id, principal)
                if message.context_id and message.context_id != task.context_id:
                    raise InvalidParamsError("contextId does not match task")
            else:
                if command["command"] != "lookup" and not (command["command"] == "cancel" and command.get("order_id")):
                    raise InvalidParamsError("Start with lookup, or a new cancel command against an order; subsequent messages require taskId")
                task = t.Task(id=uuid4().hex, context_id=message.context_id or uuid4().hex)
                task.metadata.update({"owner": principal.id, "owner_scope": scope, "principal": principal.public(), "correlation_id": correlation_id(),
                    "context": {"customer_id": command.get("need", {}).get("customer_id") if isinstance(command.get("need", {}), dict) else None,
                                "supplier_id": "valio-aimo", "request_ref": None, "effective_constraints": None,
                                "mandate_ref": None, "policy_version": sha256(canonical_json(self.gateway.policy._editable(self.gateway.policy.get()))).hexdigest(),
                                "environment": self.gateway.settings.environment}})
                if command["command"] == "cancel":
                    order = self.gateway.orders.get_order(command["order_id"], principal.id)
                    task.metadata.update({"order_id": order.id, "context": order.context or {}, "purpose": "cancel"})
            if task.status.state in TERMINAL and command["command"] != "get_evidence":
                raise DomainError("INVALID_TRANSITION", "Task is terminal; cancel a confirmed order with a new cancel command", 409)
            if retry_task is None:
                task.history.append(message)
            terminal_state = task.status.state if command["command"] == "get_evidence" and task.status.state in TERMINAL else None
            task.metadata.update({"retry_message_id": message.message_id})
            if terminal_state is None:
                self._update(conn, task, t.TaskState.TASK_STATE_WORKING, "Executing " + command["command"])
            else:
                self._save(conn, task, principal.id)
            conn.execute("INSERT OR IGNORE INTO a2a_messages VALUES(?,?,?,?)", (scope, message.message_id, digest, task.id))
        # Publish WORKING before starting supplier I/O. The next transaction
        # still commits the core command and its resulting artifacts atomically.
        with self.db.connect(write=True) as conn:
            task = self._load(conn, task.id, principal)
            task.metadata.update({"retry_message_id": None})
            task_token = TASK.set(task.id)
            try:
                self._execute(conn, task, command, message.message_id, principal, mandate)
            except ValidationError as caught:
                recovery = problem(validation_problem(caught))
                self._update(conn, task, t.TaskState.TASK_STATE_INPUT_REQUIRED, "Supply the missing or corrected request details", recovery)
            except DomainError as caught:
                caught.data = {**caught.data, "task_id": task.id}
                recovery = problem(caught)
                state = t.TaskState.TASK_STATE_AUTH_REQUIRED if recovery["next_action"] == "CONTACT_ADMIN" else (
                    t.TaskState.TASK_STATE_INPUT_REQUIRED if recovery["next_action"] in {"FIX_REQUEST", "REFRESH_QUOTE", "LOOKUP_REQUIRED", "REVIEW_UPDATED_EVIDENCE"} or recovery["retryable"] else t.TaskState.TASK_STATE_FAILED)
                if caught.code == "HARD_CONSTRAINT_VIOLATION":
                    state = t.TaskState.TASK_STATE_REJECTED
                if metadata(task).get("order_id") and metadata(task).get("purpose") != "cancel":
                    order = self.gateway.orders._order(conn, metadata(task)["order_id"])
                    state = ORDER_STATES[order.status]
                if terminal_state is not None:
                    state = terminal_state
                if recovery["retryable"]:
                    task.metadata.update({"retry_message_id": message.message_id})
                self._update(conn, task, state, caught.message, recovery)
                error = caught
            except Exception:
                error = DomainError("INTERNAL_ERROR", "The purchasing command could not be completed", 500, {"task_id": task.id})
                self._update(conn, task, terminal_state if terminal_state is not None else t.TaskState.TASK_STATE_FAILED,
                             error.message, problem(error))
            finally:
                TASK.reset(task_token)
            conn.execute("INSERT OR IGNORE INTO a2a_messages VALUES(?,?,?,?)", (scope, message.message_id, digest, task.id))
        if error:
            raise error
        if metadata(task).get("order_id") and task.status.state == t.TaskState.TASK_STATE_WORKING:
            self.gateway.orders.dispatch_pending(metadata(task)["order_id"])
            with self.db.connect(write=True) as conn:
                task = self._load(conn, task.id, principal)
                order = self.gateway.orders._order(conn, metadata(task)["order_id"])
                if order.status != "PENDING_EXECUTION":
                    task.metadata.update({"retry_message_id": None})
                self._order_result(conn, task, order)
        return task

    def _execute(self, conn, task, command, key, principal, mandate):
        meta, name = metadata(task), command["command"]
        if name == "lookup":
            if command.get("skill") == "find_basket_offers":
                arguments = command.get("arguments", {})
                from .baskets import BasketRequest, find_basket_offers
                result = find_basket_offers(self.gateway, BasketRequest.model_validate(arguments.get("request", arguments)), principal.id, mandate)
                self._clear_selection(task, meta)
                context = result.get("context") or {}
                scope = arguments.get("request", arguments)
                meta.update(request_id=result["basket_lookup_id"], offers_ref=result["basket_lookup_id"], context=context,
                            basket_ref={"basket_lookup_id": result["basket_lookup_id"]},
                            customer_id=context.get("customer_id") or scope.get("customer_id"), deliver_by=scope.get("deliver_by"))
                task.metadata.update(meta)
                artifact(task, "request", arguments)
                artifact(task, "offers", result)
                self._update(conn, task, t.TaskState.TASK_STATE_INPUT_REQUIRED, "Select the ingredient basket", {"next_action": "SELECT_PLAN"})
                return
            need = NeedSpec.model_validate(command.get("need", {}))
            result = self.gateway.find_offers(need, actor=f"agent:{principal.id}", mandate=mandate, command_key=key)
            self._clear_selection(task, meta)
            meta.update(request_id=result.lookup_id, context=result.context, offers_ref=result.lookup_id)
            task.metadata.update(meta)
            artifact(task, "request", need.model_dump(mode="json"))
            artifact(task, "offers", result.model_dump(mode="json"))
            self._update(conn, task, t.TaskState.TASK_STATE_INPUT_REQUIRED, "Select a fulfilment plan", {"next_action": "SELECT_PLAN"})
        elif name == "select_plan":
            if not meta.get("request_id"):
                raise DomainError("NEED_REF_REQUIRED", "Lookup is required before selecting a plan", 422)
            if meta.get("basket_ref"):
                request = QuoteRequest.model_validate({"customer_id": meta["customer_id"], "deliver_by": meta["deliver_by"], "basket_ref": meta["basket_ref"]})
                quote = self.gateway.orders.create_quote(request, principal.id, operator=principal.operator, mandate=mandate, command_key=key)
                meta.update(quote_id=quote.id, context=quote.context, selected_plan=meta["basket_ref"])
                task.metadata.update(meta)
                artifact(task, "selected_plan", meta["basket_ref"])
                artifact(task, "quote", quote.model_dump(mode="json"))
                self._update(conn, task, t.TaskState.TASK_STATE_INPUT_REQUIRED, "Place the selected basket quote", {"next_action": "PLACE_ORDER"})
                return
            # Selection is already admitted as one mutation by the A2A boundary.
            lookup = self.gateway.lookup._get(meta["request_id"], principal.owner)
            selection = {"customer_id": lookup.need.customer_id, "deliver_by": str(lookup.need.deliver_by),
                         "plan_ref": {"lookup_id": lookup.lookup_id, "plan_index": command.get("plan_index")}}
            request = QuoteRequest.model_validate(selection)
            quote = self.gateway.orders.create_quote(request, principal.id, operator=principal.operator, mandate=mandate, command_key=key)
            meta.update(quote_id=quote.id, context=quote.context, selected_plan=selection["plan_ref"])
            task.metadata.update(meta)
            artifact(task, "selected_plan", selection["plan_ref"])
            artifact(task, "quote", quote.model_dump(mode="json"))
            self._update(conn, task, t.TaskState.TASK_STATE_INPUT_REQUIRED, "Place the selected fixed-price quote", {"next_action": "PLACE_ORDER"})
        elif name == "place_order":
            if not meta.get("quote_id"):
                raise DomainError("NEED_REF_REQUIRED", "Select a plan before placement", 422)
            quote = self.gateway.orders._quote(conn, meta["quote_id"])
            if quote.need_ref != meta.get("request_id"):
                self._clear_selection(task, meta)
                raise DomainError("NEED_REF_REQUIRED", "Select a plan for the current lookup before placement", 422)
            order = self.gateway.orders.place_order(meta["quote_id"], key, principal.id, operator=principal.operator, mandate=mandate)
            if order.status == "PENDING_EXECUTION":
                task.metadata.update({"retry_message_id": key})
            self._order_result(conn, task, order)
        elif name == "cancel":
            order_id = meta.get("order_id") or command.get("order_id")
            if not order_id:
                self._reauthorize(conn, command, None, task, principal, mandate)
                from .commands import charge
                charge(self.gateway, principal.id, "mutation")
                self._update(conn, task, t.TaskState.TASK_STATE_CANCELED, "Procurement task canceled before ordering")
            else:
                self.gateway.orders.get_order(order_id, principal.id)
                order = self.gateway.orders.cancel(order_id, actor=f"agent:{principal.id}", reason=command.get("reason", ""), mandate=mandate, command_key=key)
                self._order_result(conn, task, order)
        else:
            self._reauthorize(conn, command, None, task, principal, mandate)
            terminal_state = task.status.state if task.status.state in TERMINAL else None
            if not meta.get("order_id"):
                raise DomainError("ORDER_NOT_FOUND", "Task has no order evidence", 404)
            from .evidence import build_bundle
            # Share the evidence core on this transaction's thread; the MCP SDK
            # dispatches synchronous tools to a different SQLite-incompatible thread.
            evidence = build_bundle(self.gateway, meta["order_id"], principal)[0]
            order = self.gateway.orders._order(conn, meta["order_id"])
            artifact(task, "evidence", evidence)
            self._order_result(conn, task, order, state=terminal_state)

    @staticmethod
    def _clear_selection(task, meta):
        for field in ("quote_id", "selected_plan", "basket_ref", "customer_id", "deliver_by"):
            meta.pop(field, None)
            if field in task.metadata:
                del task.metadata[field]
        retained = [item for item in task.artifacts if item.name not in {"quote", "selected_plan"}]
        del task.artifacts[:]
        task.artifacts.extend(retained)

    def create_push(self, params, principal):
        task = self.gateway.a2a.get(params.task_id, principal)
        customer = metadata(task).get("context", {}).get("customer_id")
        if not customer:
            raise InvalidParamsError("Lookup customer details before configuring push")
        from .webhooks import notification_headers
        notification_headers(MessageToDict(params))
        config = t.TaskPushNotificationConfig()
        config.CopyFrom(params)
        config.id = config.id or uuid4().hex
        with self.gateway.db.connect() as conn:
            previous = conn.execute("SELECT * FROM a2a_push_configs WHERE task_id=? AND id=?", (task.id, config.id)).fetchone()
            if previous:
                if json.loads(previous["data"]) == MessageToDict(config):
                    return config
        body = SubscriptionCreate(url=config.url, customer_ids=[customer],
            event_types=sorted(e for e in EVENT_TYPES if not e.startswith("platform.")))
        from .commands import ADMISSION
        self.gateway.limiter.consume(principal.id, "mutation")
        prepared = self.gateway.webhooks.prepare("webhooks.create", {"body": body, "push_config": MessageToDict(config)}, principal)
        credit = ADMISSION.set({"identity": principal.id, "category": "mutation", "used": False})
        try:
            with self.gateway.db.connect(write=True) as conn:
                self._load(conn, task.id, principal)
                previous = conn.execute("SELECT * FROM a2a_push_configs WHERE task_id=? AND id=?", (task.id, config.id)).fetchone()
                if previous:
                    self.gateway.webhooks.disable(previous["subscription_id"], principal)
                sub = self.gateway.webhooks.create(body, principal, task_id=task.id, push_config=MessageToDict(config))
                conn.execute("INSERT INTO a2a_push_configs VALUES(?,?,?,?) ON CONFLICT(task_id,id) DO UPDATE SET subscription_id=excluded.subscription_id,data=excluded.data",
                             (task.id, config.id, sub["id"], json.dumps(MessageToDict(config))))
                conn.execute("INSERT OR IGNORE INTO outbox(event_id,consumer,status,attempts,next_attempt_at) "
                             "SELECT id,?,'pending',0,? FROM events WHERE aggregate_type='task' AND aggregate_id=? ORDER BY aggregate_revision DESC LIMIT 1",
                             ("webhook:" + sub["id"], audit.utcnow().isoformat(), task.id))
        finally:
            self.gateway.webhooks.prepared.reset(prepared)
            ADMISSION.reset(credit)
        return config


    def get(self, task_id, principal, history_length=0):
        self.gateway.limiter.consume(principal.id, "read")
        with self.db.connect() as conn:
            task = self._load(conn, task_id, principal)
        if task.status.state != t.TaskState.TASK_STATE_WORKING and metadata(task).get("order_id"):
            self.gateway.orders.expire()
            with self.db.connect() as conn:
                task = self._load(conn, task_id, principal)
        if history_length > 0:
            history = list(task.history)[-history_length:]
            del task.history[:]
            task.history.extend(history)
        return task

    def event_matches(self, event, task_id):
        return event["aggregate_type"] == "task" and event["aggregate_id"] == task_id

    def push_body(self, event, task_id):
        task = proto(t.Task, event["payload"]["task"])
        return canonical_json(MessageToDict(t.StreamResponse(status_update=t.TaskStatusUpdateEvent(
            task_id=task_id, context_id=task.context_id, status=task.status,
            metadata={"event_id": event["id"], "aggregate_revision": event["aggregate_revision"]})))).decode()

    def updates(self, task_id, principal, after=0):
        with self.db.connect() as conn:
            self._load(conn, task_id, principal)
            rows = conn.execute("SELECT e.* FROM events e JOIN outbox o ON o.event_id=e.id AND o.consumer='a2a:stream' "
                "WHERE e.aggregate_type='task' AND e.aggregate_id=? AND e.aggregate_revision>? ORDER BY e.aggregate_revision", (task_id, after)).fetchall()
        return [dict(row) for row in rows]


class ContextBuilder(DefaultServerCallContextBuilder):
    def build(self, request):
        context = super().build(request)
        context.state["gateway"] = request.app.state.gateway
        context.state["keys"] = request.app.state.keys
        context.state["source"] = request.client.host if request.client else "unknown"
        return context


class Handler(RequestHandler):
    def __init__(self, audience="buyer"):
        self.audience = audience

    @contextmanager
    def authenticated(self, context, params=None):
        gateway = context.state["gateway"]
        headers = context.state.get("headers", {})
        scheme, _, secret = headers.get("authorization", "").partition(" ")
        try:
            if scheme.lower() != "bearer" or not secret:
                raise DomainError("UNAUTHENTICATED", "A valid Bearer credential is required", 401)
            principal = admit_credential(gateway, context.state["keys"], context.state.get("source", "unknown"), secret)
            from .oauth import require_audience
            require_audience(principal, self.audience, "/a2a" + ("" if self.audience == "buyer" else "/"+self.audience))
            meta = MessageToDict(params).get("metadata", {}) if params is not None else {}
            if params is not None and isinstance(params, t.SendMessageRequest):
                meta = {**MessageToDict(params.message).get("metadata", {}), **meta}
            c = CORRELATION.set(meta.get("correlation_id") or headers.get("x-correlation-id") or correlation_id())
            p = PRINCIPAL.set(principal)
            cause = CAUSATION.set(params.message.message_id if isinstance(params, t.SendMessageRequest) else None)
            try:
                yield gateway, principal, headers.get("agent-mandate")
            finally:
                CAUSATION.reset(cause)
                PRINCIPAL.reset(p)
                CORRELATION.reset(c)
        except DomainError as error:
            if error.code == "TASK_NOT_FOUND":
                raise TaskNotFoundError(data=problem(error)) from None
            # A2A 1.0 typed ErrorInfo.metadata carries the same RFC 9457 payload.
            raise InvalidParamsError(error.message, data=problem(error)) from None

    async def on_message_send(self, params, context):
        with self.authenticated(context, params) as (gateway, principal, mandate):
            if self.audience != "buyer":
                return await run_in_threadpool(human_send, gateway, principal, self.audience, params)
            if not params.configuration.return_immediately:
                return await run_in_threadpool(gateway.a2a.send, params, principal, mandate)
            job = asyncio.create_task(run_in_threadpool(gateway.a2a.send, params, principal, mandate))
            job.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
            while not job.done():
                with gateway.db.connect() as conn:
                    row = conn.execute("SELECT task_id FROM a2a_messages WHERE owner=? AND message_id=?",
                                       (f"{principal.kind}:{principal.id}", params.message.message_id)).fetchone()
                    if row:
                        return gateway.a2a._load(conn, row[0], principal)
                await asyncio.sleep(.01)
            return await job

    async def on_get_task(self, params, context):
        with self.authenticated(context, params) as (gateway, principal, _):
            return await run_in_threadpool(gateway.a2a.get, params.id, principal, params.history_length)

    async def on_list_tasks(self, params, context):
        with self.authenticated(context, params) as (gateway, principal, _):
            gateway.limiter.consume(principal.id, "read")
            with gateway.db.connect() as conn:
                tasks = [proto(t.Task, json.loads(row[0])) for row in conn.execute("SELECT data FROM a2a_tasks WHERE owner=? ORDER BY rowid DESC", (f"{principal.kind}:{principal.id}",))]
            tasks = [task for task in tasks if (not params.context_id or task.context_id == params.context_id) and
                     (not params.status or task.status.state == params.status) and
                     (not params.HasField("status_timestamp_after") or task.status.timestamp.ToNanoseconds() > params.status_timestamp_after.ToNanoseconds())]
            try:
                offset, size = int(params.page_token or "0"), min(params.page_size or 50, 100)
                if offset < 0 or size < 1:
                    raise ValueError()
            except ValueError:
                raise InvalidParamsError("Invalid pagination") from None
            page = tasks[offset:offset + size]
            for task in page:
                if params.history_length:
                    history = list(task.history)[-params.history_length:]
                    del task.history[:]
                    task.history.extend(history)
                if not params.include_artifacts:
                    del task.artifacts[:]
            return t.ListTasksResponse(tasks=page, total_size=len(tasks), page_size=size,
                                       next_page_token=str(offset + size) if offset + size < len(tasks) else "")

    async def on_cancel_task(self, params, context):
        with self.authenticated(context, params) as (gateway, principal, mandate):
            task = gateway.a2a.get(params.id, principal)
            if task.status.state in TERMINAL:
                from a2a.utils.errors import TaskNotCancelableError
                raise TaskNotCancelableError("Terminal task cannot be canceled; send a new cancel command against the confirmed order")
            if self.audience != "buyer":
                from .accounts import digest
                with gateway.db.connect(write=True) as conn:
                    for item in task.artifacts:
                        for part in item.parts:
                            value = MessageToDict(part).get("data", {})
                            if value.get("confirmation_token"):
                                conn.execute("UPDATE decision_confirmations SET used_at=? WHERE digest=? AND user_id=? AND used_at IS NULL",
                                    (audit.utcnow().isoformat(), digest(value["confirmation_token"]), principal.user_id))
                    task.status.CopyFrom(t.TaskStatus(state=t.TaskState.TASK_STATE_CANCELED))
                    task.status.timestamp.FromDatetime(audit.utcnow())
                    gateway.a2a._save(conn, task, principal.id)
                return task
            request = t.SendMessageRequest(message=t.Message(message_id="cancel-task:" + task.id,
                task_id=task.id, role=t.Role.ROLE_USER, parts=[data_part({"version": 1, "command": "cancel"})]))
            return await run_in_threadpool(gateway.a2a.send, request, principal, mandate)

    @staticmethod
    def _event(row):
        task = proto(t.Task, json.loads(row["payload"])["task"])
        return task, t.TaskStatusUpdateEvent(task_id=task.id, context_id=task.context_id, status=task.status,
                 metadata={"event_id": row["id"], "aggregate_revision": row["aggregate_revision"]})

    async def on_message_send_stream(self, params, context):
        # The SDK reads the first event in one context and the remainder in an
        # SSE task. ContextVar tokens must never cross a yield boundary.
        with self.authenticated(context, params) as (gateway, principal, mandate):
            after = 0
            if params.message.task_id:
                after = int(metadata(gateway.a2a.get(params.message.task_id, principal)).get("aggregate_revision", 0))
            job = asyncio.create_task(run_in_threadpool(gateway.a2a.send, params, principal, mandate))
            scope = f"{principal.kind}:{principal.id}"
        try:
            while True:
                with self.authenticated(context, params) as (gateway, principal, _):
                    with gateway.db.connect() as conn:
                        row = conn.execute("SELECT task_id FROM a2a_messages WHERE owner=? AND message_id=?",
                                           (scope, params.message.message_id)).fetchone()
                    rows = gateway.a2a.updates(row[0], principal, after) if row else []
                    done = job.done()
                    if done:
                        # Convert domain errors using the same recovery mapping
                        # before leaving this authentication context.
                        await job
                        rows = gateway.a2a.updates(row[0], principal, after) if row else []
                for row in rows:
                    snapshot, event = self._event(row)
                    after = row["aggregate_revision"]
                    gateway.connection_trust.check(principal)
                    yield event
                    if snapshot.status.state != t.TaskState.TASK_STATE_WORKING:
                        for item in snapshot.artifacts:
                            gateway.connection_trust.check(principal)
                            yield t.TaskArtifactUpdateEvent(task_id=snapshot.id, context_id=snapshot.context_id,
                                                           artifact=item, last_chunk=True)
                if done:
                    return
                await asyncio.sleep(.01)
        finally:
            if not job.done():
                # Disconnecting a stream does not cancel a purchasing command.
                job.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)

    async def on_subscribe_to_task(self, params, context):
        with self.authenticated(context, params) as (gateway, principal, _):
            gateway.limiter.consume(principal.id, "read")
            with gateway.db.connect() as conn:
                gateway.a2a._load(conn, params.id, principal)
            headers = context.state.get("headers", {})
            try:
                after = int(headers.get("x-a2a-after-revision", "0"))
                if after < 0:
                    raise ValueError()
            except ValueError:
                raise InvalidParamsError("X-A2A-After-Revision must be a nonnegative revision") from None
            if headers.get("last-event-id"):
                with gateway.db.connect() as conn:
                    row = conn.execute("SELECT aggregate_revision FROM events WHERE id=? AND aggregate_type='task' AND aggregate_id=?",
                                       (headers["last-event-id"], params.id)).fetchone()
                if row is None:
                    raise InvalidParamsError("Unknown replay cursor for this task")
                after = row[0]
        while True:
            with self.authenticated(context, params) as (gateway, principal, _):
                rows = await run_in_threadpool(gateway.a2a.updates, params.id, principal, after)
                with gateway.db.connect() as conn:
                    current = gateway.a2a._load(conn, params.id, principal)
            for row in rows:
                task, event = self._event(row)
                after = row["aggregate_revision"]
                gateway.connection_trust.check(principal)
                yield event
                if task.status.state != t.TaskState.TASK_STATE_WORKING:
                    for item in task.artifacts:
                        gateway.connection_trust.check(principal)
                        yield t.TaskArtifactUpdateEvent(task_id=task.id, context_id=task.context_id, artifact=item, last_chunk=True)
            if (current.status.state in TERMINAL or current.status.state == t.TaskState.TASK_STATE_INPUT_REQUIRED) and after >= int(metadata(current).get("aggregate_revision", 0)):
                return
            await asyncio.sleep(.1)

    async def on_create_task_push_notification_config(self, params, context):
        with self.authenticated(context, params) as (gateway, principal, _):
            return await run_in_threadpool(gateway.a2a.create_push, params, principal)

    async def on_get_task_push_notification_config(self, params, context):
        with self.authenticated(context, params) as (gateway, principal, _):
            gateway.a2a.get(params.task_id, principal)
            with gateway.db.connect() as conn:
                row = conn.execute("SELECT data FROM a2a_push_configs WHERE task_id=? AND id=?", (params.task_id, params.id)).fetchone()
            if row is None:
                raise TaskNotFoundError("Push configuration was not found")
            return proto(t.TaskPushNotificationConfig, json.loads(row[0]))

    async def on_list_task_push_notification_configs(self, params, context):
        with self.authenticated(context, params) as (gateway, principal, _):
            gateway.a2a.get(params.task_id, principal)
            try:
                offset, size = int(params.page_token or "0"), min(params.page_size or 50, 100)
                if offset < 0 or size < 1:
                    raise ValueError()
            except ValueError:
                raise InvalidParamsError("Invalid pagination") from None
            with gateway.db.connect() as conn:
                rows = conn.execute("SELECT data FROM a2a_push_configs WHERE task_id=? ORDER BY id", (params.task_id,)).fetchall()
            return t.ListTaskPushNotificationConfigsResponse(configs=[proto(t.TaskPushNotificationConfig, json.loads(r[0])) for r in rows[offset:offset+size]],
                next_page_token=str(offset + size) if offset + size < len(rows) else "")

    async def on_delete_task_push_notification_config(self, params, context):
        with self.authenticated(context, params) as (gateway, principal, _):
            gateway.a2a.get(params.task_id, principal)
            with gateway.db.connect(write=True) as conn:
                row = conn.execute("SELECT subscription_id FROM a2a_push_configs WHERE task_id=? AND id=?", (params.task_id, params.id)).fetchone()
                if row is None:
                    raise TaskNotFoundError("Push configuration was not found")
                gateway.webhooks.disable(row[0], principal)
                conn.execute("DELETE FROM a2a_push_configs WHERE task_id=? AND id=?", (params.task_id, params.id))

    async def on_get_extended_agent_card(self, params, context):
        with self.authenticated(context, params) as (gateway, principal, mandate):
            return await agent_card(gateway, audience=self.audience)


async def agent_card(gateway=None, base_url="http://localhost:8000", audience="buyer"):
    tools = {}
    if gateway is not None:
        from .mcp_server import create_server
        tools = {tool.name: tool for tool in await create_server(gateway).list_tools()}
    skills = []
    schemas = {}
    for skill, tool_name, description in (
        ("find_offers", "find_offers", "Find ranked live offers and fulfilment plans"),
        ("find_basket_offers", "find_basket_offers", "Find ingredient basket offers through the shared basket adapter"),
        ("quote_and_order", "create_quote", "Select a fixed-price plan and place an order under buyer authority"),
        ("order_status", "get_order_status", "Read task and purchasing state"),
        ("cancel_order", "cancel_order", "Cancel under the gateway order cancellation rules"),
        ("get_evidence", "get_order_evidence", "Retrieve signed quote, receipt and transaction evidence")):
        tool = tools.get(tool_name)
        skills.append(t.AgentSkill(id=skill, name=skill.replace("_", " "), description=tool.description if tool else description,
                                  tags=["procurement"], input_modes=["application/json"], output_modes=["application/json", "text/plain"]))
        if tool:
            schemas[skill] = {"input": tool.input_schema, "output": tool.output_schema}
    card = t.AgentCard(name="Valio Aimo Trusted Trade Agents", description="Fixed-price procurement under buyer authority; gateway-simulated commitments.",
        provider=t.AgentProvider(organization="Junction Trusted Trade Agents", url=base_url), version="1.0.0",
        supported_interfaces=[t.AgentInterface(url=base_url.rstrip("/") + "/a2a", protocol_binding="JSONRPC", protocol_version="1.0")],
        capabilities=t.AgentCapabilities(streaming=True, push_notifications=True),
        security_schemes={"BearerAuth": t.SecurityScheme(http_auth_security_scheme=t.HTTPAuthSecurityScheme(scheme="bearer"))},
        security_requirements=[t.SecurityRequirement(schemes={"BearerAuth": t.StringList()})] if hasattr(t, "StringList") else [],
        default_input_modes=["application/json"], default_output_modes=["application/json", "text/plain"], skills=skills)
    # SecurityRequirement map values are protobuf string-list messages.
    if not card.security_requirements:
        ParseDict({"schemes": {"BearerAuth": []}}, card.security_requirements.add())
    if schemas:
        card.capabilities.extensions.add(uri="urn:trusted-trade:procurement-command:v1", description="Shared MCP input/output schemas; command data part version=1", params={"schemas": schemas})
    ParseDict({"oauth2SecurityScheme": {"oauth2MetadataUrl": base_url+"/.well-known/oauth-authorization-server",
        "flows": {"authorizationCode": {"authorizationUrl": base_url+"/oauth/authorize", "tokenUrl": base_url+"/oauth/token", "scopes": {"*": "Consented gateway operations"}}}}}, card.security_schemes["OAuth2"])
    ParseDict({"schemes": {"OAuth2": {"list": ["*"]}}}, card.security_requirements.add())
    if audience != "buyer":
        from .human_tools import available
        del card.skills[:]
        card.name += " — " + audience
        card.supported_interfaces[0].url = base_url+"/a2a/"+audience
        card.capabilities.streaming = False
        card.capabilities.push_notifications = False
        del card.capabilities.extensions[:]
        for name, (model, _, _, description, _) in available(audience).items():
            card.skills.add(id=name, name=name.replace("_", " "), description=description, tags=[audience], input_modes=["application/json"], output_modes=["application/json"])
        if audience == "tenant":
            from .governance_api import Operation
            for operation in Operation.__args__:
                card.skills.add(id=operation, name=operation.replace("_", " "), description="Tenant governance operation; mutations require human confirmation.", tags=["tenant", "governance"])
        card.capabilities.extensions.add(uri="urn:trusted-trade:human-command:v1", description="Data part: version=1, command=skill id, arguments=object. Decisions return a five-minute single-use confirmation_token; retry with it.",
            params={"schemas": {name: model.model_json_schema() for name, (model, *_) in available(audience).items()}})
    from .agent_surface_cards import enrich_agent_card
    return await enrich_agent_card(card, gateway, audience)


def install_routes(app):
    @app.middleware("http")
    async def resource_auth(request, call_next):
        path = request.url.path
        if path in {"/a2a", "/a2a/tenant", "/a2a/operator"}:
            from .accounts import origin
            from .oauth import require_audience
            from fastapi.responses import JSONResponse
            scheme, _, secret = request.headers.get("authorization", "").partition(" ")
            try:
                p = admit_credential(request.app.state.gateway, request.app.state.keys,
                    request.client.host if request.client else "unknown", secret if scheme.lower() == "bearer" else "")
                require_audience(p, path.split("/")[-1] if path != "/a2a" else "buyer", path, origin(request))
            except DomainError as error:
                headers = {"WWW-Authenticate": f'Bearer resource_metadata="{origin(request)}/.well-known/oauth-protected-resource{path}"'} if error.status == 401 else {}
                return JSONResponse(problem(error, path), status_code=error.status, headers=headers, media_type="application/problem+json")
        return await call_next(request)

    @app.get("/.well-known/agent-card.json", response_model=dict, summary="A2A 1.0 Agent Card generated from shared MCP schemas")
    async def card(request: Request):
        from .agent_surface_cards import card_response
        return await card_response(request)

    add_a2a_routes_to_fastapi(app, jsonrpc_routes=create_jsonrpc_routes(Handler(), rpc_url="/a2a", context_builder=ContextBuilder()))
    for audience in ("tenant", "operator"):
        def card_endpoint(selected):
            async def get(request: Request):
                from .agent_surface_cards import card_response
                return await card_response(request, selected)
            return get
        app.add_api_route("/a2a/"+audience+"/.well-known/agent-card.json", card_endpoint(audience), methods=["GET"], response_model=dict, tags=["a2a"])
        if audience == "tenant":
            for alias in ("/a2a/tenant/agent-card.json", "/.well-known/agent-card/tenant.json"):
                app.add_api_route(alias, card_endpoint(audience), methods=["GET"], response_model=dict, tags=["a2a"])
        add_a2a_routes_to_fastapi(app, jsonrpc_routes=create_jsonrpc_routes(Handler(audience), rpc_url="/a2a/"+audience, context_builder=ContextBuilder()))
    for route in app.routes:
        if getattr(route, "path", None) in {"/a2a", "/a2a/tenant", "/a2a/operator"}:
            route.openapi_extra["security"] = [{"BearerAuth": []}, {"OAuth2": ["*"]}]
            route.openapi_extra["responses"] = {"200": {"description": "A2A 1.0 JSON-RPC response, or SSE StreamResponse events.",
                "content": {"application/json": {"schema": {"type": "object"}}, "text/event-stream": {"schema": {"type": "string"}}}}}


def human_send(gateway, principal, audience, params):
    """A2A human commands use REST's two-step decision and the existing task store."""
    from .human_tools import execute
    from .commands import json_value
    from .tenant_api import DecisionPreview
    parts = [MessageToDict(part)["data"] for part in params.message.parts if part.WhichOneof("content") == "data"]
    if len(parts) == 1 and parts[0].get("command") == "tool":
        from .agent_surface_interpret import a2a_skill
        return a2a_skill(gateway, params, principal, audience)
    if len(parts) == 1 and audience == "tenant" and "operation" in parts[0]:
        from .governance_api import invoke
        command = command_data(parts[0])
        value = invoke(gateway, principal, command.get("operation"), command.get("arguments", {}), command.get("confirmation_token"))
        result = json_value(value)
        return t.Message(message_id=uuid4().hex, role=t.Role.ROLE_AGENT,
                         parts=[data_part(result if isinstance(result, dict) else {"items": result})])
    if len(parts) != 1 or parts[0].get("version") != 1:
        raise DomainError("VALIDATION_ERROR", "Supply one version=1 command data part", 422)
    command = parts[0]
    task_id = params.message.task_id or uuid4().hex
    if params.message.task_id:
        with gateway.db.connect() as conn:
            task = gateway.a2a._load(conn, task_id, principal)
        if metadata(task).get("audience") != audience:
            raise DomainError("TASK_NOT_FOUND", "Task was not found", 404)
    else:
        task = t.Task(id=task_id, context_id=params.message.context_id or uuid4().hex)
    # Do not surround confirm_rest with another transaction: evidence refresh
    # must commit even when a decision returns EVIDENCE_CHANGED.
    try:
        value = execute(gateway, principal, audience, command.get("command"), command.get("arguments", {}))
    except ValidationError as error:
        raise validation_problem(error) from None
    preview = isinstance(value, DecisionPreview)
    task.metadata.update({"owner": principal.id, "owner_scope": f"{principal.kind}:{principal.id}", "audience": audience})
    task.status.CopyFrom(t.TaskStatus(state=t.TaskState.TASK_STATE_INPUT_REQUIRED if preview else t.TaskState.TASK_STATE_COMPLETED,
        message=status_message(task_id, "Confirm the human decision" if preview else "Command completed", json_value(value))))
    task.status.timestamp.FromDatetime(audit.utcnow())
    artifact(task, "decision" if preview else "result", json_value(value))
    with gateway.db.connect(write=True) as conn:
        gateway.a2a._save(conn, task, principal.id)
    return task
