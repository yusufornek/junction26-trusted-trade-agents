import { Link } from "react-router-dom";
import { useRef } from "react";
import { ChevronLeft, ChevronRight, FileSignature, Inbox, Plus } from "lucide-react";
import { api, type Order } from "../api";
import { useRange } from "../Shell";
import { AreaChart, Avatar, Bars, Progress, Ring, Sparkline, type Marker } from "../charts";
import { Card, Countdown, Empty, ErrorNote, Spinner, StatusChip, eur, num, useAsync, when } from "../ui";

const POLL = 5000;
const HOUR = 3600000;
const DAY = 24 * HOUR;
/** A trend line needs this many buckets that actually moved; fewer reads as a broken chart. */
const MIN_TREND_POINTS = 4;
/** One activity card plus the gap between cards: what each strip arrow scrolls by. */
const STRIP_STEP = 244;

/** Status colours as everywhere else: confirmed green, pending amber, rejected coral; ink stands in for grey. */
const MARKER_TONE: Record<Order["status"], Marker["tone"]> = {
  CONFIRMED: "positive",
  PENDING_APPROVAL: "warn",
  REJECTED: "negative",
  CANCELLED: "muted",
  EXPIRED: "muted",
};

/** "09:00" for hours (the date at midnight), "13 Nov" for UTC days. */
function bucketLabel(t: number, size: number) {
  const d = new Date(t);
  return size === HOUR && d.getHours() !== 0
    ? d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: size === DAY ? "UTC" : undefined });
}

/**
 * Running total in buckets from the first event to now. The gateway stores events, not a time series.
 * Hourly while the history is under three days, so a young gateway draws a curve instead of one spike
 * at the right edge; daily after that. `moves` counts the buckets that changed the total.
 */
