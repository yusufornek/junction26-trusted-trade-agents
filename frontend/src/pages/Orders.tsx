import { useState } from "react";
import { Link } from "react-router-dom";
import { api, type OrderStatus } from "../api";
import { Avatar } from "../charts";
import { Card, Empty, ErrorNote, Spinner, StatusChip, eur, useAsync, when } from "../ui";

// Every status an order can end in, so no order is ever unreachable by filter.
const FILTERS: (OrderStatus | "ALL")[] = ["ALL", "PENDING_APPROVAL", "CONFIRMED", "REJECTED", "CANCELLED", "EXPIRED"];

export default function Orders() {
  const { data, error, loading } = useAsync(() => api.orders(), [], 5000);
  const [filter, setFilter] = useState<OrderStatus | "ALL">("ALL");

  if (error) return <ErrorNote error={error} />;
  if (loading || !data) return <Spinner />;

  const newest = [...data].sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at));
  const rows = filter === "ALL" ? newest : newest.filter((o) => o.status === filter);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap gap-2">
        {FILTERS.map((f) => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            aria-pressed={filter === f}
            className={`chip h-8 border px-3.5 text-[12px] transition-colors ${
              filter === f
                ? "border-brand bg-brand text-white"
                : "border-line bg-surface text-ink-muted hover:border-brand/30 hover:text-brand"
            }`}
          >
            {f.replaceAll("_", " ").toLowerCase()}
            <span className="num opacity-60">{f === "ALL" ? data.length : data.filter((o) => o.status === f).length}</span>
          </button>
        ))}
      </div>

      <Card className="overflow-hidden !p-0">
        {!rows.length ? (
          <Empty>No orders with this status.</Empty>
        ) : (
          // Below sm each row becomes a small grid (status + total, product, agent + time), so nothing hides
          // off-screen; between sm and the full width the table scrolls inside its card, never the page.
          <div className="overflow-x-auto">
            <table className="w-full text-[13px] max-sm:block sm:min-w-[720px]">
              <thead className="max-sm:hidden">
                <tr className="border-b border-line text-left text-[12px] text-ink-muted">
                  <th className="px-5 py-3 font-medium">Status</th>
                  <th className="px-5 py-3 font-medium">Product</th>
                  <th className="px-5 py-3 font-medium">Agent</th>
                  <th className="px-5 py-3 font-medium">Placed</th>
                  <th className="px-5 py-3 text-right font-medium">Total</th>
                </tr>
              </thead>
              <tbody className="max-sm:block">
                {rows.map((o) => (
                  <tr
                    key={o.id}
                    className="border-b border-line transition-colors last:border-0 hover:bg-brand-soft/40 max-sm:grid max-sm:grid-cols-[minmax(0,1fr)_auto] max-sm:items-center max-sm:gap-x-3 max-sm:gap-y-2 max-sm:px-4 max-sm:py-3.5"
                  >
                    <td className="whitespace-nowrap px-5 py-3 max-sm:p-0">
                      <StatusChip status={o.status} />
                    </td>
                    <td className="px-5 py-3 max-sm:col-span-2 max-sm:row-start-2 max-sm:p-0">
                      <Link to={`/orders/${o.id}`} className="font-medium hover:text-brand hover:underline">
                        {o.lines[0]?.name ?? o.id}
                      </Link>
                      {o.lines.length > 1 ? <span className="text-ink-muted"> + {o.lines.length - 1} more</span> : null}
                    </td>
                    <td className="px-5 py-3 text-ink-muted max-sm:col-start-1 max-sm:row-start-3 max-sm:min-w-0 max-sm:p-0">
                      <span className="flex items-center gap-2 whitespace-nowrap">
                        <Avatar name={o.agent_id} size={24} />
                        <span className="truncate">{o.agent_id}</span>
                      </span>
                    </td>
                    <td className="whitespace-nowrap px-5 py-3 text-ink-muted max-sm:col-start-2 max-sm:row-start-3 max-sm:p-0 max-sm:text-right">
                      {when(o.created_at)}
                    </td>
                    <td className="num whitespace-nowrap px-5 py-3 text-right font-semibold max-sm:col-start-2 max-sm:row-start-1 max-sm:p-0">
                      {eur(o.total, o.currency)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
