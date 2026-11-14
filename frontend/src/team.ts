// Types, API calls and mock fixtures for the Team page.
// Shapes are taken from the running gateway (backend/app/accounts.py, guardrail_profiles.py, governance_api.py).
import { ApiError, call } from "./api";

export interface TeamUser {
  id: string;
  tenant_id: string | null;
  email: string;
  role: string;
  status: "invited" | "active" | "disabled" | "removed";
  created_at: string;
  disabled_at: string | null;
  last_sign_in: string | null;
  invited_by: string | null;
}

export interface GuardrailProfile {
  tenant_id: string;
  profile: {
    business_type: string;
    sites: number;
    buyers_and_approvers: number;
    monthly_purchase_volume_band: string;
    meals_per_day_band: string;
    required_claims: string[];
    exclude_allergens: string[];
  };
  preset: string;
  /** Each value names where it came from, e.g. "preset:hotel_chain". */
  guardrails: Record<string, { value: unknown; source: string }>;
  updated_at: string;
}

export interface Organisation {
  id: string;
  name: string;
  created_at: string;
  disabled_at: string | null;
  paused_by: string | null;
  paused_at: string | null;
  pause_reason: string | null;
  guardrail_profile: GuardrailProfile | null;
  customer_ids?: string[];
}

export interface Session {
  /** null for the operator token: it is Valio staff without a personal account. */
  user: TeamUser | null;
  role: string;
  tenant_id: string | null;
  tenant: Organisation | null;
  customer_ids: string[];
  permissions: string[];
}

export interface RoleInfo {
  role: string;
  permissions: string[];
}

/** An invite or reset link: the gateway returns it once and sends no e-mail. */
export interface OnceLink {
  user_id: string;
  expires_at: string;
  link: string;
  token: string;
}

interface Option {
  value: string;
  label: string;
}
export interface SignupOptions {
  business_types: Option[];
  monthly_purchase_volume_bands: Option[];
  meals_per_day_bands: Option[];
}

/** First answer to a pause or resume: nothing has changed yet, resubmit the same body with the token. */
export interface PausePreview {
  confirmation_required: true;
  confirmation_token: string;
  expires_at: string;
}

const send = <T>(method: string, path: string, body: unknown = {}) => call<T>(path, { method, body: JSON.stringify(body) });

/** Valio staff manage staff under /v1/operator/users; an organisation's admin manages its own people under /v1/tenant/users. */
export const peoplePath = (staff: boolean) => (staff ? "/v1/operator/users" : "/v1/tenant/users");
export const orgPeoplePath = (orgId: string) => `/v1/operator/tenants/${encodeURIComponent(orgId)}/users`;

export const team = {
  session: () => call<Session>("/v1/auth/session"),
  roles: () => call<RoleInfo[]>("/v1/tenant/roles"),
  options: () => call<SignupOptions>("/v1/auth/signup/options"),
  users: (base: string) => call<TeamUser[]>(base),
  /** `base` is a people path, or orgPeoplePath(id) to invite into an organisation as Valio staff. */
  invite: (base: string, email: string, role: string) => send<OnceLink>("POST", base, { email, role }),
  change: (base: string, id: string, change: { role?: string; disabled?: boolean }) =>
    send<TeamUser>("PATCH", `${base}/${encodeURIComponent(id)}`, change),
  link: (base: string, id: string, kind: "resend-invite" | "password-reset") =>
    send<OnceLink>("POST", `${base}/${encodeURIComponent(id)}/${kind}`),
  organisations: () => call<Organisation[]>("/v1/operator/tenants"),
  createOrganisation: (name: string, customer_ids: string[]) =>
    send<Organisation>("POST", "/v1/operator/tenants", { name, customer_ids }),
  setPaused: (id: string, paused: boolean, reason: string, confirmation_token?: string) =>
    send<PausePreview | { tenant_id: string; paused: boolean; agent_ids: string[] }>(
      "POST",
      `/v1/operator/tenants/${encodeURIComponent(id)}/${paused ? "pause" : "resume"}`,
      { reason, confirmation_token },
    ),
};

const ROLE_LABELS: Record<string, string> = {
  platform_admin: "Valio staff",
  tenant_admin: "Admin",
  approver: "Approver",
  viewer: "Viewer",
  member: "Member",
};
const sentence = (code: string) => code.charAt(0).toUpperCase() + code.slice(1).replace(/[_:]/g, " ");
export const roleLabel = (role: string) => ROLE_LABELS[role] ?? sentence(role);

