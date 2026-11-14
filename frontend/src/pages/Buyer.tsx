import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ChevronDown, FileSignature, Leaf, ShieldCheck, Sparkles, Truck, Zap } from "lucide-react";
import {
  api,
  type FulfilmentPlan,
  type LookupResult,
  type NeedSpec,
  type OfferCard,
  type Order,
  type Quote,
} from "../api";
import { Progress } from "../charts";
import { Card, Countdown, ErrorNote, Spinner, StatusChip, WarningChips, eur, num, pct, dateOnly } from "../ui";

const DEFAULT_NEED: NeedSpec = {
  customer_id: "1",
  intent: "1000 litres of lactose-free milk for a hotel",
  search_terms: ["milk"],
  required_claims: ["FREE_FROM_LACTOSE"],
  exclude_allergens: ["AE"],
  quantity: { value: 1000, unit: "L" },
  deliver_by: "2026-11-16",
  max_candidates: 10,
};

const RANK_STYLE: Record<string, string> = {
  best: "bg-positive-soft text-positive",
  better: "bg-chip text-ink",
  good: "bg-chip text-ink-muted",
};

const OUTCOME = {
  CONFIRMED: { label: "Agent may confirm alone", style: "bg-positive-soft text-positive" },
  PENDING_APPROVAL: { label: "Will ask a human", style: "bg-warn-soft text-warn" },
  REJECTED: { label: "Would be rejected", style: "bg-negative-soft text-negative" },
} as Record<string, { label: string; style: string }>;

const PLAN_TITLE = {
  single: "One product covers it",
  split: "Split across products",
  partial: "Partial — not everything in time",
};

const FACTOR_STYLE: Record<string, string> = {
  price: "bg-brand",
  coverage: "bg-positive",
  reliability: "bg-[#5b8def]",
  co2: "bg-[#8bc34a]",
  fit: "bg-warn",
};

const list = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);

