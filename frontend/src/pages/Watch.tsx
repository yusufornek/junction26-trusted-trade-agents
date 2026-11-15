import { useState } from "react";
import { Link } from "react-router-dom";
import { ApiError } from "../api";
import { watchApi, type StockoutAlert, type TradeOff, type WatchRun } from "../watch";
import { Card, Empty, ErrorNote, Spinner, allergenName as allergen, dateOnly, eur, num, ruleLabel, useAsync, when } from "../ui";

const UNITS: Record<string, string> = { ST: "piece", KI: "case", PAK: "pack", KG: "kg", L: "litre", SALES_UNIT: "pack" };
const claim = (code: string) => code.replaceAll("_", " ").toLowerCase();
const unitWord = (code: string) => UNITS[code] ?? "sales unit";
const qtyUnit = (unit: string) => (unit === "SALES_UNIT" ? "packs" : unit);

/** What the alert was found in, from its source key ("recurring:<id>:<date>", "quote:<id>", "basket:<id>"). */
function sourceWords(source: string) {
  const kind = source.split(":")[0];
  if (kind === "recurring") return <>a <Link to="/repeat" className="text-brand underline">recurring order</Link></>;
  if (kind === "quote") return "an open quote";
  if (kind === "basket") return "an open recipe basket";
  return "a planned order";
}

/** The gateway says why it could not verify a substitute; these are the two reasons it gives today. */
const NO_SUBSTITUTE: Record<string, string> = {
  "recorded functional need required":
    "No substitute could be verified: this order does not record what the product is used for in the recipe, and the gateway never guesses that.",
  "no function-compatible in-time product passes the constraints":
    "No substitute could be verified: no product that does the same job can arrive in time and still meet this kitchen's rules.",
};

export default function Watch() {
  const inbox = useAsync(() => watchApi.inbox(), [], 30000);
  const canRun = useAsync(() => watchApi.canRun(), []);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<WatchRun | null>(null);
  const [runError, setRunError] = useState<unknown>(null);
  // A 403 means this identity may not start a check after all: drop the button rather than offer it again.
  const refused = runError instanceof ApiError && runError.code === "OPERATOR_REQUIRED";

  const check = async () => {
    setRunning(true);
    setRunError(null);
    try {
      setResult(await watchApi.run());
      await inbox.reload();
    } catch (e) {
      setRunError(e);
    } finally {
      setRunning(false);
    }
  };

  const alerts = inbox.data?.alerts ?? [];
  const name = (id: string) => inbox.data?.names[id] ?? `Product ${id}`;

  return (
    <div className="flex flex-col gap-8">
      <section className="flex flex-col gap-3">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div className="min-w-0">
            <h2 className="text-[16px] font-semibold">Stock-out watch</h2>
            <p className="label">
              Every minute the gateway checks the next seven days of recurring orders, open quotes and recipe baskets against
              Valio's stock. Nothing is changed or ordered by the watch.
            </p>
          </div>
          {canRun.data && !refused ? (
            <button className="btn-dark" disabled={running} onClick={check}>
              {running ? "Checking stock…" : "Check now"}
            </button>
          ) : null}
        </div>

        {result ? <RunNote result={result} /> : null}
        {runError && !refused ? <ErrorNote error={runError} /> : null}

        {inbox.error ? (
          <ErrorNote error={inbox.error} />
        ) : inbox.loading ? (
          <Spinner />
        ) : !alerts.length ? (
          <Card>
            <Empty>No stock-outs expected in the next seven days for the kitchens you can see.</Empty>
          </Card>
        ) : (
          alerts.map((a) => <AlertCard key={a.id} a={a} name={name} />)
        )}
      </section>

      <section className="flex flex-col gap-3">
        <div>
          <h2 className="text-[16px] font-semibold">New products from Valio</h2>
          <p className="label">Valio's own sales agent, working inside the buyer's consent and quotas.</p>
        </div>
        <Card className="flex flex-col gap-2 text-[13px]">
          <p>
            Valio's sales agent suggests products a kitchen has never bought, in categories it already buys, and only ones
            that pass the kitchen's allergen, diet and nutrition rules. A kitchen gets at most a few suggestions a week, and
            only if it has opted in.
          </p>
          <p>
            The suggestions go to the kitchen's purchasing agent, which can turn one into a normal quote and order under its
            usual limits and approvals. Valio only sees how many kitchens received a campaign and how many accepted, never
            their purchase history.
          </p>
          <p className="label">
            Campaigns are sent by Valio's sales agent with its own supplier key, and the suggestions are read by the
            kitchen's agent, so neither appears in this console.
          </p>
        </Card>
      </section>
    </div>
  );
}

