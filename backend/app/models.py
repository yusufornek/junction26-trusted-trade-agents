from datetime import date, datetime, timedelta
import re
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, PrivateAttr, WrapValidator, computed_field, field_validator, model_serializer, model_validator
from pydantic_core import PydanticCustomError


def historical_input(value, handler, info):
    # New request bounds must not invalidate already persisted procurement evidence.
    if (info.context or {}).get("stored"):
        return value
    return handler(value)


InputText = Annotated[str, Field(max_length=4000), WrapValidator(historical_input)]
InputTexts = Annotated[list[InputText], Field(max_length=100), WrapValidator(historical_input)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    @model_serializer(mode="wrap")
    def additive_fields(self, handler):
        data = handler(self)
        # Preserve historical signatures when loading resources issued before specs 25/26.
        for field in ("supplier_id", "environment", "synthetic"):
            if field not in self.model_fields_set:
                data.pop(field, None)
        return data


AutonomyLevel = Literal["approval_required", "autonomous_within_limits"]


class MandateRecord(Model):
    id: str
    issuer: str
    limits: dict[str, float]
    valid_until: datetime
    autonomy: AutonomyLevel | None = Field(default=None, exclude_if=lambda v: v is None)


class Quantity(Model):
    value: float = Field(gt=0, le=1_000_000, strict=True, description="Requested positive quantity, at most 1,000,000.")
    unit: Literal["L", "KG", "SALES_UNIT"] = Field(description="Litres, kilograms or product sales units.")

    @field_validator("unit", mode="before")
    @classmethod
    def known_unit(cls, value):
        unit = value.strip().upper() if isinstance(value, str) else value
        if unit not in ("L", "KG", "SALES_UNIT"):
            raise PydanticCustomError("UNKNOWN_CODE", "quantity.unit: unknown code {code}; valid values: L, KG, SALES_UNIT", {"code": str(value)})
        return unit


class PackSizePreference(Model):
    min: float | None = Field(default=None, gt=0, strict=True)
    max: float | None = Field(default=None, gt=0, strict=True)

    @model_validator(mode="after")
    def ordered_bounds(self):
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("pack_size.min must not exceed pack_size.max")
        return self


class Preferences(Model):
    brands: InputTexts = Field(default_factory=list)
    max_price_per_unit: float | None = Field(default=None, ge=0, strict=True, description="Maximum net EUR per L or KG; strict number.")
    pack_size: PackSizePreference | None = Field(default=None, description="L or KG per sales unit.")
    labels: InputTexts = Field(default_factory=list, description="Preferred catalog packaging label IDs.")

    @field_validator("brands")
    @classmethod
    def nonblank_brands(cls, values):
        if any(not value.strip() for value in values):
            raise ValueError("brands must contain non-blank entries")
        return [value.strip() for value in values]


class RequestScope(Model):
    customer_id: InputText = Field(min_length=1, description="Required non-blank Valio customer identifier.")
    deliver_by: date = Field(description="Delivery date between today and today + 60 days, Europe/Helsinki.")

    @field_validator("customer_id")
    @classmethod
    def nonblank_customer(cls, value):
        if not value.strip():
            raise ValueError("customer_id must not be blank")
        return value.strip()

    @field_validator("deliver_by")
    @classmethod
    def delivery_window(cls, value, info):
        # Historical requests remain readable after their input window passes.
        if not (info.context or {}).get("stored"):
            from .runtime import CLOCK
            clock = CLOCK.get()
            today = (clock() if clock else datetime.now(ZoneInfo("Europe/Helsinki"))).astimezone(ZoneInfo("Europe/Helsinki")).date()
            if not today <= value <= today + timedelta(days=60):
                raise ValueError("deliver_by must be between today and today + 60 days (Europe/Helsinki)")
        return value


class FunctionalNeed(Model):
    product_type: InputText = Field(min_length=1)
    intended_use: InputText = Field(min_length=1)
    preparation: InputText | None = None
    acceptable_units: list[Literal["L", "KG", "SALES_UNIT"]] = Field(min_length=1)


class SubstituteFor(Model):
    product_id: InputText = Field(min_length=1)


class NeedSpec(RequestScope):
    function: FunctionalNeed | None = Field(default=None, exclude_if=lambda v: v is None)
    substitute_for: SubstituteFor | None = Field(default=None, exclude_if=lambda v: v is None)
    supplier_id: InputText = Field(default="valio", min_length=1, description="One supplier per procurement request; valio by default. nordfood is synthetic.")

    intent: InputText = Field(min_length=1, description="Buyer need; the outside agent structures this request.")
    search_terms: InputTexts | None = Field(default=None, description="Optional FTS terms, combined with AND.")
    required_claims: InputTexts = Field(default_factory=list, description="All must exist as catalog nutritionalClaim IDs.")
    exclude_allergens: InputTexts = Field(default_factory=list, description="Requires an uncontradicted free-from entry or a declaration not listing the excluded allergen; missing data never proves absence.")
    quantity: Quantity
    max_candidates: int = Field(default=10, ge=1, le=10, strict=True, description="Strict integer maximum ranked offers returned; suitable candidates are priced up to shortlist.live_max.")
    preferences: Preferences = Field(default_factory=Preferences, description="Soft preferences; differences mark substitutes and require approval by default.")


class AnomalyFix(Model):
    id: str
    kind: Literal["correct_quantity", "cheaper_alternative", "split", "pause", "resume"]
    product_id: str | None = None
    quantity_sales_units: float | None = None
    remainder_sales_units: float | None = None
    description: str
    action_url: str | None = None


class AnomalyCheck(Model):
    code: Literal["QUANTITY_ANOMALY", "PRICE_ANOMALY", "VELOCITY_ANOMALY", "NEW_CATEGORY"]
    status: Literal["normal", "anomaly", "insufficient_history"]
    product_id: str | None = None
    observed: float | str
    expected_band: dict[str, float] | None = None
    band_source: Literal["statistical", "forecast"] = "statistical"
    history_window: dict
    unit: str
    fixes: list[AnomalyFix] = Field(default_factory=list)
    explanation_text: str | None = None


class Warning(Model):
    code: str = Field(description="Stable machine-readable warning code.")
    text: str = Field(description="Human-readable explanation.")


class LiveCalls(Model):
    price: int = Field(default=0, description="Actual outbound price HTTP requests, including failed attempts.")
    availability: int = Field(default=0, description="Actual outbound availability HTTP requests.")


class Product(Model):
    supplier_id: str = "valio"
    supplier_sku: str | None = Field(default=None, exclude_if=lambda v: v is None)
    synthetic: bool = False
    id: str
    gtin: str | None = Field(default=None, description="Catalog salesUnitGtin, preserving leading zeros.")
    unit_gtins: dict[str, str] = Field(default_factory=dict, description="Catalog units[].unitId to gtin.")
    name_en: str
    name_fi: str = ""
    category: str | None = None
    sales_unit: str
    base_unit: str
    minimum_order_quantity: float = Field(default=1, ge=1, description="Minimum sales quantity, independent of the order multiple.")
    allowed_lot_size: float = Field(default=1, description="Whole sales-unit step; organiser lot sizes below one count as one, including KG.")

    @field_validator("allowed_lot_size")
    @classmethod
    def whole_lot(cls, value):
        from math import ceil
        return float(max(1, ceil(value)))
    vendor_name: str | None = None
    brand: str | None = None
    country: str | None = None
    temperature_condition: int | str | None = None
    units: dict[str, float] = Field(default_factory=dict, description="unitId to sizeInBaseUnits.")
    claims: list[str] = Field(default_factory=list)
    allergens_contains: list[str] = Field(default_factory=list)
    allergen_status: dict[str, list[Literal["contains", "may_contain", "undeclared", "free_from"]]] = Field(default_factory=dict)
    has_allergen_declaration: bool = False
    free_from: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    base_volume_l: float | None = None
    base_net_weight_kg: float | None = None
    size_suspect: bool = False
    volume_suspect: bool | None = Field(default=None, description="Volume evidence is suspect independently of weight; null on legacy records, recalculated when used.")
    weight_suspect: bool | None = Field(default=None, description="Weight evidence is suspect independently of volume; null on legacy records, recalculated when used.")
    sales_volume_l: float | None = Field(default=None, description="Content of the CASE record when sold as a case.")
    sales_net_weight_kg: float | None = None
    variable_unit: bool = False
    deposits: list[dict[str, Any]] = Field(default_factory=list)
    delivery_reliability: float | None = None
    co2_kg_per_kg: float | None = Field(default=None, description="Product-group average, not an individual product measurement.")
    density_kg_per_l: float | None = Field(default=None, gt=0, strict=True, exclude_if=lambda v: v is None,
                                          description="Explicit catalog density evidence; never inferred from product type.")


class Provenance(Model):
    environment: Literal["live", "sandbox"] = "live"
    synthetic: bool = False
    requested_date: date | None = Field(default=None, exclude_if=lambda v: v is None)
    returned_dates: list[date] | None = Field(default=None, exclude_if=lambda v: v is None)
    source: str = Field(description="Upstream endpoint or catalog source.")
    fetched_at: datetime = Field(description="Upstream timeNow when supplied; otherwise local UTC receipt time.")
    valid_to: datetime | None = None


class Availability(Model):
    stock_semantics: Literal["finite", "unlimited", "unknown"] = "finite"
    available_amount: float | None = Field(description="On-time sales units after local reservations; null when a usable on-time upstream amount >= 999999 means unknown but orderable.")
    availability_known: bool = Field(default=True, description="False for usable on-time upstream amounts >= 999999, independently of logistic model; no local reservation is counted. Later unknown stock does not exempt finite on-time stock from reservations.")
    logistic_model: str | None = None
    closing_time: datetime | None = None
    available_date: date | None = Field(default=None, description="Earliest unblocked upstream delivery date.")
    enough: bool
    coverage: float = Field(ge=0, le=1, description="Fraction of requested sales units covered by on-time stock.")

    @model_serializer(mode="wrap")
    def preserve_signed_field_presence(self, handler):
        data = handler(self)
        # Historical evidence must retain its signed shape through reads and saves.
        if "availability_known" not in self.model_fields_set:
            data.pop("availability_known", None)
        if "stock_semantics" not in self.model_fields_set:
            data.pop("stock_semantics", None)
        return data


class Substitution(Model):
    model_config = ConfigDict(extra="forbid", serialize_by_alias=True)
    for_product: str = Field(alias="for")
    differences: list[str]
    allergen_evidence: list[str]
    comparison: dict | None = Field(default=None, exclude_if=lambda v: v is None,
        description="Signed quote-time brand, pack and comparable price-increase evidence. Absent on historical records; missing evidence requires review.")


class NameSizeCandidate(Model):
    size_per_sales_unit: float = Field(description="Plausible case content in the offer's size_unit; display evidence, not proof of conversion.")
    source_text: str = Field(description="Original product name containing this measure.")
    expression: str = Field(description="Local measure and count expression; repeated complete expressions remain independent.")


class OfferCard(Model):
    supplier_id: str = "valio"
    synthetic: bool = False
    tax_basis: Literal["net"] = Field(default="net", exclude_if=lambda v: v == "net")
    substitution: Substitution | None = Field(default=None, exclude_if=lambda v: v is None)

    product_id: str
    gtin: str | None = Field(default=None, description="GS1 GTIN of the sales unit, from salesUnitGtin.")
    name: str
    sales_unit: str
    minimum_order_quantity: float = Field(default=1, ge=1, description="Minimum sales quantity, independent of allowed_lot_size.")
    allowed_lot_size: float = Field(default=1, ge=1, description="Effective whole sales-unit step used by this offer and its plans, including KG.")
    size_per_sales_unit: float | None = Field(default=None, description="Positive quantity unit per sales unit, quantized to six decimal places before pack arithmetic; CASE content already includes its multiplier. Nested name counts retain inner packages; slash portion counts preserve package totals and net/drained names use net weight. With contradictory catalog/name evidence and no price ratio, SIZE_MISMATCH reports both sizes and retains the plausible catalog package total requiring approval. A name from either language can replace it when the live ratio confirms that name within 3%. Null when conversion is unknown.")

    size_unit: Literal["L", "KG", "SALES_UNIT"]
    size_source: str | None = Field(default=None, description="price_ratio, catalog, name_parse, base_unit or sales_unit.")
    size_confirmed_by_price: bool = Field(default=False, description="True when the live price ratio agrees with the selected exact catalog or name size within 3%.")
    name_size_candidates: list[NameSizeCandidate] = Field(default_factory=list, description="All parsed name candidates with original source text. A name conversion requires a confirming price ratio, matching explicit catalog content, one count expression matching the sales multiplier without contradiction, or one measure at multiplier 1. Otherwise quantities are display estimates and SIZE_UNCONFIRMED requires approval.")
    units_needed: float | None = Field(default=None, description="Whole sales units rounded to the effective allowed lot step, including KG.")
    unit_price: float | None = Field(default=None, description="Net catalog price per sales unit, fixed on quote creation.")
    unit_price_with_tax: float | None = None
    price_per_l_or_kg: float | None = None
    total: float | None = Field(default=None, description="Net line total in currency.")
    purchase_total: float | None = Field(default=None, description="Net cost of all sales units needed to cover the request, including overbuy; unit_price is per sales unit.")
    overbuy: "Overbuy | None" = None
    currency: str | None = Field(default=None, description="Upstream currency; missing currency is a PRICE_ERROR.")
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
    score_breakdown: dict[str, float] = Field(default_factory=dict, description="Weighted price, coverage, reliability, co2 and fit contributions; sums to score.")
    rank_label: Literal["best", "better", "good"] | None = None

    @model_serializer(mode="wrap")
    def preserve_signed_field_presence(self, handler):
        data = handler(self)
        for field in ("allowed_lot_size", "minimum_order_quantity"):
            if field not in self.model_fields_set:
                data.pop(field, None)
        if "name_size_candidates" not in self.model_fields_set:
            data.pop("name_size_candidates", None)
        for field in ("supplier_id", "synthetic"):
            if field not in self.model_fields_set:
                data.pop(field, None)
        return data


class Overbuy(Model):
    amount: float = Field(ge=0)
    unit: Literal["L", "KG", "SALES_UNIT"]


class CoveredQuantity(Model):
    value: float = Field(ge=0)
    unit: Literal["L", "KG", "SALES_UNIT"]


class PlanLine(Model):
    product_id: str
    units: float = Field(gt=0, description="Whole sales quantity in allowed_lot_size steps, including KG; limited by on-time stock when known.")
    covers: CoveredQuantity


class RuleHit(Model):
    rule: str
    detail: str


class PolicyResult(Model):
    outcome: Literal["CONFIRMED", "PENDING_APPROVAL", "REJECTED"]
    hits: list[RuleHit]


class Shortage(Model):
    requested: CoveredQuantity
    best_single_coverage: CoveredQuantity
    reason: str = Field(description="Plain-language explanation of why this plan combines or partially fulfils the need.")


class FulfilmentPlan(Model):
    kind: Literal["single", "split", "partial"]
    lines: list[PlanLine] = Field(min_length=1, max_length=10)
    total: float = Field(ge=0, description="Sum of fixed net EUR line totals at lookup time.")
    coverage: CoveredQuantity = Field(description="Sum of line covers in the need's unit; whole packs may over-cover.")
    uncovered: CoveredQuantity = Field(description="Requested quantity still missing by the deadline; zero for a complete plan.")
    shortage: Shortage | None = Field(default=None, exclude_if=lambda v: v is None,
                                     description="Requested and best eligible single-product on-time quantity; present on split and partial plans.")
    warnings: list[Warning] = Field(default_factory=list)
    policy_preview: PolicyResult = Field(description="Side-effect-free current policy evaluation for the caller, including mandate and daily limits. Stock and policy may change before placement; the order result is authoritative.")


class Retrieval(Model):
    mode: Literal["terms", "intent", "intent_broadened"] = "intent"
    terms_used: list[str] = Field(default_factory=list, description="Search words or phrases actually tried; broadened terms are lower-case and omit numbers, units and stop words.")


class LookupResult(Model):
    supplier_id: str = "valio"
    environment: Literal["live", "sandbox"] = "live"
    synthetic: bool = False
    basket: dict | None = Field(default=None, exclude_if=lambda v: v is None,
                                description="Ingredient basket and per-line request bindings; absent on historical single needs.")
    links: dict = Field(default_factory=dict, exclude_if=lambda v: not v)
    context: dict | None = Field(default=None, exclude_if=lambda v: v is None)
    correlation_id: str | None = Field(default=None, exclude_if=lambda v: v is None)
    mandate: MandateRecord | None = Field(default=None, exclude_if=lambda v: v is None)
    agent_id: str | None = Field(default=None, description="Creating agent; legacy unowned records are operator-only.")
    lookup_id: str = Field(description="Server-side need reference; pass as need_ref when quoting to preserve fit and constraints.")
    need: NeedSpec
    effective_constraints: "EffectiveConstraints" = Field(default_factory=lambda: EffectiveConstraints())
    exclusion_counts: dict[str, int] = Field(default_factory=dict, description="Removal counts by constraint source and evidence reason; counts may overlap.")
    offers: list[OfferCard]
    plans: list[FulfilmentPlan] = Field(default_factory=list, description="Single, split or partial plans from all ranked priced offers, before the returned offer slice; no extra upstream calls.")
    no_match_reason: str | None = Field(default=None, description="Explains empty catalog results, including how many candidates each hard constraint removed.")
    excluded_count: int = Field(description="FTS matches removed by hard catalog filters, before shortlist truncation.")
    retrieval: Retrieval = Field(default_factory=Retrieval, description="Deterministic retrieval mode and terms; older stored lookups default to intent with no recorded terms.")
    matched_count: int = Field(default=0, ge=0, description="All search matches passing hard constraints, before catalog-pool truncation.")
    priced_count: int = Field(default=0, ge=0, description="Shortlisted products checked for live prices, including cached evidence.")
    screened_count: int = Field(default=0, ge=0, description="Candidates actually submitted to Decisions, including failed requests or missing answers; zero when screening is skipped.")
    live_calls: LiveCalls
    decision_calls: int
    warnings: list[Warning] = Field(default_factory=list)


class IngredientQuantity(Model):
    value: float = Field(gt=0, le=1_000_000, strict=True)
    unit: Literal["L", "KG", "G", "ML", "SALES_UNIT"]

    @field_validator("unit", mode="before")
    @classmethod
    def normalize_unit(cls, value):
        return value.strip().upper() if isinstance(value, str) else value


class Ingredient(Model):
    name: InputText = Field(min_length=1)
    quantity: IngredientQuantity
    search_terms: InputTexts | None = None
    required_claims: InputTexts = Field(default_factory=list)
    exclude_allergens: InputTexts = Field(default_factory=list)
    substitutions_allowed: bool = False
    density_kg_per_l: float | None = Field(default=None, gt=0, strict=True)


class IngredientManifest(Model):
    version: Literal[1] = 1
    recipe_name: InputText = Field(min_length=1)
    servings: float = Field(gt=0, le=1_000_000, strict=True, description="Servings represented by the quantities unless base_servings is supplied; then the target servings.")
    base_servings: float | None = Field(default=None, gt=0, le=1_000_000, strict=True, exclude_if=lambda v: v is None,
                                      description="Optional explicit base for per-serving manifests; quantities scale by target/base_servings.")
    ingredients: list[Ingredient] = Field(min_length=1, max_length=50)
    yield_note: InputText | None = None


class BasketLookupRequest(RequestScope):
    manifest: IngredientManifest
    servings: float | None = Field(default=None, gt=0, le=1_000_000, strict=True,
                                 description="Target servings; omission preserves manifest.servings.")


class IngredientCoverage(Model):
    ingredient_index: int
    name: str
    requested: IngredientQuantity
    covered: float = Field(ge=0, le=1, description="Fraction of this scaled ingredient covered on time.")
    product_id: str | None = None
    coverage: float | None = Field(default=None, ge=0, le=1, exclude_if=lambda v: v is None)
    quantity: IngredientQuantity | None = Field(default=None, exclude_if=lambda v: v is None)
    purchasable_quantity: float | None = Field(default=None, exclude_if=lambda v: v is None)
    overbuy: Overbuy | None = None


class UncoveredIngredient(Model):
    ingredient_index: int
    name: str
    reason: str


class IngredientOffers(Model):
    ingredient_index: int
    name: str
    offers: list[OfferCard]
    chosen_line: str | None = Field(default=None, description="Product ID of the shared basket line.")
    purchasable_quantity: float | None = None
    overbuy: Overbuy | None = None
    coverage: float = Field(default=0, ge=0, le=1)
    exclusion_counts: dict[str, int] = Field(default_factory=dict)


class BasketPlan(Model):
    lines: list[OfferCard]
    total: float
    coverage_by_ingredient: list[IngredientCoverage]
    uncovered: list[UncoveredIngredient]
    policy_preview: PolicyResult


class BasketLookupResult(Model):
    context: dict | None = Field(default=None, exclude_if=lambda v: v is None)
    basket_lookup_id: str
    agent_id: str
    customer_id: str
    deliver_by: date
    scaled_manifest: IngredientManifest
    ingredients: list[IngredientOffers]
    basket: BasketPlan
    line_needs: dict[str, NeedSpec] = Field(description="Bound per-SKU ingredient constraints, rechecked at quote, order and approval.")
    live_calls: LiveCalls
    decision_calls: int


class QuoteLineRequest(Model):
    product_id: InputText = Field(min_length=1)
    quantity_sales_units: float = Field(gt=0, le=1_000_000, strict=True, description="Explicit whole sales quantity, at most 1,000,000, in allowedLotSize steps (minimum one), including KG; fractional lots return INVALID_LOT_SIZE.")


class PlanRef(Model):
    lookup_id: InputText = Field(min_length=1)
    plan_index: int = Field(ge=0, description="Zero-based index in the stored lookup's plans.")


class BasketRef(Model):
    basket_lookup_id: InputText = Field(min_length=1)


class QuoteRequest(RequestScope):
    intent_id: str | None = Field(default=None, exclude_if=lambda v: v is None)
    intent_version: int | None = Field(default=None, ge=1, exclude_if=lambda v: v is None)
    basket_ref: BasketRef | None = Field(default=None, exclude_if=lambda v: v is None, description="Quote the selected, deduplicated lines of a stored recipe basket; mutually exclusive with lines and plan_ref.")
    lines: list[QuoteLineRequest] | None = Field(default=None, min_length=1, max_length=10)
    plan_ref: PlanRef | None = Field(default=None, description="Recheck a stored plan live. Exactly one of lines, plan_ref or basket_ref is required.")
    need_ref: InputText | None = Field(default=None, description="Required with explicit lines: stored lookup_id binding fit, preferences and current customer constraints. Otherwise 422 NEED_REF_REQUIRED.")

    @model_validator(mode="after")
    def unique_lines(self):
        if (self.intent_id is None) != (self.intent_version is None):
            raise ValueError("intent_id and intent_version must be supplied together")
        if sum(x is not None for x in (self.lines, self.plan_ref, self.basket_ref)) != 1:
            raise ValueError("Exactly one of lines, plan_ref or basket_ref is required")
        if self.basket_ref:
            if self.need_ref is not None and self.need_ref != self.basket_ref.basket_lookup_id:
                raise ValueError("need_ref must match basket_ref.basket_lookup_id")
            self.need_ref = self.basket_ref.basket_lookup_id
        if self.plan_ref:
            if self.need_ref is None:
                self.need_ref = self.plan_ref.lookup_id
            elif self.need_ref != self.plan_ref.lookup_id:
                raise ValueError("need_ref must match plan_ref.lookup_id")
        if self.lines and len({x.product_id for x in self.lines}) != len(self.lines):
            raise ValueError("Duplicate products must be combined into one line")
        return self


class Quote(Model):
    intent_id: str | None = Field(default=None, exclude_if=lambda v: v is None)
    intent_version: int | None = Field(default=None, exclude_if=lambda v: v is None)
    anomalies: list[AnomalyCheck] = Field(default_factory=list, exclude_if=lambda v: not v, description="Spec 45 checks, including insufficient_history and one-step proposed fixes.")
    basket_ref: BasketRef | None = Field(default=None, exclude_if=lambda v: v is None)
    clinical_review: Literal["required"] | None = Field(default=None, exclude_if=lambda v: v is None)
    supplier_id: str = "valio"
    environment: Literal["live", "sandbox"] = "live"
    synthetic: bool = False
    expiration_reason: str | None = Field(default=None, exclude_if=lambda v: v is None)

    links: dict = Field(default_factory=dict, exclude_if=lambda v: not v)
    context: dict | None = Field(default=None, exclude_if=lambda v: v is None)
    correlation_id: str | None = Field(default=None, exclude_if=lambda v: v is None)
    mandate: MandateRecord | None = Field(default=None, exclude_if=lambda v: v is None)
    id: str
    signature: str | None = Field(default=None, description="Detached RFC 7515 EdDSA JWS over the spec 10/13 RFC 8785 payload: id, customer_id, deliver_by, lines (product_id, decimal-string units and unit_price, currency, gtin, provenance), decimal-string total, expires_at, price_commitment. Protected header: alg, kid, quote_version=2, quote_integrity (SHA-256 of canonical immutable quote fields, excluding signature/status/live_calls, numbers as decimal strings). Earlier unversioned spec and expanded-integrity signatures remain verifiable and usable until expiry. Public keys: /.well-known/jwks.json. Unsigned legacy quotes require re-quoting; existing orders remain replayable.")
    price_commitment: Literal["gateway-locked"] = Field(default="gateway-locked", description="Gateway holds the fixed price until expires_at; included in the signature.")
    agent_id: str | None = Field(default=None, description="Creating agent; legacy unowned records are operator-only.")
    customer_id: str
    deliver_by: date
    need_ref: str | None = None
    plan_ref: PlanRef | None = None
    warnings: list[Warning] = Field(default_factory=list, description="Plan-level risks, including an uncovered part of the original need; retained through ordering.")
    status: Literal["OPEN", "ACCEPTED", "EXPIRED"]
    created_at: datetime
    expires_at: datetime = Field(description="Earliest price validity, future cutoff or 30 minutes after creation.")
    lines: list[OfferCard] = Field(description="Fixed-price lines; units_needed is the quoted sales quantity.")
    total: float
    currency: str
    hard_constraint_violations: list[str] = Field(default_factory=list)
    live_calls: LiveCalls


class OrderRequest(Model):
    quote_id: InputText = Field(description="An unexpired OPEN fixed-price quote.")


class Execution(Model):
    mode: Literal["simulated", "supplier_backed"]
    supplier_ref: str | None = None
    acknowledged_at: datetime | None = None
    status: str


class Order(Model):
    intent_state: str | None = Field(default=None, exclude_if=lambda v: v is None)
    delivery_notice: str | None = Field(default=None, exclude_if=lambda v: v is None)
    intent_id: str | None = Field(default=None, exclude_if=lambda v: v is None)
    intent_version: int | None = Field(default=None, exclude_if=lambda v: v is None)
    anomalies: list[AnomalyCheck] = Field(default_factory=list, exclude_if=lambda v: not v, description="Anomaly evidence rechecked under the placement lock; warnings require human approval.")
    basket_ref: BasketRef | None = Field(default=None, exclude_if=lambda v: v is None)
    clinical_review: Literal["required"] | None = Field(default=None, exclude_if=lambda v: v is None)
    atomicity: str | None = Field(default=None, exclude_if=lambda v: v is None,
                                 description="Adapter commitment semantics; simulation reserves all basket lines or none locally.")
    supplier_id: str = "valio"
    environment: Literal["live", "sandbox"] = "live"
    synthetic: bool = False
    execution: Execution | None = Field(default=None, exclude_if=lambda v: v is None)
    expiration_reason: str | None = Field(default=None, exclude_if=lambda v: v is None)

    context: dict | None = Field(default=None, exclude_if=lambda v: v is None)
    correlation_id: str | None = Field(default=None, exclude_if=lambda v: v is None)
    links: dict = Field(default_factory=dict)
    mandate: MandateRecord | None = Field(default=None, exclude_if=lambda v: v is None)
    id: str
    customer_id: str | None = None
    commitment: Literal["gateway-simulated", "supplier-acknowledged"] = Field(default="gateway-simulated", description="Supplier acknowledgement only after a successful supplier-backed submit; otherwise gateway-simulated. Included in the signed receipt.")
    quote_signature_digest: str | None = Field(default=None, description="SHA-256 hex digest of the quote's detached JWS.")
    receipt: str | None = Field(default=None, description="Detached RFC 7515 Ed25519 JWS over RFC 8785 receipt_payload; total is a decimal string. Verifiable via the public JWKS. Present for new order decisions.")
    decided_at: datetime | None = None
    decided_by_kind: Literal["agent", "operator", "system"] | None = None
    quote_id: str
    agent_id: str
    idempotency_key: str
    status: Literal["CONFIRMED", "PENDING_APPROVAL", "PENDING_EXECUTION", "REJECTED", "EXPIRED", "CANCELLED"]
    created_at: datetime
    expires_at: datetime
    total: float
    currency: str
    lines: list[OfferCard]
    warnings: list[Warning] = Field(default_factory=list, description="Order-level risks retained from the quoted fulfilment plan.")
    evidence_revision: int = Field(default=1, ge=1, description="Decision evidence revision; starts at 1 and increases when approval refresh changes the facts requiring review.")
    order_revision: int = Field(default=1, ge=1, description="Durable order revision; increases on every status or decision-evidence change, including approval, rejection, expiry and cancellation. Polls and idempotent retries do not increase it.")
    policy: PolicyResult
    live_calls: LiveCalls
    approver: str | None = None
    approver_user_id: str | None = None
    approver_role: str | None = None
    approval_side: Literal["buyer", "valio_operator"] | None = Field(default=None, description="Organisation side that signed the human decision; null before review or on legacy orders.")
    comment: str | None = None
    cancelled_by: str | None = Field(default=None, description="Human name or placing agent ID responsible for cancellation.")
    cancel_reason: str | None = Field(default=None, description="Reason supplied when the order was cancelled.")

    @computed_field
    @property
    def next_action(self) -> Literal["WAIT_FOR_APPROVAL", "RETRY_SAME_COMMAND", "STOP", "LOOKUP_REQUIRED"] | None:
        if self.status == "PENDING_EXECUTION":
            return "RETRY_SAME_COMMAND"

        if self.status == "PENDING_APPROVAL":
            return "WAIT_FOR_APPROVAL"
        if self.status == "EXPIRED":
            return "LOOKUP_REQUIRED"
        return "STOP" if self.status in {"REJECTED", "CANCELLED"} else None

    @computed_field
    @property
    def approval_reason(self) -> str | None:
        """Summarise the recorded rule hits without inventing evidence of compliance."""
        details = list(dict.fromkeys(
            re.sub(r"[.!?]\s+", "; ", " ".join(hit.detail.split()).rstrip(".!?")).replace(" < ", " is below ")
            for hit in self.policy.hits if hit.detail.strip()))
        return "; ".join(details) + "." if details else None

    @computed_field
    @property
    def poll_after_seconds(self) -> int | None:
        return 5 if self.status == "PENDING_APPROVAL" else None

    @computed_field
    @property
    def retry_after_seconds(self) -> int | None:
        return 1 if self.status == "PENDING_EXECUTION" else None

    @model_validator(mode="before")
    @classmethod
    def stored_guidance(cls, value):
        if isinstance(value, dict):
            value = {k: v for k, v in value.items() if k not in {"next_action", "poll_after_seconds", "approval_reason", "retry_after_seconds"}}
        return value


class CancelRequest(Model):
    confirmation_token: str | None = Field(default=None, repr=False, exclude=True)
    cancelled_by: InputText = Field(min_length=1, description="Non-blank human name responsible for cancellation.")
    reason: InputText = ""

    @model_validator(mode="after")
    def named_human(self):
        self.cancelled_by = self.cancelled_by.strip()
        if not self.cancelled_by:
            raise ValueError("cancelled_by must contain a human name")
        return self


class ApprovalRequest(Model):
    approver: InputText = Field(min_length=1, description="Display name stated by the approver; authenticated user id and role are recorded separately.")
    comment: InputText = ""
    confirmation_token: str | None = Field(default=None, repr=False, exclude=True, description="Single-use REST confirmation returned by the first decision call; expires in five minutes.")
    evidence_revision: int | None = Field(default=None, ge=1, description="Optional revision displayed to the approver. A stale revision prevents approval with 409 EVIDENCE_CHANGED before any upstream call; ignored for rejection.")


class ApprovalItem(Model):
    order: Order
    quote: Quote


class AuditEntry(Model):
    tenant_id: str | None = None
    id: int
    prev_hash: str = ""
    hash: str = ""
    ts: str = Field(description="Exact stored timestamp string used in the audit hash; preserve its UTC offset spelling for independent verification.")
    actor: str
    action: str
    order_id: str | None
    quote_id: str | None
    summary: str
    data: dict[str, Any]


class TimelineStep(Model):
    hash: str = Field(default="", description="SHA-256 hash of this step's audit entry in the gateway chain.")
    ts: datetime = Field(description="UTC audit event time; steps are oldest first, with audit ID breaking ties.")
    actor: str = Field(description="Recorded actor, such as agent:buyer, human:Ada or system.")
    action: str = Field(description="Original audit action, including EVIDENCE_CHANGED, replays and failed attempts.")
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


class PublicJWK(Model):
    kid: str
    kty: Literal["OKP"]
    crv: Literal["Ed25519"]
    alg: Literal["EdDSA"]
    use: Literal["sig"]
    x: str


class JWKSet(Model):
    keys: list[PublicJWK]


class AuditHead(Model):
    environment: Literal["live", "sandbox"] = "live"
    entries: int
    head_hash: str
    at: str
    jws: str = Field(description="Detached Ed25519 JWS over canonical {entries, head_hash, at}; keep outside the gateway as a checkpoint.")


class AuditVerification(Model):
    valid: bool
    entries: int
    head_hash: str
    first_break: int | None = None
    checkpoint_match: bool | None = None
    anchor_match: bool | None = Field(default=None, exclude_if=lambda v: v is None)
    anchor_block_time: datetime | None = Field(default=None, exclude_if=lambda v: v is None)


class AuditAnchor(Model):
    entries: int
    head_hash: str
    chain_id: int
    tx_hash: str | None = None
    block_number: int | None = None
    status: Literal["sent", "confirmed", "failed"]
    explorer_url: str | None = None
    at: datetime


class RevocationRequest(Model):
    jwt: str = Field(min_length=1, max_length=32768, description="Buyer-signed EdDSA JWT containing iss, jti_revoked and iat.")


class ErrorResponse(Model):
    type: str
    title: str
    status: int
    detail: str
    instance: str
    recovery_version: Literal[1] = 1
    retryable: bool
    next_action: Literal["RETRY_SAME_COMMAND", "REFRESH_QUOTE", "LOOKUP_REQUIRED", "WAIT_FOR_APPROVAL",
                         "REVIEW_UPDATED_EVIDENCE", "FIX_REQUEST", "CONTACT_ADMIN", "STOP"]
    retry_after_seconds: int | None = None
    resource_ref: str | None = Field(default=None, exclude_if=lambda v: v is None)
    correlation_id: str
    violations: list[dict] = Field(default_factory=list)
    code: str = Field(description="Stable domain error code, e.g. EXPIRED_QUOTE.")
    message: str
    data: dict | None = Field(default=None, description="Additional error evidence, including approval rule_hits when available.")


class UpstreamHealth(Model):
    blocked_until: datetime | None = Field(default=None, description="UTC end of the process-wide Valio 429 block, or null when unblocked.")
    calls_last_minute: int = Field(default=0, ge=0, description="Actual outbound Valio calls in the preceding 60 seconds, across this process.")


class Health(Model):
    status: str
    catalog_rows: int
    coverage: dict[str, dict[str, int]] = Field(default_factory=dict, description="Catalog build coverage: filled and empty counts per field.")
    anchoring: dict | None = Field(default=None, exclude_if=lambda v: v is None)
    upstream: UpstreamHealth = Field(default_factory=UpstreamHealth)


class ApprovalPolicy(Model):
    max_auto_daily_total_eur: float = Field(default=5000, ge=0, description="Default rolling 24-hour autonomous EUR ceiling for newly connected agents.")
    max_auto_total_eur: float = Field(ge=0, description="Net EUR ceiling for automatic confirmation.")
    min_fit_probability: float = Field(ge=0, le=1, description="Minimum fit probability for automatic confirmation.")
    warnings_need_approval: list[str] = Field(description="Known warning codes requiring human review.")
    ignored_warnings: list[str] = Field(default_factory=list, description="Only these warnings may be ignored; all other non-reject warnings require approval.")


class RejectPolicy(Model):
    on_hard_constraint_violation: bool = Field(description="Reject request hard-constraint failures; false routes them to approval. Customer standing constraints always reject.")
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


class BucketPolicy(Model):
    capacity: int = Field(gt=0)
    refill_per_minute: float = Field(gt=0)


class RateLimits(Model):
    lookup: BucketPolicy = Field(default_factory=lambda: BucketPolicy(capacity=60, refill_per_minute=60))
    mutation: BucketPolicy = Field(default_factory=lambda: BucketPolicy(capacity=120, refill_per_minute=120))
    read: BucketPolicy = Field(default_factory=lambda: BucketPolicy(capacity=600, refill_per_minute=600))


class Organisation(Model):
    customer_ids: InputTexts = Field(default_factory=list)


class IdentityPolicy(Model):
    kind: Literal["agent", "operator", "buyer_user", "supplier_agent"] = "agent"
    organisation_id: InputText | None = None
    role: Literal["agent", "valio_operator", "buyer_admin", "buyer_approver", "buyer_viewer", "supplier_agent"] = "agent"


class AgentMandate(Model):
    recurring: bool = False
    supplier_ids: InputTexts | None = Field(default=None, description="Allowed suppliers, intersected with buyer VC supplier_ids; omission is unrestricted.")

    autonomy: AutonomyLevel | None = Field(default=None, description="Omission uses policy.autonomy.default; new agents require sign-off by default.")
    rate_limits: RateLimits | None = None
    customer_ids: InputTexts = Field(min_length=1, description="Customers this agent may quote and order for.")
    max_auto_total_eur: float | None = Field(default=None, ge=0, description="Optional per-order ceiling; the lower of this and the global ceiling applies.")
    max_auto_daily_total_eur: float = Field(ge=0, description="Rolling 24-hour autonomous EUR ceiling; cancelled and human-approved orders are excluded.")
    note: InputText = Field(default="", description="Free-text record of who granted the mandate.")

    @model_validator(mode="after")
    def named_customers(self):
        if any(not customer.strip() for customer in self.customer_ids):
            raise ValueError("customer_ids must contain non-blank customer identifiers")
        return self


ClientKind = Literal["claude_desktop", "claude_code", "cursor", "vscode", "chatgpt", "mcp_other", "rest", "curl", "python", "typescript", "openai_agents_sdk", "anthropic_api", "langchain", "a2a"]


class ConnectionRequest(Model):
    agent_id: InputText = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,47}$")
    client: ClientKind = "rest"
    customer_ids: InputTexts = Field(min_length=1)
    label: InputText | None = None
    type: Literal["mcp", "rest", "a2a", "webhook", "tool_pack"] = "mcp"
    audience: Literal["buyer", "tenant", "operator"] = "buyer"
    scopes: list[str] = Field(default_factory=lambda: ["*"])
    expires_at: AwareDatetime | None = None
    tenant_id: str | None = None