export default function Buyer() {
  const [need, setNeed] = useState<NeedSpec>(DEFAULT_NEED);
  const [prefsOpen, setPrefsOpen] = useState(false);
  const [lookup, setLookup] = useState<LookupResult | null>(null);
  const [quote, setQuote] = useState<Quote | null>(null);
  const [order, setOrder] = useState<Order | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  /** product_id → sales units. Several offers can be split across one quote when no single one covers the need. */
  const [cart, setCart] = useState<Record<string, number>>({});
  // The quote and the order render below the whole offer list; without this, "Quote this plan" looks like it did nothing.
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

  const prefs = need.preferences ?? {};
  const setPrefs = (p: NonNullable<NeedSpec["preferences"]>) => setNeed({ ...need, preferences: { ...prefs, ...p } });

  const search = () =>
    guard("search", async () => {
      setQuote(null);
      setOrder(null);
      setCart({});
      setLookup(await api.offers(need));
    });

  const toggleLine = (offer: OfferCard) =>
    setCart((c) => {
      const next = { ...c };
      if (next[offer.product_id] != null) delete next[offer.product_id];
      else next[offer.product_id] = offer.units_needed ?? 0;
      return next;
    });

  const setUnits = (productId: string, units: number) =>
    setCart((c) => ({ ...c, [productId]: Math.max(0, units) }));

  const lockCart = () =>
    guard("quote", async () => {
      setOrder(null);
      setQuote(
        await api.createQuote({
          customer_id: need.customer_id,
          deliver_by: need.deliver_by,
          need_ref: lookup!.lookup_id,
          lines: Object.entries(cart)
            .filter(([, units]) => units > 0)
            .map(([product_id, quantity_sales_units]) => ({ product_id, quantity_sales_units })),
        }),
      );
    });

  // A plan quote is built server-side from the stored lookup, so the client cannot alter its lines.
  const lockPlan = (index: number) =>
    guard("plan:" + index, async () => {
      setOrder(null);
      setCart({});
      setQuote(
        await api.createQuote({
          customer_id: need.customer_id,
          deliver_by: need.deliver_by,
          plan_ref: { lookup_id: lookup!.lookup_id, plan_index: index },
        }),
      );
    });

  const placeOrder = () =>
    guard("order", async () => {
      setOrder(await api.placeOrder(quote!.id, crypto.randomUUID()));
    });

  const offersById = new Map((lookup?.offers ?? []).map((o) => [o.product_id, o]));
  const cartOffers = (lookup?.offers ?? []).filter((o) => cart[o.product_id] != null);
  const cartTotal = cartOffers.reduce((sum, o) => sum + (o.unit_price ?? 0) * (cart[o.product_id] ?? 0), 0);
  const cartCovered = cartOffers.reduce((sum, o) => sum + (o.size_per_sales_unit ?? 0) * (cart[o.product_id] ?? 0), 0);

  return (
    <div className="flex flex-col gap-4">
      <Card className="flex flex-col gap-4">
        <div>
          <h2 className="text-[15px] font-semibold">What does the kitchen need?</h2>
          <p className="label">
            A buyer agent turns a sentence into this structured need. The gateway never guesses the hard constraints.
          </p>
        </div>

        <div className="grid gap-3 lg:grid-cols-4">
          <label className="flex flex-col gap-1.5 lg:col-span-3">
            <span className="label">Intent</span>
            <input className="field" value={need.intent} onChange={(e) => setNeed({ ...need, intent: e.target.value })} />
          </label>

          <label className="flex flex-col gap-1.5">
            <span className="label">Quantity</span>
            <div className="flex gap-2">
              <input
                className="field"
                type="number"
                value={need.quantity.value}
                onChange={(e) => setNeed({ ...need, quantity: { ...need.quantity, value: Number(e.target.value) } })}
              />
              <select
                className="field w-24"
                value={need.quantity.unit}
                onChange={(e) => setNeed({ ...need, quantity: { ...need.quantity, unit: e.target.value } })}
              >
                <option value="L">L</option>
                <option value="KG">KG</option>
              </select>
            </div>
          </label>

          <label className="flex flex-col gap-1.5">
            <span className="label">Deliver by</span>
            <input className="field" type="date" value={need.deliver_by} onChange={(e) => setNeed({ ...need, deliver_by: e.target.value })} />
          </label>

          <label className="flex flex-col gap-1.5">
            <span className="label">Search terms</span>
            <input className="field" value={need.search_terms.join(", ")} onChange={(e) => setNeed({ ...need, search_terms: list(e.target.value) })} />
          </label>

          <label className="flex flex-col gap-1.5">
            <span className="label">Required claims</span>
            <input className="field" value={need.required_claims.join(", ")} onChange={(e) => setNeed({ ...need, required_claims: list(e.target.value) })} />
          </label>

          <label className="flex flex-col gap-1.5">
            <span className="label">Exclude allergens</span>
            <input className="field" value={need.exclude_allergens.join(", ")} onChange={(e) => setNeed({ ...need, exclude_allergens: list(e.target.value) })} />
          </label>
        </div>

        <div className="rounded-[var(--radius-tile)] border border-line">
          <button
            className="flex w-full items-center justify-between px-4 py-3 text-left"
            onClick={() => setPrefsOpen((v) => !v)}
            aria-expanded={prefsOpen}
          >
            <span>
              <span className="text-[13px] font-medium">Preferences</span>{" "}
              <span className="label">— soft: offers outside them still appear, ranked lower and flagged for sign-off</span>
            </span>
            <ChevronDown size={16} className={`text-ink-soft transition-transform ${prefsOpen ? "rotate-180" : ""}`} />
          </button>
          {prefsOpen ? (
            <div className="grid gap-3 border-t border-line p-4 lg:grid-cols-4">
              <label className="flex flex-col gap-1.5">
                <span className="label">Preferred brands</span>
                <input className="field" placeholder="Valio" value={(prefs.brands ?? []).join(", ")} onChange={(e) => setPrefs({ brands: list(e.target.value) })} />
              </label>
              <label className="flex flex-col gap-1.5">
                <span className="label">Max price per unit (EUR)</span>
                <input
                  className="field"
                  type="number"
                  step="0.01"
                  value={prefs.max_price_per_unit ?? ""}
                  onChange={(e) => setPrefs({ max_price_per_unit: e.target.value === "" ? null : Number(e.target.value) })}
                />
              </label>
              <label className="flex flex-col gap-1.5">
                <span className="label">Pack size ({need.quantity.unit}) min – max</span>
                <div className="flex gap-2">
                  <input
                    className="field"
                    type="number"
                    placeholder="min"
                    value={prefs.pack_size?.min ?? ""}
                    onChange={(e) => setPrefs({ pack_size: { ...prefs.pack_size, min: e.target.value === "" ? null : Number(e.target.value) } })}
                  />
                  <input
                    className="field"
                    type="number"
                    placeholder="max"
                    value={prefs.pack_size?.max ?? ""}
                    onChange={(e) => setPrefs({ pack_size: { ...prefs.pack_size, max: e.target.value === "" ? null : Number(e.target.value) } })}
                  />
                </div>
              </label>
              <label className="flex flex-col gap-1.5">
                <span className="label">Labels</span>
                <input className="field" placeholder="GOODS_FROM_FINLAND_BLUE_SWAN" value={(prefs.labels ?? []).join(", ")} onChange={(e) => setPrefs({ labels: list(e.target.value) })} />
              </label>
            </div>
          ) : null}
        </div>

        <div className="flex flex-wrap items-center gap-3">
          <button className="btn-dark" onClick={search} disabled={busy === "search"}>
            {busy === "search" ? "Searching…" : "Find offers"}
          </button>
          {lookup ? <LookupSummary lookup={lookup} /> : null}
        </div>
      </Card>

      {error ? <ErrorNote error={error} /> : null}
      {busy === "search" ? <Spinner label="Checking live prices and availability" /> : null}

      {lookup && busy !== "search" ? (
        <>
          <Constraints lookup={lookup} />

          {lookup.no_match_reason && !lookup.offers.length ? (
            <Card className="border-warn/30 bg-warn-soft">
              <div className="text-[14px] font-semibold text-warn">Nothing fits this need</div>
              <p className="mt-1 text-[13px] text-ink">{lookup.no_match_reason}</p>
            </Card>
          ) : null}

          {lookup.plans?.length ? (
            <section className="flex flex-col gap-3">
              <div>
                <h2 className="text-[15px] font-semibold">Ways to fulfil it</h2>
                <p className="label">
                  Each plan shows what the policy would decide before anything is committed.
                </p>
              </div>
              {/* grid-cols-1, not the implicit auto column: that one grows to fit the nowrap product names and widens the page on phones. */}
              <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
                {lookup.plans.map((plan, i) => (
                  <PlanCard
                    key={i}
                    plan={plan}
                    offersById={offersById}
                    busy={busy === "plan:" + i}
                    onQuote={() => lockPlan(i)}
                  />
                ))}
              </div>
            </section>
          ) : null}

          {lookup.offers.length ? (
            <section className="flex flex-col gap-3">
              <div>
                <h2 className="text-[15px] font-semibold">Offers</h2>
                <p className="label">Ranked by total cost, coverage, reliability, CO₂ and fit — or build your own split.</p>
              </div>
              <div className="grid gap-4 xl:grid-cols-2">
                {lookup.offers.map((o) => (
                  <OfferView
                    key={o.product_id}
                    o={o}
                    need={need}
                    units={cart[o.product_id]}
                    onToggle={() => toggleLine(o)}
                    onUnits={(u) => setUnits(o.product_id, u)}
                  />
                ))}
              </div>
            </section>
          ) : null}
        </>
      ) : null}

      {cartOffers.length && !quote ? (
        <div className="sticky bottom-4 z-10">
          {/* Phone: summary on its own row, total and button below, so neither squeezes the text into a tall column. */}
          <Card className="flex flex-wrap items-center gap-x-4 gap-y-3 !p-4 shadow-[0_8px_30px_rgba(0,0,0,0.08)] sm:!p-5">
            <div className="min-w-0 basis-full sm:flex-1 sm:basis-0">
              <div className="text-[14px] font-medium">
                {cartOffers.length} line{cartOffers.length > 1 ? "s" : ""} selected
              </div>
              <div className="label">
                covers {num(cartCovered)} of {num(need.quantity.value)} {need.quantity.unit}
                {cartCovered < need.quantity.value ? (
                  <span className="text-warn"> · {num(need.quantity.value - cartCovered)} short</span>
                ) : (
                  <span className="text-positive"> · need met</span>
                )}
              </div>
            </div>
            <div className="num mr-auto text-[20px] font-semibold sm:mr-0">{eur(cartTotal)}</div>
            <button className="btn-dark" onClick={lockCart} disabled={busy === "quote"}>
              {busy === "quote" ? "Locking…" : "Lock price"}
            </button>
          </Card>
        </div>
      ) : null}

      {quote ? (
        <div ref={quoteRef} className="card flex scroll-mt-4 flex-col gap-3 border-ink/15 p-5">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div>
              <h2 className="text-[15px] font-semibold">
                Quote locked{quote.plan_ref ? ` · plan ${quote.plan_ref.plan_index + 1}` : ""}
              </h2>
              <p className="label">
                Fixed price · <Countdown to={quote.expires_at} /> · quote {quote.id.slice(0, 8)} ·{" "}
                {quote.live_calls.price + quote.live_calls.availability} re-checks
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
                {/* What is being bought wraps rather than ellipsizes: a phone has no hover to reveal the rest. */}
                <span className="min-w-0 flex-1 text-ink-muted">{l.name}</span>
                <span className="num shrink-0 font-medium">{eur(l.total, l.currency)}</span>
              </li>
            ))}
          </ul>

          {quote.warnings?.length ? <WarningChips warnings={quote.warnings} /> : null}

          {quote.hard_constraint_violations.length ? (
            <div className="rounded-[var(--radius-tile)] bg-negative-soft p-3 text-[12px] text-negative">
              {quote.hard_constraint_violations.join(" · ")}
            </div>
          ) : null}

          {quote.signature ? (
            <div className="flex items-start gap-2 rounded-[var(--radius-tile)] border border-line p-3 text-[12px] text-ink-muted">
              <FileSignature size={14} className="mt-px shrink-0 text-ink" />
              <div className="flex min-w-0 flex-1 flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
                <span>Signed by the gateway (JWS) — an agent can verify the price offline against the published keys.</span>
                <code className="break-all text-[11px] text-ink-muted" title={quote.signature}>
                  {quote.signature.slice(0, 28)}…
                </code>
              </div>
            </div>
          ) : null}

          {!order ? (
            <div>
              <button className="btn-dark" onClick={placeOrder} disabled={busy === "order"}>
                {busy === "order" ? "Placing…" : "Place order"}
              </button>
            </div>
          ) : null}
        </div>
      ) : null}

      {order ? (
        <div ref={orderRef} className="card flex scroll-mb-4 flex-col gap-3 p-5">
          <div className="flex items-baseline gap-3">
            <StatusChip status={order.status} />
            <h2 className="text-[15px] font-semibold">
              {order.status === "CONFIRMED"
                ? "The agent confirmed this on its own"
                : order.status === "PENDING_APPROVAL"
                  ? "Held for a human signature"
                  : "Order " + order.status.toLowerCase()}
            </h2>
          </div>
          {order.policy?.hits.length ? (
            <ul className="flex flex-col gap-1.5 text-[13px]">
              {order.policy.hits.map((h) => (
                <li key={h.rule + h.detail} className="flex gap-2">
                  <Zap size={14} className="mt-0.5 shrink-0 text-warn" />
                  <span>
                    <strong>{h.rule}</strong> — <span className="text-ink-muted">{h.detail}</span>
                  </span>
                </li>
              ))}
            </ul>
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
          <p className="label">Deliver by {dateOnly(quote?.deliver_by)}</p>
        </div>
      ) : null}
    </div>
  );
}