function runningTotal(events: { t: number; v: number }[], fixedSize?: number) {
  if (!events.length) return { values: [] as number[], labels: [] as string[], index: () => 0, moves: 0 };
  const first = Math.min(...events.map((e) => e.t));
  const size = fixedSize ?? (Date.now() - first < 3 * DAY ? HOUR : DAY);
  const start = Math.floor(first / size) * size;
  const count = Math.floor((Date.now() - start) / size) + 1;
  const index = (t: number) => Math.min(count - 1, Math.max(0, Math.floor((t - start) / size)));
  const per = new Array<number>(count).fill(0);
  for (const e of events) per[index(e.t)] += e.v;
  let total = 0;
  return {
    values: per.map((v) => (total += v)),
    labels: per.map((_, i) => bucketLabel(start + i * size, size)),
    index,
    moves: per.filter(Boolean).length,
  };
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
  const decidedShare = decided + pending ? decided / (decided + pending) : 1;

  const days = rangeLabel.includes("7") ? 7 : rangeLabel.includes("30") ? 30 : 0;
  const from = days ? Date.now() - days * DAY : 0;
  const placedAt = (o: Order) => Date.parse(o.created_at);
  const inRange = all.filter((o) => placedAt(o) >= from);
  const spendEvents = inRange.map((o) => ({ t: placedAt(o), v: o.status === "CONFIRMED" ? o.total : 0 }));
  // The curve starts at the first order in range, not at a long run of empty buckets.
  const spend = runningTotal(spendEvents);
  const markers: Marker[] = inRange.map((o) => ({ index: spend.index(placedAt(o)), label: o.id, tone: MARKER_TONE[o.status] }));
  // Lookups as the stats count them: LOOKUP entries in the audit trail.
  const lookups = runningTotal(
    (audit.data ?? []).filter((e) => e.action === "LOOKUP" && Date.parse(e.ts) >= from).map((e) => ({ t: Date.parse(e.ts), v: 1 })),
  );

  // Last days as columns, like the reference's three-month block.
  const daily = runningTotal(spendEvents, DAY);
  const bars = daily.labels.slice(-4).map((label, i, arr) => {
    const idx = daily.values.length - arr.length + i;
    const amount = daily.values[idx] - (idx > 0 ? daily.values[idx - 1] : 0);
    return { label, value: amount, caption: eur(amount) };
  });

  const feed = [...(audit.data ?? [])].reverse().slice(0, 6);
  const scrollStrip = (direction: 1 | -1) =>
    strip.current?.scrollBy({ left: direction * STRIP_STEP, behavior: "smooth" });

  return (
    <div className="flex flex-col gap-4">
      {/* Top: four metrics over the spend curve; the summary card over the live feed on the right.
          On lg+ the feed list has no height of its own, so the left column sets the row height and both columns end flush. */}
      <section className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_280px] xl:grid-cols-[minmax(0,1fr)_300px]">
        <div className="flex min-w-0 flex-col gap-4">
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <Metric
              label="Committed"
              value={eur(s.confirmed_spend_total_eur)}
              foot={`${confirmed} confirmed`}
              badge={autoShare > 0 ? `${Math.round(autoShare * 100)}% auto` : undefined}
              trend={spend.moves >= MIN_TREND_POINTS ? spend.values : undefined}
            />
            <Metric
              label="Awaiting human"
              value={String(pending)}
              foot={pending ? "needs a decision" : "inbox clear"}
              warn={pending > 0}
            />
            <Metric
              label="Lookups"
              value={String(s.lookups)}
              foot={`${s.total_decisions_calls} fit checks`}
              trend={lookups.moves >= MIN_TREND_POINTS ? lookups.values : undefined}
            />
            <Metric label="Live calls per lookup" value={num(avgCalls, 1)} foot="price + availability" />
          </div>

          <Card className="flex flex-1 flex-col">
            <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
              <div>
                <div className="label">Committed spend</div>
                <div className="metric mt-2">{eur(s.confirmed_spend_total_eur)}</div>
                <span className="chip mt-3 bg-brand-soft text-brand">{rangeLabel.toLowerCase()}</span>
              </div>
              <div className="flex flex-wrap gap-x-4 gap-y-1.5 text-[12px]">
                <Legend tone="bg-positive" label="Confirmed" value={confirmed} />
                <Legend tone="bg-warn" label="Pending" value={pending} />
                <Legend tone="bg-negative" label="Rejected" value={byStatus.REJECTED ?? 0} />
                <Legend
                  tone="bg-ink-soft"
                  label="Cancelled / expired"
                  value={(byStatus.CANCELLED ?? 0) + (byStatus.EXPIRED ?? 0)}
                />
              </div>
            </div>
            <div className="mt-auto pt-6">
              <AreaChart values={spend.values} labels={spend.labels} markers={markers} height={300} />
            </div>
          </Card>
        </div>

        <div className="flex min-w-0 flex-col gap-4">
          <div className="relative flex flex-col overflow-hidden rounded-[var(--radius-card)] bg-gradient-to-br from-brand to-brand-strong p-5 text-white">
            <span aria-hidden className="pointer-events-none absolute -right-20 -top-24 size-56 rounded-full bg-white/10 blur-2xl" />
            <div className="relative flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="text-[13px] text-white/70">Trusted Trade Agents</div>
                <div className="num mt-3 whitespace-nowrap text-[34px] font-semibold leading-none">
                  {eur(s.confirmed_spend_total_eur)}
                </div>
                <div className="mt-1.5 text-[13px] text-white/70">committed · {rangeLabel.toLowerCase()}</div>
              </div>
              <Avatar name="operator" size={34} />
            </div>

            <dl className="relative mt-4 flex flex-col text-[12px] text-white/75">
              <SummaryRow label="Decided by the agent" value={s.placement_outcomes.CONFIRMED ?? 0} />
              <SummaryRow label="Signed off by a human" value={s.human_decisions.approved} />
              <SummaryRow label="Rejected" value={s.human_decisions.rejected + (s.placement_outcomes.REJECTED ?? 0)} />
            </dl>

            {/* Content-sized halves: equal halves wrap "New request" in the 280px column. */}
            <div className="relative mt-4 flex gap-2">
              <Link
                to="/buyer"
                className="inline-flex h-10 flex-auto items-center justify-center gap-2 whitespace-nowrap rounded-full bg-white px-3 text-[13px] font-medium text-brand transition-colors hover:bg-white/90"
              >
                <Plus size={14} /> New request
              </Link>
              <Link
                to="/approvals"
                className="inline-flex h-10 flex-auto items-center justify-center gap-2 whitespace-nowrap rounded-full bg-white/15 px-3 text-[13px] font-medium text-white transition-colors hover:bg-white/25"
              >
                <Inbox size={14} /> Inbox
              </Link>
            </div>
          </div>

          <Card className="flex flex-col gap-4 lg:min-h-[220px] lg:flex-1">
            <CardHead title="Activity" sub="Audit trail, newest first">
              <Link to="/audit" className="text-[12px] font-medium text-brand hover:underline">
                View all
              </Link>
            </CardHead>
            {!feed.length ? (
              <Empty>No decisions recorded yet.</Empty>
            ) : (
              // basis-0 (not flex-1's 0%): a percentage basis falls back to content height while the grid sizes the row.
              <ul className="flex flex-col gap-4 overflow-y-auto lg:grow lg:basis-0">
                {feed.map((e) => (
                  <li key={e.id} className="flex gap-3">
                    <Avatar name={e.actor} size={30} />
                    <div className="min-w-0 flex-1">
                      <p className="line-clamp-2 text-[13px] leading-snug">
                        <span className="font-semibold">{e.action.replaceAll("_", " ").toLowerCase()}</span>
                        {e.summary ? <span className="text-ink-muted"> · {e.summary}</span> : null}
                      </p>
                      <p className="mt-1 truncate text-[11px] text-ink-muted" title={e.actor}>
                        {e.actor} <span className="text-ink-soft">· {when(e.ts)}</span>
                      </p>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>
      </section>

      {/* The thin tile strip. */}
      <section className="grid grid-cols-2 gap-4 md:grid-cols-3 xl:grid-cols-6">
        <Tile label="Orders placed" value={String(placed)} />
        <Tile label="Human decisions" value={String(decided)} />
        <Tile label="Fit checks" value={String(s.total_decisions_calls)} />
        <Tile label="Agents" value={String(s.agents_seen.length)} />
        <Tile label="Autonomy" value={`${Math.round(autoShare * 100)}%`}>
          <Ring value={autoShare} size={40} tone={autoShare > 0.5 ? "positive" : "warn"} />
        </Tile>
        <Tile
          label="Approval rate"
          value={decided ? `${Math.round((s.human_decisions.approved / decided) * 100)}%` : "—"}
        >
          <Ring value={decided ? s.human_decisions.approved / decided : 0} size={40} />
        </Tile>
      </section>

      {/* The activity strip: scrolls inside its card, never the page. */}
      <Card className="flex flex-col gap-4">
        <CardHead title="Latest activity" sub="Orders, newest first">
          <div className="flex gap-1.5">
            <StripButton label="Scroll back" onClick={() => scrollStrip(-1)}>
              <ChevronLeft size={15} />
            </StripButton>
            <StripButton label="Scroll forward" onClick={() => scrollStrip(1)}>
              <ChevronRight size={15} />
            </StripButton>
          </div>
        </CardHead>
        {!all.length ? (
          <Empty>
            Nothing yet — run the{" "}
            <Link to="/buyer" className="underline">
              buyer flow
            </Link>
            .
          </Empty>
        ) : (
          <div
            ref={strip}
            className="-mx-5 flex snap-x snap-mandatory scroll-px-5 gap-3 overflow-x-auto px-5 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
          >
            {[...all].reverse().map((o) => (
              <Link
                key={o.id}
                to={`/orders/${o.id}`}
                className="flex w-[232px] shrink-0 snap-start flex-col rounded-[var(--radius-tile)] border border-line p-4 transition-colors hover:border-brand/25 hover:bg-brand-soft/40"
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="chip bg-chip text-ink-muted">order</span>
                  <StatusChip status={o.status} />
                </div>
                <div className="mt-4 flex min-w-0 items-center gap-2">
                  <Avatar name={o.agent_id} size={26} />
                  <span className="truncate text-[12px] text-ink-muted">{o.agent_id}</span>
                </div>
                <div className="mt-3 line-clamp-2 min-h-[2lh] text-[13px] font-medium leading-snug" title={o.lines[0]?.name}>
                  {o.lines[0]?.name ?? o.id}
                </div>
                <div className="num mt-2 text-[20px] font-semibold leading-tight">{eur(o.total, o.currency)}</div>
                <div className="mt-1 text-[12px] text-ink-soft">{when(o.created_at)}</div>
              </Link>
            ))}
          </div>
        )}
      </Card>

      {/* Bottom: the inbox and why it fills on the left, agents and daily spend on the right.
          Same column template as the top so edges line up; the last card in each column absorbs the difference. */}
      <section className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_280px] xl:grid-cols-[minmax(0,1fr)_300px]">
        <div className="flex min-w-0 flex-col gap-4">
          <Card className="flex flex-col gap-4">
            <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3">
              <div className="min-w-0">
                <h2 className="text-[15px] font-semibold">
                  Approval inbox{" "}
                  <span className="ml-1 text-[13px] font-normal text-ink-muted">
                    {decided} of {decided + pending} decided
                  </span>
                </h2>
                <p className="label">Orders the policy held back</p>
              </div>
              <div className="flex items-center gap-3">
                <div className="w-28">
                  <Progress value={decidedShare} tone="brand" />
                </div>
                <span className="num w-9 text-right text-[13px] font-medium">{Math.round(decidedShare * 100)}%</span>
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
                      className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-[var(--radius-tile)] border border-line px-4 py-3 transition-colors hover:border-brand/25 hover:bg-brand-soft/40"
                    >
                      <span className="size-4 shrink-0 rounded-[5px] border-2 border-line" />
                      <div className="min-w-0 flex-1 basis-48">
                        <div className="text-[14px] font-medium leading-snug">{order.lines[0]?.name ?? order.id}</div>
                        <div className="mt-0.5 text-[12px] text-ink-muted">
                          {order.agent_id} · {order.policy?.hits.map((h) => h.rule).join(", ") || "policy hit"}
                        </div>
                      </div>
                      <div className="ml-auto flex shrink-0 items-center gap-3">
                        <span className="chip bg-warn-soft text-warn">
                          <Countdown to={order.expires_at} />
                        </span>
                        <span className="num min-w-24 text-right text-[15px] font-semibold">
                          {eur(order.total, order.currency)}
                        </span>
                      </div>
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <Card className="flex flex-1 flex-col gap-4">
            <CardHead title="Why humans got involved" sub="Policy rules, most frequent first" />
            {!s.top_rule_hits.length ? (
              <Empty>No rule has fired yet.</Empty>
            ) : (
              <ul className="flex flex-col gap-3.5">
                {s.top_rule_hits.slice(0, 4).map((r) => (
                  <li key={r.rule} className="flex flex-col gap-2">
                    <div className="flex items-baseline justify-between gap-3 text-[13px]">
                      <span className="min-w-0 break-words font-medium">{r.rule}</span>
                      <span className="num shrink-0 text-ink-muted">
                        {r.count} {r.count === 1 ? "hit" : "hits"}
                      </span>
                    </div>
                    <Progress value={r.count / s.top_rule_hits[0].count} tone="warn" />
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>

        <div className="flex min-w-0 flex-col gap-4">
          <Card className="flex flex-col gap-4">
            <CardHead title="Agents" sub="Who placed the orders">
              <Link to="/agents" className="text-[12px] font-medium text-brand hover:underline">
                View all
              </Link>
            </CardHead>
            {!s.agents_seen.length ? (
              <Empty>No agent has ordered yet.</Empty>
            ) : (
              <ul className="flex flex-col gap-3">
                {s.agents_seen.map((a) => (
                  <li key={a.actor} className="flex items-center gap-3">
                    <Avatar name={a.actor} size={32} />
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-[13px] font-medium">{a.actor.replace(/^agent:/, "")}</div>
                      {a.registered == null ? null : a.registered ? (
                        <span className="chip mt-1 bg-positive-soft text-positive">registered</span>
                      ) : (
                        <span className="chip mt-1 bg-warn-soft text-warn">unregistered</span>
                      )}
                    </div>
                    <div className="shrink-0 text-right">
                      <div className="num text-[15px] font-semibold leading-none">{a.order_count}</div>
                      <div className="mt-1 text-[11px] text-ink-soft">{a.order_count === 1 ? "order" : "orders"}</div>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <Card className="flex flex-1 flex-col gap-4">
            <CardHead title="Spend by day" sub="Confirmed orders only" />
            {bars.length ? (
              <div className="mt-auto">
                <Bars data={bars} tone="brand" />
              </div>
            ) : (
              <Empty>No orders yet.</Empty>
            )}
          </Card>
        </div>
      </section>
    </div>
  );
}

/**
 * Three fixed-height rows in every KPI card (name, number, one-line foot), so a row of them shares one height
 * and every baseline whether or not a card has a trend. The trend line sits beside the name, never in a slot
 * that would stand empty on cards without one.
 */
function Metric({
  label,
  value,
  foot,
  badge,
  trend,
  warn = false,
}: {
  label: string;
  value: string;
  foot: string;
  badge?: string;
  trend?: number[];
  warn?: boolean;
}) {
  return (
    <Card className="@container flex min-w-0 flex-col">
      <div className="flex h-6 items-center justify-between gap-3">
        <span className="label whitespace-nowrap">{label}</span>
        {trend ? (
          <div className="w-16 shrink-0">
            <Sparkline values={trend} tone="brand" height={24} />
          </div>
        ) : null}
      </div>
      {/* 24px while the card is narrow (4-up at 1280), so a six-figure total still fits on one line. */}
      <div
        className={`num mt-3 whitespace-nowrap text-[24px] font-semibold leading-7 @min-[184px]:text-[28px] ${warn ? "text-warn" : ""}`}
      >
        {value}
      </div>
      <div className="mt-2.5 flex h-[22px] min-w-0 items-center gap-2 text-[12px] text-ink-muted">
        <span className="min-w-0 truncate" title={foot}>
          {foot}
        </span>
        {badge ? (
          <span className="chip bg-brand-soft text-brand" title="Share of orders the agent confirmed on its own">
            {badge}
          </span>
        ) : null}
      </div>
    </Card>
  );
}

function Tile({ label, value, children }: { label: string; value: string; children?: React.ReactNode }) {
  return (
    <Card className="flex min-h-[104px] min-w-0 items-center justify-between gap-2.5">
      <div className="flex min-w-0 flex-col self-stretch">
        <div className="label leading-snug">{label}</div>
        <div className="num mt-auto pt-2 text-[24px] font-semibold leading-none">{value}</div>
      </div>
      {children ? <div className="shrink-0">{children}</div> : null}
    </Card>
  );
}

function CardHead({ title, sub, children }: { title: string; sub: string; children?: React.ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-3">
      <div className="min-w-0">
        <h2 className="text-[15px] font-semibold">{title}</h2>
        <p className="label">{sub}</p>
      </div>
      {children ? <div className="shrink-0 pt-0.5">{children}</div> : null}
    </div>
  );
}

function Legend({ tone, label, value }: { tone: string; label: string; value: number }) {
  return (
    <span className="flex items-center gap-1.5 whitespace-nowrap text-ink-muted">
      <span className={`size-2 rounded-full ${tone}`} />
      {label} <span className="num font-semibold text-ink">{value}</span>
    </span>
  );
}

function SummaryRow({ label, value }: { label: string; value: number }) {
  return (
    <div className="flex items-center justify-between gap-3 border-b border-white/15 py-2 last:border-0 last:pb-0">
      <dt>{label}</dt>
      <dd className="num text-[13px] font-semibold text-white">{value}</dd>
    </div>
  );
}

function StripButton({ label, onClick, children }: { label: string; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      onClick={onClick}
      aria-label={label}
      className="grid size-8 place-items-center rounded-full border border-line text-ink-muted transition-colors hover:border-brand/30 hover:bg-brand-soft hover:text-brand"
    >
      {children}
    </button>
  );
}
