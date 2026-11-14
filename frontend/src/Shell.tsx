import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate, useOutletContext } from "react-router-dom";
import {
  Activity,
  Bell,
  Bot,
  ChevronDown,
  CircleHelp,
  Fingerprint,
  Inbox,
  LayoutGrid,
  LogOut,
  Package,
  Plus,
  ScrollText,
  Search,
  ShieldCheck,
} from "lucide-react";
import { api, hasToken, onAuthChange, setToken } from "./api";
import { Avatar } from "./charts";
import { Card, ErrorNote, useAsync } from "./ui";

const NAV = [
  { to: "/", icon: LayoutGrid, label: "Dashboard" },
  { to: "/buyer", icon: Search, label: "Buyer flow" },
  { to: "/approvals", icon: Inbox, label: "Approvals" },
  { to: "/orders", icon: Package, label: "Orders" },
  { to: "/audit", icon: ScrollText, label: "Audit" },
  { to: "/policy", icon: ShieldCheck, label: "Policy" },
  { to: "/agents", icon: Bot, label: "Agents" },
  { to: "/trust", icon: Fingerprint, label: "Trust & connect" },
];

const TITLES: Record<string, { title: string; sub: string }> = {
  "/": { title: "Good day, operator", sub: "Here is what the purchasing agent did on your behalf." },
  "/buyer": { title: "Buyer flow", sub: "Describe the need, compare offers, lock a price, order." },
  "/approvals": { title: "Approval inbox", sub: "Orders the agent would not confirm on its own." },
  "/orders": { title: "Orders", sub: "Every commitment this gateway has made." },
  "/audit": { title: "Audit trail", sub: "Append-only record of agent and human decisions." },
  "/policy": { title: "Policy", sub: "The guardrails the buyer owns, not the code." },
  "/agents": { title: "Agents", sub: "Who may buy, for which customer, and up to what limit." },
  "/trust": { title: "Trust & connect", sub: "Verify what the gateway recorded, and plug an outside agent in." },
};

export const RANGES = [
  { label: "Last 7 days", days: 7 },
  { label: "Last 30 days", days: 30 },
  { label: "All time", days: 0 },
];

export type ShellContext = { since: string | undefined; rangeLabel: string };
export const useRange = () => useOutletContext<ShellContext>();

