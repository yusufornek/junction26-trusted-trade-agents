import { useState } from "react";
import { Link } from "react-router-dom";
import { TriangleAlert } from "lucide-react";
import { api, type Anomaly, type Order, type Quote } from "./api";
import { ErrorNote, StatusChip, eur, num, ruleLabel } from "./ui";

/** The gateway also lists these as policy hits, with the band printed as a raw dictionary; screens that show the
 *  structured note below leave those hits out. */
export const ANOMALY_CODES = new Set(["QUANTITY_ANOMALY", "PRICE_ANOMALY", "VELOCITY_ANOMALY", "NEW_CATEGORY"]);

// history_window.weekday counts from Monday = 0.
const WEEKDAYS = ["Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays", "Sundays"];
const UNITS: Record<string, string> = { SALES_UNIT: "sales units", "orders/hour": "orders an hour" };

function sentence(a: Anomaly) {
  const w = a.history_window;
  const basis = w?.points
    ? ` Based on ${w.points} past orders${w.weekday != null ? ` on ${WEEKDAYS[w.weekday]}` : ""}${a.band_source === "forecast" ? " and a forecast" : ""}.`
    : "";
  if (a.code === "NEW_CATEGORY") return `Nothing from this product category was bought before.${basis}`;
  const b = a.expected_band;
  const usual = b?.upper == null ? "" : b.lower ? ` against the usual ${num(b.lower, 2)} to ${num(b.upper, 2)}` : ` against a usual ${num(b.upper, 2)} at most`;
  const seen = typeof a.observed === "number" ? num(a.observed, 2) : a.observed;
  return `${seen} ${UNITS[a.unit ?? ""] ?? a.unit ?? ""}${usual}.${basis}`;
}

/** What looks unlike this buyer's history, in words, with the gateway's proposed corrections. A correction makes fresh
 *  quotes and cancels the order waiting on the old one; it never places or approves anything. */
export function AnomalyNotes({
  anomalies,
  quoteId,
  onFixed,
}: {
  anomalies?: Anomaly[];
  /** With a quote and a handler, corrections can be applied here. */
  quoteId?: string;
  onFixed?: (quotes: Quote[]) => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const all = (anomalies ?? []).filter((a) => a.status === "anomaly");
  if (!all.length) return null;
  // A basket of first-time products raises one note per product; say it once, with the count.
  const fresh = all.filter((a) => a.code === "NEW_CATEGORY");
  const found = fresh.length > 1 ? [...all.filter((a) => a.code !== "NEW_CATEGORY"), { ...fresh[0], product_id: null, fixes: [] }] : all;

  const apply = async (fixId: string) => {
    setBusy(fixId);
    setError(null);
    try {
      onFixed!((await api.applyAnomalyFix(quoteId!, fixId)).quotes);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="flex flex-col gap-2">
      {found.map((a) => (
        <div key={a.code + (a.product_id ?? "")} className="flex flex-col gap-2 rounded-[var(--radius-tile)] border border-warn/30 bg-warn-soft p-3 text-[13px]">
          <div className="flex items-start gap-2">
            <TriangleAlert size={15} className="mt-0.5 shrink-0 text-warn" aria-hidden />
            <p className="min-w-0">
              <strong className="text-warn" title={a.code}>
                {ruleLabel(a.code)}
              </strong>
              {a.product_id ? <span className="text-ink-muted"> · product {a.product_id}</span> : null}
              {a.code === "NEW_CATEGORY" && fresh.length > 1 ? <span className="text-ink-muted"> · {fresh.length} products</span> : null}
              <span className="block text-ink">{sentence(a)}</span>
              {a.explanation_text ? <span className="block text-ink-muted">{a.explanation_text}</span> : null}
            </p>
          </div>
          {quoteId && onFixed
            ? a.fixes
                // A fix with an address is done elsewhere by a buyer admin (suspend the agent), not by re-quoting.
                .filter((f) => !f.action_url)
                .map((f) => (
                  <div key={f.id} className="flex flex-wrap items-center gap-x-3 gap-y-2">
                    <span className="min-w-0 flex-1 basis-56 text-ink-muted">{f.description}</span>
                    <button className="btn-ghost h-8 !py-0" onClick={() => apply(f.id)} disabled={busy !== null}>
                      {busy === f.id ? "Re-quoting…" : "Apply this correction"}
                    </button>
                  </div>
                ))
            : null}
        </div>
      ))}
      {error ? <ErrorNote error={error} /> : null}
    </div>
  );
}

/** The quotes a correction produced. Each is placed separately; the part still above the usual goes to a person. */
export function CorrectedQuotes({ quotes }: { quotes: Quote[] }) {
  const [orders, setOrders] = useState<Record<string, Order>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);

  const place = async (quote: Quote) => {
    setBusy(quote.id);
    setError(null);
    try {
      const order = await api.placeOrder(quote.id, crypto.randomUUID());
      setOrders((all) => ({ ...all, [quote.id]: order }));
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="card flex flex-col gap-3 p-5">
      <div>
        <h2 className="text-[15px] font-semibold">Corrected quotes</h2>
        <p className="label">The earlier quote and any order waiting on it are cancelled. Nothing is ordered until you place these.</p>
      </div>
      <ul className="flex flex-col gap-2">
        {quotes.map((q) => {
          const order = orders[q.id];
          return (
            <li key={q.id} className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-[var(--radius-tile)] bg-page p-3 text-[13px]">
              <span className="min-w-0 flex-1 basis-52">
                {q.lines.map((l) => `${num(l.quantity_sales_units ?? l.units_needed)} ${l.sales_unit} of ${l.name}`).join(", ")}
                {q.anomalies?.some((a) => a.status === "anomaly") ? (
                  <span className="block text-[12px] text-warn">Still above the usual, so a person signs this part.</span>
                ) : null}
              </span>
              <span className="num shrink-0 font-medium">{eur(q.total, q.currency)}</span>
              {order ? (
                <>
                  <StatusChip status={order.status} />
                  <Link to={`/orders/${order.id}`} className="btn-ghost h-8 !py-0">
                    Open order
                  </Link>
                </>
              ) : (
                <button className="btn-dark h-8 !py-0" onClick={() => place(q)} disabled={busy !== null}>
                  {busy === q.id ? "Placing…" : "Place order"}
                </button>
              )}
            </li>
          );
        })}
      </ul>
      {error ? <ErrorNote error={error} /> : null}
    </div>
  );
}
