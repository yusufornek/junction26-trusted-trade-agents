/**
 * Fixtures for `VITE_MOCK=1` — design review without a gateway or an operator token.
 * Products, prices and warnings are taken from real gateway responses seen on 13–14.11; every order
 * status and every warning family appears at least once so each visual state can be inspected.
 * Never imported by production builds (see MOCK in api.ts).
 */
import type {
  Anomaly,
  AgentTrackRecord,
  AuditEntry,
  Connection,
  FulfilmentPlan,
  LookupResult,
  OfferCard,
  Order,
  OrderStatus,
  PolicyResource,
  Quote,
  QuoteLine,
  Stats,
  TimelineStep,
  Warning,
} from "./api";
import { mockRepeat } from "./repeat";
import { mockBasket } from "./basket";
import { mockGovernance } from "./governance";
import { mockChanges } from "./changes";
import { mockTeam } from "./team";
import { mockWatch } from "./watch";

const NOW = Date.now();
const ago = (min: number) => new Date(NOW - min * 60000).toISOString();
const ahead = (min: number) => new Date(NOW + min * 60000).toISOString();
const dayAhead = (d: number) => new Date(NOW + d * 86400000).toISOString().slice(0, 10);
const DELIVER_BY = dayAhead(2);

const W = {
  CUTOFF_PASSED: { code: "CUTOFF_PASSED", text: "Upstream closingTime is before timeNow; the cutoff meaning needs human review." },
  PARTIAL_STOCK: { code: "PARTIAL_STOCK", text: "Only part of the requested quantity is available by the delivery date." },
  DATE_SHIFT: { code: "DATE_SHIFT", text: "The earliest available date is after the requested delivery date." },
  OUTSIDE_PREFERENCE: { code: "OUTSIDE_PREFERENCE", text: "brand Arla, preferred Valio; 10 L pack, preferred <= 1 L" },
  NOT_AVAILABLE: { code: "NOT_AVAILABLE", text: "Valio reports this product as not available" },
} satisfies Record<string, Warning>;

type Row = [
  id: string, name: string, unit: string, size: number, units: number, price: number,
  model: "Stock" | "Terminal", available: number, reliability: number | null, co2: number, warnings: Warning[],
  rank: "best" | "better" | "good" | null, fit: number,
];

const ROWS: Row[] = [
  ["464308", "Valio Hyvä suomalainen Arki® Eila® semi skimmed milk drink 1 l lactose free", "ST", 1, 1000, 1.01, "Stock", 4200, 0.98, 1.1, [], "best", 0.94],
  ["464407", "Valio Hyvä suomalainen Arki® Eila® skimmed milk drink 1 l lactose free", "ST", 1, 1000, 1.1, "Stock", 1800, 0.97, 1.1, [], "better", 0.91],
  ["465036", "Valio Eila® semi skimmed milk drink 1 l lactose free", "KI", 5, 200, 6.86, "Terminal", 999999, null, 1.1, [W.CUTOFF_PASSED], "good", 0.9],
  ["465202", "Valio Eila® 3 % milk drink 1 l lactose free", "ST", 1, 1000, 1.4, "Stock", 2600, 0.95, 1.2, [], null, 0.86],
  ["463038", "Valio Eila® semi skimmed milk drink 10 l novobox lactose free", "KI", 10, 100, 15.97, "Terminal", 999999, null, 1.1, [W.CUTOFF_PASSED], null, 0.82],
  ["469330", "5 x Arla 1 L lactose free semi-skimmed milk drink Suomesta ESL", "KI", 5, 200, 8.54, "Terminal", 999999, null, 1.1, [W.CUTOFF_PASSED, W.OUTSIDE_PREFERENCE], null, 0.9],
  ["469386", "5 x Arla Lactose- and fatfree milkdrink 1 L Suomi ESL", "KI", 5, 200, 8.8, "Stock", 120, 0.91, 1.1, [W.PARTIAL_STOCK, W.OUTSIDE_PREFERENCE], null, 0.88],
  ["464768", "12 x Arla Whole milk drink 1 L UHT lactose free", "KI", 12, 84, 30.42, "Stock", 300, 0.89, 1.3, [W.OUTSIDE_PREFERENCE], null, 0.8],
  ["466279", "Arla 10 L Novobox Suomi semi skimmed lactose free milk drink ESL", "ST", 10, 100, 27.22, "Stock", 400, 0.86, 1.1, [W.DATE_SHIFT, W.OUTSIDE_PREFERENCE], null, 0.84],
  ["462775", "Arla Milkdrink 1L lactose free fat free UHT Suomesta", "ST", 1, 1000, 2.57, "Stock", 51, 0.9, 1.1, [W.PARTIAL_STOCK, W.OUTSIDE_PREFERENCE], null, 0.83],
  // Empty availability from Valio: listed, but the policy rejects it, so the console offers no Add to quote.
  ["466104", "Valio Eila® semi skimmed milk drink 1,5 l lactose free", "ST", 1.5, 667, 1.62, "Stock", 0, null, 1.1, [W.NOT_AVAILABLE, W.PARTIAL_STOCK], null, 0.78],
];

