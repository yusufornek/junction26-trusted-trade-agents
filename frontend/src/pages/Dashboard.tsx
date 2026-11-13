import { Link } from "react-router-dom";
import { api } from "../api";
import { Card, Empty, ErrorNote, Metric, Spinner, StatusChip, eur, useAsync, when } from "../ui";

export default function Dashboard() {
  const orders = useAsync(() => api.orders(), []);
  const approvals = useAsync(() => api.approvals(), []);
  const audit = useAsync(() => api.audit(), []);

  if (orders.error) return <ErrorNote error={orders.error} />;
  if (orders.loading || !orders.data) return <Spinner />;

  const all = orders.data;
  const confirmed = all.filter((o) => o.status === "CONFIRMED");
  const pending = all.filter((o) => o.status === "PENDING_APPROVAL");
  const committed = confirmed.reduce((s, o) => s + o.total, 0);
  const liveCalls = all.reduce((s, o) => s + (o.live_calls?.price ?? 0) + (o.live_calls?.availability ?? 0), 0);
  const autonomyRate = all.length ? confirmed.length / all.length : 0;

  return (
    <div className="flex flex-col gap-4">
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Metric label="Committed" value={eur(committed)} sub={`${confirmed.length} confirmed orders`} />
        <Metric
          label="Awaiting human"
          value={pending.length}
          accent={pending.length ? "warn" : undefined}
          sub={pending.length ? "needs a decision" : "inbox clear"}
        />
        <Metric
          label="Decided autonomously"
          value={`${Math.round(autonomyRate * 100)}%`}
          accent="positive"
          sub={`${confirmed.length} of ${all.length} orders`}
        />
        <Metric label="Upstream calls" value={liveCalls} sub="price + availability, on demand only" />
      </div>

      <div className="grid gap-4 lg:grid-cols-[1.4fr_1fr]">
        <Card className="flex flex-col gap-3">
          <div className="flex items-center justify-between">
            <div>
              <h2 className="text-[15px] font-semibold">Needs your signature</h2>
              <p className="label">Orders the policy would not auto-confirm</p>
            </div>
            <Link to="/approvals" className="btn-ghost">
              Open inbox
            </Link>
          </div>
          {approvals.loading ? (
            <Spinner />
          ) : !approvals.data?.length ? (
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
                        {order.policy?.hits.map((h) => h.rule).join(", ") || "policy hit"} · {when(order.created_at)}
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
          <div>
            <h2 className="text-[15px] font-semibold">Activity</h2>
            <p className="label">Latest entries in the audit trail</p>
          </div>
          {audit.loading ? (
            <Spinner />
          ) : !audit.data?.length ? (
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
        {!all.length ? (
          <Empty>
            No orders yet — run the <Link to="/buyer" className="underline">buyer flow</Link>.
          </Empty>
        ) : (
          <ul className="flex flex-col gap-2">
            {all.slice(0, 5).map((o) => (
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
