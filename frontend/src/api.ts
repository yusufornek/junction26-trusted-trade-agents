// Types mirror contracts/openapi.yaml (owned by backend). Regenerate by hand when the contract moves.

export type Warning = { code: string; text: string };
export type Provenance = { source: string; fetched_at: string; valid_to: string | null };
export type LiveCalls = { price: number; availability: number };
export type Quantity = { value: number; unit: string };
export type CoveredQuantity = { value: number; unit: "L" | "KG" | "SALES_UNIT" };

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
  gtin?: string | null;
  name: string;
  sales_unit: string;
  allowed_lot_size?: number;
  size_per_sales_unit: number | null;
  size_unit: string | null;
  size_source: string | null;
  size_confirmed_by_price?: boolean;
  units_needed: number | null;
  unit_price: number | null;
  unit_price_with_tax: number | null;
  price_per_l_or_kg: number | null;
  total: number | null;
  /** What the whole packs cost — can exceed `total` when packs overshoot the need. */
  purchase_total?: number | null;
  overbuy?: { amount: number; unit: string } | null;
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
  score_breakdown?: Record<string, number>;
  rank_label: string | null;
};

export type Preferences = {
  brands?: string[];
  max_price_per_unit?: number | null;
  pack_size?: { min?: number | null; max?: number | null } | null;
  labels?: string[];
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
  preferences?: Preferences;
};

export type RuleHit = { rule: string; detail: string };
export type PolicyResult = { outcome: string; hits: RuleHit[] };

export type FulfilmentPlan = {
  kind: "single" | "split" | "partial";
  lines: { product_id: string; units: number; covers: CoveredQuantity }[];
  total: number;
  coverage: CoveredQuantity;
  uncovered: CoveredQuantity;
  warnings?: Warning[];
  /** What the policy would decide if this plan were ordered now — before anything is committed. */
  policy_preview: PolicyResult;
};

export type MandateRecord = { id: string; issuer: string; limits: Record<string, number>; valid_until: string };
export type ConstraintSource = { code: string; source: "request" | "customer_policy" };

export type LookupResult = {
  lookup_id: string;
  agent_id?: string | null;
  mandate?: MandateRecord | null;
  need: NeedSpec;
  effective_constraints?: { required_claims?: ConstraintSource[]; exclude_allergens?: ConstraintSource[] };
  exclusion_counts?: Record<string, number>;
  offers: OfferCard[];
  plans?: FulfilmentPlan[];
  no_match_reason?: string | null;
  excluded_count: number;
  retrieval?: { mode?: "terms" | "intent" | "intent_broadened"; terms_used?: string[] };
  matched_count?: number;
  priced_count?: number;
  screened_count?: number;
  live_calls: LiveCalls;
  decision_calls: number;
  warnings: Warning[];
};

export type QuoteLine = OfferCard & { quantity_sales_units?: number };

/** One history check on a quote or order (spec 45). Only status "anomaly" is a warning; the gateway raises none
 *  with fewer than four past orders. */
export type AnomalyFix = {
  id: string;
  kind: string;
  description: string;
  product_id?: string | null;
  quantity_sales_units?: number | null;
  remainder_sales_units?: number | null;
  /** Set for fixes a buyer admin performs elsewhere (suspend the agent), not for re-quotes. */
  action_url?: string | null;
};
export type Anomaly = {
  code: string;
  status: "normal" | "anomaly" | "insufficient_history";
  product_id?: string | null;
  observed: number | string | null;
  unit?: string | null;
  expected_band?: { lower?: number; upper?: number; median?: number; last_paid?: number } | null;
  history_window?: { start?: string; end?: string; points?: number; weekday?: number | null; categories?: string[] } | null;
  band_source?: "statistical" | "forecast";
  explanation_text?: string | null;
  fixes: AnomalyFix[];
};

/** A second supplier is synthetic: it shows the gateway is not tied to one catalogue. */
export type Supplier = {
  supplier_id: string;
  synthetic: boolean;
  capabilities: { currencies: string[]; pricing: string; stock: string[]; execution: { mode: string; atomicity: string; partial_acceptance: boolean } };
};

export type Quote = {
  id: string;
  anomalies?: Anomaly[];
  signature?: string | null;
  price_commitment?: string;
  agent_id?: string | null;
  customer_id: string;
  deliver_by: string;
  need_ref: string | null;
  plan_ref?: { lookup_id: string; plan_index: number } | null;
  warnings?: Warning[];
  status: "OPEN" | "ACCEPTED" | "EXPIRED" | string;
  created_at: string;
  expires_at: string;
  lines: QuoteLine[];
  total: number;
  currency: string;
  hard_constraint_violations: string[];
  live_calls: LiveCalls;
};