class Connection(Model):
    connection_id: str
    agent_id: str
    client: ClientKind
    key_prefix: str
    created_at: datetime
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None
    credential_kind: Literal["api_key", "oauth"] = Field(
        default="api_key", exclude_if=lambda value: value == "api_key",
        description="OAuth grants have no copyable key. Omitted for legacy API-key connections.")
    tenant_id: str | None = None
    created_by: str | None = None
    expires_at: datetime | None = None
    type: str = "mcp"
    audience: str = "buyer"
    scopes: list[str] = Field(default_factory=lambda: ["*"])
    customer_ids: list[str] = Field(default_factory=list)


class NewConnection(Connection):
    key: str | None = Field(repr=False, description="Secret returned only on initial creation; null on an idempotent replay.")
    mcp_url: str | None = Field(description="Public origin plus /mcp when served. Without GATEWAY_PUBLIC_URL, only loopback request origins are advertised; otherwise null (use REST).")
    rest_base_url: str
    mandate: AgentMandate


class BuyerJWK(Model):
    kty: Literal["OKP"] = "OKP"
    crv: Literal["Ed25519"] = "Ed25519"
    x: str
    kid: str
    alg: Literal["EdDSA"] = "EdDSA"
    use: Literal["sig"] = "sig"


class SubstitutionProfile(Model):
    allowed_brands: InputTexts | None = None
    max_price_increase_pct: float = Field(ge=0, strict=True)
    pack_size_range: PackSizePreference
    requires_approval: bool


