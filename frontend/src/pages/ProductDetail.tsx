import { Link, useNavigate, useParams } from "react-router-dom";
import { ArrowLeft, Leaf, Thermometer, Truck } from "lucide-react";
import { api } from "../api";
import { Ring } from "../charts";
import { ALLERGEN_NAMES as ALLERGENS, Card, Empty, ErrorNote, Spinner, num, pct, useAsync } from "../ui";


const STATUS_STYLE: Record<string, string> = {
  contains: "bg-negative-soft text-negative",
  may_contain: "bg-warn-soft text-warn",
  undeclared: "bg-chip text-ink-muted",
  free_from: "bg-positive-soft text-positive",
};

const pretty = (s: string) => s.replaceAll("_", " ").toLowerCase();

export default function ProductDetail() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const { data: p, error, loading } = useAsync(() => api.product(id), [id]);

  if (error) return <ErrorNote error={error} />;
  if (loading || !p) return <Spinner />;

  const allergens = Object.entries(p.allergen_status ?? {});

  return (
    <div className="flex flex-col gap-4">
      <div>
        <button className="btn-ghost" onClick={() => navigate(-1)}>
          <ArrowLeft size={14} /> Back
        </button>
      </div>

      {/* Row from sm up, so a long name wraps beside the figures instead of pushing them onto a row of their own. */}
      <Card className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 sm:flex-1">
          <div className="mb-1.5 flex flex-wrap items-center gap-2">
            <span className="chip bg-chip text-ink-muted">#{p.id}</span>
            {p.gtin ? <span className="chip bg-chip text-ink-muted">GTIN {p.gtin}</span> : null}
            {p.category ? <span className="chip bg-chip text-ink-muted">category {p.category}</span> : null}
          </div>
          <h2 className="text-[20px] font-semibold leading-snug">{p.name_en}</h2>
          {p.name_fi && p.name_fi !== p.name_en ? <p className="mt-0.5 text-[13px] text-ink-muted">{p.name_fi}</p> : null}
          <p className="label mt-1">
            {[p.brand, p.vendor_name, p.country?.toUpperCase()].filter(Boolean).join(" · ")}
          </p>
        </div>
        {/* items-center: both label/value pairs centre on the ring, so the two figures share a line. */}
        <div className="flex shrink-0 items-center gap-4">
          <div className="flex items-center gap-2">
            <Ring value={p.delivery_reliability ?? 0} tone={(p.delivery_reliability ?? 0) >= 0.9 ? "positive" : "warn"}>
              {p.delivery_reliability == null ? "—" : Math.round(p.delivery_reliability * 100)}
            </Ring>
            <div>
              <div className="flex items-center gap-1 text-[11px] text-ink-muted">
                <Truck size={12} /> reliability
              </div>
              <div className="num text-[14px] font-medium">{p.delivery_reliability == null ? "no data" : pct(p.delivery_reliability)}</div>
            </div>
          </div>
          <div>
            <div className="flex items-center gap-1 text-[11px] text-ink-muted">
              <Leaf size={12} /> CO₂, group average
            </div>
            <div className="num text-[14px] font-medium">{p.co2_kg_per_kg == null ? "—" : `${num(p.co2_kg_per_kg, 2)} kg/kg`}</div>
          </div>
        </div>
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card className="flex flex-col gap-3">
          <h3 className="text-[14px] font-semibold">Pack and units</h3>
          <div className="grid grid-cols-2 gap-2 text-[12px]">
            <Fact label="Sales unit" value={p.sales_unit} />
            <Fact label="Base unit" value={p.base_unit} />
            <Fact label="Volume per sales unit" value={p.sales_volume_l != null ? `${num(p.sales_volume_l, 3)} L` : "—"} />
            <Fact label="Net weight per sales unit" value={p.sales_net_weight_kg != null ? `${num(p.sales_net_weight_kg, 3)} kg` : "—"} />
            <Fact label="Order step" value={p.allowed_lot_size != null ? String(p.allowed_lot_size) : "—"} />
            <Fact
              label="Storage"
              value={p.temperature_condition != null ? `condition ${p.temperature_condition}` : "—"}
              icon={<Thermometer size={12} />}
            />
          </div>
          {p.units && Object.keys(p.units).length ? (
            <div className="text-[12px] text-ink-muted">
              Units: {Object.entries(p.units).map(([u, n]) => `${u} = ${num(n, 3)}`).join(" · ")}
            </div>
          ) : null}
          {p.variable_unit ? (
            <div className="rounded-[var(--radius-tile)] bg-warn-soft p-2.5 text-[12px] text-warn">
              Variable weight — the invoiced quantity can differ from the ordered one.
            </div>
          ) : null}
        </Card>

        <Card className="flex flex-col gap-3">
          <h3 className="text-[14px] font-semibold">Claims and labels</h3>
          <div className="flex flex-wrap gap-1.5">
            {/* A claim FREE_FROM_X and a free_from entry X say the same thing — show it once. */}
            {[...new Set([...(p.claims ?? []).map(pretty), ...(p.free_from ?? []).map((c) => `free from ${pretty(c)}`)])].map((c) => (
              <span key={c} className="chip bg-positive-soft text-positive">
                {c}
              </span>
            ))}
            {(p.labels ?? []).map((c) => (
              <span key={"l" + c} className="chip bg-chip text-ink-muted">
                {pretty(c)}
              </span>
            ))}
            {!p.claims?.length && !p.free_from?.length && !p.labels?.length ? (
              <span className="label">No claims or labels in the catalog.</span>
            ) : null}
          </div>
          <p className="label">
            A claim only counts when the catalog states it. Missing data never passes as proof.
          </p>
        </Card>

        <Card className="flex flex-col gap-3 lg:col-span-2">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <h3 className="text-[14px] font-semibold">Allergens</h3>
            <span className={`chip ${p.has_allergen_declaration ? "bg-positive-soft text-positive" : "bg-warn-soft text-warn"}`}>
              {p.has_allergen_declaration ? "declaration on file" : "no allergen declaration"}
            </span>
          </div>
          {!p.has_allergen_declaration ? (
            <p className="text-[12px] text-ink-muted">
              Without a declaration this product cannot pass any allergen exclusion — the gateway treats “not listed” as
              unknown, not as absent.
            </p>
          ) : null}
          {!allergens.length ? (
            <Empty>No allergen statuses recorded.</Empty>
          ) : (
            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
              {allergens.map(([code, statuses]) => (
                <div key={code} className="flex items-center justify-between gap-2 rounded-[var(--radius-tile)] border border-line px-3 py-2">
                  <span className="text-[13px]">
                    <strong>{code}</strong> <span className="text-ink-muted">{ALLERGENS[code] ?? ""}</span>
                  </span>
                  <span className="flex gap-1">
                    {statuses.map((s) => (
                      <span key={s} className={`chip ${STATUS_STYLE[s] ?? "bg-chip text-ink-muted"}`}>
                        {pretty(s)}
                      </span>
                    ))}
                  </span>
                </div>
              ))}
            </div>
          )}
        </Card>
      </div>

      <div className="text-[12px] text-ink-muted">
        Catalog data is synthetic challenge data. <Link to="/buyer" className="underline">Back to the buyer flow</Link>
      </div>
    </div>
  );
}

function Fact({ label, value, icon }: { label: string; value: string; icon?: React.ReactNode }) {
  return (
    // Value pinned to the bottom: when a label wraps on a phone, both values in the row still share a baseline.
    <div className="flex flex-col justify-between gap-0.5 rounded-[var(--radius-tile)] border border-line p-2.5">
      <div className="flex items-center gap-1 text-[11px] text-ink-muted">
        {icon}
        {label}
      </div>
      <div className="num truncate text-[14px] font-medium" title={value}>
        {value}
      </div>
    </div>
  );
}
