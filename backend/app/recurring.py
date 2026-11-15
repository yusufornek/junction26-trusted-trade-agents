"""Durable recurring purchases, wall-clock scheduling and signed drift review evidence."""
import json
import logging
import threading
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.relativedelta import relativedelta
from dateutil.rrule import rrulestr
from fastapi import APIRouter, Depends, Header, Request
from pydantic import Field, model_validator

from . import audit
from .auth import Principal, resolve_principal
from .db import DomainError
from .foundation import PRINCIPAL
from .mandates import decode
from .models import InputText, InputTexts, BasketLookupRequest, Model, NeedSpec, QuoteRequest, delivery_today


class Cadence(Model):
    rrule: InputText = Field(default="FREQ=WEEKLY", description="Daily, weekly weekdays, or monthly RRULE. Monthly clamping uses BYMONTHDAY=28,...,preferred_day;BYSETPOS=-1.")
    timezone: InputText = "Europe/Helsinki"
    start_at: datetime
    catch_up: Literal["once"] = "once"

    @model_validator(mode="after")
    def supported(self):
        try:
            zone = ZoneInfo(self.timezone)
            parts = dict(p.split("=", 1) for p in self.rrule.upper().split(";"))
            if (set(parts) - {"FREQ", "INTERVAL", "BYDAY", "BYMONTHDAY", "BYSETPOS"} or
                    parts.get("FREQ") not in {"DAILY", "WEEKLY", "MONTHLY"} or
                    ("BYDAY" in parts and parts["FREQ"] != "WEEKLY") or
                    ("BYMONTHDAY" in parts and parts["FREQ"] != "MONTHLY")):
                raise ValueError("Use daily, weekly weekdays, or monthly RRULEs")
            if not 1 <= int(parts.get("INTERVAL", "1")) <= 366:
                raise ValueError("Interval must be 1 through 366")
            if "BYDAY" in parts and any(d not in {"MO", "TU", "WE", "TH", "FR", "SA", "SU"} for d in parts["BYDAY"].split(",")):
                raise ValueError("Use weekday names without ordinal prefixes")
            if "BYMONTHDAY" in parts and any(not 1 <= int(d) <= 31 for d in parts["BYMONTHDAY"].split(",")):
                raise ValueError("Month days must be 1 through 31")
            if "BYSETPOS" in parts:
                days = [int(d) for d in parts.get("BYMONTHDAY", "").split(",")]
                if parts["FREQ"] != "MONTHLY" or parts["BYSETPOS"] != "-1" or days != list(range(28, max(days) + 1)):
                    raise ValueError("Use BYSETPOS=-1 with consecutive month days 28 through the preferred day")
            if self.start_at.tzinfo is None:
                self.start_at = self.start_at.replace(tzinfo=zone)
            else:
                self.start_at = self.start_at.astimezone(zone)
            if self.start_at.microsecond:
                # RRULE has second precision; never skip its first occurrence or
                # purchase before the supplied fractional-second lower bound.
                self.start_at = self.start_at.replace(microsecond=0) + timedelta(seconds=1)
            rule = rrulestr(self.rrule.upper(), dtstart=self.start_at)
            if rule.after(self.start_at, inc=True) is None:
                raise ValueError("Cadence has no occurrence")
        except (ValueError, TypeError, ZoneInfoNotFoundError) as error:
            raise ValueError("Invalid recurrence rule or timezone") from error
        return self


class Deviations(Model):
    price_pct: float = Field(default=0, ge=0)
    cumulative_price_pct: float = Field(default=0, ge=0)
    quantity_pct: float = Field(default=0, ge=0)
    cumulative_quantity_pct: float = Field(default=0, ge=0)
    brand_change: bool = False
    pack_change: bool = False


class RecurringRequest(Model):
    approved_order_id: InputText
    cadence: Cadence
    permitted_deviations: Deviations = Field(default_factory=Deviations)


class BaselineRequest(Model):
    approved_order_id: InputText


class RunRequest(Model):
    occurrence_at: datetime | None = None


def rule_for(row):
    cadence = row["cadence"]
    start = datetime.fromisoformat(cadence["start_at"]).astimezone(ZoneInfo(cadence["timezone"]))
    return rrulestr(cadence["rrule"].upper(), dtstart=start)


