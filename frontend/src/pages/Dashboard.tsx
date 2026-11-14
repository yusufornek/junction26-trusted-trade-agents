import { Link } from "react-router-dom";
import { useRef } from "react";
import { ChevronLeft, ChevronRight, FileSignature, Inbox, Plus } from "lucide-react";
import { api, type Order } from "../api";
import { useRange } from "../Shell";
import { AreaChart, Avatar, Bars, Progress, Ring, Sparkline, type Marker } from "../charts";
import { Card, Countdown, Empty, ErrorNote, Spinner, StatusChip, eur, num, useAsync, when } from "../ui";

const POLL = 5000;
const DAY = 86400000;

const dayKey = (iso: string) => iso.slice(0, 10);
const dayLabel = (key: string) =>
  new Date(key).toLocaleDateString("en-GB", { day: "numeric", month: "short" });

/** Daily buckets from the oldest order to today — the gateway stores events, not a time series. */
function buildSeries(orders: Order[], days: number) {
  if (!orders.length) return { values: [], labels: [], markers: [] as Marker[] };
  const oldest = orders.reduce((a, o) => Math.min(a, Date.parse(o.created_at)), Date.now());
  const start = Math.max(oldest, Date.now() - (days || 3650) * DAY);
  const keys: string[] = [];
  for (let t = start; t <= Date.now() + DAY; t += DAY) keys.push(new Date(t).toISOString().slice(0, 10));

  let running = 0;
  const values: number[] = [];
  const markers: Marker[] = [];
  keys.forEach((k, i) => {
    for (const o of orders) {
      if (dayKey(o.created_at) !== k) continue;
      if (o.status === "CONFIRMED") running += o.total;
      markers.push({
        index: i,
        label: o.id,
        tone: o.status === "CONFIRMED" ? "positive" : o.status === "PENDING_APPROVAL" ? "ink" : "negative",
      });
    }
    values.push(running);
  });
  return { values, labels: keys.map(dayLabel), markers };
}