class CustomerConstraints(Model):
    substitution_profile: SubstitutionProfile | None = None
    clinical_review: Literal["required"] | None = None
    mandate_issuers: dict[InputText, list[BuyerJWK]] = Field(default_factory=dict, description="Trusted buyer issuer IDs mapped to public Ed25519 JWKs; operator onboarding only.")
    required_claims: InputTexts = Field(default_factory=list)
    exclude_allergens: InputTexts = Field(default_factory=list)
    note: InputText = ""


class ConstraintSource(Model):
    code: str
    source: Literal["request", "customer_policy"]


class EffectiveConstraints(Model):
    required_claims: list[ConstraintSource] = Field(default_factory=list)
    exclude_allergens: list[ConstraintSource] = Field(default_factory=list)


class AutonomyPromotion(Model):
    min_approved_unchanged: int = Field(default=5, ge=0, strict=True)
    max_rejection_share: float = Field(default=0.1, ge=0, le=1)
    days_since_last_rejection_or_refusal: int = Field(default=7, ge=0, strict=True)


class AutonomyPolicy(Model):
    default: AutonomyLevel = "approval_required"
    promotion: AutonomyPromotion = Field(default_factory=AutonomyPromotion)


class AutonomyRecommendation(Model):
    eligible: bool
    reasons: list[str]


