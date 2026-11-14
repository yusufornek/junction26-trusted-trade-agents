import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import {
  LayoutGrid,
  Search,
  Inbox,
  Package,
  ScrollText,
  ShieldCheck,
  Activity,
} from "lucide-react";
import { api, hasToken, onAuthChange, setToken } from "./api";
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
  "/": { title: "Trusted Trade Agents", sub: "What the purchasing agent did, and what needs you." },
  "/buyer": { title: "Buyer flow", sub: "Describe the need, compare offers, lock a price, order." },
  "/approvals": { title: "Approval inbox", sub: "Orders the agent would not confirm on its own." },
  "/orders": { title: "Orders", sub: "Every commitment this gateway has made." },
  "/audit": { title: "Audit trail", sub: "Append-only record of agent and human decisions." },
  "/policy": { title: "Policy", sub: "The guardrails the buyer owns, not the code." },
};

export default function Shell() {
  const { pathname } = useLocation();
  const [signedIn, setSignedIn] = useState(hasToken);
  const [tokenInput, setTokenInput] = useState("");
  const [signingIn, setSigningIn] = useState(false);
  const [signInError, setSignInError] = useState<unknown>(null);
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
  const head = TITLES[pathname] ?? { title: "Order", sub: "Commitment detail" };

  return (
    <div className="flex min-h-screen gap-4 p-4">
      <nav className="sticky top-4 flex h-[calc(100vh-2rem)] w-16 shrink-0 flex-col items-center gap-1 rounded-[var(--radius-card)] bg-rail py-5">
        <div className="mb-4 grid size-9 place-items-center rounded-xl bg-white/10 text-white">
          <Activity size={18} />
        </div>
        {NAV.map(({ to, icon: Icon, label }) => (
          <NavLink
            key={to}
            to={to}
            end={to === "/"}
            title={label}
            className={({ isActive }) =>
              `grid size-10 place-items-center rounded-xl transition-colors ${
                isActive ? "bg-white/15 text-white" : "text-white/40 hover:text-white/80"
              }`
            }
          >
            <Icon size={18} />
          </NavLink>
        ))}
        <div className="mt-auto text-center text-[9px] leading-tight text-white/30">
          {health.data ? (
            <>
              <div className="mx-auto mb-1 size-1.5 rounded-full bg-positive" />
              {health.data.catalog_rows.toLocaleString("en-GB")}
              <br />
              SKUs
            </>
          ) : (
            <div className="mx-auto size-1.5 rounded-full bg-negative" />
          )}
        </div>
      </nav>

      <main className="min-w-0 flex-1 pb-8">
        <header className="mb-6 flex items-end justify-between gap-4 pt-2">
          <div>
            <h1 className="text-[26px] font-semibold tracking-tight">{head.title}</h1>
            <p className="label mt-0.5">{head.sub}</p>
          </div>
          <div className="chip bg-surface border border-line text-ink-muted">
            customer <span className="font-semibold text-ink">1</span>
          </div>
        </header>
        {signedIn && !signingIn ? (
          <>
            <div className="mb-4 flex justify-end">
              <button className="btn-ghost" onClick={() => setToken("")}>Sign out</button>
            </div>
            <Outlet />
          </>
        ) : (
          <Card className="mx-auto max-w-md">
            <form className="flex flex-col gap-3" onSubmit={signIn}>
              <label className="flex flex-col gap-1.5">
                <span className="label">Operator token</span>
                <input className="field" type="password" autoComplete="off" required
                  value={tokenInput} onChange={(event) => setTokenInput(event.target.value)} />
              </label>
              <button className="btn-dark" disabled={signingIn}>{signingIn ? "Signing in…" : "Sign in"}</button>
              {signInError ? <ErrorNote error={signInError} /> : null}
            </form>
          </Card>
        )}
      </main>
    </div>
  );
}
