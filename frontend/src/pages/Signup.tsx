import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { Check } from "lucide-react";
import { call, type Warning } from "../api";
import { Card, ErrorNote, Spinner, eur, useAsync } from "../ui";

type Option = { value: string; label: string };
type Options = { business_types: Option[]; monthly_purchase_volume_bands: Option[]; meals_per_day_bands: Option[] };
type Guardrail = { value: unknown; source: string };
type Created = {
  tenant: { id: string; name: string };
  user: { email: string; role: string };
  customer_ids: string[];
  guardrail_profile: { preset: string; guardrails: Record<string, Guardrail>; warnings: Warning[] };
};

// Catalog vocabulary (GS1 allergen codes, nutritional claims), so the gateway can check every product against it.
const CLAIMS: [string, string][] = [["FREE_FROM_LACTOSE", "Lactose free"], ["FREE_FROM_GLUTEN", "Gluten free"], ["VEGETARIAN", "Vegetarian"]];
const ALLERGENS: [string, string][] = [
  ["AM", "Milk"], ["AE", "Egg"], ["AW", "Cereals with gluten"], ["AN", "Tree nuts"], ["AP", "Peanuts"], ["AY", "Soy"], ["AF", "Fish"], ["AC", "Crustaceans"],
];
const CARE = "hospital_or_school_kitchen";

const GUARDRAIL_WORDS: Record<string, string> = {
  per_order_ceiling_eur: "Largest order an agent may confirm alone",
  daily_budget_eur: "Spend per day before a person must approve",
  monthly_budget_eur: "Spend per month before a person must approve",
  approver_amount_threshold_eur: "Above this amount only an admin may approve",
  four_eyes: "A second person approves what someone set up",
  substitutes_policy: "Substitute products",
  anomaly_sensitivity: "Warning on unusual orders",
  starting_autonomy: "New agents start",
};

function shown(field: string, value: unknown): string {
  if (typeof value === "number") return eur(value);
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (value === "approval_required") return field === "starting_autonomy" ? "With a person signing each order" : "Always need approval";
  if (field === "anomaly_sensitivity" && value && typeof value === "object") {
    const v = value as { price_increase_pct?: number };
    return v.price_increase_pct != null ? `On, a price rise over ${v.price_increase_pct}% is flagged` : "On";
  }
  if (Array.isArray(value)) return `${value.length} sites`;
  return String(value).replaceAll("_", " ");
}

const field = "flex flex-col gap-1.5";