export type OrderStatus = "PENDING_APPROVAL" | "CONFIRMED" | "REJECTED" | "EXPIRED" | "CANCELLED";

export type Order = {
  id: string;
  anomalies?: Anomaly[];
  customer_id?: string | null;
  quote_id: string;
  agent_id: string;
  idempotency_key: string;
  status: OrderStatus;
  created_at: string;
  expires_at: string;
  decided_at?: string | null;
  decided_by_kind?: "agent" | "operator" | "system" | null;
  total: number;
  currency: string;
  lines: QuoteLine[];
  warnings?: Warning[];
  policy: PolicyResult | null;
  live_calls: LiveCalls | null;
  evidence_revision: number;
  approver: string | null;
  comment: string | null;
  cancelled_by?: string | null;
  cancel_reason?: string | null;
  /** Hash of the fixed-price commitment and the signed receipt — the agent can verify both offline. */
  commitment?: string;
  quote_signature_digest?: string | null;
  receipt?: string | null;
  mandate?: MandateRecord | null;
  next_action?: "WAIT_FOR_APPROVAL" | "STOP" | null;
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

export type TimelineStep = {
  hash?: string;
  ts: string;
  actor: string;
  action: string;
  step: "lookup" | "quote" | "order" | "decision" | "cancellation" | "system";
  summary: string;
  data: Record<string, unknown>;
  live_calls?: LiveCalls;
  decision_calls?: number;
};

export type OrderTimeline = {
  order_id: string;
  quote_id: string;
  lookup_id: string | null;
  agent_id: string;
  status: OrderStatus;
  steps: TimelineStep[];
  total_live_calls: LiveCalls;
  total_decision_calls: number;
};

export type Product = {
  id: string;
  gtin?: string | null;
  name_en: string;
  name_fi?: string;
  category?: string | null;
  sales_unit: string;
  base_unit: string;
  allowed_lot_size?: number;
  vendor_name?: string | null;
  brand?: string | null;
  country?: string | null;
  temperature_condition?: number | string | null;
  units?: Record<string, number>;
  claims?: string[];
  allergens_contains?: string[];
  allergen_status?: Record<string, ("contains" | "may_contain" | "undeclared" | "free_from")[]>;
  has_allergen_declaration?: boolean;
  free_from?: string[];
  labels?: string[];
  base_volume_l?: number | null;
  base_net_weight_kg?: number | null;
  sales_volume_l?: number | null;
  sales_net_weight_kg?: number | null;
  variable_unit?: boolean;
  delivery_reliability?: number | null;
  co2_kg_per_kg?: number | null;
};

export type Health = { status: string; catalog_rows: number };

/** Spec 40: every agent starts approval-first and earns autonomy from a measured track record. */
export type AutonomyLevel = "approval_required" | "autonomous_within_limits";

export type AgentMandate = {
  customer_ids: string[];
  max_auto_total_eur?: number | null;
  max_auto_daily_total_eur: number;
  /** Missing means the policy default applies. */
  autonomy?: AutonomyLevel | null;
  note?: string;
};

export type AutonomyPolicy = {
  default?: AutonomyLevel;
  promotion?: {
    min_approved_unchanged?: number;
    max_rejection_share?: number;
    days_since_last_rejection_or_refusal?: number;
  };
};

export type AgentTrackRecord = {
  agent_id: string;
  window_days: number;
  window_start: string;
  window_end: string;
  orders_placed: number;
  approved_unchanged: number;
  rejected: number;
  expired: number;
  evidence_changes: number;
  cancellations: number;
  hard_constraint_refusals: number;
  average_approval_time_seconds: number | null;
  approved_unchanged_share: number;
  rejection_share: number;
  last_rejection_at: string | null;
  last_hard_constraint_refusal_at: string | null;
  /** A recommendation only — a person decides. `reasons` names every unmet threshold. */
  autonomy_recommendation: { eligible: boolean; reasons: string[] };
};

export type AutonomyChangeResult = {
  agent_id: string;
  level: AutonomyLevel;
  reason: string;
  track_record: AgentTrackRecord;
};

export type PolicyResource = {
  approval: {
    max_auto_total_eur: number;
    min_fit_probability: number;
    warnings_need_approval: string[];
    /** Informational codes the policy deliberately does not act on. */
    ignored_warnings?: string[];
  };
  reject: { on_hard_constraint_violation: boolean; on: string[] };
  ranking_weights: { price: number; coverage: number; reliability: number; co2: number; fit: number };
  shortlist: { catalog_pool: number; live_max?: number };
  agents?: Record<string, AgentMandate>;
  autonomy?: AutonomyPolicy;
  customers?: Record<string, { required_claims?: string[]; exclude_allergens?: string[]; note?: string }>;
  updated_at: string | null;
  updated_by: string | null;
  known_warning_codes: string[];
};

/** The writable part of the policy. Sections left out of a PUT keep their current values. */
export type PolicyDocument = Pick<PolicyResource, "approval" | "reject" | "ranking_weights" | "shortlist"> &
  Partial<Pick<PolicyResource, "agents">>;

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
  agents_seen: { actor: string; order_count: number; registered?: boolean; mandate_id?: string | null }[];
  throttles?: number;
  upstream_rate_limits?: number;
};

