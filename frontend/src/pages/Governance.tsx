import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { Avatar, Progress } from "../charts";
import { governanceApi, isConfirmation, loadBoard, type Alert, type BudgetLimits, type GovernedAgent, type Tenant } from "../governance";
import { Card, Empty, ErrorNote, Spinner, eur, num, ruleLabel, useAsync, when } from "../ui";

const CLIENTS: Record<string, string> = {
  claude_desktop: "Claude Desktop", claude_code: "Claude Code", cursor: "Cursor", vscode: "VS Code",
  chatgpt: "ChatGPT", mcp_other: "MCP client", rest: "REST client",
};
const clientName = (code: string) => CLIENTS[code] ?? ruleLabel(code);
const plural = (n: number, word: string) => `${num(n)} ${word}${n === 1 ? "" : "s"}`;
/** Audit actors are "agent:x", "human:y", "system[:part]", a user id, or the built-in operator account. */
const who = (actor: string | null) =>
  !actor ? "someone" : actor === "legacy-operator" ? "the operator" : actor.startsWith("system") ? "the gateway" : actor.replace(/^(agent|human):/, "");

function alertText(a: Alert): string {
  const budget = `${a.period === "month" ? "monthly" : "daily"} budget (${eur(a.used_eur)} of ${eur(a.limit_eur)})`;
  switch (a.type) {
    case "budget.80": return `${a.agent_id} has used over 80% of its ${budget}.`;
    case "budget.100": return `${a.agent_id} has reached its ${budget}.`;
    case "refusal.burst": return `${a.agent_id} was refused ${a.count} times within ${a.window_minutes} minutes.`;
    case "activity.outside_hours": return `${a.agent_id} tried to act outside its ordering hours.`;
    case "connection.new_client": return `A new client (${clientName(a.client ?? "")}) connected as ${a.agent_id}.`;
    case "connection.new_address": return `${a.agent_id} connected from a new address (${a.address}).`;
    case "policy.change_pending": return `A change to the limits of ${a.agent_id} is waiting for review.`;
    default: return `${ruleLabel(a.type.replaceAll(".", "_"))}: ${a.agent_id}.`;
  }
}

function Alerts({ alerts }: { alerts: Alert[] }) {
  const [all, setAll] = useState(false);
  if (!alerts.length) return null;
  return (
    <Card className="flex flex-col gap-3 border-warn/30">
      <h2 className="text-[16px] font-semibold">Needs attention</h2>
      <ul className="flex flex-col gap-1.5">
        {(all ? alerts : alerts.slice(0, 5)).map((a) => {
          const serious = a.type === "budget.100" || a.type === "refusal.burst";
          return (
            <li key={a.id} className={`flex flex-wrap items-baseline gap-x-3 gap-y-1 rounded-[var(--radius-tile)] p-3 text-[13px] ${serious ? "bg-negative-soft" : "bg-warn-soft"}`}>
              <span className="min-w-0 flex-1 basis-56 break-words">{alertText(a)}</span>
              <span className="text-ink-muted">{when(a.created_at)}</span>
              <Link to={`/changes?agent=${encodeURIComponent(a.agent_id)}`} className="font-medium text-brand underline">
                See its limits
              </Link>
            </li>
          );
        })}
      </ul>
      {alerts.length > 5 ? (
        <button className="btn-ghost self-start" onClick={() => setAll(!all)}>
          {all ? "Show the latest 5" : `Show all ${alerts.length} alerts`}
        </button>
      ) : null}
    </Card>
  );
}

/** The gateway asks for a confirmation token before a governance change; api.call() completes that step, so the
 *  submitted reason form is the person's confirmation. The token branch stays for callers that do not. */
function ReasonForm({ title, effect, verb, danger, run, onDone, onCancel }: {
  title: string;
  effect: string;
  verb: string;
  danger?: boolean;
  run: (reason: string, token?: string) => Promise<unknown>;
  onDone: () => Promise<unknown>;
  onCancel: () => void;
}) {
  const [reason, setReason] = useState("");
  const [token, setToken] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const result = await run(reason.trim(), token);
      if (isConfirmation(result)) setToken(result.confirmation_token);
      else await onDone();
    } catch (err) {
      setError(err);
      setToken(undefined); // a token is spent or void after any failure: start again from the reason
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} className={`flex flex-col gap-3 rounded-[var(--radius-tile)] border p-4 ${danger ? "border-negative/40 bg-negative-soft/40" : "border-line bg-page"}`}>
      <div>
        <p className="text-[14px] font-semibold">{title}</p>
        <p className="mt-0.5 text-[13px] text-ink-muted">{effect}</p>
      </div>
      <label className="flex flex-col gap-1.5">
        <span className="label">Reason (stored in the audit trail)</span>
        <input className="field" required maxLength={2000} autoFocus value={reason} disabled={busy || !!token} onChange={(e) => setReason(e.target.value)} />
      </label>
      {token ? <p className="text-[13px] font-medium">Nothing has changed yet. Confirm to apply it.</p> : null}
      {error ? <ErrorNote error={error} /> : null}
      <div className="flex flex-wrap gap-2">
        <button className={danger ? "btn-danger" : "btn-dark"} disabled={busy || !reason.trim()}>
          {busy ? "Working…" : token ? `Confirm: ${verb.toLowerCase()}` : verb}
        </button>
        <button type="button" className="btn-ghost" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
      </div>
    </form>
  );
}

