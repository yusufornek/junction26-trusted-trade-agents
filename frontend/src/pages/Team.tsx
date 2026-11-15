import { useState, type FormEvent } from "react";
import { Check, Copy } from "lucide-react";
import { Avatar } from "../charts";
import {
  orgPeoplePath, peoplePath, permissionLabel, plainError, roleLabel, team,
  type GuardrailProfile, type OnceLink, type Organisation, type RoleInfo, type Session, type SignupOptions, type TeamUser,
} from "../team";
import { Card, Empty, ErrorNote, Spinner, dateOnly, eur, num, useAsync, when } from "../ui";

/** A link the gateway returned once; it lives in page state only until the person dismisses it. */
type Handover = { title: string; link: OnceLink };

export default function Team() {
  const { data, error, loading } = useAsync(() => Promise.all([team.session(), team.roles(), team.options()]), []);
  if (error) return <ErrorNote error={error} />;
  if (loading || !data) return <Spinner />;
  const [session, roles, options] = data;
  const staff = session.role === "platform_admin";

  return (
    <div className="flex flex-col gap-4">
      <Card className="flex flex-col gap-2">
        <h2 className="text-[16px] font-semibold">Who is signed in</h2>
        <p className="text-[14px]">
          {session.user ? session.user.email : "The operator token"}, as {roleLabel(session.role).toLowerCase()}
          {session.tenant ? ` of ${session.tenant.name}` : staff ? ", not tied to one organisation" : ""}.{" "}
          {session.customer_ids.length
            ? `Orders for customer ${session.customer_ids.length === 1 ? "number" : "numbers"} ${session.customer_ids.join(", ")}.`
            : "No customer numbers of its own."}
        </p>
        <Permissions codes={session.permissions} lead="This sign-in may:" />
      </Card>

      <Card className="flex flex-col gap-3">
        <h2 className="text-[16px] font-semibold">Roles in an organisation</h2>
        <div className="grid gap-3 sm:grid-cols-2">
          {roles.map((r) => (
            <div key={r.role} className="rounded-[var(--radius-tile)] bg-page p-3">
              <div className="text-[14px] font-medium">{roleLabel(r.role)}</div>
              <Permissions codes={r.permissions} />
            </div>
          ))}
        </div>
      </Card>

      <People session={session} staff={staff} roles={roles} />
      {staff ? (
        <Organisations roles={roles} options={options} />
      ) : session.tenant ? (
        <Card className="flex flex-col gap-2">
          <h2 className="text-[16px] font-semibold">{session.tenant.name}</h2>
          <Profile org={session.tenant} options={options} />
        </Card>
      ) : null}
    </div>
  );
}

function Permissions({ codes, lead }: { codes: string[]; lead?: string }) {
  return (
    <div className="text-[13px] text-ink-muted">
      {lead}
      <ul className="mt-1 list-disc pl-5">
        {codes.map((c) => <li key={c}>{permissionLabel(c)}</li>)}
      </ul>
    </div>
  );
}

/** One action at a time per list: a busy key for the button, a plain error, and a reload afterwards. */
function useAction(reload: () => Promise<unknown>) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const run = async (key: string, fn: () => Promise<unknown>) => {
    setBusy(key);
    setError(null);
    try {
      await fn();
      await reload();
    } catch (e) {
      setError(plainError(e));
    } finally {
      setBusy(null);
    }
  };
  return { busy, error, run };
}

function HandoverNote({ handover, onDone }: { handover: Handover; onDone: () => void }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(handover.link.link);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard can be blocked outside a secure context; the text stays selectable.
    }
  };
  return (
    <div className="flex min-w-0 flex-col gap-2 rounded-[var(--radius-tile)] border border-warn/30 bg-warn-soft p-3">
      <p className="text-[13px] font-medium text-warn">
        {handover.title}. Copy it now: it is shown once and the gateway sends no e-mail. It works one time, until{" "}
        {dateOnly(handover.link.expires_at)}.
      </p>
      <pre className="overflow-x-auto whitespace-pre-wrap break-all rounded-[var(--radius-tile)] bg-surface p-3 font-mono text-[12px] text-ink">
        {handover.link.link}
      </pre>
      <div className="flex flex-wrap gap-2">
        <button className="btn-dark" onClick={copy}>
          {copied ? <Check size={14} aria-hidden /> : <Copy size={14} aria-hidden />} {copied ? "Copied" : "Copy link"}
        </button>
        <button className="btn-ghost" onClick={onDone}>I have handed it over</button>
      </div>
    </div>
  );
}