const NEED = 1000;
const offer = ([id, name, unit, size, units, price, model, available, reliability, co2, warnings, rank, fit]: Row): OfferCard => {
  const coverage = model === "Terminal" ? 1 : Math.min(1, available / units);
  const shifted = warnings.some((w) => w.code === "DATE_SHIFT");
  const cutoff = warnings.some((w) => w.code === "CUTOFF_PASSED");
  return {
    product_id: id,
    gtin: "64" + id.padStart(11, "0"),
    name,
    sales_unit: unit,
    allowed_lot_size: 1,
    size_per_sales_unit: size,
    size_unit: "L",
    size_source: size === 1 ? "name_parse" : "catalog",
    size_confirmed_by_price: true,
    units_needed: units,
    unit_price: price,
    unit_price_with_tax: Math.round(price * 1.135 * 100) / 100,
    price_per_l_or_kg: Math.round((price / size) * 100) / 100,
    total: Math.round(price * units * 100) / 100,
    purchase_total: Math.round(price * units * 100) / 100,
    overbuy: size * units > NEED ? { amount: size * units - NEED, unit: "L" } : null,
    currency: "EUR",
    valid_from: "2026-09-01T00:00:00+03:00",
    valid_to: "9999-12-31T00:00:00+02:00",
    net_price_found: false,
    availability: {
      available_amount: available,
      logistic_model: model,
      closing_time: cutoff ? ago(60 * 20) : ahead(60 * 30),
      available_date: shifted ? dayAhead(4) : DELIVER_BY,
      enough: coverage >= 1 && !shifted,
      coverage,
    },
    warnings,
    delivery_reliability: reliability,
    co2_kg_per_kg: co2,
    labels: name.startsWith("Valio") ? ["GOODS_FROM_FINLAND_BLUE_SWAN", "coolTemperature"] : ["coolTemperature"],
    matched_because: [
      "FREE_FROM_LACTOSE from catalog nutritionalClaim (request)",
      "AE not listed in the catalog allergen declaration (request)",
      reliability == null ? "reliability: no data (neutral)" : `reliability ${Math.round(reliability * 100)}% from delivery history`,
    ],
    fit,
    fit_question: `Does product ${id} (${name}) satisfy the buyer need? Judge intended use and suitability using only the supplied catalog facts.`,
    provenance: {
      price: { source: "valio:/api/price", fetched_at: ago(3), valid_to: "9999-12-31T00:00:00+02:00" },
      availability: { source: "valio:/api/availability", fetched_at: ago(3), valid_to: cutoff ? ago(60 * 20) : ahead(60 * 30) },
    },
    score: 0,
    score_breakdown: {},
    rank_label: rank,
  };
};

const OFFERS = ROWS.map(offer).map((o, i, all) => {
  const totals = all.map((x) => x.total ?? 0);
  const [lo, hi] = [Math.min(...totals), Math.max(...totals)];
  const price = 1 - ((o.total ?? 0) - lo) / (hi - lo || 1);
  const cov = o.availability?.coverage ?? 0;
  const rel = o.delivery_reliability ?? 0;
  const co2 = 1 - ((o.co2_kg_per_kg ?? 1.3) - 1.1) / 0.2;
  const breakdown = { price: 0.4 * price, coverage: 0.25 * cov, reliability: 0.15 * rel, co2: 0.1 * co2, fit: 0.1 * (o.fit ?? 0) };
  return {
    ...o,
    score_breakdown: breakdown,
    score: Object.values(breakdown).reduce((a, b) => a + b, 0) - i * 0.001,
  };
});
const byId = (id: string) => OFFERS.find((o) => o.product_id === id)!;

const PLANS: FulfilmentPlan[] = [
  {
    kind: "single",
    lines: [{ product_id: "464308", units: 1000, covers: { value: 1000, unit: "L" } }],
    total: 1010,
    coverage: { value: 1000, unit: "L" },
    uncovered: { value: 0, unit: "L" },
    warnings: [],
    policy_preview: { outcome: "CONFIRMED", hits: [] },
  },
  {
    kind: "split",
    lines: [
      { product_id: "469386", units: 120, covers: { value: 600, unit: "L" } },
      { product_id: "464407", units: 400, covers: { value: 400, unit: "L" } },
    ],
    total: 1496,
    coverage: { value: 1000, unit: "L" },
    uncovered: { value: 0, unit: "L" },
    warnings: [W.OUTSIDE_PREFERENCE],
    policy_preview: {
      outcome: "PENDING_APPROVAL",
      hits: [{ rule: "OUTSIDE_PREFERENCE", detail: "469386: brand Arla, preferred Valio" }],
    },
  },
  {
    kind: "partial",
    lines: [{ product_id: "462775", units: 51, covers: { value: 51, unit: "L" } }],
    total: 131.07,
    coverage: { value: 51, unit: "L" },
    uncovered: { value: 949, unit: "L" },
    warnings: [W.PARTIAL_STOCK],
    policy_preview: {
      outcome: "PENDING_APPROVAL",
      hits: [{ rule: "PARTIAL_STOCK", detail: "Covers 51 of 1,000 L by the delivery date" }],
    },
  },
];

const LOOKUP_ID = "4cc38466-facf-40c3-b59f-ac959d50ef6b";
const LOOKUP: LookupResult = {
  lookup_id: LOOKUP_ID,
  agent_id: "web-console",
  mandate: null,
  need: {
    customer_id: "1",
    intent: "1000 litres of lactose-free milk for a hotel",
    search_terms: ["milk"],
    required_claims: ["FREE_FROM_LACTOSE"],
    exclude_allergens: ["AE"],
    quantity: { value: NEED, unit: "L" },
    deliver_by: DELIVER_BY,
    max_candidates: 10,
    preferences: { brands: ["Valio"], pack_size: { max: 1 } },
  },
  effective_constraints: {
    required_claims: [{ code: "FREE_FROM_LACTOSE", source: "request" }],
    exclude_allergens: [{ code: "AE", source: "request" }],
  },
  exclusion_counts: { FREE_FROM_LACTOSE: 1287, AE: 116 },
  offers: OFFERS,
  plans: PLANS,
  no_match_reason: null,
  excluded_count: 1403,
  retrieval: { mode: "terms", terms_used: ["milk"] },
  matched_count: 1443,
  screened_count: 40,
  priced_count: 15,
  live_calls: { price: 1, availability: 1 },
  decision_calls: 11,
  warnings: [{ code: "POOL_TRUNCATED", text: "40 of 1,443 matching products screened" }],
};

const line = (id: string, units: number): QuoteLine => {
  const o = byId(id);
  const total = Math.round((o.unit_price ?? 0) * units * 100) / 100;
  return { ...o, units_needed: units, quantity_sales_units: units, total, purchase_total: total };
};

type Seed = {
  id: string; status: OrderStatus; agent: string; minutesAgo: number; lines: [string, number][];
  hits?: { rule: string; detail: string }[]; approver?: string; comment?: string; cancelReason?: string;
  /** Rejected by the policy at placement rather than by a person. */
  outcome?: "REJECTED";
};

const AUTONOMY_HIT = { rule: "autonomy_level", detail: "This agent's orders need sign-off until autonomy is granted." };

