import { useState } from "react";
import { Link } from "react-router-dom";
import { Leaf, ShieldCheck, Truck, Zap } from "lucide-react";
import { api, type NeedSpec, type OfferCard, type Order, type Quote } from "../api";
import { Card, ErrorNote, Spinner, StatusChip, WarningChips, eur, num, pct, dateOnly } from "../ui";

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

export default function Buyer() {
  const [need, setNeed] = useState<NeedSpec>(DEFAULT_NEED);
  const [lookup, setLookup] = useState<Awaited<ReturnType<typeof api.offers>> | null>(null);
  const [quote, setQuote] = useState<Quote | null>(null);
  const [order, setOrder] = useState<Order | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);

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

  const search = () =>
    guard("search", async () => {
      setQuote(null);
      setOrder(null);
      setLookup(await api.offers(need));
    });

  const lockPrice = (offer: OfferCard) =>
    guard("quote:" + offer.product_id, async () => {
      setOrder(null);
      setQuote(
        await api.createQuote({
          customer_id: need.customer_id,
          deliver_by: need.deliver_by,
          need_ref: lookup!.lookup_id,
          lines: [{ product_id: offer.product_id, quantity_sales_units: offer.units_needed ?? 0 }],
        }),
      );
    });

  const placeOrder = () =>
    guard("order", async () => {
      setOrder(await api.placeOrder(quote!.id, crypto.randomUUID()));
    });

  return (
    <div className="flex flex-col gap-4">
      <Card className="flex flex-col gap-4">
        <div>
          <h2 className="text-[15px] font-semibold">What does the kitchen need?</h2>
          <p className="label">
            A buyer agent turns a sentence into this structured need. The gateway never guesses the hard
            constraints.
          </p>
        </div>

        <div className="grid gap-3 lg:grid-cols-4">
          <label className="lg:col-span-4 flex flex-col gap-1.5">
            <span className="label">Intent</span>
            <input
              className="field"
              value={need.intent}
              onChange={(e) => setNeed({ ...need, intent: e.target.value })}
            />
          </label>

          <label className="flex flex-col gap-1.5">
            <span className="label">Quantity</span>
            <div className="flex gap-2">
              <input
                className="field"
                type="number"
                value={need.quantity.value}
                onChange={(e) =>
                  setNeed({ ...need, quantity: { ...need.quantity, value: Number(e.target.value) } })
                }
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
            <input
              className="field"
              type="date"
              value={need.deliver_by}
              onChange={(e) => setNeed({ ...need, deliver_by: e.target.value })}
            />
          </label>

          <label className="flex flex-col gap-1.5">
            <span className="label">Search terms</span>
            <input
              className="field"
              value={need.search_terms.join(", ")}
              onChange={(e) =>
                setNeed({ ...need, search_terms: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) })
              }
            />
          </label>

          <label className="flex flex-col gap-1.5">
            <span className="label">Required claims</span>
            <input
              className="field"
              value={need.required_claims.join(", ")}
              onChange={(e) =>
                setNeed({
                  ...need,
                  required_claims: e.target.value.split(",").map((s) => s.trim()).filter(Boolean),
                })
              }
            />
          </label>
        </div>

        <div className="flex items-center gap-3">
          <button className="btn-dark" onClick={search} disabled={busy === "search"}>
            {busy === "search" ? "Searching…" : "Find offers"}
          </button>
          {lookup ? (
            <span className="label">
              {lookup.offers.length} shortlisted of {lookup.offers.length + lookup.excluded_count} ·{" "}
              <strong className="text-ink">
                {lookup.live_calls.price + lookup.live_calls.availability}
              </strong>{" "}
              upstream calls · {lookup.decision_calls} fit checks
            </span>
          ) : null}
        </div>
      </Card>

      {error ? <ErrorNote error={error} /> : null}
      {busy === "search" ? <Spinner label="Checking live prices and availability" /> : null}

      {lookup && !busy ? (
        <div className="grid gap-4 xl:grid-cols-2">
          {lookup.offers.map((o) => (
            <Card key={o.product_id} className="flex flex-col gap-3">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="mb-1 flex items-center gap-2">
                    {o.rank_label ? (
                      <span className={`chip ${RANK_STYLE[o.rank_label] ?? "bg-chip text-ink-muted"}`}>
                        {o.rank_label}
                      </span>
                    ) : null}
                    <span className="label">#{o.product_id}</span>
                  </div>
                  <h3 className="text-[15px] font-medium leading-snug">{o.name}</h3>
                </div>
                <div className="text-right">
                  <div className="num text-[20px] font-semibold">{eur(o.total, o.currency)}</div>
                  <div className="label">{eur(o.price_per_l_or_kg, o.currency)} / {o.size_unit ?? "unit"}</div>
                </div>
              </div>

              <div className="rounded-[var(--radius-tile)] bg-page p-3 text-[12px] text-ink-muted">
                {num(need.quantity.value)} {need.quantity.unit} ÷ {num(o.size_per_sales_unit, 2)}{" "}
                {o.size_unit}/{o.sales_unit} ={" "}
                <strong className="text-ink">
                  {num(o.units_needed)} {o.sales_unit}
                </strong>{" "}
                at {eur(o.unit_price, o.currency)} each
                {o.size_source ? <span className="text-ink-soft"> · size from {o.size_source}</span> : null}
              </div>

              <div className="grid grid-cols-3 gap-2 text-[12px]">
                <Stat icon={<Truck size={13} />} label="Coverage" value={pct(o.availability?.coverage)} />
                <Stat
                  icon={<ShieldCheck size={13} />}
                  label="Reliability"
                  value={o.delivery_reliability == null ? "—" : pct(o.delivery_reliability)}
                />
                <Stat
                  icon={<Leaf size={13} />}
                  label="CO₂"
                  value={o.co2_kg_per_kg == null ? "—" : `${num(o.co2_kg_per_kg, 2)} kg/kg`}
                />
              </div>

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

              <div className="flex items-center justify-between gap-3 border-t border-line pt-3">
                <span className="label">
                  {o.availability?.logistic_model ?? "—"} · arrives {o.availability?.available_date ?? "—"}
                  {o.fit != null ? ` · fit ${pct(o.fit)}` : ""}
                </span>
                <button
                  className="btn-dark"
                  onClick={() => lockPrice(o)}
                  disabled={busy === "quote:" + o.product_id}
                >
                  {busy === "quote:" + o.product_id ? "Locking…" : "Lock price"}
                </button>
              </div>
            </Card>
          ))}
        </div>
      ) : null}

      {quote ? (
        <Card className="flex flex-col gap-3 border-ink/15">
          <div className="flex items-start justify-between gap-4">
            <div>
              <h2 className="text-[15px] font-semibold">Quote locked</h2>
              <p className="label">
                Fixed price until {new Date(quote.expires_at).toLocaleTimeString("en-GB")} · quote{" "}
                {quote.id.slice(0, 8)} · {quote.live_calls.price + quote.live_calls.availability} re-checks
              </p>
            </div>
            <div className="num text-[24px] font-semibold">{eur(quote.total, quote.currency)}</div>
          </div>

          {quote.hard_constraint_violations.length ? (
            <div className="rounded-[var(--radius-tile)] bg-negative-soft p-3 text-[12px] text-negative">
              {quote.hard_constraint_violations.join(" · ")}
            </div>
          ) : null}

          {!order ? (
            <div>
              <button className="btn-dark" onClick={placeOrder} disabled={busy === "order"}>
                {busy === "order" ? "Placing…" : "Place order"}
              </button>
            </div>
          ) : null}
        </Card>
      ) : null}

      {order ? (
        <Card className="flex flex-col gap-3">
          <div className="flex items-center gap-3">
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
                <li key={h.rule} className="flex gap-2">
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
          <div className="flex gap-2">
            <Link to={`/orders/${order.id}`} className="btn-ghost">
              Open order
            </Link>
            {order.status === "PENDING_APPROVAL" ? (
              <Link to="/approvals" className="btn-dark">
                Go to approval inbox
              </Link>
            ) : null}
          </div>
          <p className="label">Deliver by {dateOnly(order.lines[0] ? quote?.deliver_by : null)}</p>
        </Card>
      ) : null}
    </div>
  );
}

function Stat({ icon, label, value }: { icon: React.ReactNode; label: string; value: string }) {
  return (
    <div className="rounded-[var(--radius-tile)] border border-line p-2.5">
      <div className="flex items-center gap-1 text-ink-soft">
        {icon}
        <span className="text-[11px]">{label}</span>
      </div>
      <div className="num mt-0.5 text-[14px] font-medium">{value}</div>
    </div>
  );
}
