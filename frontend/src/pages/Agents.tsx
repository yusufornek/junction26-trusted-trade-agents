import { useState } from "react";
import { Link } from "react-router-dom";
import { KeyRound, ShieldAlert, ShieldCheck } from "lucide-react";
import { api, type AgentMandate } from "../api";
import { Avatar, Progress } from "../charts";
import { Card, Empty, ErrorNote, Spinner, eur, useAsync, when } from "../ui";

const DAY = 86400000;

export default function Agents() {
  const policy = useAsync(() => api.policy(), []);
  const stats = useAsync(() => api.stats(), [], 10000);
  const orders = useAsync(() => api.orders(), [], 10000);

  if (policy.error) return <ErrorNote error={policy.error} />;
  if (policy.loading || !policy.data) return <Spinner />;

  const mandates = policy.data.agents ?? {};
  const seen = new Map((stats.data?.agents_seen ?? []).map((a) => [a.actor.replace(/^agent:/, ""), a]));
  const ids = [...new Set([...Object.keys(mandates), ...seen.keys()])];
  const all = orders.data ?? [];

  return (
    <div className="flex flex-col gap-4">
      <Card className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-[15px] font-semibold">Every agent acts under a mandate</h2>
          <p className="text-[13px] text-ink-muted">
            A mandate names the customers an agent may buy for and how much it may commit on its own. Without one,
            an agent can search and quote, but the gateway refuses its orders.
          </p>
        </div>
        <Link to="/policy" className="btn-ghost">
          Edit limits
        </Link>
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
}: {
  id: string;
  mandate?: AgentMandate;
  seen?: { order_count: number; registered?: boolean; mandate_id?: string | null };
  ceiling: number;
  orders: { status: string; total: number; created_at: string; decided_by_kind?: string | null }[];
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
