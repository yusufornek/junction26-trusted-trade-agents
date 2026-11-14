import { useState } from "react";
import { Link } from "react-router-dom";
import { api, type OrderStatus } from "../api";
import { Avatar } from "../charts";
import { Card, Empty, ErrorNote, Spinner, StatusChip, eur, useAsync, when } from "../ui";

const FILTERS: (OrderStatus | "ALL")[] = ["ALL", "PENDING_APPROVAL", "CONFIRMED", "REJECTED", "EXPIRED"];

export default function Orders() {
  const { data, error, loading } = useAsync(() => api.orders(), [], 5000);
  const [filter, setFilter] = useState<OrderStatus | "ALL">("ALL");

  if (error) return <ErrorNote error={error} />;
  if (loading || !data) return <Spinner />;

  const rows = filter === "ALL" ? data : data.filter((o) => o.status === filter);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap gap-2">
        {FILTERS.map((f) => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            className={`chip border transition-colors ${
              filter === f ? "border-ink bg-ink text-white" : "border-line bg-surface text-ink-muted hover:bg-chip"
            }`}
          >
            {f.replaceAll("_", " ").toLowerCase()}
            <span className="opacity-60">{f === "ALL" ? data.length : data.filter((o) => o.status === f).length}</span>
          </button>
        ))}
      </div>

      <Card className="p-0">
        {!rows.length ? (
          <Empty>No orders with this status.</Empty>
        ) : (
          <table className="w-full text-[13px]">
            <thead>
              <tr className="border-b border-line text-left text-ink-soft">
                <th className="px-5 py-3 font-medium">Status</th>
                <th className="px-5 py-3 font-medium">Product</th>
                <th className="px-5 py-3 font-medium">Agent</th>
                <th className="px-5 py-3 font-medium">Placed</th>
                <th className="px-5 py-3 text-right font-medium">Total</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((o) => (
                <tr key={o.id} className="border-b border-line last:border-0 hover:bg-page">
                  <td className="px-5 py-3">
                    <StatusChip status={o.status} />
                  </td>
                  <td className="max-w-80 truncate px-5 py-3">
                    <Link to={`/orders/${o.id}`} className="hover:underline">
                      {o.lines[0]?.name ?? o.id}
                    </Link>
                  </td>
                  <td className="px-5 py-3 text-ink-muted">
                    <span className="flex items-center gap-2">
                      <Avatar name={o.agent_id} size={24} />
                      {o.agent_id}
                    </span>
                  </td>
                  <td className="px-5 py-3 text-ink-muted">{when(o.created_at)}</td>
                  <td className="num px-5 py-3 text-right font-medium">{eur(o.total, o.currency)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );
}
