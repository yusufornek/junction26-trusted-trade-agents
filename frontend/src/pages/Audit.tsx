import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { Avatar } from "../charts";
import { Card, Empty, ErrorNote, LiveDot, Spinner, useAsync, when } from "../ui";

export default function Audit() {
  const { data, error, loading } = useAsync(() => api.audit(), [], 5000);
  const [open, setOpen] = useState<number | null>(null);

  if (error) return <ErrorNote error={error} />;
  if (loading || !data) return <Spinner />;

  const entries = [...data].reverse();

  return (
    <Card className="flex flex-col gap-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
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
                className="flex w-full items-start gap-3 py-3 text-left md:items-center"
                onClick={() => setOpen(open === e.id ? null : e.id)}
                aria-expanded={open === e.id}
              >
                <Avatar name={e.actor} size={26} />
                {/* Phone: action and actor on one line, the summary wrapped below. md+: fixed columns, summary truncated
                    (it is shown in full once the row is opened). Nothing here forces a minimum width. */}
                <div className="flex min-w-0 flex-1 flex-col gap-0.5 md:flex-row md:items-center md:gap-3">
                  <div className="flex min-w-0 flex-wrap items-baseline gap-x-2 md:contents">
                    <span className="shrink-0 text-[13px] font-medium md:w-44">
                      {e.action.replaceAll("_", " ").toLowerCase()}
                    </span>
                    {/* The actor is evidence: it wraps to its own line on a phone instead of being cut. */}
                    <span className="min-w-0 break-all text-[12px] text-ink-muted md:w-44 md:shrink-0 md:truncate md:break-normal" title={e.actor}>
                      {e.actor}
                    </span>
                  </div>
                  {e.summary ? (
                    <span className="min-w-0 text-[12px] text-ink-muted md:flex-1 md:truncate">{e.summary}</span>
                  ) : null}
                </div>
                <span className="shrink-0 text-[12px] text-ink-soft">{when(e.ts)}</span>
              </button>
              {open === e.id ? (
                <div className="flex min-w-0 flex-col gap-2 pb-4 pl-[38px]">
                  {e.summary ? <p className="hidden text-[13px] md:block">{e.summary}</p> : null}
                  {e.order_id ? (
                    <Link to={`/orders/${e.order_id}`} className="self-start text-[12px] font-medium text-brand underline">
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