function Budget({ label, used, limit }: { label: string; used: number; limit: number | null | undefined }) {
  const share = limit == null ? 0 : limit > 0 ? used / limit : 1;
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex flex-wrap justify-between gap-x-3 text-[13px]">
        <span className="text-ink-muted">{label}</span>
        <span className={`num ${share >= 1 ? "font-medium text-negative" : ""}`}>
          {limit === undefined ? `${eur(used)} spent` : limit === null ? `${eur(used)} spent, no limit set` : `${eur(used)} of ${eur(limit)}`}
        </span>
      </div>
      {limit == null ? null : <Progress value={share} tone={share >= 0.8 ? "warn" : "brand"} />}
    </div>
  );
}

function ActivityFeed({ agentId }: { agentId: string }) {
  const { data, error, loading } = useAsync(() => governanceApi.activity(agentId), [agentId]);
  if (error) return <ErrorNote error={error} />;
  if (loading) return <Spinner label="Loading activity" />;
  if (!data?.length) return <p className="label">Nothing recorded for this agent yet.</p>;
  return (
    <div className="flex flex-col gap-1.5">
      <ul className="flex flex-col gap-1.5">
        {data.slice(0, 12).map((e) => (
          <li key={e.id} className="rounded-[var(--radius-tile)] bg-page p-3 text-[13px]">
            <div className="flex flex-wrap items-baseline justify-between gap-x-3">
              <strong className="font-medium" title={e.action}>{ruleLabel(e.action)}</strong>
              <span className="text-ink-soft">{who(e.actor)} · {when(e.ts)}</span>
            </div>
            <p className="break-words text-ink-muted">
              {e.summary}{" "}
              {e.order_id ? <Link to={`/orders/${e.order_id}`} className="text-brand underline">Open the order</Link> : null}
            </p>
          </li>
        ))}
      </ul>
      {data.length > 12 ? <p className="label">Latest 12 of {num(data.length)} entries. The audit page has the full trail.</p> : null}
    </div>
  );
}

