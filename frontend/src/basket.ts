// Types, API calls and mock fixtures for the Basket page (spec 32: recipe → one priced basket → quote → order).
import { MOCK, call, type LiveCalls, type OfferCard, type PolicyResult, type Quantity, type Quote, type Warning } from "./api";

export type Ingredient = {
  name: string;
  /** Units the gateway accepts: KG, L, G, ML. */
  quantity: Quantity;
  /** Catalog search words; without them the gateway searches by the ingredient's name. */
  search_terms?: string[] | null;
  /** Physical stock in the kitchen (spec 46 R-1); it does not scale with portions. The gateway buys only the rest. */
  on_hand?: Quantity | null;
  /** What the ingredient does in the recipe (spec 31); without it the gateway offers no alternatives for a late line. */
  function?: { product_type: string; intended_use: string; preparation?: string | null; acceptable_units: string[] } | null;
  /** The product the kitchen usually buys; any other product is then a substitute that needs approval. */
  substitute_for?: { product_id: string } | null;
  substitutions_allowed?: boolean;
};

export type Manifest = {
  version: 1;
  recipe_name: string;
  /** Portions the quantities are written for; the request's own `servings` is the target they are scaled to. */
  servings: number;
  /** The ingredient the menu is built around: kept first and never swapped without approval. */
  focus_ingredient?: string | null;
  ingredients: Ingredient[];
};

export type BasketRequest = { customer_id: string; deliver_by: string; servings: number; manifest: Manifest };

export type IngredientCoverage = {
  ingredient_index: number;
  name: string;
  /** The scaled quantity the recipe needs. */
  requested: Quantity;
  /** Needed, in the kitchen and to buy, in KG or L (spec 46 R-1); absent on baskets priced before it. */
  needed?: Quantity | null;
  on_hand?: Quantity | null;
  to_buy?: Quantity | null;
  /** Share of it that arrives on time, 0–1. */
  covered: number;
  /** null when nothing is bought: no product fits, or the kitchen's own stock covers it. */
  product_id: string | null;
};

/** What changes for the kitchen if it takes this product instead (spec 46 R-3). Every field may be null without evidence. */
export type TradeOff = {
  /** Positive: costs more for the required quantity. */
  price_difference_per_required_quantity: number | null;
  required_quantity: Quantity;
  currency: string;
  /** Pack content in the required quantity's unit. */
  pack_change: { from: number | null; to: number | null };
  /** Sales units, e.g. KI (case) to ST (piece). */
  unit_change: { from: string | null; to: string | null };
  allergen_differences: { removed: string[]; added: string[] };
  claim_differences: { removed: string[]; added: string[] };
  delivery_date_difference: { from: string | null; to: string | null; days: number | null };
  explanation: string;
};

export type LateLine = {
  /** null when no supplier evidence establishes a later date; the gateway does not guess one. */
  earliest_possible_date: string | null;
  in_time_alternatives: { product_id: string; requires_approval: boolean; trade_off: TradeOff }[];
  reason: string;
};

export type BasketLine = OfferCard & {
  /** Set when the request has a deliver-by date; false means this product cannot arrive by then. */
  arrives_in_time?: boolean;
  cannot_source_in_time?: LateLine | null;
  /** This product replaces the one the kitchen usually buys (`substitution.for`). */
  substitution?: { for: string; differences: string[] } | null;
  trade_off?: TradeOff | null;
};

export type BasketLookup = {
  basket_lookup_id: string;
  customer_id: string;
  deliver_by: string;
  scaled_manifest: Manifest;
  /** Every ingredient's ranked candidates; the page reads only product names from them (alternatives, the usual product). */
  ingredients?: { ingredient_index: number; name: string; offers: BasketLine[]; chosen_line: string | null }[];
  basket: {
    /** One line per distinct product: ingredients met by the same product share a line. */
    lines: BasketLine[];
    total: number;
    coverage_by_ingredient: IngredientCoverage[];
    uncovered: { ingredient_index: number; name: string; reason: string }[];
    policy_preview: PolicyResult;
  };
  live_calls: LiveCalls;
  decision_calls: number;
};

export const priceBasket = (body: BasketRequest) =>
  call<BasketLookup>("/v1/basket-lookups", { method: "POST", body: JSON.stringify(body) });

export const getBasket = (id: string) => call<BasketLookup>(`/v1/basket-lookups/${encodeURIComponent(id)}`);

