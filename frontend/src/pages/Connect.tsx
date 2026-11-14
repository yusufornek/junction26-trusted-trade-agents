import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Check, CircleDashed, Copy, ExternalLink, KeyRound, Plug, ShieldAlert, Trash2 } from "lucide-react";
import { ApiError, api, type ClientKind, type Connection, type NewConnection } from "../api";
import { Card, ErrorNote, Spinner, useAsync, when } from "../ui";

type Client = {
  id: ClientKind;
  name: string;
  /** What the person does in that tool, in its own words. */
  how: string;
  transport: "mcp" | "rest";
  /** claude.ai and ChatGPT add remote tools only through sign-in (OAuth); the gateway issues keys today. */
  needsSignIn?: boolean;
};

const CLIENTS: Client[] = [
  { id: "claude_desktop", name: "Claude Desktop", how: "Adds the gateway as a tool server in Claude's desktop app.", transport: "mcp" },
  { id: "claude_code", name: "Claude Code", how: "One command in your terminal, or a message Claude Code runs for you.", transport: "mcp" },
  { id: "cursor", name: "Cursor", how: "One click installs the server in Cursor.", transport: "mcp" },
  { id: "vscode", name: "VS Code", how: "One click installs it for Copilot's agent mode.", transport: "mcp" },
  { id: "chatgpt", name: "ChatGPT", how: "ChatGPT adds remote tools through a sign-in window.", transport: "mcp", needsSignIn: true },
  { id: "mcp_other", name: "Other MCP client", how: "Any client that speaks MCP over streamable HTTP.", transport: "mcp" },
  { id: "rest", name: "REST API", how: "Your own code or agent framework, with plain HTTPS calls.", transport: "rest" },
];

const PLACEHOLDER = "<YOUR_AGENT_KEY>";
const SERVER_NAME = "luotsi";
const TRY_PROMPT = "Find 50 litres of lactose-free milk for delivery on Monday and show me the best offer. Do not order yet.";

export default function Connect() {
  const [clientId, setClientId] = useState<ClientKind>("claude_desktop");
  const client = CLIENTS.find((c) => c.id === clientId)!;
  const mcp = useAsync(() => api.mcpExposed(), []);
  const policy = useAsync(() => api.policy(), []);
  const me = useAsync(() => api.me(), []);
  const connections = useAsync(() => api.connections(), []);
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
      const c = await api.createConnection({ agent_id: agentId.trim(), client: client.id, customer_ids: customerId ? [customerId] : [] });
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

      <Card className="flex flex-col gap-4">
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
        <StepTitle n={2} title={`Connect ${client.name}`} text={client.how} />
        <ClientSteps client={client} mcpUrl={mcpUrl} restBase={restBase} agentKey={key} hasKey={!!created} mcpLive={!!mcp.data} />
      </Card>

      <Card className="flex flex-col gap-4">
        <StepTitle n={3} title="Check it works" text="Ask your agent for an offer. Its first call to the gateway shows up here." />
        <CopyBlock label="Try this in your agent" value={TRY_PROMPT} wrap />
        <FirstCall agentId={validAgentId ? agentId.trim() : ""} since={startedAt} onSeen={connections.reload} />
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
  if (client.needsSignIn)
    return (
      <StatusLine tone="warn">
        {client.name} adds remote tools only through a sign-in window (OAuth). This gateway issues per-agent keys today, so
        {` ${client.name}`} can connect once sign-in is added on the gateway. Until then, use Claude Desktop, Claude Code, Cursor or VS Code.
      </StatusLine>
    );
  return mcpExposed ? (
    <StatusLine tone="ok">MCP is live on this address.</StatusLine>
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
            Claude Desktop reaches remote servers through mcp-remote, a small bridge that runs with Node.js. claude.ai in the browser
            adds remote tools through sign-in only, which the gateway does not offer yet.
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
    case "cursor": {
      const link = `cursor://anysphere.cursor-deeplink/mcp/install?name=${SERVER_NAME}&config=${encodeURIComponent(
        btoa(JSON.stringify({ url: mcpUrl, headers: { Authorization: header } })),
      )}`;
      return (
        <>
          <OneClick href={link} label="Add to Cursor" ready={oneClickReady} reason={blockedReason} />
          <CopyBlock label="Or add it to .cursor/mcp.json" value={JSON.stringify({ mcpServers: { [SERVER_NAME]: { url: mcpUrl, headers: { Authorization: header } } } }, null, 2)} />
        </>
      );
    }
    case "vscode": {
      const link = `vscode:mcp/install?${encodeURIComponent(JSON.stringify({ name: SERVER_NAME, ...httpConfig }))}`;
      return (
        <>
          <OneClick href={link} label="Add to VS Code" ready={oneClickReady} reason={blockedReason} />
          <CopyBlock label="Or add it to .vscode/mcp.json" value={JSON.stringify({ servers: { [SERVER_NAME]: httpConfig } }, null, 2)} />
        </>
      );
    }
    case "chatgpt":
      return (
        <>
          <Steps
            items={[
              "In ChatGPT, turn on developer mode under Settings, Connectors, Advanced.",
              "Create a connector with the server address below and choose sign-in (OAuth).",
              "Sign in to the gateway in the window that opens.",
            ]}
          />
          <CopyBlock label="Server address" value={mcpUrl} />
          <p className="text-[12px] text-ink-muted">The last step needs sign-in on the gateway, which is not available yet.</p>
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
        <h3 className="text-[15px] font-semibold">Keys in use</h3>
        <p className="label mt-0.5">Revoking a key stops that client at its next call. The agent's mandate and history stay.</p>
      </div>
      {connections.loading && !connections.data ? (
        <Spinner />
      ) : connections.error ? (
        <ErrorNote error={connections.error} />
      ) : !list.length ? (
        <p className="text-[13px] text-ink-muted">No keys created from the console yet.</p>
      ) : (
        <ul className="flex flex-col divide-y divide-line">
          {list.map((c) => (
            <li key={c.connection_id} className="flex flex-wrap items-center gap-x-4 gap-y-1 py-2.5 text-[13px]">
              <span className="font-medium">{c.agent_id}</span>
              <span className="text-ink-muted">{CLIENTS.find((x) => x.id === c.client)?.name ?? c.client}</span>
              <code className="text-ink-muted">{c.key_prefix}…</code>
              <span className="text-ink-muted">{c.last_used_at ? `used ${when(c.last_used_at)}` : "not used yet"}</span>
              <button className="btn-danger ml-auto h-8 !py-0" onClick={() => revoke(c.connection_id)} disabled={busy === c.connection_id}>
                <Trash2 size={13} /> {busy === c.connection_id ? "Revoking…" : "Revoke"}
              </button>
            </li>
          ))}
        </ul>
      )}
      {error ? <ErrorNote error={error} /> : null}
    </Card>
  );
}
