import { useState, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  FIELD_ORDER, LAYER_WORDS, changesApi, fieldLabel, flat, isUnset, plainError, showValue,
  type ChangeRequest, type Direction, type Layer, type Outcome, type PolicyPatch, type Preview, type Simulation,
} from "../changes";
import { Card, Empty, ErrorNote, Spinner, dateOnly, num, useAsync, when } from "../ui";

const LIMITS = ["per_order_ceiling_eur", "daily_budget_eur", "monthly_budget_eur"] as const;
const LAYER_CHIP: Record<Layer, string> = { platform: "bg-chip text-ink-muted", tenant: "bg-accent-soft text-accent", agent: "bg-brand-soft text-brand" };
const DIRECTION: Record<Direction, [string, string]> = {
  tightening: ["tighter", "bg-positive-soft text-positive"],
  loosening: ["looser", "bg-warn-soft text-warn"],
  mixed: ["partly looser", "bg-warn-soft text-warn"],
  unchanged: ["unchanged", "bg-chip text-ink-muted"],
};
const REQUEST_STATUS: Record<ChangeRequest["status"], [string, string]> = {
  pending: ["waiting for a second person", "bg-warn-soft text-warn"],
  approved: ["approved", "bg-positive-soft text-positive"],
  rejected: ["rejected", "bg-negative-soft text-negative"],
};
const sentence = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);
const shortVersion = (id: string) => id.slice(id.indexOf("_") + 1, id.indexOf("_") + 7);
/** A value inside a sentence: our own words go lower case, names from the data (customers, categories) stay as written. */
const inline = (field: string, v: unknown) => (Array.isArray(v) && v.length ? showValue(field, v) : showValue(field, v).toLowerCase());

/** The gateway asks every change to be confirmed with a one-time token; this is that question, waiting for the click. */
type Ask = { slot: string; key: string; text: string; yes: string; token: string; run: (token?: string) => Promise<Outcome> };

function outcomeWords(out: Exclude<Outcome, Preview>): ["positive" | "warn", string] {
  if ("change_request" in out)
    return ["warn", "This makes a limit looser, so nothing has changed yet. It now waits under change requests, and a second person has to approve it."];
  if ("version" in out) return ["positive", "Applied at once, because nothing in it makes a limit looser. The new version is in force now."];
  return out.status === "approved"
    ? ["positive", "Approved. The new limits are in force now."]
    : ["positive", "Rejected. The agent's limits stay as they were."];
}

const simulationWords = (s: Simulation) =>
  s.quotes + s.orders === 0
    ? "The agent had no quotes or orders in the 30 days before, so there was nothing to check the change against."
    : `Checked against the ${num(s.quotes + s.orders)} quotes and orders of the 30 days before: ${num(s.passed)} would have gone through alone, ${num(s.needed_approval)} would have needed a person, ${num(s.refused)} would have been refused.`;

function Change({ field, before, after, direction }: { field: string; before?: unknown; after: unknown; direction: Direction }) {
  return (
    <li className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[13px]">
      <span className="min-w-0">
        <span className="text-ink-muted">{fieldLabel(field)}:</span>{" "}
        {before === undefined ? "" : `from ${inline(field, before)} `}to{" "}
        <strong className="font-medium">{inline(field, after)}</strong>
      </span>
      <span className={`chip ${DIRECTION[direction][1]}`}>{DIRECTION[direction][0]}</span>
    </li>
  );
}

export default function Changes() {
  const [params, setParams] = useSearchParams();
  const agents = useAsync(() => changesApi.agents(), []);
  const id = params.get("agent") || agents.data?.[0]?.agent_id || "";

  if (agents.error) return <ErrorNote error={agents.error} />;
  if (agents.loading) return <Spinner />;
  if (!id) return <Card><Empty>No agents are registered yet, so there is no policy to show.</Empty></Card>;

  return (
    <div className="flex flex-col gap-4">
      <Card className="flex flex-wrap items-end gap-3">
        <label className="flex min-w-0 flex-col gap-1.5">
          <span className="label">Agent</span>
          <select className="field w-72 max-w-full" value={id} onChange={(e) => setParams({ agent: e.target.value }, { replace: true })}>
            {agents.data?.some((a) => a.agent_id === id) ? null : <option value={id}>{id}</option>}
            {agents.data?.map((a) => (
              <option key={a.agent_id} value={a.agent_id}>
                {a.agent_id} ({a.tenant_id})
              </option>
            ))}
          </select>
        </label>
        <p className="min-w-0 flex-1 basis-64 text-[13px] text-ink-muted">
          Every limit has a source. Making a limit tighter applies at once; making it looser waits for a second person.
        </p>
      </Card>
      {/* Keyed by agent: a draft, a pending confirmation or a result never carries over to another agent. */}
      <AgentChanges key={id} id={id} />
    </div>
  );
}