function AgentCard({ agent, limits, reload }: { agent: GovernedAgent; limits: BudgetLimits | null; reload: () => Promise<unknown> }) {
  const [form, setForm] = useState(false);
  const [open, setOpen] = useState(false);
  const pausedHere = agent.status === "paused" && !!agent.paused_at;
  const live = agent.connections.filter((c) => !c.revoked_at);
  const refusals = Object.entries(agent.refusals_by_code);
  const stats: [string, number][] = [
    ["Orders", agent.orders],
    ["Sent for sign-off", agent.approvals_asked],
    ["Signed off", agent.approvals_granted],
    ["Refused", refusals.reduce((n, [, c]) => n + c, 0)],
  ];
  const tone = { active: "bg-positive-soft text-positive", paused: "bg-warn-soft text-warn", disabled: "bg-birch-soft text-birch-ink" }[agent.status];

  return (
    <Card className="flex flex-col gap-4">
      <div className="flex flex-wrap items-start gap-3">
        <Avatar name={agent.agent_id} size={38} />
        <div className="min-w-0 flex-1 basis-48">
          <h3 className="break-words text-[16px] font-semibold leading-snug">{agent.name}</h3>
          <p className="label break-words">
            {agent.name === agent.agent_id ? "" : `${agent.agent_id} · `}owned by {agent.owner_user_id}
          </p>
        </div>
        <div className="flex flex-wrap gap-1.5">
          <span className={`chip ${tone}`}>{agent.status}</span>
          <span className="chip bg-chip text-ink-muted">
            {agent.autonomy_level === "approval_required" ? "Every order needs sign-off" : "Orders alone within limits"}
          </span>
        </div>
      </div>

      {agent.status === "paused" ? (
        <p className="break-words rounded-[var(--radius-tile)] bg-warn-soft p-3 text-[13px] text-warn">
          {pausedHere
            ? `Paused by ${who(agent.paused_by)}, ${when(agent.paused_at)}: ${agent.pause_reason ?? "no reason given"}`
            : "Stopped together with every agent of this organisation."}{" "}
          It cannot look up, quote or order.
        </p>
      ) : null}

      <div className="grid gap-x-8 gap-y-4 sm:grid-cols-2">
        <div className="flex flex-col gap-3">
          <Budget label="Spent today" used={agent.budget_use.day_eur} limit={limits ? limits.day_eur : undefined} />
          <Budget label="Spent this month" used={agent.budget_use.month_eur} limit={limits ? limits.month_eur : undefined} />
        </div>
        <div className="flex flex-col gap-2 text-[13px]">
          <div className="flex flex-wrap gap-x-6 gap-y-2">
            {stats.map(([label, n]) => (
              <div key={label}>
                <div className="num text-[18px] font-semibold leading-tight">{num(n)}</div>
                <div className="label">{label}</div>
              </div>
            ))}
          </div>
          {refusals.length ? (
            <div className="flex flex-wrap gap-1.5">
              {refusals.map(([code, n]) => (
                <span key={code} className="chip bg-negative-soft text-negative" title={code}>{ruleLabel(code)} × {n}</span>
              ))}
            </div>
          ) : null}
          <p className="break-words text-ink-muted">
            {live.length ? `${plural(live.length, "connection")}: ${[...new Set(live.map((c) => clientName(c.client)))].join(", ")}` : "No live connections"}
            {" · "}
            {agent.last_activity ? `last active ${when(agent.last_activity)}` : "no activity yet"}
          </p>
        </div>
      </div>

      {form ? (
        <ReasonForm
          danger={!pausedHere}
          title={pausedHere ? `Resume ${agent.name}` : `Pause ${agent.name}`}
          effect={pausedHere
            ? "It can look up, quote and order again within its limits."
            : "It is refused at once on lookups, quotes and orders, also on quotes it already holds. Orders waiting for approval stay in the inbox."}
          verb={pausedHere ? "Resume agent" : "Pause agent"}
          run={(reason, token) => governanceApi.setAgentPaused(agent.agent_id, !pausedHere, reason, token)}
          onDone={() => reload().then(() => setForm(false))}
          onCancel={() => setForm(false)}
        />
      ) : null}
      {open ? <ActivityFeed agentId={agent.agent_id} /> : null}

      <div className="flex flex-wrap items-center gap-2 border-t border-line pt-3">
        {form || agent.status === "disabled" || (agent.status === "paused" && !pausedHere) ? null : (
          <button className={pausedHere ? "btn-dark" : "btn-danger"} onClick={() => setForm(true)}>
            {pausedHere ? "Resume agent" : "Pause agent"}
          </button>
        )}
        <button className="btn-ghost" onClick={() => setOpen(!open)} aria-expanded={open}>
          {open ? "Hide activity" : "Show activity"}
        </button>
        <Link to={`/changes?agent=${encodeURIComponent(agent.agent_id)}`} className="btn-ghost ml-auto">
          See its limits
        </Link>
      </div>
    </Card>
  );
}

function Organisation({ id, tenant, viaOperator, heading, agents, limits, reload }: {
  id: string;
  tenant: Tenant | undefined;
  viaOperator: boolean;
  heading: string;
  agents: GovernedAgent[];
  limits: Record<string, BudgetLimits | null>;
  reload: () => Promise<unknown>;
}) {
  const [form, setForm] = useState(false);
  // shortcut: without the operator's tenant list a stop is only visible as "paused with no pause of its own";
  // if every agent is also paused individually it reads as not stopped. Upgrade when the register reports the tenant stop.
  const stopped = !!tenant?.paused_at || agents.some((a) => a.status === "paused" && !a.paused_at);
  const paused = agents.filter((a) => a.status === "paused").length;

  return (
    <section className="flex flex-col gap-3">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="min-w-0">
          <h2 className="break-words text-[16px] font-semibold">{heading}</h2>
          <p className="label">{plural(agents.length, "agent")}, {paused ? `${paused} paused` : "all running"}</p>
        </div>
        {form ? null : (
          <button className={stopped ? "btn-dark" : "btn-danger"} onClick={() => setForm(true)}>
            {stopped ? "Resume every agent" : "Pause every agent"}
          </button>
        )}
      </div>
      {stopped ? (
        <p className="break-words rounded-[var(--radius-tile)] border border-negative/30 bg-negative-soft p-3 text-[13px] text-negative">
          <strong>Emergency stop is on.</strong> No agent of {heading} can look up, quote or order.
          {tenant?.paused_at ? ` Stopped by ${who(tenant.paused_by)}, ${when(tenant.paused_at)}: ${tenant.pause_reason ?? "no reason given"}` : ""}
        </p>
      ) : null}
      {form ? (
        <ReasonForm
          danger={!stopped}
          title={stopped ? `Resume every agent of ${heading}` : `Emergency stop: pause every agent of ${heading}`}
          effect={stopped
            ? "Agents that were paused one by one stay paused until each is resumed."
            : `All ${plural(agents.length, "agent")} are refused at once on lookups, quotes and orders. People can still decide the orders waiting for approval.`}
          verb={stopped ? "Resume every agent" : "Pause every agent"}
          run={(reason, token) => governanceApi.setAllPaused(viaOperator ? id : null, !stopped, reason, token)}
          onDone={() => reload().then(() => setForm(false))}
          onCancel={() => setForm(false)}
        />
      ) : null}
      {agents.map((a) => (
        <AgentCard key={a.agent_id} agent={a} limits={limits[a.agent_id] ?? null} reload={reload} />
      ))}
    </section>
  );
}

