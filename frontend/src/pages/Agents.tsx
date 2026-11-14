import { useState } from "react";
import { Link } from "react-router-dom";
import { BadgeCheck, CircleDashed, Gauge, KeyRound, ShieldAlert, ShieldCheck, Undo2, UserCheck } from "lucide-react";
import { ApiError, api, type AgentMandate, type AutonomyLevel, type AutonomyPolicy } from "../api";
import { Avatar, Progress } from "../charts";
import { Card, Empty, ErrorNote, Spinner, eur, pct, useAsync, when } from "../ui";

const DAY = 86400000;

const LEVEL = {
  approval_required: { label: "approval first", style: "bg-warn-soft text-warn" },
  autonomous_within_limits: { label: "autonomous within limits", style: "bg-positive-soft text-positive" },
} satisfies Record<AutonomyLevel, { label: string; style: string }>;

export default function Agents() {
  const policy = useAsync(() => api.policy(), []);
  const stats = useAsync(() => api.stats(), [], 10000);
  const orders = useAsync(() => api.orders(), [], 10000);

  if (policy.error) return <ErrorNote error={policy.error} />;
  if (policy.loading || !policy.data) return <Spinner />;

  const mandates = policy.data.agents ?? {};
  const autonomy = policy.data.autonomy ?? {};
  const seen = new Map((stats.data?.agents_seen ?? []).map((a) => [a.actor.replace(/^agent:/, ""), a]));
  const ids = [...new Set([...Object.keys(mandates), ...seen.keys()])];
  const all = orders.data ?? [];

  return (
    <div className="flex flex-col gap-4">
      <Card className="flex flex-col gap-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="text-[15px] font-semibold">Autonomy is earned, not assumed</h2>
            <p className="text-[13px] text-ink-muted">
              Every agent starts with a human signing each order. Once its record shows it buys what the kitchen
              would have bought, the buyer can let it confirm alone — within the mandate's limits, and revocable in
              one step.
            </p>
          </div>
          <Link to="/policy" className="btn-ghost">
            Edit limits
          </Link>
        </div>
        <AutonomyPath policy={autonomy} />
      </Card>

      {!ids.length ? (
        <Card>
          <Empty>No agent has connected or been granted a mandate yet.</Empty>
        </Card>
      ) : (
        // grid-cols-1, not the implicit auto column: that one grows to the limit tiles' nowrap values and widens the page on phones.
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          {ids.map((id) => (
            <AgentCard
              key={id}
              id={id}
              mandate={mandates[id]}
              seen={seen.get(id)}
              ceiling={policy.data!.approval.max_auto_total_eur}
              orders={all.filter((o) => o.agent_id === id)}
              autonomy={autonomy}
              onChanged={policy.reload}
            />
          ))}
        </div>
      )}

      <RevokeCard />
    </div>
  );
}

