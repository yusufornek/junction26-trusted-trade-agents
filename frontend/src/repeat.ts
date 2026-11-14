// Types, API calls and mock fixtures for the Repeat page.
// Shapes are taken from real gateway responses (backend/app/purchase_memory.py, backend/app/recurring.py).
import { ApiError, api, call, type LookupResult, type OfferCard, type Quantity } from "./api";
import { eur, num, ruleLabel } from "./ui";

export type Suggestion = {
  id: string;
  customer_id: string;
  product_id: string;
  supplier_id: string;
  name: string;
  unit: string;
  order_count: number;
  median_interval_days: number;
  typical_quantity: number;
  suggested_quantity: Quantity;
  last_order_date: string;
  predicted_next_need_date: string;
  confidence: "low" | "medium" | "high";
  confidence_reason: string;
  cadence_rrule: string;
  /** Confirmed orders the pattern was learned from, oldest first. */
  evidence: { date: string; quantity: number; unit: string; total_paid: number; currency: string; agent_id: string; order_id: string }[];
};

/** What one run bought, in the dimensions the gateway compares between runs. */
export type SnapshotLine = {
  product: string;
  brand: string | null;
  pack: number | null;
  unit: string | null;
  sales_unit: string;
  quantity: number;
  unit_cost: number | null;
  claims: string[] | null;
  allergens: Record<string, string[]> | null;
  evidence: { warnings: string[] };
  delivery_offset: number | null;
};
export type Snapshot = { lines: SnapshotLine[]; basket_cost: number };

export type RecurringOrder = {
  id: string;
  agent_id: string;
  customer_id: string;
  /** A need (intent + quantity) or, when `basket` is true, a recipe manifest. */
  template: { intent?: string; quantity?: Quantity };
  basket: boolean;
  approved_order_id: string;
  baseline_revision: number;
  baseline: Snapshot;
  cadence: { rrule: string; timezone: string; start_at: string };
  permitted_deviations: { price_pct: number; cumulative_price_pct: number; brand_change: boolean; pack_change: boolean };
  status: "ACTIVE" | "PAUSED";
  next_run_at: string;
};

export type Comparison = { previous: Snapshot; baseline: Snapshot; current: Snapshot; diff: string[] };

export type RecurringRun = {
  occurrence_key: string;
  occurrence_at: string;
  baseline_revision: number;
  /** The order's status when the run finished, or SKIPPED with a `reason` code when nothing was ordered. */
  status: string;
  reason?: string;
  order_id?: string;
  comparison?: Comparison;
};

const post = <T>(path: string, body: unknown = {}) => call<T>(path, { method: "POST", body: JSON.stringify(body) });

export const repeatApi = {
  suggestions: () => call<Suggestion[]>("/v1/suggestions"),
  /** An operator must name the agent to buy through (BUYER_AGENT_REQUIRED otherwise); an agent's own token needs none. */
  prepare: (id: string, agent?: string) => post<LookupResult>(`/v1/suggestions/${id}/prepare`, { buyer_agent_id: agent }),
  makeRecurring: (id: string, agent?: string) =>
    post<RecurringOrder>(`/v1/suggestions/${id}/make-recurring`, { buyer_agent_id: agent }),
  dismiss: (id: string) => post<Suggestion>(`/v1/suggestions/${id}/dismiss`),
  snooze: (id: string, days: number) => post<Suggestion>(`/v1/suggestions/${id}/snooze`, { days }),
  recurring: () => call<RecurringOrder[]>("/v1/recurring-orders"),
  runs: (id: string) => call<RecurringRun[]>(`/v1/recurring-orders/${id}/runs`),
  runNow: (id: string) => post<RecurringRun>(`/v1/recurring-orders/${id}/run-now`),
  setBaseline: (id: string, orderId: string) =>
    post<RecurringOrder>(`/v1/recurring-orders/${id}/baseline`, { approved_order_id: orderId }),
  /** Agents in the policy, and whether their mandate allows repeat orders (`recurring` is not in api.ts's AgentMandate yet). */
  agents: async () =>
    Object.entries((await api.policy()).agents ?? {}).map(([id, grant]) => ({
      id,
      recurring: Boolean((grant as { recurring?: boolean }).recurring),
    })),
};

