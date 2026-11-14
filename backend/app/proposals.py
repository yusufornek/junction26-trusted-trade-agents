"""Consent-scoped supplier content; accepting always re-enters buyer procurement authority."""
import json
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
from hashlib import sha256
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, Request
from pydantic import Field, model_validator

from . import audit
from .auth import Principal, resolve_principal
from .canonical import canonical_json
from .catalog import hard_violations
from .db import DomainError
from .foundation import PRINCIPAL
from .models import Model, NeedSpec, QuoteRequest
from .mandates import decode


class QuietPeriod(Model):
    start: time
    end: time
    weekdays: list[int] = Field(default_factory=lambda: list(range(7)), description="Monday=0 through Sunday=6")

    @model_validator(mode="after")
    def valid(self):
        if any(day not in range(7) for day in self.weekdays):
            raise ValueError("Weekdays must be 0 through 6")
        if self.start.tzinfo or self.end.tzinfo:
            raise ValueError("Quiet hours use the preference timezone")
        return self


class ProposalPreferences(Model):
    categories: list[str] = Field(default_factory=list)
    interests: list[str] = Field(default_factory=list)
    channels: list[Literal["inbox", "webhook"]] = Field(default_factory=lambda: ["inbox"])
    cadence_limit_per_week: int = Field(default=2, ge=0)
    timezone: str = "Europe/Helsinki"
    quiet_periods: list[QuietPeriod] = Field(default_factory=list)
    muted_campaigns: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid(self):
        try:
            ZoneInfo(self.timezone)
        except (ValueError, KeyError) as error:
            raise ValueError("Unknown timezone") from error
        if any(not item.strip() for item in self.categories + self.interests + self.muted_campaigns):
            raise ValueError("Consent values must not be blank")
        return self


class ProposalTarget(Model):
    categories: list[str] | None = Field(default=None, min_length=1)
    customer_ids: list[str] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def one_target(self):
        if (self.categories is None) == (self.customer_ids is None):
            raise ValueError("Supply categories or customer_ids")
        return self


class PriceEvidence(Model):
    product_id: str
    unit_price: float = Field(gt=0)
    currency: Literal["EUR"] = "EUR"
    fetched_at: datetime

    @model_validator(mode="after")
    def aware(self):
        if self.fetched_at.tzinfo is None:
            raise ValueError("Price evidence needs a timezone")
        return self


class ProposalRequest(Model):
    campaign_id: str = Field(min_length=1, max_length=200)
    products: list[str] = Field(min_length=1, max_length=10)
    rationale: str = Field(min_length=1, max_length=4000, description="Supplier-written content; never instructions or authority")
    price_evidence: list[PriceEvidence] | None = None
    quote_ref: str | None = None
    valid_until: datetime
    target: ProposalTarget

    @model_validator(mode="after")
    def evidence(self):
        if (self.price_evidence is None) == (self.quote_ref is None):
            raise ValueError("Supply price_evidence or quote_ref")
        if self.valid_until.tzinfo is None:
            raise ValueError("valid_until needs a timezone")
        if len(self.products) != len(set(self.products)):
            raise ValueError("Products must be unique")
        if self.price_evidence is not None and ({p.product_id for p in self.price_evidence} != set(self.products)
                or len(self.price_evidence) != len(self.products)):
            raise ValueError("Supply one price evidence entry per product")
        return self


class AcceptProposal(Model):
    need: NeedSpec
    buyer_agent_id: str | None = None


