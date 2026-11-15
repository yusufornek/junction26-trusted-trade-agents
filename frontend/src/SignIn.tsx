import { useState, type FormEvent, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { ChevronLeft, KeyRound, Mail, Sparkles } from "lucide-react";
import { LuotsiWordmark } from "./brand";
import { ErrorNote } from "./ui";

type Props = {
  /** Present only in demo builds: one tap signs a judge in as the operator. */
  demoToken?: string;
  signingIn: boolean;
  error: unknown;
  onToken: (token: string) => void;
  onEmail: (email: string, password: string) => void;
  onClearError: () => void;
};

const BIG = "flex h-14 w-full items-center justify-center gap-2.5 rounded-2xl text-[17px] font-medium transition-colors disabled:opacity-60";
const PRIMARY = `${BIG} bg-white text-rail hover:bg-white/90`;
const SECONDARY = `${BIG} bg-white/10 text-white hover:bg-white/[0.16]`;
const OUTLINE = `${BIG} border border-white/30 text-white hover:bg-white/[0.06]`;
const FIELD = "h-13 w-full rounded-2xl border border-white/20 bg-white px-4 text-[16px] text-ink outline-none focus:border-white focus:ring-2 focus:ring-white/40";

/** Signed-out screen: the brand alone on the page, every way in stacked on one sheet below it. */
export default function SignIn({ demoToken, signingIn, error, onToken, onEmail, onClearError }: Props) {
  const [method, setMethod] = useState<"choose" | "email" | "token">("choose");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [token, setTokenValue] = useState("");

  const go = (next: typeof method) => {
    onClearError();
    setMethod(next);
  };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (method === "token") {
      onToken(token);
      setTokenValue("");
    } else {
      onEmail(email.trim(), password);
      setPassword("");
    }
  };

  return (
    <div className="flex min-h-dvh flex-col bg-page">
      <div className="flex flex-1 flex-col items-center justify-center gap-3 px-6 py-12">
        <LuotsiWordmark className="block h-12 w-auto text-rail sm:h-16" />
        <p className="text-center text-[15px] text-ink-muted">Purchasing agents, under your rules.</p>
      </div>

      <div className="rounded-t-[32px] bg-rail px-5 pb-[max(1.5rem,env(safe-area-inset-bottom))] pt-6 text-white shadow-[0_-12px_40px_rgba(0,67,87,0.18)] sm:mx-auto sm:mb-10 sm:w-full sm:max-w-[30rem] sm:rounded-[32px] sm:px-7 sm:pb-6">
        {method === "choose" ? (
          <div className="flex flex-col gap-3">
            {demoToken ? (
              <button className={PRIMARY} disabled={signingIn} onClick={() => onToken(demoToken)}>
                <Sparkles size={19} aria-hidden />
                {signingIn ? (
                  "Signing in…"
                ) : (
                  <span>
                    {/* Phones fit the account name on one line only without the verb. */}
                    <span className="hidden sm:inline">Continue with </span>Junction 2026 Special Account
                  </span>
                )}
              </button>
            ) : null}
            <button className={demoToken ? SECONDARY : PRIMARY} onClick={() => go("email")}>
              <Mail size={19} aria-hidden /> Continue with e-mail
            </button>
            <Link to="/signup" className={SECONDARY}>
              Sign up
            </Link>
            <button className={OUTLINE} onClick={() => go("token")}>
              <KeyRound size={18} aria-hidden /> Use an operator token
            </button>
          </div>
        ) : (
          <form className="flex flex-col gap-3" onSubmit={submit}>
            <button type="button" onClick={() => go("choose")} className="-ml-1 flex items-center gap-1 self-start text-[15px] text-white/80 hover:text-white">
              <ChevronLeft size={18} aria-hidden /> All sign-in options
            </button>
            {method === "email" ? (
              <>
                <Labeled label="E-mail">
                  <input className={FIELD} type="email" autoComplete="username" required autoFocus value={email} onChange={(e) => setEmail(e.target.value)} />
                </Labeled>
                <Labeled label="Password">
                  <input className={FIELD} type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
                </Labeled>
              </>
            ) : (
              <Labeled label="Operator token" hint="Kept in memory only, so a reload asks again.">
                <input className={FIELD} type="password" autoComplete="off" required autoFocus value={token} onChange={(e) => setTokenValue(e.target.value)} />
              </Labeled>
            )}
            <button className={`${PRIMARY} mt-1`} disabled={signingIn}>
              {signingIn ? "Signing in…" : "Log in"}
            </button>
          </form>
        )}

        {error ? (
          <div className="mt-3">
            <ErrorNote error={error} />
          </div>
        ) : null}
        <p className="mt-4 text-center text-[13px] text-white/60">For the best experience, use a computer.</p>
      </div>
    </div>
  );
}

function Labeled({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="text-[14px] text-white/80">{label}</span>
      {children}
      {hint ? <span className="text-[12px] text-white/60">{hint}</span> : null}
    </label>
  );
}