function AgentCard({
  id,
  mandate,
  seen,
  ceiling,
  orders,
  autonomy,
  onChanged,
}: {
  id: string;
  mandate?: AgentMandate;
  seen?: { order_count: number; registered?: boolean; mandate_id?: string | null };
  ceiling: number;
  orders: { status: string; total: number; created_at: string; decided_by_kind?: string | null }[];
  autonomy: AutonomyPolicy;
  onChanged: () => void;
}) {
  const registered = mandate != null || seen?.registered === true;
  // Daily limit counts what the agent confirmed on its own in the last 24 h — human-approved orders don't use it up.
  const autoToday = orders
    .filter((o) => o.status === "CONFIRMED" && o.decided_by_kind === "agent" && Date.now() - Date.parse(o.created_at) < DAY)
    .reduce((a, o) => a + o.total, 0);
  const daily = mandate?.max_auto_daily_total_eur ?? 0;
  const last = orders.map((o) => o.created_at).sort().at(-1);

  return (
    <Card className="flex flex-col gap-4">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-center gap-3">
          <Avatar name={id} size={42} />
          <div className="min-w-0">
            {/* The identity and its note wrap on a phone rather than lose their ends. */}
            <div className="text-[15px] font-semibold wrap-break-word">{id}</div>
            <div className="text-[12px] text-ink-muted">{mandate?.note || (registered ? "registered agent" : "seen, never granted")}</div>
          </div>
        </div>
        {registered ? (
          <span className="chip bg-positive-soft text-positive">
            <ShieldCheck size={12} /> mandate
          </span>
        ) : (
          <span className="chip bg-negative-soft text-negative">
            <ShieldAlert size={12} /> no mandate
          </span>
        )}
      </div>

      {mandate ? (
        <>
          {/* Tiles share three subgrid rows (label, value, hint): a label that wraps on a phone keeps the values level. */}
          <div className="grid grid-cols-3 gap-x-2">
            <Fact label="Customers" value={mandate.customer_ids.join(", ") || "—"} />
            <Fact label="Per order, alone" value={eur(mandate.max_auto_total_eur ?? ceiling)} hint={mandate.max_auto_total_eur == null ? "policy ceiling" : undefined} />
            <Fact label="Per day, alone" value={eur(daily)} />
          </div>
          <div className="flex flex-col gap-1.5">
            <div className="flex justify-between gap-3 text-[12px]">
              <span className="text-ink-muted">Confirmed on its own, last 24 h</span>
              <span className="num font-medium">
                {eur(autoToday)} <span className="text-ink-muted">of {eur(daily)}</span>
              </span>
            </div>
            <Progress value={daily ? autoToday / daily : 0} tone={daily && autoToday / daily > 0.8 ? "warn" : "positive"} />
          </div>
          <AutonomyPanel id={id} mandate={mandate} policy={autonomy} onChanged={onChanged} />
        </>
      ) : (
        <div className="rounded-[var(--radius-tile)] bg-negative-soft p-3 text-[12px] text-negative">
          It can look and quote. Its orders answer <code>403 AGENT_NOT_REGISTERED</code> and its plan previews
          show “rejected” until a mandate is granted in the policy.
        </div>
      )}

      <div className="flex flex-wrap gap-x-4 gap-y-1 border-t border-line pt-3 text-[12px] text-ink-muted">
        <span>
          <strong className="num text-ink">{seen?.order_count ?? orders.length}</strong> orders
        </span>
        <span>last activity {last ? when(last) : "never"}</span>
        {seen?.mandate_id ? <span>credential {seen.mandate_id}</span> : null}
      </div>
    </Card>
  );
}

/** Valio Aimo's own expectation (Junction Discord, 14.11): human approval at first, autonomy once reliability is proven. */
function AutonomyPath({ policy }: { policy: AutonomyPolicy }) {
  const p = policy.promotion ?? {};
  const steps = [
    {
      icon: UserCheck,
      title: "Approval first",
      body:
        (policy.default ?? "approval_required") === "approval_required"
          ? "Every new agent starts here: a person signs each order, even inside the limits."
          : "The policy default currently skips this step for new agents.",
    },
    {
      icon: Gauge,
      title: "Track record",
      body: `${p.min_approved_unchanged ?? 5} orders approved unchanged, at most ${pct(p.max_rejection_share ?? 0.1)} rejected, ${
        p.days_since_last_rejection_or_refusal ?? 7
      } days without a rejection or refusal.`,
    },
    {
      icon: BadgeCheck,
      title: "Autonomy within limits",
      body: "Confirms alone up to the mandate's limits. A rejection suggests a review; one step returns it to approval first.",
    },
  ];
  return (
    <ol className="grid grid-cols-1 gap-2 md:grid-cols-3">
      {steps.map(({ icon: Icon, title, body }, i) => (
        <li key={title} className="flex gap-3 rounded-[var(--radius-tile)] bg-page p-3">
          <span className="grid size-8 shrink-0 place-items-center rounded-full bg-brand-soft text-brand">
            <Icon size={15} />
          </span>
          <div className="min-w-0">
            <div className="text-[13px] font-semibold">
              <span className="text-ink-muted">{i + 1}.</span> {title}
            </div>
            <p className="mt-0.5 text-[12px] text-ink-muted">{body}</p>
          </div>
        </li>
      ))}
    </ol>
  );
}

