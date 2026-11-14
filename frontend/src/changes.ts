// Types, API calls and mock fixtures for the Changes page.
// Shapes are taken from real gateway responses (backend/app/governance.py, governance_api.py; spec 44).
import { ApiError, call } from "./api";
import { eur, ruleLabel } from "./ui";

export type Layer = "platform" | "tenant" | "agent";
export type Direction = "unchanged" | "tightening" | "loosening" | "mixed";

export type PolicyData = {
  allowed_customers: string[] | null;
  allowed_operations: ("lookup" | "quote" | "order")[] | null;
  per_order_ceiling_eur: number | null;
  daily_budget_eur: number | null;
  monthly_budget_eur: number | null;
  allowed_categories: string[] | null;
  blocked_categories: string[];
  required_claims: string[];
  excluded_allergens: string[];
  substitutes_allowed: boolean;
  ordering_hours: number[] | null;
  delivery_day_window: { min_days: number | null; max_days: number | null };
  autonomy_level: "approval_required" | "autonomous_within_limits";
  approval_routing: { approver_user_ids: string[] | null; tenant_admin_above_eur: number | null };
};
/** What the page's editor sends; omitted fields keep their value, null clears the agent's own limit. */
export type PolicyPatch = Partial<Pick<PolicyData, "per_order_ceiling_eur" | "daily_budget_eur" | "monthly_budget_eur" | "autonomy_level">>;

/** The register row has more (connections, budget use, counts); the page needs only who the agent is. */
export type AgentRow = { agent_id: string; tenant_id: string; owner_user_id: string | null; status: "active" | "paused" | "disabled" };

export type PolicySource = { source_layer: Layer; version_id: string; author: string; value: unknown };
/** One effective value, the layer that decided it, and what every layer says. Nested fields use dotted names. */
export type PolicyField = PolicySource & { sources: PolicySource[] };
export type EffectivePolicy = { agent_id: string; tenant_id: string; version_id: string; fields: Record<string, PolicyField> };

export type PolicyVersion = {
  id: string;
  layer: Layer;
  target: string | null;
  data: PolicyData;
  author: string;
  created_at: string;
  reason: string;
  diff: Record<string, { before: unknown; after: unknown; direction: Direction }>;
};

/** Replay of the agent's stored quotes and orders of the last 30 days under a proposed policy. */
export type Simulation = {
  passed: number;
  needed_approval: number;
  refused: number;
  quotes: number;
  orders: number;
  window_start: string;
  window_end: string;
  examples: { kind: "quote" | "order"; id: string; outcome: "passed" | "needed_approval" | "refused"; rules: string[] }[];
};

export type ChangeRequest = {
  id: string;
  agent_id: string;
  requester: string;
  base_version: string;
  status: "pending" | "approved" | "rejected";
  /** The whole proposed agent policy, not a patch. */
  policy: PolicyData;
  reason: string;
  created_at: string;
  classification: Record<string, Direction>;
  simulation: Simulation;
  rollback_version: string | null;
  version_id?: string;
  decided_by?: string;
  decided_at?: string;
  decision_reason?: string;
};

/** Every governance mutation answers this first; repeating the call with the token applies it. */
export type Preview = { confirmation_required: true; confirmation_token: string; expires_at: string; summary: unknown };
export type ChangeResult = { status: "applied"; version: PolicyVersion } | { status: "pending"; change_request: ChangeRequest };
export type Outcome = Preview | ChangeResult | ChangeRequest;

const json = (method: string, body: unknown): RequestInit => ({ method, body: JSON.stringify(body) });
const policyPath = (id: string) => `/v1/tenant/agents/${encodeURIComponent(id)}/policy`;

export const changesApi = {
  agents: () => call<AgentRow[]>("/v1/tenant/agents"),
  effective: (id: string) => call<EffectivePolicy>(`${policyPath(id)}/effective`),
  versions: (id: string) => call<PolicyVersion[]>(`${policyPath(id)}/versions`),
  requests: () => call<ChangeRequest[]>("/v1/tenant/policy/change-requests"),
  simulate: (id: string, policy: PolicyPatch) => call<Simulation>(`${policyPath(id)}/simulate`, json("POST", { policy })),
  change: (id: string, policy: PolicyPatch, reason: string, confirmation_token?: string) =>
    call<Preview | ChangeResult>(policyPath(id), json("PUT", { policy, reason, confirmation_token }), false),
  rollback: (id: string, versionId: string, reason: string, confirmation_token?: string) =>
    call<Preview | ChangeResult>(`${policyPath(id)}/versions/${encodeURIComponent(versionId)}/rollback`, json("POST", { reason, confirmation_token }), false),
  decide: (requestId: string, kind: "approve" | "reject", reason: string, confirmation_token?: string) =>
    call<Preview | ChangeRequest>(`/v1/tenant/policy/change-requests/${encodeURIComponent(requestId)}/${kind}`, json("POST", { reason, confirmation_token }), false),
};

