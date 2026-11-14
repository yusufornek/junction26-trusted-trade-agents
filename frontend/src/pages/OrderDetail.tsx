import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  Ban,
  Check,
  Copy,
  FileCode2,
  FileSignature,
  Gavel,
  Package,
  Search,
  Settings2,
  Ticket,
  X,
} from "lucide-react";
import { ApiError, api, type Quote, type TimelineStep } from "../api";
import { ANOMALY_CODES, AnomalyNotes, CorrectedQuotes } from "../anomalies";
import { Avatar } from "../charts";
import {
  Card,
  Countdown,
  Empty,
  ErrorNote,
  Spinner,
  StatusChip,
  WarningChips,
  eur,
  num,
  pct,
  RejectionReason,
  ruleLabel,
  splitRejection,
  useAsync,
  when,
} from "../ui";

const STEP_ICON: Record<TimelineStep["step"], typeof Search> = {
  lookup: Search,
  quote: Ticket,
  order: Package,
  decision: Gavel,
  cancellation: Ban,
  system: Settings2,
};

const OUTCOME_WORDS: Record<string, string> = {
  CONFIRMED: "Within the agent's authority",
  PENDING_APPROVAL: "Held for a human",
  REJECTED: "Rejected by policy",
};

// Valio uses 9999-12-31 to mean "no end date"; printing the year 9999 reads like a bug.
const validTo = (iso: string) => (iso.startsWith("9999") ? "no end date" : `valid to ${new Date(iso).toLocaleDateString("en-GB")}`);

const DECIDED_BY: Record<string, string> = {
  agent: "decided by the agent within its authority",
  operator: "decided by a human operator",
  system: "decided by the gateway",
};