/** "FREQ=WEEKLY;INTERVAL=2" → "every 2 weeks". */
export function cadenceWords(rrule: string): string {
  const part = (key: string) => new RegExp(`${key}=([^;]+)`, "i").exec(rrule)?.[1].toUpperCase();
  const units: Record<string, string> = { DAILY: "day", WEEKLY: "week", MONTHLY: "month", YEARLY: "year" };
  const unit = units[part("FREQ") ?? ""];
  const every = Number(part("INTERVAL") ?? 1);
  return !unit ? "on its own schedule" : every === 1 ? `every ${unit}` : `every ${every} ${unit}s`;
}

const FIELD_WORDS: Record<string, string> = {
  product: "Product",
  brand: "Brand",
  pack: "Pack content",
  unit: "Pack unit",
  sales_unit: "Sales unit",
  claims: "Product claims",
  allergens: "Allergen information",
  delivery_offset: "Delivery",
  evidence: "Stock and delivery checks",
};

/** One compared value as a person would say it; "" when the value itself is not worth showing (allergen code tables). */
function shown(field: string, line: SnapshotLine | undefined): string {
  const value = line?.[field as keyof SnapshotLine];
  if (value == null || field === "allergens") return "";
  if (field === "delivery_offset") {
    const days = Math.abs(value as number);
    return days === 0 ? "on the requested day" : `${days} ${days === 1 ? "day" : "days"} ${(value as number) > 0 ? "late" : "early"}`;
  }
  if (field === "evidence") return line!.evidence.warnings.map(ruleLabel).join(", ") || "no warnings";
  if (field === "pack") return `${num(value as number, 3)} ${line!.unit ?? ""}`.trim();
  if (field === "claims") return (value as string[]).map(ruleLabel).join(", ") || "none";
  return String(value);
}

/** The gateway's drift lines are written for a log ("line 1 pack: 5.0 → 2.5 (baseline 5.0)"). The same facts sit
 *  structured in the comparison, so each line is only used to find which fact changed. Unknown lines pass through. */
function driftWords(diff: string, c: Comparison): string {
  if (diff.includes("comparison evidence missing"))
    return "Some details could not be compared with the approved order, so a person has to check.";
  const m = /^(?:line (\d+) )?(basket cost €|unit cost €|quantity|\w+:)/.exec(diff);
  if (!m) return diff;
  const index = m[1] ? Number(m[1]) - 1 : 0;
  const lines = [c.current, c.previous, c.baseline].map((s) => s.lines[index]);
  const item = m[1] && c.current.lines.length > 1 ? `Item ${m[1]} (product ${lines[0]?.product ?? "unknown"}): ` : "";
  let label: string;
  let values: string[];
  if (m[2] === "basket cost €") {
    label = "Total cost";
    values = [c.current, c.previous, c.baseline].map((s) => eur(s.basket_cost));
  } else if (m[2] === "unit cost €") {
    label = "Price per litre or kilo";
    values = lines.map((l) => eur(l?.unit_cost));
  } else if (m[2] === "quantity") {
    label = "Quantity";
    values = lines.map((l) => num(l?.quantity, 2));
  } else {
    const field = m[2].slice(0, -1);
    label = FIELD_WORDS[field] ?? ruleLabel(field);
    values = lines.map((l) => shown(field, l));
  }
  const [now, last, approved] = values;
  return !now || (now === last && now === approved)
    ? `${item}${label} changed since the approved order.`
    : `${item}${label}: now ${now} (last run ${last}, approved order ${approved}).`;
}

/** What changed in a run, in plain words, without repeats. */
export const driftList = (c: Comparison) => [...new Set(c.diff.map((d) => driftWords(d, c)))];

// ── Mock fixtures (VITE_MOCK=1). Mutable so dismiss, snooze, make-recurring and run-now show an effect. ──

const day = (offset: number) => new Date(Date.now() + offset * 86400000).toISOString();

