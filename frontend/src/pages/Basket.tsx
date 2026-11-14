import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Plus, X } from "lucide-react";
import { api, type Order, type Quantity, type Quote, type RuleHit, type Warning } from "../api";
import { getBasket, priceBasket, quoteBasket, type BasketLookup, type Ingredient } from "../basket";
import { Card, Countdown, ErrorNote, Spinner, StatusChip, WarningChips, dateOnly, eur, num, pct, ruleLabel } from "../ui";

// The console signs in as the operator of this one customer, as on the single-product page.
const CUSTOMER = "1";

// Search words are set for the example because the catalog names its products differently ("potato", not "Potatoes").
const EXAMPLE: Ingredient[] = [
  { name: "Salmon fillet", search_terms: ["salmon", "fillet"], quantity: { value: 1.5, unit: "KG" } },
  { name: "Potatoes", search_terms: ["potato"], quantity: { value: 2, unit: "KG" } },
  { name: "Cooking cream", search_terms: ["cooking", "cream"], quantity: { value: 0.5, unit: "L" } },
  { name: "Butter", search_terms: ["butter"], quantity: { value: 100, unit: "G" } },
  { name: "Leek", search_terms: ["leek"], quantity: { value: 300, unit: "G" } },
  { name: "Dill", search_terms: ["dill"], quantity: { value: 50, unit: "G" } },
];

const OUTCOME: Record<string, { label: string; style: string }> = {
  CONFIRMED: { label: "Agent may confirm alone", style: "bg-positive-soft text-positive" },
  PENDING_APPROVAL: { label: "Will ask a human", style: "bg-warn-soft text-warn" },
  REJECTED: { label: "Would be rejected", style: "bg-negative-soft text-negative" },
};

const ORDER_TITLE: Record<string, string> = {
  CONFIRMED: "Confirmed without waiting for a person",
  PENDING_APPROVAL: "Held for a human signature",
  REJECTED: "The policy rejected this order",
};

const inDays = (n: number) => new Date(Date.now() + n * 864e5).toISOString().slice(0, 10);
const unit = (u: string | null) => (u ?? "").toLowerCase();
const qty = (q: Quantity) => `${num(q.value, 2)} ${unit(q.unit)}`;
// Several lines can carry the same warning; one chip per kind is enough (and WarningChips keys by code).
const distinct = (warnings: Warning[] = []) => [...new Map(warnings.map((w) => [w.code, w])).values()];