function LookupSummary({ lookup }: { lookup: LookupResult }) {
  const calls = lookup.live_calls.price + lookup.live_calls.availability;
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-[13px] text-ink-muted">
      {lookup.matched_count != null ? (
        <span>
          <strong className="num text-ink">{num(lookup.matched_count)}</strong> matched
        </span>
      ) : null}
      {lookup.screened_count != null ? (
        <span>
          <strong className="num text-ink">{lookup.screened_count}</strong> screened
        </span>
      ) : null}
      {lookup.priced_count != null ? (
        <span>
          <strong className="num text-ink">{lookup.priced_count}</strong> priced
        </span>
      ) : null}
      <span>
        <strong className="num text-ink">{lookup.offers.length}</strong> shortlisted
      </span>
      <span>
        <strong className="num text-ink">{calls}</strong> Valio call{calls === 1 ? "" : "s"}
      </span>
      <span>
        <strong className="num text-ink">{lookup.decision_calls}</strong> fit checks
      </span>
      {lookup.retrieval?.mode ? (
        <span className="chip bg-chip text-ink-muted" title={`terms: ${(lookup.retrieval.terms_used ?? []).join(", ")}`}>
          search: {lookup.retrieval.mode.replaceAll("_", " ")}
        </span>
      ) : null}
      {lookup.warnings?.length ? <WarningChips warnings={lookup.warnings} /> : null}
    </div>
  );
}

