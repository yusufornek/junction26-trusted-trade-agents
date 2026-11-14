// Types, API calls and mock fixtures for the Basket page (spec 32: recipe → one priced basket → quote → order).
import { MOCK, call, type LiveCalls, type OfferCard, type PolicyResult, type Quantity, type Quote, type Warning } from "./api";

export type Ingredient = {
  name: string;
  /** Units the gateway accepts: KG, L, G, ML. */
  quantity: Quantity;
  /** Catalog search words; without them the gateway searches by the ingredient's name. */
  search_terms?: string[] | null;
};

export type Manifest = {
  version: 1;
  recipe_name: string;
  /** Portions the quantities are written for; the request's own `servings` is the target they are scaled to. */
  servings: number;
  ingredients: Ingredient[];
};

export type BasketRequest = { customer_id: string; deliver_by: string; servings: number; manifest: Manifest };

export type IngredientCoverage = {
  ingredient_index: number;
  name: string;
  /** The scaled quantity the recipe needs. */
  requested: Quantity;
  /** Share of it that arrives on time, 0–1. */
  covered: number;
  product_id: string | null;
};

/** The response also carries every ingredient's ranked alternatives (`ingredients[].offers`); the page shows the chosen basket only. */
export type BasketLookup = {
  basket_lookup_id: string;
  customer_id: string;
  deliver_by: string;
  scaled_manifest: Manifest;
  basket: {
    /** One line per distinct product: ingredients met by the same product share a line. */
    lines: OfferCard[];
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

// Recorded from the local gateway on 2026-11-14: salmon soup written for 10 portions, scaled to 80.
const MOCK_LINES = [
  line("471555", "Kalavapriikki Salmon fillet C, 2 pcs frozen", "KG", 1, "KG", 12, 16.65, 0, 0.93, [
    { code: "VARIABLE_WEIGHT", text: "Final price follows the delivered weight." },
  ]),
  line("475402", "SPL potato peeled 10kg", "ST", 10, "KG", 2, 8.17, 4, 0.95, [
    { code: "CUTOFF_PASSED", text: "Order cut-off for delivery on 2026-11-16 passed at 2026-11-12T15:00:00+02:00" },
    UNKNOWN_STOCK,
  ]),
  line("469162", "Valio cooking cream alternative 15 % 1 l lactose free", "ST", 1, "L", 4, 1.79, 0, 0.95, [UNKNOWN_STOCK]),
  line("464411", "Valio butter 500 g unsalted", "ST", 0.5, "KG", 2, 3.3, 0.2, 1, [UNKNOWN_STOCK]),
  line("460354", "LEEK 1.CL FR/NL", "KG", 1, "KG", 3, 1.66, 0.6, 1, [UNKNOWN_STOCK]),
  line("463024", "GREENS 1KG DILL", "BAG", 1, "KG", 1, 4.71, 0.6, 0.82, [
    UNKNOWN_STOCK,
    { code: "SIZE_UNCONFIRMED", text: "Catalog or name pack content is not confirmed by a live price ratio." },
  ]),
];

const MOCK_NEEDS: [string, number, string][] = [
  ["Salmon fillet", 12, "KG"], ["Potatoes", 16, "KG"], ["Cooking cream", 4, "L"],
  ["Butter", 0.8, "KG"], ["Leek", 2.4, "KG"], ["Dill", 0.4, "KG"],
];

const MOCK_BASKET: BasketLookup = {
  basket_lookup_id: "a6c5d8a2-2e29-4548-999e-2536fc76cfc2",
  customer_id: "1",
  deliver_by: "2026-11-18",
  scaled_manifest: {
    version: 1, recipe_name: "Salmon soup", servings: 80,
    ingredients: MOCK_NEEDS.map(([name, value, unit]) => ({ name, quantity: { value, unit } })),
  },
  basket: {
    lines: MOCK_LINES,
    total: 239.59,
    coverage_by_ingredient: MOCK_NEEDS.map(([name, value, unit], i) => ({
      ingredient_index: i, name, requested: { value, unit }, covered: 1, product_id: MOCK_LINES[i].product_id,
    })),
    uncovered: [],
    policy_preview: {
      outcome: "PENDING_APPROVAL",
      hits: [
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
