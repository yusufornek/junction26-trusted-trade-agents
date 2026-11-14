import { useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, type LookupResult } from "../api";
import { cadenceWords, driftList, repeatApi, type RecurringOrder, type RecurringRun, type Suggestion } from "../repeat";
import { Card, Empty, ErrorNote, Spinner, StatusChip, WarningChips, dateOnly, eur, num, ruleLabel, useAsync } from "../ui";

/** One card's buttons: which one is busy, and the last error. */
function useAction() {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const act = async (name: string, fn: () => Promise<unknown>) => {
    setBusy(name);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  };
  return { busy, error, act };
}

export default function Repeat() {
  // Not polled: the gateway writes every read of the suggestions to the audit trail as "shown".
  const suggestions = useAsync(() => repeatApi.suggestions(), []);
  const recurring = useAsync(() => repeatApi.recurring(), [], 10000);
  // An agent's own token cannot read the policy; it then buys as itself and needs no choice here.
  const agents = useAsync(() => repeatApi.agents(), []);
  const [picked, setPicked] = useState("");
  const [created, setCreated] = useState<RecurringOrder | null>(null);
  // The gateway lets only the buying agent's own key pause; after one refusal the buttons are hidden for this sign-in.
  const [pauseRefused, setPauseRefused] = useState(false);
  // Start on an agent whose mandate allows repeat orders, so "Repeat automatically" works without a detour.
  const agent = picked || agents.data?.find((a) => a.recurring)?.id || agents.data?.[0]?.id;

  return (
    <div className="flex flex-col gap-8">
      <section className="flex flex-col gap-3">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div className="min-w-0">
            <h2 className="text-[16px] font-semibold">Suggestions</h2>
            <p className="label">Learned from this kitchen's confirmed orders. A suggestion never buys by itself.</p>
          </div>
          {agents.data?.length ? (
            <label className="flex flex-col gap-1.5">
              <span className="label">Buy through agent</span>
              <select className="field w-60" value={agent} onChange={(e) => setPicked(e.target.value)}>
                {agents.data.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.id}
                    {a.recurring ? "" : " (one-off orders only)"}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
        </div>
        {suggestions.error ? (
          <ErrorNote error={suggestions.error} />
        ) : suggestions.loading ? (
          <Spinner />
        ) : !suggestions.data?.length ? (
          <Card>
            <Empty>Nothing to suggest right now. Suggestions appear once a product has been ordered at least three times.</Empty>
          </Card>
        ) : (
          suggestions.data.map((s) => (
            <SuggestionCard
              key={s.id}
              s={s}
              agent={agent}
              canRepeat={!!agents.data?.find((a) => a.id === agent)?.recurring}
              onChanged={suggestions.reload}
              onRecurring={(row) => {
                setCreated(row);
                suggestions.reload();
                recurring.reload();
              }}
            />
          ))
        )}
      </section>

      {/* The gateway lists templates only to the agent or buyer that owns them, so an operator's list is always empty:
          the section appears once there is something to show rather than claiming there are none. */}
      <section className={`flex flex-col gap-3 ${created || recurring.data?.length ? "" : "hidden"}`}>
        <div>
          <h2 className="text-[16px] font-semibold">Recurring orders</h2>
          <p className="label">
            Each run checks today's price, pack and stock against the order a person approved. Any change waits for approval.
          </p>
        </div>
        {created ? (
          <p className="rounded-[var(--radius-tile)] bg-positive-soft p-3 text-[13px] text-positive">
            Recurring order created for {title(created)}: it runs {cadenceWords(created.cadence.rrule)}, first on{" "}
            {dateOnly(created.next_run_at)}, and buys through {created.agent_id}.
          </p>
        ) : null}
        {recurring.error ? (
          <ErrorNote error={recurring.error} />
        ) : recurring.loading ? (
          <Spinner />
        ) : !recurring.data?.length ? (
          <Card>
            <Empty>No recurring orders yet. Choose "Repeat automatically" on a suggestion to start one.</Empty>
          </Card>
        ) : (
          recurring.data.map((r) => (
            <RecurringCard
              key={r.id}
              r={r}
              onChanged={recurring.reload}
              canPause={!pauseRefused}
              onRefused={() => setPauseRefused(true)}
            />
          ))
        )}
      </section>
    </div>
  );
}

const CONFIDENCE_STYLE = { high: "bg-positive-soft text-positive", medium: "bg-brand-soft text-brand", low: "bg-warn-soft text-warn" };

function SuggestionCard({
  s,
  agent,
  canRepeat,
  onChanged,
  onRecurring,
}: {
  s: Suggestion;
  agent?: string;
  /** The chosen agent's mandate allows repeat orders; without it the gateway refuses, so the button is left out. */
  canRepeat: boolean;
  onChanged: () => void;
  onRecurring: (row: RecurringOrder) => void;
}) {
  const { busy, error, act } = useAction();
  const [prepared, setPrepared] = useState<LookupResult | null>(null);
  const last = s.evidence[s.evidence.length - 1];

  return (
    <Card className="flex flex-col gap-3">
      <div className="flex flex-wrap items-start justify-between gap-x-3 gap-y-2">
        <div className="min-w-0">
          <h3 className="break-words text-[16px] font-semibold leading-snug">{s.name}</h3>
          <p className="label mt-0.5">
            About {num(s.typical_quantity)} {s.unit} {cadenceWords(s.cadence_rrule)} · ordered {num(s.order_count)} times
          </p>
          <p className="label">
            Last ordered {dateOnly(s.last_order_date)}
            {last ? ` (${num(last.quantity)} ${last.unit} for ${eur(last.total_paid, last.currency)})` : ""} · next expected
            around {dateOnly(s.predicted_next_need_date)}
          </p>
        </div>
        <span className={`chip ${CONFIDENCE_STYLE[s.confidence] ?? "bg-chip text-ink-muted"}`} title={s.confidence_reason}>
          {s.confidence} confidence
        </span>
      </div>

      {prepared ? <Prepared result={prepared} /> : null}
      {error ? <ErrorNote error={error} /> : null}
      {error instanceof ApiError && error.code === "RECURRING_NOT_AUTHORIZED" ? (
        <p className="label">
          This agent's mandate does not allow repeat orders yet. Pick another agent above, or allow repeat orders in its mandate.
        </p>
      ) : null}

      <div className="flex flex-wrap items-center gap-2 border-t border-line pt-3">
        <button
          className="btn-dark"
          disabled={busy !== null}
          onClick={() => act("prepare", async () => setPrepared(await repeatApi.prepare(s.id, agent)))}
        >
          {busy === "prepare" ? "Checking prices and stock…" : prepared ? "Check again" : "Prepare order"}
        </button>
        <button
          className={`btn-ghost ${canRepeat ? "" : "hidden"}`}
          disabled={busy !== null}
          onClick={() => act("recurring", async () => onRecurring(await repeatApi.makeRecurring(s.id, agent)))}
        >
          {busy === "recurring" ? "Setting up…" : "Repeat automatically"}
        </button>
        <button
          className="btn-ghost"
          disabled={busy !== null}
          onClick={() => act("snooze", () => repeatApi.snooze(s.id, 7).then(onChanged))}
        >
          {busy === "snooze" ? "Saving…" : "Remind me in a week"}
        </button>
        <button
          className="btn-danger sm:ml-auto"
          disabled={busy !== null}
          onClick={() => act("dismiss", () => repeatApi.dismiss(s.id).then(onChanged))}
        >
          {busy === "dismiss" ? "Saving…" : "Stop suggesting this"}
        </button>
      </div>
    </Card>
  );
}

const PLAN_WORDS = {
  single: "One product covers the whole amount",
  split: "Split across several products",
  partial: "Only part of the amount can arrive on time",
};
const POLICY_WORDS: Record<string, [string, string]> = {
  CONFIRMED: ["The policy would confirm this order without asking anyone.", "bg-positive-soft text-positive"],
  PENDING_APPROVAL: ["The policy would hold this order until a person approves it.", "bg-warn-soft text-warn"],
  REJECTED: ["The policy would refuse this order.", "bg-negative-soft text-negative"],
};
const unitWords = (unit: string) => (unit === "SALES_UNIT" ? "packs" : unit);

/** A prepared suggestion is a plain lookup: the best plan and what the policy would say. Nothing is quoted or ordered. */
function Prepared({ result }: { result: LookupResult }) {
  const plan = result.plans?.[0];
  const best = result.offers[0];
  const need = result.need.quantity;
  const nameOf = (id: string) => result.offers.find((o) => o.product_id === id)?.name ?? `Product ${id}`;
  const [verdict, tone] = POLICY_WORDS[plan?.policy_preview.outcome ?? ""] ?? ["", ""];

  return (
    <div className="flex flex-col gap-2 rounded-[var(--radius-tile)] bg-page p-3 text-[13px]">
      {plan ? (
        <>
          <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
            <span className="font-medium">{PLAN_WORDS[plan.kind]}</span>
            <span className="num text-[18px] font-semibold">{eur(plan.total)}</span>
          </div>
          <ul className="flex flex-col gap-1">
            {plan.lines.map((l) => (
              <li key={l.product_id} className="min-w-0 break-words">
                {num(l.units)} × {nameOf(l.product_id)}{" "}
                <span className="text-ink-muted">
                  covers {num(l.covers.value, 1)} {unitWords(l.covers.unit)}
                </span>
              </li>
            ))}
          </ul>
          <p className="text-ink-muted">
            Covers {num(plan.coverage.value, 1)} of {num(need.value, 1)} {unitWords(need.unit)} by {dateOnly(result.need.deliver_by)}
            {plan.uncovered.value > 0 ? `; ${num(plan.uncovered.value, 1)} ${unitWords(plan.uncovered.unit)} would still be missing` : ""}.
          </p>
          <WarningChips warnings={plan.warnings ?? []} />
          {verdict ? (
            <div className={`rounded-[var(--radius-tile)] p-3 ${tone}`}>
              <p className="font-medium">{verdict}</p>
              {plan.policy_preview.hits.length ? (
                <ul className="mt-1 flex flex-col gap-1 text-ink">
                  {plan.policy_preview.hits.map((h, i) => (
                    <li key={i}>
                      <strong title={h.rule}>{ruleLabel(h.rule)}</strong> <span className="text-ink-muted">{h.detail}</span>
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>
          ) : null}
        </>
      ) : best ? (
        <>
          <p className="font-medium">
            Nothing can deliver {num(need.value, 1)} {unitWords(need.unit)} by {dateOnly(result.need.deliver_by)} right now.
          </p>
          <p className="min-w-0 break-words text-ink-muted">
            The closest offer is {best.name} at {eur(best.total, best.currency)}
            {best.availability?.available_date ? `, arriving ${dateOnly(best.availability.available_date)}` : ""}.
          </p>
          <WarningChips warnings={best.warnings} />
        </>
      ) : (
        <p className="font-medium">{result.no_match_reason ?? "No product in the catalogue matches this any more."}</p>
      )}
      <p className="label">
        Nothing has been quoted or ordered. To buy it, continue in the{" "}
        <Link to="/buyer" className="text-brand underline">
          Buyer flow
        </Link>
        .
      </p>
    </div>
  );
}

const title = (r: RecurringOrder) =>
  r.basket || !r.template.intent
    ? "a recipe basket"
    : r.template.intent + (r.template.quantity ? `, ${num(r.template.quantity.value)} ${unitWords(r.template.quantity.unit)}` : "");

const runWords = (run: RecurringRun, changes: number) =>
  run.status === "SKIPPED"
    ? `Nothing was ordered: ${ruleLabel(run.reason ?? "unknown reason").toLowerCase()}.`
    : changes
      ? "This run differed from the approved order, so it was held for a person:"
      : run.status === "CONFIRMED"
        ? "Same price, pack and delivery as the approved order. Ordered without asking anyone."
        : "Nothing changed against the approved order.";

/** Pause and resume refusals in plain words; ui.tsx's FRIENDLY map does not cover these codes. */
const PAUSE_WORDS: Record<string, string> = {
  RECURRING_FORBIDDEN: "Only the agent that buys this order can pause or resume it, using its own key.",
  RECURRING_NOT_AUTHORIZED: "This agent's mandate no longer allows repeat orders, so it cannot pause or resume them.",
  RECURRING_NOT_FOUND: "This recurring order no longer exists, or you cannot see it.",
};

function RecurringCard({
  r,
  onChanged,
  canPause,
  onRefused,
}: {
  r: RecurringOrder;
  onChanged: () => void;
  /** False once the gateway has refused this sign-in a pause or resume (403), so the button is left out. */
  canPause: boolean;
  onRefused: () => void;
}) {
  const runs = useAsync(() => repeatApi.runs(r.id), [r.id], 10000);
  const { busy, error, act } = useAction();
  const paused = r.status === "PAUSED";
  const allowed = r.permitted_deviations;
  const latest = [...(runs.data ?? [])].reverse().slice(0, 5);

  return (
    <Card className="flex flex-col gap-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="break-words text-[16px] font-semibold leading-snug first-letter:uppercase">{title(r)}</h3>
          <p className="label mt-0.5">
            Runs {cadenceWords(r.cadence.rrule)} · {r.status === "ACTIVE" ? `next run ${dateOnly(r.next_run_at)}` : "paused"} ·
            bought by {r.agent_id}
          </p>
          <p className="label">
            Approved at {eur(r.baseline.basket_cost)} per run.{" "}
            {allowed.price_pct || allowed.cumulative_price_pct
              ? `The price may move up to ${num(allowed.price_pct, 1)}% between runs before a person is asked.`
              : "Any change in price, pack or delivery goes to a person first."}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          {canPause ? (
            <button
              className="btn-ghost"
              title={paused ? "Scheduled runs start again from the next date" : "No scheduled run buys anything until you resume"}
              disabled={busy !== null}
              onClick={() =>
                act("pause", () =>
                  (paused ? repeatApi.resume : repeatApi.pause)(r.id)
                    .then(onChanged)
                    .catch((e) => {
                      if (e instanceof ApiError && e.status === 403) onRefused();
                      throw e instanceof ApiError && PAUSE_WORDS[e.code] ? new ApiError(e.code, PAUSE_WORDS[e.code], e.status) : e;
                    }),
                )
              }
            >
              {busy === "pause" ? "Saving…" : paused ? "Resume" : "Pause"}
            </button>
          ) : null}
          <button
            className="btn-dark"
            title="Buys today's order now, under the same checks as a scheduled run"
            disabled={busy !== null || paused}
            onClick={() => act("run", () => repeatApi.runNow(r.id).then(runs.reload))}
          >
            {busy === "run" ? "Running…" : "Run now"}
          </button>
        </div>
      </div>

      {error ? <ErrorNote error={error} /> : null}
      {runs.error ? (
        <ErrorNote error={runs.error} />
      ) : !latest.length ? (
        <p className="label">{runs.loading ? "Loading runs…" : "No runs yet."}</p>
      ) : (
        <ul className="flex flex-col gap-1.5">
          {latest.map((run) => {
            const drift = run.comparison ? driftList(run.comparison) : [];
            return (
              <li key={run.occurrence_key} className="rounded-[var(--radius-tile)] bg-page p-3 text-[13px]">
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                  <span className="font-medium">{dateOnly(run.occurrence_at)}</span>
                  <StatusChip status={run.status} />
                  {run.order_id ? (
                    <Link to={`/orders/${run.order_id}`} className="ml-auto font-medium text-brand hover:underline">
                      Open order
                    </Link>
                  ) : null}
                </div>
                <p className="mt-1 text-ink-muted">{runWords(run, drift.length)}</p>
                {drift.length ? (
                  <ul className="mt-1 list-disc pl-5">
                    {drift.map((d) => (
                      <li key={d} className="break-words">
                        {d}
                      </li>
                    ))}
                  </ul>
                ) : null}
                {drift.length && run.order_id ? (
                  <button
                    className="btn-ghost mt-2"
                    title="Works once this order is confirmed. Later runs are then compared with it."
                    disabled={busy !== null}
                    onClick={() =>
                      act(run.occurrence_key, () => repeatApi.setBaseline(r.id, run.order_id!).then(onChanged).then(runs.reload))
                    }
                  >
                    {busy === run.occurrence_key ? "Saving…" : "Use this order as the new baseline"}
                  </button>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}