/** Where each hard constraint came from — the request, or a standing rule in the customer's policy. */
function Constraints({ lookup }: { lookup: LookupResult }) {
  const ec = lookup.effective_constraints;
  const items = [
    ...(ec?.required_claims ?? []).map((c) => ({ ...c, kind: "required" })),
    ...(ec?.exclude_allergens ?? []).map((c) => ({ ...c, kind: "excluded" })),
  ];
  if (!items.length) return null;
  const removed = lookup.exclusion_counts ?? {};
  return (
    <Card className="flex flex-wrap items-center gap-2 !py-3">
      <span className="label mr-1">Hard constraints applied</span>
      {items.map((c) => (
        <span
          key={c.kind + c.code}
          className={`chip ${c.source === "customer_policy" ? "bg-brand text-white" : "bg-chip text-ink"}`}
          title={c.source === "customer_policy" ? "Standing constraint from the customer's policy" : "From this request"}
        >
          {c.kind === "required" ? "must be" : "excludes"} {c.code.replaceAll("_", " ").toLowerCase()}
          {removed[c.code] ? <span className="opacity-60">−{num(removed[c.code])}</span> : null}
          {c.source === "customer_policy" ? <span className="opacity-60">· standing</span> : null}
        </span>
      ))}
    </Card>
  );
}