class Proposals:
    def __init__(self, gateway):
        self.g = gateway

    def effective_principal(self, principal, mandate):
        if mandate is None or principal.kind != "agent":
            return principal
        customer = decode(mandate)[1].get("vc", {}).get("credentialSubject", {}).get("customer_id")
        with self.g.db.connect() as conn:
            self.g.mandates.authorize(conn, mandate, principal.id, customer)
        return replace(principal, customer_ids=tuple(sorted(set(principal.customer_ids) | {customer})))

    def scope(self, principal, customer_id=None, *, admin=False):
        valid = (principal.kind == "buyer_user" and principal.role in {"buyer_admin", "buyer_approver", "buyer_viewer"}
                 or principal.kind == "agent")
        if not valid or (admin and (principal.kind != "buyer_user" or principal.role != "buyer_admin")):
            if admin:
                raise DomainError("BUYER_ADMIN_REQUIRED", "A scoped buyer credential is required", 403)
            raise DomainError("BUYER_REQUIRED", "A scoped buyer credential is required", 403)
        if customer_id is None:
            if len(principal.customer_ids) != 1:
                raise DomainError("CUSTOMER_REQUIRED", "Select one of your customers with customer_id", 422)
            customer_id = principal.customer_ids[0]
        if customer_id not in principal.customer_ids:
            raise DomainError("CUSTOMER_FORBIDDEN", "Customer is outside this credential's scope", 403)
        return customer_id

    def preferences(self, customer_id):
        with self.g.db.connect() as conn:
            row = conn.execute("SELECT data FROM proposal_preferences WHERE customer_id=?", (customer_id,)).fetchone()
        return ProposalPreferences.model_validate_json(row[0]) if row else ProposalPreferences()

    def put_preferences(self, body, principal, customer_id=None):
        customer_id = self.scope(principal, customer_id, admin=True)
        with self.g.db.connect(write=True) as conn:
            conn.execute("INSERT INTO proposal_preferences VALUES(?,?) ON CONFLICT(customer_id) DO UPDATE SET data=excluded.data",
                         (customer_id, body.model_dump_json()))
            audit.append(conn, f"human:buyer_admin:{principal.credential_ref}", "PROPOSAL_PREFERENCES",
                         "Buyer updated supplier proposal consent.", data={"customer_id": customer_id, "preferences": body.model_dump(mode="json")})
        return {"customer_id": customer_id, **body.model_dump(mode="json")}

    @staticmethod
    def week_start(now, zone="UTC"):
        local = now.astimezone(ZoneInfo(zone))
        return (local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=local.weekday())).astimezone(timezone.utc).isoformat()

    @staticmethod
    def quiet(prefs, now):
        local = now.astimezone(ZoneInfo(prefs.timezone))
        for period in prefs.quiet_periods:
            clock = local.time()
            if period.start == period.end and local.weekday() in period.weekdays:
                return True
            if period.start < period.end and local.weekday() in period.weekdays and period.start <= clock < period.end:
                return True
            if period.start > period.end and ((local.weekday() in period.weekdays and clock >= period.start)
                    or ((local.weekday() - 1) % 7 in period.weekdays and clock < period.end)):
                return True
        return False

    @staticmethod
    def relevant(product, prefs):
        text = " ".join([product.name_en, product.name_fi, *product.claims]).casefold().replace("_", " ")
        return product.category in prefs.categories or any(i.casefold().replace("_", " ") in text for i in prefs.interests)

    def counts(self, campaign, principal):
        if principal.kind != "supplier_agent" or campaign["supplier_id"] != principal.id:
            raise DomainError("SUPPLIER_REQUIRED", "Only the submitting supplier can see campaign totals", 403)
        with self.g.db.connect() as conn:
            rows = [json.loads(r[0]) for r in conn.execute("SELECT data FROM proposals WHERE campaign_id=?", (campaign["id"],))]
            held = conn.execute("SELECT count(DISTINCT customer_id) FROM proposal_filter_log WHERE campaign_id=?", (campaign["id"],)).fetchone()[0]
        return {"id": campaign["id"], "campaign_id": campaign["campaign_id"], "delivery_count": len(rows),
                "accept_total": sum(r["status"] == "accepted" for r in rows),
                "decline_total": sum(r["status"] == "declined" for r in rows), "held_back_count": held}

    def submit(self, body, principal):
        if principal.kind != "supplier_agent":
            raise DomainError("SUPPLIER_REQUIRED", "Only a supplier_agent may submit proposals", 403)
        now = audit.utcnow()
        if body.valid_until <= now:
            raise DomainError("PROPOSAL_EXPIRED", "Campaign has expired", 409)
        products = [self.g.catalog.get(pid) for pid in body.products]
        evidence = body.price_evidence
        if body.quote_ref:
            # An opaque quote reference must never reveal another buyer's quote.
            quote = self.g.orders.get_quote(body.quote_ref, principal.id)
            if quote.expires_at <= now or set(body.products) != {l.product_id for l in quote.lines}:
                raise DomainError("INVALID_PRICE_EVIDENCE", "Use a current quote for exactly these products", 422)
            evidence = [PriceEvidence(product_id=l.product_id, unit_price=l.unit_price, currency=l.currency,
                                      fetched_at=quote.created_at) for l in quote.lines]
        if any(e.fetched_at > now or e.fetched_at >= body.valid_until for e in evidence):
            raise DomainError("INVALID_PRICE_EVIDENCE", "Evidence must predate submission and expiry", 422)
        fingerprint = sha256(canonical_json(sorted([e.product_id, e.unit_price, e.currency] for e in evidence))).hexdigest()
        policy = self.g.policy.get()
        with self.g.db.connect(write=True) as conn:
            all_campaigns = [json.loads(r[0]) for r in conn.execute("SELECT data FROM proposal_campaigns WHERE supplier_id=?", (principal.id,))]
            duplicate = next((c for c in all_campaigns if (c["campaign_id"] == body.campaign_id or c["fingerprint"] == fingerprint)
                              and datetime.fromisoformat(c["valid_until"]) > now), None)
            if duplicate:
                for customer in sorted(set(body.target.customer_ids or [])):
                    prefs = self.preferences(customer)
                    muted = body.campaign_id in prefs.muted_campaigns or fingerprint in prefs.muted_campaigns
                    reason = "muted campaign" if muted else "equivalent duplicate campaign"
                    conn.execute("INSERT INTO proposal_filter_log(campaign_id,customer_id,reason,at) VALUES(?,?,?,?)",
                                 (duplicate["id"], customer, reason, now.isoformat()))
                return {**self.counts(duplicate, principal), "merged": True, "new_delivery_count": 0}
            count = sum(c["created_at"] >= self.week_start(now) for c in all_campaigns)
            if count >= policy.proposal_quotas.per_supplier_per_week:
                audit.append(conn, f"supplier:{principal.id}", "PROPOSAL_QUOTA", "Supplier weekly proposal quota reached.", data={"supplier_id": principal.id})
                return {"campaign_id": body.campaign_id, "delivery_count": 0, "accept_total": 0, "decline_total": 0, "reason": "supplier quota"}
            campaign = {**body.model_dump(mode="json"), "id": str(uuid4()), "supplier_id": principal.id,
                        "fingerprint": fingerprint, "created_at": now.isoformat(),
                        "price_evidence": [e.model_dump(mode="json") for e in evidence]}
            conn.execute("INSERT INTO proposal_campaigns VALUES(?,?,?,?)", (campaign["id"], principal.id, fingerprint, json.dumps(campaign)))
            targets = set(body.target.customer_ids or [])
            if body.target.categories:
                targets.update(r[0] for r in conn.execute("SELECT customer_id FROM proposal_preferences"))
            for customer in sorted(targets):
                prefs = self.preferences(customer)
                permitted = [p for p in products if (not body.target.categories or p.category in body.target.categories)
                             and self.relevant(p, prefs)]
                reason = None
                if not (prefs.categories or prefs.interests) or not prefs.channels:
                    reason = "no opt-in"
                elif campaign["campaign_id"] in prefs.muted_campaigns or fingerprint in prefs.muted_campaigns:
                    reason = "muted campaign"
                elif self.quiet(prefs, now):
                    reason = "quiet period"
                delivered = conn.execute("SELECT count(*) FROM proposals WHERE customer_id=? AND json_extract(data,'$.delivered_at')>=?",
                                         (customer, self.week_start(now, prefs.timezone))).fetchone()[0]
                if reason is None and delivered >= min(prefs.cadence_limit_per_week, policy.proposal_quotas.per_customer_per_week):
                    reason = "weekly cadence or customer quota"
                constraints = policy.customers.get(customer)
                removed = {}
                filtered = []
                for p in permitted:
                    failures = hard_violations(p, constraints.required_claims if constraints else [], constraints.exclude_allergens if constraints else [])
                    if failures:
                        removed[p.id] = failures
                    else:
                        filtered.append(p.id)
                if removed:
                    audit.append(conn, "system:proposals", "PROPOSAL_FILTERED", "Noncompliant products removed before delivery.",
                                 data={"customer_id": customer, "campaign_id": campaign["id"], "removed": removed})
                if not filtered and reason is None:
                    reason = "no relevant compliant products"
                if reason:
                    conn.execute("INSERT INTO proposal_filter_log(campaign_id,customer_id,reason,at) VALUES(?,?,?,?)",
                                 (campaign["id"], customer, reason, now.isoformat()))
                    audit.append(conn, "system:proposals", "PROPOSAL_HELD", reason,
                                 data={"customer_id": customer, "campaign_id": campaign["id"], "reason": reason})
                    continue
                proposal = {"id": str(uuid4()), "campaign_id": campaign["id"], "customer_id": customer,
                            "supplier_id": principal.id, "products": filtered, "rationale": body.rationale,
                            "valid_until": body.valid_until.isoformat(), "delivered_at": now.isoformat(),
                            "price_evidence": [e.model_dump(mode="json") for e in evidence if e.product_id in filtered],
                            "channels": prefs.channels, "status": "received"}
                conn.execute("INSERT INTO proposals VALUES(?,?,?,?)", (proposal["id"], campaign["id"], customer, json.dumps(proposal)))
                # The durable outbox feeds spec 33 consumers. Channels travel with the event;
                # a webhook consumer must require the customer's explicit webhook consent.
                audit.append(conn, f"supplier:{principal.id}", "PROPOSAL_RECEIVED", "Consented supplier proposal received.",
                             data=proposal)
        return self.counts(campaign, principal)

    def inbox(self, principal, mandate=None):
        principal = self.effective_principal(principal, mandate)
        self.scope(principal, principal.customer_ids[0] if principal.customer_ids else None)
        with self.g.db.connect() as conn:
            return [json.loads(r["data"]) for r in conn.execute("SELECT * FROM proposals ORDER BY rowid DESC")
                    if r["customer_id"] in principal.customer_ids and "inbox" in json.loads(r["data"])["channels"]]

    def get(self, conn, id, principal):
        row = conn.execute("SELECT data FROM proposals WHERE id=?", (id,)).fetchone()
        proposal = json.loads(row[0]) if row else None
        if not proposal or principal.kind not in {"agent", "buyer_user"} or proposal["customer_id"] not in principal.customer_ids:
            raise DomainError("PROPOSAL_NOT_FOUND", "Proposal was not found", 404)
        return proposal

    def action(self, id, action, principal, body=None, mandate=None):
        principal = self.effective_principal(principal, mandate)
        with self.g.db.connect(write=True) as conn:
            proposal = self.get(conn, id, principal)
            if principal.kind == "buyer_user" and principal.role not in {"buyer_admin", "buyer_approver"}:
                raise DomainError("BUYER_APPROVER_REQUIRED", "Buyer viewer cannot act on proposals", 403)
            if action == "accept" and proposal["status"] == "accepted":
                agent = principal.id if principal.kind == "agent" else body.buyer_agent_id
                if agent != proposal["accepted_by"]:
                    raise DomainError("BUYER_AGENT_REQUIRED", "Replay requires the original purchasing agent", 403)
                order = self.g.orders.get_order(proposal["order_id"])
                return {"proposal": proposal, "order": self.g.orders.place_order(
                    order.quote_id, f"proposal:{id}", agent, mandate=mandate)}
            if action == "accept" and proposal["status"] != "received":
                raise DomainError("INVALID_TRANSITION", "Only a received proposal can be accepted", 409)
            result = {}
            if action == "accept":
                if datetime.fromisoformat(proposal["valid_until"]) <= audit.utcnow():
                    raise DomainError("PROPOSAL_EXPIRED", "Proposal expired; request a new offer", 409)
                agent = principal.id if principal.kind == "agent" else body.buyer_agent_id
                policy = self.g.policy.get()
                if not agent or (body.buyer_agent_id and principal.kind == "agent" and body.buyer_agent_id != agent):
                    raise DomainError("BUYER_AGENT_REQUIRED", "Accept using your own buyer agent or name a scoped buyer agent", 403)
                if principal.kind == "buyer_user":
                    identity = policy.identities.get(agent)
                    if not identity or identity.kind != "agent" or identity.organisation_id != principal.organisation_id:
                        raise DomainError("BUYER_AGENT_REQUIRED", "Select an agent in your buyer organisation", 403)
                if body.need.customer_id != proposal["customer_id"]:
                    raise DomainError("CUSTOMER_FORBIDDEN", "Need must use the proposal customer", 403)
                context = PRINCIPAL.set(Principal(agent, customer_ids=(proposal["customer_id"],)))
                try:
                    self.g.valio.cache.clear()
                    lookup = self.g.find_offers(body.need, f"agent:{agent}", mandate=mandate)
                    offers = [o for o in lookup.offers if o.product_id in proposal["products"] and o.units_needed]
                    if not offers:
                        raise DomainError("PROPOSAL_NO_MATCH", "No proposal product meets this current need", 409)
                    offer = offers[0]
                    quote = self.g.orders.create_quote(QuoteRequest(customer_id=body.need.customer_id,
                        deliver_by=body.need.deliver_by, need_ref=lookup.lookup_id,
                        lines=[{"product_id": offer.product_id, "quantity_sales_units": offer.units_needed}]), agent, mandate=mandate)
                    advertised = next(e for e in proposal["price_evidence"] if e["product_id"] == offer.product_id)
                    if quote.lines[0].unit_price != advertised["unit_price"]:
                        # Persist the replacement quote for review, but never buy a changed advertised price.
                        return {"proposal": proposal, "status": "REQUOTE_REQUIRED", "quote": quote,
                                "previous_unit_price": advertised["unit_price"], "new_unit_price": quote.lines[0].unit_price}
                    order = self.g.orders.place_order(quote.id, f"proposal:{id}", agent, mandate=mandate)
                    proposal.update(status="accepted", order_id=order.id, accepted_by=agent)
                    result.update(order=order, quote=quote)
                finally:
                    PRINCIPAL.reset(context)
            else:
                if proposal["status"] == "accepted":
                    raise DomainError("INVALID_TRANSITION", "Accepted proposal purchases must be cancelled through the order", 409)
                proposal["status"] = "declined" if action == "decline" else "muted"
                if action == "mute":
                    prefs = self.preferences(proposal["customer_id"])
                    campaign = json.loads(conn.execute("SELECT data FROM proposal_campaigns WHERE id=?", (proposal["campaign_id"],)).fetchone()[0])
                    prefs.muted_campaigns = sorted(set(prefs.muted_campaigns + [campaign["campaign_id"], campaign["fingerprint"]]))
                    conn.execute("INSERT INTO proposal_preferences VALUES(?,?) ON CONFLICT(customer_id) DO UPDATE SET data=excluded.data",
                                 (proposal["customer_id"], prefs.model_dump_json()))
            proposal["acted_at"] = audit.utcnow().isoformat()
            conn.execute("UPDATE proposals SET data=? WHERE id=?", (json.dumps(proposal), id))
            audit.append(conn, f"{principal.kind}:{principal.id}", "PROPOSAL_ACTION", f"Buyer chose {action}.",
                         data={"id": id, "customer_id": proposal["customer_id"], "action": action})
            return {"proposal": proposal, **result}


