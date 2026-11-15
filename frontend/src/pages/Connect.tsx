import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Activity, Check, CircleDashed, Copy, ExternalLink, KeyRound, Plug, ShieldAlert, Trash2 } from "lucide-react";
import { ApiError, api, call, type ClientKind, type Connection, type NewConnection } from "../api";
import { Card, ErrorNote, Spinner, ruleLabel, useAsync, when } from "../ui";

/** GET /v1/connect/clients/{client} (spec 60 R-7): the gateway's own setup for the live address. */
type ClientSetup = { steps: string[]; snippet: string; secret_placeholder: string; url: string; auth: { method: "oauth" | "bearer" } };
/** GET /v1/connect/connections/{id}/health (spec 60 R-8). */
type ConnectionHealth = {
  last_used_at: string | null;
  calls_last_24h: Record<string, number>;
  error_codes: string[];
  scope: { audience: string; scopes: string[]; customer_ids: string[]; tenant_id: string | null; all_tenants: boolean };
};
type ConnectionTest = ConnectionHealth & { successful_read: boolean; result: { message?: string } | null; can_do: string[] };

/** Clients the gateway has a ready setup for, its id for each and where the configuration goes.
 *  claude.ai and REST keep the hand-written steps. */
const SETUP: Partial<Record<Client["id"], { id: string; file: string }>> = {
  claude_desktop: { id: "claude-desktop", file: "claude_desktop_config.json" },
  claude_code: { id: "claude-code", file: ".mcp.json in your project" },
  chatgpt: { id: "chatgpt", file: "Server address" },
  cursor: { id: "cursor", file: ".cursor/mcp.json" },
  vscode: { id: "vscode", file: ".vscode/mcp.json" },
  mcp_other: { id: "generic", file: "Configuration" },
};

type Client = {
  /** claude.ai is not a key client: its sign-ins reach the connection list as "mcp_other". */
  id: ClientKind | "claude_ai";
  name: string;
  /** What the person does in that tool, in its own words. */
  how: string;
  transport: "mcp" | "rest";
  /** Added by address and approved in the gateway's sign-in window (OAuth): no key is created or copied. */
  oauth?: boolean;
};

const CLIENTS: Client[] = [
  { id: "claude_desktop", name: "Claude Desktop", how: "Adds the gateway as a tool server in Claude's desktop app.", transport: "mcp" },
  { id: "claude_ai", name: "claude.ai", how: "Paste the address as a custom connector, then sign in. No key to copy.", transport: "mcp", oauth: true },
  { id: "chatgpt", name: "ChatGPT", how: "Paste the address as a connector, then sign in. No key to copy.", transport: "mcp", oauth: true },
  { id: "claude_code", name: "Claude Code", how: "One command in your terminal, or a message Claude Code runs for you.", transport: "mcp" },
  { id: "cursor", name: "Cursor", how: "One click installs the server in Cursor.", transport: "mcp" },
  { id: "vscode", name: "VS Code", how: "One click installs it for Copilot's agent mode.", transport: "mcp" },
  { id: "mcp_other", name: "Other MCP client", how: "Any client that speaks MCP over streamable HTTP.", transport: "mcp" },
  { id: "rest", name: "REST API", how: "Your own code or agent framework, with plain HTTPS calls.", transport: "rest" },
];

const PLACEHOLDER = "<YOUR_AGENT_KEY>";
const SERVER_NAME = "luotsi";
const TRY_PROMPT = "Find 50 litres of lactose-free milk for delivery on Monday and show me the best offer. Do not order yet.";

