// Types, API calls and mock fixtures for the Governance page.
import { ApiError, call, type AutonomyLevel } from "./api";
import { mockChanges } from "./changes";

export type AgentConnection = {
  connection_id: string;
  client: string;
  type: string;
  audience: string;
  revoked_at: string | null;
  last_used_at: string | null;
};

/** One row of GET /v1/tenant/agents. A tenant-wide stop reports every agent as "paused" with no paused_at of its own. */
export type GovernedAgent = {
  agent_id: string;
  tenant_id: string;
  name: string;
  owner_user_id: string;
  status: "active" | "paused" | "disabled";
  paused_by: string | null;
  paused_at: string | null;
  pause_reason: string | null;
  connections: AgentConnection[];
  autonomy_level: AutonomyLevel;
  budget_use: { day_eur: number; month_eur: number };
  orders: number;
  approvals_asked: number;
  approvals_granted: number;
  refusals_by_code: Record<string, number>;
  last_activity: string | null;
};

/** null means the policy sets no limit for that period. */
export type BudgetLimits = { day_eur: number | null; month_eur: number | null };

export type Activity = { id: number; ts: string; actor: string; action: string; order_id: string | null; summary: string };

export type Alert = {
  id: string;
  type: string;
  agent_id: string;
  tenant_id: string;
  created_at: string;
  period?: "day" | "month";
  used_eur?: number;
  limit_eur?: number;
  count?: number;
  window_minutes?: number;
  client?: string;
  address?: string;
};

/** GET /v1/operator/tenants: only the platform operator may read it. */
export type Tenant = { id: string; name: string; paused_by: string | null; paused_at: string | null; pause_reason: string | null };

export type ReportAgent = {
  agent_id: string;
  owner_user_id: string;
  spend_eur: number;
  orders: unknown[];
  approvals: unknown[];
  refusals: unknown[];
  pauses: unknown[];
  changes: unknown[];
};
export type SignedReport = {
  report: { tenant_id: string | null; from: string; to: string; agents: ReportAgent[] };
  signature: string;
  jwks_url: string;
};

/** First answer to every governance change: nothing is applied until the same call is repeated with the token. */
export type Confirmation = { confirmation_required: true; confirmation_token: string; expires_at: string };
export const isConfirmation = (r: unknown): r is Confirmation =>
  typeof r === "object" && r !== null && "confirmation_required" in r;

const agentPath = (id: string) => `/v1/tenant/agents/${encodeURIComponent(id)}`;
const change = (path: string, reason: string, token?: string) =>
  call<unknown>(path, { method: "POST", body: JSON.stringify({ reason, ...(token ? { confirmation_token: token } : {}) }) });

export const governanceApi = {
  agents: () => call<GovernedAgent[]>("/v1/tenant/agents"),
  alerts: () => call<Alert[]>("/v1/tenant/alerts"),
  tenants: () => call<Tenant[]>("/v1/operator/tenants"),
  activity: (id: string) => call<Activity[]>(`${agentPath(id)}/activity`),
  /** The register reports spend only; the limits come from the agent's effective policy. */
  limits: async (id: string): Promise<BudgetLimits> => {
    const { fields } = await call<{ fields: Record<string, { value: unknown } | undefined> }>(`${agentPath(id)}/policy/effective`);
    const limit = (key: string) => (typeof fields[key]?.value === "number" ? (fields[key]!.value as number) : null);
    return { day_eur: limit("daily_budget_eur"), month_eur: limit("monthly_budget_eur") };
  },
  setAgentPaused: (id: string, paused: boolean, reason: string, token?: string) =>
    change(`${agentPath(id)}/${paused ? "pause" : "resume"}`, reason, token),
  /** The operator has no tenant of its own, so it stops a named tenant through the operator route
   *  (the tenant route answers 500 for it); a tenant admin stops its own tenant. */
  setAllPaused: (operatorTenantId: string | null, paused: boolean, reason: string, token?: string) =>
    change(
      (operatorTenantId ? `/v1/operator/tenants/${encodeURIComponent(operatorTenantId)}` : "/v1/tenant/agents") +
        (paused ? "/pause" : "/resume"),
      reason,
      token,
    ),
  /** Always the signed form: the summary on screen and the downloaded file are then the same document. */
  report: (from: string, to: string) =>
    call<SignedReport>(`/v1/tenant/governance/report?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}&signed=true`),
};

