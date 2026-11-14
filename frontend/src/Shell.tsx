import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate, useOutletContext } from "react-router-dom";
import {
  Activity,
  Bell,
  ChevronDown,
  CircleHelp,
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
];

const TITLES: Record<string, { title: string; sub: string }> = {
  "/": { title: "Good day, operator", sub: "Here is what the purchasing agent did on your behalf." },
  "/buyer": { title: "Buyer flow", sub: "Describe the need, compare offers, lock a price, order." },
  "/approvals": { title: "Approval inbox", sub: "Orders the agent would not confirm on its own." },
  "/orders": { title: "Orders", sub: "Every commitment this gateway has made." },
  "/audit": { title: "Audit trail", sub: "Append-only record of agent and human decisions." },
  "/policy": { title: "Policy", sub: "The guardrails the buyer owns, not the code." },
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
  const head = TITLES[pathname] ?? { title: "Order", sub: "Commitment detail" };
  const range = RANGES[rangeIndex];
  const since = range.days ? new Date(Date.now() - range.days * 86400000).toISOString() : undefined;
  const waiting = approvals.data?.length ?? 0;

  return (
    <div className="flex min-h-screen gap-4 p-4">
      <nav className="sticky top-4 flex h-[calc(100vh-2rem)] w-[72px] shrink-0 flex-col items-center gap-1 rounded-[26px] bg-rail py-4">
        <div className="mb-1 grid size-10 place-items-center rounded-2xl bg-white text-rail">
          <Activity size={18} strokeWidth={2.4} />
        </div>
        <button
          onClick={() => navigate("/buyer")}
          title="New request"
          className="mb-3 grid size-10 place-items-center rounded-2xl text-white/60 transition-colors hover:bg-white/10 hover:text-white"
        >
          <Plus size={20} />
        </button>

        {NAV.map(({ to, icon: Icon, label }) => (
          <NavLink
            key={to}
            to={to}
            end={to === "/"}
            title={label}
            className={({ isActive }) =>
              `relative grid size-10 place-items-center rounded-2xl transition-colors ${
                isActive ? "bg-white/15 text-white" : "text-white/40 hover:bg-white/10 hover:text-white/90"
              }`
            }
          >
            <Icon size={18} />
            {to === "/approvals" && waiting ? (
              <span className="absolute right-1.5 top-1.5 size-1.5 rounded-full bg-warn" />
            ) : null}
          </NavLink>
        ))}

        <div className="mt-auto flex flex-col items-center gap-1">
          <a
            href="http://localhost:8000/docs"
            target="_blank"
            rel="noreferrer"
            title="API reference"
            className="grid size-10 place-items-center rounded-2xl text-white/40 transition-colors hover:bg-white/10 hover:text-white/90"
          >
            <CircleHelp size={18} />
          </a>
          {signedIn ? (
            <button
              onClick={() => setToken("")}
              title="Sign out"
              className="grid size-10 place-items-center rounded-2xl text-white/40 transition-colors hover:bg-white/10 hover:text-white/90"
            >
              <LogOut size={18} />
            </button>
          ) : null}
          <div className="mt-1 grid size-10 place-items-center rounded-full border border-white/15">
            <Avatar name="operator" size={28} />
          </div>
        </div>
      </nav>

      <main className="min-w-0 flex-1 pb-10">
        <div className="mb-5 flex items-center justify-end gap-2">
          <span className="chip border border-line bg-surface text-ink-muted">
            <span className={`size-1.5 rounded-full ${health.data ? "bg-positive" : "bg-negative"}`} />
            {health.data ? `${health.data.catalog_rows.toLocaleString("en-GB")} products indexed` : "gateway offline"}
          </span>
          <button
            className="relative grid size-9 place-items-center rounded-full border border-line bg-surface text-ink-muted transition-colors hover:text-ink"
            onClick={() => navigate("/approvals")}
            title={`${waiting} orders waiting`}
          >
            <Bell size={15} />
            {waiting ? (
              <span className="absolute -right-1 -top-1 grid min-w-4 place-items-center rounded-full bg-warn px-1 text-[10px] font-semibold text-white">
                {waiting}
              </span>
            ) : null}
          </button>
          <Avatar name="operator" size={36} />
        </div>

        <header className="mb-6 flex flex-wrap items-end justify-between gap-4">
          <div>
            <h1 className="text-[28px] font-semibold tracking-tight">{head.title}</h1>
            <p className="label mt-1">{head.sub}</p>
          </div>
          <div className="relative">
            <button
              className="flex items-center gap-2 rounded-full border border-line bg-surface px-4 py-2 text-[13px] font-medium"
              onClick={() => setRangeOpen((v) => !v)}
            >
              {range.label}
              <ChevronDown size={14} className="text-ink-soft" />
            </button>
            {rangeOpen ? (
              <div className="absolute right-0 z-20 mt-2 w-44 overflow-hidden rounded-[var(--radius-tile)] border border-line bg-surface shadow-[0_10px_40px_rgba(0,0,0,0.08)]">
                {RANGES.map((r, i) => (
                  <button
                    key={r.label}
                    className={`block w-full px-4 py-2.5 text-left text-[13px] transition-colors hover:bg-page ${
                      i === rangeIndex ? "font-semibold" : "text-ink-muted"
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
          <Card className="mx-auto mt-10 max-w-md">
            <form className="flex flex-col gap-3" onSubmit={signIn}>
              <div>
                <h2 className="text-[15px] font-semibold">Operator sign-in</h2>
                <p className="label">
                  The token lives in memory only — a reload asks again.
                </p>
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