export default function Connect() {
  const [clientId, setClientId] = useState<Client["id"]>("claude_desktop");
  const client = CLIENTS.find((c) => c.id === clientId)!;
  const mcp = useAsync(() => api.mcpExposed(), []);
  const policy = useAsync(() => api.policy(), []);
  const me = useAsync(() => api.me(), []);
  // Polled: a sign-in approved in ChatGPT or claude.ai appears here by itself, and "used" moves with each call.
  const connections = useAsync(() => api.connections(), [], 5000);
  const setupId = SETUP[clientId]?.id;
  const setup = useAsync(
    () => (setupId ? call<ClientSetup>(`/v1/connect/clients/${setupId}`) : Promise.resolve(null)),
    [setupId],
  );
  // A 404 on the list means the gateway does not issue keys from the console yet; everything else still works.
  const issuing = !(connections.error instanceof ApiError && connections.error.status === 404);

  const [agentId, setAgentId] = useState("");
  const [customerId, setCustomerId] = useState("");
  const [created, setCreated] = useState<NewConnection | null>(null);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<unknown>(null);
  // Calls before this moment belong to earlier sessions, so they must not count as "connected".
  const [startedAt, setStartedAt] = useState(() => new Date().toISOString());

  const customers = useMemo(
    () => [...new Set([...(me.data?.customer_ids ?? []), ...Object.keys(policy.data?.customers ?? {})])],
    [me.data, policy.data],
  );
  useEffect(() => {
    if (!customerId && customers.length) setCustomerId(customers[0]);
  }, [customers, customerId]);

  const existingAgents = Object.keys(policy.data?.agents ?? {});
  const agentIsNew = agentId.trim() !== "" && !existingAgents.includes(agentId.trim());
  const origin = window.location.origin;
  const key = created?.key ?? PLACEHOLDER;
  const mcpUrl = created?.mcp_url ?? `${origin}/mcp`;
  const restBase = created?.rest_base_url ?? `${origin}/v1`;
  const validAgentId = /^[a-z0-9][a-z0-9-]{1,47}$/.test(agentId.trim());

  const create = async () => {
    setCreating(true);
    setCreateError(null);
    try {
      const c = await api.createConnection({
        agent_id: agentId.trim(),
        client: client.id === "claude_ai" ? "mcp_other" : client.id,
        customer_ids: customerId ? [customerId] : [],
      });
      setCreated(c);
      setStartedAt(c.created_at);
      connections.reload();
      policy.reload();
    } catch (e) {
      setCreateError(e);
    } finally {
      setCreating(false);
    }
  };

  return (
    <div className="flex flex-col gap-4">
      <Card className="flex flex-col gap-4">
        <div>
          <h2 className="text-[17px] font-semibold">Where does your agent work?</h2>
          <p className="label mt-0.5">The gateway is the same for every tool. Only the way you plug it in changes.</p>
        </div>
        <div role="group" aria-label="Client" className="flex flex-wrap gap-2">
          {CLIENTS.map((c) => (
            <button
              key={c.id}
              aria-pressed={c.id === clientId}
              onClick={() => setClientId(c.id)}
              className={`flex h-10 items-center gap-2 rounded-full border px-4 text-[14px] font-medium transition-colors ${
                c.id === clientId
                  ? "border-brand bg-brand text-white"
                  : "border-line bg-surface text-ink hover:border-brand/30 hover:bg-brand-soft"
              }`}
            >
              {c.name}
            </button>
          ))}
        </div>
        <TransportStatus client={client} mcpExposed={mcp.data} checking={mcp.loading} />
      </Card>

      {/* Sign-in clients get no key: the operator picks the agent in the gateway's window. Hidden, not unmounted,
          so a name typed here survives a look at another client. */}
      <Card className={`flex flex-col gap-4 ${client.oauth ? "hidden" : ""}`}>
        <StepTitle n={1} title="Who it buys for" text="The key belongs to one agent and the customers it may buy for." />
        <div className="grid grid-cols-1 gap-3 md:grid-cols-[1fr_220px]">
          <label className="flex flex-col gap-1.5">
            <span className="label">Agent name</span>
            <input
              className="field"
              list="known-agents"
              placeholder="central-kitchen-agent"
              value={agentId}
              onChange={(e) => setAgentId(e.target.value.toLowerCase())}
              // The snippets and the first-call check follow this name, so it is fixed once its key exists.
              disabled={!!created}
            />
            <datalist id="known-agents">
              {existingAgents.map((a) => (
                <option key={a} value={a} />
              ))}
            </datalist>
            {created ? null : (
              <span className="text-[12px] text-ink-muted">
                {agentId.trim() === ""
                  ? "Lowercase letters, digits and hyphens. Pick an existing agent or name a new one."
                  : !validAgentId
                    ? "Use 2–48 lowercase letters, digits or hyphens."
                    : agentIsNew
                      ? "A new agent. It starts with approval first: a person signs each order until its record earns autonomy."
                      : "An existing agent. Its mandate and autonomy level stay as they are."}
              </span>
            )}
          </label>
          <label className="flex flex-col gap-1.5">
            <span className="label">Buys for customer</span>
            <select className="field" value={customerId} onChange={(e) => setCustomerId(e.target.value)} disabled={!!created}>
              {customers.length ? customers.map((c) => <option key={c} value={c}>Customer {c}</option>) : <option value="">No customer</option>}
            </select>
          </label>
        </div>

        {created ? (
          <KeyReveal created={created} />
        ) : issuing ? (
          <div className="flex flex-wrap items-center gap-3">
            <button className="btn-dark" onClick={create} disabled={creating || !validAgentId}>
              <KeyRound size={15} /> {creating ? "Creating key…" : "Create key"}
            </button>
            <span className="text-[12px] text-ink-muted">Shown once. The gateway keeps only a fingerprint of it.</span>
          </div>
        ) : (
          <div className="rounded-[var(--radius-tile)] bg-warn-soft p-3 text-[13px] text-warn">
            This gateway does not issue keys from the console yet. Ask the operator to add <strong>{agentId.trim() || "the agent"}</strong> to{" "}
            <code>AGENT_KEYS</code> with <code>scripts/gen_keys.py</code>. The steps below show <code>{PLACEHOLDER}</code> where the key goes.
          </div>
        )}
        {createError ? (
          createError instanceof ApiError && createError.status === 404 ? (
            <div className="rounded-[var(--radius-tile)] bg-warn-soft p-3 text-[13px] text-warn">
              Key issuing is not live on this gateway yet. The steps below still work with a key from the operator.
            </div>
          ) : (
            <ErrorNote error={createError} />
          )
        ) : null}
      </Card>

      <Card className="flex flex-col gap-4">
        <StepTitle
          n={client.oauth ? 1 : 2}
          title={client.oauth ? `Add Luotsi to ${client.name}` : `Connect ${client.name}`}
          text={client.how}
        />
        {setupId && setup.loading ? (
          <Spinner label="Loading setup" />
        ) : setup.data && !setup.error ? (
          <GatewaySetup client={client} setup={setup.data} agentKey={created?.key} mcpLive={!!mcp.data} />
        ) : (
          // The gateway's setup could not be read: the hand-written steps still work.
          <ClientSteps client={client} mcpUrl={mcpUrl} restBase={restBase} agentKey={key} hasKey={!!created} mcpLive={!!mcp.data} />
        )}
      </Card>

      {client.oauth ? (
        <Card className="flex flex-col gap-4">
          <StepTitle n={2} title="Approve the sign-in" text={`${client.name} opens a Luotsi window. Whoever runs the gateway approves it there.`} />
          <Steps
            items={[
              `Sign in with the operator token. It stays on the gateway; ${client.name} never sees it.`,
              "Pick an existing agent or name a new one, and the customers it may buy for. A new agent starts with approval first: a person signs each order.",
              `Approve. The connection appears under Connections below, where revoking it signs ${client.name} out.`,
            ]}
          />
        </Card>
      ) : null}

      <Card className="flex flex-col gap-4">
        <StepTitle
          n={3}
          title="Check it works"
          text={
            client.oauth
              ? "Ask for an offer. Its first call shows as “used” on the new connection below."
              : "Ask your agent for an offer. Its first call to the gateway shows up here."
          }
        />
        <CopyBlock label="Try this in your agent" value={TRY_PROMPT} wrap />
        {client.oauth ? null : <FirstCall agentId={validAgentId ? agentId.trim() : ""} since={startedAt} onSeen={connections.reload} />}
      </Card>

      {issuing ? <ConnectionList connections={connections} /> : null}
    </div>
  );
}