export default function OrderDetail() {
  const { id = "" } = useParams();
  const order = useAsync(() => api.order(id), [id], 5000);
  const timeline = useAsync(() => api.timeline(id), [id], 5000);
  // Which codes reject outright, so a rejection by policy can lead with the rule that decided it.
  const policy = useAsync(() => api.policy(), []);
  /** Fresh quotes from an anomaly correction on this order; the order itself is cancelled by it. */
  const [corrected, setCorrected] = useState<Quote[] | null>(null);
  const quoteId = order.data?.quote_id;
  // What else the agent considered: the lookup behind this order's quote still holds the ranked offers.
  const alternatives = useAsync(async () => {
    if (!quoteId) return null;
    const quote = await api.quote(quoteId);
    if (!quote.need_ref) return null;
    return api.lookup(quote.need_ref);
  }, [quoteId]);

  if (order.error) return <ErrorNote error={order.error} />;
  if (order.loading || !order.data) return <Spinner />;
  const o = order.data;
  const [decisive, flagged] = splitRejection(o.policy?.hits ?? [], o.policy?.outcome === "REJECTED" ? policy.data : null);
  const orderedIds = new Set(o.lines.map((l) => l.product_id));
  const rejected = (alternatives.data?.offers ?? []).filter((x) => !orderedIds.has(x.product_id));

  return (
    <div className="flex flex-col gap-4">
      {/* Row from sm up, so a long product name wraps beside the total instead of pushing it onto a row of its own. */}
      <Card className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex min-w-0 gap-3 sm:flex-1">
          <Avatar name={o.agent_id} size={40} />
          <div className="min-w-0">
            <div className="mb-1.5 flex flex-wrap items-center gap-2">
              <StatusChip status={o.status} />
              <span className="label">order {o.id.slice(0, 8)}</span>
              {o.mandate ? (
                <span className="chip bg-chip text-ink-muted" title={`issued by ${o.mandate.issuer}, valid until ${o.mandate.valid_until}`}>
                  mandate {o.mandate.id}
                </span>
              ) : null}
            </div>
            <h2 className="text-[18px] font-semibold leading-snug">
              {o.lines[0]?.name ?? o.id}
              {o.lines.length > 1 ? <span className="text-ink-muted"> + {o.lines.length - 1} more</span> : null}
            </h2>
            <p className="label mt-1">
              placed by {o.agent_id} · {when(o.created_at)}
              {o.decided_by_kind ? ` · ${DECIDED_BY[o.decided_by_kind]}` : ""}
              {o.approver ? ` · signed off by ${o.approver}` : ""}
              {o.self_approved ? " · approved by the person who runs the agent (no second person)" : ""}
            </p>
            {o.comment ? <p className="mt-2 text-[13px] text-ink-muted">“{o.comment}”</p> : null}
          </div>
        </div>
        <div className="shrink-0 sm:text-right">
          <div className="num text-[28px] font-semibold">{eur(o.total, o.currency)}</div>
          <div className="label">
            {o.status === "PENDING_APPROVAL" ? (
              <Countdown to={o.expires_at} />
            ) : o.live_calls ? (
              `${o.live_calls.price + o.live_calls.availability} upstream calls`
            ) : null}
          </div>
        </div>
      </Card>

      {o.status === "CANCELLED" ? (
        <Card className="flex items-start gap-3 !py-3.5">
          <Ban size={16} className="mt-0.5 shrink-0 text-ink-muted" />
          <span className="text-[13px]">
            Cancelled by <strong>{o.cancelled_by ?? "an operator"}</strong>
            {o.cancel_reason ? <span className="text-ink-muted"> — “{o.cancel_reason}”</span> : null}. The stock
            reservation was released.
          </span>
        </Card>
      ) : null}

      {/* minmax(0, …) and min-w-0: long names and hashes truncate inside their cards instead of widening the columns. */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1.5fr)_minmax(0,1fr)]">
        <div className="flex min-w-0 flex-col gap-4">
          <Card className="flex flex-col gap-3">
            <h3 className="text-[15px] font-semibold">Lines</h3>
            {o.lines.map((l) => (
              <div key={l.product_id} className="flex flex-col gap-2 rounded-[var(--radius-tile)] bg-page p-3">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <Link to={`/products/${l.product_id}`} className="block text-[14px] font-medium leading-snug hover:underline">
                      {l.name}
                    </Link>
                    <div className="label">
                      {num(l.quantity_sales_units ?? l.units_needed)} {l.sales_unit} × {eur(l.unit_price, l.currency ?? "EUR")} ·{" "}
                      {eur(l.price_per_l_or_kg, l.currency ?? "EUR")}/{l.size_unit ?? "unit"}
                    </div>
                  </div>
                  <div className="num text-[15px] font-semibold">{eur(l.total, l.currency ?? "EUR")}</div>
                </div>
                <WarningChips warnings={l.warnings} />
                {/* Phone: the three facts flow and wrap whole; from sm up they sit in columns that line up across lines. */}
                <div className="flex flex-wrap gap-x-4 gap-y-1 text-[12px] text-ink-muted *:whitespace-nowrap sm:grid sm:grid-cols-3 sm:gap-2">
                  <span>coverage {pct(l.availability?.coverage)}</span>
                  <span>reliability {l.delivery_reliability == null ? "no data" : pct(l.delivery_reliability)}</span>
                  <span>CO₂ {l.co2_kg_per_kg == null ? "—" : `${num(l.co2_kg_per_kg, 2)} kg/kg`}</span>
                </div>
                {l.provenance ? (
                  <div className="border-t border-line pt-2 text-[11px] text-ink-muted wrap-anywhere">
                    {Object.entries(l.provenance).map(([k, p]) => (
                      <div key={k}>
                        {k}: {p.source} · fetched {when(p.fetched_at)}
                        {p.valid_to ? ` · ${validTo(p.valid_to)}` : ""}
                      </div>
                    ))}
                  </div>
                ) : null}
              </div>
            ))}
          </Card>

          <Card className="flex flex-col gap-4">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <div>
                <h3 className="text-[15px] font-semibold">Timeline</h3>
                <p className="label">From the search to the last decision — one hash-chained record</p>
              </div>
              {timeline.data ? (
                <span className="label">
                  {timeline.data.total_live_calls.price + timeline.data.total_live_calls.availability} Valio calls ·{" "}
                  {timeline.data.total_decision_calls} fit checks in total
                </span>
              ) : null}
            </div>
            {timeline.error ? (
              <ErrorNote error={timeline.error} />
            ) : timeline.loading && !timeline.data ? (
              <Spinner />
            ) : !timeline.data?.steps.length ? (
              <Empty>No steps recorded for this order.</Empty>
            ) : (
              <ol className="relative flex flex-col">
                {timeline.data.steps.map((s, i, all) => {
                  const Icon = STEP_ICON[s.step] ?? Settings2;
                  const human = s.actor.startsWith("human");
                  return (
                    <li key={(s.hash ?? "") + i} className="relative flex gap-3 pb-5 last:pb-0">
                      {i < all.length - 1 ? (
                        <span className="absolute left-[15px] top-8 bottom-0 w-px bg-line" aria-hidden />
                      ) : null}
                      <span
                        className={`relative grid size-8 shrink-0 place-items-center rounded-full border ${
                          human ? "border-positive/30 bg-positive-soft text-positive" : "border-line bg-surface text-ink-muted"
                        }`}
                      >
                        <Icon size={14} />
                      </span>
                      <div className="min-w-0 flex-1 pt-0.5">
                        <div className="flex flex-wrap items-baseline justify-between gap-x-3">
                          <span className="text-[13px] font-medium">{s.action.replaceAll("_", " ").toLowerCase()}</span>
                          <span className="text-[11px] text-ink-soft">{when(s.ts)}</span>
                        </div>
                        <div className="text-[12px] text-ink-muted">{s.actor}</div>
                        {s.summary ? <p className="mt-1 text-[13px]">{s.summary}</p> : null}
                        {s.live_calls || s.decision_calls || s.hash ? (
                          <div className="mt-1 flex flex-wrap gap-x-3 text-[11px] text-ink-muted">
                            {s.live_calls ? <span>{s.live_calls.price + s.live_calls.availability} Valio calls</span> : null}
                            {s.decision_calls ? <span>{s.decision_calls} fit checks</span> : null}
                            {s.hash ? <code title={s.hash}>{s.hash.slice(0, 18)}…</code> : null}
                          </div>
                        ) : null}
                      </div>
                    </li>
                  );
                })}
              </ol>
            )}
          </Card>

          <Card className="flex flex-col gap-3">
            <div>
              <h3 className="text-[15px] font-semibold">What else the agent considered</h3>
              <p className="label">
                {alternatives.data
                  ? `${rejected.length} other offers from the same lookup, ranked but not ordered`
                  : "No lookup is linked to this order"}
              </p>
            </div>
            {alternatives.loading ? (
              <Spinner />
            ) : !rejected.length ? (
              <Empty>No alternatives recorded.</Empty>
            ) : (
              <ul className="flex flex-col gap-1.5">
                {rejected.slice(0, 6).map((x) => (
                  <li
                    key={x.product_id}
                    className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-[var(--radius-tile)] border border-line p-2.5 text-[13px]"
                  >
                    {x.rank_label ? <span className="chip bg-chip text-ink-muted">{x.rank_label}</span> : null}
                    {/* Two lines, not one: cut to one line on a phone, sibling products read the same ("Valio Hyvä suomalainen…"). */}
                    <Link to={`/products/${x.product_id}`} className="min-w-0 flex-1 line-clamp-2 hover:underline" title={x.name}>
                      {x.name}
                    </Link>
                    <span className="num text-ink-muted">{eur(x.purchase_total ?? x.total, x.currency ?? "EUR")}</span>
                    {/* An empty full-width row would still add the row gap under every warning-free offer. */}
                    {x.warnings?.length ? (
                      <div className="w-full">
                        <WarningChips warnings={x.warnings} />
                      </div>
                    ) : null}
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>

        <div className="flex min-w-0 flex-col gap-4">
          <Card className="flex flex-col gap-3">
            <div>
              <h3 className="text-[15px] font-semibold">Policy at placement</h3>
              <p className="label">What the rules decided when the agent placed it</p>
            </div>
            <div className="text-[18px] font-semibold">{OUTCOME_WORDS[o.policy?.outcome ?? ""] ?? "—"}</div>
            {/* The placement outcome is history; say plainly how it was resolved so "held" is never read as "still pending". */}
            {o.policy?.outcome === "PENDING_APPROVAL" && o.status !== "PENDING_APPROVAL" ? (
              <p className="text-[13px] text-ink-muted">
                then{" "}
                {o.status === "CONFIRMED"
                  ? `approved by ${o.approver ?? "an operator"}`
                  : o.status === "REJECTED"
                    ? `rejected by ${o.approver ?? "an operator"}`
                    : o.status === "EXPIRED"
                      ? "expired without a decision"
                      : `cancelled by ${o.cancelled_by ?? "an operator"}`}
              </p>
            ) : null}
            <AnomalyNotes
              anomalies={o.anomalies}
              quoteId={o.status === "PENDING_APPROVAL" ? o.quote_id : undefined}
              onFixed={(quotes) => {
                setCorrected(quotes);
                order.reload();
              }}
            />
            {corrected ? <CorrectedQuotes quotes={corrected} /> : null}
            {o.policy?.hits.length ? (
              <>
                {/* A rejection leads with the rule that decided it; the other rules would only have asked for a human. */}
                {decisive.map((h) => (
                  <RejectionReason key={h.rule + h.detail} hit={h} />
                ))}
                {flagged.length ? (
                  <div className="flex flex-col gap-2">
                    {decisive.length ? <span className="label">Also flagged</span> : null}
                    <ul className="flex flex-col gap-2 text-[13px]">
                      {flagged
                        // Shown above as a sentence; the hit repeats it with the band as a raw dictionary.
                        .filter((h) => !ANOMALY_CODES.has(h.rule))
                        .map((h) => (
                        <li key={h.rule + h.detail} className="rounded-[var(--radius-tile)] bg-warn-soft p-2.5">
                          <strong className="text-warn" title={h.rule}>
                            {ruleLabel(h.rule)}
                          </strong>
                          <div className="text-ink-muted">{h.detail}</div>
                        </li>
                      ))}
                    </ul>
                  </div>
                ) : null}
              </>
            ) : (
              <p className="label">No rule matched — inside the agent's own authority.</p>
            )}
            <div className="flex flex-wrap gap-2">
              <Link to="/policy" className="btn-ghost">
                See the rules
              </Link>
              {o.status === "PENDING_APPROVAL" ? (
                <Link to="/approvals" className="btn-dark">
                  Decide in the inbox
                </Link>
              ) : null}
            </div>
          </Card>

          {o.status === "CONFIRMED" ? <CancelCard orderId={o.id} onDone={order.reload} /> : null}

          <ProofCard
            orderId={o.id}
            commitment={o.commitment}
            quoteDigest={o.quote_signature_digest}
            receipt={o.receipt}
          />

          <Card className="flex flex-col gap-2 text-[12px] text-ink-muted wrap-anywhere">
            <h3 className="text-[15px] font-semibold text-ink">References</h3>
            <div>customer {o.customer_id ?? "—"}</div>
            <div>quote {o.quote_id}</div>
            <div>idempotency {o.idempotency_key}</div>
            <div>price lock until {new Date(o.expires_at).toLocaleString("en-GB")}</div>
            {o.decided_at ? <div>decided {new Date(o.decided_at).toLocaleString("en-GB")}</div> : null}
            {o.next_action === "WAIT_FOR_APPROVAL" ? <div>the ordering agent is told to wait for approval</div> : null}
          </Card>
        </div>
      </div>
    </div>
  );
}

/** The one place a human can undo something the agent committed on its own. */
function CancelCard({ orderId, onDone }: { orderId: string; onDone: () => void }) {
  const [open, setOpen] = useState(false);
  const [by, setBy] = useState("Ada");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const cancel = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.cancel(orderId, by, reason || "cancelled in console");
      setOpen(false);
      onDone();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  const tooLate = error instanceof ApiError && error.code === "CUTOFF_PASSED";

  return (
    <Card className="flex flex-col gap-3">
      <div>
        <h3 className="text-[15px] font-semibold">Cancel this order</h3>
        <p className="label">Possible until the supplier's order cut-off; the stock reservation is released.</p>
      </div>
      {!open ? (
        <button className="btn-danger self-start" onClick={() => setOpen(true)}>
          <X size={14} /> Cancel order
        </button>
      ) : (
        <div className="flex flex-col gap-2">
          <label className="flex flex-col gap-1.5">
            <span className="label">Cancelling as</span>
            <input className="field" value={by} onChange={(e) => setBy(e.target.value)} />
          </label>
          <label className="flex flex-col gap-1.5">
            <span className="label">Reason (stored in the audit trail)</span>
            <input className="field" placeholder="Event called off" value={reason} onChange={(e) => setReason(e.target.value)} />
          </label>
          <div className="flex gap-2">
            <button className="btn-danger" onClick={cancel} disabled={busy || !by.trim()}>
              {busy ? "Cancelling…" : "Confirm cancellation"}
            </button>
            <button className="btn-ghost" onClick={() => setOpen(false)} disabled={busy}>
              Keep it
            </button>
          </div>
        </div>
      )}
      {tooLate ? (
        <div className="rounded-[var(--radius-tile)] bg-warn-soft p-3 text-[12px] text-warn">
          Too late: the supplier's order cut-off has passed, so the gateway cannot honour a cancellation.
        </div>
      ) : error ? (
        <ErrorNote error={error} />
      ) : null}
    </Card>
  );
}

/** Hashes and signatures an outside party can check without trusting this console. */
function ProofCard({
  orderId,
  commitment,
  quoteDigest,
  receipt,
}: {
  orderId: string;
  commitment?: string;
  quoteDigest?: string | null;
  receipt?: string | null;
}) {
  const [busy, setBusy] = useState(false);
  const [xml, setXml] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);

  const loadUbl = async () => {
    setBusy(true);
    setError(null);
    try {
      setXml(await api.ubl(orderId));
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  const download = () => {
    if (!xml) return;
    const url = URL.createObjectURL(new Blob([xml], { type: "application/xml" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = `order-${orderId.slice(0, 8)}.ubl.xml`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <Card className="flex flex-col gap-3">
      <div className="flex items-center gap-2">
        <FileSignature size={16} />
        <h3 className="text-[15px] font-semibold">Proof</h3>
      </div>
      <p className="label">Verifiable without trusting this screen — check them against the published keys.</p>
      <Proof label="Price commitment" value={commitment} />
      <Proof label="Quote signature digest" value={quoteDigest} />
      <Proof label="Signed receipt (JWS)" value={receipt} />
      <div className="flex flex-wrap gap-2">
        <Link to="/trust" className="btn-ghost">
          Verify keys
        </Link>
        {xml ? (
          <button className="btn-dark" onClick={download}>
            <FileCode2 size={14} /> Download UBL
          </button>
        ) : (
          <button className="btn-ghost" onClick={loadUbl} disabled={busy}>
            <FileCode2 size={14} /> {busy ? "Building…" : "UBL order document"}
          </button>
        )}
      </div>
      {error ? <ErrorNote error={error} /> : null}
      {xml ? (
        <pre className="max-h-64 overflow-auto rounded-[var(--radius-tile)] bg-page p-3 text-[11px] leading-relaxed">{xml}</pre>
      ) : null}
    </Card>
  );
}

function Proof({ label, value }: { label: string; value?: string | null }) {
  const [copied, setCopied] = useState(false);
  if (!value) return null;
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard can be blocked outside a secure context; the value stays visible to select by hand.
    }
  };
  return (
    <div className="flex items-center gap-2 rounded-[var(--radius-tile)] border border-line px-3 py-2">
      <div className="min-w-0 flex-1">
        <div className="text-[11px] text-ink-muted">{label}</div>
        {/* Wraps (three lines, then scrolls) so the whole value can be read and compared, not just its first half. */}
        <code className="block max-h-[4.5em] overflow-y-auto text-[12px] leading-[1.5] break-all" title={value}>
          {value}
        </code>
      </div>
      <button
        className="grid size-9 shrink-0 place-items-center rounded-full text-ink-muted hover:bg-page"
        onClick={copy}
        title="Copy"
        aria-label={`Copy ${label}`}
      >
        {copied ? <Check size={13} className="text-positive" /> : <Copy size={13} />}
      </button>
    </div>
  );
}