/** Public: a kitchen without an operator token creates its organisation here (spec 45). */
export default function Signup() {
  const options = useAsync(() => call<Options>("/v1/auth/signup/options"), []);
  const [form, setForm] = useState({ name: "", email: "", password: "", business_type: "", sites: "1", people: "2", volume: "", meals: "" });
  const [claims, setClaims] = useState<string[]>([]);
  const [allergens, setAllergens] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [created, setCreated] = useState<Created | null>(null);
  const set = (key: keyof typeof form) => (e: { target: { value: string } }) => setForm({ ...form, [key]: e.target.value });
  const toggle = (list: string[], put: (v: string[]) => void, code: string) =>
    put(list.includes(code) ? list.filter((c) => c !== code) : [...list, code]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      setCreated(
        await call<Created>("/v1/auth/signup", {
          method: "POST",
          body: JSON.stringify({
            name: form.name.trim(),
            email: form.email.trim(),
            password: form.password,
            profile: {
              business_type: form.business_type,
              sites: Number(form.sites),
              buyers_and_approvers: Number(form.people),
              monthly_purchase_volume_band: form.volume,
              meals_per_day_band: form.meals,
              required_claims: claims,
              exclude_allergens: allergens,
            },
          }),
        }),
      );
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  if (created) {
    const g = created.guardrail_profile;
    const preset = options.data?.business_types.find((t) => t.value === g.preset)?.label.toLowerCase() ?? g.preset.replaceAll("_", " ");
    return (
      <Card className="mx-auto flex w-full max-w-2xl flex-col gap-5">
        <div className="flex items-start gap-3">
          <span className="grid size-10 shrink-0 place-items-center rounded-2xl bg-positive-soft text-positive">
            <Check size={20} aria-hidden />
          </span>
          <div className="min-w-0">
            <h2 className="break-words text-[17px] font-semibold">{created.tenant.name} is set up</h2>
            <p className="label mt-0.5">
              {created.user.email} is its admin. Customer number {created.customer_ids.join(", ")}.
            </p>
          </div>
        </div>
        <div>
          <h3 className="text-[15px] font-semibold">Starting limits, from the {preset} preset</h3>
          <p className="label">Every agent this kitchen connects works inside these. An admin can tighten them at any time.</p>
        </div>
        <ul className="flex flex-col divide-y divide-line text-[13px]">
          {Object.entries(g.guardrails).map(([key, rail]) => (
            <li key={key} className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-0.5 py-2">
              <span className="min-w-0 flex-1 basis-56">{GUARDRAIL_WORDS[key] ?? key.replaceAll("_", " ")}</span>
              <span className="num font-medium">{shown(key, rail.value)}</span>
            </li>
          ))}
        </ul>
        {g.warnings.map((w) => (
          <p key={w.code} className="rounded-[var(--radius-tile)] bg-warn-soft p-3 text-[13px] text-warn">
            {w.code === "SELF_APPROVAL_ALLOWED"
              ? "A small kitchen has no second person, so one person may approve their own orders. Each such approval is marked."
              : w.text}
          </p>
        ))}
        <p className="text-[13px] text-ink-muted">
          Sign in with that e-mail and password to manage approvals, limits and the team. The Valio team also sees the new
          organisation on its Team page.
        </p>
        <Link to="/" className="btn-ghost self-start">
          Back to sign-in
        </Link>
      </Card>
    );
  }

  if (options.error) return <ErrorNote error={options.error} />;
  const o = options.data;
  if (!o) return <Spinner />;
  const care = form.business_type === CARE;

  return (
    <Card className="mx-auto w-full max-w-2xl">
      <form onSubmit={submit} className="flex flex-col gap-5">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <label className={`${field} sm:col-span-2`}>
            <span className="label">Organisation name</span>
            <input className="field" required maxLength={120} value={form.name} onChange={set("name")} placeholder="Hotel Aurora chain" />
          </label>
          <label className={field}>
            <span className="label">Your e-mail (you become its admin)</span>
            <input className="field" type="email" required autoComplete="email" value={form.email} onChange={set("email")} />
          </label>
          <label className={field}>
            <span className="label">Password, at least 10 characters</span>
            <input className="field" type="password" required minLength={10} autoComplete="new-password" value={form.password} onChange={set("password")} />
          </label>
        </div>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <label className={`${field} sm:col-span-2`}>
            <span className="label">What kind of kitchen</span>
            <select className="field" required value={form.business_type} onChange={set("business_type")}>
              <option value="" disabled>
                Choose one
              </option>
              {o.business_types.map((t) => (
                <option key={t.value} value={t.value}>
                  {t.label}
                </option>
              ))}
            </select>
          </label>
          <label className={field}>
            <span className="label">Sites</span>
            <input className="field" type="number" required min={1} value={form.sites} onChange={set("sites")} />
          </label>
          <label className={field}>
            <span className="label">People who buy or approve</span>
            <input className="field" type="number" required min={1} value={form.people} onChange={set("people")} />
          </label>
          <label className={field}>
            <span className="label">Purchases per month</span>
            <select className="field" required value={form.volume} onChange={set("volume")}>
              <option value="" disabled>
                Choose a range
              </option>
              {o.monthly_purchase_volume_bands.map((b) => (
                <option key={b.value} value={b.value}>
                  {b.label}
                </option>
              ))}
            </select>
          </label>
          <label className={field}>
            <span className="label">Meals per day</span>
            <select className="field" required value={form.meals} onChange={set("meals")}>
              <option value="" disabled>
                Choose a range
              </option>
              {o.meals_per_day_bands.map((b) => (
                <option key={b.value} value={b.value}>
                  {b.label}
                </option>
              ))}
            </select>
          </label>
        </div>

        <fieldset className="flex flex-col gap-2">
          <legend className="label mb-1.5">Rules every product must meet{care ? " (needed before the first order)" : " (optional)"}</legend>
          <div className="flex flex-wrap gap-2">
            {CLAIMS.map(([code, label]) => (
              <Pick key={code} label={label} on={claims.includes(code)} flip={() => toggle(claims, setClaims, code)} />
            ))}
          </div>
          <span className="label mt-1">Never contains</span>
          <div className="flex flex-wrap gap-2">
            {ALLERGENS.map(([code, label]) => (
              <Pick key={code} label={label} on={allergens.includes(code)} flip={() => toggle(allergens, setAllergens, code)} />
            ))}
          </div>
          {care ? (
            <span className="text-[12px] text-ink-muted">A hospital or school kitchen cannot order until it has at least one of each.</span>
          ) : null}
        </fieldset>

        {error ? <ErrorNote error={error} /> : null}
        <button className="btn-dark self-start" disabled={busy}>
          {busy ? "Creating…" : "Create organisation"}
        </button>
      </form>
    </Card>
  );
}

function Pick({ label, on, flip }: { label: string; on: boolean; flip: () => void }) {
  return (
    <label className="flex h-9 cursor-pointer items-center gap-2 rounded-full border border-line bg-surface px-3.5 text-[13px] has-checked:border-brand has-checked:bg-brand-soft">
      <input type="checkbox" className="accent-brand" checked={on} onChange={flip} />
      {label}
    </label>
  );
}