const SEEDS: Seed[] = [
  { id: "d03628db-3531-4965-b3fc-c860c80bfd6a", status: "CONFIRMED", agent: "hotel-chain-agent", minutesAgo: 60 * 26, lines: [["469330", 201]],
    hits: [{ rule: "max_auto_total_eur", detail: "Net total 1716.54 exceeds 1500.00 EUR" }, { rule: "CUTOFF_PASSED", detail: "469330: Upstream closingTime is before timeNow" }],
    approver: "Ada", comment: "Checked budget and delivery date" },
  { id: "e8a1f2c4-6b0d-4c1e-9f77-2a5b3c9d1e01", status: "CANCELLED", agent: "web-console", minutesAgo: 60 * 20, lines: [["464407", 1000]],
    approver: undefined, cancelReason: "Event called off" },
  { id: "5b9d4e2a-8c3f-4a1b-b6d7-0e9f8a7c6b52", status: "REJECTED", agent: "hotel-chain-agent", minutesAgo: 60 * 14, lines: [["466279", 100]],
    hits: [{ rule: "DATE_SHIFT", detail: "466279: earliest delivery is two days after the requested date" }, { rule: "max_auto_total_eur", detail: "Net total 2722.00 exceeds 1500.00 EUR" }],
    approver: "Ada", comment: "Too late for the Monday booking" },
  { id: "9e3b7c1d-4f2a-4d8e-b5c6-1a2b3c4d5e6f", status: "REJECTED", agent: "hotel-chain-agent", minutesAgo: 60 * 8, lines: [["466104", 667]],
    outcome: "REJECTED",
    hits: [{ rule: "min_fit_probability", detail: "466104: 0.780 < 0.800" },
      { rule: "NOT_AVAILABLE", detail: "466104: Valio reports this product as not available" },
      { rule: "PARTIAL_STOCK", detail: "466104: On-time available stock covers only part of the requested quantity." }] },
  { id: "57077558-efd5-4e3e-a07d-258fb3af57c5", status: "CONFIRMED", agent: "web-console", minutesAgo: 190, lines: [["464308", 1000], ["464407", 1000]],
    hits: [{ rule: "max_auto_total_eur", detail: "Net total 2110.00 exceeds 1500.00 EUR" }], approver: "Ada", comment: "approved in console" },
  { id: "d7333e82-3a71-4204-b89c-9a65240f6ece", status: "CONFIRMED", agent: "web-console", minutesAgo: 120, lines: [["464308", 1000]] },
  { id: "a41c7e90-2d58-4f3b-8e16-7c0d9b5a3f28", status: "PENDING_APPROVAL", agent: "hotel-chain-agent", minutesAgo: 6, lines: [["463038", 100]],
    hits: [{ rule: "max_auto_total_eur", detail: "Net total 1597.00 exceeds 1500.00 EUR" }, { rule: "CUTOFF_PASSED", detail: "463038: Upstream closingTime is before timeNow" }] },
  { id: "c2f6b8d1-9e4a-4c7b-a3f5-6d8e0b1c2a93", status: "PENDING_APPROVAL", agent: "hotel-chain-agent", minutesAgo: 2, lines: [["469386", 120], ["464407", 400]],
    hits: [{ rule: "OUTSIDE_PREFERENCE", detail: "469386: brand Arla, preferred Valio" }] },
  // Spec 40: a newly registered agent on the approval-first path. Small in-limit orders, all signed off unchanged
  // so far — three of the five the promotion threshold asks for.
  ...[60 * 70, 60 * 46, 60 * 23].map((minutesAgo, i): Seed => ({
    id: `f1a0c3e5-7b2d-4e8f-9a1c-00000000000${i + 1}`, status: "CONFIRMED", agent: "kitchen-planner-agent", minutesAgo,
    lines: [["464308", 300 + i * 50]], hits: [AUTONOMY_HIT], approver: "Ada", comment: "Unchanged, within budget",
  })),
  { id: "f1a0c3e5-7b2d-4e8f-9a1c-000000000004", status: "PENDING_APPROVAL", agent: "kitchen-planner-agent", minutesAgo: 4,
    lines: [["464407", 400]], hits: [AUTONOMY_HIT] },
];

const ORDERS: Order[] = SEEDS.map((s, i) => {
  const lines = s.lines.map(([id, u]) => line(id, u));
  const total = Math.round(lines.reduce((a, l) => a + (l.total ?? 0), 0) * 100) / 100;
  const decided = s.status !== "PENDING_APPROVAL";
  const byHuman = !!s.approver;
  return {
    id: s.id,
    customer_id: "1",
    quote_id: `q-${s.id.slice(0, 8)}`,
    agent_id: s.agent,
    idempotency_key: `idem-${i + 1}`,
    status: s.status,
    created_at: ago(s.minutesAgo),
    expires_at: s.status === "PENDING_APPROVAL" ? ahead(30 - s.minutesAgo) : ago(s.minutesAgo - 30),
    decided_at: decided ? ago(Math.max(0, s.minutesAgo - 4)) : null,
    decided_by_kind: !decided ? null : byHuman || s.status === "CANCELLED" ? "operator" : s.outcome ? "system" : "agent",
    total,
    currency: "EUR",
    lines,
    warnings: lines.flatMap((l) => l.warnings),
    policy: { outcome: s.outcome ?? (s.hits?.length ? "PENDING_APPROVAL" : "CONFIRMED"), hits: s.hits ?? [] },
    live_calls: { price: 1, availability: 1 },
    evidence_revision: 1,
    approver: s.approver ?? null,
    comment: s.comment ?? null,
    cancelled_by: s.status === "CANCELLED" ? "Ada" : null,
    cancel_reason: s.cancelReason ?? null,
    commitment: "sha256:" + s.id.replaceAll("-", "").slice(0, 32),
    quote_signature_digest: "sha256:" + s.id.replaceAll("-", "").slice(8, 40),
    receipt: decided ? "eyJhbGciOiJFZERTQSJ9.eyJvcmRlciI6Ii4uLiJ9.c2lnbmF0dXJl" : null,
    mandate: s.agent === "hotel-chain-agent"
      ? { id: "mandate-hotel-1", issuer: "did:web:hotel-chain.example", limits: { max_auto_daily_total_eur: 5000 }, valid_until: dayAhead(30) + "T00:00:00Z" }
      : null,
    next_action: s.status === "PENDING_APPROVAL" ? "WAIT_FOR_APPROVAL" : "STOP",
  };
});

