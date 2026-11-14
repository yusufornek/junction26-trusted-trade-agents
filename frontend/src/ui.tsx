import { useCallback, useEffect, useState, type ReactNode } from "react";
import { ApiError, type OrderStatus, type Warning } from "./api";

export const eur = (n: number | null | undefined, currency = "EUR") =>
  n == null ? "—" : new Intl.NumberFormat("en-GB", { style: "currency", currency }).format(n);

export const num = (n: number | null | undefined, digits = 0) =>
  n == null ? "—" : new Intl.NumberFormat("en-GB", { maximumFractionDigits: digits }).format(n);

export const pct = (n: number | null | undefined) => (n == null ? "—" : `${Math.round(n * 100)}%`);

export const when = (iso: string | null | undefined) => {
  if (!iso) return "—";
  const d = new Date(iso);
  const mins = Math.round((Date.now() - d.getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  if (mins < 60 * 24) return `${Math.round(mins / 60)} h ago`;
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
};

export const dateOnly = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" }) : "—";

export function Card({ className = "", children }: { className?: string; children: ReactNode }) {
  return <div className={`card p-5 ${className}`}>{children}</div>;
}

export function Metric({
  label,
  value,
  sub,
  accent,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  accent?: "positive" | "negative" | "warn";
}) {
  const color =
    accent === "positive" ? "text-positive" : accent === "negative" ? "text-negative" : accent === "warn" ? "text-warn" : "";
  return (
    <Card className="flex flex-col gap-2">
      <div className="label">{label}</div>
      <div className={`metric ${color}`}>{value}</div>
      {sub ? <div className="text-[12px] text-ink-soft">{sub}</div> : null}
    </Card>
  );
}

const STATUS_STYLE: Record<string, string> = {
  CONFIRMED: "bg-positive-soft text-positive",
  PENDING_APPROVAL: "bg-warn-soft text-warn",
  REJECTED: "bg-negative-soft text-negative",
  EXPIRED: "bg-chip text-ink-muted",
  CANCELLED: "bg-chip text-ink-muted",
  OPEN: "bg-chip text-ink-muted",
  ACCEPTED: "bg-positive-soft text-positive",
};

export function StatusChip({ status }: { status: OrderStatus | string }) {
  return (
    <span className={`chip ${STATUS_STYLE[status] ?? "bg-chip text-ink-muted"}`}>
      {status.replaceAll("_", " ").toLowerCase()}
    </span>
  );
}

export function WarningChips({ warnings }: { warnings: Warning[] }) {
  if (!warnings?.length) return null;
  return (
    <div className="flex flex-wrap gap-1.5">
      {warnings.map((w) => (
        <span key={w.code} className="chip bg-warn-soft text-warn" title={w.text}>
          {w.code.replaceAll("_", " ").toLowerCase()}
        </span>
      ))}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="py-12 text-center text-[13px] text-ink-soft">{children}</div>;
}

export function Spinner({ label = "Loading" }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 py-12 justify-center text-[13px] text-ink-soft">
      <span className="size-3 rounded-full border-2 border-line border-t-ink animate-spin" />
      {label}
    </div>
  );
}

export function ErrorNote({ error }: { error: unknown }) {
  const msg = error instanceof Error ? error.message : String(error);
  // Only blame reachability when it is the likely cause; a 409 or a 500 means the gateway did answer.
  const unreachable =
    error instanceof TypeError || (error instanceof ApiError && error.status >= 502 && error.status <= 504);
  const crashed = error instanceof ApiError && error.status === 500;
  return (
    <div className="card border-negative/30 bg-negative-soft p-4 text-[13px] text-negative">
      {msg}
      {unreachable ? (
        <div className="mt-1 text-[12px] opacity-70">
          Can't reach the gateway — is it running? <code>uvicorn app.api:app --port 8000</code>
        </div>
      ) : crashed ? (
        <div className="mt-1 text-[12px] opacity-70">
          The gateway hit an internal error; its terminal log has the traceback.
        </div>
      ) : null}
    </div>
  );
}

/**
 * Minimal data hook: no cache, explicit reload, optional polling.
 * The backend has no push events, so polling is what keeps a shared console honest
 * when an agent orders while a human is looking at the screen.
 */
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[] = [], pollMs = 0) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);

  const run = useCallback(
    (quiet = false) => {
      if (!quiet) setLoading(true);
      return fn()
        .then((d) => {
          setData(d);
          setError(null);
        })
        .catch(setError)
        .finally(() => setLoading(false));
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    deps,
  );

  useEffect(() => {
    run();
    if (!pollMs) return;
    // Background refreshes stay quiet so the page never flashes a spinner at the user.
    const id = setInterval(() => {
      if (!document.hidden) run(true);
    }, pollMs);
    return () => clearInterval(id);
  }, [run, pollMs]);

  return { data, error, loading, reload: () => run(true) };
}

/** Pending orders hold a fixed price for ~30 minutes; whoever approves needs to see that burn down. */
export function Countdown({ to, className = "" }: { to: string; className?: string }) {
  const [left, setLeft] = useState(() => new Date(to).getTime() - Date.now());

  useEffect(() => {
    setLeft(new Date(to).getTime() - Date.now());
    const id = setInterval(() => setLeft(new Date(to).getTime() - Date.now()), 1000);
    return () => clearInterval(id);
  }, [to]);

  if (left <= 0) return <span className={`text-negative ${className}`}>expired</span>;
  const mins = Math.floor(left / 60000);
  const secs = Math.floor((left % 60000) / 1000);
  const urgent = left < 5 * 60000;
  return (
    <span className={`${urgent ? "text-negative" : "text-ink-muted"} ${className}`}>
      {mins}:{String(secs).padStart(2, "0")} left
    </span>
  );
}

export function LiveDot({ label = "live" }: { label?: string }) {
  return (
    <span className="chip bg-chip text-ink-muted">
      <span className="size-1.5 animate-pulse rounded-full bg-positive" />
      {label}
    </span>
  );
}