export type Board = {
  agents: GovernedAgent[];
  alerts: Alert[];
  /** null for everyone but the operator. */
  tenants: Tenant[] | null;
  limits: Record<string, BudgetLimits | null>;
};

export async function loadBoard(): Promise<Board> {
  const [agents, alerts, tenants] = await Promise.all([
    governanceApi.agents(),
    governanceApi.alerts(),
    // Refused for tenant users; the page then works on the caller's own tenant.
    governanceApi.tenants().catch(() => null),
  ]);
  const limits = await Promise.all(agents.map((a) => governanceApi.limits(a.agent_id).catch(() => null)));
  return { agents, alerts, tenants, limits: Object.fromEntries(agents.map((a, i) => [a.agent_id, limits[i]])) };
}

// ---- mock fixtures ----

const ago = (min: number) => new Date(Date.now() - min * 60000).toISOString();

const mockAgent = (agent_id: string, name: string, over: Partial<GovernedAgent>): GovernedAgent => ({
  agent_id, name, tenant_id: "hotel-aurora", owner_user_id: "aurora-director", status: "active",
  paused_by: null, paused_at: null, pause_reason: null, connections: [], autonomy_level: "autonomous_within_limits",
  budget_use: { day_eur: 0, month_eur: 0 }, orders: 0, approvals_asked: 0, approvals_granted: 0,
  refusals_by_code: {}, last_activity: null, ...over,
});

const mockAgents: GovernedAgent[] = [
  mockAgent("hotel-chain-agent", "Hotel chain purchasing agent", {
    budget_use: { day_eur: 1259.69, month_eur: 8420.5 }, orders: 14, approvals_asked: 4, approvals_granted: 3,
    refusals_by_code: { POLICY_REJECTED: 2, OUTSIDE_ORDERING_HOURS: 5 }, last_activity: ago(6),
    connections: [{ connection_id: "con_1", client: "claude_desktop", type: "mcp", audience: "buyer", revoked_at: null, last_used_at: ago(6) }],
  }),
  mockAgent("web-console", "web-console", {
    budget_use: { day_eur: 310, month_eur: 1904.2 }, orders: 5, approvals_asked: 1, approvals_granted: 1, last_activity: ago(95),
  }),
  mockAgent("night-restock-agent", "Night restock agent", {
    owner_user_id: "aurora-kitchen-lead", status: "paused", paused_by: "aurora-director", paused_at: ago(180),
    pause_reason: "Ordered outside kitchen hours twice; paused until its limits are reviewed",
    autonomy_level: "approval_required", budget_use: { day_eur: 0, month_eur: 2260 }, orders: 6, approvals_asked: 6,
    approvals_granted: 5, refusals_by_code: { HUMAN_REJECTED: 1 }, last_activity: ago(180),
    connections: [{ connection_id: "con_2", client: "chatgpt", type: "mcp", audience: "buyer", revoked_at: null, last_used_at: ago(200) }],
  }),
];
const MOCK_LIMITS: Record<string, [number | null, number | null]> = {
  "hotel-chain-agent": [1500, 10000],
  "night-restock-agent": [800, 9000],
};
let mockStopped = false;

const mockAlert = (type: string, agent_id: string, min: number, data: Partial<Alert>): Alert => ({
  id: `${type}:${agent_id}`, type, agent_id, tenant_id: "hotel-aurora", created_at: ago(min), ...data,
});
const mockAlerts: Alert[] = [
  mockAlert("budget.80", "hotel-chain-agent", 6, { period: "day", used_eur: 1259.69, limit_eur: 1500 }),
  mockAlert("refusal.burst", "hotel-chain-agent", 48, { count: 5, window_minutes: 10 }),
  mockAlert("activity.outside_hours", "night-restock-agent", 185, {}),
  mockAlert("connection.new_client", "night-restock-agent", 200, { client: "chatgpt" }),
];

