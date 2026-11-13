import { Link } from "react-router-dom";
import { api } from "../api";
import {
  Card,
  Countdown,
  Empty,
  ErrorNote,
  LiveDot,
  Metric,
  Spinner,
  StatusChip,
  eur,
  num,
  useAsync,
  when,
} from "../ui";

const POLL = 5000;

export default function Dashboard() {
  const stats = useAsync(() => api.stats(), [], POLL);
  const approvals = useAsync(() => api.approvals(), [], POLL);
  const audit = useAsync(() => api.audit(), [], POLL);
  const orders = useAsync(() => api.orders(), [], POLL);

  if (stats.error) return <ErrorNote error={stats.error} />;
  if (stats.loading || !stats.data) return <Spinner />;

  const s = stats.data;
  const pending = s.orders_by_status.PENDING_APPROVAL ?? 0;
  const confirmed = s.orders_by_status.CONFIRMED ?? 0;
  const totalOrders = Object.values(s.orders_by_status).reduce((a, b) => a + b, 0);
  const autoShare = totalOrders ? (s.placement_outcomes.CONFIRMED ?? 0) / totalOrders : 0;
  const avgCalls = s.average_live_calls_per_lookup.price + s.average_live_calls_per_lookup.availability;

  return (
    <div className="flex flex-col gap-4">
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Metric
          label="Committed"
          value={eur(s.confirmed_spend_total_eur)}
          sub={`${confirmed} confirmed orders`}
        />
        <Metric
          label="Awaiting human"
          value={pending}
          accent={pending ? "warn" : undefined}
          sub={pending ? "needs a decision" : "inbox clear"}
        />
        <Metric
          label="Decided autonomously"
          value={`${Math.round(autoShare * 100)}%`}
          accent="positive"
          sub={`${s.human_decisions.approved} approved · ${s.human_decisions.rejected} rejected by a human`}
        />
        <Metric
          label="Calls per lookup"
          value={num(avgCalls, 1)}
          sub={`${s.lookups} lookups · ${s.total_decisions_calls} fit checks`}
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-[1.4fr_1fr]">
        <Card className="flex flex-col gap-3">
          <div className="flex items-center justify-between">
            <div>
              <h2 className="text-[15px] font-semibold">Needs your signature</h2>
              <p className="label">Orders the policy would not auto-confirm</p>
            </div>
            <div className="flex items-center gap-2">
              <LiveDot />
              <Link to="/approvals" className="btn-ghost">
                Open inbox
              </Link>
            </div>
          </div>
          {!approvals.data?.length ? (
            <Empty>Nothing waiting. Every order so far cleared the policy on its own.</Empty>
          ) : (
            <ul className="flex flex-col gap-2">
              {approvals.data.slice(0, 4).map(({ order }) => (
                <li key={order.id}>
                  <Link
                    to={`/orders/${order.id}`}
                    className="flex items-center gap-3 rounded-[var(--radius-tile)] border border-line p-3 transition-colors hover:bg-page"
                  >
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-[14px] font-medium">{order.lines[0]?.name ?? order.id}</div>
                      <div className="label">
                        {order.policy?.hits.map((h) => h.rule).join(", ") || "policy hit"} ·{" "}
                        <Countdown to={order.expires_at} />
                      </div>
                    </div>
                    <div className="num text-[15px] font-semibold">{eur(order.total, order.currency)}</div>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card className="flex flex-col gap-3">
          <div className="flex items-center justify-between">
            <div>
              <h2 className="text-[15px] font-semibold">Activity</h2>
              <p className="label">Latest entries in the audit trail</p>
            </div>
            <LiveDot />
          </div>
          {!audit.data?.length ? (
            <Empty>No decisions recorded yet.</Empty>
          ) : (
            <ul className="flex flex-col gap-3">
              {[...audit.data].reverse().slice(0, 6).map((e) => (
                <li key={e.id} className="flex gap-3">
                  <span
                    className={`mt-1.5 size-2 shrink-0 rounded-full ${
                      e.actor.startsWith("human") ? "bg-positive" : "bg-ink-soft"
                    }`}
                  />
                  <div className="min-w-0">
                    <div className="text-[13px]">
                      <span className="font-medium">{e.action.replaceAll("_", " ").toLowerCase()}</span>{" "}
                      <span className="text-ink-muted">by {e.actor}</span>
                    </div>
                    <div className="text-[12px] text-ink-soft">{when(e.ts)}</div>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <Card className="flex flex-col gap-3">
          <div>
            <h2 className="text-[15px] font-semibold">Why humans got involved</h2>
            <p className="label">Most frequent policy rules</p>
          </div>
          {!s.top_rule_hits.length ? (
            <Empty>No rule has fired yet.</Empty>
          ) : (
            <ul className="flex flex-col gap-2">
              {s.top_rule_hits.map((r) => (
                <li key={r.rule} className="flex items-center gap-3 text-[13px]">
                  <span className="min-w-0 flex-1 truncate">{r.rule}</span>
                  <Bar value={r.count} max={s.top_rule_hits[0].count} tone="warn" />
                  <span className="num w-6 text-right text-ink-muted">{r.count}</span>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card className="flex flex-col gap-3">
          <div>
            <h2 className="text-[15px] font-semibold">What the data pushed back on</h2>
            <p className="label">Most frequent warnings</p>
          </div>
          {!s.top_warning_codes.length ? (
            <Empty>No warnings recorded.</Empty>
          ) : (
            <ul className="flex flex-col gap-2">
              {s.top_warning_codes.map((w) => (
                <li key={w.code} className="flex items-center gap-3 text-[13px]">
                  <span className="min-w-0 flex-1 truncate">{w.code.replaceAll("_", " ").toLowerCase()}</span>
                  <Bar value={w.count} max={s.top_warning_codes[0].count} tone="ink" />
                  <span className="num w-6 text-right text-ink-muted">{w.count}</span>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card className="flex flex-col gap-3">
          <div>
            <h2 className="text-[15px] font-semibold">Agents</h2>
            <p className="label">Who placed the orders</p>
          </div>
          {!s.agents_seen.length ? (
            <Empty>No agent has ordered yet.</Empty>
          ) : (
            <ul className="flex flex-col gap-2">
              {s.agents_seen.map((a) => (
                <li key={a.actor} className="flex items-center gap-3 text-[13px]">
                  <span className="min-w-0 flex-1 truncate">{a.actor}</span>
                  <span className="num text-ink-muted">{a.order_count}</span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      <Card className="flex flex-col gap-3">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-[15px] font-semibold">Recent orders</h2>
            <p className="label">Newest first</p>
          </div>
          <Link to="/orders" className="btn-ghost">
            View all
          </Link>
        </div>
        {!orders.data?.length ? (
          <Empty>
            No orders yet — run the{" "}
            <Link to="/buyer" className="underline">
              buyer flow
            </Link>
            .
          </Empty>
        ) : (
          <ul className="flex flex-col gap-2">
            {[...orders.data].reverse().slice(0, 5).map((o) => (
              <li key={o.id}>
                <Link
                  to={`/orders/${o.id}`}
                  className="flex items-center gap-3 rounded-[var(--radius-tile)] border border-line p-3 transition-colors hover:bg-page"
                >
                  <StatusChip status={o.status} />
                  <div className="min-w-0 flex-1 truncate text-[14px]">{o.lines[0]?.name ?? "—"}</div>
                  <div className="label hidden sm:block">{o.agent_id}</div>
                  <div className="num text-[15px] font-semibold">{eur(o.total, o.currency)}</div>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}

function Bar({ value, max, tone }: { value: number; max: number; tone: "warn" | "ink" }) {
  return (
    <span className="h-1.5 w-20 overflow-hidden rounded-full bg-chip">
      <span
        className={`block h-full rounded-full ${tone === "warn" ? "bg-warn" : "bg-ink-soft"}`}
        style={{ width: `${Math.max(8, (value / Math.max(max, 1)) * 100)}%` }}
      />
    </span>
  );
}
