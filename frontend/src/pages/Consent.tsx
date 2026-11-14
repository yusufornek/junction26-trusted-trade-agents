import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { ShieldCheck } from "lucide-react";
import { api } from "../api";
import { Card, Countdown, ErrorNote, Spinner, useAsync } from "../ui";

const NEW = "";
const SCOPE_WORDS: Record<string, string> = { mcp: "Use this gateway's purchasing tools: search, offers, quotes and orders" };

/** The gateway's OAuth sign-in lands here (FRONTEND_CONSENT_URL) with ?request_id=. The shell has already asked for
 *  the operator token, which stays in this console: the client only ever receives the gateway's answer. */
export default function Consent() {
  const [params] = useSearchParams();
  const id = params.get("request_id") ?? "";
  const request = useAsync(() => (id ? api.consentRequest(id) : Promise.reject(new Error("no request"))), [id]);
  const [agent, setAgent] = useState<string | null>(null);
  const [newAgent, setNewAgent] = useState("");
  const [customers, setCustomers] = useState<string[] | null>(null);
  const [busy, setBusy] = useState<"approve" | "deny" | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [leaving, setLeaving] = useState(false);

  const r = request.data;
  if (!id || request.error || (r && r.status !== "pending"))
    return (
      <Card className="mx-auto w-full max-w-xl">
        <h2 className="text-[15px] font-semibold">This sign-in request is no longer open</h2>
        <p className="label mt-1">It expired, was already answered, or the link is incomplete. Start the connection again from your assistant.</p>
      </Card>
    );
  if (!r) return <Spinner />;

  // An existing agent is the default when there is one: its mandate is known. One customer needs no choosing.
  const chosen = agent ?? r.agents[0] ?? NEW;
  const agentId = chosen === NEW ? newAgent.trim() : chosen;
  const picked = customers ?? (r.customer_ids.length === 1 ? r.customer_ids : []);
  const validAgent = /^[a-z0-9][a-z0-9-]{1,47}$/.test(agentId);

  const decide = async (decision: "approve" | "deny") => {
    setBusy(decision);
    setError(null);
    try {
      const result = await api.consentDecision(id, decision === "deny" ? { decision } : { decision, agent_id: agentId, customer_ids: picked });
      setLeaving(true);
      // replace, not assign: the answer carries a one-time code and must not sit in this window's history.
      window.location.replace(result.redirect_url);
    } catch (e) {
      setError(e);
      setBusy(null);
    }
  };

  if (leaving) return <Spinner label={`Returning to ${r.client_name}`} />;

  return (
    <Card className="mx-auto flex w-full max-w-xl flex-col gap-5">
      <div className="flex items-start gap-3">
        <span className="grid size-10 shrink-0 place-items-center rounded-2xl bg-brand-soft text-brand">
          <ShieldCheck size={20} aria-hidden />
        </span>
        <div className="min-w-0">
          <h2 className="break-words text-[17px] font-semibold leading-snug">{r.client_name} wants to buy through this gateway</h2>
          <p className="label mt-0.5">
            The answer goes to <strong className="text-ink">{r.redirect_host}</strong>. Check that this is where you started. You have{" "}
            <Countdown to={r.expires_at} /> to answer.
          </p>
        </div>
      </div>

      <div className="rounded-[var(--radius-tile)] bg-page p-3 text-[13px]">
        <div className="label mb-1">It asks to</div>
        <ul className="flex flex-col gap-1">
          {r.scopes.map((s) => (
            <li key={s}>{SCOPE_WORDS[s] ?? s}</li>
          ))}
        </ul>
      </div>

      <label className="flex flex-col gap-1.5">
        <span className="label">It buys as</span>
        <select className="field" value={chosen} onChange={(e) => setAgent(e.target.value)}>
          {r.agents.map((a) => (
            <option key={a} value={a}>
              {a}
            </option>
          ))}
          <option value={NEW}>A new agent</option>
        </select>
        {chosen === NEW ? (
          <input
            className="field"
            placeholder="events-buyer-agent"
            aria-label="New agent name"
            value={newAgent}
            onChange={(e) => setNewAgent(e.target.value.toLowerCase())}
          />
        ) : null}
        <span className="text-[12px] text-ink-muted">
          {chosen === NEW
            ? "2 to 48 lowercase letters, digits or hyphens. A new agent starts with approval first: a person signs each order."
            : "Its mandate and autonomy level stay as they are."}
        </span>
      </label>

      <fieldset className="flex flex-col gap-2">
        <legend className="label mb-1.5">It may buy for</legend>
        <div className="flex flex-wrap gap-2">
          {r.customer_ids.map((c) => (
            <label key={c} className="flex h-9 cursor-pointer items-center gap-2 rounded-full border border-line bg-surface px-3.5 text-[13px] has-checked:border-brand has-checked:bg-brand-soft">
              <input
                type="checkbox"
                className="accent-brand"
                checked={picked.includes(c)}
                onChange={(e) => setCustomers(e.target.checked ? [...picked, c] : picked.filter((x) => x !== c))}
              />
              Customer {c}
            </label>
          ))}
        </div>
        <span className="text-[12px] text-ink-muted">Choosing customers here never widens what the agent's mandate allows.</span>
      </fieldset>

      {error ? <ErrorNote error={error} /> : null}

      <div className="flex flex-wrap items-center gap-2 border-t border-line pt-4">
        <button className="btn-dark" onClick={() => decide("approve")} disabled={busy !== null || !validAgent || !picked.length}>
          {busy === "approve" ? "Approving…" : "Approve"}
        </button>
        <button className="btn-ghost" onClick={() => decide("deny")} disabled={busy !== null}>
          {busy === "deny" ? "Denying…" : "Deny"}
        </button>
        <span className="min-w-0 flex-1 basis-48 text-[12px] text-ink-muted">You can revoke it later on the Connect page.</span>
      </div>
    </Card>
  );
}
