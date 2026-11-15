import { auth, call, hasSession } from "./api";

/** Notification types a connection may receive (spec 61 R-1), in the gateway's order, with plain words. */
export const NOTIFICATION_TYPES = [
  ["approval_needed", "An order needs approval"],
  ["order_decided", "An order was approved or rejected"],
  ["stockout_predicted", "A product is likely to run out"],
  ["anomaly", "An unusual order"],
  ["proposal_received", "A new product suggestion"],
  ["suggestion_due", "A repeat order is due"],
  ["agent_paused", "The agent was paused"],
  ["policy_changed", "The buying rules changed"],
  ["delivery_issue", "A delivery problem"],
] as const;
export type NotificationConsent = Record<(typeof NOTIFICATION_TYPES)[number][0], boolean>;
/** The gateway's defaults: approval and order decisions on, the rest off. */
export const DEFAULT_CONSENT = Object.fromEntries(
  NOTIFICATION_TYPES.map(([k]) => [k, k === "approval_needed" || k === "order_decided"]),
) as NotificationConsent;

export type NotificationPreferences = {
  consent: NotificationConsent;
  quiet_hours: { start: string; end: string; timezone: string } | null;
  rate_per_minute: number;
  channels?: { channel: string; status: string }[];
};

/** One observed place the credential was used from (spec 62 decisions: network peer, transport, client). */
export type TrustContext = { id: string; network: string; protocol: string; client: string; first_seen: string; last_seen: string };
export type TrustState = "ACTIVE" | "READ_ONLY" | "REAUTH_REQUIRED" | "PAUSED" | "REVOKED" | "EXPIRED" | string;
export type Trust = {
  tier: "verified" | "unverified" | string;
  state: TrustState;
  confirmed: boolean;
  /** 0/1 from SQLite. */
  order_granted: number | boolean;
  confirmed_context: string | null;
  context_history: TrustContext[];
};

export type CheckStatus = "passed" | "failed" | "not_attempted";
export type DrivingStatus = {
  result: { payload: { checks: Record<string, CheckStatus>; iat?: number; exp?: number; environment?: string }; signature: string } | null;
  valid: boolean;
  required_checks: string[];
  override: boolean;
  enabled_operations: string[];
  negotiation: { protocol_revision: string | null; capabilities: Record<string, { status: "declared" | "proven" }>; fallback_removed: string[] };
};

/** GET /v1/connect/connections/{id}/health: the parts this page reads beyond the base health (spec 60 R-8, 61 R-6, 62 R-6, 63 R-8). */
export type TrustHealth = {
  notifications?: { sent: number; delivered: number; read: number; failed: number };
  trust?: Trust;
  connection_contract?: DrivingStatus | null;
};

/** Owner inbox of lifecycle notices (spec 62 R-6). */
export type LifecycleNotice = { id: number; connection_id: string; event: string; created_at: string; resolved_at: string | null };

export const STATE_WORDS: Record<string, string> = {
  ACTIVE: "Active",
  READ_ONLY: "Read-only: the key was used from two places at once",
  REAUTH_REQUIRED: "Sign-in must be renewed: reconnect from the agent's app",
  PAUSED: "Paused: the agent is paused",
  REVOKED: "Revoked",
  EXPIRED: "Expired",
};

export const NOTICE_WORDS: Record<string, string> = {
  CONCURRENT_CONTEXT: "Key used from two places at once. Confirm which one is yours.",
  CONNECTION_CONTINUITY_REQUIRED: "Used from a new place. Confirm it is the same agent.",
  CONNECTION_REAUTH_REQUIRED: "Sign-in must be renewed. Reconnect from the agent's app.",
};

export const CHECK_WORDS: Record<string, string> = {
  unit: "Reads units right (cartons versus cases)",
  retry: "Retries a dropped answer without ordering twice",
  approval: "Waits when an order needs approval",
  expired_quote: "Refreshes an expired quote",
  unknown_argument: "Stops on an argument the gateway does not know",
  elicitation: "Answers a question from the gateway",
};

export const CAPABILITY_WORDS: Record<string, string> = {
  elicitation: "Asks the person questions inside the agent",
  sampling: "Lets the gateway ask its model",
  roots: "Shares workspace folders",
  notifications: "Receives notifications",
  resources: "Reads resources",
};

/** Fields GET /v1/oauth/requests/{id} adds since spec 61/62; absent on an older gateway. */
export type ConsentExtras = {
  client_tier?: "verified" | "unverified" | string;
  /** "verified", "not verified" or "not verified — not <publisher>" when the name resembles a known publisher. */
  verification_label?: string;
  registered_at?: string | null;
  notifications?: NotificationConsent;
};

const conn = (id: string) => `/v1/connect/connections/${encodeURIComponent(id)}`;
// Notification preferences have no /v1/connect/ route; the plain one serves the operator token and the tenant session.
const notifyPath = (id: string) => `/v1/connections/${encodeURIComponent(id)}/notifications`;

export const trustApi = {
  inbox: () => call<{ notifications: LifecycleNotice[] }>("/v1/connect/connection-notifications").then((r) => r.notifications),
  confirm: (id: string, contextId?: string) =>
    call<Trust>(`${conn(id)}/trust/confirm`, { method: "POST", body: JSON.stringify({ confirmed: true, context_id: contextId ?? null }) }),
  allowOrders: (id: string) => call<Trust>(`${conn(id)}/trust/order-scope`, { method: "POST" }),
  notifications: (id: string) => call<NotificationPreferences>(notifyPath(id)),
  saveNotifications: (id: string, { consent, quiet_hours, rate_per_minute }: NotificationPreferences) =>
    // The model forbids unknown fields, so the read-only channel list stays out of the body.
    call<NotificationPreferences>(notifyPath(id), { method: "PUT", body: JSON.stringify({ consent, quiet_hours, rate_per_minute }) }),
  /** Operator consent with the notification choices (field `notifications`, spec 61 R-1). redirect_url carries a
   *  one-time code: navigate to it once, never store or log it. */
  approveConsent: (id: string, body: { agent_id: string; customer_ids: string[]; notifications?: NotificationConsent }) =>
    call<{ status: "approved" | "denied"; redirect_url: string }>(`/v1/oauth/requests/${encodeURIComponent(id)}/decision`, {
      method: "POST",
      body: JSON.stringify({ decision: "approve", ...body }),
    }),
  /** The operator token has no account; it may do everything a tenant admin may. */
  role: () => (hasSession() ? auth.session().then((a) => a.role) : Promise.resolve("operator")),
};