function AgentChanges({ id }: { id: string }) {
  const { data, error, loading, reload } = useAsync(
    () => Promise.all([changesApi.effective(id), changesApi.versions(id), changesApi.requests()]),
    [id],
    10000,
  );
  // Only what the person typed; untouched inputs keep following the agent's stored values across reloads.
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [reason, setReason] = useState("");
  const [decisionNote, setDecisionNote] = useState("");
  const [sim, setSim] = useState<{ key: string; now: Simulation; then: Simulation } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [ask, setAsk] = useState<Ask | null>(null);
  const [said, setSaid] = useState<{ slot: string; tone: "positive" | "warn"; text: string } | null>(null);
  const [failed, setFailed] = useState<{ slot: string; error: unknown } | null>(null);

  if (error) return <ErrorNote error={error} />;
  if (loading || !data) return <Spinner />;
  const [effective, versions, allRequests] = data;
  const requests = allRequests.filter((r) => r.agent_id === id);
  const elsewhere = [...new Set(allRequests.filter((r) => r.agent_id !== id && r.status === "pending").map((r) => r.agent_id))];
  const fields = Object.keys(effective.fields).sort((a, b) => (FIELD_ORDER.indexOf(a) + 1 || 99) - (FIELD_ORDER.indexOf(b) + 1 || 99));

  const own = (field: string) => effective.fields[field]?.sources.find((s) => s.source_layer === "agent")?.value ?? null;
  const patch: PolicyPatch = {};
  for (const field of LIMITS) {
    if (draft[field] === undefined) continue;
    const value = draft[field].trim() === "" ? null : Number(draft[field]);
    if (value !== own(field)) patch[field] = value;
  }
  if (draft.autonomy_level && draft.autonomy_level !== own("autonomy_level"))
    patch.autonomy_level = draft.autonomy_level as PolicyPatch["autonomy_level"];
  const patchKey = JSON.stringify(patch);
  const valid = LIMITS.every((f) => patch[f] == null || (Number.isFinite(patch[f]) && patch[f]! >= 0));
  const checked = sim?.key === patchKey;
  const edit = (field: string, value: string) => {
    setDraft((d) => ({ ...d, [field]: value }));
    setSaid(null);
  };

  // First call: the gateway validates and answers with a confirmation token. Second call (with the token) applies.
  const act = async (slot: string, key: string, text: string, yes: string, run: Ask["run"], token?: string) => {
    setBusy(key);
    setFailed(null);
    setSaid(null);
    try {
      const out = await run(token);
      if ("confirmation_required" in out) {
        setAsk({ slot, key, text, yes, run, token: out.confirmation_token });
        return;
      }
      const [tone, words] = outcomeWords(out);
      setAsk(null);
      setSaid({ slot, tone, text: words });
      if (slot === "propose") {
        setDraft({});
        setReason("");
        setSim(null);
      }
      await reload();
    } catch (e) {
      setAsk(null);
      setFailed({ slot, error: e });
      await reload();
    } finally {
      setBusy(null);
    }
  };

  const simulate = async () => {
    setBusy("simulate");
    setFailed(null);
    try {
      const [now, then] = await Promise.all([changesApi.simulate(id, {}), changesApi.simulate(id, patch)]);
      setSim({ key: patchKey, now, then });
    } catch (e) {
      setFailed({ slot: "propose", error: e });
    } finally {
      setBusy(null);
    }
  };

  const feedback = (slot: string): ReactNode => {
    const plain = failed?.slot === slot ? plainError(failed.error) : null;
    return (
      <>
        {ask?.slot === slot ? (
          <div className="flex flex-wrap items-center gap-2 rounded-[var(--radius-tile)] border border-warn/30 bg-warn-soft p-3 text-[13px]">
            <p className="min-w-0 flex-1 basis-56">{ask.text}</p>
            <button className="btn-dark" disabled={busy !== null} onClick={() => act(slot, ask.key, ask.text, ask.yes, ask.run, ask.token)}>
              {busy === ask.key ? "Working…" : ask.yes}
            </button>
            <button className="btn-ghost" disabled={busy !== null} onClick={() => setAsk(null)}>
              Cancel
            </button>
          </div>
        ) : null}
        {said?.slot === slot ? (
          <p role="status" className={`rounded-[var(--radius-tile)] p-3 text-[13px] ${said.tone === "positive" ? "bg-positive-soft text-positive" : "bg-warn-soft text-warn"}`}>
            {said.text}
          </p>
        ) : null}
        {failed?.slot === slot ? <ErrorNote error={plain ? new Error(plain) : failed.error} /> : null}
      </>
    );
  };

  return (
    <>
      <Card className="flex flex-col gap-3">
        <div>
          <h2 className="text-[16px] font-semibold">What {id} works under, and who set it</h2>
          <p className="label">Valio sets the outer limits for everyone, an organisation can tighten them, and one agent can be held tighter still.</p>
        </div>
        <ul>
          {fields.map((field) => {
            const f = effective.fields[field];
            const outer = f.sources.filter(
              (s) => s.source_layer !== f.source_layer && !isUnset(s.value) && s.value !== true && s.value !== "autonomous_within_limits" && JSON.stringify(s.value) !== JSON.stringify(f.value),
            );
            return (
              <li key={field} className="flex flex-wrap items-baseline gap-x-4 gap-y-1 border-t border-line py-2.5 first:border-t-0">
                <span className="min-w-0 flex-1 basis-60 text-[13px] text-ink-muted">{fieldLabel(field)}</span>
                <span className="min-w-0 flex-1 basis-48 break-words text-[14px] font-medium">{showValue(field, f.value)}</span>
                <span className="flex min-w-0 flex-1 basis-64 flex-wrap items-center gap-x-2 gap-y-1 text-[12px] text-ink-muted">
                  {f.sources.every((s) => isUnset(s.value)) ? (
                    "Nobody has set a limit here"
                  ) : (
                    <>
                      <span className={`chip ${LAYER_CHIP[f.source_layer]}`}>{sentence(LAYER_WORDS[f.source_layer])}</span>
                      <span title={f.version_id}>by {f.author}, version {shortVersion(f.version_id)}</span>
                    </>
                  )}
                  {outer.map((s) => (
                    <span key={s.source_layer} className="w-full" title={s.version_id}>
                      Outer limit {LAYER_WORDS[s.source_layer]}: {inline(field, s.value)}
                    </span>
                  ))}
                </span>
              </li>
            );
          })}
        </ul>
      </Card>

      <Card className="flex flex-col gap-4">
        <div>
          <h2 className="text-[16px] font-semibold">Propose a change for this agent</h2>
          <p className="label">Leave a limit empty to give the agent none of its own; the outer limit then applies. Check the change before you submit it.</p>
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          {LIMITS.map((field) => {
            const outer = effective.fields[field]?.sources
              .filter((s) => s.source_layer !== "agent" && typeof s.value === "number")
              .sort((a, b) => (a.value as number) - (b.value as number))[0];
            return (
              <label key={field} className="flex min-w-0 flex-col gap-1.5">
                <span className="label">{fieldLabel(field)} (EUR)</span>
                <input
                  className="field"
                  type="number"
                  min={0}
                  step="any"
                  inputMode="decimal"
                  placeholder="No limit of its own"
                  value={draft[field] ?? (own(field) == null ? "" : String(own(field)))}
                  onChange={(e) => edit(field, e.target.value)}
                />
                <span className="text-[12px] text-ink-soft">
                  {outer ? `Cannot go above ${showValue(field, outer.value)}, ${LAYER_WORDS[outer.source_layer]}.` : "No outer limit is set above this agent."}
                </span>
              </label>
            );
          })}
          <label className="flex min-w-0 flex-col gap-1.5">
            <span className="label">{fieldLabel("autonomy_level")}</span>
            <select className="field" value={draft.autonomy_level ?? String(own("autonomy_level") ?? "autonomous_within_limits")} onChange={(e) => edit("autonomy_level", e.target.value)}>
              <option value="autonomous_within_limits">{showValue("autonomy_level", "autonomous_within_limits")}</option>
              <option value="approval_required">{showValue("autonomy_level", "approval_required")}</option>
            </select>
          </label>
        </div>
        <label className="flex flex-col gap-1.5">
          <span className="label">Why (kept in the audit trail)</span>
          <input className="field" placeholder="Weekend banquets need larger orders" value={reason} onChange={(e) => setReason(e.target.value)} />
        </label>

        {checked && sim ? (
          <div className="rounded-[var(--radius-tile)] bg-page p-3 text-[13px]">
            {sim.then.quotes + sim.then.orders === 0 ? (
              <p>This agent has no quotes or orders in the last 30 days, so there is nothing to check the change against. You can still submit it.</p>
            ) : (
              <>
                <p className="font-medium">
                  Replayed on this agent's last 30 days ({num(sim.then.quotes)} quotes, {num(sim.then.orders)} orders), without calling any supplier:
                </p>
                <ul className="mt-1.5 flex flex-col gap-1">
                  {([["passed", "Go through alone"], ["needed_approval", "Need a person's approval"], ["refused", "Refused"]] as const).map(([k, label]) => (
                    <li key={k} className="flex flex-wrap gap-x-2">
                      <span className="min-w-0 flex-1 basis-44 text-ink-muted">{label}</span>
                      <span className="num">
                        {num(sim.now[k])} today, <strong className="font-semibold">{num(sim.then[k])} with this change</strong>
                      </span>
                    </li>
                  ))}
                </ul>
                {sim.then.examples.filter((x) => x.outcome !== "passed").slice(0, 3).map((x) => (
                  <p key={x.kind + x.id} className="mt-1.5 text-[12px] text-ink-muted" title={x.id}>
                    {sentence(x.kind)} {x.id.slice(0, 8)} would have {x.outcome === "refused" ? "been refused" : "needed a person"}: {x.rules.map(fieldLabel).join(", ").toLowerCase()}
                  </p>
                ))}
              </>
            )}
          </div>
        ) : null}
        {feedback("propose")}
        <div className="flex flex-wrap items-center gap-2">
          <button className="btn-ghost" disabled={busy !== null || patchKey === "{}" || !valid} onClick={simulate}>
            {busy === "simulate" ? "Checking…" : "Check against the last 30 days"}
          </button>
          <button
            className="btn-dark"
            disabled={busy !== null || !checked || patchKey === "{}" || !valid || !reason.trim() || ask?.slot === "propose"}
            onClick={() =>
              act("propose", "submit", `Submit this change for ${id}? Tighter limits apply at once; a looser limit waits for a second person.`, "Yes, submit the change",
                (token) => changesApi.change(id, patch, reason.trim(), token))
            }
          >
            {busy === "submit" ? "Submitting…" : "Submit this change"}
          </button>
          <span className="min-w-0 text-[12px] text-ink-soft">
            {patchKey === "{}" ? "Change a value to begin." : !valid ? "Limits must be zero or more." : !checked ? "Check the change first." : !reason.trim() ? "Say why, then submit." : ""}
          </span>
        </div>
      </Card>

      <Card className="flex flex-col gap-3">
        <div className="flex flex-wrap items-end gap-3">
          <div className="min-w-0 flex-1 basis-64">
            <h2 className="text-[16px] font-semibold">Change requests for this agent</h2>
            <p className="label">A looser limit waits here until someone other than the person who asked approves it.</p>
          </div>
          {requests.some((r) => r.status === "pending") ? (
            <label className="flex min-w-0 flex-1 basis-56 flex-col gap-1.5">
              <span className="label">Note with your decision (kept in the audit trail)</span>
              <input className="field" placeholder="Checked with the kitchen manager" value={decisionNote} onChange={(e) => setDecisionNote(e.target.value)} />
            </label>
          ) : null}
        </div>
        {!requests.length ? <Empty>No change has been requested for this agent.</Empty> : null}
        {requests.map((r) => {
          const base = versions.find((v) => v.id === r.base_version);
          const before = base ? flat(base.data) : null;
          const after = flat(r.policy);
          const decide = (kind: "approve" | "reject") =>
            act(r.id, r.id + kind,
              kind === "approve" ? `Approve this change? The looser limits apply to ${r.agent_id} at once.` : "Reject this change? The agent's limits stay as they are.",
              kind === "approve" ? "Yes, approve and apply" : "Yes, reject",
              (token) => changesApi.decide(r.id, kind, decisionNote.trim() || (kind === "approve" ? "Approved in the console" : "Rejected in the console"), token));
          return (
            <div key={r.id} className="flex flex-col gap-2 rounded-[var(--radius-tile)] border border-line p-3">
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                <p className="min-w-0 flex-1 basis-56 text-[14px] font-medium">
                  {r.requester} asked {r.rollback_version ? "to go back to an earlier version" : "for a change"}
                  <span className="font-normal text-ink-muted"> · {when(r.created_at)}</span>
                </p>
                <span className={`chip ${REQUEST_STATUS[r.status][1]}`}>{REQUEST_STATUS[r.status][0]}</span>
              </div>
              <p className="text-[13px] text-ink-muted">Reason given: {r.reason}</p>
              <ul className="flex flex-col gap-1">
                {Object.entries(r.classification).filter(([, d]) => d !== "unchanged").map(([field, d]) => (
                  <Change key={field} field={field} before={before ? before[field] : undefined} after={after[field]} direction={d} />
                ))}
              </ul>
              <p className="text-[12px] text-ink-muted">{simulationWords(r.simulation)}</p>
              {r.decided_by ? (
                <p className="text-[13px]">
                  {sentence(r.status)} by {r.decided_by}, {when(r.decided_at)}
                  {r.decision_reason ? `: ${r.decision_reason}` : ""}
                </p>
              ) : null}
              {feedback(r.id)}
              {r.status === "pending" && ask?.slot !== r.id ? (
                <div className="flex flex-wrap gap-2">
                  <button className="btn-dark" disabled={busy !== null} onClick={() => decide("approve")}>
                    {busy === r.id + "approve" ? "Checking…" : "Approve and apply"}
                  </button>
                  <button className="btn-danger" disabled={busy !== null} onClick={() => decide("reject")}>
                    {busy === r.id + "reject" ? "Checking…" : "Reject"}
                  </button>
                </div>
              ) : null}
            </div>
          );
        })}
        {elsewhere.length ? (
          <div className="flex flex-wrap items-center gap-2 border-t border-line pt-3 text-[13px] text-ink-muted">
            Requests are also waiting for:
            {elsewhere.map((agent) => (
              <Link key={agent} className="btn-ghost" to={`/changes?agent=${encodeURIComponent(agent)}`}>
                {agent}
              </Link>
            ))}
          </div>
        ) : null}
      </Card>

      <Card className="flex flex-col gap-3">
        <div>
          <h2 className="text-[16px] font-semibold">Versions of this agent's own limits</h2>
          <p className="label">Nothing is overwritten. Going back makes a new version, and waits for a second person if it loosens a limit.</p>
        </div>
        {versions.slice(0, 8).map((v, index) => (
          <div key={v.id} className="flex flex-col gap-2 border-t border-line pt-3 first:border-t-0 first:pt-0">
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <p className="min-w-0 flex-1 basis-56 text-[14px] font-medium" title={v.id}>
                {dateOnly(v.created_at)} by {v.author}
                <span className="font-normal text-ink-muted"> · {v.reason}</span>
              </p>
              {index === 0 ? (
                <span className="chip bg-positive-soft text-positive">in force now</span>
              ) : ask?.slot === v.id ? null : (
                <button
                  className="btn-ghost"
                  disabled={busy !== null}
                  onClick={() =>
                    act(v.id, v.id, `Go back to the version from ${dateOnly(v.created_at)}? If that makes any limit looser, it waits for a second person instead of applying.`, "Yes, go back",
                      (token) => changesApi.rollback(id, v.id, `Went back to the version from ${dateOnly(v.created_at)} in the console`, token))
                  }
                >
                  {busy === v.id ? "Checking…" : "Go back to this version"}
                </button>
              )}
            </div>
            <ul className="flex flex-col gap-1">
              {Object.entries(v.diff).map(([field, d]) => (
                <Change key={field} field={field} before={d.before} after={d.after} direction={d.direction} />
              ))}
            </ul>
            {Object.keys(v.diff).length ? null : (
              <p className="text-[13px] text-ink-muted">{v.id.startsWith("base_") ? "The starting point: what the agent was allowed when governance was switched on." : "No values changed."}</p>
            )}
            {feedback(v.id)}
          </div>
        ))}
      </Card>
    </>
  );
}
