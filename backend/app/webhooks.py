"""Durable, customer-scoped Standard Webhooks v1a delivery.

The outbox is the queue; delivery records are immutable payload snapshots. DNS is
checked on every attempt and the connection is pinned to that checked address.
"""
from .telemetry import traced, metric
import base64
import html
import ipaddress
import json
import random
import re
import secrets
import socket
import threading
import time
from contextvars import ContextVar
from datetime import timedelta
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from . import audit
from .auth import Principal, require_reviewer, resolve_principal
from .canonical import canonical_json
from .db import DomainError
from .models import InputTexts
from .signing import unb64

EventType = Literal["order.approval_required", "order.evidence_changed", "order.confirmed", "order.rejected",
                    "order.cancelled", "order.expired", "mandate.revoked", "proposal.received",
                    "suggestion.shown", "suggestion.prepared", "suggestion.made_recurring",
                    "suggestion.dismissed", "suggestion.snoozed",
                    "INTENT_AMENDED", "INTENT_HANDOVER", "INTENT_PARTIALLY_CONFIRMED"]
EVENT_TYPES = set(EventType.__args__)


def notification_headers(config):
    headers = {}
    token = config.get("token", "")
    auth = config.get("authentication", {})
    scheme, credentials = auth.get("scheme", ""), auth.get("credentials", "")
    if any("\r" in value or "\n" in value for value in (token, scheme, credentials)) or (
            scheme and not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", scheme)):
        raise DomainError("VALIDATION_ERROR", "Invalid push authentication header", 422)
    if token:
        headers["X-A2A-Notification-Token"] = token
    if scheme and credentials:
        headers["Authorization"] = scheme + " " + credentials
    return headers


class SubscriptionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=2048)
    customer_ids: InputTexts = Field(min_length=1)
    event_types: list[EventType] = Field(min_length=1)
    audience: Literal["agent", "approver"] = "agent"


class SubscriptionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str | None = Field(default=None, min_length=1, max_length=2048)
    customer_ids: InputTexts | None = Field(default=None, min_length=1)
    event_types: list[EventType] | None = Field(default=None, min_length=1)
    audience: Literal["agent", "approver"] | None = None
    enabled: bool | None = None




def signed_headers(signing, event_id, body, timestamp=None):
    timestamp = str(timestamp if timestamp is not None else int(audit.utcnow().timestamp()))
    registry = signing._registry()
    key = serialization.load_pem_private_key(
        (signing.directory / (registry["active"] + ".pem")).read_bytes(), password=None)
    signature = key.sign(event_id.encode() + b"." + timestamp.encode() + b"." + body)
    return {"webhook-id": event_id, "webhook-timestamp": timestamp,
            "webhook-signature": "v1a," + base64.b64encode(signature).decode(), "Content-Type": "application/json"}


def verify_webhook(body, headers, jwks, *, now=None, tolerance=300):
    """Verify raw bytes before parsing; reject stale timestamps and accept any published key."""
    timestamp = headers["webhook-timestamp"]
    if abs((now if now is not None else audit.utcnow().timestamp()) - int(timestamp)) > tolerance:
        raise ValueError("Webhook timestamp outside replay window")
    content = headers["webhook-id"].encode() + b"." + timestamp.encode() + b"." + body
    for item in headers["webhook-signature"].split():
        version, _, signature = item.partition(",")
        if version != "v1a":
            continue
        for key in jwks["keys"]:
            if key.get("kty") != "OKP" or key.get("crv") != "Ed25519":
                continue
            try:
                Ed25519PublicKey.from_public_bytes(unb64(key["x"])).verify(
                    base64.b64decode(signature, validate=True), content)
                return True
            except (ValueError, InvalidSignature):
                continue
    raise ValueError("Invalid webhook signature")


