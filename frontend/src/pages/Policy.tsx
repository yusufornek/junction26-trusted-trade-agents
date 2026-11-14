import { useEffect, useState } from "react";
import { Bot, RotateCcw, Save, Users } from "lucide-react";
import { api, type AgentMandate, type PolicyDocument, type PolicyResource } from "../api";
import { Avatar } from "../charts";
import { Card, Empty, ErrorNote, Spinner, eur, useAsync, when } from "../ui";

const doc = (p: PolicyResource): PolicyDocument => ({
  approval: p.approval,
  reject: p.reject,
  ranking_weights: p.ranking_weights,
  shortlist: p.shortlist,
  agents: p.agents,
});

const pretty = (code: string) => code.replaceAll("_", " ").toLowerCase();

export default function Policy() {
  const { data, error, loading, reload } = useAsync(() => api.policy(), []);
  const [draft, setDraft] = useState<PolicyDocument | null>(null);
  const [author, setAuthor] = useState("Ada");
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<unknown>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    if (data) setDraft(doc(data));
  }, [data]);

  if (error) return <ErrorNote error={error} />;
  if (loading || !data || !draft) return <Spinner />;

  const dirty = JSON.stringify(draft) !== JSON.stringify(doc(data));
  const codes = data.known_warning_codes;
  const ignored = draft.approval.ignored_warnings ?? [];

  const run = async (kind: "save" | "reset") => {
    setBusy(kind);
    setSaveError(null);
    try {
      if (kind === "save") await api.updatePolicy(draft, author, comment || "changed in console");
      else await api.resetPolicy(author);
      setComment("");
      setSaved(true);
      setTimeout(() => setSaved(false), 2500);
      reload();
    } catch (e) {
      setSaveError(e);
    } finally {
      setBusy(null);
    }
  };

  const toggle = (list: string[], code: string) =>
    list.includes(code) ? list.filter((c) => c !== code) : [...list, code];

  const setAgent = (id: string, patch: Partial<AgentMandate>) =>
    setDraft({ ...draft, agents: { ...draft.agents, [id]: { ...draft.agents![id], ...patch } } });

  return (
    <div className="flex flex-col gap-4">
      {/* Actions stay beside the text from sm up and wrap as a group: the transient "saved" chip must not push them out of the card. */}
      <Card className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0 sm:flex-1">
          <h2 className="text-[15px] font-semibold">The buyer owns these rules</h2>
          <p className="text-[13px] text-ink-muted">
            Guardrails are not compiled into the agent. Change them here and the next order is judged by the new
            limits — every edit is written to the audit trail.
          </p>
          <p className="label mt-1">
            {data.updated_by ? `last changed by ${data.updated_by} · ${when(data.updated_at)}` : "unchanged default"}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2 sm:shrink-0 sm:justify-end">
          {saved ? <span className="chip bg-positive-soft text-positive">saved</span> : null}
          <button className="btn-ghost" onClick={() => run("reset")} disabled={!!busy} title="Restore the rules in policy.yaml">
            <RotateCcw size={14} /> Reset to defaults
          </button>
          <button className="btn-dark" onClick={() => run("save")} disabled={!dirty || !!busy}>
            <Save size={14} /> {busy === "save" ? "Saving…" : dirty ? "Save changes" : "No changes"}
          </button>
        </div>
      </Card>

      {saveError ? <ErrorNote error={saveError} /> : null}

      <div className="grid gap-4 lg:grid-cols-2">
        <Card className="flex flex-col gap-4">
          <div>
            <h3 className="text-[14px] font-semibold">Where autonomy stops</h3>
            <p className="label">Anything matching goes to a human</p>
          </div>

          <label className="flex flex-col gap-1.5">
            <span className="label">
              Spend ceiling per order <code className="text-ink-muted">max_auto_total_eur</code>
            </span>
            <div className="flex items-center gap-2">
              <input
                type="number"
                className="field w-40"
                value={draft.approval.max_auto_total_eur}
                onChange={(e) => setDraft({ ...draft, approval: { ...draft.approval, max_auto_total_eur: Number(e.target.value) } })}
              />
              <span className="label">EUR net</span>
            </div>
          </label>

          <label className="flex flex-col gap-1.5">
            <span className="label">
              Confidence floor <code className="text-ink-muted">min_fit_probability</code>
            </span>
            <div className="flex items-center gap-3">
              <input
                type="range"
                min={0}
                max={1}
                step={0.05}
                className="flex-1 accent-brand max-sm:h-9"
                value={draft.approval.min_fit_probability}
                onChange={(e) => setDraft({ ...draft, approval: { ...draft.approval, min_fit_probability: Number(e.target.value) } })}
              />
              <span className="num w-12 text-right text-[14px] font-medium">{Math.round(draft.approval.min_fit_probability * 100)}%</span>
            </div>
          </label>

          <CodeToggles
            title="Warnings that need a signature"
            field="warnings_need_approval"
            codes={codes}
            active={draft.approval.warnings_need_approval}
            onStyle="border-warn bg-warn-soft text-warn"
            disabled={ignored}
            onToggle={(c) =>
              setDraft({ ...draft, approval: { ...draft.approval, warnings_need_approval: toggle(draft.approval.warnings_need_approval, c) } })
            }
          />

          <CodeToggles
            title="Informational only — never block"
            field="ignored_warnings"
            codes={codes}
            active={ignored}
            onStyle="border-ink/20 bg-chip text-ink"
            onToggle={(c) => setDraft({ ...draft, approval: { ...draft.approval, ignored_warnings: toggle(ignored, c) } })}
          />
        </Card>

        <Card className="flex flex-col gap-4">
          <div>
            <h3 className="text-[14px] font-semibold">Hard stops</h3>
            <p className="label">Rejected outright — no human can wave these through</p>
          </div>

          <label className="flex items-center gap-3 text-[13px] max-sm:min-h-9">
            <input
              type="checkbox"
              className="size-4 accent-brand"
              checked={draft.reject.on_hard_constraint_violation}
              onChange={(e) => setDraft({ ...draft, reject: { ...draft.reject, on_hard_constraint_violation: e.target.checked } })}
            />
            Reject on a violated hard constraint
          </label>

          <CodeToggles
            title="Warnings that reject"
            field="reject.on"
            codes={codes}
            active={draft.reject.on}
            onStyle="border-negative bg-negative-soft text-negative"
            onToggle={(c) => setDraft({ ...draft, reject: { ...draft.reject, on: toggle(draft.reject.on, c) } })}
          />

          {/* justify-between keeps the two inputs level when one label wraps to a second line. */}
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="flex flex-col justify-between gap-1.5">
              <span className="label">
                Screened before live calls <code className="text-ink-muted">catalog_pool</code>
              </span>
              <input
                type="number"
                className="field"
                value={draft.shortlist.catalog_pool}
                onChange={(e) => setDraft({ ...draft, shortlist: { ...draft.shortlist, catalog_pool: Number(e.target.value) } })}
              />
            </label>
            <label className="flex flex-col justify-between gap-1.5">
              <span className="label">
                Priced live at most <code className="text-ink-muted">live_max</code>
              </span>
              <input
                type="number"
                className="field"
                value={draft.shortlist.live_max ?? ""}
                onChange={(e) =>
                  setDraft({ ...draft, shortlist: { ...draft.shortlist, live_max: e.target.value === "" ? undefined : Number(e.target.value) } })
                }
              />
            </label>
          </div>
        </Card>

        <Card className="flex flex-col gap-4 lg:col-span-2">
          <div>
            <h3 className="text-[14px] font-semibold">What “best” means to this buyer</h3>
            <p className="label">Ranking weights — they decide which offer is labelled best</p>
          </div>
          <div className="grid gap-4 sm:grid-cols-5">
            {(Object.keys(draft.ranking_weights) as (keyof PolicyDocument["ranking_weights"])[]).map((k) => (
              <label key={k} className="flex flex-col gap-1.5">
                <span className="label capitalize">{k === "co2" ? "CO₂" : k}</span>
                <input
                  type="range"
                  min={0}
                  max={1}
                  step={0.05}
                  className="accent-brand max-sm:h-9"
                  value={draft.ranking_weights[k]}
                  onChange={(e) => setDraft({ ...draft, ranking_weights: { ...draft.ranking_weights, [k]: Number(e.target.value) } })}
                />
                <span className="num text-[13px] font-medium">{draft.ranking_weights[k].toFixed(2)}</span>
              </label>
            ))}
          </div>
          <p className="label">
            Sum {Object.values(draft.ranking_weights).reduce((a, b) => a + b, 0).toFixed(2)} — the gateway normalises, so
            these are relative, not percentages.
          </p>
        </Card>

        <Card className="flex flex-col gap-4 lg:col-span-2">
          <div className="flex items-start gap-2">
            <Bot size={16} className="mt-0.5 shrink-0" />
            <div>
              <h3 className="text-[14px] font-semibold">Agent mandates</h3>
              <p className="label">
                An agent without a mandate can look and quote, but its orders are refused. Limits here sit on top of
                the rules above.
              </p>
            </div>
          </div>
          {!draft.agents || !Object.keys(draft.agents).length ? (
            <Empty>No agent holds a mandate. Every order attempt will answer 403 AGENT_NOT_REGISTERED.</Empty>
          ) : (
            <div className="grid gap-3 md:grid-cols-2">
              {Object.entries(draft.agents).map(([id, m]) => (
                <div key={id} className="flex flex-col gap-3 rounded-[var(--radius-tile)] border border-line p-4">
                  <div className="flex items-center gap-3">
                    <Avatar name={id} size={34} />
                    <div className="min-w-0">
                      <div className="truncate text-[14px] font-medium" title={id}>
                        {id}
                      </div>
                      <div className="text-[12px] text-ink-muted">{m.note || "no note"}</div>
                    </div>
                  </div>
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="label mr-1">customers</span>
                    {m.customer_ids.map((c) => (
                      <span key={c} className="chip bg-chip text-ink">
                        {c}
                      </span>
                    ))}
                  </div>
                  <div className="grid gap-3 sm:grid-cols-2">
                    <label className="flex flex-col gap-1.5">
                      <span className="label">Per order, auto (EUR)</span>
                      <input
                        type="number"
                        className="field"
                        placeholder={`policy: ${eur(draft.approval.max_auto_total_eur)}`}
                        value={m.max_auto_total_eur ?? ""}
                        onChange={(e) => setAgent(id, { max_auto_total_eur: e.target.value === "" ? null : Number(e.target.value) })}
                      />
                    </label>
                    <label className="flex flex-col gap-1.5">
                      <span className="label">Per day, auto (EUR)</span>
                      <input
                        type="number"
                        className="field"
                        value={m.max_auto_daily_total_eur}
                        onChange={(e) => setAgent(id, { max_auto_daily_total_eur: Number(e.target.value) })}
                      />
                    </label>
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>

        <Card className="flex flex-col gap-4 lg:col-span-2">
          <div className="flex items-start gap-2">
            <Users size={16} className="mt-0.5 shrink-0" />
            <div>
              <h3 className="text-[14px] font-semibold">Standing customer constraints</h3>
              <p className="label">
                Applied to every request for that customer, even when the agent forgets to ask. Edited in policy.yaml.
              </p>
            </div>
          </div>
          {!data.customers || !Object.keys(data.customers).length ? (
            <Empty>No standing constraints — each request carries its own.</Empty>
          ) : (
            <div className="grid gap-3 md:grid-cols-2">
              {Object.entries(data.customers).map(([id, c]) => (
                <div key={id} className="flex flex-col gap-2 rounded-[var(--radius-tile)] border border-line p-4">
                  <div className="text-[14px] font-medium">Customer {id}</div>
                  {c.note ? <div className="text-[12px] text-ink-muted">{c.note}</div> : null}
                  <div className="flex flex-wrap gap-1.5">
                    {(c.required_claims ?? []).map((x) => (
                      <span key={x} className="chip bg-positive-soft text-positive">
                        must be {pretty(x)}
                      </span>
                    ))}
                    {(c.exclude_allergens ?? []).map((x) => (
                      <span key={x} className="chip bg-negative-soft text-negative">
                        excludes {x}
                      </span>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>
      </div>

      <Card className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1.5">
          <span className="label">Changing as</span>
          <input className="field w-48" value={author} onChange={(e) => setAuthor(e.target.value)} />
        </label>
        <label className="flex min-w-60 flex-1 flex-col gap-1.5">
          <span className="label">Why (goes into the audit trail)</span>
          <input
            className="field"
            placeholder="Tighter ceiling before the holiday week"
            value={comment}
            onChange={(e) => setComment(e.target.value)}
          />
        </label>
      </Card>
    </div>
  );
}

function CodeToggles({
  title,
  field,
  codes,
  active,
  onStyle,
  disabled = [],
  onToggle,
}: {
  title: string;
  field: string;
  codes: string[];
  active: string[];
  onStyle: string;
  disabled?: string[];
  onToggle: (code: string) => void;
}) {
  return (
    <div className="flex flex-col gap-2">
      <span className="label">
        {title} <code className="text-ink-muted">{field}</code>
      </span>
      {/* Phone: rows 36px apart and an invisible 7px hit margin above and below, so each pill is a 36px tap target. */}
      <div className="flex flex-wrap gap-x-1.5 gap-y-3.5 sm:gap-y-1.5">
        {codes.map((c) => {
          const on = active.includes(c);
          const off = disabled.includes(c);
          return (
            <button
              key={c}
              onClick={() => onToggle(c)}
              disabled={off}
              aria-pressed={on}
              title={off ? "Marked informational — it never blocks an order" : undefined}
              className={`chip relative border transition-colors max-sm:before:absolute max-sm:before:inset-x-0 max-sm:before:-inset-y-2 disabled:cursor-not-allowed disabled:opacity-40 ${
                on ? onStyle : "border-line bg-surface text-ink-muted hover:text-ink"
              }`}
            >
              {pretty(c)}
            </button>
          );
        })}
      </div>
    </div>
  );
}
