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

export class ApiError extends Error {
  constructor(readonly code: string, message: string, readonly status: number) {
    super(message);
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  const text = await res.text();
  const body = text ? JSON.parse(text) : null;
  if (!res.ok) {
    // The gateway returns {code, message} for domain errors; anything else is unexpected.
    throw new ApiError(body?.code ?? "HTTP_" + res.status, body?.message ?? res.statusText, res.status);
  }
  return body as T;
}

export const api = {
  health: () => call<Health>("/health"),
  offers: (need: NeedSpec) => call<LookupResult>("/v1/offers", { method: "POST", body: JSON.stringify(need) }),
  createQuote: (body: {
    customer_id: string;
    deliver_by: string;
    need_ref: string;
    lines: { product_id: string; quantity_sales_units: number }[];
  }) => call<Quote>("/v1/quotes", { method: "POST", body: JSON.stringify(body) }),
  quote: (id: string) => call<Quote>(`/v1/quotes/${id}`),
  placeOrder: (quoteId: string, idempotencyKey: string, agentId = "web-console") =>
    call<Order>("/v1/orders", {
      method: "POST",
      body: JSON.stringify({ quote_id: quoteId }),
      headers: { "Idempotency-Key": idempotencyKey, "X-Agent-Id": agentId },
    }),
  orders: () => call<Order[]>("/v1/orders"),
  order: (id: string) => call<Order>(`/v1/orders/${id}`),
  approvals: () => call<ApprovalItem[]>("/v1/approvals"),
  approve: (orderId: string, approver: string, comment: string) =>
    call<Order>(`/v1/approvals/${orderId}/approve`, {
      method: "POST",
      body: JSON.stringify({ approver, comment }),
    }),
  reject: (orderId: string, approver: string, comment: string) =>
    call<Order>(`/v1/approvals/${orderId}/reject`, {
      method: "POST",
      body: JSON.stringify({ approver, comment }),
    }),
  audit: (params?: { order_id?: string; quote_id?: string }) => {
    const q = new URLSearchParams(
      Object.entries(params ?? {}).filter(([, v]) => v) as [string, string][],
    ).toString();
    return call<AuditEntry[]>("/v1/audit" + (q ? `?${q}` : ""));
  },
};