export default function Basket() {
  const [recipe, setRecipe] = useState("Salmon soup");
  const [writtenFor, setWrittenFor] = useState(10);
  const [portions, setPortions] = useState(80);
  const [deliverBy, setDeliverBy] = useState(() => inDays(4));
  const [rows, setRows] = useState<Ingredient[]>(EXAMPLE);
  const [basket, setBasket] = useState<BasketLookup | null>(null);
  const [quote, setQuote] = useState<Quote | null>(null);
  const [order, setOrder] = useState<Order | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [params, setParams] = useSearchParams();
  // The quote and the order render below the basket; without this a phone shows no sign that the button worked.
  const quoteRef = useRef<HTMLDivElement>(null);
  const orderRef = useRef<HTMLDivElement>(null);
  useEffect(() => quoteRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }), [quote]);
  useEffect(() => orderRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" }), [order]);

  const guard = async (stage: string, fn: () => Promise<void>) => {
    setBusy(stage);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  };

  // The gateway keeps every priced basket, so a reload or a shared link reads it back instead of pricing again.
  useEffect(() => {
    const id = params.get("lookup");
    if (id) guard("price", async () => setBasket(await getBasket(id)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const setRow = (i: number, patch: Partial<Ingredient>) =>
    setRows((rs) => rs.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const usable = rows.filter((r) => r.name.trim() && r.quantity.value > 0);
  const ready = recipe.trim() && writtenFor > 0 && portions > 0 && deliverBy && usable.length > 0;

  const price = () =>
    guard("price", async () => {
      setQuote(null);
      setOrder(null);
      const priced = await priceBasket({
        customer_id: CUSTOMER,
        deliver_by: deliverBy,
        servings: portions,
        manifest: {
          version: 1,
          recipe_name: recipe.trim(),
          servings: writtenFor,
          ingredients: usable.map((r) => ({ ...r, name: r.name.trim() })),
        },
      });
      setBasket(priced);
      setParams({ lookup: priced.basket_lookup_id }, { replace: true });
    });

  const lock = () =>
    guard("quote", async () => {
      setOrder(null);
      setQuote(await quoteBasket(basket!));
    });

  const placeOrder = () => guard("order", async () => setOrder(await api.placeOrder(quote!.id, crypto.randomUUID())));

  const plan = basket?.basket;
  const names = new Map((plan?.lines ?? []).map((l) => [l.product_id, l.name]));
  const outcome = OUTCOME[plan?.policy_preview.outcome ?? ""] ?? OUTCOME.PENDING_APPROVAL;
  const calls = basket ? basket.live_calls.price + basket.live_calls.availability : 0;

  return (
    <div className="flex flex-col gap-4">
      <Card className="flex flex-col gap-4">
        <div>
          <h2 className="text-[15px] font-semibold">What is the kitchen cooking?</h2>
          <p className="label">
            A chef's agent sends the recipe and the number of portions. The gateway scales it, picks a product for every
            ingredient and prices one basket.
          </p>
        </div>

        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <label className="flex flex-col gap-1.5">
            <span className="label">Recipe</span>
            <input className="field" value={recipe} onChange={(e) => setRecipe(e.target.value)} />
          </label>
          <label className="flex flex-col gap-1.5">
            <span className="label">Recipe is written for (portions)</span>
            <input className="field" type="number" min={1} value={writtenFor || ""} onChange={(e) => setWrittenFor(Number(e.target.value))} />
          </label>
          <label className="flex flex-col gap-1.5">
            <span className="label">Portions to cook</span>
            <input className="field" type="number" min={1} value={portions || ""} onChange={(e) => setPortions(Number(e.target.value))} />
          </label>
          <label className="flex flex-col gap-1.5">
            <span className="label">Deliver by</span>
            <input className="field" type="date" min={inDays(0)} max={inDays(60)} value={deliverBy} onChange={(e) => setDeliverBy(e.target.value)} />
          </label>
        </div>

        <div className="flex flex-col gap-2">
          <span className="label">Ingredients{writtenFor > 0 ? ` for ${num(writtenFor)} portions` : ""}</span>
          {rows.map((r, i) => (
            // On a phone the name takes the first row; amount, unit and remove share the second.
            <div key={i} className="flex flex-wrap items-center gap-2">
              <input
                className="field min-w-0 flex-1 basis-full sm:basis-40"
                aria-label={`Ingredient ${i + 1}`}
                placeholder="Ingredient"
                value={r.name}
                // The example's search words describe the example's ingredient, so they go when the name changes.
                onChange={(e) => setRow(i, { name: e.target.value, search_terms: null })}
              />
              <input
                className="field w-24"
                type="number"
                min={0}
                step="any"
                aria-label={`Amount of ${r.name || "ingredient " + (i + 1)}`}
                value={r.quantity.value || ""}
                onChange={(e) => setRow(i, { quantity: { ...r.quantity, value: Number(e.target.value) } })}
              />
              <select
                className="field w-20"
                aria-label={`Unit of ${r.name || "ingredient " + (i + 1)}`}
                value={r.quantity.unit}
                onChange={(e) => setRow(i, { quantity: { ...r.quantity, unit: e.target.value } })}
              >
                {["KG", "G", "L", "ML"].map((u) => (
                  <option key={u} value={u}>
                    {unit(u)}
                  </option>
                ))}
              </select>
              <button
                className="btn-ghost !px-2.5"
                aria-label={`Remove ${r.name || "ingredient " + (i + 1)}`}
                onClick={() => setRows((rs) => rs.filter((_, j) => j !== i))}
              >
                <X size={14} />
              </button>
            </div>
          ))}
          <button
            className="btn-ghost self-start"
            onClick={() => setRows((rs) => [...rs, { name: "", quantity: { value: 0, unit: "KG" } }])}
          >
            <Plus size={14} /> Add ingredient
          </button>
        </div>

        <div className="flex flex-wrap items-center gap-3">
          <button className="btn-dark" onClick={price} disabled={busy === "price" || !ready}>
            {busy === "price" ? "Pricing…" : "Price basket"}
          </button>
          {!ready ? <span className="label">Name the recipe, the portions and at least one ingredient with an amount.</span> : null}
        </div>
      </Card>

      {error ? <ErrorNote error={error} /> : null}
      {busy === "price" ? <Spinner label="Checking live prices and availability" /> : null}

      {basket && plan && busy !== "price" ? (
        <Card className="flex flex-col gap-4">
          <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
            <div className="min-w-0">
              <h2 className="text-[15px] font-semibold">
                {basket.scaled_manifest.recipe_name} for {num(basket.scaled_manifest.servings)} portions
              </h2>
              <p className="label">
                {plan.coverage_by_ingredient.length} ingredient{plan.coverage_by_ingredient.length === 1 ? "" : "s"} in{" "}
                {plan.lines.length} product{plan.lines.length === 1 ? "" : "s"} · deliver by {dateOnly(basket.deliver_by)} ·
                price and stock checked live in {calls} supplier call{calls === 1 ? "" : "s"}
              </p>
            </div>
            <div className="num text-[24px] font-semibold">{eur(plan.total)}</div>
          </div>

          <ul className="flex flex-col gap-2">
            {plan.lines.map((l) => {
              // Ingredients met by the same product share one line, so the pack rounding happens once.
              const uses = plan.coverage_by_ingredient.filter((c) => c.product_id === l.product_id);
              const short = uses.find((c) => c.covered < 1);
              const packs = l.units_needed ?? 0;
              return (
                <li key={l.product_id} className="flex flex-col gap-2 rounded-[var(--radius-tile)] bg-page p-3 text-[13px]">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="font-medium">{uses.map((c) => `${c.name}, ${qty(c.requested)}`).join(" and ")}</div>
                      <Link to={`/products/${l.product_id}`} className="text-ink-muted hover:underline">
                        {l.name}
                      </Link>
                    </div>
                    <div className="num shrink-0 font-medium">{eur(l.total, l.currency)}</div>
                  </div>
                  <div className="text-[12px] text-ink-muted">
                    {/* Sold by weight or volume reads as "12 kg"; sold in packs as "2 × 10 kg". */}
                    {l.size_per_sales_unit == null || !l.size_unit
                      ? `${num(packs)} ${l.sales_unit} at ${eur(l.unit_price, l.currency)} each`
                      : l.sales_unit === l.size_unit
                        ? `${qty({ value: packs * l.size_per_sales_unit, unit: l.size_unit })} at ${eur(l.unit_price, l.currency)} per ${unit(l.size_unit)}`
                        : `${num(packs)} × ${qty({ value: l.size_per_sales_unit, unit: l.size_unit })} at ${eur(l.unit_price, l.currency)} each`}
                    {l.overbuy && l.overbuy.amount > 0 ? ` · ${qty({ value: l.overbuy.amount, unit: l.overbuy.unit })} more than the recipe needs` : ""}
                    {short ? <span className="text-warn"> · only {pct(short.covered)} can arrive in time</span> : null}
                  </div>
                  <WarningChips warnings={l.warnings} />
                </li>
              );
            })}
          </ul>

          {plan.uncovered.length ? (
            <div className="rounded-[var(--radius-tile)] bg-warn-soft p-3 text-[13px] text-warn">
              <div className="font-medium">Not covered by this basket</div>
              <ul className="mt-1 flex flex-col gap-0.5">
                {plan.uncovered.map((u) => (
                  <li key={u.ingredient_index + u.reason}>
                    {u.name}: {u.reason}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          <div className="flex flex-col gap-2 border-t border-line pt-3">
            <div className="flex flex-wrap items-center gap-2">
              <span className="label">If this basket is ordered</span>
              <span className={`chip ${outcome.style}`}>{outcome.label}</span>
            </div>
            {plan.policy_preview.hits.length ? (
              <Hits hits={plan.policy_preview.hits} names={names} />
            ) : (
              <p className="label">Inside the agent's own authority: no rule would fire.</p>
            )}
            <p className="text-[12px] text-ink-muted">A preview only. The policy decides again when the order is placed.</p>
          </div>

          {quote ? null : plan.lines.length ? (
            <div>
              <button className="btn-dark" onClick={lock} disabled={busy === "quote"}>
                {busy === "quote" ? "Locking…" : "Lock price"}
              </button>
            </div>
          ) : (
            <p className="label">Nothing to order: no ingredient found a product that fits.</p>
          )}
        </Card>
      ) : null}

      {quote ? (
        <div ref={quoteRef} className="card flex scroll-mt-4 flex-col gap-3 border-ink/15 p-5">
          <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
            <div className="min-w-0">
              <h2 className="text-[15px] font-semibold">Price locked for the whole basket</h2>
              <p className="label">
                Fixed price · <Countdown to={quote.expires_at} /> · quote {quote.id.slice(0, 8)}
                {quote.signature ? " · signed by the gateway" : ""}
              </p>
            </div>
            <div className="num text-[24px] font-semibold">{eur(quote.total, quote.currency)}</div>
          </div>

          <ul className="flex flex-col gap-1.5">
            {quote.lines.map((l) => (
              <li key={l.product_id} className="flex items-baseline gap-3 rounded-[var(--radius-tile)] bg-page p-3 text-[13px]">
                <span className="num shrink-0 font-medium">
                  {num(l.quantity_sales_units ?? l.units_needed)} {l.sales_unit}
                </span>
                <span className="min-w-0 flex-1 text-ink-muted">{l.name}</span>
                <span className="num shrink-0 font-medium">{eur(l.total, l.currency)}</span>
              </li>
            ))}
          </ul>

          <WarningChips warnings={distinct(quote.warnings)} />

          {quote.hard_constraint_violations.length ? (
            <div className="rounded-[var(--radius-tile)] bg-negative-soft p-3 text-[12px] text-negative">
              {quote.hard_constraint_violations.join(" · ")}
            </div>
          ) : null}

          {!order ? (
            <div className="flex flex-wrap items-center gap-3">
              <button className="btn-dark" onClick={placeOrder} disabled={busy === "order"}>
                {busy === "order" ? "Placing…" : "Place order"}
              </button>
              <span className="label">One order for the basket: every line is reserved, or none.</span>
            </div>
          ) : null}
        </div>
      ) : null}

      {order ? (
        <div ref={orderRef} className="card flex scroll-mb-4 flex-col gap-3 p-5">
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <StatusChip status={order.status} />
            <h2 className="text-[15px] font-semibold">{ORDER_TITLE[order.status] ?? "Order " + order.status.toLowerCase()}</h2>
          </div>
          {order.policy?.hits.length ? (
            <Hits hits={order.policy.hits} names={names} />
          ) : (
            <p className="label">No policy rule matched; the order cleared automatically.</p>
          )}
          <div className="flex flex-wrap gap-2">
            <Link to={`/orders/${order.id}`} className="btn-ghost">
              Open order
            </Link>
            {order.status === "PENDING_APPROVAL" ? (
              <Link to="/approvals" className="btn-dark">
                Go to approval inbox
              </Link>
            ) : null}
          </div>
          <p className="label">
            {eur(order.total, order.currency)} · deliver by {dateOnly(quote?.deliver_by)}
          </p>
        </div>
      ) : null}
    </div>
  );
}

/** Policy rules in plain words, one row per rule, naming the products it is about instead of their numbers. */
function Hits({ hits, names }: { hits: RuleHit[]; names: Map<string, string> }) {
  const byRule = new Map<string, string[]>();
  for (const h of hits) {
    const about = /^(\d+): (.*)$/.exec(h.detail);
    const fit = about && /^([\d.]+) < ([\d.]+)$/.exec(about[2]);
    const general = h.detail.replace(/^plan: /, "");
    const text = about
      ? (names.get(about[1]) ?? `product ${about[1]}`) + (fit ? ` (fit ${pct(+fit[1])}, the policy asks for ${pct(+fit[2])})` : "")
      : general.startsWith(h.rule + ":") // a detail that repeats its own code is a data dump for machines
        ? ""
        : general;
    const texts = byRule.get(h.rule) ?? [];
    if (text && !texts.includes(text)) texts.push(text);
    byRule.set(h.rule, texts);
  }
  return (
    <ul className="flex flex-col gap-1.5 text-[13px]">
      {[...byRule].map(([rule, texts]) => (
        <li key={rule}>
          <strong title={rule}>{ruleLabel(rule)}</strong>
          {texts.length ? <span className="text-ink-muted"> — {texts.join(", ")}</span> : null}
        </li>
      ))}
    </ul>
  );
}
