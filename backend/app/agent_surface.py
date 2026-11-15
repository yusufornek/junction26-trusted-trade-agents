"""Spec 60: one authenticated registry for REST, MCP, resources and workflows.

The REST adapter uses FastAPI's dependency solver, not an HTTP loopback or a
second implementation of procurement rules. Only credential resolution is
replaced: the already authenticated MCP principal still passes the REST scope,
target, role, validation and command guards.
"""
import asyncio
import inspect
import keyword
import json
import re
import weakref
from copy import copy
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.dependencies.utils import solve_dependencies
from fastapi.routing import APIRoute, run_endpoint_function
from mcp import types as mt
from mcp.server.mcpserver import Context
from mcp.shared.subscriptions import ResourceUpdated
from pydantic import BaseModel, ValidationError
from starlette.responses import Response

from . import audit
from .agent_context import agent_context
from .auth import Principal, check_scope, require_operator, require_reviewer, resolve_principal
from .commands import json_value
from .db import DomainError
from .foundation import PRINCIPAL
from .recovery import problem, validation_problem

PRIVATE = "private, no-store"


def argument_name(value):
    name = value.replace("-", "_")
    return name + "_" if keyword.iskeyword(name) else name
PROMPTS = {
    "order_for_need": "interpret_request → confirm_interpretation → find_offers → choose_offer → create_quote → place_order → get_order_status",
    "event_tomorrow": "get_agent_context → find_basket_offers → choose_offer → create_quote → place_order → get_order_status",
    "replace_out_of_stock": "get_product → find_offers (substitute_for) → confirm_substitute → create_quote → place_order",
    "weekly_replenishment": "get_suggestions → prepare_suggestion → find_offers → create_quote → place_order",
    "review_my_spend": "get_spend → get_remaining_authority → list_orders → query_audit",
    "explain_an_order": "get_order_status → get_order_timeline → get_order_evidence",
}
# Stable names retain the established client vocabulary. All other REST operations
# get a deterministic name from their endpoint and path, including future routes.
NAMES = {
    ("GET", "/v1/me"): "get_agent_context",
    ("POST", "/v1/offers"): "find_offers",
    ("POST", "/v1/basket-lookups"): "find_basket_offers",
    ("GET", "/v1/products/{id}"): "get_product",
    ("GET", "/v1/lookups/{lookup_id}"): "get_lookup",
    ("GET", "/v1/basket-lookups/{lookup_id}"): "get_basket_lookup",
    ("POST", "/v1/quotes"): "create_quote",
    ("GET", "/v1/quotes/{id}"): "get_quote",
    ("POST", "/v1/orders"): "place_order",
    ("GET", "/v1/orders"): "list_orders",
    ("GET", "/v1/orders/{id}"): "get_order_status",
    ("GET", "/v1/orders/{id}/receipt"): "get_order_receipt",
    ("GET", "/v1/orders/{id}/ubl"): "get_order_ubl",
    ("GET", "/v1/orders/{order_id}/timeline"): "get_order_timeline",
    ("POST", "/v1/orders/{order_id}/cancel"): "cancel_order",
    ("GET", "/v1/audit"): "query_audit",
    ("GET", "/v1/buyer/proposal-preferences"): "get_proposal_preferences",
    ("PUT", "/v1/buyer/proposal-preferences"): "set_proposal_preferences",
    ("GET", "/v1/buyer/memory-preferences"): "get_memory_preferences",
    ("PUT", "/v1/buyer/memory-preferences"): "set_memory_preferences",
    ("GET", "/v1/webhook-subscriptions"): "list_webhook_subscriptions",
    ("POST", "/v1/webhook-subscriptions"): "create_webhook_subscription",
    ("PATCH", "/v1/webhook-subscriptions/{subscription_id}"): "update_webhook_subscription",
    ("DELETE", "/v1/webhook-subscriptions/{subscription_id}"): "disable_webhook_subscription",
    ("GET", "/v1/proposals/inbox"): "list_proposals",
    ("GET", "/v1/suggestions"): "get_replenishment_suggestions",
}


def canonical_origin(gateway):
    value = (gateway.settings.public_url or "http://localhost:8000").rstrip("/")
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.netloc or parts.username or parts.password or parts.query or parts.fragment or parts.path:
        raise ValueError("PUBLIC_URL must be a canonical HTTP(S) origin")
    return value


def effective_audience(p, default="buyer"):
    if default == "buyer" and p and p.kind in {"tenant_user", "platform_admin", "buyer_user"}:
        return "operator" if p.operator else "tenant"
    return default


def dependencies(route):
    pending, calls = [route.dependant], set()
    while pending:
        item = pending.pop()
        calls.add(item.call)
        pending.extend(item.dependencies)
    return calls


def rest_routes(app):
    # FastAPI 0.135+ keeps included routers lazy; enumerate their effective
    # contexts so prefixed routes and router-level dependencies are preserved.
    from fastapi.routing import iter_route_contexts
    for context in iter_route_contexts(app.routes):
        if isinstance(context.original_route, APIRoute):
            route = copy(context.original_route)
            route.path = context.path
            route.dependant = context.dependant
            route._embed_body_fields = context._embed_body_fields
            yield route