const PERMISSIONS: Record<string, string> = {
  "orders:read": "See orders",
  "evidence:read": "See the evidence behind each order",
  "audit:read": "Read the audit trail",
  "spend:read": "See spending",
  "suggestions:read": "See reorder suggestions",
  "approvals:decide": "Approve or reject orders",
  "orders:cancel": "Cancel orders",
  "users:manage": "Invite people, change their role and disable them",
  "agents:manage": "Add agents and switch them off",
  "connections:manage": "Create and revoke connection keys",
  "mandates:manage": "Set how much each agent may spend",
  "policy:manage": "Change the purchasing rules",
  "agents:manage:own": "Add their own agents",
  "connections:manage:own": "Connect their own agents",
  "policy:manage:own": "Set rules for their own agents",
  "activity:read:own": "See what their own agents did",
  "platform:*": "Work across every organisation on the gateway",
  "tenants:manage": "Create, pause and resume organisations",
};
export const permissionLabel = (code: string) => PERMISSIONS[code] ?? sentence(code);

const ERRORS: Record<string, string> = {
  LAST_TENANT_ADMIN: "An organisation needs at least one admin. Make someone else an admin first.",
  EMAIL_EXISTS: "That e-mail address already has an account.",
  CUSTOMER_ASSIGNED: "One of those customer numbers already belongs to another organisation.",
  CONFIRMATION_INVALID: "That confirmation ran out or was already used. Start again.",
  TENANT_NOT_FOUND: "That organisation no longer exists or is closed.",
  USER_NOT_FOUND: "That person is no longer on this team.",
  ALREADY_ACTIVE: "That person has already accepted the invite. Create a reset link instead.",
};
/** The gateway's own message unless the page has a plainer sentence for the code. */
export const plainError = (e: unknown) => (e instanceof ApiError && ERRORS[e.code] ? new Error(ERRORS[e.code]) : e);

// ---- mock fixtures (VITE_MOCK=1) ----

const ago = (mins: number) => new Date(Date.now() - mins * 60000).toISOString();
const sourced = (values: Record<string, unknown>, source: string) =>
  Object.fromEntries(Object.entries(values).map(([k, value]) => [k, { value, source }]));

const USERS: TeamUser[] = [
  { id: "valio-admin", tenant_id: null, email: "staff@valio.example", role: "platform_admin", status: "active", created_at: ago(60 * 24 * 30), disabled_at: null, last_sign_in: ago(42), invited_by: null },
  { id: "usr_mock1", tenant_id: null, email: "onboarding@valio.example", role: "platform_admin", status: "invited", created_at: ago(60 * 5), disabled_at: null, last_sign_in: null, invited_by: "valio-admin" },
];
const ORGS: Organisation[] = [
  {
    id: "hotel-aurora", name: "Hotel Aurora chain", created_at: ago(60 * 24 * 30), disabled_at: null, paused_by: null, paused_at: null, pause_reason: null, customer_ids: ["1"],
    guardrail_profile: {
      tenant_id: "hotel-aurora", preset: "hotel_chain", updated_at: ago(60 * 24 * 30),
      profile: { business_type: "hotel_chain", sites: 12, buyers_and_approvers: 8, monthly_purchase_volume_band: "25–100k", meals_per_day_band: "500–2000", required_claims: [], exclude_allergens: [] },
      guardrails: sourced({ per_order_ceiling_eur: 5000, approver_amount_threshold_eur: 2500, four_eyes: true, monthly_budget_eur: 100000 }, "preset:hotel_chain"),
    },
  },
  { id: "kaari-hospital", name: "Kaari Hospital Kitchen", created_at: ago(60 * 24 * 30), disabled_at: null, paused_by: null, paused_at: null, pause_reason: null, guardrail_profile: null, customer_ids: ["2"] },
];
const ROLES: RoleInfo[] = [
  { role: "member", permissions: ["agents:manage:own", "connections:manage:own", "policy:manage:own", "activity:read:own"] },
  { role: "viewer", permissions: ["orders:read", "evidence:read", "audit:read", "spend:read", "suggestions:read"] },
  { role: "approver", permissions: ["orders:read", "evidence:read", "audit:read", "spend:read", "suggestions:read", "approvals:decide", "orders:cancel"] },
  { role: "tenant_admin", permissions: ["orders:read", "evidence:read", "audit:read", "spend:read", "suggestions:read", "approvals:decide", "orders:cancel", "users:manage", "agents:manage", "connections:manage", "mandates:manage", "policy:manage"] },
];
const mockLink = (user_id: string, kind: "invites" | "password-resets"): OnceLink => ({
  user_id, token: "mock-single-use-token", link: `http://localhost:8000/v1/auth/${kind}/mock-single-use-token/accept`,
  expires_at: new Date(Date.now() + (kind === "invites" ? 7 : 1) * 86400000).toISOString(),
});

