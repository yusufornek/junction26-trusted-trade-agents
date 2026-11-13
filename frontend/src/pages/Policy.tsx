// Read from the file the gateway actually loads — a copy here would drift within hours.
import policySource from "../../../backend/policy.yaml?raw";
import { Card } from "../ui";

const RULES = [
  {
    title: "Spend ceiling",
    body: "Any order above the auto-total goes to a human, however good the offer looks.",
    key: "max_auto_total_eur",
  },
  {
    title: "Confidence floor",
    body: "A fuzzy fit judgement below the threshold is treated as not good enough to act alone on.",
    key: "min_fit_probability",
  },
  {
    title: "Delivery risk",
    body: "Partial stock, a silently shifted date or a passed cut-off are never accepted silently.",
    key: "warnings_need_approval",
  },
  {
    title: "Hard stops",
    body: "A blocked product, an unknown code or a violated hard constraint is rejected, and no human can wave it through.",
    key: "reject",
  },
];

export default function Policy() {
  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_1.1fr]">
      <div className="flex flex-col gap-4">
        <Card className="flex flex-col gap-2">
          <h2 className="text-[15px] font-semibold">The buyer owns these rules</h2>
          <p className="text-[13px] text-ink-muted">
            Guardrails are not compiled into the agent. They live in one file the customer can read, diff and
            change; the gateway evaluates every rule on each order and records which ones matched.
          </p>
        </Card>

        {RULES.map((r) => (
          <Card key={r.key} className="flex flex-col gap-1.5">
            <div className="flex items-center gap-2">
              <h3 className="text-[14px] font-semibold">{r.title}</h3>
              <code className="chip bg-chip text-ink-muted">{r.key}</code>
            </div>
            <p className="text-[13px] text-ink-muted">{r.body}</p>
          </Card>
        ))}
      </div>

      <Card className="flex flex-col gap-3">
        <div>
          <h2 className="text-[15px] font-semibold">backend/policy.yaml</h2>
          <p className="label">The live file, loaded at startup</p>
        </div>
        <pre className="overflow-auto rounded-[var(--radius-tile)] bg-page p-4 text-[12px] leading-relaxed">
          {policySource}
        </pre>
      </Card>
    </div>
  );
}