function StepTitle({ n, title, text }: { n: number; title: string; text: string }) {
  return (
    <div className="flex items-start gap-3">
      <span className="grid size-8 shrink-0 place-items-center rounded-full bg-brand-soft text-[14px] font-semibold text-brand">{n}</span>
      <div className="min-w-0">
        <h3 className="text-[15px] font-semibold">{title}</h3>
        <p className="label mt-0.5">{text}</p>
      </div>
    </div>
  );
}

function TransportStatus({ client, mcpExposed, checking }: { client: Client; mcpExposed: boolean | null; checking: boolean }) {
  if (client.transport === "rest")
    return <StatusLine tone="ok">The REST API is live on this address.</StatusLine>;
  if (checking) return <StatusLine tone="wait">Checking whether this address serves MCP…</StatusLine>;
  return mcpExposed ? (
    <StatusLine tone="ok">
      MCP is live on this address.{client.oauth ? ` ${client.name} signs in through the gateway's own window (OAuth).` : ""}
    </StatusLine>
  ) : (
    <StatusLine tone="warn">
      This address does not serve MCP yet, so the steps below work once the operator enables it. Outside agents can use the REST API now.
    </StatusLine>
  );
}

function StatusLine({ tone, children }: { tone: "ok" | "warn" | "wait"; children: React.ReactNode }) {
  const style = tone === "ok" ? "bg-positive-soft text-positive" : tone === "warn" ? "bg-warn-soft text-warn" : "bg-page text-ink-muted";
  const Icon = tone === "ok" ? Check : tone === "warn" ? ShieldAlert : CircleDashed;
  return (
    <div className={`flex items-start gap-2 rounded-[var(--radius-tile)] px-3 py-2.5 text-[13px] ${style}`}>
      <Icon size={15} className="mt-0.5 shrink-0" />
      <span>{children}</span>
    </div>
  );
}