// Spec 45: the pending novobox order is far above this buyer's usual quantity; the gateway proposes a split.
const UNUSUAL: Anomaly = {
  code: "QUANTITY_ANOMALY", status: "anomaly", product_id: "463038", observed: 100, unit: "SALES_UNIT",
  expected_band: { lower: 38, upper: 46, median: 42 }, band_source: "statistical",
  history_window: { start: ago(60 * 24 * 84), end: ago(60 * 24 * 7), points: 12, weekday: 5 },
  fixes: [{ id: "463038:split", kind: "split", product_id: "463038", quantity_sales_units: 42, remainder_sales_units: 58,
    description: "Quote the usual quantity now and hold the remainder for human approval." }],
};
ORDERS.find((o) => o.id === "a41c7e90-2d58-4f3b-8e16-7c0d9b5a3f28")!.anomalies = [UNUSUAL];

const quoteFor = (o: Order): Quote => ({
  id: o.quote_id,
  signature: "eyJhbGciOiJFZERTQSIsImtpZCI6ImdhdGV3YXktMSJ9..sig",
  price_commitment: o.commitment,
  agent_id: o.agent_id,
  customer_id: "1",
  deliver_by: DELIVER_BY,
  need_ref: LOOKUP_ID,
  plan_ref: null,
  warnings: o.warnings,
  status: "ACCEPTED",
  created_at: o.created_at,
  expires_at: o.expires_at,
  lines: o.lines,
  total: o.total,
  currency: "EUR",
  hard_constraint_violations: [],
  live_calls: { price: 1, availability: 1 },
});

/** Entries written after load, such as a new agent's first call from the Connect page. */
const EXTRA_AUDIT: Omit<AuditEntry, "id">[] = [];
const CONNECTIONS: Connection[] = [
  { connection_id: "con_5d1e7a2b", agent_id: "hotel-chain-agent", client: "claude_desktop", key_prefix: "vak_3f9c",
    created_at: ago(60 * 30), last_used_at: ago(6), revoked_at: null, credential_kind: "api_key" },
  // An operator approved ChatGPT's sign-in: no key exists, so the prefix is only a label.
  { connection_id: "con_9a4f0c6e", agent_id: "events-buyer-agent", client: "chatgpt", key_prefix: "oauth",
    created_at: ago(60 * 3), last_used_at: ago(40), revoked_at: null, credential_kind: "oauth" },
];

function auditFor(): AuditEntry[] {
  const rows: Omit<AuditEntry, "id">[] = [];
  for (const o of ORDERS) {
    const t = Date.parse(o.created_at);
    const at = (dm: number) => new Date(t + dm * 60000).toISOString();
    const agent = `agent:${o.agent_id}`;
    rows.push({ ts: at(-3), actor: agent, action: "LOOKUP", order_id: null, quote_id: null,
      summary: "Shortlisted 10 offers from 40 screened, 1 + 1 Valio calls.", data: { lookup_id: LOOKUP_ID, live_calls: { price: 1, availability: 1 } } });
    rows.push({ ts: at(-1), actor: agent, action: "CREATE_QUOTE", order_id: null, quote_id: o.quote_id,
      summary: `Locked fixed price ${o.total.toFixed(2)} EUR.`, data: { total: o.total } });
    rows.push({ ts: at(0), actor: agent, action: "PLACE_ORDER", order_id: o.id, quote_id: o.quote_id,
      summary: `Order ${o.policy?.hits.length ? "PENDING_APPROVAL" : "CONFIRMED"} at the locked total of ${o.total.toFixed(2)} EUR.`,
      data: { policy: o.policy } });
    if (o.status === "CONFIRMED" && o.approver)
      rows.push({ ts: at(4), actor: "human:operator", action: "APPROVE", order_id: o.id, quote_id: o.quote_id,
        summary: `Human ${o.approver} approved the order.`, data: { stated_name: o.approver, comment: o.comment } });
    if (o.status === "REJECTED")
      rows.push({ ts: at(4), actor: "human:operator", action: "REJECT", order_id: o.id, quote_id: o.quote_id,
        summary: `Human ${o.approver} rejected the order.`, data: { stated_name: o.approver, comment: o.comment } });
    if (o.status === "CANCELLED")
      rows.push({ ts: at(40), actor: "human:operator", action: "CANCEL", order_id: o.id, quote_id: o.quote_id,
        summary: "Order cancelled before the cut-off; reservation released.", data: { stated_name: "Ada", reason: o.cancel_reason } });
  }
  rows.push({ ts: ago(60 * 22), actor: "human:operator", action: "POLICY_UPDATED", order_id: null, quote_id: null,
    summary: "Updated procurement policy.", data: { stated_name: "Ada", before: { max_auto_total_eur: 1000 }, after: { max_auto_total_eur: 1500 } } });
  rows.push(...EXTRA_AUDIT);
  return rows.sort((a, b) => a.ts.localeCompare(b.ts)).map((r, i) => ({ id: i + 1, ...r }));
}

const POLICY: PolicyResource = {
  approval: {
    max_auto_total_eur: 1500,
    min_fit_probability: 0.8,
    warnings_need_approval: ["PARTIAL_STOCK", "DATE_SHIFT", "CUTOFF_PASSED", "SIZE_UNKNOWN", "SIZE_MISMATCH", "NO_FIT_SCORE",
      "PRICE_ERROR", "AVAILABILITY_ERROR", "OUTSIDE_PREFERENCE", "SIZE_UNCONFIRMED", "UNIT_MISMATCH", "STOCK_MODEL_UNKNOWN", "CUTOFF_UNKNOWN"],
    ignored_warnings: ["VARIABLE_WEIGHT", "DEPOSIT", "RETRIEVAL_BROADENED", "POOL_TRUNCATED", "AVAILABILITY_UNCONFIRMED"],
  },
  reject: { on_hard_constraint_violation: true, on: ["SALES_BLOCK", "UNKNOWN_PRODUCT", "NOT_AVAILABLE", "NO_STOCK"] },
  ranking_weights: { price: 0.4, coverage: 0.25, reliability: 0.15, co2: 0.1, fit: 0.1 },
  shortlist: { catalog_pool: 40, live_max: 25 },
  agents: {
    // Same as backend/policy.yaml (spec 40 R-6): the demo mandates carry autonomy so the demo still confirms alone.
    "hotel-chain-agent": { autonomy: "autonomous_within_limits", customer_ids: ["1"], max_auto_daily_total_eur: 5000,
      note: "autonomy granted after the pilot's approved orders (demo setting)" },
    "web-console": { autonomy: "autonomous_within_limits", customer_ids: ["1"], max_auto_daily_total_eur: 5000,
      note: "autonomy granted after the pilot's approved orders (demo setting)" },
    "kitchen-planner-agent": { customer_ids: ["1"], max_auto_daily_total_eur: 1500, note: "Central-kitchen planner, registered this week." },
  },
  autonomy: {
    default: "approval_required",
    promotion: { min_approved_unchanged: 5, max_rejection_share: 0.1, days_since_last_rejection_or_refusal: 7 },
  },
  customers: {
    "1": { required_claims: ["FREE_FROM_LACTOSE"], exclude_allergens: ["AE"], note: "Hotel-chain standing procurement constraints." },
  },
  updated_at: ago(60 * 22),
  updated_by: "Ada",
  known_warning_codes: ["AVAILABILITY_ERROR", "AVAILABILITY_UNCONFIRMED", "CUTOFF_PASSED", "CUTOFF_UNKNOWN", "DATE_SHIFT", "DEPOSIT",
    "NOT_AVAILABLE", "NO_FIT_SCORE", "NO_STOCK", "OUTSIDE_PREFERENCE", "PARTIAL_STOCK", "POOL_TRUNCATED", "PRICE_ERROR",
    "RETRIEVAL_BROADENED", "SALES_BLOCK", "SIZE_MISMATCH", "SIZE_UNCONFIRMED", "SIZE_UNKNOWN", "STOCK_MODEL_UNKNOWN",
    "UNIT_MISMATCH", "UNKNOWN_PRODUCT", "VARIABLE_WEIGHT"],
};