export default function Shell() {
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const [signedIn, setSignedIn] = useState(hasToken);
  const [tokenInput, setTokenInput] = useState("");
  const [signingIn, setSigningIn] = useState(false);
  const [signInError, setSignInError] = useState<unknown>(null);
  const [rangeIndex, setRangeIndex] = useState(1);
  const [rangeOpen, setRangeOpen] = useState(false);

  useEffect(() => onAuthChange(() => setSignedIn(hasToken())), []);

  const signIn = async (event: React.FormEvent) => {
    event.preventDefault();
    setSigningIn(true);
    setSignInError(null);
    // Keep the outlet unmounted until the token is verified as an operator token.
    setToken(tokenInput);
    setTokenInput("");
    try {
      await api.policy();
      setSignedIn(true);
    } catch (error) {
      setToken("");
      setSignInError(error);
    } finally {
      setSigningIn(false);
    }
  };

  const health = useAsync(() => api.health(), []);
  const approvals = useAsync(() => (signedIn ? api.approvals() : Promise.resolve([])), [signedIn], 10000);
  const head =
    TITLES[pathname] ??
    (pathname.startsWith("/products/")
      ? { title: "Product", sub: "Catalog facts the agent judged this product on." }
      : { title: "Order", sub: "Commitment detail" });
  const range = RANGES[rangeIndex];
  const since = range.days ? new Date(Date.now() - range.days * 86400000).toISOString() : undefined;
  const waiting = approvals.data?.length ?? 0;

  // One class per rail button so every icon sits on the same 40px grid, vertical rail or mobile bar.
  const railItem =
    "relative grid size-10 shrink-0 place-items-center rounded-2xl transition-colors";
  const railIdle = `${railItem} text-white/55 hover:bg-white/10 hover:text-white`;

  return (
    <div className="flex min-h-screen flex-col gap-4 p-4 md:flex-row">
      {/* Floating navy rail; below md it becomes a bar that scrolls inside itself, never the page. */}
      <nav className="flex shrink-0 items-center gap-1 overflow-x-auto rounded-[22px] bg-rail p-2 [scrollbar-width:none] md:sticky md:top-4 md:h-[calc(100vh-2rem)] md:w-[72px] md:flex-col md:overflow-x-hidden md:overflow-y-auto md:rounded-[26px] md:px-0 md:py-4">
        <div className="grid size-10 shrink-0 place-items-center rounded-2xl bg-white text-rail md:mb-1">
          <Activity size={18} strokeWidth={2.4} />
        </div>
        <button onClick={() => navigate("/buyer")} title="New request" className={`${railIdle} md:mb-3`}>
          <Plus size={20} />
        </button>

        {NAV.map(({ to, icon: Icon, label }) => (
          <NavLink
            key={to}
            to={to}
            end={to === "/"}
            title={label}
            aria-label={label}
            className={({ isActive }) => (isActive ? `${railItem} bg-white/20 text-white` : railIdle)}
          >
            <Icon size={18} />
            {to === "/approvals" && waiting ? (
              <span className="absolute right-1.5 top-1.5 size-2 rounded-full bg-warn ring-2 ring-rail" />
            ) : null}
          </NavLink>
        ))}

        <div className="ml-auto flex shrink-0 items-center gap-1 md:ml-0 md:mt-auto md:flex-col">
          <a
            href="/docs"
            target="_blank"
            rel="noreferrer"
            title="API reference"
            className={railIdle}
          >
            <CircleHelp size={18} />
          </a>
          {signedIn ? (
            <button onClick={() => setToken("")} title="Sign out" className={railIdle}>
              <LogOut size={18} />
            </button>
          ) : null}
          <div className="grid size-10 shrink-0 place-items-center rounded-full border border-white/25 md:mt-1">
            <Avatar name="operator" size={28} />
          </div>
        </div>
      </nav>

      <main className="min-w-0 flex-1 pb-10">
        <div className="mb-5 flex items-center justify-end gap-2">
          <span className="chip h-9 border border-line bg-surface px-3.5 text-ink-muted">
            <span className={`size-1.5 rounded-full ${health.data ? "bg-positive" : "bg-negative"}`} />
            {health.data ? `${health.data.catalog_rows.toLocaleString("en-GB")} products indexed` : "gateway offline"}
          </span>
          <button
            className="relative grid size-9 place-items-center rounded-full border border-line bg-surface text-ink-muted transition-colors hover:border-brand/30 hover:bg-brand-soft hover:text-brand"
            onClick={() => navigate("/approvals")}
            title={`${waiting} orders waiting`}
          >
            <Bell size={15} />
            {waiting ? (
              <span className="absolute -right-1 -top-1 grid h-4 min-w-4 place-items-center rounded-full bg-warn-ink px-1 text-[10px] font-semibold text-white ring-2 ring-page">
                {waiting}
              </span>
            ) : null}
          </button>
          <Avatar name="operator" size={36} />
        </div>

        <header className="mb-6 flex flex-wrap items-end justify-between gap-x-4 gap-y-3">
          <div className="min-w-0">
            <h1 className="text-[28px] font-semibold leading-tight tracking-tight">{head.title}</h1>
            <p className="label mt-1">{head.sub}</p>
          </div>
          {/* Only the dashboard reads the range; elsewhere it would be a control that does nothing. */}
          <div className={`relative ${pathname === "/" ? "" : "hidden"}`}>
            <button
              className={`flex h-9 items-center gap-2 rounded-full border bg-surface px-4 text-[13px] font-medium transition-colors hover:border-brand/30 hover:text-brand ${
                rangeOpen ? "border-brand/30 text-brand" : "border-line"
              }`}
              onClick={() => setRangeOpen((v) => !v)}
              aria-expanded={rangeOpen}
            >
              {range.label}
              <ChevronDown size={14} className={`transition-transform ${rangeOpen ? "rotate-180" : ""}`} />
            </button>
            {rangeOpen ? (
              <div className="absolute right-0 z-20 mt-2 w-44 overflow-hidden rounded-[var(--radius-tile)] border border-line bg-surface p-1 shadow-[0_10px_40px_rgba(0,47,109,0.10)]">
                {RANGES.map((r, i) => (
                  <button
                    key={r.label}
                    className={`block w-full rounded-[10px] px-3 py-2 text-left text-[13px] transition-colors ${
                      i === rangeIndex ? "bg-brand-soft font-semibold text-brand" : "text-ink-muted hover:bg-brand-soft/60 hover:text-ink"
                    }`}
                    onClick={() => {
                      setRangeIndex(i);
                      setRangeOpen(false);
                    }}
                  >
                    {r.label}
                  </button>
                ))}
              </div>
            ) : null}
          </div>
        </header>

        {signedIn && !signingIn ? (
          <Outlet context={{ since, rangeLabel: range.label } satisfies ShellContext} />
        ) : (
          <Card className="mx-auto mt-10 w-full max-w-md">
            <form className="flex flex-col gap-4" onSubmit={signIn}>
              <div className="flex items-center gap-3">
                <span className="grid size-10 shrink-0 place-items-center rounded-2xl bg-rail text-white">
                  <Activity size={18} strokeWidth={2.4} />
                </span>
                <div className="min-w-0">
                  <h2 className="text-[15px] font-semibold">Operator sign-in</h2>
                  <p className="label">The token lives in memory only — a reload asks again.</p>
                </div>
              </div>
              <label className="flex flex-col gap-1.5">
                <span className="label">Operator token</span>
                <input
                  className="field"
                  type="password"
                  autoComplete="off"
                  required
                  value={tokenInput}
                  onChange={(event) => setTokenInput(event.target.value)}
                />
              </label>
              <button className="btn-dark" disabled={signingIn}>
                {signingIn ? "Signing in…" : "Sign in"}
              </button>
              {signInError ? <ErrorNote error={signInError} /> : null}
            </form>
          </Card>
        )}
      </main>
    </div>
  );
}