class Webhooks:
    max_attempts = 5
    base_delay = 2
    challenge_max_bytes = 16 * 1024
    response_deadline = 5

    def __init__(self, gateway):
        self.gateway, self.db = gateway, gateway.db
        self.lock = threading.Lock()
        self.client = httpx.Client(timeout=5, follow_redirects=False, trust_env=False)
        self.resolver = socket.getaddrinfo
        self.jitter = random.random
        self.clock = time.monotonic
        self.prepared = ContextVar("verified_webhook_destination", default=None)

    def _destination(self, url):
        try:
            parsed = urlsplit(url)
            host, port = parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
            local = (getattr(self.gateway.settings, "webhook_sandbox", False) and host == "localhost")
            if not host or parsed.username or parsed.password or parsed.fragment or (
                    parsed.scheme != "https" and not (local and parsed.scheme == "http")):
                raise ValueError()
            addresses = list(dict.fromkeys(row[4][0] for row in self.resolver(host, port, type=socket.SOCK_STREAM)))
            if not addresses:
                raise ValueError()
            for address in addresses:
                ip = ipaddress.ip_address(address)
                if local and ip.is_loopback:
                    continue
                if not ip.is_global or ip.is_multicast or ip.is_reserved or (ip.version == 6 and (ip.ipv4_mapped or ip.sixtofour or ip.teredo)):
                    raise ValueError()
            return parsed, addresses[0]
        except (ValueError, OSError):
            raise DomainError("WEBHOOK_DESTINATION_UNSAFE", "Use a public HTTPS destination (sandbox permits localhost).", 422) from None

    @traced("webhook.deliver")
    def _post(self, url, body, event_id, extra_headers=None, *, challenge=False):
        metric("delivery.attempts")
        deadline = self.clock() + self.response_deadline
        parsed, address = self._destination(url)
        original = httpx.URL(url)
        pinned = original.copy_with(host=address)
        headers = signed_headers(self.gateway.signing, event_id, body)
        headers.update(extra_headers or {})
        headers["Host"] = original.netloc.decode()
        headers["Accept-Encoding"] = "identity"
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise ValueError("Webhook deadline exceeded")
        with self.client.stream("POST", pinned, content=body, headers=headers, timeout=remaining,
                                extensions={"sni_hostname": parsed.hostname}) as response:
            content = bytearray()
            if challenge:
                if response.headers.get("Content-Encoding", "identity") != "identity":
                    raise ValueError("Challenge must use an uncompressed JSON response")
                chunks = [response.content] if response.is_stream_consumed else response.iter_raw()
                for chunk in chunks:
                    if self.clock() >= deadline or len(content) + len(chunk) > self.challenge_max_bytes:
                        raise ValueError("Webhook challenge response exceeds limits")
                    content.extend(chunk)
            if self.clock() >= deadline:
                raise ValueError("Webhook deadline exceeded")
            # Deliveries use only the status; closing never buffers their body.
            return httpx.Response(response.status_code, content=bytes(content))

    def _challenge(self, url, extra_headers=None):
        if self.prepared.get() == (url, extra_headers or {}):
            return
        challenge = secrets.token_urlsafe(32)
        try:
            response = self._post(url, canonical_json({"type": "webhook.challenge", "challenge": challenge}), "challenge_" + uuid4().hex, extra_headers, challenge=True)
            if not 200 <= response.status_code < 300 or response.json().get("challenge") != challenge:
                raise ValueError()
        except DomainError:
            raise
        except (httpx.HTTPError, ValueError, AttributeError):
            raise DomainError("WEBHOOK_CHALLENGE_FAILED", "Destination must echo the subscription challenge.", 422) from None

    def prepare(self, route, arguments, principal):
        """Verify outside the shared keyed-command transaction; writes recheck scope."""
        from .buyer import check_suspension
        if principal.kind == "agent":
            check_suspension(self.db, principal.id)
        body = arguments["body"]
        if route == "webhooks.create":
            self._scope(principal, body.customer_ids, body.audience)
            url, headers = body.url, notification_headers(arguments.get("push_config") or {})
        else:
            with self.db.connect() as conn:
                sub = self._get(arguments["subscription_id"], principal, conn)
            changes = body.model_dump(exclude_unset=True)
            if "url" not in changes and not changes.get("enabled"):
                return None
            proposed = {**sub, **changes}
            self._scope(principal, proposed["customer_ids"], proposed["audience"])
            url, headers = proposed["url"], {}
        self._challenge(url, headers)
        return self.prepared.set((url, headers))

    @staticmethod
    def _scope(principal, customers, audience="agent"):
        if not principal.operator and not set(customers) <= set(principal.customer_ids):
            raise DomainError("CUSTOMER_NOT_IN_MANDATE", "Subscription customers exceed principal scope", 403)
        if audience == "approver":
            require_reviewer(principal)

    def _get(self, subscription_id, principal, conn):
        row = conn.execute("SELECT * FROM webhook_subscriptions WHERE id=?", (subscription_id,)).fetchone()
        if not row or (not principal.operator and row["owner"] != f"{principal.kind}:{principal.id}"):
            raise DomainError("WEBHOOK_NOT_FOUND", "Subscription was not found", 404)
        sub = json.loads(row["data"])
        self._scope(principal, sub["customer_ids"], sub["audience"])
        return sub

    def create(self, body, principal, *, task_id=None, push_config=None):
        self._scope(principal, body.customer_ids, body.audience)
        self._challenge(body.url, notification_headers(push_config or {}))
        sub = {"id": uuid4().hex, **body.model_dump(), "enabled": True, "verified": True,
               "owner": principal.id, "owner_kind": principal.kind, "operator": principal.operator,
               "created_at": audit.utcnow().isoformat(), "task_id": task_id, "push_config": push_config}
        with self.db.connect(write=True) as conn:
            conn.execute("INSERT INTO webhook_subscriptions VALUES(?,?,?)", (sub["id"], f"{principal.kind}:{principal.id}", json.dumps(sub)))
            conn.execute("INSERT INTO event_consumers VALUES(?,1)", ("webhook:" + sub["id"],))
        return self._public(sub)

    @staticmethod
    def _public(sub):
        return {k: v for k, v in sub.items() if k not in {"push_config", "operator"}}

    def list(self, principal):
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM webhook_subscriptions ORDER BY rowid").fetchall()
        return [self._public(json.loads(row["data"])) for row in rows if
                (principal.operator or row["owner"] == f"{principal.kind}:{principal.id}") and
                (principal.operator or set(json.loads(row["data"])["customer_ids"]) <= set(principal.customer_ids))]

    def update(self, subscription_id, body, principal):
        with self.db.connect() as conn:
            sub = self._get(subscription_id, principal, conn)
        changes = body.model_dump(exclude_unset=True)
        if any(value is None for value in changes.values()):
            raise DomainError("VALIDATION_ERROR", "Subscription fields cannot be null", 422)
        sub.update(changes)
        self._scope(principal, sub["customer_ids"], sub["audience"])
        if "url" in changes or changes.get("enabled"):
            self._challenge(sub["url"])
        with self.db.connect(write=True) as conn:
            conn.execute("UPDATE webhook_subscriptions SET data=? WHERE id=?", (json.dumps(sub), subscription_id))
            conn.execute("UPDATE event_consumers SET enabled=? WHERE name=?", (int(sub["enabled"]), "webhook:" + subscription_id))
            if not sub["enabled"]:
                conn.execute("UPDATE outbox SET status='skipped' WHERE consumer=? AND status='pending'", ("webhook:" + subscription_id,))
        return self._public(sub)

    def disable(self, subscription_id, principal):
        return self.update(subscription_id, SubscriptionPatch(enabled=False), principal)

    def deliveries(self, subscription_id, principal):
        with self.db.connect() as conn:
            self._get(subscription_id, principal, conn)
            return [dict(row) for row in conn.execute(
                "SELECT d.id,d.event_id,o.status,o.attempts,o.next_attempt_at,d.last_error,d.http_status "
                "FROM webhook_deliveries d JOIN outbox o ON o.event_id=d.event_id AND o.consumer=? "
                "WHERE d.subscription_id=? ORDER BY d.rowid", ("webhook:" + subscription_id, subscription_id))]

    def redeliver(self, subscription_id, delivery_id, principal):
        with self.db.connect(write=True) as conn:
            sub = self._get(subscription_id, principal, conn)
            row = conn.execute("SELECT * FROM webhook_deliveries WHERE id=? AND subscription_id=?", (delivery_id, subscription_id)).fetchone()
            if row is None:
                raise DomainError("WEBHOOK_NOT_FOUND", "Delivery was not found", 404)
            if not sub["enabled"]:
                raise DomainError("INVALID_TRANSITION", "Enable the subscription before redelivery", 409)
            conn.execute("UPDATE outbox SET status='pending',attempts=0,next_attempt_at=? WHERE consumer=? AND event_id=?",
                         (audit.utcnow().isoformat(), "webhook:" + subscription_id, row["event_id"]))
        return {"id": delivery_id, "status": "pending"}

    @staticmethod
    def event_type(event):
        if event["type"] == "order.placed":
            return {"PENDING_APPROVAL": "order.approval_required", "CONFIRMED": "order.confirmed",
                    "REJECTED": "order.rejected"}.get(event["payload"].get("status"))
        return event["type"]

    def _body(self, event, sub):
        event_type = self.event_type(event)
        if sub.get("task_id"):
            return self.gateway.a2a.push_body(event, sub["task_id"])
        envelope = {**event, "type": event_type}
        if sub["audience"] == "approver" and event_type == "order.approval_required":
            with self.db.connect() as conn:
                order = self.gateway.orders._order(conn, event["aggregate_id"])
            revision = event["payload"].get("evidence_revision", order.evidence_revision)
            envelope["payload"] = {**event["payload"], "evidence_revision": revision,
                                   "review_url": f"/approvals/{order.id}?revision={revision}"}
        return canonical_json(envelope).decode()

    def dispatch(self, limit=100):
        if not self.lock.acquire(blocking=False):
            return
        try:
            # Retire disabled backlog left by older releases as well as new disables.
            with self.db.connect(write=True, notify=False) as conn:
                conn.execute("UPDATE outbox SET status='skipped' WHERE status='pending' AND consumer IN "
                             "(SELECT 'webhook:'||id FROM webhook_subscriptions WHERE json_extract(data,'$.enabled')=0)")
            with self.db.connect() as conn:
                rows = conn.execute("SELECT * FROM (SELECT o.*,e.customer_id,e.type,e.payload,s.data,e.rowid AS event_row, "
                    "ROW_NUMBER() OVER (PARTITION BY o.consumer ORDER BY e.rowid) AS queue_position FROM outbox o "
                    "JOIN events e ON e.id=o.event_id JOIN webhook_subscriptions s ON o.consumer='webhook:'||s.id "
                    "WHERE o.status='pending' AND json_extract(s.data,'$.enabled')=1 AND o.next_attempt_at<=?) "
                    "ORDER BY queue_position,event_row LIMIT ?",
                    (audit.utcnow().isoformat(), limit)).fetchall()
            for row in rows:
                sub = json.loads(row["data"])
                if not sub["enabled"]:
                    continue
                with self.db.connect() as conn:
                    event = dict(conn.execute("SELECT * FROM events WHERE id=?", (row["event_id"],)).fetchone())
                    delivery = conn.execute("SELECT * FROM webhook_deliveries WHERE subscription_id=? AND event_id=?",
                                           (sub["id"], row["event_id"])).fetchone()
                event["payload"] = json.loads(event["payload"])
                matches = event["customer_id"] in sub["customer_ids"] and self.event_type(event) in sub["event_types"]
                if event["type"] == "proposal.received":
                    prefs = self.gateway.proposals.preferences(event["customer_id"])
                    matches = (matches and "webhook" in event["payload"].get("channels", [])
                               and "webhook" in prefs.channels and bool(prefs.categories or prefs.interests))
                if sub.get("task_id"):
                    matches = event["customer_id"] in sub["customer_ids"] and self.gateway.a2a.event_matches(event, sub["task_id"])
                if not sub.get("operator"):
                    policy = self.gateway.policy.get()
                    identity = policy.identities.get(sub["owner"])
                    organisation = policy.organisations.get(identity.organisation_id) if identity else None
                    agent = policy.agents.get(sub["owner"])
                    customers = organisation.customer_ids if organisation else agent.customer_ids if agent else []
                    matches = matches and event["customer_id"] in customers
                    from .visibility import event_visible
                    subscriber = Principal(sub["owner"], kind=sub["owner_kind"],
                        organisation_id=identity.organisation_id if identity else None, customer_ids=tuple(customers),
                        role=identity.role if identity else "agent")
                    with self.db.connect() as conn:
                        matches = matches and event_visible(self.gateway, conn, event, subscriber)
                if not matches:
                    with self.db.connect(write=True, notify=False) as conn:
                        conn.execute("UPDATE outbox SET status='skipped' WHERE event_id=? AND consumer=?", (row["event_id"], row["consumer"]))
                    continue
                if delivery is None:
                    body, delivery_id = self._body(event, sub), uuid4().hex
                    with self.db.connect(write=True, notify=False) as conn:
                        conn.execute("INSERT OR IGNORE INTO webhook_deliveries(id,subscription_id,event_id,body) VALUES(?,?,?,?)",
                                     (delivery_id, sub["id"], row["event_id"], body))
                    with self.db.connect() as conn:
                        delivery = conn.execute("SELECT * FROM webhook_deliveries WHERE subscription_id=? AND event_id=?", (sub["id"], row["event_id"])).fetchone()
                status, error, http_status = "delivered", None, None
                try:
                    extra = notification_headers(sub.get("push_config") or {})
                    response = self._post(sub["url"], delivery["body"].encode(), row["event_id"], extra)
                    http_status = response.status_code
                    if not 200 <= http_status < 300:
                        raise ValueError()
                except (httpx.HTTPError, ValueError, DomainError):
                    status = "failed" if row["attempts"] + 1 >= self.max_attempts else "pending"
                    error = "Destination refused, unsafe or unavailable"
                delay = self.base_delay * 2 ** min(row["attempts"], 12) * (1 + self.jitter() * .25)
                with self.db.connect(write=True, notify=False) as conn:
                    conn.execute("UPDATE webhook_deliveries SET last_error=?,http_status=? WHERE id=?", (error, http_status, delivery["id"]))
                    conn.execute("UPDATE outbox SET status=?,attempts=attempts+1,next_attempt_at=? WHERE event_id=? AND consumer=?",
                                 (status, (audit.utcnow() + timedelta(seconds=delay)).isoformat(), row["event_id"], row["consumer"]))
        finally:
            self.lock.release()