const duration = (s: number | null) =>
  s == null ? "—" : s < 3600 ? `${Math.max(1, Math.round(s / 60))} min` : `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min`;

function AutonomyPanel({
  id,
  mandate,
  policy,
  onChanged,
}: {
  id: string;
  mandate: AgentMandate;
  policy: AutonomyPolicy;
  onChanged: () => void;
}) {
  const record = useAsync(() => api.trackRecord(id), [id, mandate.autonomy]);
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  // A mandate without its own level inherits the policy default — the stricter, approval-first one unless changed.
  const effective: AutonomyLevel = mandate.autonomy ?? policy.default ?? "approval_required";
  const target: AutonomyLevel = effective === "approval_required" ? "autonomous_within_limits" : "approval_required";
  const need = policy.promotion?.min_approved_unchanged ?? 5;
  const r = record.data;
  const eligible = r?.autonomy_recommendation.eligible ?? false;
  // The same thresholds read differently once autonomy is granted: falling short is a reason to review, not to wait.
  const autonomous = effective === "autonomous_within_limits";
  const verdict = eligible
    ? { text: autonomous ? "The record still supports its autonomy" : "Ready: the record meets every threshold", style: "bg-positive-soft text-positive" }
    : autonomous
      ? { text: "Review suggested: this record would not earn autonomy today", style: "bg-warn-soft text-warn" }
      : { text: "Not yet recommended", style: "bg-page text-ink-muted" };

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.setAutonomy(id, target, reason.trim());
      setOpen(false);
      setReason("");
      onChanged();
      record.reload();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-3 rounded-[var(--radius-tile)] border border-line p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-[13px] font-semibold">Autonomy</span>
        <span className="flex flex-wrap items-center gap-1.5">
          <span className={`chip ${LEVEL[effective].style}`}>{LEVEL[effective].label}</span>
          {!mandate.autonomy ? <span className="text-[11px] text-ink-muted">policy default</span> : null}
        </span>
      </div>

      {record.error ? (
        <ErrorNote error={record.error} />
      ) : !r ? (
        <Spinner label="Reading the track record" />
      ) : (
        <>
          <div className="flex flex-col gap-1.5">
            <div className="flex justify-between gap-3 text-[12px]">
              <span className="text-ink-muted">Approved unchanged, last {r.window_days} days</span>
              <span className="num font-medium">
                {r.approved_unchanged} <span className="text-ink-muted">{autonomous ? `· threshold ${need}` : `of ${need} needed`}</span>
              </span>
            </div>
            <Progress value={r.approved_unchanged / Math.max(need, 1)} tone={r.approved_unchanged >= need ? "positive" : "brand"} />
          </div>

          <div className="grid grid-cols-3 gap-x-2">
            <Fact label="Orders placed" value={String(r.orders_placed)} />
            <Fact label="Rejected" value={`${r.rejected} · ${pct(r.rejection_share)}`} />
            <Fact label="Time to sign" value={duration(r.average_approval_time_seconds)} hint="average" />
          </div>

          {r.expired || r.cancellations || r.evidence_changes || r.hard_constraint_refusals ? (
            <p className="text-[12px] text-ink-muted">
              Also: {r.expired} expired · {r.cancellations} cancelled · {r.evidence_changes} evidence changes ·{" "}
              {r.hard_constraint_refusals} hard-constraint refusals
            </p>
          ) : null}

          <div className={`rounded-[var(--radius-tile)] p-3 text-[12px] ${verdict.style}`}>
            <div className="flex items-center gap-1.5 font-semibold">
              {eligible ? <BadgeCheck size={14} /> : <CircleDashed size={14} />}
              {verdict.text}
            </div>
            {r.autonomy_recommendation.reasons.length ? (
              <ul className="mt-1.5 flex list-disc flex-col gap-0.5 pl-5 text-ink-muted">
                {r.autonomy_recommendation.reasons.map((reason) => (
                  <li key={reason}>{reason}</li>
                ))}
              </ul>
            ) : null}
            <p className="mt-1.5 text-ink-muted">A recommendation only — a person decides.</p>
          </div>
        </>
      )}

      {!open ? (
        <div>
          {effective === "approval_required" ? (
            <button className="btn-dark" onClick={() => setOpen(true)}>
              <BadgeCheck size={14} /> Grant autonomy
            </button>
          ) : (
            <button className="btn-ghost" onClick={() => setOpen(true)}>
              <Undo2 size={14} /> Return to approval first
            </button>
          )}
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          {target === "autonomous_within_limits" && r && !eligible ? (
            <div className="rounded-[var(--radius-tile)] bg-warn-soft p-2.5 text-[12px] text-warn">
              The record does not meet the thresholds yet. You can still grant autonomy; your reason and this record
              are stored in the audit trail.
            </div>
          ) : null}
          <label className="flex flex-col gap-1.5">
            <span className="label">Why (stored in the audit trail with the record)</span>
            <input
              className="field"
              placeholder={target === "autonomous_within_limits" ? "Pilot orders all approved unchanged" : "Rejected order needs a review"}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          </label>
          <div className="flex flex-wrap gap-2">
            <button className={target === "autonomous_within_limits" ? "btn-dark" : "btn-danger"} onClick={submit} disabled={busy || !reason.trim()}>
              {busy ? "Saving…" : target === "autonomous_within_limits" ? "Confirm autonomy" : "Confirm return"}
            </button>
            <button className="btn-ghost" onClick={() => setOpen(false)} disabled={busy}>
              Cancel
            </button>
          </div>
        </div>
      )}
      {error ? (
        error instanceof ApiError && error.status === 404 ? (
          <div className="rounded-[var(--radius-tile)] bg-negative-soft p-2.5 text-[12px] text-negative">
            This agent is not in your organisation, so its autonomy cannot be changed from here.
          </div>
        ) : (
          <ErrorNote error={error} />
        )
      ) : null}
    </div>
  );
}