function RoleSelect({ roles, value, onChange, label, disabled }: { roles: string[]; value: string; onChange: (role: string) => void; label: string; disabled?: boolean }) {
  return (
    <select className="field !w-auto" aria-label={label} value={value} disabled={disabled} onChange={(e) => onChange(e.target.value)}>
      {(roles.includes(value) ? roles : [value, ...roles]).map((r) => <option key={r} value={r}>{roleLabel(r)}</option>)}
    </select>
  );
}

const STATUS_TONE: Record<TeamUser["status"], string> = {
  active: "bg-positive-soft text-positive",
  invited: "bg-warn-soft text-warn",
  disabled: "bg-birch-soft text-birch-ink",
  removed: "bg-birch-soft text-birch-ink",
};

function People({ session, staff, roles }: { session: Session; staff: boolean; roles: RoleInfo[] }) {
  const base = peoplePath(staff);
  // Valio staff have a single role; an organisation's admin picks from the organisation roles.
  const choices = staff ? ["platform_admin"] : roles.map((r) => r.role);
  const users = useAsync(() => team.users(base), [base]);
  const { busy, error, run } = useAction(users.reload);
  const [handover, setHandover] = useState<Handover | null>(null);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState(staff ? "platform_admin" : "viewer");
  const [armed, setArmed] = useState<string | null>(null);

  const invite = (e: FormEvent) => {
    e.preventDefault();
    run("invite", async () => {
      setHandover({ title: `Invite link for ${email.trim()}`, link: await team.invite(base, email.trim(), role) });
      setEmail("");
    });
  };
  const issue = (u: TeamUser, kind: "resend-invite" | "password-reset") =>
    run(`link:${u.id}`, async () =>
      setHandover({ title: `${kind === "resend-invite" ? "New invite link" : "Password reset link"} for ${u.email}`, link: await team.link(base, u.id, kind) }),
    );

  return (
    <Card className="flex flex-col gap-3">
      <div>
        <h2 className="text-[16px] font-semibold">{staff ? "Valio staff" : "People"}</h2>
        <p className="label mt-0.5">
          {staff
            ? "The gateway shows an organisation's people only to that organisation's own admin, so this list holds Valio staff. You can invite a person into an organisation further down."
            : `Everyone who can sign in to ${session.tenant?.name ?? "your organisation"}.`}
        </p>
      </div>
      {error ? <ErrorNote error={error} /> : null}
      {handover ? <HandoverNote handover={handover} onDone={() => setHandover(null)} /> : null}

      {users.error ? <ErrorNote error={users.error} /> : users.loading ? <Spinner /> : !users.data?.length ? (
        <Empty>Nobody here yet. Invite the first person below.</Empty>
      ) : (
        <ul className="flex flex-col gap-1.5">
          {users.data.map((u) => {
            const self = session.user?.id === u.id;
            return (
              <li key={u.id} className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-[var(--radius-tile)] bg-page p-3">
                <Avatar name={u.email.split("@")[0]} />
                <div className="min-w-0 flex-1 basis-44">
                  <div className="truncate text-[14px] font-medium">{u.email}{self ? " (you)" : ""}</div>
                  <div className="label">
                    {roleLabel(u.role)} · {u.last_sign_in ? `last signed in ${when(u.last_sign_in)}` : "has not signed in yet"}
                  </div>
                </div>
                <span className={`chip ${STATUS_TONE[u.status]}`}>{u.status}</span>
                <div className="flex w-full flex-wrap items-center gap-2 md:w-auto">
                  {choices.length > 1 && !self && u.status !== "disabled" ? (
                    <RoleSelect roles={choices} value={u.role} label={`Role of ${u.email}`} disabled={busy !== null}
                      onChange={(next) => run(`role:${u.id}`, () => team.change(base, u.id, { role: next }))} />
                  ) : null}
                  {u.status === "invited" ? (
                    <button className="btn-ghost" disabled={busy !== null} onClick={() => issue(u, "resend-invite")}>
                      {busy === `link:${u.id}` ? "Creating…" : "New invite link"}
                    </button>
                  ) : u.status === "active" ? (
                    <button className="btn-ghost" disabled={busy !== null} onClick={() => issue(u, "password-reset")}>
                      {busy === `link:${u.id}` ? "Creating…" : "Create reset link"}
                    </button>
                  ) : null}
                  {self ? null : u.status === "disabled" ? (
                    <button className="btn-ghost" disabled={busy !== null} onClick={() => run(`able:${u.id}`, () => team.change(base, u.id, { disabled: false }))}>
                      {busy === `able:${u.id}` ? "Enabling…" : "Enable again"}
                    </button>
                  ) : (
                    // Two clicks: disabling also ends their sessions and revokes every key they created.
                    <button className="btn-danger" disabled={busy !== null}
                      onClick={() => {
                        setArmed(armed === u.id ? null : u.id);
                        if (armed === u.id) run(`able:${u.id}`, () => team.change(base, u.id, { disabled: true }));
                      }}>
                      {busy === `able:${u.id}` ? "Disabling…" : armed === u.id ? "Yes, disable and revoke their keys" : "Disable"}
                    </button>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}

      <form onSubmit={invite} className="flex flex-wrap items-end gap-2 border-t border-line pt-3">
        <label className="flex min-w-0 flex-1 basis-56 flex-col gap-1.5">
          <span className="label">{staff ? "Invite a Valio colleague" : "Invite a person"}</span>
          <input className="field" type="email" required placeholder="name@company.example" value={email} onChange={(e) => setEmail(e.target.value)} />
        </label>
        {choices.length > 1 ? <RoleSelect roles={choices} value={role} onChange={setRole} label="Role for the new person" /> : null}
        <button className="btn-dark" disabled={busy !== null || !email.trim()}>{busy === "invite" ? "Creating…" : "Create invite link"}</button>
      </form>
    </Card>
  );
}

function Profile({ org, options }: { org: Organisation; options: SignupOptions }) {
  const p: GuardrailProfile | null = org.guardrail_profile;
  if (!p) return <p className="label">No business profile yet. An organisation fills it in when it signs itself up.</p>;
  const label = (list: { value: string; label: string }[], v: string) => list.find((x) => x.value === v)?.label ?? v;
  const g = (key: string) => p.guardrails[key]?.value;
  const b = p.profile;
  return (
    <p className="text-[13px] text-ink-muted">
      {label(options.business_types, b.business_type)}, {num(b.sites)} {b.sites === 1 ? "site" : "sites"},{" "}
      {label(options.monthly_purchase_volume_bands, b.monthly_purchase_volume_band)} a month,{" "}
      {label(options.meals_per_day_bands, b.meals_per_day_band)} meals a day. Its guardrails started from the{" "}
      {label(options.business_types, p.preset).toLowerCase()} preset: no order above {eur(g("per_order_ceiling_eur") as number)}, approvers sign off up to{" "}
      {eur(g("approver_amount_threshold_eur") as number)}, {g("four_eyes") ? "a second person must approve" : "people may approve their own agent's orders"},{" "}
      {eur(g("monthly_budget_eur") as number)} a month in total.
    </p>
  );
}

function Organisations({ roles, options }: { roles: RoleInfo[]; options: SignupOptions }) {
  const orgs = useAsync(() => team.organisations(), []);
  const action = useAction(orgs.reload);
  const [handover, setHandover] = useState<Handover | null>(null);
  const [name, setName] = useState("");
  const [customers, setCustomers] = useState("");

  const create = (e: FormEvent) => {
    e.preventDefault();
    action.run("create", async () => {
      await team.createOrganisation(name.trim(), customers.split(/[\s,]+/).filter(Boolean));
      setName("");
      setCustomers("");
    });
  };

  return (
    <Card className="flex flex-col gap-3">
      <div>
        <h2 className="text-[16px] font-semibold">Organisations</h2>
        <p className="label mt-0.5">Every buyer organisation on this gateway. Only Valio staff see this list.</p>
      </div>
      {action.error ? <ErrorNote error={action.error} /> : null}
      {handover ? <HandoverNote handover={handover} onDone={() => setHandover(null)} /> : null}

      {orgs.error ? <ErrorNote error={orgs.error} /> : orgs.loading ? <Spinner /> : !orgs.data?.length ? (
        <Empty>No organisations yet. Create the first one below.</Empty>
      ) : (
        <ul className="flex flex-col gap-1.5">
          {orgs.data.map((o) => (
            <OrgRow key={o.id} org={o} roles={roles.map((r) => r.role)} options={options} action={action} onLink={setHandover} />
          ))}
        </ul>
      )}

      <form onSubmit={create} className="flex flex-wrap items-end gap-2 border-t border-line pt-3">
        <label className="flex min-w-0 flex-1 basis-56 flex-col gap-1.5">
          <span className="label">New organisation</span>
          <input className="field" required maxLength={200} placeholder="Name, for example Ravintola Saaristo" value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="flex min-w-0 flex-1 basis-44 flex-col gap-1.5">
          <span className="label">Valio customer numbers (optional)</span>
          <input className="field" placeholder="For example 3, 4" value={customers} onChange={(e) => setCustomers(e.target.value)} />
        </label>
        <button className="btn-dark" disabled={action.busy !== null || !name.trim()}>{action.busy === "create" ? "Creating…" : "Create organisation"}</button>
        <p className="label w-full">A new organisation starts empty. Invite its first admin from its row once it appears.</p>
      </form>
    </Card>
  );
}

function OrgRow({ org, roles, options, action, onLink }: {
  org: Organisation; roles: string[]; options: SignupOptions; action: ReturnType<typeof useAction>; onLink: (h: Handover) => void;
}) {
  const [open, setOpen] = useState<"invite" | "pause" | null>(null);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("tenant_admin");
  const [reason, setReason] = useState("");
  const paused = Boolean(org.paused_at);
  const busy = action.busy !== null;

  const invite = (e: FormEvent) => {
    e.preventDefault();
    action.run(`invite:${org.id}`, async () => {
      onLink({ title: `Invite link for ${email.trim()} at ${org.name}`, link: await team.invite(orgPeoplePath(org.id), email.trim(), role) });
      setEmail("");
      setOpen(null);
    });
  };
  // The gateway answers the first request with a confirmation token and changes nothing; the form itself is the
  // review step, so the second request follows at once with the same reason.
  const setPaused = (e: FormEvent) => {
    e.preventDefault();
    action.run(`pause:${org.id}`, async () => {
      const first = await team.setPaused(org.id, !paused, reason.trim());
      if ("confirmation_token" in first) await team.setPaused(org.id, !paused, reason.trim(), first.confirmation_token);
      setReason("");
      setOpen(null);
    });
  };

  return (
    <li className="flex flex-col gap-2 rounded-[var(--radius-tile)] bg-page p-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <Avatar name={org.name} />
        <div className="min-w-0 flex-1 basis-44">
          <div className="truncate text-[14px] font-medium">{org.name}</div>
          <div className="label">
            {org.customer_ids?.length ? `Customer ${org.customer_ids.length === 1 ? "number" : "numbers"} ${org.customer_ids.join(", ")}` : "No customer numbers yet"} · since {dateOnly(org.created_at)}
          </div>
        </div>
        <span className={`chip ${org.disabled_at ? STATUS_TONE.disabled : paused ? STATUS_TONE.invited : STATUS_TONE.active}`}>
          {org.disabled_at ? "closed" : paused ? "paused" : "active"}
        </span>
      </div>
      {paused ? (
        <p className="rounded-[var(--radius-tile)] bg-warn-soft p-2.5 text-[13px] text-warn">
          Paused {when(org.paused_at)} by {org.paused_by === "legacy-operator" ? "the operator token" : org.paused_by}: {org.pause_reason}
        </p>
      ) : null}
      <Profile org={org} options={options} />
      {org.disabled_at ? null : (
        <div className="flex flex-wrap gap-2">
          <button className="btn-ghost" disabled={busy} onClick={() => setOpen(open === "invite" ? null : "invite")}>Invite a person</button>
          <button className={paused ? "btn-ghost" : "btn-danger"} disabled={busy} onClick={() => setOpen(open === "pause" ? null : "pause")}>
            {paused ? "Resume organisation" : "Pause organisation"}
          </button>
        </div>
      )}
      {open === "invite" ? (
        <form onSubmit={invite} className="flex flex-wrap items-end gap-2">
          <input className="field min-w-0 flex-1 basis-56" type="email" required aria-label={`E-mail of the person to invite to ${org.name}`} placeholder="name@company.example" value={email} onChange={(e) => setEmail(e.target.value)} />
          <RoleSelect roles={roles} value={role} onChange={setRole} label="Role for the new person" />
          <button className="btn-dark" disabled={busy || !email.trim()}>{action.busy === `invite:${org.id}` ? "Creating…" : "Create invite link"}</button>
          <p className="label w-full">You will not see this person in a list afterwards: only the organisation's own admin can.</p>
        </form>
      ) : null}
      {open === "pause" ? (
        <form onSubmit={setPaused} className="flex flex-wrap items-end gap-2">
          <label className="flex min-w-0 flex-1 basis-56 flex-col gap-1.5">
            <span className="label">
              {paused
                ? `Resuming lets the agents of ${org.name} order again. Why now?`
                : `Pausing stops every agent of ${org.name} from ordering until someone resumes it. Why?`}
            </span>
            <input className="field" required maxLength={2000} placeholder="Goes into the audit trail" value={reason} onChange={(e) => setReason(e.target.value)} />
          </label>
          <button className={paused ? "btn-dark" : "btn-danger"} disabled={busy || !reason.trim()}>
            {action.busy === `pause:${org.id}` ? "Working…" : paused ? "Resume this organisation" : "Pause this organisation now"}
          </button>
          <button type="button" className="btn-ghost" onClick={() => setOpen(null)}>Keep as is</button>
        </form>
      ) : null}
    </li>
  );
}