class AgentTrackRecord(Model):
    agent_id: str
    window_days: int
    window_start: datetime
    window_end: datetime
    orders_placed: int
    approved_unchanged: int
    rejected: int
    expired: int
    evidence_changes: int
    cancellations: int
    hard_constraint_refusals: int
    average_approval_time_seconds: float | None
    approved_unchanged_share: float
    rejection_share: float
    last_rejection_at: datetime | None
    last_hard_constraint_refusal_at: datetime | None
    autonomy_recommendation: AutonomyRecommendation


class AutonomyChange(Model):
    confirmation_token: str | None = Field(default=None, repr=False, exclude=True)
    level: AutonomyLevel
    reason: InputText = Field(min_length=1)

    @field_validator("reason")
    @classmethod
    def nonblank_reason(cls, value):
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value.strip()


class AutonomyChangeResult(Model):
    agent_id: str
    level: AutonomyLevel
    reason: str
    track_record: AgentTrackRecord


PROTECTED_WARNINGS = {"PRICE_ERROR", "AVAILABILITY_ERROR", "SIZE_UNKNOWN", "SIZE_UNCONFIRMED",
                      "UNIT_MISMATCH", "STOCK_MODEL_UNKNOWN", "NO_FIT_SCORE", "CUTOFF_UNKNOWN",
                      "CLINICAL_REVIEW", "SUBSTITUTION_PROFILE", "PARTIAL_BASKET",
                      "QUANTITY_ANOMALY", "PRICE_ANOMALY", "VELOCITY_ANOMALY", "NEW_CATEGORY"}