/** Fixture answer for this page's endpoints in mock mode; undefined means "not mine". */
export function mockTeam(method: string, path: string, body: unknown): unknown {
  const b = (body ?? {}) as { email?: string; role?: string; disabled?: boolean; name?: string; customer_ids?: string[]; reason?: string; confirmation_token?: string };
  let m: RegExpMatchArray | null;
  if (path === "/v1/auth/session")
    return { user: null, role: "platform_admin", tenant_id: null, tenant: null, customer_ids: ["1"], permissions: ["platform:*", "tenants:manage", "users:manage", "connections:manage", "approvals:decide"] } satisfies Session;
  if (path === "/v1/tenant/roles") return ROLES;
  if (path === "/v1/auth/signup/options")
    return {
      business_types: [{ value: "single_restaurant", label: "Single restaurant" }, { value: "restaurant_group", label: "Restaurant group" }, { value: "hotel_chain", label: "Hotel chain" }, { value: "hospital_or_school_kitchen", label: "Hospital or school kitchen" }, { value: "catering", label: "Catering" }],
      monthly_purchase_volume_bands: [{ value: "<5k", label: "Under €5,000" }, { value: "5–25k", label: "€5,000–25,000" }, { value: "25–100k", label: "€25,000–100,000" }, { value: ">100k", label: "Over €100,000" }],
      meals_per_day_bands: [{ value: "<100", label: "Under 100" }, { value: "100–500", label: "100–500" }, { value: "500–2000", label: "500–2,000" }, { value: ">2000", label: "Over 2,000" }],
    } satisfies SignupOptions;

  if ((m = path.match(/^\/v1\/(?:operator|tenant)\/users(?:\/([^/]+))?(?:\/(resend-invite|password-reset))?$/))) {
    if (!m[1]) {
      if (method === "GET") return USERS;
      const user: TeamUser = { id: `usr_mock${USERS.length}`, tenant_id: null, email: b.email ?? "", role: b.role ?? "viewer", status: "invited", created_at: ago(0), disabled_at: null, last_sign_in: null, invited_by: "legacy-operator" };
      USERS.push(user);
      return mockLink(user.id, "invites");
    }
    const user = USERS.find((u) => u.id === m![1]);
    if (!user) return undefined;
    if (m[2]) return mockLink(user.id, m[2] === "resend-invite" ? "invites" : "password-resets");
    if (method === "PATCH") {
      if (b.role) user.role = b.role;
      if (b.disabled !== undefined) {
        user.disabled_at = b.disabled ? ago(0) : null;
        user.status = b.disabled ? "disabled" : user.last_sign_in ? "active" : "invited";
      }
    }
    return user;
  }

  if (path === "/v1/operator/tenants") {
    if (method === "GET") return ORGS;
    const org: Organisation = { id: `ten_mock${ORGS.length}`, name: b.name ?? "", created_at: ago(0), disabled_at: null, paused_by: null, paused_at: null, pause_reason: null, guardrail_profile: null, customer_ids: b.customer_ids ?? [] };
    ORGS.push(org);
    return { id: org.id, name: org.name, customer_ids: org.customer_ids };
  }
  if ((m = path.match(/^\/v1\/operator\/tenants\/([^/]+)\/(users|pause|resume)$/))) {
    const org = ORGS.find((o) => o.id === decodeURIComponent(m![1]));
    if (!org) return undefined;
    if (m[2] === "users") return mockLink("usr_mock_org", "invites");
    if (!b.confirmation_token)
      return { confirmation_required: true, confirmation_token: "mock-confirmation", expires_at: new Date(Date.now() + 300000).toISOString() } satisfies PausePreview;
    const paused = m[2] === "pause";
    Object.assign(org, { paused_by: paused ? "legacy-operator" : null, paused_at: paused ? ago(0) : null, pause_reason: paused ? b.reason ?? null : null });
    return { tenant_id: org.id, paused, agent_ids: [] };
  }
  return undefined;
}