router = APIRouter(tags=["Supplier proposals"])


@router.get("/v1/buyer/proposal-preferences", response_model=dict)
def get_preferences(request: Request, customer_id: str | None = None, principal: Principal = Depends(resolve_principal)):
    service = request.app.state.gateway.proposals
    customer = service.scope(principal, customer_id, admin=True)
    return {"customer_id": customer, **service.preferences(customer).model_dump(mode="json")}


@router.put("/v1/buyer/proposal-preferences", response_model=dict)
def put_preferences(body: ProposalPreferences, request: Request, customer_id: str | None = None, principal: Principal = Depends(resolve_principal),
                    idempotency_key: str | None = Header(default=None)):
    return request.app.state.gateway.proposals.put_preferences(body, principal, customer_id, command_key=idempotency_key)


@router.post("/v1/proposals", response_model=dict, summary="Submit supplier content; returns only aggregate campaign delivery and action counts")
def submit(body: ProposalRequest, request: Request, principal: Principal = Depends(resolve_principal),
           idempotency_key: str | None = Header(default=None)):
    return request.app.state.gateway.proposals.submit(body, principal, command_key=idempotency_key)


@router.get("/v1/proposals/inbox", response_model=list[dict])
def inbox(request: Request, principal: Principal = Depends(resolve_principal), agent_mandate: str | None = Header(default=None)):
    return request.app.state.gateway.proposals.inbox(principal, agent_mandate)