function Fact({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="row-span-3 grid grid-rows-subgrid rounded-[var(--radius-tile)] border border-line p-2.5">
      <div className="text-[11px] text-ink-muted">{label}</div>
      <div className="num mt-0.5 truncate text-[14px] font-medium" title={value}>
        {value}
      </div>
      {hint ? <div className="text-[11px] text-ink-muted">{hint}</div> : null}
    </div>
  );
}

/** Buyer-issued mandates are W3C credentials the gateway cannot edit — only revoke, by presenting the credential. */
function RevokeCard() {
  const [jwt, setJwt] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const revoke = async () => {
    setBusy(true);
    setError(null);
    setDone(false);
    try {
      await api.revokeMandate(jwt.trim());
      setDone(true);
      setJwt("");
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card className="flex flex-col gap-3">
      <div className="flex items-start gap-2">
        <KeyRound size={16} className="mt-0.5 shrink-0" />
        <div>
          <h3 className="text-[14px] font-semibold">Revoke a buyer-issued mandate</h3>
          <p className="label">
            A buyer can hand its agent a signed credential (VC-JWT) instead of a policy entry. Paste it to revoke it —
            the agent's next order is refused.
          </p>
        </div>
      </div>
      <textarea
        className="field min-h-20 font-mono text-[12px]"
        placeholder="eyJhbGciOiJFZERTQSIsInR5cCI6InZjK2p3dCJ9…"
        value={jwt}
        onChange={(e) => setJwt(e.target.value)}
      />
      <div className="flex items-center gap-3">
        <button className="btn-danger" onClick={revoke} disabled={busy || !jwt.trim()}>
          {busy ? "Revoking…" : "Revoke credential"}
        </button>
        {done ? <span className="chip bg-positive-soft text-positive">revoked</span> : null}
      </div>
      {error ? <ErrorNote error={error} /> : null}
    </Card>
  );
}
