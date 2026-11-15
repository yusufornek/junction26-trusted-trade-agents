import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { ArrowLeft, Building2, KeyRound, Sparkles } from "lucide-react";
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

const BUTTON = "flex h-14 w-full items-center justify-center gap-2.5 rounded-xl text-[17px] font-medium transition-colors disabled:opacity-60";
const PRIMARY = `${BUTTON} bg-rail text-white hover:bg-brand-strong`;
const QUIET = `${BUTTON} bg-ink/[0.07] text-ink hover:bg-ink/[0.11]`;
const FIELD =
  "h-14 w-full rounded-xl border-2 border-transparent bg-ink/[0.07] px-4 text-[17px] text-ink outline-none placeholder:text-ink-soft focus:border-ink focus:bg-surface";

/** Signed-out screen: one question at a time, other ways in below it. */
export default function SignIn({ demoToken, signingIn, error, onToken, onEmail, onClearError }: Props) {
  const [step, setStep] = useState<"email" | "password" | "token">("email");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [token, setTokenValue] = useState("");

  const go = (next: typeof step) => {
    onClearError();
    setStep(next);
  };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (step === "email") return go("password");
    if (step === "token") {
      onToken(token);
      setTokenValue("");
      return;
    }
    onEmail(email.trim(), password);
    setPassword("");
  };

  const question = step === "email" ? "What's your e-mail?" : step === "password" ? "Enter your password" : "Enter your operator token";

  return (
    <div className="flex min-h-dvh flex-col bg-surface">
      <header className="bg-rail px-5 py-4 sm:px-12 sm:py-6">
        <LuotsiWordmark className="block h-6 w-auto text-white sm:h-7" />
      </header>

      <main className="flex w-full flex-1 justify-center px-5 pb-10 pt-10 sm:pt-24">
        <div className="flex w-full max-w-[480px] flex-col">
          {step !== "email" ? (
            <button type="button" onClick={() => go("email")} className="-ml-2 mb-4 grid size-10 place-items-center rounded-full hover:bg-ink/[0.07]" aria-label="Back">
              <ArrowLeft size={22} />
            </button>
          ) : null}
          <form className="flex flex-col gap-4" onSubmit={submit}>
            <label htmlFor="sign-in-field" className="text-[26px] font-medium leading-tight tracking-tight sm:text-[30px]">
              {question}
            </label>
            {step === "password" ? <p className="-mt-2 text-[15px] text-ink-muted">{email}</p> : null}
            {step === "email" ? (
              <input id="sign-in-field" className={FIELD} type="email" autoComplete="username" required placeholder="name@company.com" value={email} onChange={(e) => setEmail(e.target.value)} />
            ) : step === "password" ? (
              <input id="sign-in-field" className={FIELD} type="password" autoComplete="current-password" required autoFocus value={password} onChange={(e) => setPassword(e.target.value)} />
            ) : (
              <input id="sign-in-field" className={FIELD} type="password" autoComplete="off" required autoFocus value={token} onChange={(e) => setTokenValue(e.target.value)} />
            )}
            {step === "token" ? <p className="-mt-2 text-[13px] text-ink-muted">Kept in memory only, so a reload asks again.</p> : null}
            <button className={`${PRIMARY} mt-2`} disabled={signingIn}>
              {signingIn ? "Signing in…" : step === "email" ? "Continue" : "Log in"}
            </button>
          </form>

          {error ? (
            <div className="mt-4">
              <ErrorNote error={error} />
            </div>
          ) : null}

          {step === "email" ? (
            <>
              <Divider />
              <div className="flex flex-col gap-3">
                {demoToken ? (
                  <button className={QUIET} disabled={signingIn} onClick={() => onToken(demoToken)}>
                    <Sparkles size={20} className="shrink-0 text-rail" aria-hidden />
                    <span className="truncate">
                      {/* Phones fit the account name on one line only without the verb. */}
                      <span className="hidden sm:inline">Continue with </span>Junction 2026 Special Account
                    </span>
                  </button>
                ) : null}
                <button className={QUIET} onClick={() => go("token")}>
                  <KeyRound size={19} className="shrink-0" aria-hidden /> Use an operator token
                </button>
              </div>
              <Divider />
              <Link to="/signup" className="flex items-center justify-center gap-2.5 py-2 text-[17px] font-medium hover:underline">
                <Building2 size={19} aria-hidden /> Create an organisation account
              </Link>
            </>
          ) : null}

          <p className="mt-10 text-[14px] leading-relaxed text-ink-muted">For the best experience, use a computer.</p>
        </div>
      </main>
    </div>
  );
}

function Divider() {
  return (
    <div className="my-6 flex items-center gap-4 text-[15px] text-ink-soft">
      <span className="h-px flex-1 bg-line" />
      or
      <span className="h-px flex-1 bg-line" />
    </div>
  );
}