function KeyReveal({ created }: { created: NewConnection }) {
  return (
    <div className="flex flex-col gap-2 rounded-[var(--radius-tile)] border border-positive/30 bg-positive-soft p-3">
      <div className="flex items-center gap-2 text-[13px] font-semibold text-positive">
        <Check size={15} /> Key created for {created.agent_id}
      </div>
      {/* Wrapped: a key shown only once should be readable whole on a phone, not half hidden in a scroll box. */}
      <CopyBlock label="Agent key: copy it now, it will not be shown again" value={created.key} wrap />
      <p className="text-[12px] text-ink-muted">
        {created.mandate.autonomy === "autonomous_within_limits" ? (
          "This agent may confirm orders within its limits."
        ) : (
          <>
            Every order from this agent waits for a person's signature until autonomy is granted on{" "}
            <Link to="/agents" className="font-medium text-brand hover:underline">
              Agents
            </Link>
            .
          </>
        )}
      </p>
    </div>
  );
}

function ClientSteps({
  client,
  mcpUrl,
  restBase,
  agentKey,
  hasKey,
  mcpLive,
}: {
  client: Client;
  mcpUrl: string;
  restBase: string;
  agentKey: string;
  hasKey: boolean;
  mcpLive: boolean;
}) {
  const header = `Bearer ${agentKey}`;
  const httpConfig = { type: "http", url: mcpUrl, headers: { Authorization: header } };
  // One-click installs carry the key inside the link, so they only make sense with a real key and a live endpoint.
  const oneClickReady = hasKey && mcpLive;
  const blockedReason = !mcpLive ? "Available once this address serves MCP." : !hasKey ? "Create a key in step 1 first." : "";

  switch (client.id) {
    case "claude_desktop":
      return (
        <>
          <Steps
            items={[
              "Open Claude Desktop, then Settings, Developer, Edit config.",
              "Add the server below to claude_desktop_config.json and save.",
              "Restart Claude Desktop. The Luotsi tools appear under the tools icon.",
            ]}
          />
          <CopyBlock
            label="claude_desktop_config.json"
            value={JSON.stringify(
              {
                mcpServers: {
                  [SERVER_NAME]: {
                    command: "npx",
                    args: ["-y", "mcp-remote", mcpUrl, "--header", "Authorization:${VALIO_AIMO_AUTH}"],
                    env: { VALIO_AIMO_AUTH: header },
                  },
                },
              },
              null,
              2,
            )}
          />
          <p className="text-[12px] text-ink-muted">
            Claude Desktop reaches remote servers through mcp-remote, a small bridge that runs with Node.js. To connect with sign-in
            instead of a key, choose claude.ai above.
          </p>
        </>
      );
    case "claude_code":
      return (
        <>
          <CopyBlock label="Run in your terminal" value={`claude mcp add --transport http ${SERVER_NAME} ${mcpUrl} --header "Authorization: ${header}"`} />
          <CopyBlock
            label="Or send this message to Claude Code"
            wrap
            value={`Connect me to the Luotsi purchasing gateway for Valio Aimo: run \`claude mcp add --transport http ${SERVER_NAME} ${mcpUrl} --header "Authorization: ${header}"\`, then list the ${SERVER_NAME} tools and tell me when you can search offers.`}
          />
        </>
      );
    case "cursor":
      return (
        <>
          <OneClick href={installLink("cursor", mcpUrl, header)} label="Add to Cursor" ready={oneClickReady} reason={blockedReason} />
          <CopyBlock label="Or add it to .cursor/mcp.json" value={JSON.stringify({ mcpServers: { [SERVER_NAME]: { url: mcpUrl, headers: { Authorization: header } } } }, null, 2)} />
        </>
      );
    case "vscode":
      return (
        <>
          <OneClick href={installLink("vscode", mcpUrl, header)} label="Add to VS Code" ready={oneClickReady} reason={blockedReason} />
          <CopyBlock label="Or add it to .vscode/mcp.json" value={JSON.stringify({ servers: { [SERVER_NAME]: httpConfig } }, null, 2)} />
        </>
      );
    case "claude_ai":
      return (
        <>
          <Steps
            items={[
              "In claude.ai, open Settings, then Connectors, and choose Add custom connector.",
              "Name it Luotsi, paste the server address below and leave the advanced fields empty.",
              "Choose Connect. A Luotsi sign-in window opens.",
            ]}
          />
          <CopyBlock label="Server address" value={mcpUrl} />
        </>
      );
    case "chatgpt":
      return (
        <>
          <Steps
            items={[
              "In ChatGPT, turn on developer mode under Settings, Connectors, Advanced.",
              "Create a connector named Luotsi with the server address below and choose OAuth.",
              "Choose Create. A Luotsi sign-in window opens.",
            ]}
          />
          <CopyBlock label="Server address" value={mcpUrl} />
        </>
      );
    case "mcp_other":
      return (
        <>
          <Steps items={["Add a remote server of type streamable HTTP in your client.", "Send the agent key as a Bearer token in the Authorization header."]} />
          <CopyBlock label="Server address" value={mcpUrl} />
          <CopyBlock label="Typical JSON config" value={JSON.stringify({ mcpServers: { [SERVER_NAME]: httpConfig } }, null, 2)} />
        </>
      );
    case "rest":
      return (
        <>
          <Steps
            items={[
              "Send the agent key as a Bearer token on every call.",
              "Find offers, lock a quote, then place the order. The policy decides whether it confirms or waits for a person.",
            ]}
          />
          <CopyBlock
            label="Find offers"
            value={`curl -s ${restBase}/offers \\\n  -H "Authorization: ${header}" \\\n  -H "Content-Type: application/json" \\\n  -d '{"customer_id":"1","intent":"lactose-free milk for a hotel","search_terms":["milk"],"required_claims":["FREE_FROM_LACTOSE"],"quantity":{"value":50,"unit":"L"},"deliver_by":"2026-11-16"}'`}
          />
          <p className="text-[12px] text-ink-muted">
            The full contract is at{" "}
            <a href="/docs" target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 font-medium text-brand hover:underline">
              API reference <ExternalLink size={12} />
            </a>
            .
          </p>
        </>
      );
  }
}