/** The gateway rebuilds the lines from the stored basket and re-checks price and stock, so the client cannot alter them. */
export async function quoteBasket(b: BasketLookup): Promise<Quote> {
  const quote = await call<Quote>("/v1/quotes", {
    method: "POST",
    body: JSON.stringify({
      customer_id: b.customer_id,
      deliver_by: b.deliver_by,
      basket_ref: { basket_lookup_id: b.basket_lookup_id },
    }),
  });
  // shortcut: mock.ts's quote handler ignores basket_ref and stores an empty quote; filling that same object lets the
  // mock order carry the basket. Replace with a basket_ref branch in mock.ts when its owner is free.
  if (MOCK) {
    const lines = b.basket.lines.map((l) => ({ ...l, quantity_sales_units: l.units_needed ?? 0 }));
    Object.assign(quote, { lines, total: b.basket.total, warnings: lines.flatMap((l) => l.warnings) });
  }
  return quote;
}

const UNKNOWN_STOCK: Warning = {
  code: "AVAILABILITY_UNCONFIRMED",
  text: "Valio does not know this product's stock; its ordering logic accepts any quantity",
};

/** A basket line as the gateway returns it; everything the page does not show takes a neutral value. */
function line(
  product_id: string, name: string, sales_unit: string, size: number, size_unit: string, units: number,
  unit_price: number, overbuy: number, fit: number, warnings: Warning[],
): OfferCard {
  const total = Math.round(units * unit_price * 100) / 100;
  return {
    product_id, name, sales_unit, size_per_sales_unit: size, size_unit, size_source: "catalog", units_needed: units,
    unit_price, unit_price_with_tax: null, price_per_l_or_kg: unit_price / size, total, purchase_total: total,
    overbuy: { amount: overbuy, unit: size_unit }, currency: "EUR", valid_from: null, valid_to: null,
    net_price_found: false, availability: null, warnings, delivery_reliability: null, co2_kg_per_kg: null, labels: [],
    matched_because: [], fit, fit_question: null, provenance: null, score: 0, rank_label: null,
  };
}

const tradeOff = (
  price: number | null, required: Quantity, pack: [number, number], units: [string, string],
  claims: { removed: string[]; added: string[] }, dates: [string, string, number], explanation: string,
): TradeOff => ({
  price_difference_per_required_quantity: price, required_quantity: required, currency: "EUR",
  pack_change: { from: pack[0], to: pack[1] }, unit_change: { from: units[0], to: units[1] },
  allergen_differences: { removed: [], added: [] }, claim_differences: claims,
  delivery_date_difference: { from: dates[0], to: dates[1], days: dates[2] }, explanation,
});

// Recorded from the local gateway on 2026-11-14 (deliver by 2026-11-16): salmon soup for 10 portions scaled to 80,
// 6 kg potatoes and 2 kg butter on hand, the kitchen's usual cream 464223 missed its cut-off. The dill delay is staged
// so mock mode also shows a late line with an in-time alternative (live, every in-time product was chosen directly).
const USUAL_CREAM: BasketLine = {
  ...line("464223", "Valio Hyvä suomalainen Arki® cooking cream 10 % 2 dl UHT HYLA®", "KI", 3, "L", 2, 10.8, 2, 1, [
    { code: "CUTOFF_PASSED", text: "Order cut-off for delivery on 2026-11-16 passed at 2026-11-12T15:00:00+02:00" },
  ]),
  arrives_in_time: false,
  cannot_source_in_time: { earliest_possible_date: null, in_time_alternatives: [], reason: "cut-off missed; next supplier date unknown" },
};
const SMALL_DILL: BasketLine = {
  ...line("468087", "CROWN DILL 500G", "ST", 0.25, "KG", 2, 2.8, 0.1, 0.95, [UNKNOWN_STOCK]),
  arrives_in_time: true,
};