function RunNote({ result }: { result: WatchRun }) {
  if (result.busy)
    return <p className="rounded-[var(--radius-tile)] bg-page p-3 text-[13px]">A check is already running. Try again in a moment.</p>;
  const failed = result.errors ?? [];
  return (
    <p className="rounded-[var(--radius-tile)] bg-positive-soft p-3 text-[13px] text-positive">
      Checked {num(result.checked)} planned {result.checked === 1 ? "delivery" : "deliveries"}:{" "}
      {result.alerts.length
        ? `${num(result.alerts.length)} new ${result.alerts.length === 1 ? "stock-out" : "stock-outs"} found.`
        : "nothing new to report."}
      {failed.length ? (
        <span className="mt-1 block" title={failed.map((e) => e.code).join(", ")}>
          {num(failed.length)} could not be checked ({[...new Set(failed.map((e) => ruleLabel(e.code).toLowerCase()))].join(", ")}).
        </span>
      ) : null}
    </p>
  );
}

function AlertCard({ a, name }: { a: StockoutAlert; name: (id: string) => string }) {
  const sub = a.substitute;
  const reason = a.substitute_unavailable_reason;
  const others = a.alternatives.length - 1;

  return (
    <Card className="flex flex-col gap-3">
      <div className="min-w-0">
        <h3 className="break-words text-[16px] font-semibold leading-snug">
          {name(a.product_id)} will not be available for the delivery on {dateOnly(a.order_date)}.
        </h3>
        <p className="label mt-0.5">
          Found in {sourceWords(a.source)} for customer {a.customer_id}, bought by {a.agent_id} · noticed {when(a.predicted_at)}
        </p>
      </div>

      {sub ? (
        <div className="flex flex-col gap-2 rounded-[var(--radius-tile)] bg-page p-3 text-[13px]">
          <p className="min-w-0 break-words font-medium">Use {name(sub.product_id)} instead:</p>
          <TradeOffList t={sub.trade_off} />
          {others > 0 ? (
            <p className="text-ink-muted">
              {num(others)} other {others === 1 ? "product does" : "products do"} the same job and also pass.
            </p>
          ) : null}
        </div>
      ) : (
        <p className="rounded-[var(--radius-tile)] bg-warn-soft p-3 text-[13px] text-warn">
          {reason ? NO_SUBSTITUTE[reason] ?? `No substitute could be verified: ${reason}.` : "No substitute could be verified."}
        </p>
      )}

      <p className="label border-t border-line pt-3">
        Substitutes always need approval. The buyer's agent prepares a new quote; a person decides on the Approvals page.
      </p>
    </Card>
  );
}

function TradeOffList({ t }: { t: TradeOff }) {
  const diff = t.price_difference_per_required_quantity;
  const need = `${num(t.required_quantity.value, 1)} ${qtyUnit(t.required_quantity.unit)}`;
  const { pack_change: pack, unit_change: unit, allergen_differences: al, claim_differences: cl, delivery_date_difference: d } = t;
  const packText =
    pack.from === pack.to && unit.from === unit.to
      ? "Same pack size."
      : `Pack changes from ${num(pack.from, 2)} ${qtyUnit(t.required_quantity.unit)} per ${unitWord(unit.from)} to ${num(pack.to, 2)} per ${unitWord(unit.to)}.`;
  const allergenText = [
    al.added.length ? `Also contains ${al.added.map(allergen).join(", ")}.` : "",
    al.removed.length ? `No longer contains ${al.removed.map(allergen).join(", ")}.` : "",
  ].join(" ").trim() || "Same allergens.";
  const claimText = [
    cl.added.length ? `Adds ${cl.added.map(claim).join(", ")}.` : "",
    cl.removed.length ? `Loses ${cl.removed.map(claim).join(", ")}.` : "",
  ].join(" ").trim() || "Same claims.";
  const deliveryText =
    d.days == null
      ? d.to ? `Arrives ${dateOnly(d.to)}.` : "Delivery date not confirmed."
      : d.days === 0
        ? "Same delivery date."
        : `Arrives ${dateOnly(d.to)}, ${num(Math.abs(d.days))} ${Math.abs(d.days) === 1 ? "day" : "days"} ${d.days < 0 ? "earlier" : "later"}.`;
  const rows: [string, string][] = [
    [
      "Price",
      diff == null
        ? `No price comparison for ${need}.`
        : diff === 0
          ? `Same cost for ${need}.`
          : `${eur(Math.abs(diff), t.currency)} ${diff > 0 ? "more" : "less"} for ${need}.`,
    ],
    ["Pack", packText],
    ["Allergens", allergenText],
    ["Claims", claimText],
    ["Delivery", deliveryText],
  ];

  return (
    <>
      <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1">
        {rows.map(([k, v]) => (
          <div key={k} className="contents">
            <dt className="text-ink-muted">{k}</dt>
            <dd className="min-w-0 break-words">{v}</dd>
          </div>
        ))}
      </dl>
      <p className="min-w-0 break-words text-ink-muted">{t.explanation}</p>
    </>
  );
}
