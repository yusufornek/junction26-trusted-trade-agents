import { useState } from "react";
import { Link } from "react-router-dom";
import { Anchor, BadgeCheck, CircleAlert, Copy, KeyRound, Link2, Plug, ShieldCheck } from "lucide-react";
import { api, type AuditVerification } from "../api";
import { Card, Empty, ErrorNote, Spinner, useAsync, when } from "../ui";

// Exact tool names from backend/app/mcp_server.py — the console only lists, it never calls them.
const TOOLS: [string, string][] = [
  ["search_products", "catalog search with hard filters, no live calls"],
  ["find_offers", "ranked live offers and fulfilment plans for a structured need"],
  ["create_quote", "lock fixed prices for lines or a stored plan (signed)"],
  ["place_order", "accept a quote; the policy confirms, holds or rejects"],
  ["get_order_status", "current state, warnings and rule hits"],
  ["get_order_ubl", "the order as UBL 2.1 XML"],
  ["cancel_order", "cancel a confirmed order before the cut-off"],
];

const CHAINS: Record<number, string> = { 137: "Polygon", 80002: "Polygon Amoy (testnet)", 1: "Ethereum" };

const short = (h: string, n = 10) => (h.length > n * 2 + 1 ? `${h.slice(0, n)}…${h.slice(-6)}` : h);