router = APIRouter()


@router.post("/v1/webhook-subscriptions", response_model=dict,
             description="Verify ownership by echoing the signed webhook.challenge, then enable customer-scoped delivery. Optional Idempotency-Key.")
def create_subscription(body: SubscriptionCreate, request: Request, principal: Principal = Depends(resolve_principal),
                        idempotency_key: str | None = Header(default=None, min_length=1)):
    return request.app.state.gateway.webhooks.create(body, principal, command_key=idempotency_key)


@router.get("/v1/webhook-subscriptions", response_model=list[dict])
def subscriptions(request: Request, principal: Principal = Depends(resolve_principal)):
    return request.app.state.gateway.webhooks.list(principal)


@router.patch("/v1/webhook-subscriptions/{subscription_id}", response_model=dict)
def update_subscription(subscription_id: str, body: SubscriptionPatch, request: Request,
                        principal: Principal = Depends(resolve_principal), idempotency_key: str | None = Header(default=None, min_length=1)):
    return request.app.state.gateway.webhooks.update(subscription_id, body, principal, command_key=idempotency_key)


@router.delete("/v1/webhook-subscriptions/{subscription_id}", response_model=dict, description="Disable without deleting delivery history.")
def disable_subscription(subscription_id: str, request: Request, principal: Principal = Depends(resolve_principal),
                         idempotency_key: str | None = Header(default=None, min_length=1)):
    return request.app.state.gateway.webhooks.disable(subscription_id, principal, command_key=idempotency_key)