const suggestion = (id: string, name: string, quantity: number, rrule: string, confidence: Suggestion["confidence"], orders: number): Suggestion => ({
  id,
  customer_id: "1",
  product_id: id === "sug-milk" ? "464306" : "471000",
  supplier_id: "valio",
  name,
  unit: "L",
  order_count: orders,
  median_interval_days: 7,
  typical_quantity: quantity,
  suggested_quantity: { value: quantity, unit: "L" },
  last_order_date: day(-6).slice(0, 10),
  predicted_next_need_date: day(1).slice(0, 10),
  confidence,
  confidence_reason:
    confidence === "high" ? "Stable cadence and quantity across at least six orders" : "Limited history or moderate variation",
  cadence_rrule: rrule,
  evidence: [
    { date: day(-6).slice(0, 10), quantity, unit: "L", total_paid: quantity * 1.01, currency: "EUR", agent_id: "hotel-chain-agent", order_id: "ord-1001" },
  ],
});

let mockSuggestions: Suggestion[] = [
  suggestion("sug-milk", "Valio Eila lactose-free milk 1 L", 1000, "FREQ=WEEKLY;INTERVAL=1", "high", 12),
  suggestion("sug-oat", "Oat drink barista 1 L", 120, "FREQ=WEEKLY;INTERVAL=2", "medium", 4),
];

const snapshot = (cost: number, change: Partial<SnapshotLine> = {}): Snapshot => ({
  lines: [
    {
      product: "464306", brand: "Valio", pack: 1, unit: "L", sales_unit: "ST", quantity: 1000, unit_cost: cost / 1000,
      claims: ["FREE_FROM_LACTOSE"], allergens: { AM: ["contains"] }, evidence: { warnings: [] }, delivery_offset: 0, ...change,
    },
  ],
  basket_cost: cost,
});

const recurringOrder = (id: string, intent: string, rrule: string): RecurringOrder => ({
  id,
  agent_id: "hotel-chain-agent",
  customer_id: "1",
  template: { intent, quantity: { value: 1000, unit: "L" } },
  basket: false,
  approved_order_id: "ord-1001",
  baseline_revision: 1,
  baseline: snapshot(1010),
  cadence: { rrule, timezone: "Europe/Helsinki", start_at: day(-14) },
  permitted_deviations: { price_pct: 2, cumulative_price_pct: 5, brand_change: false, pack_change: false },
  status: "ACTIVE",
  next_run_at: day(1),
});

const run = (offset: number, status: string, current: Snapshot | null, diff: string[] = [], reason?: string): RecurringRun => ({
  occurrence_key: `rec-milk:${day(offset).slice(0, 10)}`,
  occurrence_at: day(offset),
  baseline_revision: 1,
  status,
  ...(reason ? { reason } : {}),
  ...(current
    ? { order_id: `ord-run${offset}`, comparison: { previous: snapshot(1010), baseline: snapshot(1010), current, diff } }
    : {}),
});

const mockRecurring: RecurringOrder[] = [recurringOrder("rec-milk", "lactose-free milk", "FREQ=WEEKLY;INTERVAL=1")];
const mockRuns: Record<string, RecurringRun[]> = {
  "rec-milk": [
    run(-21, "SKIPPED", null, [], "AGENT_SUSPENDED"),
    run(-14, "CONFIRMED", snapshot(1010)),
    run(-7, "PENDING_APPROVAL", snapshot(1110, { delivery_offset: 1, evidence: { warnings: ["PARTIAL_STOCK"] } }), [
      "basket cost € 1010 → 1110 (+9.9 %, cumulative +9.9 % since baseline)",
      "line 1 delivery_offset: 0 → 1 (baseline 0)",
      "line 1 evidence: {'warnings': []} → {'warnings': ['PARTIAL_STOCK']} (baseline {'warnings': []})",
      "line 1 unit cost € 1.01 → 1.11 (+9.9 %, cumulative +9.9 % since baseline)",
    ]),
  ],
};