/** Deep links that install the server with the key inside, for the clients that offer one. */
function installLink(client: "cursor" | "vscode", url: string, header: string) {
  return client === "cursor"
    ? `cursor://anysphere.cursor-deeplink/mcp/install?name=${SERVER_NAME}&config=${encodeURIComponent(btoa(JSON.stringify({ url, headers: { Authorization: header } })))}`
    : `vscode:mcp/install?${encodeURIComponent(JSON.stringify({ name: SERVER_NAME, type: "http", url, headers: { Authorization: header } }))}`;
}

function GatewaySetup({ client, setup, agentKey, mcpLive }: { client: Client; setup: ClientSetup; agentKey?: string; mcpLive: boolean }) {
  const oauth = setup.auth.method === "oauth";
  const placeholder = setup.secret_placeholder;
  // The gateway's snippet names the key as a placeholder. Once step 1 made a key it goes straight in, so the
  // copy works as pasted. Claude Desktop keeps its env indirection (mcp-remote reads the header from env).
  const filled = !!agentKey && !oauth;
  let snippet = setup.snippet;
  if (filled) {
    snippet = snippet.replaceAll("<YOUR_CONNECTION_KEY>", agentKey);
    if (client.id !== "claude_desktop") snippet = snippet.replaceAll("${" + placeholder + "}", agentKey);
  }
  // With the key filled in, the gateway's "copy the key / set the variable" steps are already done.
  const steps = filled
    ? ["Your new key is already in the configuration below.", ...setup.steps.filter((s) => !s.includes(placeholder) && !/connection key/i.test(s))]
    : setup.steps;
  const oneClick = client.id === "cursor" || client.id === "vscode" ? client.id : null;
  return (
    <>
      <Steps items={steps} />
      {oneClick ? (
        <OneClick
          href={installLink(oneClick, setup.url, `Bearer ${agentKey}`)}
          label={`Add to ${client.name}`}
          ready={!!agentKey && mcpLive}
          reason={!mcpLive ? "Available once this address serves MCP." : "Create a key in step 1 first."}
        />
      ) : null}
      {oauth ? <CopyBlock label="Server address" value={setup.url} /> : <CopyBlock label={SETUP[client.id]!.file} value={snippet} />}
      <p className="text-[12px] text-ink-muted">Configuration from the gateway for {setup.url}.</p>
    </>
  );
}

