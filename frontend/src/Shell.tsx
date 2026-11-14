import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate, useOutletContext } from "react-router-dom";
import {
  Bell,
  Bot,
  ChevronDown,
  CircleHelp,
  Fingerprint,
  Inbox,
  LayoutGrid,
  LogOut,
  Menu,
  Package,
  Plug,
  Plus,
  ScrollText,
  Search,
  ShieldCheck,
  X,
} from "lucide-react";
import { api, hasToken, onAuthChange, setToken } from "./api";
import { Avatar } from "./charts";
import { LuotsiMark, LuotsiWordmark } from "./brand";
import { Card, ErrorNote, useAsync } from "./ui";

const NAV = [
  { to: "/", icon: LayoutGrid, label: "Dashboard" },
  { to: "/buyer", icon: Search, label: "Buyer flow" },
  { to: "/approvals", icon: Inbox, label: "Approvals" },
  { to: "/orders", icon: Package, label: "Orders" },
  { to: "/audit", icon: ScrollText, label: "Audit trail" },
  { to: "/policy", icon: ShieldCheck, label: "Policy" },
  { to: "/agents", icon: Bot, label: "Agents" },
  { to: "/connect", icon: Plug, label: "Connect an agent" },
  { to: "/trust", icon: Fingerprint, label: "Trust" },
];