/** A policy in the dotted field names the effective response, diffs and classifications use. */
export function flat(p: PolicyData): Record<string, unknown> {
  const { delivery_day_window: d, approval_routing: r, ...rest } = p;
  return {
    ...rest,
    "delivery_day_window.min_days": d.min_days,
    "delivery_day_window.max_days": d.max_days,
    "approval_routing.approver_user_ids": r.approver_user_ids,
    "approval_routing.tenant_admin_above_eur": r.tenant_admin_above_eur,
  };
}

export const LAYER_WORDS: Record<Layer, string> = {
  platform: "set by Valio for everyone",
  tenant: "set by this organisation",
  agent: "set for this agent",
};

/** Field → [what a person calls it, what "nobody set this" means]. Going over a money limit asks a person; it does not refuse. */
const FIELDS: Record<string, [string, string]> = {
  per_order_ceiling_eur: ["Largest order it may confirm alone", "No limit"],
  daily_budget_eur: ["Spend per day before a person must approve", "No limit"],
  monthly_budget_eur: ["Spend per month before a person must approve", "No limit"],
  autonomy_level: ["Autonomy", ""],
  allowed_customers: ["Customers it may buy for", "Any customer"],
  allowed_operations: ["What it may do", "Search, quote and order"],
  allowed_categories: ["Product categories it may buy", "Any category"],
  blocked_categories: ["Product categories it may not buy", "None"],
  required_claims: ["Claims every product must carry", "None"],
  excluded_allergens: ["Allergens it must avoid", "None"],
  substitutes_allowed: ["Substitute products", ""],
  ordering_hours: ["Hours it may order (UTC)", "Any hour"],
  "delivery_day_window.min_days": ["Earliest delivery, days after ordering", "No limit"],
  "delivery_day_window.max_days": ["Latest delivery, days after ordering", "No limit"],
  "approval_routing.approver_user_ids": ["People who may approve its orders", "Any approver"],
  "approval_routing.tenant_admin_above_eur": ["Orders above this need an administrator", "No such rule"],
};
export const FIELD_ORDER = Object.keys(FIELDS);
export const fieldLabel = (field: string) => FIELDS[field]?.[0] ?? ruleLabel(field);
/** Null and the empty list mean "no restriction here", which is not the same as zero or nothing. */
export const isUnset = (v: unknown) => v == null || (Array.isArray(v) && !v.length);

export function showValue(field: string, v: unknown): string {
  if (v == null) return FIELDS[field]?.[1] || "Not set";
  if (Array.isArray(v)) {
    // An empty allowlist permits nothing; an empty block or requirement list asks for nothing.
    if (!v.length) return /^(blocked|required|excluded)/.test(field) ? "None" : "Nothing allowed";
    return v.map((x) => (x === "lookup" ? "search" : String(x))).join(", ");
  }
  if (typeof v === "boolean") return v ? "Allowed" : "Not allowed";
  if (field === "autonomy_level") return v === "approval_required" ? "Every order needs a person's approval" : "Confirms orders alone, within its limits";
  return field.endsWith("_eur") && typeof v === "number" ? eur(v) : String(v);
}

/** The gateway's governance refusals as sentences; null means the message it sent is already fine to show. */
export function plainError(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null;
  const d = error.data as { field?: string; limit?: unknown; source_layer?: Layer; author?: string } | undefined;
  switch (error.code) {
    case "OUTSIDE_GUARDRAIL":
      return d?.field && d.source_layer
        ? `Not sent. "${fieldLabel(d.field)}" cannot go past ${showValue(d.field, d.limit)}: that limit is ${LAYER_WORDS[d.source_layer]} (by ${d.author}), and an agent's own limits have to stay inside it.`
        : "Not sent. The change goes past a limit that is set above this agent.";
    case "SELF_APPROVAL_FORBIDDEN":
      return "The gateway refused: you asked for this change yourself, so you cannot approve or reject it. A second administrator has to decide it.";
    case "POLICY_CHANGED":
      return "The agent's policy has changed since this was proposed, so it cannot be approved as it stands. Propose the change again with a fresh check.";
    case "CONFIRMATION_INVALID":
      return "The confirmation ran out or was already used. Nothing changed; start the action again.";
    case "TENANT_ADMIN_REQUIRED":
      return "Only an administrator of this organisation can do this.";
    case "INVALID_TRANSITION":
      return "This request has already been decided.";
    default:
      return null;
  }
}