const MOCK_LINES: BasketLine[] = [
  { ...line("471555", "Kalavapriikki Salmon fillet C, 2 pcs frozen", "KG", 1, "KG", 12, 16.65, 0, 0.89, [
    { code: "VARIABLE_WEIGHT", text: "Final price follows the delivered weight." },
  ]), arrives_in_time: true },
  { ...line("475402", "SPL potato peeled 10kg", "ST", 10, "KG", 1, 8.17, 0, 0.88, [
    { code: "CUTOFF_PASSED", text: "Order cut-off for delivery on 2026-11-16 passed at 2026-11-12T15:00:00+02:00" },
    UNKNOWN_STOCK,
  ]), arrives_in_time: false,
  cannot_source_in_time: { earliest_possible_date: null, in_time_alternatives: [], reason: "cut-off missed; next supplier date unknown" } },
  { ...line("464249", "Valio cooking cream 15 % 1 l UHT lactose free", "ST", 1, "L", 4, 2.63, 0, 1, [UNKNOWN_STOCK]),
    arrives_in_time: true,
    substitution: { for: "464223", differences: ["claims absent from substitute: LOW_LACTOSE", "pack size: 3.0 → 1.0 L"] },
    trade_off: tradeOff(-0.24, { value: 4, unit: "L" }, [3, 1], ["KI", "ST"],
      { removed: ["LOW_LACTOSE"], added: ["FREE_FROM_LACTOSE"] }, ["2026-11-16", "2026-11-16", 0],
      "Valio cooking cream 15 % 1 l UHT lactose free meets the recorded function and constraints; compare price, pack and delivery evidence before approving.") },
  { ...line("460354", "LEEK 1.CL FR/NL", "KG", 1, "KG", 3, 1.66, 0.6, 1, [UNKNOWN_STOCK]), arrives_in_time: true },
  { ...line("463024", "GREENS 1KG DILL", "BAG", 1, "KG", 1, 4.71, 0.6, 0.97, [
    UNKNOWN_STOCK,
    { code: "SIZE_UNCONFIRMED", text: "Catalog or name pack content is not confirmed by a live price ratio." },
  ]), arrives_in_time: false,
  cannot_source_in_time: { earliest_possible_date: "2026-11-17", reason: "insufficient stock by deadline", in_time_alternatives: [
    { product_id: "468087", requires_approval: true, trade_off: tradeOff(0.89, { value: 0.4, unit: "KG" }, [1, 0.25], ["BAG", "ST"],
      { removed: [], added: [] }, ["2026-11-17", "2026-11-16", -1],
      "CROWN DILL 500G meets the recorded function and constraints; compare price, pack and delivery evidence before approving.") },
  ] } },
];

// [name, needed, unit, on hand, product]; butter is covered by the kitchen's own stock, so nothing is bought for it.
const MOCK_NEEDS: [string, number, string, number, string | null][] = [
  ["Salmon fillet", 12, "KG", 0, "471555"], ["Potatoes", 16, "KG", 6, "475402"], ["Cooking cream", 4, "L", 0, "464249"],
  ["Butter", 0.8, "KG", 2, null], ["Leek", 2.4, "KG", 0, "460354"], ["Dill", 0.4, "KG", 0, "463024"],
];

const MOCK_BASKET: BasketLookup = {
  basket_lookup_id: "a6c5d8a2-2e29-4548-999e-2536fc76cfc2",
  customer_id: "1",
  deliver_by: "2026-11-16",
  scaled_manifest: {
    version: 1, recipe_name: "Salmon soup", servings: 80, focus_ingredient: "Salmon fillet",
    ingredients: MOCK_NEEDS.map(([name, value, unit]) => ({ name, quantity: { value, unit } })),
  },
  ingredients: [
    { ingredient_index: 2, name: "Cooking cream", offers: [MOCK_LINES[2], USUAL_CREAM], chosen_line: "464249" },
    { ingredient_index: 5, name: "Dill", offers: [MOCK_LINES[4], SMALL_DILL], chosen_line: "463024" },
  ],
  basket: {
    lines: MOCK_LINES,
    total: 228.18,
    coverage_by_ingredient: MOCK_NEEDS.map(([name, value, unit, hand, product_id], i) => ({
      ingredient_index: i, name, requested: { value, unit }, covered: 1, product_id,
      needed: { value, unit }, on_hand: { value: hand, unit }, to_buy: { value: Math.max(0, value - hand), unit },
    })),
    uncovered: [],
    policy_preview: {
      outcome: "PENDING_APPROVAL",
      hits: [
        { rule: "SUBSTITUTION_REQUIRED", detail: "464249: substitutes require human approval" },
        { rule: "CUTOFF_PASSED", detail: "475402: Order cut-off for delivery on 2026-11-16 passed at 2026-11-12T15:00:00+02:00" },
        { rule: "SIZE_UNCONFIRMED", detail: "463024: Catalog or name pack content is not confirmed by a live price ratio." },
      ],
    },
  },
  live_calls: { price: 1, availability: 1 },
  decision_calls: 6,
};

/** Fixture answer for this page's endpoints in mock mode; undefined means "not mine". */
export function mockBasket(method: string, path: string, body: unknown): unknown {
  if (path === "/v1/basket-lookups" && method === "POST") {
    // The fixture's lines stay salmon soup; only the heading follows what was asked.
    const asked = body as Partial<BasketRequest>;
    return {
      ...MOCK_BASKET,
      deliver_by: asked.deliver_by ?? MOCK_BASKET.deliver_by,
      scaled_manifest: {
        ...MOCK_BASKET.scaled_manifest,
        recipe_name: asked.manifest?.recipe_name ?? MOCK_BASKET.scaled_manifest.recipe_name,
        servings: asked.servings ?? MOCK_BASKET.scaled_manifest.servings,
      },
    };
  }
  if (method === "GET" && path.startsWith("/v1/basket-lookups/")) return MOCK_BASKET;
  return undefined;
}