class ProposalQuotas(Model):
    per_supplier_per_week: int = Field(default=100, ge=0)
    per_customer_per_week: int = Field(default=10, ge=0)


class AnomalyPolicy(Model):
    sensitivity: Literal["strict", "normal", "relaxed"] = "normal"
    sensitivity_k: dict[str, float] = Field(default_factory=lambda: {"strict": 2, "normal": 3, "relaxed": 5})
    history_days: int = Field(default=90, ge=1, le=3650)
    price_increase_pct: float = Field(default=20, ge=0)
    velocity_min_upper: float = Field(default=5, ge=1)

    @model_validator(mode="after")
    def valid_sensitivity(self):
        import math
        if self.sensitivity not in self.sensitivity_k or any(not math.isfinite(k) or k <= 0 for k in self.sensitivity_k.values()):
            raise ValueError("Sensitivity requires a finite positive k")
        return self


class PolicyDocument(Model):
    anomalies: AnomalyPolicy = Field(default_factory=AnomalyPolicy)
    proposal_quotas: ProposalQuotas = Field(default_factory=ProposalQuotas)
    autonomy: AutonomyPolicy = Field(default_factory=AutonomyPolicy)
    organisations: dict[str, Organisation] = Field(default_factory=dict)
    identities: dict[str, IdentityPolicy] = Field(default_factory=dict)
    rate_limits: RateLimits = Field(default_factory=RateLimits)
    approval: ApprovalPolicy
    reject: RejectPolicy
    ranking_weights: RankingWeights
    shortlist: ShortlistPolicy = Field(default_factory=lambda: ShortlistPolicy(catalog_pool=40))
    agents: dict[str, AgentMandate] = Field(default_factory=dict, description="Agent identity to mandate; ordering requires a mandate. Lookups and quotes are open to unregistered agents.")
    customers: dict[str, CustomerConstraints] = Field(default_factory=dict, description="Standing buyer constraints unioned with each request at every step.")

    @model_validator(mode="after")
    def known_codes(self):
        from .lookup import WARNING_TEXT

        unknown = set(self.approval.warnings_need_approval + self.approval.ignored_warnings + self.reject.on) - WARNING_TEXT.keys()
        if unknown:
            raise ValueError(f"Unknown warning codes: {', '.join(sorted(unknown))}")
        if any(not agent.strip() for agent in self.agents):
            raise ValueError("agents must have non-blank identifiers")
        if any(not customer.strip() for customer in self.customers):
            raise ValueError("customers must have non-blank identifiers")
        protected = PROTECTED_WARNINGS.intersection(self.approval.ignored_warnings)
        if protected:
            raise ValueError(f"Protected warnings cannot be ignored: {', '.join(sorted(protected))}")
        return self