@router.get("/v1/proposals/campaigns/{id}", response_model=dict)
def campaign(id: str, request: Request, principal: Principal = Depends(resolve_principal)):
    service = request.app.state.gateway.proposals
    if principal.kind != "supplier_agent":
        raise DomainError("SUPPLIER_REQUIRED", "Supplier credential required", 403)
    with service.g.db.connect() as conn:
        row = conn.execute("SELECT data FROM proposal_campaigns WHERE id=? AND supplier_id=?", (id, principal.id)).fetchone()
    if not row:
        raise DomainError("CAMPAIGN_NOT_FOUND", "Campaign was not found", 404)
    return service.counts(json.loads(row[0]), principal)


@router.post("/v1/proposals/{id}/accept", response_model=dict, summary="Revalidate buyer lookup, price and stock; changed prices return a replacement quote without ordering")
def accept(id: str, body: AcceptProposal, request: Request, principal: Principal = Depends(resolve_principal),
           agent_mandate: str | None = Header(default=None), idempotency_key: str | None = Header(default=None)):
    return request.app.state.gateway.proposals.action(id, "accept", principal, body, agent_mandate, command_key=idempotency_key)


@router.post("/v1/proposals/{id}/decline", response_model=dict)
def decline(id: str, request: Request, principal: Principal = Depends(resolve_principal),
            idempotency_key: str | None = Header(default=None)):
    return request.app.state.gateway.proposals.action(id, "decline", principal, command_key=idempotency_key)


@router.post("/v1/proposals/{id}/mute", response_model=dict)
def mute(id: str, request: Request, principal: Principal = Depends(resolve_principal),
            idempotency_key: str | None = Header(default=None)):
    return request.app.state.gateway.proposals.action(id, "mute", principal, command_key=idempotency_key)
