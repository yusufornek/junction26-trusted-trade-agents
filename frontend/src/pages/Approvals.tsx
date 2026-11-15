import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, ApiError, signedInEmail } from "../api";
import { ANOMALY_CODES, AnomalyNotes } from "../anomalies";
import { Avatar, Ring } from "../charts";
import { Card, Countdown, Empty, ErrorNote, LiveDot, Spinner, WarningChips, eur, num, pct, ruleLabel, useAsync, when } from "../ui";

export default function Approvals() {
  const { data, error, loading, reload } = useAsync(() => api.approvals(), [], 5000);
  // Approval links in webhooks and messages name the order: /approvals/{order_id}?revision=N.
  const { id: linked } = useParams();
  const [approver, setApprover] = useState(() => signedInEmail() || "Ada");
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<unknown>(null);

  const act = async (orderId: string, revision: number, kind: "approve" | "reject") => {
    setBusy(orderId + kind);
    setActionError(null);
    try {
      if (kind === "approve") {
        await api.approve(orderId, approver, comment || "Approved in console", revision);
      } else {
        await api.reject(orderId, approver, comment || "Rejected in console");
      }
      setComment("");
      reload();
    } catch (e) {
      setActionError(e);
      if (e instanceof ApiError && e.code === "EVIDENCE_CHANGED") await reload();
    } finally {
      setBusy(null);
    }
  };

  if (error) return <ErrorNote error={error} />;
  if (loading) return <Spinner />;

  return (
    <div className="flex flex-col gap-4">
      <Card className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1.5">
          <span className="label">Signing as</span>
          <input className="field w-48" value={approver} onChange={(e) => setApprover(e.target.value)} />
        </label>
        <label className="flex min-w-60 flex-1 flex-col gap-1.5">
          <span className="label">Comment (stored in the audit trail)</span>
          <input
            className="field"
            placeholder="Checked budget and delivery date"
            value={comment}
            onChange={(e) => setComment(e.target.value)}
          />
        </label>
        {/* Same height as a field, so the chip sits on the inputs' centre line rather than their bottom edge. */}
        <div className="flex h-9 items-center">
          <LiveDot label={`${data?.length ?? 0} waiting`} />
        </div>
      </Card>

      {actionError ? <ErrorNote error={actionError} /> : null}
      {actionError instanceof ApiError && actionError.code === "EVIDENCE_CHANGED" ? (
        <Card>
          <p className="text-[13px] font-medium">Evidence changed. Review the refreshed order before approving again.</p>
          <ul className="mt-2 list-disc pl-5 text-[13px] text-ink-muted">
            {actionError.data?.changes?.map((change, index) => <li key={index}>{change}</li>)}
          </ul>
        </Card>
      ) : null}

      {linked && data && !data.some((x) => x.order.id === linked) ? (
        <Card className="flex flex-wrap items-center gap-3">
          <p className="min-w-0 flex-1 basis-56 text-[13px]">The order from your link is no longer waiting for a decision.</p>
          <Link to={`/orders/${linked}`} className="btn-ghost">
            Open that order
          </Link>
        </Card>
      ) : null}

      {!data?.length ? (
        <Card>
          <Empty>Inbox is clear. Every order so far satisfied the policy without a human.</Empty>
        </Card>
      ) : (
        // The linked order comes first and is outlined, so the person lands on the decision they were asked for.
        [...data].sort((a, b) => Number(b.order.id === linked) - Number(a.order.id === linked)).map(({ order, quote }) => (
          <Card key={order.id} className={`flex flex-col gap-4 ${order.id === linked ? "ring-2 ring-brand" : ""}`}>
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="flex min-w-0 gap-3">
                <Avatar name={order.agent_id} size={38} />
                <div className="min-w-0">
                  <h2 className="text-[16px] font-semibold leading-snug">{order.lines[0]?.name ?? order.id}</h2>
                  <p className="label mt-0.5">
                    requested by {order.agent_id} · {when(order.created_at)} · deliver by {quote.deliver_by}
                  </p>
                </div>
              </div>
              {/* ml-auto keeps the amount on the right edge when it wraps under the title on a phone. */}
              <div className="ml-auto text-right">
                <div className="num text-[24px] font-semibold">{eur(order.total, order.currency)}</div>
                <div className="label">
                  price locked · <Countdown to={order.expires_at} />
                </div>
              </div>
            </div>

            <ul className="flex flex-col gap-1.5">
              {order.lines.map((l) => (
                <li
                  key={l.product_id}
                  className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-[var(--radius-tile)] bg-page p-3 text-[13px]"
                >
                  {l.availability?.coverage != null ? (
                    <Ring
                      value={l.availability.coverage}
                      size={30}
                      tone={l.availability.coverage >= 1 ? "positive" : "warn"}
                    >
                      {pct(l.availability.coverage).replace("%", "")}
                    </Ring>
                  ) : null}
                  <span className="font-medium">
                    {num(l.quantity_sales_units ?? l.units_needed)} {l.sales_unit}
                  </span>
                  <span className="text-ink-muted">at {eur(l.unit_price, l.currency)} each</span>
                  <span className="text-ink-muted">#{l.product_id}</span>
                  <span className="ml-auto num font-medium">{eur(l.total, l.currency)}</span>
                  <div className="w-full">
                    <WarningChips warnings={l.warnings} />
                  </div>
                </li>
              ))}
            </ul>

            <AnomalyNotes anomalies={order.anomalies} />
            {order.policy?.hits.length ? (
              <div className="rounded-[var(--radius-tile)] border border-warn/30 bg-warn-soft p-3">
                <div className="mb-1 text-[12px] font-semibold text-warn">Why this needs you</div>
                <ul className="flex flex-col gap-1 text-[13px] text-ink">
                  {order.policy.hits
                    .filter((h) => !ANOMALY_CODES.has(h.rule))
                    .map((h) => (
                    <li key={h.rule}>
                      <strong title={h.rule}>{ruleLabel(h.rule)}</strong> — <span className="text-ink-muted">{h.detail}</span>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}

            <div className="flex flex-wrap items-center gap-2 border-t border-line pt-3">
              <button
                className="btn-dark"
                onClick={() => act(order.id, order.evidence_revision, "approve")}
                disabled={busy === order.id + "approve"}
              >
                {busy === order.id + "approve" ? "Approving…" : `Approve ${eur(order.total, order.currency)}`}
              </button>
              <button
                className="btn-danger"
                onClick={() => act(order.id, order.evidence_revision, "reject")}
                disabled={busy === order.id + "reject"}
              >
                Reject
              </button>
              <Link to={`/orders/${order.id}`} className="btn-ghost ml-auto">
                Full detail
              </Link>
            </div>
          </Card>
        ))
      )}
    </div>
  );
}
