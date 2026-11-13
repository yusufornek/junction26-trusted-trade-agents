import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { Card, Empty, ErrorNote, LiveDot, Spinner, useAsync, when } from "../ui";

export default function Audit() {
  const { data, error, loading } = useAsync(() => api.audit(), [], 5000);
  const [open, setOpen] = useState<number | null>(null);

  if (error) return <ErrorNote error={error} />;
  if (loading || !data) return <Spinner />;

  const entries = [...data].reverse();

  return (
    <Card className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-[15px] font-semibold">{entries.length} entries</h2>
          <p className="label">Append-only. The database forbids updates and deletes.</p>
        </div>
        <LiveDot />
      </div>

      {!entries.length ? (
        <Empty>Nothing recorded yet.</Empty>
      ) : (
        <ol className="flex flex-col">
          {entries.map((e) => (
            <li key={e.id} className="border-b border-line last:border-0">
              <button
                className="flex w-full items-center gap-3 py-3 text-left"
                onClick={() => setOpen(open === e.id ? null : e.id)}
              >
                <span
                  className={`size-2 shrink-0 rounded-full ${
                    e.actor.startsWith("human") ? "bg-positive" : "bg-ink-soft"
                  }`}
                />
                <span className="w-44 shrink-0 text-[13px] font-medium">
                  {e.action.replaceAll("_", " ").toLowerCase()}
                </span>
                <span className="w-44 shrink-0 text-[12px] text-ink-muted">{e.actor}</span>
                <span className="min-w-0 flex-1 truncate text-[12px] text-ink-muted">{e.summary ?? ""}</span>
                <span className="shrink-0 text-[12px] text-ink-soft">{when(e.ts)}</span>
              </button>
              {open === e.id ? (
                <div className="flex flex-col gap-2 pb-4 pl-5">
                  {e.order_id ? (
                    <Link to={`/orders/${e.order_id}`} className="text-[12px] underline">
                      order {e.order_id.slice(0, 8)}
                    </Link>
                  ) : null}
                  <pre className="overflow-auto rounded-[var(--radius-tile)] bg-page p-3 text-[11px] leading-relaxed">
                    {JSON.stringify(e.data ?? {}, null, 2)}
                  </pre>
                </div>
              ) : null}
            </li>
          ))}
        </ol>
      )}
    </Card>
  );
}