class PolicyResource(PolicyDocument):
    _tenant_daily_ceiling: float | None = PrivateAttr(default=None)
    updated_at: datetime | None = Field(default=None, description="Last runtime update in UTC; null for committed defaults.")
    updated_by: str | None = Field(default=None, description="Human responsible for the last runtime update.")
    known_warning_codes: list[str] = Field(default_factory=list, description="All warning codes the lookup engine can emit; server supplied.")


class PolicyResetRequest(Model):
    confirmation_token: str | None = Field(default=None, repr=False, exclude=True)
    updated_by: InputText = Field(min_length=1, description="Name of the human changing the policy.")

    @model_validator(mode="after")
    def named_human(self):
        self.updated_by = self.updated_by.strip()
        if not self.updated_by:
            raise ValueError("updated_by must contain a human name")
        return self


class PolicyUpdateRequest(PolicyResetRequest):
    policy: PolicyResource = Field(description="Editable policy sections. Response metadata may be echoed; the server replaces it.")
    comment: InputText | None = Field(default=None, description="Optional reason for the policy change.")


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
    registered: bool = Field(description="Whether the agent currently has a policy mandate or a valid, trusted, unrevoked buyer credential.")
    mandate_id: str | None = Field(default=None, exclude_if=lambda v: v is None, description="Currently valid buyer mandate in use; historical IDs remain on objects and audit entries.")


class Stats(Model):
    throttles: int = 0
    upstream_rate_limits: int = Field(default=0, description="Valio HTTP 429 blocks started in the reporting window; one audit event per block.")
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