function Steps({ items }: { items: string[] }) {
  return (
    <ol className="flex flex-col gap-2">
      {items.map((s, i) => (
        <li key={s} className="flex gap-3 text-[14px]">
          <span className="num w-5 shrink-0 text-right font-semibold text-brand">{i + 1}.</span>
          <span>{s}</span>
        </li>
      ))}
    </ol>
  );
}

function OneClick({ href, label, ready, reason }: { href: string; label: string; ready: boolean; reason: string }) {
  return (
    <div className="flex flex-wrap items-center gap-3">
      {ready ? (
        <a href={href} className="btn-dark">
          <Plug size={15} /> {label}
        </a>
      ) : (
        <button className="btn-dark" disabled>
          <Plug size={15} /> {label}
        </button>
      )}
      {!ready ? <span className="text-[12px] text-ink-muted">{reason}</span> : null}
    </div>
  );
}

function CopyBlock({ label, value, wrap = false }: { label: string; value: string; wrap?: boolean }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard can be blocked outside a secure context; the text stays selectable.
    }
  };
  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      <div className="flex items-center justify-between gap-3">
        <span className="label">{label}</span>
        <button onClick={copy} className="inline-flex h-8 items-center gap-1.5 rounded-full px-3 text-[12px] font-medium text-brand hover:bg-brand-soft">
          {copied ? <Check size={13} /> : <Copy size={13} />} {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <pre
        className={`overflow-x-auto rounded-[var(--radius-tile)] bg-page p-3 font-mono text-[12px] leading-relaxed text-ink ${
          wrap ? "whitespace-pre-wrap break-words" : ""
        }`}
      >
        {value}
      </pre>
    </div>
  );
}