function stats(): Stats {
  const by: Record<string, number> = { CONFIRMED: 0, PENDING_APPROVAL: 0, REJECTED: 0, EXPIRED: 0, CANCELLED: 0 };
  ORDERS.forEach((o) => (by[o.status] += 1));
  const placement = { CONFIRMED: 0, PENDING_APPROVAL: 0, REJECTED: 0 };
  ORDERS.forEach((o) => (o.policy?.hits.length ? placement.PENDING_APPROVAL++ : placement.CONFIRMED++));
  const warnings = new Map<string, number>();
  OFFERS.forEach((o) => o.warnings.forEach((w) => warnings.set(w.code, (warnings.get(w.code) ?? 0) + 3)));
  const rules = new Map<string, number>();
  ORDERS.forEach((o) => o.policy?.hits.forEach((h) => rules.set(h.rule, (rules.get(h.rule) ?? 0) + 1)));
  const agents = new Map<string, number>();
  ORDERS.forEach((o) => agents.set(o.agent_id, (agents.get(o.agent_id) ?? 0) + 1));
  return {
    since: null,
    // Count the same LOOKUP entries the gateway counts, so the KPI and its trend line agree.
    lookups: auditFor().filter((e) => e.action === "LOOKUP").length,
    orders_by_status: by,
    placement_outcomes: placement,
    human_decisions: {
      approved: ORDERS.filter((o) => o.status === "CONFIRMED" && o.approver).length,
      rejected: ORDERS.filter((o) => o.status === "REJECTED").length,
    },
    confirmed_spend_total_eur: Math.round(ORDERS.filter((o) => o.status === "CONFIRMED").reduce((a, o) => a + o.total, 0) * 100) / 100,
    average_live_calls_per_lookup: { price: 1, availability: 1 },
    total_decisions_calls: 131,
    top_warning_codes: [...warnings].map(([code, count]) => ({ code, count })).sort((a, b) => b.count - a.count),
    top_rule_hits: [...rules].map(([rule, count]) => ({ rule, count })).sort((a, b) => b.count - a.count),
    agents_seen: [
      ...[...agents].map(([id, n]) => ({ actor: `agent:${id}`, order_count: n, registered: id in (POLICY.agents ?? {}), mandate_id: id === "hotel-chain-agent" ? "mandate-hotel-1" : null })),
      { actor: "agent:unknown-reseller-bot", order_count: 0, registered: false, mandate_id: null },
    ],
    throttles: 0,
    upstream_rate_limits: 0,
  };
}

function timeline(orderId: string) {
  const o = ORDERS.find((x) => x.id === orderId)!;
  const kind: Record<string, TimelineStep["step"]> = {
    LOOKUP: "lookup", CREATE_QUOTE: "quote", PLACE_ORDER: "order", APPROVE: "decision", REJECT: "decision", CANCEL: "cancellation",
  };
  const entries = auditFor().filter((e) => e.order_id === orderId || e.quote_id === o.quote_id || (e.action === "LOOKUP" && Math.abs(Date.parse(e.ts) - Date.parse(o.created_at)) < 5 * 60000));
  const steps: TimelineStep[] = entries.map((e) => ({
    hash: "sha256:" + (e.id * 2654435761).toString(16).padStart(12, "0"),
    ts: e.ts,
    actor: e.actor,
    action: e.action,
    step: kind[e.action] ?? "system",
    summary: e.summary ?? "",
    data: e.data ?? {},
    live_calls: e.action === "LOOKUP" || e.action === "CREATE_QUOTE" ? { price: 1, availability: 1 } : undefined,
    decision_calls: e.action === "LOOKUP" ? 11 : undefined,
  }));
  return {
    order_id: o.id, quote_id: o.quote_id, lookup_id: LOOKUP_ID, agent_id: o.agent_id, status: o.status, steps,
    total_live_calls: { price: 2, availability: 3 }, total_decision_calls: 11,
  };
}

function product(id: string) {
  const o = byId(id) ?? OFFERS[0];
  return {
    id: o.product_id, gtin: o.gtin, name_en: o.name, name_fi: o.name.replace("milk drink", "maitojuoma"),
    category: "10400", sales_unit: o.sales_unit, base_unit: "L", allowed_lot_size: 1,
    vendor_name: o.name.includes("Arla") ? "ARLA OY" : "VALIO OY", brand: o.name.includes("Arla") ? "Arla" : "Valio",
    country: "fi", temperature_condition: 4, units: { [o.sales_unit]: o.size_per_sales_unit ?? 1 },
    claims: ["FREE_FROM_LACTOSE"], allergens_contains: ["AM"],
    allergen_status: { AM: ["contains"], ML: ["free_from"] }, has_allergen_declaration: true,
    free_from: ["LACTOSE"], labels: o.labels, base_volume_l: 1, sales_volume_l: o.size_per_sales_unit,
    variable_unit: false, delivery_reliability: o.delivery_reliability, co2_kg_per_kg: o.co2_kg_per_kg,
  };
}

