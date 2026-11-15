import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { ArrowLeft, Building2, CircleHelp, KeyRound, LogIn, Mail, Sparkles } from "lucide-react";
import { LuotsiMark, LuotsiWordmark } from "./brand";
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

// The same rows, radii and white-on-teal levels as the console's rail, so signing in already looks like the console.
const ROW = "flex h-10 w-full shrink-0 items-center gap-3 rounded-2xl px-[11px] text-left text-[14px] font-medium transition-colors";
const IDLE = "text-white/75 hover:bg-white/10 hover:text-white focus-visible:outline-2 focus-visible:outline-white";
const FIELD =
  "h-11 w-full rounded-2xl border border-white/15 bg-white/10 px-4 text-[15px] text-white outline-none transition-colors placeholder:text-white/45 focus:border-white/60 focus:bg-white/[0.14]";
const WIDTH = "w-[min(380px,calc(100vw-2rem))]";

/** Signed-out screen in the rail's design: it opens as the 72 px icon rail and widens to show every way in. */
export default function SignIn({ demoToken, signingIn, error, onToken, onEmail, onClearError }: Props) {
  const [open, setOpen] = useState(false);
  const [method, setMethod] = useState<"email" | "token">("email");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [token, setTokenValue] = useState("");

  // One orchestrated moment: the rail widens just after the first paint, like the console rail on hover.
  useEffect(() => {
    const id = requestAnimationFrame(() => setOpen(true));
    return () => cancelAnimationFrame(id);
  }, []);

  const pick = (next: typeof method) => {
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
    <div className="grid min-h-dvh place-items-center bg-page p-4">
      <div
        className={`overflow-hidden rounded-[26px] bg-rail shadow-[0_24px_60px_rgba(0,67,87,0.22)] transition-[width] duration-500 ease-out motion-reduce:transition-none ${
          open ? WIDTH : "w-[72px]"
        }`}
      >
        <div className={`flex ${WIDTH} flex-col px-4 py-4 text-white`}>
          <div className="flex h-10 shrink-0 items-center gap-3">
            <span className="grid size-10 shrink-0 place-items-center rounded-2xl bg-white text-rail">
              <LuotsiMark className="block h-6 w-auto" />
            </span>
            <Label open={open}>
              <LuotsiWordmark className="block h-[18px] w-auto text-white" />
            </Label>
          </div>

          <form className="mt-6 flex flex-col gap-2.5" onSubmit={submit}>
            <div className="mb-1 flex h-10 items-center gap-3 px-[11px]">
              {method === "email" ? <Mail size={18} className="shrink-0" aria-hidden /> : <KeyRound size={18} className="shrink-0" aria-hidden />}
              <Label open={open} className="text-[15px] font-semibold">
                {method === "email" ? "Sign in with e-mail" : "Sign in with an operator token"}
              </Label>
            </div>
            <Fade open={open}>
              {method === "email" ? (
                <>
                  <input className={FIELD} type="email" autoComplete="username" required placeholder="E-mail" aria-label="E-mail" value={email} onChange={(e) => setEmail(e.target.value)} />
                  <input
                    className={FIELD}
                    type="password"
                    autoComplete="current-password"
                    required
                    placeholder="Password"
                    aria-label="Password"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                  />
                </>
              ) : (
                <>
                  <input className={FIELD} type="password" autoComplete="off" required autoFocus placeholder="Operator token" aria-label="Operator token" value={token} onChange={(e) => setTokenValue(e.target.value)} />
                  <span className="px-1 text-[12px] text-white/60">Kept in memory only, so a reload asks again.</span>
                </>
              )}
              <button className="mt-1 flex h-11 w-full items-center justify-center gap-2 rounded-2xl bg-white text-[15px] font-semibold text-rail transition-colors hover:bg-white/90 disabled:opacity-60" disabled={signingIn}>
                <LogIn size={17} aria-hidden /> {signingIn ? "Signing in…" : "Log in"}
              </button>
              {error ? <ErrorNote error={error} /> : null}
            </Fade>
          </form>

          <div className="my-4 h-px bg-white/15" />

          <div className="flex flex-col gap-1">
            {demoToken ? (
              <Row icon={<Sparkles size={18} />} open={open} onClick={() => onToken(demoToken)} disabled={signingIn}>
                Junction 2026 Special Account
              </Row>
            ) : null}
            {method === "email" ? (
              <Row icon={<KeyRound size={18} />} open={open} onClick={() => pick("token")}>
                Use an operator token
              </Row>
            ) : (
              <Row icon={<ArrowLeft size={18} />} open={open} onClick={() => pick("email")}>
                Use e-mail instead
              </Row>
            )}
            <Link to="/signup" className={`${ROW} ${IDLE}`}>
              <Building2 size={18} className="shrink-0" />
              <Label open={open}>Create an organisation account</Label>
            </Link>
          </div>

          <div className="mt-8 flex flex-col gap-1">
            <a href="/docs" target="_blank" rel="noreferrer" className={`${ROW} ${IDLE}`}>
              <CircleHelp size={18} className="shrink-0" />
              <Label open={open}>API reference</Label>
            </a>
            <Fade open={open}>
              <p className="px-[11px] pt-1 text-[12px] text-white/55">For the best experience, use a computer.</p>
            </Fade>
          </div>
        </div>
      </div>
    </div>
  );
}

function Row({ icon, open, onClick, disabled, children }: { icon: ReactNode; open: boolean; onClick: () => void; disabled?: boolean; children: ReactNode }) {
  return (
    <button type="button" onClick={onClick} disabled={disabled} className={`${ROW} ${IDLE} disabled:opacity-60`}>
      <span className="shrink-0">{icon}</span>
      <Label open={open}>{children}</Label>
    </button>
  );
}

/** Names slide in after the rail has started to widen, as on the console rail. */
function Label({ open, className = "", children }: { open: boolean; className?: string; children: ReactNode }) {
  return (
    <span
      className={`whitespace-nowrap transition-[opacity,transform] duration-300 motion-reduce:transition-none ${
        open ? "translate-x-0 opacity-100 delay-200" : "-translate-x-1 opacity-0"
      } ${className}`}
    >
      {children}
    </span>
  );
}

function Fade({ open, children }: { open: boolean; children: ReactNode }) {
  return (
    <div className={`flex flex-col gap-2.5 transition-opacity duration-300 motion-reduce:transition-none ${open ? "opacity-100 delay-300" : "opacity-0"}`}>
      {children}
    </div>
  );
}