export type AuditHead = { entries: number; head_hash: string; at: string; jws: string };
export type AuditAnchor = {
  entries: number;
  head_hash: string;
  chain_id: number;
  tx_hash?: string | null;
  block_number?: number | null;
  status: "sent" | "confirmed" | "failed";
  explorer_url?: string | null;
  at: string;
};
export type AuditVerification = {
  valid: boolean;
  entries: number;
  head_hash: string;
  first_break?: number | null;
  checkpoint_match?: boolean | null;
  anchor_match?: boolean | null;
  anchor_block_time?: string | null;
};

/**
 * Connections: an outside client plus the agent identity and key it uses. Proposed contract for the backend
 * (docs/CONNECT-API.md); until the gateway serves it, the console falls back to placeholder keys.
 */
export type ClientKind = "claude_desktop" | "claude_code" | "cursor" | "vscode" | "chatgpt" | "mcp_other" | "rest";

export type Connection = {
  connection_id: string;
  agent_id: string;
  client: ClientKind;
  key_prefix: string;
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
  /** "oauth": approved through the sign-in window (ChatGPT, claude.ai); no key, and key_prefix is just a label. */
  credential_kind?: "api_key" | "oauth";
};

/** The key is returned once, at creation; the gateway keeps only a hash. */
export type NewConnection = Connection & {
  key: string;
  mcp_url: string | null;
  rest_base_url: string;
  mandate: AgentMandate;
};

export type Me = {
  principal: { id: string; kind?: string; role?: string; organisation_id?: string | null };
  customer_ids: string[];
  environment: string;
  rate_limits?: Record<string, unknown>;
};

/** /v1/meta is the gateway's self-description for outside agents (server card / connect kit). */
export type Meta = Record<string, unknown>;

export type Jwks = { keys: { kid?: string; kty: string; crv?: string; alg?: string; use?: string; x?: string }[] };

export class ApiError extends Error {
  constructor(readonly code: string, message: string, readonly status: number, readonly data?: { changes?: string[]; evidence_revision?: number }) {
    super(message);
  }
}

/** Dev-only: `VITE_MOCK=1 npm run dev` serves fixtures so screens can be reviewed without a gateway or token.
 *  The flag is replaced at build time, so production bundles drop the branch and never load mock.ts. */
export const MOCK = import.meta.env.VITE_MOCK === "1";

let token = MOCK ? "mock" : "";
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

async function send(path: string, init?: RequestInit): Promise<{ res: Response; text: string }> {
  const res = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}) },
  });
  if (res.status === 401) {
    setToken("");
    throw new AuthenticationError();
  }
  return { res, text: await res.text() };
}

export async function call<T>(path: string, init?: RequestInit): Promise<T> {
  if (MOCK) return (await import("./mock")).mockCall(path, init) as Promise<T>;
  const { res, text } = await send(path, init);
  let body: any = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    // A crash or a proxy failure answers in plain text ("Internal Server Error"), not {code, message}.
    if (res.ok) throw new ApiError("BAD_RESPONSE", "The gateway sent a response that is not JSON.", res.status);
  }
  if (!res.ok) {
    // The gateway returns {code, message, data} for domain errors.
    const message = body?.message ?? (`Gateway answered ${res.status}` + (text && !body ? `: ${text.slice(0, 120)}` : ""));
    throw new ApiError(body?.code ?? "HTTP_" + res.status, message, res.status, body?.data);
  }
  return body as T;
}