/** Same thresholds and wording shape as the gateway: every unmet threshold is named, and a person decides. */
function trackRecord(agentId: string, windowDays: number): AgentTrackRecord {
  const DAY = 86400000;
  const since = NOW - windowDays * DAY;
  const mine = ORDERS.filter((o) => o.agent_id === agentId && Date.parse(o.created_at) >= since);
  const approvedUnchanged = mine.filter((o) => o.status === "CONFIRMED" && o.approver).length;
  const rejected = mine.filter((o) => o.status === "REJECTED");
  const lastRejection = rejected.map((o) => o.decided_at ?? o.created_at).sort().at(-1) ?? null;
  const promo = POLICY.autonomy?.promotion ?? {};
  const need = promo.min_approved_unchanged ?? 5;
  const maxShare = promo.max_rejection_share ?? 0.1;
  const quietDays = promo.days_since_last_rejection_or_refusal ?? 7;
  const rejectionShare = mine.length ? rejected.length / mine.length : 0;
  const reasons: string[] = [];
  if (approvedUnchanged < need) reasons.push(`${approvedUnchanged} of ${need} orders approved unchanged`);
  if (rejectionShare > maxShare)
    reasons.push(`${Math.round(rejectionShare * 100)}% of orders rejected; at most ${Math.round(maxShare * 100)}% allowed`);
  if (lastRejection && NOW - Date.parse(lastRejection) < quietDays * DAY)
    reasons.push(`last rejection ${Math.round((NOW - Date.parse(lastRejection)) / 3600000)} h ago; ${quietDays} days without one required`);
  return {
    agent_id: agentId,
    window_days: windowDays,
    window_start: new Date(since).toISOString(),
    window_end: new Date(NOW).toISOString(),
    orders_placed: mine.length,
    approved_unchanged: approvedUnchanged,
    rejected: rejected.length,
    expired: mine.filter((o) => o.status === "EXPIRED").length,
    evidence_changes: 0,
    cancellations: mine.filter((o) => o.status === "CANCELLED").length,
    hard_constraint_refusals: 0,
    average_approval_time_seconds: mine.some((o) => o.approver) ? 240 : null,
    approved_unchanged_share: mine.length ? approvedUnchanged / mine.length : 0,
    rejection_share: rejectionShare,
    last_rejection_at: lastRejection,
    last_hard_constraint_refusal_at: null,
    autonomy_recommendation: { eligible: reasons.length === 0, reasons },
  };
}

