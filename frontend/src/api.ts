// Types mirror contracts/openapi.yaml (owned by backend). Regenerate by hand when the contract moves.

export type Warning = { code: string; text: string };
export type Provenance = { source: string; fetched_at: string; valid_to: string | null };
export type LiveCalls = { price: number; availability: number };
export type Quantity = { value: number; unit: string };

export type Availability = {
  available_amount: number | null;
  logistic_model: string | null;
  closing_time: string | null;
  available_date: string | null;
  enough: boolean;
  coverage: number | null;
};

export type OfferCard = {
  product_id: string;
  name: string;
  sales_unit: string;
  size_per_sales_unit: number | null;
  size_unit: string | null;
  size_source: string | null;
  units_needed: number | null;
  unit_price: number | null;
  unit_price_with_tax: number | null;
  price_per_l_or_kg: number | null;
  total: number | null;
  currency: string;
  valid_from: string | null;
  valid_to: string | null;
  net_price_found: boolean;
  availability: Availability | null;
  warnings: Warning[];
  delivery_reliability: number | null;
  co2_kg_per_kg: number | null;
  labels: string[];
  matched_because: string[];
  fit: number | null;
  fit_question: string | null;
  provenance: Record<string, Provenance> | null;
  score: number;
  rank_label: string | null;
};

export type NeedSpec = {
  customer_id: string;
  intent: string;
  search_terms: string[];
  required_claims: string[];
  exclude_allergens: string[];
  quantity: Quantity;
  deliver_by: string;
  max_candidates?: number;
};

export type LookupResult = {
  lookup_id: string;
  need: NeedSpec;
  offers: OfferCard[];
  excluded_count: number;
  live_calls: LiveCalls;
  decision_calls: number;
  warnings: Warning[];
};

export type QuoteLine = OfferCard & { quantity_sales_units?: number };

export type Quote = {
  id: string;
  customer_id: string;
  deliver_by: string;
  need_ref: string | null;
  status: "OPEN" | "ACCEPTED" | "EXPIRED" | string;
  created_at: string;
  expires_at: string;
  lines: QuoteLine[];
  total: number;
  currency: string;
  hard_constraint_violations: string[];
  live_calls: LiveCalls;
};

export type RuleHit = { rule: string; detail: string };
export type PolicyResult = { outcome: string; hits: RuleHit[] };

export type OrderStatus = "PENDING_APPROVAL" | "CONFIRMED" | "REJECTED" | "EXPIRED" | "CANCELLED";

export type Order = {
  id: string;
  quote_id: string;
  agent_id: string;
  idempotency_key: string;
  status: OrderStatus;
  created_at: string;
  expires_at: string;
  total: number;
  currency: string;
  lines: QuoteLine[];
  policy: PolicyResult | null;
  live_calls: LiveCalls | null;
  evidence_revision: number;
  approver: string | null;
  comment: string | null;
};

export type ApprovalItem = { order: Order; quote: Quote };

export type AuditEntry = {
  id: number;
  ts: string;
  actor: string;
  action: string;
  order_id: string | null;
  quote_id: string | null;
  summary: string | null;
  data: Record<string, unknown> | null;
};

export type Health = { status: string; catalog_rows: number };

export type PolicyResource = {
  approval: {
    max_auto_total_eur: number;
    min_fit_probability: number;
    warnings_need_approval: string[];
  };
  reject: { on_hard_constraint_violation: boolean; on: string[] };
  ranking_weights: { price: number; coverage: number; reliability: number; co2: number; fit: number };
  shortlist: { catalog_pool: number };
  updated_at: string | null;
  updated_by: string | null;
  known_warning_codes: string[];
};

/** The writable part of the policy — `updated_at` and the code list are server-owned. */
export type PolicyDocument = Pick<
  PolicyResource,
  "approval" | "reject" | "ranking_weights" | "shortlist"
>;