// ---- mock fixtures (VITE_MOCK=1) ----

const ago = (min: number) => new Date(Date.now() - min * 60000).toISOString();
const DEFAULTS: PolicyData = {
  allowed_customers: null, allowed_operations: null, per_order_ceiling_eur: null, daily_budget_eur: null, monthly_budget_eur: null,
  allowed_categories: null, blocked_categories: [], required_claims: [], excluded_allergens: [], substitutes_allowed: true,
  ordering_hours: null, delivery_day_window: { min_days: null, max_days: null }, autonomy_level: "autonomous_within_limits",
  approval_routing: { approver_user_ids: null, tenant_admin_above_eur: null },
};
const version = (id: string, layer: Layer, author: string, data: Partial<PolicyData>, minutesAgo: number,
  reason = "Existing authority at governance adoption", diff: PolicyVersion["diff"] = {}): PolicyVersion =>
  ({ id, layer, target: null, author, data: { ...DEFAULTS, ...data }, created_at: ago(minutesAgo), reason, diff });

const mockAgents: AgentRow[] = ["hotel-chain-agent", "web-console"].map((agent_id) =>
  ({ agent_id, tenant_id: "hotel-aurora", owner_user_id: "aurora-director", status: "active" }));
const mockOuter = [
  version("base_platform", "platform", "valio-admin", { per_order_ceiling_eur: 1500 }, 9000),
  version("base_tenant", "tenant", "aurora-director", { allowed_customers: ["1"], daily_budget_eur: 4000 }, 9000),
];
const mockVersions: Record<string, PolicyVersion[]> = {
  "hotel-chain-agent": [
    version("pol_3f9a1c20", "agent", "aurora-director", { per_order_ceiling_eur: 800, daily_budget_eur: 2500 }, 2900, "Start small while the agent earns trust", {
      per_order_ceiling_eur: { before: null, after: 800, direction: "tightening" },
      daily_budget_eur: { before: null, after: 2500, direction: "tightening" },
    }),
    version("base_hotel", "agent", "aurora-director", {}, 9000),
  ],
};
// The sibling fixture for /v1/tenant/agents may name other agents, so any id gets a starting version.
const versionsOf = (id: string) => (mockVersions[id] ??= [version("base_" + id, "agent", "aurora-director", {}, 9000)]);

function mockSimulation(p: PolicyData): Simulation {
  const passed = p.autonomy_level === "approval_required" ? 0 : Math.round((Math.min(p.per_order_ceiling_eur ?? 1500, 1500) / 1500) * 16);
  return {
    passed, needed_approval: 20 - passed, refused: 0, quotes: 15, orders: 5, window_start: ago(43200), window_end: ago(0),
    examples: passed < 16 ? [{ kind: "order", id: "ff5a4e94-d8f6-45c1-b0fa-a60432c61716", outcome: "needed_approval", rules: ["per_order_ceiling_eur", "QUANTITY_ANOMALY"] }] : [],
  };
}

let mockRequests: ChangeRequest[] = [{
  id: "pcr_demo1", agent_id: "hotel-chain-agent", requester: "aurora-buyer", base_version: "pol_3f9a1c20", status: "pending",
  policy: { ...DEFAULTS, per_order_ceiling_eur: 1200, daily_budget_eur: 2500 }, reason: "Weekend banquets need larger dairy orders",
  created_at: ago(95), classification: { per_order_ceiling_eur: "loosening", daily_budget_eur: "unchanged" },
  simulation: mockSimulation({ ...DEFAULTS, per_order_ceiling_eur: 1200 }), rollback_version: null,
}];

function mockEffective(id: string): EffectivePolicy {
  const layers = [...mockOuter, versionsOf(id)[0]];
  const fields: EffectivePolicy["fields"] = {};
  for (const key of FIELD_ORDER) {
    const sources = layers.map((v) => ({ source_layer: v.layer, version_id: v.id, author: v.author, value: flat(v.data)[key] }));
    // Fixture rule: the lowest number wins, then the first layer that lists anything, else the platform default.
    const numbers = sources.filter((s) => typeof s.value === "number");
    const winner = numbers.length
      ? numbers.reduce((a, b) => ((b.value as number) < (a.value as number) ? b : a))
      : (sources.find((s) => s.value === "approval_required") ?? sources.find((s) => Array.isArray(s.value) && s.value.length) ?? sources[0]);
    fields[key] = { ...winner, sources };
  }
  return { agent_id: id, tenant_id: "hotel-aurora", version_id: "eff_mock", fields };
}