export default function Trust() {
  const head = useAsync(() => api.auditHead(), [], 15000);
  const anchors = useAsync(() => api.auditAnchors(), [], 30000);
  const jwks = useAsync(() => api.jwks(), []);
  const meta = useAsync(() => api.meta(), []);
  const me = useAsync(() => api.me(), []);
  const mcp = useAsync(() => api.mcpExposed(), []);
  const suppliers = useAsync(() => api.suppliers(), []);
  const [verification, setVerification] = useState<AuditVerification | null>(null);
  const [verifying, setVerifying] = useState(false);
  const [verifyError, setVerifyError] = useState<unknown>(null);
  const origin = window.location.origin;

  const verify = async () => {
    setVerifying(true);
    setVerifyError(null);
    try {
      setVerification(await api.auditVerify());
    } catch (e) {
      setVerifyError(e);
    } finally {
      setVerifying(false);
    }
  };

  const specs = (meta.data?.specifications ?? {}) as Record<string, unknown>;
  const packages = (meta.data?.packages ?? {}) as Record<string, string>;

  return (
    <div className="flex flex-col gap-4">
      {/* grid-cols-1, not the implicit auto column: that one grows to the nowrap hashes and widens the page on phones. */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card className="flex flex-col gap-4">
          <div className="flex items-start gap-2">
            <ShieldCheck size={16} className="mt-0.5 shrink-0" />
            <div>
              <h3 className="text-[14px] font-semibold">Audit chain</h3>
              <p className="label">Every entry carries the hash of the one before — editing history breaks the chain.</p>
            </div>
          </div>
          {head.error ? (
            <ErrorNote error={head.error} />
          ) : !head.data ? (
            <Spinner />
          ) : (
            <div className="grid grid-cols-2 gap-2">
              <Fact label="Entries" value={head.data.entries.toLocaleString("en-GB")} />
              <Fact label="Signed" value={when(head.data.at)} />
              <div className="col-span-2">
                <Fact label="Head hash" value={short(head.data.head_hash, 16)} mono copy={head.data.head_hash} />
              </div>
            </div>
          )}
          <div className="flex flex-wrap items-center gap-3">
            <button className="btn-dark" onClick={verify} disabled={verifying}>
              <BadgeCheck size={14} /> {verifying ? "Verifying…" : "Verify the whole chain"}
            </button>
            {verification ? (
              verification.valid ? (
                <span className="chip bg-positive-soft text-positive">intact · {verification.entries} entries</span>
              ) : (
                <span className="chip bg-negative-soft text-negative">
                  broken{verification.first_break != null ? ` at entry ${verification.first_break}` : ""}
                </span>
              )
            ) : null}
          </div>
          {verification ? (
            <ul className="flex flex-col gap-1.5 text-[12px]">
              <Check ok={verification.valid} label="Hash chain recomputed from the first entry" />
              {verification.checkpoint_match != null ? (
                <Check ok={verification.checkpoint_match} label="Matches the last signed checkpoint" />
              ) : null}
              {verification.anchor_match != null ? (
                <Check
                  ok={verification.anchor_match}
                  label={`Matches the public anchor${verification.anchor_block_time ? ` (block time ${when(verification.anchor_block_time)})` : ""}`}
                />
              ) : null}
            </ul>
          ) : null}
          {verifyError ? <ErrorNote error={verifyError} /> : null}
        </Card>

        <Card className="flex flex-col gap-4">
          <div className="flex items-start gap-2">
            <Anchor size={16} className="mt-0.5 shrink-0" />
            <div>
              <h3 className="text-[14px] font-semibold">Public anchors</h3>
              <p className="label">The chain head is written to a public blockchain, so even the operator cannot rewrite the past.</p>
            </div>
          </div>
          {anchors.error ? (
            <ErrorNote error={anchors.error} />
          ) : !anchors.data ? (
            <Spinner />
          ) : !anchors.data.length ? (
            <Empty>Not anchored yet — anchoring runs every few entries when a chain is configured.</Empty>
          ) : (
            <ul className="flex flex-col gap-2">
              {[...anchors.data].reverse().map((a) => (
                // Facts on one row, the full anchored hash under it: it wraps instead of losing its tail.
                <li key={a.head_hash + a.at} className="flex flex-col gap-1.5 rounded-[var(--radius-tile)] border border-line p-3">
                  <div className="flex items-center gap-3">
                    <span
                      className={`chip ${
                        a.status === "confirmed" ? "bg-positive-soft text-positive" : a.status === "failed" ? "bg-negative-soft text-negative" : "bg-warn-soft text-warn"
                      }`}
                    >
                      {a.status}
                    </span>
                    <div className="min-w-0 flex-1 text-[13px]">
                      {a.entries} entries · {CHAINS[a.chain_id] ?? `chain ${a.chain_id}`}
                      {a.block_number ? ` · block ${a.block_number.toLocaleString("en-GB")}` : ""}
                    </div>
                    <span className="shrink-0 text-[11px] text-ink-soft">{when(a.at)}</span>
                    {a.explorer_url ? (
                      <a
                        href={a.explorer_url}
                        target="_blank"
                        rel="noreferrer"
                        className="btn-ghost !size-9 !p-0"
                        title="Open in block explorer"
                        aria-label="Open in block explorer"
                      >
                        <Link2 size={13} />
                      </a>
                    ) : null}
                  </div>
                  <code className="text-[11px] text-ink-muted break-all">{a.head_hash}</code>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      <Card className="flex flex-col gap-4">
        <div className="flex items-start gap-2">
          <KeyRound size={16} className="mt-0.5 shrink-0" />
          <div>
            <h3 className="text-[14px] font-semibold">Signing keys</h3>
            <p className="label">
              Quotes and receipts are signed with these Ed25519 keys. An agent checks a quote offline against them — no
              call back to us needed.
            </p>
          </div>
        </div>
        <Fact label="Published at" value={`${origin}/.well-known/jwks.json`} mono copy={`${origin}/.well-known/jwks.json`} />
        {jwks.error ? (
          <ErrorNote error={jwks.error} />
        ) : !jwks.data ? (
          <Spinner />
        ) : (
          <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
            {jwks.data.keys.map((k, i) => (
              <div key={k.kid ?? i} className="flex flex-col gap-1 rounded-[var(--radius-tile)] border border-line p-3 text-[12px]">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-[13px] font-medium wrap-anywhere">{k.kid ?? "unnamed key"}</span>
                  <span className="chip bg-chip text-ink-muted">{[k.kty, k.crv, k.alg].filter(Boolean).join(" · ")}</span>
                </div>
                {k.x ? <code className="text-ink-muted break-all">x = {k.x}</code> : null}
              </div>
            ))}
          </div>
        )}
        <div className="rounded-[var(--radius-tile)] bg-page p-3 text-[12px] text-ink-muted">
          Offline check of a saved quote:{" "}
          <code className="text-ink wrap-anywhere">scripts/verify_quote.py quote.json --jwks jwks.json --customer 1</code>
        </div>
      </Card>

      <Card className="flex flex-col gap-4">
        <div className="flex flex-wrap items-start gap-x-2 gap-y-2">
          <Plug size={16} className="mt-0.5 shrink-0" />
          <div className="min-w-0 flex-1">
            <h3 className="text-[14px] font-semibold">Connect an outside agent</h3>
            <p className="label">
              The same core is open three ways. Each agent uses its own key and is held to its own mandate.
            </p>
          </div>
          <Link to="/connect" className="text-[13px] font-medium text-brand hover:underline">
            Set up a client
          </Link>
        </div>

        <div className="grid gap-3 lg:grid-cols-3">
          {/* The console itself runs on this REST surface, so if this page loaded, REST is live. */}
          <Door
            title="REST"
            status={{ text: "live here", style: "bg-positive-soft text-positive" }}
            body="OpenAPI 3.1, the contract the console itself uses. Each agent signs in with its own Bearer key."
            code={`${origin}/v1`}
          />
          <Door
            title="MCP · stdio"
            status={{ text: "local", style: "bg-chip text-ink-muted" }}
            body="For desktop agents. The key for --agent-id must be in AGENT_KEYS; --mandate takes a buyer-issued VC-JWT file."
            code="python -m app.mcp_server --agent-id hotel-chain-agent"
          />
          {/* Never advertise an address that answers 404: the door follows what this server really serves. */}
          {mcp.data ? (
            <Door
              title="MCP · streamable HTTP"
              status={{ text: "live here", style: "bg-positive-soft text-positive" }}
              body="Bearer key per agent; a buyer mandate travels in the Agent-Mandate header."
              code={`${origin}/mcp`}
            />
          ) : (
            <Door
              title="MCP · streamable HTTP"
              status={
                mcp.loading
                  ? { text: "checking…", style: "bg-chip text-ink-muted" }
                  : { text: "not exposed here", style: "bg-warn-soft text-warn" }
              }
              body="Not served on this address yet, so outside agents use REST here. It runs beside the gateway on a host with the repository."
              code="python -m app.mcp_server --http --port 8001"
            />
          )}
        </div>

        <div>
          <div className="label mb-2">Tools an agent gets</div>
          {/* Subgrid rows: every description starts on one shared edge instead of after its own name. */}
          <ul className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-6 gap-y-1.5 md:grid-cols-[max-content_minmax(0,1fr)_max-content_minmax(0,1fr)]">
            {TOOLS.map(([name, what]) => (
              <li key={name} className="col-span-2 grid grid-cols-subgrid gap-x-2 text-[12px]">
                <code className="shrink-0 font-medium text-ink">{name}</code>
                <span className="text-ink-muted">{what}</span>
              </li>
            ))}
          </ul>
        </div>

        {me.data ? (
          <div className="flex flex-wrap items-center gap-2 border-t border-line pt-3 text-[12px] text-ink-muted">
            <span className="label">You are signed in as</span>
            <span className="chip bg-chip text-ink">{me.data.principal.id}</span>
            {me.data.principal.role ? <span className="chip bg-chip text-ink-muted">{me.data.principal.role.replaceAll("_", " ")}</span> : null}
            <span>customers {me.data.customer_ids.join(", ") || "—"}</span>
            <span>· environment {me.data.environment}</span>
          </div>
        ) : null}
      </Card>

      <Card className="flex flex-col gap-4">
        <div>
          <h3 className="text-[14px] font-semibold">Open standards</h3>
          <p className="label">What this gateway speaks, as it reports itself on /v1/meta — nothing proprietary in the path.</p>
        </div>
        {meta.error ? (
          <ErrorNote error={meta.error} />
        ) : !meta.data ? (
          <Spinner />
        ) : (
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-3">
            {Object.entries(specs).map(([name, value]) => {
              const v = value as string | { version?: string; status?: string; commit?: string; draft_commit?: string };
              const text = typeof v === "string" ? v : v.version ?? (v.status?.split(";")[0] ?? "");
              const planned = typeof v === "object" && /planned/.test(v.status ?? "");
              return (
                // The name keeps its line; a long version string wraps at the right instead of squeezing the name.
                <div key={name} className="flex items-center justify-between gap-3 rounded-[var(--radius-tile)] border border-line px-3 py-2.5">
                  <span className="shrink-0 text-[13px] font-medium">{name.replaceAll("_", " ")}</span>
                  <span className="flex min-w-0 items-center gap-1.5">
                    <span className="text-right text-[12px] text-ink-muted wrap-anywhere">{text}</span>
                    {planned ? <span className="chip bg-warn-soft text-warn">planned</span> : null}
                  </span>
                </div>
              );
            })}
          </div>
        )}
        {Object.keys(packages).length ? (
          <div className="text-[11px] text-ink-soft">
            Built on {Object.entries(packages).map(([k, v]) => `${k} ${v}`).join(" · ")}
            {meta.data?.limiter_scope ? ` · rate limiter ${String(meta.data.limiter_scope)}` : ""}
          </div>
        ) : null}
      </Card>
      <Card className="flex flex-col gap-4">
        <div>
          <h2 className="text-[15px] font-semibold">Suppliers behind the gateway</h2>
          <p className="label">The same rules and audit trail apply to each. A second supplier shows the gateway is not tied to one catalogue.</p>
        </div>
        {suppliers.error ? (
          <ErrorNote error={suppliers.error} />
        ) : !suppliers.data ? (
          <Spinner />
        ) : (
          <ul className="flex flex-col divide-y divide-line text-[13px]">
            {suppliers.data.map((x) => (
              <li key={x.supplier_id} className="flex flex-wrap items-center gap-x-4 gap-y-1 py-2.5">
                <span className="font-medium capitalize">{x.supplier_id}</span>
                {x.synthetic ? <span className="chip bg-chip text-ink-muted">synthetic test supplier</span> : null}
                <span className="text-ink-muted">prices {x.capabilities.pricing.replaceAll("-", " ")}</span>
                <span className="text-ink-muted">{x.capabilities.currencies.join(", ")}</span>
                <span className="text-ink-muted">
                  orders {x.capabilities.execution.mode === "simulated" ? "simulated by the gateway" : "sent to the supplier"}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}

function Fact({ label, value, mono, copy }: { label: string; value: string; mono?: boolean; copy?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="flex items-center gap-2 rounded-[var(--radius-tile)] border border-line px-3 py-2">
      <div className="min-w-0 flex-1">
        <div className="text-[11px] text-ink-muted">{label}</div>
        {/* Codes (hashes, URLs) wrap so all of them stays readable; short values stay on one line. */}
        <div className={mono ? "font-mono text-[12px] break-all" : "num truncate text-[13px] font-medium"} title={copy ?? value}>
          {value}
        </div>
      </div>
      {copy ? (
        <button
          className="grid size-9 shrink-0 place-items-center rounded-full text-ink-muted hover:bg-page"
          title="Copy"
          aria-label={`Copy ${label}`}
          onClick={async () => {
            try {
              await navigator.clipboard.writeText(copy);
              setCopied(true);
              setTimeout(() => setCopied(false), 1500);
            } catch {
              // Clipboard can be blocked outside a secure context; the value stays visible to select.
            }
          }}
        >
          {copied ? <BadgeCheck size={13} className="text-positive" /> : <Copy size={13} />}
        </button>
      ) : null}
    </div>
  );
}

function Check({ ok, label }: { ok: boolean; label: string }) {
  return (
    <li className={`flex items-start gap-2 ${ok ? "text-ink" : "text-negative"}`}>
      {ok ? <BadgeCheck size={14} className="mt-0.5 shrink-0 text-positive" /> : <CircleAlert size={14} className="mt-0.5 shrink-0" />}
      {label}
    </li>
  );
}

function Door({
  title,
  body,
  code,
  status,
}: {
  title: string;
  body: string;
  code: string;
  status?: { text: string; style: string };
}) {
  return (
    <div className="flex flex-col gap-2 rounded-[var(--radius-tile)] border border-line p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-[13px] font-semibold">{title}</span>
        {status ? <span className={`chip ${status.style}`}>{status.text}</span> : null}
      </div>
      <p className="text-[12px] text-ink-muted">{body}</p>
      <code className="mt-auto break-all rounded-[10px] bg-page px-2.5 py-2 text-[11px] text-ink">{code}</code>
    </div>
  );
}