export type Stats = {
  since: string | null;
  lookups: number;
  orders_by_status: Record<string, number>;
  placement_outcomes: Record<string, number>;
  human_decisions: { approved: number; rejected: number };
  confirmed_spend_total_eur: number;
  average_live_calls_per_lookup: { price: number; availability: number };
  total_decisions_calls: number;
  top_warning_codes: { code: string; count: number }[];
  top_rule_hits: { rule: string; count: number }[];
  agents_seen: { actor: string; order_count: number }[];
};

export class ApiError extends Error {
  constructor(readonly code: string, message: string, readonly status: number, readonly data?: { changes?: string[]; evidence_revision?: number }) {
    super(message);
  }
}

let token = "";
const authListeners = new Set<() => void>();
export const hasToken = () => Boolean(token);
export function setToken(value: string) {
  token = value.trim();
  authListeners.forEach((listener) => listener());
}
export function onAuthChange(listener: () => void) {
  authListeners.add(listener);
  return () => { authListeners.delete(listener); };
}
export class AuthenticationError extends ApiError {
  constructor(message = "Sign in with your operator token.") {
    super("UNAUTHENTICATED", message, 401);
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}) },
  });
  if (res.status === 401) {
    setToken("");
    throw new AuthenticationError();
  }
  const text = await res.text();
  const body = text ? JSON.parse(text) : null;
  if (!res.ok) {
    // The gateway returns {code, message, data} for domain errors.
    throw new ApiError(body?.code ?? "HTTP_" + res.status, body?.message ?? res.statusText, res.status, body?.data);
  }
  return body as T;
}

export const api = {
  health: () => call<Health>("/health"),
  offers: (need: NeedSpec) =>
    call<LookupResult>("/v1/offers", {
      method: "POST",
      body: JSON.stringify(need),
    }),
  lookup: (id: string) => call<LookupResult>(`/v1/lookups/${id}`),
  createQuote: (
    body: {
      customer_id: string;
      deliver_by: string;
      need_ref: string;
      lines: { product_id: string; quantity_sales_units: number }[];
    },
  ) =>
    call<Quote>("/v1/quotes", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  quote: (id: string) => call<Quote>(`/v1/quotes/${id}`),
  placeOrder: (quoteId: string, idempotencyKey: string) =>
    call<Order>("/v1/orders", {
      method: "POST",
      body: JSON.stringify({ quote_id: quoteId }),
      headers: { "Idempotency-Key": idempotencyKey },
    }),
  orders: () => call<Order[]>("/v1/orders"),
  order: (id: string) => call<Order>(`/v1/orders/${id}`),
  approvals: () => call<ApprovalItem[]>("/v1/approvals"),
  approve: (orderId: string, approver: string, comment: string, evidence_revision: number) =>
    call<Order>(`/v1/approvals/${orderId}/approve`, {
      method: "POST",
      body: JSON.stringify({ approver, comment, evidence_revision }),
    }),
  reject: (orderId: string, approver: string, comment: string) =>
    call<Order>(`/v1/approvals/${orderId}/reject`, {
      method: "POST",
      body: JSON.stringify({ approver, comment }),
    }),
  policy: () => call<PolicyResource>("/v1/policy"),
  updatePolicy: (policy: PolicyDocument, updated_by: string, comment: string) =>
    call<PolicyResource>("/v1/policy", {
      method: "PUT",
      body: JSON.stringify({ policy, updated_by, comment }),
    }),
  resetPolicy: (updated_by: string) =>
    call<PolicyResource>("/v1/policy/reset", { method: "POST", body: JSON.stringify({ updated_by }) }),
  stats: (since?: string) => call<Stats>("/v1/stats" + (since ? `?since=${encodeURIComponent(since)}` : "")),
  audit: (params?: { order_id?: string; quote_id?: string }) => {
    const q = new URLSearchParams(
      Object.entries(params ?? {}).filter(([, v]) => v) as [string, string][],
    ).toString();
    return call<AuditEntry[]>("/v1/audit" + (q ? `?${q}` : ""));
  },
};