def route_audiences(route):
    """Discover protected business capabilities, excluding transport/auth plumbing."""
    if not isinstance(route, APIRoute) or not route.include_in_schema or not route.path.startswith("/v1/"):
        return set()
    p = route.path
    if p.startswith(("/v1/auth/", "/v1/oauth/", "/v1/connect/tools/", "/v1/connect/tool-pack")):
        return set()
    if p.startswith("/v1/operator/"):
        return {"operator"}
    if p.startswith(("/v1/buyer/memory", "/v1/buyer/proposal")):
        return {"buyer", "tenant"}
    if p.startswith(("/v1/tenant/", "/v1/buyer/")):
        return {"tenant"}
    guards = dependencies(route)
    if require_operator in guards or require_reviewer in guards or p.startswith(("/v1/connections", "/v1/sandbox")):
        return {"operator"}
    if resolve_principal in guards:
        return {"buyer"}
    return set()


def expand_schema(value, components, seen=()):
    if isinstance(value, list):
        return [expand_schema(v, components, seen) for v in value]
    if not isinstance(value, dict):
        return value
    if "$ref" in value:
        key = value["$ref"].split("/")[-1]
        if key in seen:
            # Preserve recursive definitions locally rather than OpenAPI refs.
            return {}
        return expand_schema({**components[key], **{k: v for k, v in value.items() if k != "$ref"}}, components, (*seen, key))
    return {k: expand_schema(v, components, seen) for k, v in value.items()}


