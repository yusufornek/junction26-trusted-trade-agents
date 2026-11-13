import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import {
  Card,
  Empty,
  ErrorNote,
  Spinner,
  StatusChip,
  WarningChips,
  eur,
  num,
  pct,
  useAsync,
  when,
} from "../ui";

export default function OrderDetail() {
  const { id = "" } = useParams();
  const order = useAsync(() => api.order(id), [id]);
  const audit = useAsync(() => api.audit({ order_id: id }), [id]);

  if (order.error) return <ErrorNote error={order.error} />;
  if (order.loading || !order.data) return <Spinner />;
  const o = order.data;

  return (
    <div className="flex flex-col gap-4">
      <Card className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="mb-2 flex items-center gap-2">
            <StatusChip status={o.status} />
            <span className="label">order {o.id.slice(0, 8)}</span>
          </div>
          <h2 className="text-[18px] font-semibold">{o.lines[0]?.name ?? o.id}</h2>
          <p className="label mt-1">
            placed by {o.agent_id} · {when(o.created_at)}
            {o.approver ? ` · signed off by ${o.approver}` : ""}
          </p>
          {o.comment ? <p className="mt-2 text-[13px] text-ink-muted">“{o.comment}”</p> : null}
        </div>
        <div className="text-right">
          <div className="num text-[28px] font-semibold">{eur(o.total, o.currency)}</div>
          <div className="label">
            {o.live_calls ? `${o.live_calls.price + o.live_calls.availability} upstream calls` : ""}
          </div>
        </div>
      </Card>

      <div className="grid gap-4 lg:grid-cols-[1.5fr_1fr]">
        <div className="flex flex-col gap-4">
          <Card className="flex flex-col gap-3">
            <h3 className="text-[15px] font-semibold">Lines</h3>
            {o.lines.map((l) => (
              <div key={l.product_id} className="flex flex-col gap-2 rounded-[var(--radius-tile)] bg-page p-3">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="text-[14px] font-medium">{l.name}</div>
                    <div className="label">
                      {num(l.quantity_sales_units ?? l.units_needed)} {l.sales_unit} ×{" "}
                      {eur(l.unit_price, l.currency)} · {eur(l.price_per_l_or_kg, l.currency)}/{l.size_unit ?? "unit"}
                    </div>
                  </div>
                  <div className="num text-[15px] font-semibold">{eur(l.total, l.currency)}</div>
                </div>
                <WarningChips warnings={l.warnings} />
                <div className="grid grid-cols-3 gap-2 text-[12px] text-ink-muted">
                  <span>coverage {pct(l.availability?.coverage)}</span>
                  <span>
                    reliability {l.delivery_reliability == null ? "—" : pct(l.delivery_reliability)}
                  </span>
                  <span>CO₂ {l.co2_kg_per_kg == null ? "—" : `${num(l.co2_kg_per_kg, 2)} kg/kg`}</span>
                </div>
                {l.provenance ? (
                  <div className="border-t border-line pt-2 text-[11px] text-ink-soft">
                    {Object.entries(l.provenance).map(([k, p]) => (
                      <div key={k}>
                        {k}: {p.source} · fetched {when(p.fetched_at)}
                        {p.valid_to ? ` · valid to ${new Date(p.valid_to).toLocaleDateString("en-GB")}` : ""}
                      </div>
                    ))}
                  </div>
                ) : null}
              </div>
            ))}
          </Card>

          <Card className="flex flex-col gap-3">
            <h3 className="text-[15px] font-semibold">Audit trail</h3>
            {audit.loading ? (
              <Spinner />
            ) : !audit.data?.length ? (
              <Empty>No entries for this order.</Empty>
            ) : (
              <ol className="flex flex-col gap-3">
                {audit.data.map((e) => (
                  <li key={e.id} className="flex gap-3">
                    <span
                      className={`mt-1.5 size-2 shrink-0 rounded-full ${
                        e.actor.startsWith("human") ? "bg-positive" : "bg-ink-soft"
                      }`}
                    />
                    <div className="min-w-0">
                      <div className="text-[13px] font-medium">{e.action.replaceAll("_", " ").toLowerCase()}</div>
                      <div className="text-[12px] text-ink-muted">
                        {e.actor} · {when(e.ts)}
                      </div>
                      {e.summary ? <div className="mt-0.5 text-[12px] text-ink-muted">{e.summary}</div> : null}
                    </div>
                  </li>
                ))}
              </ol>
            )}
          </Card>
        </div>

        <div className="flex flex-col gap-4">
          <Card className="flex flex-col gap-3">
            <h3 className="text-[15px] font-semibold">Policy outcome</h3>
            <div className="num text-[20px] font-semibold">{o.policy?.outcome ?? o.status}</div>
            {o.policy?.hits.length ? (
              <ul className="flex flex-col gap-2 text-[13px]">
                {o.policy.hits.map((h) => (
                  <li key={h.rule} className="rounded-[var(--radius-tile)] bg-warn-soft p-2.5">
                    <strong className="text-warn">{h.rule}</strong>
                    <div className="text-ink-muted">{h.detail}</div>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="label">No rule matched — inside the agent's own authority.</p>
            )}
            <Link to="/policy" className="btn-ghost">
              See the rules
            </Link>
          </Card>

          <Card className="flex flex-col gap-2 text-[12px] text-ink-muted">
            <h3 className="text-[15px] font-semibold text-ink">References</h3>
            <div>quote {o.quote_id}</div>
            <div>idempotency {o.idempotency_key}</div>
            <div>expires {new Date(o.expires_at).toLocaleString("en-GB")}</div>
          </Card>
        </div>
      </div>
    </div>
  );
}