const mockActivity = (a: GovernedAgent): Activity[] => [
  ...(a.paused_at
    ? [{ id: 9, ts: a.paused_at, actor: a.paused_by ?? "system", action: "GOVERNANCE_AGENT_PAUSED", order_id: null, summary: a.pause_reason ?? "" }]
    : []),
  { id: 8, ts: ago(200), actor: "agent:" + a.agent_id, action: "PLACE_ORDER", order_id: null, summary: "Order placed within the agent's limits and confirmed." },
  { id: 7, ts: ago(202), actor: "agent:" + a.agent_id, action: "CREATE_QUOTE", order_id: null, summary: "Fixed-price quote created for 2 lines." },
  { id: 6, ts: ago(204), actor: "agent:" + a.agent_id, action: "LOOKUP", order_id: null, summary: "Priced 16 candidates, returned 10 offers and excluded 2294 hard-filter failures." },
];

const mockReport = (): SignedReport => ({
  report: {
    tenant_id: "hotel-aurora", from: ago(7 * 1440), to: ago(0),
    agents: mockAgents.map((a) => ({
      agent_id: a.agent_id, owner_user_id: a.owner_user_id, spend_eur: a.budget_use.month_eur,
      orders: Array(a.orders).fill({}), approvals: Array(a.approvals_granted).fill({}),
      refusals: Array(Object.values(a.refusals_by_code).reduce((n, c) => n + c, 0)).fill({}),
      pauses: a.paused_at ? [{}] : [], changes: [],
    })),
  },
  signature: "eyJhbGciOiJFZERTQSIsImtpZCI6Im1vY2sifQ..bW9jay1zaWduYXR1cmU",
  jwks_url: "/.well-known/jwks.json",
});

/** Fixture answer for this page's endpoints in mock mode; undefined means "not mine". */
export function mockGovernance(method: string, path: string, body: unknown): unknown {
  const sent = (body ?? {}) as { reason?: string; confirmation_token?: string };
  // Same two steps as the gateway: the first call only hands out a token.
  const confirmed = (apply: () => unknown) =>
    sent.confirmation_token ? apply() : { confirmation_required: true, confirmation_token: "mock-confirmation", expires_at: ago(-5) };
  let m: RegExpMatchArray | null;

  if (path === "/v1/tenant/agents" && method === "GET")
    return mockAgents.map((a) => (mockStopped && a.status === "active" ? { ...a, status: "paused" } : a));
  if (path === "/v1/tenant/alerts") return mockAlerts;
  if (path === "/v1/tenant/governance/report") return mockReport();
  if ((m = path.match(/^\/v1\/(?:tenant\/agents|operator\/tenants\/[^/]+)\/(pause|resume)$/)) && method === "POST") {
    const paused = m[1] === "pause";
    return confirmed(() => {
      mockStopped = paused;
      return { tenant_id: "hotel-aurora", paused, agent_ids: mockAgents.map((a) => a.agent_id) };
    });
  }
  if ((m = path.match(/^\/v1\/tenant\/agents\/([^/]+)\/(activity|pause|resume|policy\/effective)$/))) {
    const [, id, what] = m;
    const found = mockAgents.find((a) => a.agent_id === id);
    if (!found) throw new ApiError("AGENT_NOT_FOUND", "Agent was not found", 404);
    if (what === "activity") return mockActivity(found);
    if (what === "policy/effective") {
      const [day, month] = MOCK_LIMITS[id] ?? [null, null];
      // The effective policy belongs to the policy page: its fixture wins once it exists.
      return mockChanges(method, path, body) ?? { fields: { daily_budget_eur: { value: day }, monthly_budget_eur: { value: month } } };
    }
    const paused = what === "pause";
    return confirmed(() => {
      Object.assign(found, {
        status: paused ? "paused" : "active", paused_by: paused ? "aurora-director" : null,
        paused_at: paused ? ago(0) : null, pause_reason: paused ? (sent.reason ?? null) : null,
      });
      return { agent_id: id, status: found.status, paused_by: found.paused_by, paused_at: found.paused_at };
    });
  }
  return undefined;
}