const TITLES: Record<string, { title: string; sub: string }> = {
  "/": { title: "Good day, operator", sub: "Here is what the purchasing agent did on your behalf." },
  "/buyer": { title: "Buyer flow", sub: "Describe the need, compare offers, lock a price, order." },
  "/approvals": { title: "Approval inbox", sub: "Orders the agent would not confirm on its own." },
  "/orders": { title: "Orders", sub: "Every commitment this gateway has made." },
  "/audit": { title: "Audit trail", sub: "Append-only record of agent and human decisions." },
  "/policy": { title: "Policy", sub: "The guardrails the buyer owns, not the code." },
  "/agents": { title: "Agents", sub: "Who may buy, for which customer, and up to what limit." },
  "/connect": { title: "Connect an agent", sub: "Pick the assistant or tool you work in and give it a key to this gateway." },
  "/trust": { title: "Trust", sub: "Verify what the gateway recorded, without taking its word for it." },
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
  const [menuOpen, setMenuOpen] = useState(false);

  useEffect(() => onAuthChange(() => setSignedIn(hasToken())), []);
  // A phone menu that stays open after navigating would cover the page the person just asked for.
  useEffect(() => setMenuOpen(false), [pathname]);

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

  return (
    <div className="flex min-h-screen flex-col md:flex-row md:gap-4 md:p-4">
      {/* Desktop rail: 72px of icons that widens to 236px with names on hover or keyboard focus. The column itself
          widens, so the page beside it moves over and narrows instead of being covered. The short delay on hover
          keeps a pointer that only passes over the rail from shifting the page. */}
      <div className="group/rail hidden w-[72px] shrink-0 transition-[width] duration-200 ease-out hover:w-[236px] hover:delay-150 focus-within:w-[236px] motion-reduce:transition-none md:block">
        <nav
          aria-label="Main"
          className="sticky top-4 flex h-[calc(100vh-2rem)] w-full flex-col overflow-hidden rounded-[26px] bg-rail px-4 py-4"
        >
          <div className="mb-1 flex h-10 items-center gap-3">
            <span className="grid size-10 shrink-0 place-items-center rounded-2xl bg-white text-rail">
              <LuotsiMark className="block h-6 w-auto" />
            </span>
            <RailLabel>
              <LuotsiWordmark className="block h-[18px] w-auto text-white" />
            </RailLabel>
          </div>
          <button onClick={() => navigate("/buyer")} className={`${RAIL_ROW} ${RAIL_IDLE} mb-3`}>
            <Plus size={20} className="shrink-0" />
            <RailLabel>New request</RailLabel>
          </button>

          <div className="flex flex-col gap-1">
            {NAV.map(({ to, icon: Icon, label }) => (
              <NavLink
                key={to}
                to={to}
                end={to === "/"}
                className={({ isActive }) => `${RAIL_ROW} ${isActive ? "bg-white/[0.18] text-white" : RAIL_IDLE}`}
              >
                <span className="relative shrink-0">
                  <Icon size={18} />
                  {to === "/approvals" && waiting ? (
                    <span className="absolute -right-1.5 -top-1.5 size-2 rounded-full bg-warn ring-2 ring-rail" />
                  ) : null}
                </span>
                <RailLabel>{label}</RailLabel>
                {to === "/approvals" && waiting ? (
                  <RailLabel className="ml-auto rounded-full bg-warn px-1.5 text-[11px] font-semibold text-white">
                    {waiting}
                  </RailLabel>
                ) : null}
              </NavLink>
            ))}
          </div>

          <div className="mt-auto flex flex-col gap-1">
            <a href="/docs" target="_blank" rel="noreferrer" className={`${RAIL_ROW} ${RAIL_IDLE}`}>
              <CircleHelp size={18} className="shrink-0" />
              <RailLabel>API reference</RailLabel>
            </a>
            {signedIn ? (
              <button onClick={() => setToken("")} className={`${RAIL_ROW} ${RAIL_IDLE}`}>
                <LogOut size={18} className="shrink-0" />
                <RailLabel>Sign out</RailLabel>
              </button>
            ) : null}
            <div className="mt-1 flex h-10 items-center gap-3">
              <span className="grid size-10 shrink-0 place-items-center rounded-full border border-white/25">
                <Avatar name="operator" size={28} />
              </span>
              <RailLabel>
                <span className="text-[13px] text-white">Operator</span>
              </RailLabel>
            </div>
          </div>
        </nav>
      </div>

      {/* Phone: one bar with the page name and a menu that lists every destination by name. */}
      <div className="sticky top-0 z-30 md:hidden">
        <div className="flex items-center gap-3 bg-rail px-4 py-3 text-white">
          <span className="grid size-9 shrink-0 place-items-center rounded-xl bg-white text-rail">
            <LuotsiMark className="block h-5 w-auto" label="Luotsi" />
          </span>
          <span className="min-w-0 flex-1 truncate text-[15px] font-semibold">{head.title}</span>
          <button
            onClick={() => navigate("/approvals")}
            className="relative grid size-10 place-items-center rounded-xl text-white/80 hover:bg-white/10"
            aria-label={`${waiting} orders waiting`}
          >
            <Bell size={18} />
            {waiting ? (
              <span className="absolute right-1 top-1 grid h-4 min-w-4 place-items-center rounded-full bg-warn px-1 text-[10px] font-semibold text-white">
                {waiting}
              </span>
            ) : null}
          </button>
          <button
            onClick={() => setMenuOpen((v) => !v)}
            className="grid size-10 place-items-center rounded-xl text-white hover:bg-white/10"
            aria-expanded={menuOpen}
            aria-controls="phone-menu"
            aria-label={menuOpen ? "Close menu" : "Open menu"}
          >
            {menuOpen ? <X size={20} /> : <Menu size={20} />}
          </button>
        </div>
        {menuOpen ? (
          <nav id="phone-menu" aria-label="Main" className="absolute inset-x-0 top-full max-h-[calc(100vh-64px)] overflow-y-auto bg-rail px-3 pb-4 shadow-[0_18px_40px_rgba(0,67,87,0.35)]">
            <button onClick={() => navigate("/buyer")} className={`${PHONE_ROW} text-white/80 hover:bg-white/10`}>
              <Plus size={18} /> New request
            </button>
            {NAV.map(({ to, icon: Icon, label }) => (
              <NavLink
                key={to}
                to={to}
                end={to === "/"}
                className={({ isActive }) => `${PHONE_ROW} ${isActive ? "bg-white/[0.18] text-white" : "text-white/80 hover:bg-white/10"}`}
              >
                <Icon size={18} />
                {label}
                {to === "/approvals" && waiting ? (
                  <span className="ml-auto rounded-full bg-warn px-2 text-[12px] font-semibold text-white">{waiting}</span>
                ) : null}
              </NavLink>
            ))}
            <div className="my-2 h-px bg-white/15" />
            <a href="/docs" target="_blank" rel="noreferrer" className={`${PHONE_ROW} text-white/80 hover:bg-white/10`}>
              <CircleHelp size={18} /> API reference
            </a>
            {signedIn ? (
              <button onClick={() => setToken("")} className={`${PHONE_ROW} text-white/80 hover:bg-white/10`}>
                <LogOut size={18} /> Sign out
              </button>
            ) : null}
          </nav>
        ) : null}
      </div>

      <main className="min-w-0 flex-1 px-4 pb-10 pt-4 md:px-0 md:pt-0">
        <div className="mb-5 hidden items-center justify-end gap-2 md:flex">
          <span className="chip h-9 border border-line bg-surface px-3.5 text-ink-muted">
            <span className={`size-1.5 rounded-full ${health.data ? "bg-positive" : "bg-negative"}`} />
            {health.data ? `${health.data.catalog_rows.toLocaleString("en-GB")} products indexed` : "gateway offline"}
          </span>
          <button
            className="relative grid size-9 place-items-center rounded-full border border-line bg-surface text-ink-muted transition-colors hover:border-brand/30 hover:bg-brand-soft hover:text-brand"
            onClick={() => navigate("/approvals")}
            aria-label={`${waiting} orders waiting`}
          >
            <Bell size={15} />
            {waiting ? (
              <span className="absolute -right-1 -top-1 grid h-4 min-w-4 place-items-center rounded-full bg-warn-ink px-1 text-[10px] font-semibold text-white ring-2 ring-page">
                {waiting}
              </span>
            ) : null}
          </button>
          {pathname !== "/connect" ? (
            <button className="btn-dark h-9 !py-0" onClick={() => navigate("/connect")}>
              <Plug size={15} /> Connect an agent
            </button>
          ) : null}
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
              <div className="absolute right-0 z-20 mt-2 w-44 overflow-hidden rounded-[var(--radius-tile)] border border-line bg-surface p-1 shadow-[0_10px_40px_rgba(0,67,87,0.10)]">
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
              <div className="flex flex-col gap-4">
                <LuotsiWordmark className="block h-8 w-auto self-start text-brand" />
                <div className="min-w-0">
                  <h2 className="text-[15px] font-semibold">Operator sign-in</h2>
                  <p className="label">The token lives in memory only, so a reload asks again.</p>
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

// 40px rows: the icon keeps its place on the collapsed rail, the name slides in beside it.
const RAIL_ROW = "flex h-10 w-full items-center gap-3 rounded-2xl px-[11px] text-left text-[14px] font-medium transition-colors";
const RAIL_IDLE = "text-white/75 hover:bg-white/10 hover:text-white focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-white";
const PHONE_ROW = "flex h-12 w-full items-center gap-3 rounded-xl px-3 text-left text-[15px] font-medium";

/** Hidden while the rail is collapsed but still in the accessibility tree, so the icon-only rail has names. */
function RailLabel({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return (
    <span
      className={`whitespace-nowrap opacity-0 transition-opacity duration-150 group-hover/rail:opacity-100 group-hover/rail:delay-150 group-focus-within/rail:opacity-100 motion-reduce:transition-none ${className}`}
    >
      {children}
    </span>
  );
}