function PlanCard({
  plan,
  offersById,
  busy,
  onQuote,
}: {
  plan: FulfilmentPlan;
  offersById: Map<string, OfferCard>;
  busy: boolean;
  onQuote: () => void;
}) {
  const outcome = OUTCOME[plan.policy_preview.outcome] ?? OUTCOME.PENDING_APPROVAL;
  const covered = plan.coverage.value / Math.max(plan.coverage.value + plan.uncovered.value, 1);
  return (
    // From lg the plans share four subgrid rows, so lines, coverage and the policy verdict start level in every card.
    <Card className="flex flex-col gap-3 lg:row-span-4 lg:grid lg:grid-cols-1 lg:grid-rows-subgrid">
      <div className="flex items-start justify-between gap-3">
        <div>
          <span className="chip bg-chip text-ink-muted">{plan.kind}</span>
          <h3 className="mt-2 text-[14px] font-semibold">{PLAN_TITLE[plan.kind]}</h3>
        </div>
        <div className="num text-[20px] font-semibold">{eur(plan.total)}</div>
      </div>

      <ul className="flex flex-col gap-1.5 text-[12px]">
        {plan.lines.map((l) => (
          <li key={l.product_id} className="flex items-baseline gap-2">
            <span className="num shrink-0 font-medium">
              {num(l.units)} {offersById.get(l.product_id)?.sales_unit ?? ""}
            </span>
            <Link
              to={`/products/${l.product_id}`}
              className="min-w-0 flex-1 truncate text-ink-muted hover:underline"
              title={offersById.get(l.product_id)?.name}
            >
              {offersById.get(l.product_id)?.name ?? `#${l.product_id}`}
            </Link>
            <span className="num shrink-0 text-ink-muted">
              {num(l.covers.value)} {l.covers.unit}
            </span>
          </li>
        ))}
      </ul>

      <div className="flex flex-col gap-3">
        <div className="flex flex-col gap-1.5">
          <Progress value={covered} tone={covered >= 1 ? "positive" : "warn"} />
          <div className="flex justify-between gap-3 text-[12px] text-ink-muted">
            <span>
              covers {num(plan.coverage.value)} {plan.coverage.unit}
            </span>
            {plan.uncovered.value > 0 ? (
              <span className="text-warn">
                {num(plan.uncovered.value)} {plan.uncovered.unit} uncovered
              </span>
            ) : (
              <span className="text-positive">need met</span>
            )}
          </div>
        </div>
        {plan.warnings?.length ? <WarningChips warnings={plan.warnings} /> : null}
      </div>

      {/* No auto margin from lg: in a grid it would stop the footer stretching, and its top rule would sit at different heights. */}
      <div className="mt-auto flex flex-col gap-2 border-t border-line pt-3 lg:mt-0">
        <span className={`chip self-start ${outcome.style}`}>{outcome.label}</span>
        {plan.policy_preview.hits.length ? (
          <ul className="mb-auto flex flex-col gap-1 text-[12px] text-ink-muted">
            {plan.policy_preview.hits.map((h) => (
              <li key={h.rule + h.detail}>
                <strong className="text-ink">{h.rule}</strong> — {h.detail}
              </li>
            ))}
          </ul>
        ) : (
          <p className="mb-auto text-[12px] text-ink-muted">Inside the agent's own authority — no rule would fire.</p>
        )}
        <button className="btn-dark mt-1" onClick={onQuote} disabled={busy}>
          {busy ? "Locking…" : "Quote this plan"}
        </button>
      </div>
    </Card>
  );
}