const HEAD_HASH ="9f3c1d7ab25e4c08f6e1d93b7a40c2e58b1f6d09a3e7c4b25d8f01e6a9c3b7d42";

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export async function mockCall(path: string, init?: RequestInit): Promise<unknown> {
  await sleep(140);
  const method = (init?.method ?? "GET").toUpperCase();
  const body = init?.body ? JSON.parse(String(init.body)) : {};
  const url = new URL(path, "http://mock");
  const p = url.pathname;
  let m: RegExpMatchArray | null;

  if (p === "/health") return { status: "ok", catalog_rows: 17546 };
  if (p === "/v1/me") return { principal: { id: "operator", kind: "operator", role: "valio_operator" }, customer_ids: ["1"], environment: "mock" };
  if (p === "/v1/meta")
    // Same shape as backend/app/foundation.py manifest().
    return {
      specifications: {
        openapi: "3.1.0",
        mcp_protocol: "2025-11-25",
        mcp_server_card: { status: "draft; planned_spec_37", draft_commit: "526201bbc80231daa40ffcdecfc9da4e54e5dc93" },
        a2a: { version: "1.0", status: "planned_spec_34" },
        ubl: "2.1",
        vc_data_model: "2.0",
        vc_securing_profile: "VC-JWT EdDSA / RFC 7519 / RFC 8037",
        problem_details: "RFC 9457",
        standard_webhooks: { commit: "36afae792227eae8a182a7a1a5711dbdc08a4334", status: "planned_spec_33" },
      },
      packages: { fastapi: "0.162.7", mcp: "2.3.0", pydantic: "2.14.0", httpx: "0.28.1" },
      limiter_scope: "in-process, single instance",
      recovery_version: 1,
    };
  if (p === "/.well-known/jwks.json")
    return { keys: [{ kid: "gateway-1", kty: "OKP", crv: "Ed25519", alg: "EdDSA", use: "sig", x: "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo" }] };
  if (p === "/v1/offers" && method === "POST")
    // The gateway's rule: search terms when there are any, otherwise words from the intent (broadened here).
    return { ...LOOKUP, need: { ...LOOKUP.need, ...body }, retrieval: body.search_terms?.length
      ? { mode: "terms", terms_used: body.search_terms }
      : { mode: "intent_broadened", terms_used: String(body.intent ?? "").toLowerCase().match(/[a-z]{4,}/g) ?? [] } };
  if ((m = p.match(/^\/v1\/lookups\/([^/]+)$/))) return { ...LOOKUP, lookup_id: m[1] };
  if ((m = p.match(/^\/v1\/products\/([^/]+)$/))) return product(m[1]);
  if (p === "/v1/stats") return stats();
  if (p === "/v1/policy" && method === "GET") return POLICY;
  if (p === "/v1/policy" && method === "PUT") {
    Object.assign(POLICY, body.policy, { updated_by: body.updated_by, updated_at: new Date().toISOString() });
    return POLICY;
  }
  if (p === "/v1/policy/reset") return Object.assign(POLICY, { updated_by: body.updated_by, updated_at: new Date().toISOString() });
  if (p === "/v1/mandates/revocations") return { revoked: true };
  // Copies, like a real response: handing React the same mutated array would skip the re-render.
  if (p === "/v1/connections" && method === "GET") return CONNECTIONS.map((c) => ({ ...c }));
  if (p === "/v1/connections" && method === "POST") {
    const id: string = body.agent_id;
    const agents = (POLICY.agents ??= {});
    // docs/CONNECT-API.md: a new agent starts approval-first; an existing one keeps its mandate.
    const mandate = (agents[id] ??= { autonomy: "approval_required", customer_ids: body.customer_ids, max_auto_daily_total_eur: 1000,
      note: "connected from the console" });
    const key = `vak_${crypto.randomUUID().replaceAll("-", "")}`;
    const c: Connection = { connection_id: `con_${crypto.randomUUID().slice(0, 8)}`, agent_id: id, client: body.client,
      key_prefix: key.slice(0, 8), created_at: new Date().toISOString(), last_used_at: null, revoked_at: null, credential_kind: "api_key" };
    CONNECTIONS.unshift(c);
    // The new agent "calls" a few seconds later so the Check-it-works step can be reviewed turning green.
    setTimeout(() => {
      c.last_used_at = new Date().toISOString();
      EXTRA_AUDIT.push({ ts: c.last_used_at, actor: `agent:${id}`, action: "LOOKUP", order_id: null, quote_id: null,
        summary: "Shortlisted 10 offers from 40 screened.", data: null });
    }, 6000);
    return { ...c, key, mcp_url: null, rest_base_url: `${location.origin}/v1`, mandate };
  }
  if ((m = p.match(/^\/v1\/connections\/([^/]+)$/)) && method === "DELETE") {
    const c = CONNECTIONS.find((x) => x.connection_id === decodeURIComponent(m![1]));
    if (!c) throw new Error("mock: no such connection");
    c.revoked_at = new Date().toISOString();
    return c;
  }
  if ((m = p.match(/^\/v1\/agents\/([^/]+)\/track-record$/)))
    return trackRecord(decodeURIComponent(m[1]), Number(url.searchParams.get("window_days") ?? 30));
  if ((m = p.match(/^\/v1\/agents\/([^/]+)\/autonomy$/)) && method === "POST") {
    const id = decodeURIComponent(m[1]);
    const mandate = POLICY.agents?.[id];
    if (!mandate) throw new Error(`mock: no mandate for ${id}`);
    mandate.autonomy = body.level;
    return { agent_id: id, level: body.level, reason: body.reason, track_record: trackRecord(id, 30) };
  }
  if (p.startsWith("/v1/oauth/requests/") && method === "GET") return { id: "req_demo", client_name: "ChatGPT", redirect_host: "chatgpt.com",
    resource: `${location.origin}/mcp`, scopes: ["mcp"], expires_at: ahead(10), status: "pending", customer_ids: ["1", "42"],
    agents: Object.keys(POLICY.agents ?? {}) };
  if (p.startsWith("/v1/oauth/requests/") && method === "POST") return { status: body.decision === "approve" ? "approved" : "denied", redirect_url: "/connect" };
  if (p === "/v1/auth/signup/options") return {
    business_types: [["single_restaurant", "Single restaurant"], ["hotel_chain", "Hotel chain"], ["hospital_or_school_kitchen", "Hospital or school kitchen"]].map(([value, label]) => ({ value, label })),
    monthly_purchase_volume_bands: [["<5k", "Under €5,000"], ["5–25k", "€5,000–25,000"]].map(([value, label]) => ({ value, label })),
    meals_per_day_bands: [["<100", "Under 100"], ["100–500", "100–500"]].map(([value, label]) => ({ value, label })),
  };
  if (p === "/v1/auth/signup" && method === "POST") return {
    tenant: { id: "ten_mock", name: body.name }, user: { email: body.email, role: "tenant_admin" }, customer_ids: ["buyer_mock"],
    guardrail_profile: { preset: body.profile.business_type, warnings: [], guardrails: {
      per_order_ceiling_eur: { value: 1500, source: `preset:${body.profile.business_type}` },
      four_eyes: { value: true, source: `preset:${body.profile.business_type}` },
      starting_autonomy: { value: "approval_required", source: `preset:${body.profile.business_type}` } } },
  };
  if (p === "/v1/suppliers") return [
    { supplier_id: "valio", synthetic: false, capabilities: { currencies: ["EUR"], pricing: "sale-unit", stock: ["finite", "unknown"],
      execution: { mode: "simulated", atomicity: "atomic", partial_acceptance: false } } },
    { supplier_id: "nordfood", synthetic: true, capabilities: { currencies: ["EUR", "SEK"], pricing: "case; minimum case quantity", stock: ["finite"],
      execution: { mode: "supplier_backed", atomicity: "atomic", partial_acceptance: false } } },
  ];
  if ((m = p.match(/^\/v1\/quotes\/([^/]+)\/anomaly-fixes\/([^/]+)$/)) && method === "POST") {
    const source = ORDERS.find((o) => o.quote_id === m![1]);
    if (source?.status === "PENDING_APPROVAL") source.status = "CANCELLED";
    const fix = UNUSUAL.fixes[0];
    const quotes = [fix.quantity_sales_units!, fix.remainder_sales_units!].map((units, i): Quote => {
      const l = line(fix.product_id!, units);
      const q: Quote = { id: `q-${Math.random().toString(16).slice(2, 10)}`, customer_id: "1", deliver_by: DELIVER_BY, need_ref: LOOKUP_ID,
        status: "OPEN", created_at: new Date().toISOString(), expires_at: ahead(30), lines: [l], total: l.total ?? 0, currency: "EUR",
        hard_constraint_violations: [], live_calls: { price: 1, availability: 1 }, anomalies: i ? [{ ...UNUSUAL, observed: units, fixes: [] }] : [] };
      QUOTES.set(q.id, q);
      return q;
    });
    return { source_quote_id: m[1], fix_id: decodeURIComponent(m[2]), quotes };
  }
  if (p === "/v1/quotes" && method === "POST") {
    const plan = body.plan_ref ? PLANS[body.plan_ref.plan_index] : null;
    const lines = plan
      ? plan.lines.map((l) => line(l.product_id, l.units))
      : (body.lines ?? []).map((l: { product_id: string; quantity_sales_units: number }) => line(l.product_id, l.quantity_sales_units));
    const total = Math.round(lines.reduce((a: number, l: QuoteLine) => a + (l.total ?? 0), 0) * 100) / 100;
    const q: Quote = {
      id: `q-${Math.random().toString(16).slice(2, 10)}`, signature: "eyJhbGciOiJFZERTQSJ9..sig", price_commitment: "sha256:4f1e…",
      agent_id: "web-console", customer_id: body.customer_id, deliver_by: body.deliver_by, need_ref: body.need_ref ?? LOOKUP_ID,
      plan_ref: body.plan_ref ?? null, warnings: lines.flatMap((l: QuoteLine) => l.warnings), status: "OPEN",
      created_at: new Date().toISOString(), expires_at: ahead(30), lines, total, currency: "EUR",
      hard_constraint_violations: [], live_calls: { price: 1, availability: 1 },
    };
    QUOTES.set(q.id, q);
    return q;
  }
  if ((m = p.match(/^\/v1\/quotes\/([^/]+)$/))) {
    const o = ORDERS.find((x) => x.quote_id === m![1]);
    return QUOTES.get(m[1]) ?? (o ? quoteFor(o) : quoteFor(ORDERS[0]));
  }
  if (p === "/v1/orders" && method === "POST") {
    const q = QUOTES.get(body.quote_id)!;
    const over = q.total > POLICY.approval.max_auto_total_eur;
    const o: Order = {
      ...ORDERS[ORDERS.length - 1], id: crypto.randomUUID(), quote_id: q.id, agent_id: "web-console",
      status: over ? "PENDING_APPROVAL" : "CONFIRMED", created_at: new Date().toISOString(), expires_at: q.expires_at,
      decided_at: over ? null : new Date().toISOString(), decided_by_kind: over ? null : "agent",
      total: q.total, lines: q.lines, warnings: q.warnings,
      policy: { outcome: over ? "PENDING_APPROVAL" : "CONFIRMED",
        hits: over ? [{ rule: "max_auto_total_eur", detail: `Net total ${q.total.toFixed(2)} exceeds ${POLICY.approval.max_auto_total_eur.toFixed(2)} EUR` }] : [] },
      approver: null, comment: null, cancelled_by: null, cancel_reason: null, mandate: null,
      next_action: over ? "WAIT_FOR_APPROVAL" : "STOP",
    };
    ORDERS.push(o);
    return o;
  }
  if (p === "/v1/orders") return ORDERS;
  if ((m = p.match(/^\/v1\/orders\/([^/]+)\/timeline$/))) return timeline(m[1]);
  if ((m = p.match(/^\/v1\/orders\/([^/]+)\/cancel$/))) {
    const o = ORDERS.find((x) => x.id === m![1])!;
    Object.assign(o, { status: "CANCELLED", cancelled_by: body.cancelled_by, cancel_reason: body.reason, decided_by_kind: "operator" });
    return o;
  }
  if ((m = p.match(/^\/v1\/orders\/([^/]+)$/))) return ORDERS.find((x) => x.id === m![1]) ?? ORDERS[0];
  if (p === "/v1/approvals") return ORDERS.filter((o) => o.status === "PENDING_APPROVAL").map((order) => ({ order, quote: quoteFor(order) }));
  if ((m = p.match(/^\/v1\/approvals\/([^/]+)\/(approve|reject)$/))) {
    const o = ORDERS.find((x) => x.id === m![1])!;
    Object.assign(o, { status: m[2] === "approve" ? "CONFIRMED" : "REJECTED", approver: body.approver, comment: body.comment,
      decided_at: new Date().toISOString(), decided_by_kind: "operator", next_action: "STOP" });
    return o;
  }
  if (p === "/v1/audit/head") return { entries: auditFor().length, head_hash: HEAD_HASH, at: ago(1), jws: "eyJhbGciOiJFZERTQSJ9..head" };
  if (p === "/v1/audit/anchors")
    return [
      // Anchors snapshot earlier heads, so each holds fewer entries than the current chain.
      { entries: 12, head_hash: "4b7e2a91c0d35f68e1a92c4b7d03f5e8a6c19b2d47e0f3a58c6d1b9e2f7a4c03", chain_id: 137, tx_hash: null, block_number: null, status: "confirmed", explorer_url: null, at: ago(60 * 18) },
      { entries: 24, head_hash: "d81f3c6a09e2b45f7c1d8a3e60b9f24c5a7e1d03b68f2c9a4e75d10b3c8f6a29", chain_id: 137, tx_hash: null, block_number: null, status: "sent", explorer_url: null, at: ago(25) },
    ];
  if (p === "/v1/audit/verify")
    return { valid: true, entries: auditFor().length, head_hash: HEAD_HASH, first_break: null, checkpoint_match: true, anchor_match: true, anchor_block_time: ago(60 * 18) };
  if (p === "/v1/audit") {
    const all = auditFor();
    const order = url.searchParams.get("order_id");
    return order ? all.filter((e) => e.order_id === order) : all;
  }
  // Pages added later keep their fixtures beside their own API module.
  for (const handle of [mockRepeat, mockBasket, mockGovernance, mockChanges, mockTeam, mockWatch]) {
    const result = handle(method, p, body);
    if (result !== undefined) return result;
  }
  throw new Error(`mock: no fixture for ${method} ${p}`);
}