const mockOffer: OfferCard = {
  product_id: "464306", gtin: "6408430000487", name: "Valio Eila semi skimmed milk drink 1 l lactose free", sales_unit: "ST",
  size_per_sales_unit: 1, size_unit: "L", size_source: "catalog", units_needed: 1000, unit_price: 1.01, unit_price_with_tax: 1.15,
  price_per_l_or_kg: 1.01, total: 1010, currency: "EUR", valid_from: null, valid_to: null, net_price_found: true,
  availability: { available_amount: 1400, logistic_model: "Stock", closing_time: null, available_date: day(1).slice(0, 10), enough: true, coverage: 1 },
  warnings: [], delivery_reliability: 1, co2_kg_per_kg: 1.1, labels: [], matched_because: [], fit: 0.95, fit_question: null,
  provenance: null, score: 0.9, rank_label: "best",
};

function mockPrepared(s: Suggestion): LookupResult {
  const quantity = s.suggested_quantity.value;
  const covers = { value: quantity, unit: "L" as const };
  const total = quantity * 1.01;
  return {
    lookup_id: "lookup-" + s.id,
    need: { customer_id: s.customer_id, intent: s.name, search_terms: [], required_claims: [], exclude_allergens: [], quantity: s.suggested_quantity, deliver_by: s.predicted_next_need_date },
    offers: [{ ...mockOffer, name: s.name, product_id: s.product_id, units_needed: quantity, total }],
    plans: [
      {
        kind: "single",
        lines: [{ product_id: s.product_id, units: quantity, covers }],
        total,
        coverage: covers,
        uncovered: { value: 0, unit: "L" },
        warnings: [],
        policy_preview:
          total > 500
            ? { outcome: "PENDING_APPROVAL", hits: [{ rule: "max_auto_total_eur", detail: `${eur(total)} is over the 500.00 auto-approve limit` }] }
            : { outcome: "CONFIRMED", hits: [] },
      },
    ],
    excluded_count: 0,
    live_calls: { price: 1, availability: 1 },
    decision_calls: 1,
    warnings: [],
  };
}

/** Fixture answer for this page's endpoints in mock mode; undefined means "not mine". */
export function mockRepeat(method: string, path: string, body: unknown): unknown {
  let m: RegExpMatchArray | null;
  if (path === "/v1/suggestions" && method === "GET") return [...mockSuggestions];
  if ((m = path.match(/^\/v1\/suggestions\/([^/]+)\/(prepare|make-recurring|dismiss|snooze)$/))) {
    const found = mockSuggestions.find((s) => s.id === m![1]);
    if (!found) throw new ApiError("SUGGESTION_NOT_FOUND", "Suggestion was not found or is hidden", 404);
    if (m[2] === "prepare") return mockPrepared(found);
    mockSuggestions = mockSuggestions.filter((s) => s !== found);
    if (m[2] === "dismiss") return { ...found, dismissed: true };
    if (m[2] === "snooze") return { ...found, snoozed_until: day((body as { days?: number }).days ?? 7) };
    const created = recurringOrder("rec-" + found.id, found.name, found.cadence_rrule);
    mockRecurring.push(created);
    return created;
  }
  if (path === "/v1/recurring-orders" && method === "GET") return [...mockRecurring];
  if ((m = path.match(/^\/v1\/recurring-orders\/([^/]+)\/(runs|run-now|baseline)$/))) {
    const row = mockRecurring.find((r) => r.id === m![1]);
    if (!row) throw new ApiError("RECURRING_NOT_FOUND", "Recurring order was not found", 404);
    const runs = (mockRuns[row.id] ??= []);
    if (m[2] === "runs") return [...runs];
    if (m[2] === "baseline") return Object.assign(row, { baseline_revision: row.baseline_revision + 1, approved_order_id: (body as { approved_order_id: string }).approved_order_id });
    // Like the gateway: one run per template and local date, so a second "run now" today returns the first.
    const today = runs.find((r) => r.occurrence_key.endsWith(day(0).slice(0, 10))) ?? run(0, "CONFIRMED", snapshot(1010));
    if (!runs.includes(today)) runs.push(today);
    return today;
  }
  return undefined;
}