function OfferView({
  o,
  need,
  units,
  onToggle,
  onUnits,
}: {
  o: OfferCard;
  need: NeedSpec;
  units: number | undefined;
  onToggle: () => void;
  onUnits: (u: number) => void;
}) {
  const breakdown = Object.entries(o.score_breakdown ?? {}).filter(([, v]) => v > 0);
  const scoreSum = breakdown.reduce((a, [, v]) => a + v, 0);
  const extra = o.purchase_total != null && o.total != null && o.purchase_total - o.total > 0.005;
  return (
    <Card className="flex flex-col gap-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="mb-1 flex items-center gap-2">
            {o.rank_label ? (
              <span className={`chip ${RANK_STYLE[o.rank_label] ?? "bg-chip text-ink-muted"}`}>{o.rank_label}</span>
            ) : (
              <span className="chip bg-chip text-ink-muted" title="Offers that cannot be ordered as asked carry no rank">
                unranked
              </span>
            )}
            <Link to={`/products/${o.product_id}`} className="label hover:underline">
              #{o.product_id}
            </Link>
          </div>
          <h3 className="text-[15px] font-medium leading-snug">{o.name}</h3>
        </div>
        <div className="shrink-0 text-right">
          <div className="num text-[20px] font-semibold">{eur(o.purchase_total ?? o.total, o.currency ?? "EUR")}</div>
          <div className="label">
            {eur(o.price_per_l_or_kg, o.currency ?? "EUR")} / {o.size_unit ?? "unit"}
          </div>
        </div>
      </div>

      <div className="rounded-[var(--radius-tile)] bg-page p-3 text-[12px] text-ink-muted">
        {num(need.quantity.value)} {need.quantity.unit} ÷ {num(o.size_per_sales_unit, 2)} {o.size_unit}/{o.sales_unit} ={" "}
        <strong className="text-ink">
          {num(o.units_needed)} {o.sales_unit}
        </strong>{" "}
        at {eur(o.unit_price, o.currency ?? "EUR")} each
        {o.size_source ? (
          <span>
            {" "}
            · size from {o.size_source.replaceAll("_", " ")}
            {o.size_confirmed_by_price ? ", confirmed by price" : ""}
          </span>
        ) : null}
        {o.overbuy ? (
          <span className="text-warn">
            {" "}
            · whole packs overshoot by {num(o.overbuy.amount)} {o.overbuy.unit}
          </span>
        ) : null}
        {extra ? <span> · need-only cost {eur(o.total, o.currency ?? "EUR")}</span> : null}
      </div>

      <div className="grid grid-cols-3 gap-2 text-[12px]">
        <Stat icon={<Truck size={13} />} label="Coverage" value={pct(o.availability?.coverage)} />
        <Stat icon={<ShieldCheck size={13} />} label="Reliability" value={o.delivery_reliability == null ? "no data" : pct(o.delivery_reliability)} />
        <Stat icon={<Leaf size={13} />} label="CO₂ (group avg)" value={o.co2_kg_per_kg == null ? "—" : `${num(o.co2_kg_per_kg, 2)} kg/kg`} />
      </div>

      {breakdown.length ? (
        <div className="flex flex-col gap-1.5" title="Weighted ranking factors from the buyer's policy">
          <div className="flex h-1.5 overflow-hidden rounded-full bg-chip">
            {breakdown.map(([k, v]) => (
              <span key={k} className={FACTOR_STYLE[k] ?? "bg-ink-soft"} style={{ width: `${(v / Math.max(scoreSum, 1e-9)) * 100}%` }} />
            ))}
          </div>
          <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-ink-muted">
            {breakdown.map(([k, v]) => (
              <span key={k} className="flex items-center gap-1">
                <span className={`size-1.5 rounded-full ${FACTOR_STYLE[k] ?? "bg-ink-soft"}`} />
                {k === "co2" ? "CO₂" : k} {v.toFixed(2)}
              </span>
            ))}
          </div>
        </div>
      ) : null}

      <WarningChips warnings={o.warnings} />

      {o.matched_because.length ? (
        <ul className="flex flex-col gap-1 text-[12px] text-ink-muted">
          {o.matched_because.map((m) => (
            <li key={m} className="flex gap-1.5">
              <span className="text-positive">✓</span>
              {m}
            </li>
          ))}
        </ul>
      ) : null}

      {o.fit_question && o.fit != null ? (
        <details className="text-[12px] text-ink-muted">
          {/* Padding cancelled by margin: a 36px tap target without moving the line. */}
          <summary className="-my-2.5 flex cursor-pointer list-none items-center gap-1.5 py-2.5">
            <Sparkles size={12} className="text-warn" /> fit {pct(o.fit)} — the question the model answered
          </summary>
          <p className="mt-1.5 rounded-[var(--radius-tile)] bg-page p-2.5">{o.fit_question}</p>
        </details>
      ) : null}

      <div className="mt-auto flex flex-wrap items-center justify-between gap-3 border-t border-line pt-3">
        <span className="label">
          {o.availability?.logistic_model ?? "—"} · arrives {o.availability?.available_date ?? "—"}
        </span>
        {units != null ? (
          <div className="ml-auto flex items-center gap-2">
            <input
              type="number"
              className="field w-24 text-right"
              aria-label={`${o.sales_unit} of ${o.name}`}
              value={units}
              onChange={(e) => onUnits(Number(e.target.value))}
            />
            <span className="label">{o.sales_unit}</span>
            <button className="btn-ghost" onClick={onToggle}>
              Remove
            </button>
          </div>
        ) : (
          <button className="btn-dark ml-auto" onClick={onToggle}>
            Add to quote
          </button>
        )}
      </div>
    </Card>
  );
}

function Stat({ icon, label, value }: { icon: React.ReactNode; label: string; value: string }) {
  return (
    // Value pinned to the bottom: when a label wraps on a phone, the three numbers still share a baseline.
    <div className="flex flex-col justify-between gap-0.5 rounded-[var(--radius-tile)] border border-line p-2.5">
      <div className="flex items-start gap-1 text-ink-muted">
        <span className="mt-0.5 shrink-0">{icon}</span>
        <span className="text-[11px]">{label}</span>
      </div>
      <div className="num text-[14px] font-medium">{value}</div>
    </div>
  );
}