const QUOTES = new Map<string, Quote>();

export async function mockText(path: string): Promise<string> {
  await sleep(140);
  const id = path.split("/")[3];
  const o = ORDERS.find((x) => x.id === id) ?? ORDERS[0];
  return `<?xml version="1.0" encoding="UTF-8"?>
<Order xmlns="urn:oasis:names:specification:ubl:schema:xsd:Order-2"
       xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
       xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2">
  <cbc:CustomizationID>urn:fdc:peppol.eu:poacc:trns:order:3</cbc:CustomizationID>
  <cbc:ID>${o.id}</cbc:ID>
  <cbc:IssueDate>${o.created_at.slice(0, 10)}</cbc:IssueDate>
  <cbc:DocumentCurrencyCode>EUR</cbc:DocumentCurrencyCode>
${o.lines.map((l, i) => `  <cac:OrderLine><cac:LineItem>
    <cbc:ID>${i + 1}</cbc:ID>
    <cbc:Quantity unitCode="${l.sales_unit}">${l.quantity_sales_units ?? l.units_needed}</cbc:Quantity>
    <cbc:LineExtensionAmount currencyID="EUR">${(l.total ?? 0).toFixed(2)}</cbc:LineExtensionAmount>
    <cac:Item><cbc:Name>${l.name}</cbc:Name><cac:SellersItemIdentification><cbc:ID>${l.product_id}</cbc:ID></cac:SellersItemIdentification></cac:Item>
  </cac:LineItem></cac:OrderLine>`).join("\n")}
  <cac:AnticipatedMonetaryTotal><cbc:PayableAmount currencyID="EUR">${o.total.toFixed(2)}</cbc:PayableAmount></cac:AnticipatedMonetaryTotal>
</Order>`;
}
