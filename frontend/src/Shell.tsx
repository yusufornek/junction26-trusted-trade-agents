import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate, useOutletContext } from "react-router-dom";
import {
  Bell,
  Radar,
  Gauge,
  GitPullRequestArrow,
  Users,
  ChefHat,
  Repeat,
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
import { api, auth, hasSession, hasToken, onAuthChange, setToken, type Account } from "./api";
import { Avatar } from "./charts";
import { LuotsiMark, LuotsiWordmark } from "./brand";
import { useAsync } from "./ui";
import SignIn from "./SignIn";

/** Set at build time from the server's DEMO_OPERATOR_TOKEN (deploy/docker-compose.yml); never committed. */
const DEMO_TOKEN: string = import.meta.env.VITE_DEMO_OPERATOR_TOKEN ?? "";

/** What each role can open: organisation people get 403 on the operator's stats, policy and connection endpoints, so
 *  those pages are not offered to them. Valio staff and the operator token see everything. */
const ROLE_PAGES: Record<string, string[]> = {
  tenant_admin: ["/approvals", "/orders", "/repeat", "/watch", "/governance", "/changes", "/team", "/connect", "/audit", "/trust"],
  approver: ["/approvals", "/orders", "/repeat", "/watch", "/governance", "/audit", "/trust"],
  viewer: ["/orders", "/repeat", "/watch", "/audit", "/trust"],
  member: ["/orders", "/repeat", "/audit", "/trust"],
};

const NAV = [
  { to: "/", icon: LayoutGrid, label: "Dashboard" },
  { to: "/buyer", icon: Search, label: "Buyer flow" },
  { to: "/basket", icon: ChefHat, label: "Recipe basket" },
  { to: "/repeat", icon: Repeat, label: "Repeat buying" },
  { to: "/watch", icon: Radar, label: "Supply watch" },
  { to: "/approvals", icon: Inbox, label: "Approvals" },
  { to: "/orders", icon: Package, label: "Orders" },
  { to: "/audit", icon: ScrollText, label: "Audit trail" },
  { to: "/policy", icon: ShieldCheck, label: "Policy" },
  { to: "/agents", icon: Bot, label: "Agents" },
  { to: "/governance", icon: Gauge, label: "Governance" },
  { to: "/changes", icon: GitPullRequestArrow, label: "Policy changes" },
  { to: "/team", icon: Users, label: "Team" },
  { to: "/connect", icon: Plug, label: "Connect an agent" },
  { to: "/trust", icon: Fingerprint, label: "Trust" },
];

const TITLES: Record<string, { title: string; sub: string }> = {
  "/": { title: "Good day, operator", sub: "Here is what the purchasing agent did on your behalf." },
  "/buyer": { title: "Buyer flow", sub: "Describe the need, compare offers, lock a price, order." },
  "/basket": { title: "Recipe basket", sub: "Turn a recipe and a number of portions into one priced basket." },
  "/repeat": { title: "Repeat buying", sub: "What this kitchen buys again and again. A suggestion never orders by itself." },
  "/signup": { title: "Create an organisation account", sub: "Tell us what kind of kitchen you run. The gateway sets safe starting limits from it." },
  "/consent": { title: "Connect an assistant", sub: "An assistant asks to buy through this gateway. You decide as which agent, and for which customers." },
  "/watch": { title: "Supply watch", sub: "What will run out before it is due, what to use instead, and new products worth a look." },
  "/approvals": { title: "Approval inbox", sub: "Orders the agent would not confirm on its own." },
  "/orders": { title: "Orders", sub: "Every commitment this gateway has made." },
  "/audit": { title: "Audit trail", sub: "Append-only record of agent and human decisions." },
  "/policy": { title: "Policy", sub: "The guardrails the buyer owns, not the code." },
  "/governance": { title: "Governance", sub: "Every agent, who answers for it, what it has done, and a switch to stop it." },
  "/changes": { title: "Policy changes", sub: "Where each limit comes from, and the changes that wait for a second person." },
  "/team": { title: "Team", sub: "The people who can sign in, what each role may do, and the organisations on this gateway." },
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
  const [account, setAccount] = useState<Account | null>(null);
  const [signingIn, setSigningIn] = useState(false);
  const [signInError, setSignInError] = useState<unknown>(null);
  const [rangeIndex, setRangeIndex] = useState(1);
  // The range counts back from page load. Counted from Date.now() on every render, `since` changed each time the
  // shell re-rendered (the 10 s approvals poll, opening a menu), so the dashboard refetched behind a spinner, the page
  // shrank to the spinner's height and the browser scrolled back to the top.
  const [loadedAt] = useState(Date.now);
  const [rangeOpen, setRangeOpen] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);

  useEffect(
    () =>
      onAuthChange(() => {
        setSignedIn(hasToken());
        if (!hasSession()) setAccount(null);
      }),
    [],
  );
  // A reload keeps an e-mail session (cookie + stored CSRF token): read who it is before showing any page.
  useEffect(() => {
    if (hasSession()) auth.session().then(setAccount, () => auth.signOut());
  }, []);
  const pages = account ? ROLE_PAGES[account.role] : undefined;
  const allowed = (to: string) => !pages || pages.some((p) => to === p || to.startsWith(p + "/") || (p === "/orders" && to.startsWith("/products/")));
  const nav = NAV.filter((n) => allowed(n.to));
  // Organisation people land on their first page instead of an operator page that would only answer 403.
  useEffect(() => {
    if (signedIn && pages && !allowed(pathname) && pathname !== "/signup") navigate(pages[0], { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signedIn, account, pathname]);
  const signOut = () => {
    auth.signOut();
    setAccount(null);
  };
  const who = account?.user.email ?? "operator";
  // A phone menu that stays open after navigating would cover the page the person just asked for.
  useEffect(() => setMenuOpen(false), [pathname]);

  const signInWith = async (value: string) => {
    setSigningIn(true);
    setSignInError(null);
    // Keep the outlet unmounted until the token is verified as an operator token.
    setToken(value);
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
  const signInWithEmail = async (email: string, password: string) => {
    setSigningIn(true);
    setSignInError(null);
    try {
      setAccount(await auth.login(email, password));
    } catch (error) {
      setSignInError(error);
    } finally {
      setSigningIn(false);
    }
  };

  const health = useAsync(() => api.health(), []);
  const approvals = useAsync(() => (signedIn && allowed("/approvals") ? api.approvals() : Promise.resolve([])), [signedIn, account], 10000);
  const head =
    (pathname === "/" && account ? { ...TITLES["/"], title: `Good day, ${account.user.email.split("@")[0]}` } : undefined) ??
    TITLES[pathname] ??
    (pathname.startsWith("/approvals/")
      ? TITLES["/approvals"]
      : pathname.startsWith("/products/")
      ? { title: "Product", sub: "Catalog facts the agent judged this product on." }
      : { title: "Order", sub: "Commitment detail" });
  const range = RANGES[rangeIndex];
  const since = range.days ? new Date(loadedAt - range.days * 86400000).toISOString() : undefined;
  const waiting = approvals.data?.length ?? 0;
  // Signed out, the page is only the sign-in card (or the public sign-up form): no rail, no bars to click into.
  const bare = !signedIn || signingIn;

  if (bare && pathname !== "/signup")
    return (
      <SignIn
        demoToken={DEMO_TOKEN || undefined}
        signingIn={signingIn}
        error={signInError}
        onToken={signInWith}
        onEmail={signInWithEmail}
        onClearError={() => setSignInError(null)}
      />
    );

  return (
    <div className={`flex min-h-screen flex-col ${bare ? "" : "md:flex-row md:gap-4 md:p-4"}`}>
      {bare ? null : (
        <>
      {/* Desktop rail: 72px of icons that widens to 236px with names on hover or keyboard focus. The column itself
          widens, so the page beside it moves over and narrows instead of being covered. The short delay on hover
          keeps a pointer that only passes over the rail from shifting the page. */}
      <div className="group/rail hidden w-[72px] shrink-0 transition-[width] duration-200 ease-out hover:w-[236px] hover:delay-150 focus-within:w-[236px] motion-reduce:transition-none md:block">
        <nav
          aria-label="Main"
          className="sticky top-4 flex h-[calc(100vh-2rem)] w-full flex-col overflow-hidden rounded-[26px] bg-rail px-4 py-4 [@media(max-height:900px)]:py-3"
        >
          <div className="mb-1 flex h-10 shrink-0 items-center gap-3">
            <span className="grid size-10 shrink-0 place-items-center rounded-2xl bg-white text-rail">
              <LuotsiMark className="block h-6 w-auto" />
            </span>
            <RailLabel>
              <LuotsiWordmark className="block h-[18px] w-auto text-white" />
            </RailLabel>
          </div>
          <button onClick={() => navigate("/buyer")} className={`${RAIL_ROW} ${RAIL_IDLE} mb-3 [@media(max-height:900px)]:mb-1 ${allowed("/buyer") ? "" : "hidden"}`}>
            <Plus size={20} className="shrink-0" />
            <RailLabel>New request</RailLabel>
          </button>

          {/* Fourteen destinations no longer fit a laptop's height: the list scrolls between the fixed top and bottom
              rows, and rows tighten below 900 px so most screens need no scrolling at all. */}
          <div className="-mx-1 flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto overflow-x-hidden px-1 [scrollbar-color:rgba(255,255,255,0.3)_transparent] [scrollbar-width:thin] [@media(max-height:900px)]:gap-0.5">
            {nav.map(({ to, icon: Icon, label }) => (
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

          <div className="flex shrink-0 flex-col gap-1 pt-2 [@media(max-height:900px)]:gap-0.5 [@media(max-height:900px)]:pt-1">
            <a href="/docs" target="_blank" rel="noreferrer" className={`${RAIL_ROW} ${RAIL_IDLE}`}>
              <CircleHelp size={18} className="shrink-0" />
              <RailLabel>API reference</RailLabel>
            </a>
            {signedIn ? (
              <button onClick={signOut} className={`${RAIL_ROW} ${RAIL_IDLE}`}>
                <LogOut size={18} className="shrink-0" />
                <RailLabel>Sign out</RailLabel>
              </button>
            ) : null}
            <div className="mt-1 flex h-10 items-center gap-3">
              <span className="grid size-10 shrink-0 place-items-center rounded-full border border-white/25">
                <Avatar name={who} size={28} />
              </span>
              <RailLabel>
                <span className="text-[13px] text-white">Operator</span>
              </RailLabel>
            </div>
          </div>
        </nav>
      </div>

      {/* Phone: one bar with the brand and a menu that lists every destination by name. The page name stays in the
          page's own heading right below; repeating it here said it twice. */}
      <div className="sticky top-0 z-30 md:hidden">
        <div className="flex items-center gap-3 bg-rail px-4 py-3 text-white">
          <span className="min-w-0 flex-1">
            <LuotsiWordmark className="block h-5 w-auto text-white" />
          </span>
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
          // dvh, not vh: on iPhones vh includes the space under Safari's toolbar, which hid the last rows.
          <nav id="phone-menu" aria-label="Main" className="absolute inset-x-0 top-full max-h-[calc(100dvh-64px)] overflow-y-auto bg-rail px-3 pb-4 shadow-[0_18px_40px_rgba(0,67,87,0.35)]">
            <button onClick={() => navigate("/buyer")} className={`${PHONE_ROW} text-white/80 hover:bg-white/10 ${allowed("/buyer") ? "" : "hidden"}`}>
              <Plus size={18} /> New request
            </button>
            {nav.map(({ to, icon: Icon, label }) => (
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
              <button onClick={signOut} className={`${PHONE_ROW} text-white/80 hover:bg-white/10`}>
                <LogOut size={18} /> Sign out
              </button>
            ) : null}
          </nav>
        ) : null}
      </div>
        </>
      )}

      <main
        className={
          bare ? "flex min-h-screen w-full flex-col items-center justify-center px-4 py-10" : "min-w-0 flex-1 px-4 pb-10 pt-4 md:px-0 md:pt-0"
        }
      >
        <div className={`mb-5 hidden items-center justify-end gap-2 ${bare ? "" : "md:flex"}`}>
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
          {pathname !== "/connect" && allowed("/connect") ? (
            <button className="btn-dark h-9 !py-0" onClick={() => navigate("/connect")}>
              <Plug size={15} /> Connect an agent
            </button>
          ) : null}
          <Avatar name={who} size={36} />
        </div>

        <header
          className={`mb-6 flex-wrap items-end justify-between gap-x-4 gap-y-3 ${
            !bare ? "flex" : pathname === "/signup" ? "flex w-full max-w-2xl" : "hidden"
          }`}
        >
          <div className="min-w-0">
            <h1 className="text-[28px] font-semibold leading-tight tracking-tight">{head.title}</h1>
            <p className="label mt-1">{head.sub}</p>
          </div>
          {/* Only the signed-in dashboard reads the range; elsewhere it would be a control that does nothing. */}
          <div
            className={`relative ${pathname === "/" && signedIn ? "" : "hidden"}`}
            onKeyDown={(e) => e.key === "Escape" && setRangeOpen(false)}
          >
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
              <>
                {/* A tap anywhere else closes the menu: touch browsers do not focus buttons, so blur never fires. */}
                <div className="fixed inset-0 z-10" onClick={() => setRangeOpen(false)} />
                {/* As wide as its button: a wider menu reached past the screen edge once the header wrapped on phones. */}
                <div className="absolute right-0 z-20 mt-2 w-max min-w-full overflow-hidden rounded-[var(--radius-tile)] border border-line bg-surface p-1 shadow-[0_10px_40px_rgba(0,67,87,0.10)]">
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
              </>
            ) : null}
          </div>
        </header>

        {/* Sign-up is public: a new kitchen has no operator token. */}
        {!bare || pathname === "/signup" ? (
          <>
            <Outlet context={{ since, rangeLabel: range.label } satisfies ShellContext} />
            {bare ? (
              <p className="mt-5 text-center text-[13px] text-ink-muted">
                Already have an account?{" "}
                <NavLink to="/" className="font-medium text-brand hover:underline">
                  Sign in
                </NavLink>
              </p>
            ) : null}
          </>
        ) : null}
      </main>
    </div>
  );
}

// 40px rows: the icon keeps its place on the collapsed rail, the name slides in beside it.
const RAIL_ROW = "flex h-10 w-full shrink-0 items-center gap-3 rounded-2xl px-[11px] text-left text-[14px] font-medium transition-colors [@media(max-height:900px)]:h-9";
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
