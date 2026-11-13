import { useEffect, useState } from "react";
import { RotateCcw, Save } from "lucide-react";
import { api, type PolicyDocument, type PolicyResource } from "../api";
import { Card, ErrorNote, Spinner, useAsync, when } from "../ui";

const doc = (p: PolicyResource): PolicyDocument => ({
  approval: p.approval,
  reject: p.reject,
  ranking_weights: p.ranking_weights,
  shortlist: p.shortlist,
});

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

  return (
    <div className="flex flex-col gap-4">
      <Card className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h2 className="text-[15px] font-semibold">The buyer owns these rules</h2>
          <p className="text-[13px] text-ink-muted">
            Guardrails are not compiled into the agent. Change them here and the next order is judged by the
            new limits — every edit is written to the audit trail.
          </p>
          <p className="label mt-1">
            {data.updated_by ? `last changed by ${data.updated_by} · ${when(data.updated_at)}` : "unchanged default"}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {saved ? <span className="chip bg-positive-soft text-positive">saved</span> : null}
          <button className="btn-ghost" onClick={() => run("reset")} disabled={!!busy}>
            <RotateCcw size={14} /> Reset
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
              Spend ceiling <code className="text-ink-soft">max_auto_total_eur</code>
            </span>
            <div className="flex items-center gap-2">
              <input
                type="number"
                className="field w-40"
                value={draft.approval.max_auto_total_eur}
                onChange={(e) =>
                  setDraft({
                    ...draft,
                    approval: { ...draft.approval, max_auto_total_eur: Number(e.target.value) },
                  })
                }
              />
              <span className="label">EUR net</span>
            </div>
          </label>

          <label className="flex flex-col gap-1.5">
            <span className="label">
              Confidence floor <code className="text-ink-soft">min_fit_probability</code>
            </span>
            <div className="flex items-center gap-3">
              <input
                type="range"
                min={0}
                max={1}
                step={0.05}
                className="flex-1 accent-ink"
                value={draft.approval.min_fit_probability}
                onChange={(e) =>
                  setDraft({
                    ...draft,
                    approval: { ...draft.approval, min_fit_probability: Number(e.target.value) },
                  })
                }
              />
              <span className="num w-12 text-right text-[14px] font-medium">
                {Math.round(draft.approval.min_fit_probability * 100)}%
              </span>
            </div>
          </label>

          <div className="flex flex-col gap-2">
            <span className="label">
              Warnings that need a signature <code className="text-ink-soft">warnings_need_approval</code>
            </span>
            <div className="flex flex-wrap gap-1.5">
              {codes.map((c) => {
                const on = draft.approval.warnings_need_approval.includes(c);
                return (
                  <button
                    key={c}
                    onClick={() =>
                      setDraft({
                        ...draft,
                        approval: {
                          ...draft.approval,
                          warnings_need_approval: toggle(draft.approval.warnings_need_approval, c),
                        },
                      })
                    }
                    className={`chip border transition-colors ${
                      on ? "border-warn bg-warn-soft text-warn" : "border-line bg-surface text-ink-soft"
                    }`}
                  >
                    {c.replaceAll("_", " ").toLowerCase()}
                  </button>
                );
              })}
            </div>
          </div>
        </Card>

        <Card className="flex flex-col gap-4">
          <div>
            <h3 className="text-[14px] font-semibold">Hard stops</h3>
            <p className="label">Rejected outright — no human can wave these through</p>
          </div>

          <label className="flex items-center gap-3 text-[13px]">
            <input
              type="checkbox"
              className="size-4 accent-ink"
              checked={draft.reject.on_hard_constraint_violation}
              onChange={(e) =>
                setDraft({
                  ...draft,
                  reject: { ...draft.reject, on_hard_constraint_violation: e.target.checked },
                })
              }
            />
            Reject on a violated hard constraint
          </label>

          <div className="flex flex-col gap-2">
            <span className="label">
              Warnings that reject <code className="text-ink-soft">reject.on</code>
            </span>
            <div className="flex flex-wrap gap-1.5">
              {codes.map((c) => {
                const on = draft.reject.on.includes(c);
                return (
                  <button
                    key={c}
                    onClick={() =>
                      setDraft({ ...draft, reject: { ...draft.reject, on: toggle(draft.reject.on, c) } })
                    }
                    className={`chip border transition-colors ${
                      on ? "border-negative bg-negative-soft text-negative" : "border-line bg-surface text-ink-soft"
                    }`}
                  >
                    {c.replaceAll("_", " ").toLowerCase()}
                  </button>
                );
              })}
            </div>
          </div>

          <label className="flex flex-col gap-1.5">
            <span className="label">
              Catalog candidates screened before live calls <code className="text-ink-soft">catalog_pool</code>
            </span>
            <input
              type="number"
              className="field w-40"
              value={draft.shortlist.catalog_pool}
              onChange={(e) =>
                setDraft({ ...draft, shortlist: { catalog_pool: Number(e.target.value) } })
              }
            />
          </label>
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
                  className="accent-ink"
                  value={draft.ranking_weights[k]}
                  onChange={(e) =>
                    setDraft({
                      ...draft,
                      ranking_weights: { ...draft.ranking_weights, [k]: Number(e.target.value) },
                    })
                  }
                />
                <span className="num text-[13px] font-medium">{draft.ranking_weights[k].toFixed(2)}</span>
              </label>
            ))}
          </div>
          <p className="label">
            Sum {Object.values(draft.ranking_weights).reduce((a, b) => a + b, 0).toFixed(2)} — the gateway
            normalises, so these are relative, not percentages.
          </p>
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