export default function Dashboard() {
  const { since, rangeLabel } = useRange();
  const stats = useAsync(() => api.stats(since), [since], POLL);
  const orders = useAsync(() => api.orders(), [], POLL);
  const approvals = useAsync(() => api.approvals(), [], POLL);
  const audit = useAsync(() => api.audit(), [], POLL);
  const strip = useRef<HTMLDivElement>(null);

  if (stats.error) return <ErrorNote error={stats.error} />;
  if (stats.loading || !stats.data) return <Spinner />;

  const s = stats.data;
  const all = orders.data ?? [];
  const byStatus = s.orders_by_status;
  const pending = byStatus.PENDING_APPROVAL ?? 0;
  const confirmed = byStatus.CONFIRMED ?? 0;
  const placed = Object.values(s.placement_outcomes).reduce((a, b) => a + b, 0);
  const autoShare = placed ? (s.placement_outcomes.CONFIRMED ?? 0) / placed : 0;
  const avgCalls = s.average_live_calls_per_lookup.price + s.average_live_calls_per_lookup.availability;
  const decided = s.human_decisions.approved + s.human_decisions.rejected;

  const days = rangeLabel.includes("7") ? 7 : rangeLabel.includes("30") ? 30 : 0;
  const series = buildSeries(all, days);
  const sparks = {
    spend: series.values.length > 1 ? series.values : [0, 0],
    orders: series.values.map((_, i) => all.filter((o) => Date.parse(o.created_at) <= Date.now() - (series.values.length - 1 - i) * DAY).length),
  };

  // Last buckets as columns, like the reference's three-month block.
  const bars = series.labels.slice(-4).map((label, i, arr) => {
    const idx = series.values.length - arr.length + i;
    const prev = idx > 0 ? series.values[idx - 1] : 0;
    return { label, value: series.values[idx] - prev, caption: eur(series.values[idx] - prev) };
  });

  return (
    <div className="flex flex-col gap-4">
      {/* Row 1 — four metrics with trend lines, plus the dark summary card. */}
      <div className="grid gap-4 xl:grid-cols-[1fr_1fr_1fr_1fr_320px]">
        <Metric
          label="Committed"
          value={eur(s.confirmed_spend_total_eur)}
          foot={`${confirmed} confirmed`}
          badge={autoShare > 0 ? `${Math.round(autoShare * 100)}% auto` : undefined}
          spark={sparks.spend}
        />
        <Metric
          label="Awaiting human"
          value={String(pending)}
          foot={pending ? "needs a decision" : "inbox clear"}
          tone={pending ? "warn" : "ink"}
        />
        <Metric
          label="Lookups"
          value={String(s.lookups)}
          foot={`${s.total_decisions_calls} fit checks`}
          spark={sparks.orders}
          tone="ink"
        />
        <Metric
          label="Calls per lookup"
          value={num(avgCalls, 1)}
          foot="price + availability, on demand"
        />

        <div className="relative overflow-hidden rounded-[var(--radius-card)] bg-gradient-to-br from-[#4a4a46] to-[#1d1d1b] p-5 text-white xl:row-span-2">
          <div className="flex items-start justify-between">
            <div>
              <div className="text-[13px] text-white/60">Trusted Trade Agents</div>
              <div className="num mt-4 text-[34px] font-semibold leading-none">
                {eur(s.confirmed_spend_total_eur)}
              </div>
              <div className="mt-1 text-[13px] text-white/60">committed · {rangeLabel.toLowerCase()}</div>
            </div>
            <Avatar name="operator" size={34} />
          </div>

          <div className="mt-6 flex flex-col gap-2 text-[12px] text-white/70">
            <Row label="Decided by the agent" value={`${s.placement_outcomes.CONFIRMED ?? 0}`} />
            <Row label="Signed off by a human" value={`${s.human_decisions.approved}`} />
            <Row label="Rejected" value={`${s.human_decisions.rejected + (s.placement_outcomes.REJECTED ?? 0)}`} />
          </div>

          <div className="mt-6 flex gap-2">
            <Link
              to="/buyer"
              className="inline-flex flex-1 items-center justify-center gap-2 rounded-full bg-white px-4 py-2.5 text-[13px] font-medium text-ink"
            >
              <Plus size={14} /> New request
            </Link>
            <Link
              to="/approvals"
              className="inline-flex flex-1 items-center justify-center gap-2 rounded-full bg-white/10 px-4 py-2.5 text-[13px] font-medium text-white hover:bg-white/15"
            >
              <Inbox size={14} /> Inbox
            </Link>
          </div>
        </div>
      </div>

      {/* Row 2 — the committed-spend curve with a marker per order. */}
      <Card className="xl:mr-[336px]">
        <div className="mb-2 flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="label">Committed spend</div>
            <div className="metric mt-1">{eur(s.confirmed_spend_total_eur)}</div>
          </div>
          <div className="flex flex-wrap gap-3 text-[12px]">
            <Legend tone="bg-positive" label="Confirmed" value={confirmed} />
            <Legend tone="bg-ink" label="Pending" value={pending} />
            <Legend tone="bg-negative" label="Rejected" value={byStatus.REJECTED ?? 0} />
            <Legend tone="bg-ink-soft" label="Cancelled" value={byStatus.CANCELLED ?? 0} />
          </div>
        </div>
        <AreaChart values={series.values} labels={series.labels} markers={series.markers} />
      </Card>

      {/* Row 3 — the thin tile strip. */}
      <div className="grid gap-4 sm:grid-cols-3 xl:grid-cols-6">
        <Tile label="Orders placed" value={String(placed)} />
        <Tile label="Human decisions" value={String(decided)} />
        <Tile label="Fit checks" value={String(s.total_decisions_calls)} />
        <Tile label="Agents" value={String(s.agents_seen.length)} />
        <Tile label="Autonomy" value={`${Math.round(autoShare * 100)}%`}>
          <Ring value={autoShare} tone={autoShare > 0.5 ? "positive" : "warn"} />
        </Tile>
        <Tile
          label="Approval rate"
          value={decided ? `${Math.round((s.human_decisions.approved / decided) * 100)}%` : "—"}
        >
          <Ring value={decided ? s.human_decisions.approved / decided : 0} />
        </Tile>
      </div>

      {/* Row 4 — the activity strip. */}
      <Card className="flex flex-col gap-3">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-[15px] font-semibold">Latest activity</h2>
            <p className="label">Orders and quotes in chronological order</p>
          </div>
          <div className="flex gap-1">
            <StripButton onClick={() => strip.current?.scrollBy({ left: -280, behavior: "smooth" })}>
              <ChevronLeft size={15} />
            </StripButton>
            <StripButton onClick={() => strip.current?.scrollBy({ left: 280, behavior: "smooth" })}>
              <ChevronRight size={15} />
            </StripButton>
          </div>
        </div>
        {!all.length ? (
          <Empty>
            Nothing yet — run the{" "}
            <Link to="/buyer" className="underline">
              buyer flow
            </Link>
            .
          </Empty>
        ) : (
          <div ref={strip} className="flex gap-3 overflow-x-auto pb-1 [scrollbar-width:none] [&::-webkit-scrollbar]{display:none}">
            {[...all].reverse().map((o) => (
              <Link
                key={o.id}
                to={`/orders/${o.id}`}
                className="flex w-[236px] shrink-0 flex-col gap-3 rounded-[var(--radius-tile)] border border-line p-4 transition-colors hover:bg-page"
              >
                <div className="flex items-center justify-between">
                  <span className="chip bg-chip text-ink-muted">order</span>
                  <StatusChip status={o.status} />
                </div>
                <Avatar name={o.agent_id} />
                <div className="min-w-0">
                  <div className="truncate text-[13px] text-ink-muted">{o.lines[0]?.name ?? o.id}</div>
                  <div className="num mt-1 text-[18px] font-semibold">{eur(o.total, o.currency)}</div>
                  <div className="mt-1 text-[12px] text-ink-soft">{when(o.created_at)}</div>
                </div>
              </Link>
            ))}
          </div>
        )}
      </Card>

      {/* Row 5 — inbox on the left, the small blocks on the right. */}
      <div className="grid gap-4 lg:grid-cols-[1.6fr_1fr]">
        <Card className="flex flex-col gap-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="text-[15px] font-semibold">
                Approval inbox{" "}
                <span className="font-normal text-ink-muted">
                  {decided} of {decided + pending} decided
                </span>
              </h2>
              <p className="label">Orders the policy held back</p>
            </div>
            <div className="flex items-center gap-3">
              <div className="w-28">
                <Progress value={decided + pending ? decided / (decided + pending) : 1} tone="ink" />
              </div>
              <span className="num text-[13px] font-medium">
                {Math.round((decided + pending ? decided / (decided + pending) : 1) * 100)}%
              </span>
              <Link to="/approvals" className="btn-dark">
                <FileSignature size={14} /> Review
              </Link>
            </div>
          </div>
          {!approvals.data?.length ? (
            <Empty>Nothing waiting. Every order so far cleared the policy on its own.</Empty>
          ) : (
            <ul className="flex flex-col gap-2">
              {approvals.data.map(({ order }) => (
                <li key={order.id}>
                  <Link
                    to={`/orders/${order.id}`}
                    className="flex items-center gap-3 rounded-[var(--radius-tile)] border border-line p-3 transition-colors hover:bg-page"
                  >
                    <span className="size-4 rounded-[5px] border-2 border-line" />
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-[14px] font-medium">{order.lines[0]?.name ?? order.id}</div>
                      <div className="label">
                        {order.policy?.hits.map((h) => h.rule).join(", ") || "policy hit"}
                      </div>
                    </div>
                    <span className="chip bg-warn-soft text-warn">
                      <Countdown to={order.expires_at} />
                    </span>
                    <div className="num w-24 text-right text-[15px] font-semibold">
                      {eur(order.total, order.currency)}
                    </div>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <div className="flex flex-col gap-4">
          <Card className="flex flex-col gap-3">
            <div className="flex items-center justify-between">
              <div>
                <h2 className="text-[15px] font-semibold">Agents</h2>
                <p className="label">Who placed the orders</p>
              </div>
            </div>
            {!s.agents_seen.length ? (
              <Empty>No agent has ordered yet.</Empty>
            ) : (
              <ul className="flex flex-col gap-2.5">
                {s.agents_seen.map((a) => (
                  <li key={a.actor} className="flex items-center gap-3">
                    <Avatar name={a.actor} />
                    <span className="min-w-0 flex-1 truncate text-[13px]">{a.actor.replace(/^agent:/, "")}</span>
                    <span className="num text-[13px] text-ink-muted">{a.order_count}</span>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <Card className="flex flex-col gap-3">
            <div>
              <h2 className="text-[15px] font-semibold">Why humans got involved</h2>
              <p className="label">Policy rules, most frequent first</p>
            </div>
            {!s.top_rule_hits.length ? (
              <Empty>No rule has fired yet.</Empty>
            ) : (
              <ul className="flex flex-col gap-2.5">
                {s.top_rule_hits.slice(0, 4).map((r) => (
                  <li key={r.rule} className="flex flex-col gap-1.5">
                    <div className="flex items-center justify-between text-[13px]">
                      <span className="min-w-0 truncate">{r.rule}</span>
                      <span className="num text-ink-muted">{r.count}</span>
                    </div>
                    <Progress value={r.count / s.top_rule_hits[0].count} tone="warn" />
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <Card className="flex flex-col gap-3">
            <div>
              <h2 className="text-[15px] font-semibold">Spend by day</h2>
              <p className="label">Confirmed orders only</p>
            </div>
            {bars.length ? <Bars data={bars} /> : <Empty>No orders yet.</Empty>}
          </Card>
        </div>
      </div>

      <Card className="flex flex-col gap-3">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-[15px] font-semibold">Audit feed</h2>
            <p className="label">Append-only record of agent and human decisions</p>
          </div>
          <Link to="/audit" className="btn-ghost">
            View all
          </Link>
        </div>
        {!audit.data?.length ? (
          <Empty>No decisions recorded yet.</Empty>
        ) : (
          <ul className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {[...audit.data].reverse().slice(0, 6).map((e) => (
              <li key={e.id} className="flex gap-3 rounded-[var(--radius-tile)] border border-line p-3">
                <Avatar name={e.actor} size={28} />
                <div className="min-w-0">
                  <div className="truncate text-[13px] font-medium">
                    {e.action.replaceAll("_", " ").toLowerCase()}
                  </div>
                  <div className="truncate text-[12px] text-ink-muted">{e.summary ?? e.actor}</div>
                  <div className="text-[11px] text-ink-soft">{when(e.ts)}</div>
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}

function Metric({
  label,
  value,
  foot,
  badge,
  spark,
  tone = "positive",
}: {
  label: string;
  value: string;
  foot?: string;
  badge?: string;
  spark?: number[];
  tone?: "positive" | "warn" | "ink";
}) {
  return (
    <Card className="flex flex-col justify-between gap-3 overflow-hidden !p-0">
      <div className="flex items-start justify-between gap-2 p-5 pb-0">
        <div>
          <div className="label">{label}</div>
          <div className={`metric mt-2 ${tone === "warn" ? "text-warn" : ""}`}>{value}</div>
          {foot ? <div className="mt-1 text-[12px] text-ink-soft">{foot}</div> : null}
        </div>
        {badge ? <span className="chip bg-positive-soft text-positive">↗ {badge}</span> : null}
      </div>
      {spark ? <Sparkline values={spark} tone={tone === "warn" ? "negative" : tone} /> : <div className="h-5" />}
    </Card>
  );
}

function Tile({ label, value, children }: { label: string; value: string; children?: React.ReactNode }) {
  return (
    <Card className="flex items-center justify-between gap-3">
      <div className="min-w-0">
        <div className="label truncate">{label}</div>
        <div className="num mt-1.5 text-[22px] font-semibold leading-none">{value}</div>
      </div>
      {children}
    </Card>
  );
}

function Legend({ tone, label, value }: { tone: string; label: string; value: number }) {
  return (
    <span className="flex items-center gap-1.5 text-ink-muted">
      <span className={`size-2 rounded-full ${tone}`} />
      {label} <span className="num font-semibold text-ink">{value}</span>
    </span>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between border-b border-white/10 pb-2 last:border-0">
      <span>{label}</span>
      <span className="num font-semibold text-white">{value}</span>
    </div>
  );
}

function StripButton({ onClick, children }: { onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      onClick={onClick}
      className="grid size-8 place-items-center rounded-full border border-line text-ink-muted transition-colors hover:bg-page hover:text-ink"
    >
      {children}
    </button>
  );
}