const day = (offset: number) => {
  const d = new Date(Date.now() + offset * 86400000);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
};

function Report() {
  const [from, setFrom] = useState(() => day(-7));
  const [to, setTo] = useState(() => day(0));
  // Whole local days: the gateway reads a bare date as midnight, which would drop the last day.
  const { data, error, loading } = useAsync(
    () => (from && to ? governanceApi.report(new Date(`${from}T00:00:00`).toISOString(), new Date(`${to}T23:59:59.999`).toISOString()) : Promise.resolve(null)),
    [from, to],
  );
  const agents = data?.report.agents ?? [];
  const sum = (pick: (a: (typeof agents)[number]) => number) => agents.reduce((n, a) => n + pick(a), 0);

  const download = () => {
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
    Object.assign(document.createElement("a"), { href: url, download: `governance-report-${from}-to-${to}.json` }).click();
    URL.revokeObjectURL(url);
  };

  return (
    <Card className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end gap-3">
        <div className="min-w-0 flex-1 basis-56">
          <h2 className="text-[16px] font-semibold">Governance report</h2>
          <p className="label">What every agent did in a period, signed by the gateway so an auditor can check it was not edited.</p>
        </div>
        <label className="flex flex-col gap-1.5">
          <span className="label">From</span>
          <input type="date" className="field" value={from} max={to} onChange={(e) => setFrom(e.target.value)} />
        </label>
        <label className="flex flex-col gap-1.5">
          <span className="label">To</span>
          <input type="date" className="field" value={to} min={from} onChange={(e) => setTo(e.target.value)} />
        </label>
      </div>
      {error ? <ErrorNote error={error} /> : loading ? <Spinner label="Building the report" /> : !data ? (
        <p className="label">Pick both dates to build the report.</p>
      ) : (
        <>
          <p className="text-[14px]">
            {plural(sum((a) => a.orders.length), "order")} worth {eur(sum((a) => a.spend_eur))} confirmed,{" "}
            {num(sum((a) => a.approvals.length))} signed off by a person, {num(sum((a) => a.refusals.length))} refused,{" "}
            {plural(sum((a) => a.pauses.length), "pause or resume event")}, {plural(sum((a) => a.changes.length), "change")} to limits or ownership.
          </p>
          <ul className="flex flex-col gap-1.5">
            {agents.map((a) => (
              <li key={a.agent_id} className="flex flex-wrap items-baseline gap-x-4 gap-y-0.5 rounded-[var(--radius-tile)] bg-page p-3 text-[13px]">
                <span className="min-w-0 flex-1 basis-40 break-words font-medium">{a.agent_id}</span>
                <span className="text-ink-muted">{plural(a.orders.length, "order")}</span>
                <span className="text-ink-muted">{num(a.refusals.length)} refused</span>
                <span className="num font-medium">{eur(a.spend_eur)}</span>
              </li>
            ))}
          </ul>
          <div className="flex flex-wrap items-center gap-3">
            <button className="btn-dark" onClick={download}>Download signed report</button>
            <span className="label min-w-0 flex-1 basis-56">A file with the full report and its signature; the public keys are at {data.jwks_url}.</span>
          </div>
        </>
      )}
    </Card>
  );
}

export default function Governance() {
  const { data, error, loading, reload } = useAsync(loadBoard, [], 20000);
  if (!data) return error ? <ErrorNote error={error} /> : loading ? <Spinner /> : null;

  const ids = [...new Set(data.agents.map((a) => a.tenant_id))];
  return (
    <div className="flex flex-col gap-8">
      {error ? <ErrorNote error={error} /> : null}
      <Alerts alerts={data.alerts} />
      {ids.length ? (
        ids.map((id) => {
          const tenant = data.tenants?.find((t) => t.id === id);
          return (
            <Organisation
              key={id}
              id={id}
              tenant={tenant}
              viaOperator={!!data.tenants}
              heading={tenant?.name ?? (ids.length > 1 ? id : "Your organisation")}
              agents={data.agents.filter((a) => a.tenant_id === id)}
              limits={data.limits}
              reload={reload}
            />
          );
        })
      ) : (
        <Card>
          <Empty>No agents are registered yet. Connect one and it appears here with its owner and limits.</Empty>
        </Card>
      )}
      <Report />
    </div>
  );
}