def local_schema(value, components):
    """Keep reusable definitions local to each SDK schema, including recursion."""
    definitions = {}
    def visit(node):
        if isinstance(node, list):
            return [visit(v) for v in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node and node["$ref"].startswith("#/components/schemas/"):
            key = node["$ref"].split("/")[-1]
            if key not in definitions:
                definitions[key] = {}  # Mark before descending into recursive models.
                definitions[key] = visit(components[key])
            return {**{k: visit(v) for k, v in node.items() if k != "$ref"}, "$ref": "#/$defs/" + key}
        return {k: visit(v) for k, v in node.items()}
    result = visit(value)
    if definitions:
        result["$defs"] = definitions
    return result


@dataclass
class Capability:
    route: Any
    method: str
    name: str
    input_schema: dict
    output_schema: dict

    @property
    def key(self):
        return self.method + " " + self.route.path


class AgentSurface:
    def __init__(self, gateway):
        self.gateway = gateway
        self.app = None
        self.registry = None
        self.servers = weakref.WeakSet()
        self.subscribers = []
        if not hasattr(gateway, "db"):
            return  # Schema-only registration never initializes runtime storage.
        with gateway.db.connect(write=True, notify=False) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS agent_surface_calls (
                    connection_id TEXT NOT NULL, ts TEXT NOT NULL, tool TEXT NOT NULL, code TEXT
                );
                CREATE INDEX IF NOT EXISTS agent_surface_calls_window ON agent_surface_calls(connection_id,ts);
                CREATE TABLE IF NOT EXISTS agent_surface_drafts (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, purpose TEXT NOT NULL,
                    data TEXT NOT NULL, expires_at TEXT NOT NULL, used_at TEXT
                );
            """)
        gateway.outbox.register("agent-surface", self.changed)

    def rest_app(self):
        if self.app is None:
            from .api import create_app
            from .auth import Credentials
            if hasattr(self.gateway, "db"):
                self.app = create_app(self.gateway, keys=Credentials("schema-only"), serve_mcp=False)
            else:
                self.app = create_app(settings=self.gateway.settings, keys=Credentials("schema-only"), serve_mcp=False)
            self.app.state.gateway = self.gateway
        return self.app

    def capabilities(self, audience):
        if self.registry is None:
            app = self.rest_app()
            schema = app.openapi()
            components = schema.get("components", {}).get("schemas", {})
            registry = {a: [] for a in ("buyer", "tenant", "operator")}
            used = {a: set() for a in registry}
            for route in rest_routes(app):
                for a in route_audiences(route):
                    for method in sorted(route.methods & {"GET", "POST", "PUT", "PATCH", "DELETE"}):
                        op = schema["paths"][route.path][method.lower()]
                        name = NAMES.get((method, route.path), route.name)
                        name = re.sub(r"\W+", "_", name)
                        if name in used[a]:
                            name += "_" + re.sub(r"\W+", "_", route.path.strip("/"))
                        used[a].add(name)
                        properties, required = {}, []
                        for param in op.get("parameters", []):
                            if param["in"] in {"path", "query"} or param["name"].lower() == "idempotency-key":
                                key = argument_name(param["name"].lower() if param["in"] == "header" else param["name"])
                                properties[key] = param["schema"]
                                if param.get("required"):
                                    required.append(key)
                        body = op.get("requestBody", {})
                        if body:
                            properties["body"] = body.get("content", {}).get("application/json", {}).get("schema", {})
                            if body.get("required"):
                                required.append("body")
                        response = next((r for code, r in op.get("responses", {}).items() if code.startswith("2")), {})
                        output = response.get("content", {}).get("application/json", {}).get("schema", {})
                        shape = components.get(output.get("$ref", "").split("/")[-1], output)
                        if shape.get("type") in {"array", "string", "number", "integer", "boolean", "null"}:
                            output = {"type": "object", "properties": {"result": output}, "required": ["result"]}
                        else:
                            output = {"type": "object", **output}
                        if "application/zip" in response.get("content", {}):
                            # The same REST capability can return a manifest,
                            # XML/markdown artifact, or base64 ZIP envelope.
                            output = {"type": "object", "additionalProperties": True}
                        registry[a].append(Capability(route, method, name,
                            local_schema({"type": "object", "properties": properties, "required": required, "additionalProperties": False}, components),
                            local_schema(output, components)))
            self.registry = registry
        return self.registry[audience]

    def visible(self, cap, p):
        if p is None or not hasattr(self.gateway, "db"):
            return True
        try:
            self.gateway.accounts.guard(p, cap.route.path, cap.method)
            return True
        except DomainError:
            return False

    async def invoke(self, cap, arguments, p, mandate=None):
        from jsonschema import Draft202012Validator
        errors = list(Draft202012Validator(cap.input_schema).iter_errors(arguments))
        if errors:
            raise DomainError("VALIDATION_ERROR", "; ".join(
                (".".join(map(str, e.absolute_path)) or "request") + ": failed " + str(e.validator)
                for e in errors), 422)
        target = cap.route.path
        params = {}
        for field in cap.route.dependant.path_params:
            params[field.alias] = str(arguments[argument_name(field.alias)])
            target = target.replace("{" + field.alias + "}", params[field.alias])
        query = {f.alias: arguments[argument_name(f.alias)] for f in cap.route.dependant.query_params if argument_name(f.alias) in arguments and arguments[argument_name(f.alias)] is not None}
        headers = []
        if arguments.get("idempotency_key"):
            headers.append((b"idempotency-key", arguments["idempotency_key"].encode()))
        if mandate:
            headers.append((b"agent-mandate", mandate.encode()))
        base = urlsplit(canonical_origin(self.gateway))
        request = Request({"type": "http", "app": self.rest_app(), "path": target, "raw_path": target.encode(),
            "path_params": params, "method": cap.method, "query_string": urlencode(query, doseq=True).encode(),
            "headers": headers, "scheme": base.scheme, "server": (base.hostname, base.port or (443 if base.scheme == "https" else 80)),
            "client": ("mcp", 0)})

        async def authenticated():
            from .handoff_delegated_surfaces import scope_path
            self.gateway.accounts.guard(p, scope_path(target), cap.method)
            self.gateway.accounts.guard_target(request, p)
            from .handoff_delegated_surfaces import rest_principal
            rest_principal(self.gateway, p, request)
            if p.kind == "agent":
                self.gateway.governance.check_active(p.id)
            request.state.principal = p
            return p

        async with AsyncExitStack() as stack:
            # FastAPI owns all other dependencies and input coercion. No global
            # app.dependency_overrides mutation: concurrent principals stay isolated.
            request.scope["fastapi_inner_astack"] = stack
            request.scope["fastapi_function_astack"] = stack
            solved = await solve_dependencies(request=request, dependant=cap.route.dependant,
                body=arguments.get("body"), dependency_overrides_provider=SimpleNamespace(
                    dependency_overrides={resolve_principal: authenticated}),
                async_exit_stack=stack, embed_body_fields=cap.route._embed_body_fields)
            if solved.errors:
                raise DomainError("VALIDATION_ERROR", "Correct missing or invalid request fields", 422)
            from .connection_contract_surface import apply_explicit_nulls
            apply_explicit_nulls(cap, arguments, solved.values)
            value = await run_endpoint_function(dependant=cap.route.dependant, values=solved.values,
                                               is_coroutine=inspect.iscoroutinefunction(cap.route.endpoint))
            if isinstance(value, Response):
                body = value.body
                media = value.media_type or "application/octet-stream"
                if "json" in media:
                    value = json.loads(body)
                elif media.startswith("text/") or "xml" in media:
                    value = body.decode()
                else:
                    import base64
                    value = {"result": base64.b64encode(body).decode(), "media_type": media, "encoding": "base64"}
            value = json_value(value)
            return value if isinstance(value, dict) else {"result": value}

    def note(self, p, name, result):
        if not p or not p.connection_id:
            return
        code = None
        if isinstance(result, mt.CallToolResult) and result.is_error:
            code = (result.structured_content or {}).get("code", "TOOL_ERROR")
        with self.gateway.db.connect(write=True, notify=False) as conn:
            conn.execute("INSERT INTO agent_surface_calls(connection_id,ts,tool,code) VALUES(?,?,?,?)",
                         (p.connection_id, audit.utcnow().isoformat(), name, code))

    def connection_principal(self, row):
        target = Principal(row["agent_id"], customer_ids=tuple(json.loads(row["customer_ids"])),
            tenant_id=row["tenant_id"], connection_id=row["connection_id"], credential_ref=row["connection_id"],
            audience=row["audience"], scopes=tuple(json.loads(row["scopes"])),
            user_id=row["created_by"] if row["audience"] != "buyer" else None)
        return self.gateway.accounts.bind(target)

    def health(self, cid, p):
        if p.connection_id == cid:
            from .connections import public_connection
            with self.gateway.db.connect() as conn:
                row = conn.execute("SELECT * FROM connections WHERE connection_id=?", (cid,)).fetchone()
            if row is None:
                raise DomainError("CONNECTION_NOT_FOUND", "Connection was not found", 404)
            connection = public_connection(row)
        else:
            connection = self.gateway.connections.get(cid, p)
        with self.gateway.db.connect() as conn:
            row = conn.execute("SELECT * FROM connections WHERE connection_id=?", (cid,)).fetchone()
            calls = conn.execute("SELECT tool,code,COUNT(*) n FROM agent_surface_calls WHERE connection_id=? AND ts>=? GROUP BY tool,code",
                                 (cid, (audit.utcnow() - timedelta(hours=24)).isoformat())).fetchall()
            last = conn.execute("SELECT MAX(ts) FROM agent_surface_calls WHERE connection_id=?", (cid,)).fetchone()[0]
        by_tool, codes = {}, set()
        for call in calls:
            by_tool[call["tool"]] = by_tool.get(call["tool"], 0) + call["n"]
            if call["code"]:
                codes.add(call["code"])
        current = self.connection_principal(row)
        from .connection_contract_driving import driving
        return {"connection_id": cid, "last_used_at": last or connection.last_used_at,
                "notifications": self.gateway.notifications.health(cid),
                "trust": self.gateway.connection_trust.health(cid),
                "calls_last_24h": by_tool, "error_codes": sorted(codes),
                "connection_contract": driving(self.gateway).status(current) if current.kind == "agent" else None,
                "scope": {"audience": current.audience, "scopes": list(current.scopes),
                          "customer_ids": list(current.customer_ids), "tenant_id": current.tenant_id, "all_tenants": current.operator}}

    def changed(self, event):
        """Outbox seam for spec 61; notify only live, re-authorized subscribers."""
        from .connection_notifications import classify
        notification_kind = classify(event)
        uris = {"luotsi://agent/context", "luotsi://policy/effective", "luotsi://approvals/pending", "luotsi://inbox"}
        oid = event.get("aggregate_id") if event.get("aggregate_type") == "order" else event.get("payload", {}).get("order_id")
        if oid:
            uris.update(f"luotsi://orders/{oid}{suffix}" for suffix in ("", "/timeline", "/evidence"))
        product_id = event.get("aggregate_id") if event.get("aggregate_type") in {"product", "catalog_product"} else event.get("payload", {}).get("product_id")
        if product_id:
            uris.add(f"luotsi://catalog/products/{product_id}")
        published = set()
        for loop, server, p, uri, session in list(self.subscribers):
            if p.connection_id and notification_kind:
                continue  # Consent/rate/quiet-hours-aware worker owns these updates.
            if uri not in uris or loop.is_closed():
                continue
            key = (id(server), p.connection_id, p.kind, p.id, uri)
            if session is None and key in published:
                continue
            published.add(key)
            async def notify(server=server, p=p, uri=uri, session=session):
                token = PRINCIPAL.set(p)
                try:
                    self.gateway.connection_trust.check(p)
                    # Re-read before each notification; revoked credentials and
                    # changed scope cannot leak updates through a long lived session.
                    current = self.gateway.accounts.bind(p)
                    if p.connection_id:
                        with self.gateway.db.connect() as conn:
                            row = conn.execute("SELECT * FROM connections WHERE connection_id=?", (p.connection_id,)).fetchone()
                        if not row or row["revoked_at"] or row["expires_at"] and row["expires_at"] <= audit.utcnow().isoformat():
                            return
                        current = self.gateway.accounts.bind(replace(p, scopes=tuple(json.loads(row["scopes"])), customer_ids=tuple(json.loads(row["customer_ids"]))))
                    PRINCIPAL.set(current)
                    if event.get("customer_id") and event["customer_id"] not in current.customer_ids and not current.operator:
                        return
                    await read_uri(server, uri)
                    if session is not None:
                        await session.send_resource_updated(uri)
                    else:
                        await server._subscriptions.publish(ResourceUpdated(uri=uri))
                except (DomainError, RuntimeError, ValueError):
                    return
                finally:
                    PRINCIPAL.reset(token)
            loop.call_soon_threadsafe(lambda notify=notify: asyncio.create_task(notify()))


def surface(gateway):
    if not hasattr(gateway, "agent_surface"):
        gateway.agent_surface = AgentSurface(gateway)
    return gateway.agent_surface


def principal(server):
    p = PRINCIPAL.get()
    if p is None and getattr(server, "local_credential", None):
        from .auth import resolve_credential
        from .oauth import require_audience
        p = resolve_credential(server.keys, server.local_credential, server.gateway)
        require_audience(p, server.audience, "/mcp" + ("" if server.audience == "buyer" else "/" + server.audience))
    if p is None and server.agent_id:
        policy = server.gateway.policy.get()
        row = policy.agents.get(server.agent_id)
        identity = policy.identities.get(server.agent_id)
        org = identity.organisation_id if identity else None
        customers = policy.organisations[org].customer_ids if org in policy.organisations else row.customer_ids if row else ()
        p = server.gateway.accounts.bind(Principal(server.agent_id,
            kind=identity.kind if identity else "agent", organisation_id=org,
            role=identity.role if identity else "agent", customer_ids=tuple(customers),
            credential_ref="stdio:" + server.agent_id))
    if p is None:
        raise DomainError("UNAUTHENTICATED", "An authenticated principal is required", 401)
    return p


def discovery_metadata(server, items):
    p = PRINCIPAL.get()
    if p is None or not hasattr(server.gateway, "db"):
        return items
    from .agent_surface_cards import discovery_context
    info = discovery_context(server.gateway, effective_audience(p, server.audience))
    return [item.model_copy(update={"meta": {**(getattr(item, "meta", None) or {}), "discovery": info}}) for item in items]


def effective_mandate(server):
    from .mcp_server import PRESENTED_MANDATE
    return PRESENTED_MANDATE.get() or server.mandate


async def read_uri(server, uri):
    if str(uri) == "luotsi://trading-profile":
        from .connection_contract import manager
        m = manager(server.gateway)
        return {"contract": m.public(), "profile": m.profile()}
    gateway, p = server.gateway, principal(server)
    gateway.connection_trust.check(p)
    uri = str(uri)
    if uri in {"luotsi://agent/context", "luotsi://policy/effective"}:
        check_scope(p, "read", "read")
        value = agent_context(gateway, p, effective_mandate(server)).model_dump(mode="json")
        if uri.endswith("effective"):
            value = {k: value[k] for k in ("effective_limits", "remaining_budget", "standing_constraints", "mandate")}
            if p.kind == "agent":
                effective = gateway.governance.resolve(p.agent_id)
                value.update(policy_version=effective.version_id, policy=effective.model_dump(mode="json"))
            else:
                from .human_tools import execute
                value["policy"] = json_value(execute(gateway, p, "operator" if p.operator else "tenant", "get_policy", {}))
        return value
    match = re.fullmatch(r"luotsi://orders/([^/]+)(/timeline|/evidence)?", uri)
    if match:
        check_scope(p, "orders", "read")
        oid, suffix = match.groups()
        if suffix == "/timeline":
            return json_value(gateway.orders.timeline(oid, p.owner))
        if suffix == "/evidence":
            from .evidence import build_bundle
            return build_bundle(gateway, oid, p)[0]
        return json_value(gateway.orders.get_order(oid, p.owner))
    match = re.fullmatch(r"luotsi://catalog/products/([^/]+)", uri)
    if match:
        check_scope(p, "products", "read")
        gateway.policy.check_supplier(p.id, "valio")
        return json_value(gateway.catalog.get(match[1]))
    if uri == "luotsi://approvals/pending":
        check_scope(p, "approvals", "read")
        return {"orders": json_value([o for o in gateway.orders.list_orders(agent_id=p.owner) if o.status == "PENDING_APPROVAL"])}
    if uri == "luotsi://inbox":
        if p.connection_id:
            from .connection_notifications_surface import inbox
            return {**inbox(gateway, p, effective_mandate(server)),
                    "connection_notifications": gateway.connection_trust.inbox(p)}
        check_scope(p, "proposals", "read")
        return {"connection_notifications": gateway.connection_trust.inbox(p),
                "proposals": gateway.proposals.inbox(p, effective_mandate(server)),
                "stockout_alerts": gateway.stockout_watch.inbox(p), "suggestions": gateway.memory.suggestions(p)}
    raise DomainError("HTTP_ERROR", "Unknown Luotsi resource", 404)


def next_tool(name):
    if name.startswith(("find_", "search_", "compare_")):
        return "choose_offer, then create_quote"
    if name == "create_quote":
        return "place_order"
    if name in {"place_order", "cancel_order", "approve_order", "reject_order"}:
        return "get_order_status"
    return "get_agent_context to review current authority before further actions"


class ScopedSubscriptionBus:
    """SDK fan-out isolated by authenticated connection, including shared URIs."""
    def __init__(self):
        from mcp.server.subscriptions import InMemorySubscriptionBus
        self.bus = InMemorySubscriptionBus()

    @staticmethod
    def identity():
        p = PRINCIPAL.get()
        return (p.kind, p.id, p.tenant_id, p.connection_id) if p else None

    def subscribe(self, callback):
        identity = self.identity()
        def scoped(event):
            if self.identity() == identity:
                callback(event)
        return self.bus.subscribe(scoped)

    async def publish(self, event):
        await self.bus.publish(event)


def quality(server):
    for name, tool in server._tool_manager._tools.items():
        cap = getattr(server, "surface_capabilities", {}).get(name)
        read = cap.method == "GET" if cap else name.startswith(("get_", "list_", "search_", "query_", "compare_", "verify_"))
        destructive = name.startswith(("cancel_", "revoke_", "reject_", "delete_", "disable_", "reset_", "set_", "update_", "put_")) or cap is not None and cap.method == "DELETE"
        tool.title = name.replace("_", " ").title()
        tool.annotations = mt.ToolAnnotations(read_only_hint=read, destructive_hint=bool(destructive),
            idempotent_hint=read or name in {"place_order", "cancel_order"},
            open_world_hint=name in {"find_offers", "find_basket_offers", "create_quote", "place_order"} or "webhook" in name)
        tool.description = (tool.description or tool.title) + (
            f" Use this tool when you need to {tool.title.lower()}. Next call {next_tool(name)}. "
            "Recovery codes: VALIDATION_ERROR (correct fields), INSUFFICIENT_SCOPE or WRONG_AUDIENCE (reconnect with consent), "
            "ORDER_NOT_FOUND/LOOKUP_NOT_FOUND/QUOTE_NOT_FOUND (check your own reference), MANDATE_INVALID/AGENT_PAUSED (contact administrator), "
            "RATE_LIMITED/UPSTREAM_BUSY (retry after supplied interval), EVIDENCE_CHANGED (review refreshed evidence), "
            "EXPIRED_QUOTE (create_quote again), IDEMPOTENCY_CONFLICT (restore original payload). Follow next_action on every domain error.")
        if tool.fn_metadata.output_schema is None:
            tool.fn_metadata.output_schema = {"type": "object", "additionalProperties": True}


def register(server):
    gateway, a = server.gateway, server.audience
    s = surface(gateway)
    s.servers.add(server)
    from .agent_surface_http import SessionPrincipal
    from mcp.server.subscriptions import ListenHandler
    server._lowlevel_server.middleware.append(SessionPrincipal(server))
    server._subscriptions = ScopedSubscriptionBus()
    server._lowlevel_server.add_request_handler("subscriptions/listen", mt.SubscriptionsListenRequestParams, ListenHandler(server._subscriptions))
    server.surface_capabilities = {}
    server.surface_generated = set()
    for cap in s.capabilities(a):
        server.surface_capabilities[cap.name] = cap
        if cap.name in server._tool_manager._tools:
            continue
        server.surface_generated.add(cap.name)

        def build(cap):
            async def adapter(ctx: Context, **arguments) -> dict[str, Any]:
                from .mcp_server import PRESENTED_MANDATE
                from .agent_surface_interpret import invoke_generated
                from .connection_contract_surface import supplied_arguments
                arguments = supplied_arguments(arguments)
                return await invoke_generated(server, cap, arguments, ctx, PRESENTED_MANDATE.get())
            adapter.__signature__ = inspect.Signature([inspect.Parameter("ctx", inspect.Parameter.KEYWORD_ONLY, annotation=Context), *[
                inspect.Parameter(k, inspect.Parameter.KEYWORD_ONLY, annotation=Any,
                    default=inspect.Parameter.empty if k in cap.input_schema.get("required", []) else None)
                for k in cap.input_schema["properties"]]], return_annotation=dict[str, Any])
            server.tool(name=cap.name, description=f"REST capability {cap.key}. " + (cap.route.description or cap.route.summary or cap.name))(adapter)
            tool = server._tool_manager._tools[cap.name]
            tool.parameters = cap.input_schema
            # The adapter has already serialized models and wraps non-object REST
            # responses in result; publish precisely that schema via SDK metadata.
            tool.fn_metadata.output_schema = cap.output_schema
            return adapter
        build(cap)

    if a == "buyer":
        @server.tool(description="Compare the stored fulfilment plans for your own lookup without contacting a supplier.")
        def compare_plans(lookup_id: str) -> dict[str, Any]:
            lookup = gateway.lookup.get(lookup_id, principal(server).owner)
            return {"lookup_id": lookup_id, "plans": json_value(lookup.plans)}

        @server.tool(description="Read your standing constraints, spend and remaining purchasing authority.")
        def get_standing_constraints() -> dict[str, Any]:
            return agent_context(gateway, principal(server), effective_mandate(server)).standing_constraints

        @server.tool(description="Read your rolling spending and remaining authority before a commitment.")
        def get_remaining_authority() -> dict[str, Any]:
            return agent_context(gateway, principal(server), effective_mandate(server)).model_dump(mode="json")

        @server.tool(description="Read your spend and remaining budget for the rolling 24-hour authority window.")
        def get_spend() -> dict[str, Any]:
            return agent_context(gateway, principal(server), effective_mandate(server)).remaining_budget

        @server.tool(description="Read pending approval status of your own orders; humans decide through their audience.")
        def list_approvals() -> dict[str, Any]:
            return {"orders": json_value([o for o in gateway.orders.list_orders(agent_id=principal(server).owner) if o.status == "PENDING_APPROVAL"])}

        @server.tool(description="Query your order audit history, filtered by order or quote; never returns another buyer's records.")
        def query_audit(order_id: str | None = None, quote_id: str | None = None) -> dict[str, Any]:
            own = gateway.orders.list_orders(agent_id=principal(server).owner)
            ids = {o.id for o in own}
            if order_id and order_id not in ids:
                raise DomainError("ORDER_NOT_FOUND", "Order was not found", 404)
            entries = gateway.audit(order_id, quote_id)
            return {"entries": [json_value(e) for e in entries if e.order_id in ids]}

        from .agent_surface_interpret import register as register_interpretation
        register_interpretation(server)

    register_resources(server)
    from .connection_notifications_surface import register as register_notifications
    register_notifications(server)
    register_prompts(server)
    from .connection_contract_surface import register as register_contract
    register_contract(server)
    quality(server)
    from .connection_contract_surface import capture
    capture(server)
    for name, cap in server.surface_capabilities.items():
        tool = server._tool_manager._tools[name]
        tool.meta = {**(tool.meta or {}), "restCapabilities": [cap.key]}


def register_resources(server):
    for uri in ("luotsi://agent/context", "luotsi://policy/effective", "luotsi://approvals/pending", "luotsi://inbox"):
        def build(uri):
            async def resource() -> str:
                return json.dumps(await read_uri(server, uri), default=str)
            server.resource(uri, name=uri.removeprefix("luotsi://"), mime_type="application/json")(resource)
        build(uri)
    for suffix in ("", "/timeline", "/evidence"):
        def build(suffix):
            async def order(id: str) -> str:
                return json.dumps(await read_uri(server, f"luotsi://orders/{id}{suffix}"), default=str)
            server.resource("luotsi://orders/{id}" + suffix, mime_type="application/json")(order)
        build(suffix)

    @server.resource("luotsi://catalog/products/{id}", mime_type="application/json")
    async def product(id: str) -> str:
        return json.dumps(await read_uri(server, f"luotsi://catalog/products/{id}"), default=str)

    async def subscribe(ctx, params):
        uri = str(params.uri)
        await read_uri(server, uri)
        s = surface(server.gateway)
        entry = (asyncio.get_running_loop(), server, principal(server), uri, ctx.session)
        if entry not in s.subscribers:
            s.subscribers.append(entry)
        return mt.EmptyResult()

    async def unsubscribe(ctx, params):
        s = surface(server.gateway)
        s.subscribers[:] = [v for v in s.subscribers if not (v[4] is ctx.session and v[3] == str(params.uri))]
        return mt.EmptyResult()

    # Use the SDK's legacy subscription handlers and modern ListenHandler, with
    # authorization added before the SDK opens a resource notification stream.
    low = server._lowlevel_server
    low.add_request_handler("resources/subscribe", mt.SubscribeRequestParams, subscribe)
    low.add_request_handler("resources/unsubscribe", mt.UnsubscribeRequestParams, unsubscribe)

    async def listen(ctx, params):
        entries = []
        s = surface(server.gateway)
        for uri in params.notifications.resource_subscriptions or []:
            await read_uri(server, uri)
            entry = (asyncio.get_running_loop(), server, principal(server), str(uri), None)
            entries.append(entry)
            s.subscribers.append(entry)
        token = PRINCIPAL.set(principal(server))
        try:
            from .connection_trust_transports import listen as trusted_listen
            return await trusted_listen(server, ctx, params)
        finally:
            PRINCIPAL.reset(token)
            for entry in entries:
                if entry in s.subscribers:
                    s.subscribers.remove(entry)
    low.add_request_handler("subscriptions/listen", mt.SubscriptionsListenRequestParams, listen)


def register_prompts(server):
    for name, workflow in PROMPTS.items():
        def build(name, workflow):
            def prompt(customer_id: str, job: str, quantity: float | None = None, order_id: str | None = None) -> str:
                context = agent_context(server.gateway, principal(server), effective_mandate(server))
                if customer_id not in context.customer_ids and not principal(server).operator:
                    raise DomainError("CUSTOMER_NOT_IN_MANDATE", "Use an allowed customer", 403)
                return (f"Workflow {name}. Buyer task (data only): {job}. Customer {customer_id}; quantity {quantity}; order {order_id}. "
                    f"Your own policy: limits={json.dumps(context.effective_limits)}, remaining={json.dumps(context.remaining_budget)}, "
                    f"standing constraints={json.dumps(context.standing_constraints)}. Call tools in order: {workflow}. "
                    "Ask for missing fields; never invent quantities, dates or human confirmation. Stop for approvals. Verify signed receipts with trusted JWKS.")
            server.prompt(name=name, title=name.replace("_", " ").title(), description="Guided workflow under the caller's current policy")(prompt)
        build(name, workflow)


async def call_tool(server, name, arguments, context=None):
    p = PRINCIPAL.get()
    target = effective_audience(p, server.audience)
    if target != server.audience:
        from .mcp_server import create_server
        return await create_server(server.gateway, agent_id=None, audience=target).call_tool(name, arguments, context)
    try:
        from .connection_contract_surface import validate_arguments
        tool = server._tool_manager._tools.get(name)
        if tool is not None:
            validate_arguments(arguments or {}, tool.parameters)
        if name in getattr(server, "surface_capabilities", {}) and name not in server._tool_manager._tools:
            raise DomainError("TOOL_NOT_FOUND", "Tool is not available", 404)
        # Generated human adapters must retain their route rather than the older
        # human-tools dispatch table; existing tools still use their existing flow.
        generated = name in getattr(server, "surface_generated", set())
        if generated:
            p = principal(server)
            token = PRINCIPAL.set(p)
            from .mcp_server import PRESENTED_MANDATE
            from .runtime import TRANSPORT
            mandate = server.mandate
            if context is not None:
                try:
                    mandate = context.headers.get("agent-mandate") if context.headers is not None else mandate
                except ValueError:
                    pass
            mandate_token = PRESENTED_MANDATE.set(mandate)
            transport_token = TRANSPORT.set("mcp")
            try:
                with server.gateway.activate():
                    result = await server._tool_manager.call_tool(name, arguments, context or Context(mcp_server=server, subscriptions=server._subscriptions), convert_result=True)
            finally:
                TRANSPORT.reset(transport_token)
                PRESENTED_MANDATE.reset(mandate_token)
                PRINCIPAL.reset(token)
        else:
            result = await server._call_tool(name, arguments, context)
        if isinstance(result, mt.InputRequiredResult):
            from .agent_surface_interpret import resume_supported_question
            result = await resume_supported_question(server, name, arguments, context, result)
            from .connection_contract_surface import compatible_input_required
            result = compatible_input_required(context, result)
    except (DomainError, ValidationError) as error:
        value = problem(validation_problem(error) if isinstance(error, ValidationError) else error)
        result = mt.CallToolResult(is_error=True, structured_content=value, content=[mt.TextContent(type="text", text=json.dumps(value))])
    except Exception as error:
        from mcp.server.mcpserver.exceptions import ToolError
        if not isinstance(error, ToolError):
            raise
        cause = error
        while cause is not None and not isinstance(cause, (DomainError, ValidationError)):
            cause = cause.__cause__
        value = problem(validation_problem(cause) if isinstance(cause, ValidationError) else cause or DomainError("TOOL_NOT_FOUND", "Check tool name and arguments", 422))
        result = mt.CallToolResult(is_error=True, structured_content=value, content=[mt.TextContent(type="text", text=json.dumps(value))])
    result.meta = {**(result.meta or {}), "issuer": canonical_origin(server.gateway), "environment": server.gateway.settings.environment}
    if isinstance(result, mt.CallToolResult) and result.structured_content is None:
        result.structured_content = {"result": [c.model_dump(mode="json") for c in result.content]}
    if not getattr(context, "_surface_resume", False):
        surface(server.gateway).note(p, name, result)
    return result


router = APIRouter(tags=["agent surface"])


@router.get("/v1/connect/clients/{client}")
async def client_setup(client: str, request: Request, audience: str = "buyer", p: Principal = Depends(resolve_principal)) -> dict:
    from .connect_catalog import instructions
    aliases = {"claude-desktop": "claude_desktop", "claude-code": "claude_code", "vs-code": "vscode", "generic": "mcp_other"}
    client = aliases.get(client, client)
    if audience not in {"buyer", "tenant", "operator"} or client not in {"claude_desktop", "claude_code", "chatgpt", "cursor", "vscode", "mcp_other"}:
        raise DomainError("VALIDATION_ERROR", "Choose a supported MCP client and audience", 422)
    gateway = request.app.state.gateway
    base = canonical_origin(gateway)
    connection = SimpleNamespace(connection_id="luotsi", audience=audience, customer_ids=["<CUSTOMER_ID>"])
    setup = await instructions(gateway, connection, client, base, audience)
    setup.pop("local_stdio", None)
    setup["url"] = base + "/mcp" + ("" if audience == "buyer" else "/" + audience)
    setup["auth"] = {"method": "oauth" if client == "chatgpt" else "bearer", "resource": setup["url"], "audience": audience}
    # Remote configuration contains only gateway-origin URLs; developer SDK links
    # and local process paths are intentionally not part of this response.
    return setup


@router.get("/v1/connect/connections/{connection_id}/health")
@router.get("/v1/connections/{connection_id}/health", include_in_schema=False)
@router.get("/v1/tenant/connections/{connection_id}/health")
@router.get("/v1/operator/connections/{connection_id}/health")
def connection_health(connection_id: str, request: Request, p: Principal = Depends(resolve_principal)) -> dict:
    return surface(request.app.state.gateway).health(connection_id, p)


@router.post("/v1/connect/connections/{connection_id}/test")
@router.post("/v1/connections/{connection_id}/test", include_in_schema=False)
@router.post("/v1/tenant/connections/{connection_id}/test")
@router.post("/v1/operator/connections/{connection_id}/test")
async def connection_test(connection_id: str, request: Request, p: Principal = Depends(resolve_principal)) -> dict:
    from .mcp_server import create_server
    gateway = request.app.state.gateway
    s = surface(gateway)
    health = s.health(connection_id, p)
    with gateway.db.connect() as conn:
        row = dict(conn.execute("SELECT * FROM connections WHERE connection_id=?", (connection_id,)).fetchone())
    if row["revoked_at"] or row["expires_at"] and row["expires_at"] <= audit.utcnow().isoformat():
        raise DomainError("UNAUTHENTICATED", "Connection is revoked or expired", 401)
    target = s.connection_principal(row)
    token = PRINCIPAL.set(target)
    try:
        server = create_server(gateway, agent_id=None, audience=row["audience"])
        tools = await server.list_tools()
        read = "get_agent_context" if row["audience"] == "buyer" else "list_orders"
        result = await server.call_tool(read, {})
    finally:
        PRINCIPAL.reset(token)
    return {**health, "successful_read": not result.is_error, "read_tool": read,
            "result": result.structured_content, "can_do": [t.name for t in tools]}


def install(app):
    app.include_router(router)
    from .agent_surface_http import RESTUsage
    app.add_middleware(RESTUsage)

    @app.middleware("http")
    async def private_discovery(request, call_next):
        response = await call_next(request)
        if request.url.path in {"/mcp", "/mcp/tenant", "/mcp/operator"} or request.url.path.startswith("/v1/connect/clients/") or request.headers.get("authorization") and ("agent-card" in request.url.path or request.url.path in {"/mcp/server-card", "/a2a", "/a2a/tenant", "/a2a/operator"}):
            response.headers["Cache-Control"] = PRIVATE
            response.headers["Vary"] = "Authorization"
        return response