/** For non-JSON documents (the UBL order). Same auth, same error rules. */
async function callText(path: string): Promise<string> {
  if (MOCK) return (await import("./mock")).mockText(path);
  const { res, text } = await send(path);
  if (!res.ok) throw new ApiError("HTTP_" + res.status, `Gateway answered ${res.status}`, res.status);
  return text;
}

export const api = {
  health: () => call<Health>("/health"),
  me: () => call<Me>("/v1/me"),
  meta: () => call<Meta>("/v1/meta"),
  jwks: () => call<Jwks>("/.well-known/jwks.json"),
  offers: (need: NeedSpec) =>
    call<LookupResult>("/v1/offers", {
      method: "POST",
      body: JSON.stringify(need),
    }),
  lookup: (id: string) => call<LookupResult>(`/v1/lookups/${id}`),
  product: (id: string) => call<Product>(`/v1/products/${id}`),
  createQuote: (
    body: {
      customer_id: string;
      deliver_by: string;
      need_ref?: string;
      lines?: { product_id: string; quantity_sales_units: number }[];
      plan_ref?: { lookup_id: string; plan_index: number };
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
  /** Re-quotes with the proposed correction; the source order's pending promise is cancelled. Places nothing. */
  applyAnomalyFix: (quoteId: string, fixId: string) =>
    call<{ source_quote_id: string; fix_id: string; quotes: Quote[] }>(
      `/v1/quotes/${quoteId}/anomaly-fixes/${encodeURIComponent(fixId)}`,
      { method: "POST" },
    ),
  suppliers: () => call<Supplier[]>("/v1/suppliers"),
  order: (id: string) => call<Order>(`/v1/orders/${id}`),
  timeline: (id: string) => call<OrderTimeline>(`/v1/orders/${id}/timeline`),
  cancel: (id: string, cancelled_by: string, reason: string) =>
    call<Order>(`/v1/orders/${id}/cancel`, {
      method: "POST",
      body: JSON.stringify({ cancelled_by, reason }),
    }),
  ubl: (id: string) => callText(`/v1/orders/${id}/ubl`),
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
  trackRecord: (agentId: string, windowDays = 30) =>
    call<AgentTrackRecord>(`/v1/agents/${encodeURIComponent(agentId)}/track-record?window_days=${windowDays}`),
  setAutonomy: (agentId: string, level: AutonomyLevel, reason: string) =>
    call<AutonomyChangeResult>(`/v1/agents/${encodeURIComponent(agentId)}/autonomy`, {
      method: "POST",
      body: JSON.stringify({ level, reason }),
      headers: { "Idempotency-Key": crypto.randomUUID() },
    }),
  connections: () => call<Connection[]>("/v1/connections"),
  createConnection: (body: { agent_id: string; client: ClientKind; customer_ids: string[]; label?: string }) =>
    call<NewConnection>("/v1/connections", {
      method: "POST",
      body: JSON.stringify(body),
      headers: { "Idempotency-Key": crypto.randomUUID() },
    }),
  revokeConnection: (id: string) =>
    call<Connection>(`/v1/connections/${encodeURIComponent(id)}`, { method: "DELETE" }),
  revokeMandate: (jwt: string) =>
    call<Record<string, unknown>>("/v1/mandates/revocations", { method: "POST", body: JSON.stringify({ jwt }) }),
  stats: (since?: string) => call<Stats>("/v1/stats" + (since ? `?since=${encodeURIComponent(since)}` : "")),
  audit: (params?: { order_id?: string; quote_id?: string }) => {
    const q = new URLSearchParams(
      Object.entries(params ?? {}).filter(([, v]) => v) as [string, string][],
    ).toString();
    return call<AuditEntry[]>("/v1/audit" + (q ? `?${q}` : ""));
  },
  /**
   * Whether this origin exposes MCP over streamable HTTP. Only a 404 means "no route": a mounted endpoint
   * answers anything else (401 without an agent key, 400/406 for this bare ping), so the probe stays right
   * the day the gateway starts serving /mcp, without a frontend change.
   */
  mcpExposed: async (): Promise<boolean> => {
    if (MOCK) return false;
    try {
      const res = await fetch("/mcp", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json, text/event-stream" },
        body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "ping" }),
      });
      return res.status !== 404;
    } catch {
      return false;
    }
  },
  auditHead: () => call<AuditHead>("/v1/audit/head"),
  auditAnchors: () => call<AuditAnchor[]>("/v1/audit/anchors"),
  auditVerify: () => call<AuditVerification>("/v1/audit/verify"),
};