def recent_schedule(row, now):
    """Skip whole cadence intervals arithmetically, preserving RRULE phase and wall time.

    Historical templates are accepted. A bounded recent window is enumerated
    outside the writer transaction, retaining skipped month days and leap years.
    """
    recent = deepcopy(row)
    cadence = recent["cadence"]
    start = datetime.fromisoformat(cadence["start_at"]).astimezone(ZoneInfo(cadence["timezone"]))
    local = now.astimezone(start.tzinfo)
    parts = dict(p.split("=", 1) for p in cadence["rrule"].upper().split(";"))
    interval = int(parts.get("INTERVAL", 1))
    if parts["FREQ"] == "DAILY":
        jump = max(0, (local.date() - start.date()).days // interval - 1) * interval
        anchor = start + timedelta(days=jump)
    elif parts["FREQ"] == "WEEKLY":
        jump = max(0, (local.date() - start.date()).days // (7 * interval) - 1) * 7 * interval
        anchor = start + timedelta(days=jump)
    else:
        months = (local.year - start.year) * 12 + local.month - start.month
        # Eight periods cover skipped month days and the Gregorian century leap
        # gap (e.g. February 2096 -> 2104), without scanning from DTSTART.
        jump = max(0, months // interval - 8) * interval
        parts.setdefault("BYMONTHDAY", str(start.day))
        anchor = start.replace(day=1) + relativedelta(months=jump) if jump else start
        cadence["rrule"] = ";".join(f"{key}={value}" for key, value in parts.items())
    cadence["start_at"] = anchor.isoformat()
    rule = rule_for(recent)
    return rule.before(local, inc=True), rule.after(local)


def reviewer(principal, customer):
    return (principal.kind == "buyer_user" and principal.role in {"buyer_admin", "buyer_approver"}
            and customer in principal.customer_ids)


def recurring_supplier(row):
    return row.get("supplier_id") or row["template"].get("supplier_id", "valio")


def physical_pack(product, price):
    """Content evidence independent of whether the buyer requested cases, litres or kg."""
    from .lookup import reconcile_size
    units = list(dict.fromkeys([price.get("comparisonUnit"), "L", "KG"]))
    for unit in units:
        if unit not in {"L", "KG"}:
            continue
        size, source, _, _ = reconcile_size(product, price, unit)
        if size:
            return {"size": size, "unit": unit, "source": source}
    return None


def snapshot(gateway, quote):
    lines = []
    for line in quote.lines:
        facts = (quote.context or {}).get("product_evidence", {}).get(line.product_id, {})
        # Historical physical-unit quotes retain their signed conversion. A historical
        # SALES_UNIT quote without physical evidence loads, but requires comparison review.
        physical = facts.get("physical_pack") or ({"size": line.size_per_sales_unit, "unit": line.size_unit,
                    "source": line.size_source} if line.size_unit != "SALES_UNIT" else {})
        pack = physical.get("size")
        lines.append({"product": line.product_id, "brand": facts.get("brand"), "pack": pack,
            "unit": physical.get("unit"), "sales_unit": line.sales_unit, "quantity": line.units_needed,
            "unit_cost": float(Decimal(str(line.unit_price)) / Decimal(str(pack)))
                if line.unit_price is not None and pack else None,
            "claims": facts.get("claims"), "allergens": facts.get("allergens"),
            "evidence": {"allergen_declaration": facts.get("allergen_declaration", False), "size_source": physical.get("source"),
                         "fit": line.fit is not None, "provenance": sorted(line.provenance),
                         "warnings": sorted(w.code for w in line.warnings)},
            "delivery_offset": (line.availability.available_date - quote.deliver_by).days
                if line.availability.available_date else None})
    return {"lines": lines, "basket_cost": quote.total}


def compare(current, previous, baseline, deviations):
    """Compare every dimension twice; gradual changes cannot reset the approved baseline."""
    diffs = []
    def number(label, new, old, base, per_run, cumulative):
        if new is None or old is None or base is None or old == 0 or base == 0:
            diffs.append(f"{label}: comparison evidence missing")
            return
        step = (Decimal(str(new)) / Decimal(str(old)) - 1) * 100
        total = (Decimal(str(new)) / Decimal(str(base)) - 1) * 100
        if abs(step) > Decimal(str(per_run)) or abs(total) > Decimal(str(cumulative)):
            diffs.append(f"{label} {old:g} → {new:g} ({step:+.1f} %, cumulative {total:+.1f} % since baseline)")
    number("basket cost €", current.get("basket_cost"), previous.get("basket_cost"), baseline.get("basket_cost"),
           deviations.price_pct, deviations.cumulative_price_pct)
    groups = [entry.get("lines") for entry in (current, previous, baseline)]
    if any(not isinstance(group, list) for group in groups):
        return diffs + ["product lines: comparison evidence missing"]
    if len(groups[0]) != len(groups[1]) or len(groups[0]) != len(groups[2]):
        diffs.append("product lines changed; comparison evidence missing")
    for index, line in enumerate(groups[0]):
        if index >= min(len(groups[1]), len(groups[2])):
            continue
        old, base = groups[1][index], groups[2][index]
        if not all(isinstance(item, dict) for item in (line, old, base)):
            diffs.append(f"line {index + 1}: comparison evidence missing")
            continue
        for name, permitted in (("product", False), ("brand", deviations.brand_change),
                ("pack", deviations.pack_change), ("unit", False), ("sales_unit", deviations.pack_change),
                ("claims", False), ("allergens", False), ("delivery_offset", False), ("evidence", False)):
            if line.get(name) is None or old.get(name) is None or base.get(name) is None:
                diffs.append(f"line {index + 1} {name}: comparison evidence missing")
            elif not permitted and (line[name] != old[name] or line[name] != base[name]):
                diffs.append(f"line {index + 1} {name}: {old[name]} → {line[name]} (baseline {base[name]})")
        evidence = line.get("evidence") or {}
        if not all(evidence.get(field) for field in ("allergen_declaration", "provenance", "size_source", "fit")):
            diffs.append(f"line {index + 1}: comparison evidence missing")
        number(f"line {index + 1} unit cost €", line.get("unit_cost"), old.get("unit_cost"), base.get("unit_cost"),
               deviations.price_pct, deviations.cumulative_price_pct)
        number(f"line {index + 1} quantity", line.get("quantity"), old.get("quantity"), base.get("quantity"),
               deviations.quantity_pct, deviations.cumulative_quantity_pct)
    return list(dict.fromkeys(diffs))


class Recurring:
    def __init__(self, gateway):
        self.g = gateway
        self.stop = threading.Event()
        self.thread = None

    def rows(self):
        with self.g.db.connect() as conn:
            return [json.loads(r[0]) for r in conn.execute("SELECT data FROM recurring_orders ORDER BY rowid")]

    def get(self, id, principal=None):
        row = next((r for r in self.rows() if r["id"] == id), None)
        if row is None or (principal and not (principal.id == row["agent_id"] and principal.kind == "agent"
                or principal.kind == "buyer_user" and row["customer_id"] in principal.customer_ids)):
            raise DomainError("RECURRING_NOT_FOUND", "Recurring order was not found", 404)
        if principal and principal.resource is not None and row["customer_id"] not in principal.customer_ids:
            raise DomainError("RECURRING_NOT_FOUND", "Recurring order was not found", 404)
        return row

    def save(self, conn, row):
        conn.execute("INSERT INTO recurring_orders VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                     (row["id"], json.dumps(row)))

    def authority(self, conn, row):
        policy, _, token = self.g.mandates.authorize(conn, None, row["agent_id"], row["customer_id"],
                                                    bound=("recurring", row["id"]), supplier_id=recurring_supplier(row))
        grant = policy.agents.get(row["agent_id"])
        allowed = decode(token)[1]["vc"]["credentialSubject"].get("recurring") is True if token else bool(grant and grant.recurring)
        if not allowed:
            raise DomainError("RECURRING_NOT_AUTHORIZED", "Mandate must explicitly allow recurring execution", 403)
        return token

    def approved(self, order_id):
        order = self.g.orders.get_order(order_id)
        if order.status != "CONFIRMED":
            raise DomainError("APPROVED_TEMPLATE_REQUIRED", "Use a confirmed order as the approved template", 422)
        quote = self.g.orders.get_quote(order.quote_id)
        with self.g.db.connect() as conn:
            lookup = self.g.orders.lookup_need(conn, quote.need_ref)
            basket = conn.execute("SELECT data FROM basket_lookups WHERE id=?", (quote.need_ref,)).fetchone()
        if basket:
            data = json.loads(basket[0])
            template = {"customer_id": quote.customer_id, "deliver_by": quote.deliver_by.isoformat(),
                        "manifest": data["scaled_manifest"]}
        else:
            template = lookup.need.model_dump(mode="json")
        return order, quote, template, bool(basket)

    def create(self, body, principal, mandate=None):
        order, quote, template, basket = self.approved(body.approved_order_id)
        if not reviewer(principal, quote.customer_id) and not (principal.kind == "agent" and principal.id == order.agent_id):
            raise DomainError("RECURRING_FORBIDDEN", "The template must belong to this buyer or agent", 403)
        row = {"id": str(uuid4()), "agent_id": order.agent_id, "customer_id": quote.customer_id,
               "supplier_id": quote.supplier_id,
               "template": template, "basket": basket, "approved_order_id": order.id, "baseline_revision": 1,
               "baseline": snapshot(self.g, quote), "cadence": body.cadence.model_dump(mode="json"),
               "approved_lines": [{"product_id": l.product_id, "quantity_sales_units": l.units_needed} for l in quote.lines],
               "permitted_deviations": body.permitted_deviations.model_dump(), "status": "ACTIVE",
               "delivery_offset_days": max(0, (quote.deliver_by - delivery_today()).days)}
        row["next_run_at"] = rule_for(row).after(body.cadence.start_at, inc=True).isoformat()
        with self.g.db.connect(write=True) as conn:
            token = mandate or self.g.mandates.stored(conn, "order", order.id)
            self.g.mandates.authorize(conn, token, order.agent_id, quote.customer_id,
                                      products=[line.product_id for line in quote.lines], supplier_id=quote.supplier_id)
            self.g.mandates.bind(conn, "recurring", row["id"], token)
            self.authority(conn, row)
            self.save(conn, row)
            audit.append(conn, f"{principal.kind}:{principal.id}", "RECURRING_CREATED",
                         "Created an approved recurring purchase template.", data={"id": row["id"],
                         "customer_id": row["customer_id"], "environment": self.g.settings.environment})
        return row

    def workflow_authority(self, principal, customer, mandate=None, *, supplier_id=None):
        """MCP workflows require the caller's current recurring permission, even on reads."""
        if principal.kind != "agent":
            raise DomainError("RECURRING_FORBIDDEN", "A purchasing agent credential is required", 403)
        with self.g.db.connect() as conn:
            policy, _, token = self.g.mandates.authorize(conn, mandate, principal.id, customer,
                supplier_id=supplier_id or self.g.memory.authority_supplier(principal, mandate))
            grant = policy.agents.get(principal.id)
            allowed = decode(token)[1]["vc"]["credentialSubject"].get("recurring") is True if token else bool(grant and grant.recurring)
            if not allowed:
                raise DomainError("RECURRING_NOT_AUTHORIZED", "Mandate must explicitly allow recurring execution", 403)

    def list_for_agent(self, principal, mandate=None):
        principal = self.g.memory.scoped(principal, mandate)
        # Check even when there are no workflows yet.
        if not principal.customer_ids:
            raise DomainError("RECURRING_NOT_AUTHORIZED", "A recurring customer mandate is required", 403)
        for customer in principal.customer_ids:
            self.workflow_authority(principal, customer, mandate)
        return [r for r in self.rows() if r["agent_id"] == principal.id and r["customer_id"] in principal.customer_ids]

    def from_suggestion(self, pattern, need, agent, body, mandate=None):
        """Use confirmed historical evidence as baseline; the first run revalidates it."""
        principal = Principal(agent, customer_ids=(pattern["customer_id"],))
        self.workflow_authority(principal, pattern["customer_id"], mandate, supplier_id=need.supplier_id)
        cadence = Cadence(rrule=pattern["cadence_rrule"], timezone=body.timezone,
                          start_at=datetime.combine(need.deliver_by, datetime.min.time()).replace(tzinfo=ZoneInfo(body.timezone)))
        latest = pattern["_latest"]
        baseline = deepcopy(latest["baseline"])
        for target, key, precision in ((baseline["lines"][0], "quantity", "0.000001"), (baseline, "basket_cost", "0.01")):
            target[key] = float((Decimal(str(target[key])) * Decimal(str(pattern["typical_quantity"])) /
                                 Decimal(str(latest["quantity"]))).quantize(Decimal(precision), rounding=ROUND_HALF_UP))
        row = {"id": str(uuid4()), "agent_id": agent, "customer_id": pattern["customer_id"],
               "supplier_id": need.supplier_id,
               "template": need.model_dump(mode="json"), "basket": False,
               "approved_order_id": latest["order_id"], "source_suggestion_id": pattern["id"],
               "baseline_revision": 1, "baseline": baseline, "cadence": cadence.model_dump(mode="json"),
               "approved_lines": [{"product_id": l["product"], "quantity_sales_units": l["quantity"]} for l in baseline["lines"]],
               "permitted_deviations": body.permitted_deviations.model_dump(), "status": "ACTIVE",
               "delivery_offset_days": 0}
        row["next_run_at"] = rule_for(row).after(cadence.start_at, inc=True).isoformat()
        with self.g.db.connect(write=True) as conn:
            self.g.mandates.bind(conn, "recurring", row["id"], mandate)
            self.authority(conn, row)
            self.save(conn, row)
            audit.append(conn, f"agent:{agent}", "RECURRING_CREATED", "Created a recurring purchase template from a suggestion.",
                         data={"id": row["id"], "customer_id": row["customer_id"], "environment": self.g.settings.environment})
        return row

    def pause(self, id, principal, mandate=None, *, paused=True):
        with self.g.db.connect(write=True) as conn:
            row = self.get(id, principal)
            self.workflow_authority(principal, row["customer_id"], mandate, supplier_id=recurring_supplier(row))
            row["status"] = "PAUSED" if paused else "ACTIVE"
            self.save(conn, row)
            action, reason = ("RECURRING_STOPPED", "paused") if paused else ("RECURRING_RESUMED", "resumed")
            audit.append(conn, f"agent:{principal.id}", action, f"Buyer agent {reason} recurring execution.",
                         data={"id": id, "customer_id": row["customer_id"], "reason": reason})
        return row

    def runs(self, id, principal=None):
        self.get(id, principal)
        with self.g.db.connect() as conn:
            return [json.loads(r[0]) for r in conn.execute("SELECT data FROM recurring_runs WHERE recurring_id=? ORDER BY rowid", (id,))]

    def baseline(self, id, body, principal):
        row = self.get(id, principal)
        if not reviewer(principal, row["customer_id"]):
            raise DomainError("BUYER_APPROVER_REQUIRED", "Only a buyer admin or approver may change the baseline", 403)
        order, quote, template, basket = self.approved(body.approved_order_id)
        if order.agent_id != row["agent_id"] or quote.customer_id != row["customer_id"]:
            raise DomainError("RECURRING_FORBIDDEN", "Baseline must belong to the same buyer and agent", 403)
        with self.g.db.connect(write=True) as conn:
            row = self.get(id, principal)
            row.update(baseline=snapshot(self.g, quote), template=template, basket=basket,
                       supplier_id=quote.supplier_id,
                       approved_lines=[{"product_id": l.product_id, "quantity_sales_units": l.units_needed} for l in quote.lines],
                       approved_order_id=order.id, baseline_revision=row["baseline_revision"] + 1)
            self.save(conn, row)
            audit.append(conn, f"human:{principal.role}:{principal.credential_ref}", "RECURRING_BASELINE",
                         "Buyer explicitly updated the approved recurring baseline.", data={"id": id, "customer_id": row["customer_id"], "revision": row["baseline_revision"]})
        return row

    def run(self, id, occurrence_at=None, principal=None):
        initial = self.get(id, principal)
        if initial["status"] != "ACTIVE":
            raise DomainError("INVALID_TRANSITION", "Recurring order is paused", 409)
        if principal and principal.kind == "buyer_user" and not reviewer(principal, initial["customer_id"]):
            raise DomainError("BUYER_APPROVER_REQUIRED", "Only a buyer approver may trigger a purchase", 403)
        when = occurrence_at or audit.utcnow()
        if when.tzinfo is None:
            raise DomainError("INVALID_OCCURRENCE", "Occurrence needs a timezone", 422)
        local = when.astimezone(ZoneInfo(initial["cadence"]["timezone"]))
        key = f"{id}:{local.date().isoformat()}"
        # Admission precedes the writer transaction so a throttle audit cannot roll back.
        with self.g.db.connect() as conn:
            existing = conn.execute("SELECT 1 FROM recurring_runs WHERE occurrence_key=?", (key,)).fetchone()
        self.g.limiter.consume(initial["agent_id"], "read" if existing else "mutation")
        # One transaction includes the command, fresh lookup, quote, comparison and order.
        with self.g.db.connect(write=True) as conn:
            row = self.get(id, principal)
            if row["status"] != "ACTIVE":
                raise DomainError("INVALID_TRANSITION", "Recurring order is paused", 409)
            existing = conn.execute("SELECT data FROM recurring_runs WHERE occurrence_key=?", (key,)).fetchone()
            if existing:
                return json.loads(existing[0])
            result = {"occurrence_key": key, "occurrence_at": local.isoformat(), "baseline_revision": row["baseline_revision"]}
            try:
                token = self.authority(conn, row)
            except DomainError as error:
                result.update(status="SKIPPED", reason=error.code)
                audit.append(conn, "system:recurring", "RECURRING_STOPPED", error.message,
                             data={"id": id, "customer_id": row["customer_id"], **result})
            else:
                # Scheduler authority is the original agent; the caller cannot lend its privileges.
                context = PRINCIPAL.set(self.g.accounts.bind(Principal(row["agent_id"], customer_ids=(row["customer_id"],))))
                try:
                    data = dict(row["template"])
                    # Cadence timezone controls occurrence identity; delivery
                    # bounds use the same Helsinki day as RequestScope.
                    execution_date = delivery_today()
                    data["deliver_by"] = (max(local.date(), execution_date) + timedelta(days=row["delivery_offset_days"])).isoformat()
                    self.g.valio.cache.clear()
                    if row["basket"]:
                        lookup = self.g.baskets.find_basket_offers(BasketLookupRequest.model_validate(data), f"agent:{row['agent_id']}", mandate=token)
                        request = QuoteRequest(customer_id=row["customer_id"], deliver_by=data["deliver_by"], basket_ref={"basket_lookup_id": lookup.basket_lookup_id})
                    else:
                        approved_lines = row.get("approved_lines") or [{"product_id": l["product"], "quantity_sales_units": l["quantity"]}
                                                                     for l in row["baseline"]["lines"]]
                        lookup = self.g.find_offers(NeedSpec.model_validate(data), f"agent:{row['agent_id']}", mandate=token,
                                                   product_ids=[l["product_id"] for l in approved_lines])
                        if not lookup.offers:
                            raise DomainError("NO_RECURRING_OFFER", "No current purchasable offer for this template", 422)
                        request = QuoteRequest(customer_id=row["customer_id"], deliver_by=data["deliver_by"], need_ref=lookup.lookup_id,
                            lines=approved_lines)
                    quote = self.g.orders.create_quote(request, row["agent_id"], mandate=token)
                    current = snapshot(self.g, quote)
                    prior_runs = self.runs(id)
                    previous = next((r["comparison"]["current"] for r in reversed(prior_runs)
                                     if "comparison" in r and r["baseline_revision"] == row["baseline_revision"]), row["baseline"])
                    diff = compare(current, previous, row["baseline"], Deviations.model_validate(row["permitted_deviations"]))
                    quote.context["recurring_drift"] = diff
                    quote.context["recurring_occurrence"] = key
                    quote.context["recurring_comparison"] = {"previous": previous, "baseline": row["baseline"],
                                                            "deviations": row["permitted_deviations"]}
                    quote.signature = self.g.signing.sign_quote(quote)
                    self.g.orders._save_quote(conn, quote)
                    order = self.g.orders.place_order(quote.id, key, row["agent_id"], mandate=token)
                    result.update(status=order.status, request_id=quote.need_ref, quote_id=quote.id, order_id=order.id,
                                  comparison=order.context["recurring_comparison"])
                    audit.append(conn, "system:recurring", "RECURRING_RUN", f"Recurring occurrence {order.status}.",
                                 data={"id": id, "customer_id": row["customer_id"], **result})
                finally:
                    PRINCIPAL.reset(context)
            conn.execute("INSERT INTO recurring_runs VALUES(?,?,?)", (key, id, json.dumps(result)))
        return result

    def due(self, now=None):
        now = now or audit.utcnow()
        self.g.stockout_watch.tick(now)
        for candidate in self.rows():
            try:
                if candidate["status"] != "ACTIVE" or datetime.fromisoformat(candidate["next_run_at"]) > now:
                    continue
                latest, following = recent_schedule(candidate, now)
                with self.g.db.connect(write=True) as conn:
                    row = self.get(candidate["id"])
                    if (row["status"] != "ACTIVE" or row["next_run_at"] != candidate["next_run_at"]
                            or row["cadence"] != candidate["cadence"] or row["baseline_revision"] != candidate["baseline_revision"]):
                        continue
                    if latest:
                        self.run(row["id"], latest)
                    row["next_run_at"] = following.isoformat()
                    self.save(conn, row)
            except Exception:
                logging.getLogger(__name__).exception("Recurring occurrence failed; retry on next scheduler tick")

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop.clear()
        def worker():
            while not self.stop.wait(1):
                self.due()
        self.thread = threading.Thread(target=worker, name="recurring-scheduler", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join()


router = APIRouter(prefix="/v1/recurring-orders", tags=["Recurring orders"])


@router.post("", response_model=dict, summary="Approve a recurring template from a confirmed need or recipe order")
def create(body: RecurringRequest, request: Request, principal: Principal = Depends(resolve_principal),
           agent_mandate: str | None = Header(default=None),
           idempotency_key: str | None = Header(default=None, min_length=1, max_length=200)):
    return request.app.state.gateway.recurring.create(body, principal, agent_mandate, command_key=idempotency_key)


@router.get("", response_model=list[dict])
def listing(request: Request, principal: Principal = Depends(resolve_principal)):
    service = request.app.state.gateway.recurring
    return [r for r in service.rows() if (principal.kind == "agent" and r["agent_id"] == principal.id)
            or (principal.kind == "buyer_user" and r["customer_id"] in principal.customer_ids)]


@router.get("/{id}/runs", response_model=list[dict])
def runs(id: str, request: Request, principal: Principal = Depends(resolve_principal)):
    return request.app.state.gateway.recurring.runs(id, principal)


@router.post("/{id}/baseline", response_model=dict)
def baseline(id: str, body: BaselineRequest, request: Request, principal: Principal = Depends(resolve_principal),
             idempotency_key: str | None = Header(default=None, min_length=1, max_length=200)):
    return request.app.state.gateway.recurring.baseline(id, body, principal, command_key=idempotency_key)


@router.post("/{id}/run-now", response_model=dict, summary="Execute one occurrence, deduplicated by template and local date")
def run(id: str, request: Request, body: RunRequest = RunRequest(), principal: Principal = Depends(resolve_principal),
        idempotency_key: str | None = Header(default=None, min_length=1, max_length=200)):
    return request.app.state.gateway.recurring.run(id, body.occurrence_at, principal, command_key=idempotency_key)


@router.post("/{id}/pause", response_model=dict, include_in_schema=False,
             summary="Pause your recurring workflow under current recurring permission")
def pause(id: str, request: Request, principal: Principal = Depends(resolve_principal),
          agent_mandate: str | None = Header(default=None),
          idempotency_key: str | None = Header(default=None, min_length=1, max_length=200)):
    return request.app.state.gateway.recurring.pause(id, principal, agent_mandate, command_key=idempotency_key)


@router.post("/{id}/resume", response_model=dict, include_in_schema=False,
             summary="Resume your recurring workflow under current recurring permission")
def resume(id: str, request: Request, principal: Principal = Depends(resolve_principal),
           agent_mandate: str | None = Header(default=None),
           idempotency_key: str | None = Header(default=None, min_length=1, max_length=200)):
    return request.app.state.gateway.recurring.pause(id, principal, agent_mandate, paused=False, command_key=idempotency_key)
