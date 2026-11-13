from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Quantity(Model):
    value: float = Field(gt=0, description="Requested positive quantity.")
    unit: Literal["L", "KG", "SALES_UNIT"] = Field(description="Litres, kilograms or product sales units.")


class NeedSpec(Model):
    customer_id: str = Field(default="1", min_length=1, description="Valio customer identifier.")
    intent: str = Field(min_length=1, description="Buyer need; the outside agent structures this request.")
    search_terms: list[str] | None = Field(default=None, description="Optional FTS terms, combined with AND.")
    required_claims: list[str] = Field(default_factory=list, description="All must exist as catalog nutritionalClaim IDs.")
    exclude_allergens: list[str] = Field(default_factory=list, description="Exclude products whose catalog allergen CONTAINS includes any ID.")
    quantity: Quantity
    deliver_by: date = Field(description="Latest acceptable delivery date.")
    max_candidates: int = Field(default=10, ge=1, le=10, description="Maximum shortlisted products sent to live APIs.")


class Warning(Model):
    code: str = Field(description="Stable machine-readable warning code.")
    text: str = Field(description="Human-readable explanation.")


class LiveCalls(Model):
    price: int = Field(default=0, description="Actual outbound price HTTP requests, including failed attempts.")
    availability: int = Field(default=0, description="Actual outbound availability HTTP requests.")


class Product(Model):
    id: str
    name_en: str
    name_fi: str = ""
    category: str | None = None
    sales_unit: str
    base_unit: str
    allowed_lot_size: float = 1
    vendor_name: str | None = None
    brand: str | None = None
    country: str | None = None
    temperature_condition: int | str | None = None
    units: dict[str, float] = Field(default_factory=dict, description="unitId to sizeInBaseUnits.")
    claims: list[str] = Field(default_factory=list)
    allergens_contains: list[str] = Field(default_factory=list)
    free_from: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    base_volume_l: float | None = None
    base_net_weight_kg: float | None = None
    delivery_reliability: float | None = None
    co2_kg_per_kg: float | None = Field(default=None, description="Product-group average, not an individual product measurement.")


class Provenance(Model):
    source: str = Field(description="Upstream endpoint or catalog source.")
    fetched_at: datetime = Field(description="Upstream timeNow when supplied; otherwise local UTC receipt time.")
    valid_to: datetime | None = None


class Availability(Model):
    available_amount: float = Field(description="Sales units deliverable by the deadline, after local Stock reservations.")
    logistic_model: str | None = None
    closing_time: datetime | None = None
    available_date: date | None = Field(default=None, description="Earliest unblocked upstream delivery date.")
    enough: bool
    coverage: float = Field(ge=0, le=1, description="Fraction of requested sales units covered by on-time stock.")


class OfferCard(Model):
    product_id: str
    name: str
    sales_unit: str
    size_per_sales_unit: float | None = Field(default=None, description="Quantity unit per sales unit; null when conversion is unknown.")
    size_unit: Literal["L", "KG", "SALES_UNIT"]
    size_source: str | None = Field(default=None, description="price_ratio, catalog, name_parse, base_unit or sales_unit.")
    units_needed: float | None = Field(default=None, description="Whole sales units; KG sales quantities are rounded to the allowed lot step.")
    unit_price: float | None = Field(default=None, description="Net catalog price per sales unit, fixed on quote creation.")
    unit_price_with_tax: float | None = None
    price_per_l_or_kg: float | None = None
    total: float | None = Field(default=None, description="Net line total in currency.")
    currency: str = "EUR"
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    net_price_found: bool = False
    availability: Availability
    warnings: list[Warning] = Field(default_factory=list)
    delivery_reliability: float | None = None
    co2_kg_per_kg: float | None = None
    labels: list[str] = Field(default_factory=list)
    matched_because: list[str] = Field(default_factory=list)
    fit: float | None = Field(default=None, ge=0, le=1, description="Decisions predicate probability; never inferred from a name alone.")
    fit_question: str | None = None
    provenance: dict[str, Provenance]
    score: float = 0
    rank_label: Literal["best", "better", "good"] | None = None


class LookupResult(Model):
    lookup_id: str = Field(description="Server-side need reference; pass as need_ref when quoting to preserve fit and constraints.")
    need: NeedSpec
    offers: list[OfferCard]
    excluded_count: int = Field(description="FTS matches removed by hard catalog filters, before shortlist truncation.")
    live_calls: LiveCalls
    decision_calls: int
    warnings: list[Warning] = Field(default_factory=list)


class QuoteLineRequest(Model):
    product_id: str = Field(min_length=1)
    quantity_sales_units: float = Field(gt=0, description="Explicit sales quantity; KG follows allowedLotSize, others are whole sales units.")


class QuoteRequest(Model):
    customer_id: str = Field(default="1", min_length=1)
    deliver_by: date
    lines: list[QuoteLineRequest] = Field(min_length=1, max_length=10)
    need_ref: str | None = Field(default=None, description="lookup_id to carry verified fit scores and hard constraints into policy.")

    @model_validator(mode="after")
    def unique_lines(self):
        if len({x.product_id for x in self.lines}) != len(self.lines):
            raise ValueError("Duplicate products must be combined into one line")
        return self


class Quote(Model):
    id: str
    customer_id: str
    deliver_by: date
    need_ref: str | None = None
    status: Literal["OPEN", "ACCEPTED", "EXPIRED"]
    created_at: datetime
    expires_at: datetime = Field(description="Earliest price validity, future cutoff or 30 minutes after creation.")
    lines: list[OfferCard] = Field(description="Fixed-price lines; units_needed is the quoted sales quantity.")
    total: float
    currency: str
    hard_constraint_violations: list[str] = Field(default_factory=list)
    live_calls: LiveCalls


class RuleHit(Model):
    rule: str
    detail: str


class PolicyResult(Model):
    outcome: Literal["CONFIRMED", "PENDING_APPROVAL", "REJECTED"]
    hits: list[RuleHit]


class OrderRequest(Model):
    quote_id: str = Field(description="An unexpired OPEN fixed-price quote.")


class Order(Model):
    id: str
    quote_id: str
    agent_id: str
    idempotency_key: str
    status: Literal["CONFIRMED", "PENDING_APPROVAL", "REJECTED", "EXPIRED", "CANCELLED"]
    created_at: datetime
    expires_at: datetime
    total: float
    currency: str
    lines: list[OfferCard]
    policy: PolicyResult
    live_calls: LiveCalls
    approver: str | None = None
    comment: str | None = None


class ApprovalRequest(Model):
    approver: str = Field(min_length=1, description="Human approver name (MVP: one role, no authentication).")
    comment: str = ""


class ApprovalItem(Model):
    order: Order
    quote: Quote


class AuditEntry(Model):
    id: int
    ts: datetime
    actor: str
    action: str
    order_id: str | None
    quote_id: str | None
    summary: str
    data: dict[str, Any]


class ErrorResponse(Model):
    code: str = Field(description="Stable domain error code, e.g. EXPIRED_QUOTE.")
    message: str


class Health(Model):
    status: str
    catalog_rows: int
