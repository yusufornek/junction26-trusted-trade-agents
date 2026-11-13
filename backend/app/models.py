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
    max_candidates: int = Field(default=10, ge=1, le=10, description="Maximum ranked offers returned; suitable candidates are priced up to shortlist.live_max.")


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
    size_confirmed_by_price: bool = Field(default=False, description="True when the live price ratio agrees with the selected exact catalog or name size within 3%.")
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
    priced_count: int = Field(default=0, ge=0, description="Shortlisted products checked for live prices, including cached evidence.")
    screened_count: int = Field(default=0, ge=0, description="Catalog pool size submitted for Decisions screening.")
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
    cancelled_by: str | None = Field(default=None, description="Human name or placing agent ID responsible for cancellation.")
    cancel_reason: str | None = Field(default=None, description="Reason supplied when the order was cancelled.")


class CancelRequest(Model):
    cancelled_by: str = Field(min_length=1, description="Non-blank human name responsible for cancellation.")
    reason: str = ""

    @model_validator(mode="after")
    def named_human(self):
        self.cancelled_by = self.cancelled_by.strip()
        if not self.cancelled_by:
            raise ValueError("cancelled_by must contain a human name")
        return self


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


class TimelineStep(Model):
    ts: datetime = Field(description="UTC audit event time; steps are oldest first, with audit ID breaking ties.")
    actor: str = Field(description="Recorded actor, such as agent:buyer, human:Ada or system.")
    action: str = Field(description="Original audit action, including replays and failed attempts.")
    step: Literal["lookup", "quote", "order", "decision", "cancellation", "system"] = Field(description="Human-facing stage of the order lifecycle.")
    summary: str = Field(description="Original human-readable audit summary.")
    data: dict[str, Any] = Field(description="Unmodified audit payload, including inputs, evidence and policy rule hits.")
    live_calls: LiveCalls = Field(default_factory=LiveCalls, description="Actual Valio requests for this event; absent audit counts default to zero.")
    decision_calls: int = Field(default=0, description="Actual Decisions requests for this event; absent audit counts default to zero.")


class OrderTimeline(Model):
    order_id: str = Field(description="Order whose complete history is returned.")
    quote_id: str = Field(description="Fixed-price quote accepted by the order.")
    lookup_id: str | None = Field(description="Quote need_ref linking the original lookup; null for explicit selection without a lookup.")
    agent_id: str = Field(description="Agent that placed the order.")
    status: Literal["CONFIRMED", "PENDING_APPROVAL", "REJECTED", "EXPIRED", "CANCELLED"] = Field(description="Current local order status after refreshing expirations.")
    steps: list[TimelineStep] = Field(description="Related lookup, quote and order audit events, oldest first; each event appears once.")
    total_live_calls: LiveCalls = Field(description="Sum of actual Valio requests across all steps, including failed checks and human approval.")
    total_decision_calls: int = Field(description="Sum of actual Decisions requests across all steps.")


class ErrorResponse(Model):
    code: str = Field(description="Stable domain error code, e.g. EXPIRED_QUOTE.")
    message: str


class Health(Model):
    status: str
    catalog_rows: int


class ApprovalPolicy(Model):
    max_auto_total_eur: float = Field(ge=0, description="Net EUR ceiling for automatic confirmation.")
    min_fit_probability: float = Field(ge=0, le=1, description="Minimum fit probability for automatic confirmation.")
    warnings_need_approval: list[str] = Field(description="Known warning codes requiring human review.")


class RejectPolicy(Model):
    on_hard_constraint_violation: bool = Field(description="Reject when catalog hard constraints fail.")
    on: list[str] = Field(description="Known warning codes causing rejection.")


class RankingWeights(Model):
    price: float = Field(ge=0)
    coverage: float = Field(ge=0)
    reliability: float = Field(ge=0)
    co2: float = Field(ge=0)
    fit: float = Field(ge=0)

    @model_validator(mode="after")
    def positive_sum(self):
        if sum(self.model_dump().values()) <= 0:
            raise ValueError("ranking_weights must have a sum greater than zero")
        return self


class ShortlistPolicy(Model):
    catalog_pool: int = Field(ge=10, le=100, description="Catalog candidates screened before live Valio calls.")
    live_max: int = Field(default=25, ge=10, le=40, description="Maximum suitable candidates priced before returning the top offers.")