@router.get("/v1/webhook-subscriptions/{subscription_id}/deliveries", response_model=list[dict],
            description="Notification status is independent of purchasing state. Includes attempts and next retry time.")
def deliveries(subscription_id: str, request: Request, principal: Principal = Depends(resolve_principal)):
    return request.app.state.gateway.webhooks.deliveries(subscription_id, principal)


@router.post("/v1/webhook-subscriptions/{subscription_id}/deliveries/{delivery_id}/redeliver", response_model=dict)
def redeliver(subscription_id: str, delivery_id: str, request: Request, principal: Principal = Depends(resolve_principal),
              idempotency_key: str | None = Header(default=None, min_length=1)):
    return request.app.state.gateway.webhooks.redeliver(subscription_id, delivery_id, principal, command_key=idempotency_key)


@router.get("/approvals/{order_id}", response_class=HTMLResponse,
            responses={200: {"content": {"text/html": {"schema": {"type": "string"}}}}},
            description="Authenticated revision-bound review. GET never approves. Approval uses the authenticated POST API.")
def review(order_id: str, request: Request, revision: int = Query(ge=1), principal: Principal = Depends(require_reviewer)):
    gateway = request.app.state.gateway
    order = gateway.orders.get_order(order_id)
    from .autonomy import buyer_scope
    if not principal.operator and not buyer_scope(gateway.policy.get(), principal, order.agent_id, [order.customer_id]):
        raise DomainError("ORDER_NOT_FOUND", "Order was not found", 404)
    if revision != order.evidence_revision:
        raise DomainError("EVIDENCE_CHANGED", "Review the current evidence revision", 409,
                          {"evidence_revision": order.evidence_revision, "changes": []})
    with gateway.db.connect() as conn:
        notifications = [dict(r) for r in conn.execute("SELECT o.status,o.attempts FROM outbox o JOIN events e ON e.id=o.event_id "
                        "WHERE e.aggregate_id=? AND o.consumer LIKE 'webhook:%'", (order_id,))]
    payload = json.dumps({"order": order.model_dump(mode="json"), "notification_delivery": notifications}, indent=2)
    # No credentials in the page or URL. The reviewer supplies their bearer token
    # to the POST, whose existing reviewer/ownership/revision checks stay authoritative.
    return HTMLResponse('<!doctype html><meta charset="utf-8"><title>Purchase review</title>'
        '<h1>Purchase review</h1><p>Purchasing decision and notification delivery are independent.</p><pre>' + html.escape(payload) + '</pre>'
        '<form id="review"><label>Approver <input id="name" required></label>'
        '<label>Bearer credential <input id="token" type="password" required autocomplete="off"></label>'
        '<button>Approve reviewed revision</button></form><pre id="result"></pre><script>'
        'document.getElementById("review").onsubmit=async e=>{e.preventDefault();const url='
        + json.dumps(f"/v1/approvals/{order_id}/approve") + ';const headers={"Content-Type":"application/json",'
        'Authorization:"Bearer "+document.getElementById("token").value};const body={approver:document.getElementById("name").value,evidence_revision:'
        + str(revision) + '};const send=async()=>{const r=await fetch(url,{method:"POST",headers,body:JSON.stringify(body)});return r.json();};'
        'let result=await send();if(result.confirmation_required){const order=result.summary.order;'
        'if(window.confirm("Approve "+order.total+" "+order.currency+" using evidence revision "+order.evidence_revision+" shown above?"))'
        '{body.confirmation_token=result.confirmation_token;result=await send();}else{result={detail:"Approval cancelled."};}}'
        'document.getElementById("result").textContent=result.status||result.detail||"Review the current order before retrying.";};</script>',
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"})