/** Field-by-field direction of a proposal, refusing what an outer layer forbids (as the gateway does before confirming). */
function mockDiff(id: string, proposed: PolicyData): PolicyVersion["diff"] {
  const before = flat(versionsOf(id)[0].data);
  const diff: PolicyVersion["diff"] = {};
  for (const [key, after] of Object.entries(flat(proposed))) {
    const was = before[key];
    if (JSON.stringify(after) === JSON.stringify(was)) continue;
    const outer = mockOuter.find((v) => typeof flat(v.data)[key] === "number" && typeof after === "number" && after > (flat(v.data)[key] as number));
    if (outer) {
      const data = { field: key, limit: flat(outer.data)[key], source_layer: outer.layer, version_id: outer.id, author: outer.author };
      throw new ApiError("OUTSIDE_GUARDRAIL", "Proposed policy exceeds a governing limit", 422, data as never);
    }
    const looser = typeof was === "number" ? after == null || (after as number) > was : after === "autonomous_within_limits";
    diff[key] = { before: was, after, direction: looser ? "loosening" : "tightening" };
  }
  return diff;
}

/** Fixture answer for this page's endpoints in mock mode; undefined means "not mine". */
export function mockChanges(method: string, path: string, body: unknown): unknown {
  const sent = (body ?? {}) as { policy?: PolicyPatch; reason?: string; confirmation_token?: string };
  const preview = (): Preview => ({ confirmation_required: true, confirmation_token: "mock-confirmation", expires_at: ago(-5), summary: {} });
  let m: RegExpMatchArray | null;
  if (path === "/v1/tenant/agents" && method === "GET") return mockAgents;
  if (path === "/v1/tenant/policy/change-requests" && method === "GET") return [...mockRequests];
  if ((m = path.match(/^\/v1\/tenant\/policy\/change-requests\/([^/]+)\/(approve|reject)$/))) {
    const found = mockRequests.find((r) => r.id === m![1]);
    if (!found) throw new ApiError("POLICY_REQUEST_NOT_FOUND", "Change request was not found", 404);
    if (found.requester === "legacy-operator") throw new ApiError("SELF_APPROVAL_FORBIDDEN", "A requester cannot decide its own loosening request", 403);
    if (found.status !== "pending") throw new ApiError("INVALID_TRANSITION", "Change request is already decided", 409);
    if (!sent.confirmation_token) return preview();
    const decided: ChangeRequest = { ...found, status: m[2] === "approve" ? "approved" : "rejected", decided_by: "legacy-operator", decided_at: ago(0), decision_reason: sent.reason };
    if (decided.status === "approved") {
      const applied = version("pol_" + Date.now().toString(16), "agent", "legacy-operator", found.policy, 0, found.reason, mockDiff(found.agent_id, found.policy));
      versionsOf(found.agent_id).unshift(applied);
      decided.version_id = applied.id;
    }
    mockRequests = mockRequests.map((r) => (r === found ? decided : r));
    return decided;
  }
  if (!(m = path.match(/^\/v1\/tenant\/agents\/([^/]+)\/policy(\/.*)?$/))) return undefined;
  const id = decodeURIComponent(m[1]);
  const rest = m[2] ?? "";
  const current = versionsOf(id)[0];
  if (rest === "/effective") return mockEffective(id);
  if (rest === "/versions") return [...versionsOf(id)];
  if (rest === "/simulate") return mockSimulation({ ...current.data, ...sent.policy });
  const back = rest.match(/^\/versions\/([^/]+)\/rollback$/);
  if (!back && (rest !== "" || method === "GET")) return undefined;
  const target = back ? versionsOf(id).find((v) => v.id === back[1]) : null;
  if (back && !target) throw new ApiError("POLICY_VERSION_NOT_FOUND", "Policy version was not found", 404);
  const proposed = target ? target.data : { ...current.data, ...sent.policy };
  const diff = mockDiff(id, proposed);
  if (!sent.confirmation_token) return preview();
  const reason = sent.reason ?? "";
  if (Object.values(diff).some((d) => d.direction === "loosening")) {
    const classification = Object.fromEntries(Object.entries(diff).map(([key, d]) => [key, d.direction]));
    const change_request: ChangeRequest = { id: "pcr_" + Date.now().toString(16), agent_id: id, requester: "legacy-operator", base_version: current.id,
      status: "pending", policy: proposed, reason, created_at: ago(0), classification, simulation: mockSimulation(proposed), rollback_version: back ? back[1] : null };
    mockRequests = [change_request, ...mockRequests];
    return { status: "pending", change_request } satisfies ChangeResult;
  }
  const applied = version("pol_" + Date.now().toString(16), "agent", "legacy-operator", proposed, 0, reason, diff);
  versionsOf(id).unshift(applied);
  return { status: "applied", version: applied } satisfies ChangeResult;
}