class AgentMandate(Model):
    customer_ids: list[str] = Field(min_length=1, description="Customers this agent may quote and order for.")
    max_auto_total_eur: float | None = Field(default=None, ge=0, description="Optional per-order ceiling; the lower of this and the global ceiling applies.")
    max_auto_daily_total_eur: float = Field(ge=0, description="Rolling 24-hour autonomous EUR ceiling; cancelled and human-approved orders are excluded.")
    note: str = Field(default="", description="Free-text record of who granted the mandate.")

    @model_validator(mode="after")
    def named_customers(self):
        if any(not customer.strip() for customer in self.customer_ids):
            raise ValueError("customer_ids must contain non-blank customer identifiers")
        return self


class PolicyDocument(Model):
    approval: ApprovalPolicy
    reject: RejectPolicy
    ranking_weights: RankingWeights
    shortlist: ShortlistPolicy = Field(default_factory=lambda: ShortlistPolicy(catalog_pool=40))
    agents: dict[str, AgentMandate] = Field(default_factory=dict, description="Agent identity to mandate; unregistered agents always require human approval.")

    @model_validator(mode="after")
    def known_codes(self):
        from .lookup import WARNING_TEXT

        unknown = set(self.approval.warnings_need_approval + self.reject.on) - WARNING_TEXT.keys()
        if unknown:
            raise ValueError(f"Unknown warning codes: {', '.join(sorted(unknown))}")
        if any(not agent.strip() for agent in self.agents):
            raise ValueError("agents must have non-blank identifiers")
        return self


class PolicyResource(PolicyDocument):
    updated_at: datetime | None = Field(default=None, description="Last runtime update in UTC; null for committed defaults.")
    updated_by: str | None = Field(default=None, description="Human responsible for the last runtime update.")
    known_warning_codes: list[str] = Field(default_factory=list, description="All warning codes the lookup engine can emit; server supplied.")


class PolicyResetRequest(Model):
    updated_by: str = Field(min_length=1, description="Name of the human changing the policy.")

    @model_validator(mode="after")
    def named_human(self):
        self.updated_by = self.updated_by.strip()
        if not self.updated_by:
            raise ValueError("updated_by must contain a human name")
        return self


class PolicyUpdateRequest(PolicyResetRequest):
    policy: PolicyResource = Field(description="Editable policy sections. Response metadata may be echoed; the server replaces it.")
    comment: str | None = Field(default=None, description="Optional reason for the policy change.")


class AverageLiveCalls(Model):
    price: float = Field(description="Actual price requests divided by lookups in the window; zero without lookups.")
    availability: float = Field(description="Actual availability requests divided by lookups in the window; zero without lookups.")


class HumanDecisions(Model):
    approved: int = Field(description="Successful human APPROVE audit events in the window.")
    rejected: int = Field(description="Successful human REJECT audit events in the window.")


class WarningCount(Model):
    code: str = Field(description="Warning code found on a stored lookup offer.")
    count: int = Field(description="Occurrences across offers, excluding lookup-level duplicates.")


class RuleCount(Model):
    rule: str = Field(description="Placement policy rule identifier.")
    count: int = Field(description="Rule-hit occurrences in PLACE_ORDER audit events.")


class AgentStats(Model):
    actor: str = Field(description="Distinct agent audit actor, including the agent: prefix.")
    order_count: int = Field(description="Orders created by this agent in the window, regardless of current status.")
    registered: bool = Field(description="Whether the agent currently has a mandate in the effective policy.")


class Stats(Model):
    since: datetime | None = Field(description="Inclusive UTC lower bound; naive input is interpreted as UTC. Null means all history.")
    lookups: int = Field(description="LOOKUP audit events in the window.")
    orders_by_status: dict[str, int] = Field(description="Current statuses of orders created in the window, including zero counts.")
    placement_outcomes: dict[str, int] = Field(description="Original CONFIRMED/PENDING_APPROVAL/REJECTED outcomes from PLACE_ORDER events in the window; retries excluded.")
    human_decisions: HumanDecisions = Field(description="Successful human decisions by event time, even for older orders.")
    confirmed_spend_total_eur: float = Field(description="Net EUR total of currently CONFIRMED orders created in the window.")
    average_live_calls_per_lookup: AverageLiveCalls = Field(description="Lookup-only outbound Valio request averages; quote and order checks excluded.")
    total_decisions_calls: int = Field(description="Actual Decisions HTTP requests recorded in audit events in the window.")
    top_warning_codes: list[WarningCount] = Field(description="Top five offer warning codes in lookups in the window; count descending, code ascending on ties.")
    top_rule_hits: list[RuleCount] = Field(description="Top five placement rule hits; count descending, rule ascending on ties.")
    agents_seen: list[AgentStats] = Field(description="Agent actors seen in audit events in the window, sorted by actor, with order counts.")