/** Uses the audit trail the gateway already keeps: a call by this agent after you started here proves the key and the client work. */
function FirstCall({ agentId, since, onSeen }: { agentId: string; since: string; onSeen: () => void }) {
  const audit = useAsync(() => (agentId ? api.audit() : Promise.resolve([])), [agentId], 5000);
  // Compare instants, not strings: the gateway and the browser format timestamps differently.
  const first = (audit.data ?? []).find((e) => e.actor === `agent:${agentId}` && Date.parse(e.ts) >= Date.parse(since));
  // The key list shows "last used", so it refreshes the moment the first call lands.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => void (first && onSeen()), [first?.id]);
  if (!agentId) return <p className="text-[13px] text-ink-muted">Name the agent in step 1 to watch for its first call.</p>;
  return first ? (
    <div className="flex items-start gap-2 rounded-[var(--radius-tile)] bg-positive-soft px-3 py-2.5 text-[13px] text-positive">
      <Check size={15} className="mt-0.5 shrink-0" />
      <span>
        Connected. {agentId} reached the gateway {when(first.ts)}. {first.summary ?? first.action.replaceAll("_", " ").toLowerCase()}
      </span>
    </div>
  ) : (
    <div className="flex items-center gap-2 rounded-[var(--radius-tile)] bg-page px-3 py-2.5 text-[13px] text-ink-muted">
      <CircleDashed size={15} className="animate-spin motion-reduce:animate-none" /> Waiting for the first call from {agentId}…
    </div>
  );
}

