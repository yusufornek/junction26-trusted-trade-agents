// Types, API calls and mock fixtures for the Supply watch page.
// Shapes are taken from real gateway responses (backend/app/brief_scenarios_watch.py, brief_scenarios.py trade_off()).
import { api, call, hasSession, type Account, type Quantity } from "./api";

export type TradeOff = {
  /** Cost of the required quantity with the substitute minus with the original; null without price evidence. */
  price_difference_per_required_quantity: number | null;
  required_quantity: Quantity;
  currency: string;
  /** Amount per sales unit, in the required quantity's unit. */
  pack_change: { from: number | null; to: number | null };
  unit_change: { from: string; to: string };
  allergen_differences: { removed: string[]; added: string[] };
  claim_differences: { removed: string[]; added: string[] };
  delivery_date_difference: { from: string | null; to: string | null; days: number | null };
  explanation: string;
};

export type Substitute = { product_id: string; requires_approval: boolean; trade_off: TradeOff };

export type StockoutAlert = {
  id: string;
  code: "STOCKOUT_PREDICTED";
  customer_id: string;
  agent_id: string;
  /** "recurring:<id>:<date>", "quote:<id>" or "basket:<id>". */
  source: string;
  product_id: string;
  order_date: string;
  predicted_at: string;
  substitute: Substitute | null;
  alternatives: Substitute[];
  requires_approval: boolean;
  substitute_unavailable_reason: string | null;
};

export type WatchRun = { checked: number; alerts: StockoutAlert[]; errors?: { source: string; code: string }[]; busy?: boolean };

export const watchApi = {
  /** The alerts plus the catalogue name of every product they mention (the inbox itself carries only ids). */
  inbox: async () => {
    const alerts = await call<StockoutAlert[]>("/v1/stockout-watch/inbox");
    const ids = [...new Set(alerts.flatMap((a) => [a.product_id, ...a.alternatives.map((s) => s.product_id)]))];
    const names: Record<string, string> = {};
    await Promise.all(ids.map((id) => api.product(id).then((p) => (names[id] = p.name_en), () => undefined)));
    return { alerts, names };
  },
  run: () => call<WatchRun>("/v1/stockout-watch/run", { method: "POST", body: "{}" }),
  /** Only the operator token and Valio staff (platform admin) may start a check; tenant accounts get OPERATOR_REQUIRED. */
  canRun: async () => !hasSession() || (await call<Account>("/v1/auth/session")).role === "platform_admin",
};

const day = (d: number) => new Date(Date.now() + d * 86400000).toISOString().slice(0, 10);

const ALERTS: StockoutAlert[] = [
  {
    id: "mock-stockout-1",
    code: "STOCKOUT_PREDICTED",
    customer_id: "1",
    agent_id: "hotel-chain-agent",
    source: "recurring:mock-recurring-1:" + day(3),
    product_id: "466104",
    order_date: day(3),
    predicted_at: new Date(Date.now() - 4 * 60000).toISOString(),
    substitute: null,
    alternatives: [],
    requires_approval: true,
    substitute_unavailable_reason: null,
  },
  {
    id: "mock-stockout-2",
    code: "STOCKOUT_PREDICTED",
    customer_id: "1",
    agent_id: "web-console",
    source: "basket:mock-basket-1",
    product_id: "465036",
    order_date: day(2),
    predicted_at: new Date(Date.now() - 50 * 60000).toISOString(),
    substitute: null,
    alternatives: [],
    requires_approval: true,
    substitute_unavailable_reason: "recorded functional need required",
  },
];
ALERTS[0].substitute = {
  product_id: "464308",
  requires_approval: true,
  trade_off: {
    price_difference_per_required_quantity: -18.4,
    required_quantity: { value: 200, unit: "L" },
    currency: "EUR",
    pack_change: { from: 1.5, to: 1 },
    unit_change: { from: "ST", to: "ST" },
    allergen_differences: { removed: [], added: [] },
    claim_differences: { removed: [], added: [] },
    delivery_date_difference: { from: day(4), to: day(3), days: -1 },
    explanation:
      "Valio Hyvä suomalainen Arki® Eila® semi skimmed milk drink 1 l lactose free meets the recorded function and constraints; compare price, pack and delivery evidence before approving.",
  },
};
ALERTS[0].alternatives = [ALERTS[0].substitute];

/** Fixture answer for this page's endpoints in mock mode; undefined means "not mine". */
export function mockWatch(method: string, path: string, _body: unknown): unknown {
  if (method === "GET" && path === "/v1/stockout-watch/inbox") return ALERTS;
  if (method === "POST" && path === "/v1/stockout-watch/run") return { checked: 12, alerts: [], errors: [] };
  return undefined;
}