function ConnectionList({ connections }: { connections: ReturnType<typeof useAsync<Connection[]>> }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [open, setOpen] = useState<string | null>(null);
  const revoke = async (id: string) => {
    setBusy(id);
    setError(null);
    try {
      await api.revokeConnection(id);
      connections.reload();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  };
  const list = (connections.data ?? []).filter((c) => !c.revoked_at);
  return (
    <Card className="flex flex-col gap-3">
      <div>
        <h3 className="text-[15px] font-semibold">Connections</h3>
        <p className="label mt-0.5">
          Revoking stops that client at its next call, sign-ins included. The agent's mandate and history stay.
        </p>
      </div>
      {connections.loading && !connections.data ? (
        <Spinner />
      ) : connections.error ? (
        <ErrorNote error={connections.error} />
      ) : !list.length ? (
        <p className="text-[13px] text-ink-muted">No connections yet.</p>
      ) : (
        <ul className="flex flex-col divide-y divide-line">
          {list.map((c) => (
            <li key={c.connection_id} className="flex flex-wrap items-center gap-x-4 gap-y-1 py-2.5 text-[13px]">
              <span className="font-medium">{c.agent_id}</span>
              <span className="text-ink-muted">{CLIENTS.find((x) => x.id === c.client)?.name ?? c.client}</span>
              {/* An OAuth grant's key_prefix is the word "oauth", not part of any secret. */}
              {c.credential_kind === "oauth" ? (
                <span className="text-ink-muted">signed in with OAuth</span>
              ) : (
                <code className="text-ink-muted">{c.key_prefix}…</code>
              )}
              <span className="text-ink-muted">{c.last_used_at ? `used ${when(c.last_used_at)}` : "not used yet"}</span>
              <span className="ml-auto flex gap-2">
                <button
                  className="btn-ghost h-8 !py-0"
                  aria-expanded={open === c.connection_id}
                  onClick={() => setOpen(open === c.connection_id ? null : c.connection_id)}
                >
                  <Activity size={13} /> Health
                </button>
                <button className="btn-danger h-8 !py-0" onClick={() => revoke(c.connection_id)} disabled={busy === c.connection_id}>
                  <Trash2 size={13} /> {busy === c.connection_id ? "Revoking…" : "Revoke"}
                </button>
              </span>
              {open === c.connection_id ? <HealthPanel id={c.connection_id} /> : null}
            </li>
          ))}
        </ul>
      )}
      {error ? <ErrorNote error={error} /> : null}
    </Card>
  );
}

const AUDIENCE: Record<string, string> = { buyer: "Buying agent", tenant: "Organisation agent", operator: "Operator agent" };

/** Loaded when opened, not on the list's 5 s poll: one connection's use, errors and reach (spec 60 R-8). */
function HealthPanel({ id }: { id: string }) {
  const path = `/v1/connect/connections/${encodeURIComponent(id)}`;
  const health = useAsync(() => call<ConnectionHealth>(`${path}/health`), [path]);
  const [test, setTest] = useState<ConnectionTest | null>(null);
  const [testing, setTesting] = useState(false);
  const [testError, setTestError] = useState<unknown>(null);
  const run = async () => {
    setTesting(true);
    setTestError(null);
    try {
      setTest(await call<ConnectionTest>(`${path}/test`, { method: "POST" }));
      health.reload(); // the test read is itself a call through the connection
    } catch (e) {
      setTestError(e);
    } finally {
      setTesting(false);
    }
  };
  const h = health.data;
  const calls = Object.entries(h?.calls_last_24h ?? {}).sort((a, b) => b[1] - a[1]);
  const s = h?.scope;
  return (
    <div className="flex w-full min-w-0 flex-col gap-3 rounded-[var(--radius-tile)] bg-page p-3">
      {health.loading && !h ? (
        <Spinner label="Loading health" />
      ) : health.error ? (
        <ErrorNote error={health.error} />
      ) : h && s ? (
        <dl className="grid grid-cols-1 gap-x-4 gap-y-1.5 sm:grid-cols-[150px_1fr]">
          <dt className="label">Last used</dt>
          <dd>{h.last_used_at ? when(h.last_used_at) : "Not used yet"}</dd>
          <dt className="label">Calls in the last 24 h</dt>
          <dd className="flex flex-wrap gap-1.5">
            {calls.length
              ? calls.map(([tool, n]) => (
                  <span key={tool} className="chip bg-chip text-ink">
                    {ruleLabel(tool)} · {n}
                  </span>
                ))
              : "None"}
          </dd>
          <dt className="label">Errors seen</dt>
          <dd className="flex flex-wrap gap-1.5">
            {h.error_codes.length
              ? h.error_codes.map((code) => (
                  <span key={code} className="chip bg-warn-soft text-warn">
                    {ruleLabel(code)}
                  </span>
                ))
              : "None"}
          </dd>
          <dt className="label">Can act for</dt>
          <dd>
            {AUDIENCE[s.audience] ?? "Agent"}
            {s.all_tenants ? " for every organisation" : s.tenant_id ? ` for ${s.tenant_id}` : ""}
            {s.customer_ids.length ? `, customer${s.customer_ids.length > 1 ? "s" : ""} ${s.customer_ids.join(", ")}` : ", no customers"}
            {s.scopes.includes("*") ? ", everything its mandate allows" : `, limited to: ${s.scopes.map(ruleLabel).join(", ").toLowerCase()}`}
          </dd>
        </dl>
      ) : null}

      <div className="flex flex-wrap items-center gap-3">
        <button className="btn-dark h-8 !py-0" onClick={run} disabled={testing}>
          <Plug size={13} /> {testing ? "Testing…" : "Test connection"}
        </button>
        <span className="text-[12px] text-ink-muted">Makes one harmless read as this agent. Nothing is ordered.</span>
      </div>
      {testError ? <ErrorNote error={testError} /> : null}
      {test ? (
        <div className="flex flex-col gap-2">
          <StatusLine tone={test.successful_read ? "ok" : "warn"}>
            {test.successful_read
              ? "Works. The gateway read the agent's own context through this connection."
              : `The test read did not work${test.result?.message ? `: ${test.result.message}` : "."}`}
          </StatusLine>
          <details className="text-[13px]">
            <summary className="cursor-pointer font-medium text-brand">The agent can use {test.can_do.length} tools</summary>
            <div className="mt-2 flex flex-wrap gap-1.5">
              {test.can_do.map((tool) => (
                <span key={tool} className="chip bg-chip text-ink">
                  {ruleLabel(tool)}
                </span>
              ))}
            </div>
          </details>
        </div>
      ) : null}
    </div>
  );
}
