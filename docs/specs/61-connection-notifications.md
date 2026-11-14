# 61 — Notifications to connected agents and their humans

Status: implemented · Decided on 2026-11-14 with Gökalp: when people connect their agents, Luotsi may notify them.
Builds on specs 33 (webhooks), 34 (A2A push), 42–44 and 60.

## Purpose
Once a person connects an agent, the gateway tells that agent — and through it the person — about everything that needs
attention, on the channel the agent already uses, without the person opening a dashboard.

## Requirements
- **R-1 Consent at connect time:** creating a connection (key or OAuth consent) asks which notifications the person
  allows: `approval_needed`, `order_decided`, `stockout_predicted`, `anomaly`, `proposal_received`,
  `suggestion_due`, `agent_paused`, `policy_changed`, `delivery_issue`. Defaults: approval and order decisions on.
  Stored per connection; changeable later (`GET/PUT /v1/connections/{id}/notifications`) and revocable with the
  connection.
- **R-2 One event model:** notifications come from the existing outbox events, filtered by the connection's scope,
  tenant and consent; each has `id`, `type`, `severity`, `summary` (one plain sentence), `resource` (URI of spec 60),
  `actions` (tools or links to resolve it) and `expires_at`.
- **R-3 Channels per connection type:**
  - MCP: server-to-client notifications on the streamable HTTP stream (`notifications/resources/updated` for the
    affected resource and a `notifications/message` with the summary); clients without a stream read
    `luotsi://inbox` and `get_notifications` (cursor-based, mark-read).
  - A2A: task push configurations and a notification task for events not tied to a task.
  - Webhook: Standard Webhooks delivery (spec 33) with the same payload.
  - Human fallback: an approval link (`/approvals/{id}`) inside every `approval_needed` notification.
- **R-4 Delivery guarantees:** at-least-once with idempotent ids, per-connection ordering, retry with backoff, quiet
  hours per connection, a rate cap per connection, and coalescing of repeated events of one type and resource.
- **R-5 Safety:** a notification never carries secrets or another tenant's data; summaries are generated from
  templates, never from untrusted supplier text; actions only name tools the connection may call.
- **R-6 Visibility:** the connection health view (spec 60 R-8) shows notifications sent, delivered, read and failed.
- **R-7 Destination safety everywhere:** A2A push configurations and every other outbound destination pass the
  destination checks of spec 33 (HTTPS, ownership challenge, egress re-check, no redirects).
- **R-8 Consent binding:** consent is bound to connection, channel, destination digest and recipient; changing destination
  or recipient suspends that channel (`CONSENT_STALE`) until a human re-consents.
- **R-9 Signed audience:** each notification is signed over `aud` (connection id), `tenant` and `resource`; the delivery
  worker re-checks that the event's tenant equals the subscription's tenant at send.
- **R-10 Storm control:** events carry `cause_id` and `hop`; calls made in response to a notification pass `cause_id`
  back and inherit it. A chain beyond the hop or fan-out budget is coalesced into one digest and raises `EVENT_STORM`.
- **R-11 Stale events:** each notification carries `resource_revision` and `state`; an action invoked from a superseded
  one answers `409 STALE_EVENT` with the current state and next action.
- **R-12 Ownership:** action-needed notifications move `delivered → acknowledged → claimed → resolved`. A claim is an
  exclusive expiring lease; unclaimed by its deadline, the event is offered to the tenant's next eligible connection,
  then to the human escalation ladder (spec 55 R-3).

## Acceptance criteria
- **AC-1** Connecting an agent with default consent and placing an order above its limit delivers one `approval_needed`
  notification over MCP with the approval link and the `approve`-free action list for the agent.
- **AC-2** The same event reaches an A2A push configuration and a webhook subscription with the same id.
- **AC-3** A connection that did not consent to `proposal_received` receives none; changing consent takes effect for the
  next event.
- **AC-4** A client without a stream reads the notification through `get_notifications` and marks it read.
- **AC-5** Ten stock-out events for one product within a minute arrive as one coalesced notification; quiet hours hold
  non-urgent notifications.
- **AC-6** Revoking the connection stops delivery immediately; no notification contains a secret or another tenant's
  order.
- **AC-7** A push URL that resolves to a private address, or redirects, is refused at registration and at delivery;
  editing a webhook URL stops delivery with `CONSENT_STALE` until re-consent.
- **AC-8** An event forced onto another tenant's subscription is dropped and audited; the reference client rejects a
  wrong `aud`.
- **AC-9** Two agents that update a resource on every update are cut off after N hops with one `EVENT_STORM` alert; a
  retried `approval_needed` arriving after `CONFIRMED` answers `STALE_EVENT` and does not reopen the order.
- **AC-10** A delivery-delay alert unclaimed by the purchasing agent is claimed by the receiving agent, and a late
  competing claim answers `409 ALREADY_CLAIMED`.


## Implementation and decisions taken
- `backend/app/connection_notifications*.py` owns the capability. Existing modules only register the capability,
  delegate delivery, or stamp event lineage. `clients/connection_notifications.py` verifies the detached JWS,
  expected connection audience, tenant, resource scheme and expiry before presenting a notification.
- Key issuance and both OAuth consent flows accept all nine boolean choices. Approval and order decisions default
  on. The built-in consent pages show the choices, and consent-request JSON exposes them for the external console.
  GET/PUT notification preferences also have tenant/operator audience aliases. Only the connection's human owner
  or an authorized administrator can grant or renew consent; an agent cannot authorize a new destination.
- Each HTTP binding covers its connection, channel, URL/authentication digest and recipient. MCP binds its stream
  to the connection's agent and creator. Destination, authentication, recipient, audience or customer changes
  suspend delivery until human re-consent. Re-consent challenges the destination again. Spec 33's DNS pinning,
  HTTPS checks, redirect refusal and Standard Webhooks signatures are reused for every HTTP send.
- The durable inbox is projected from outbox events, with source deduplication and a shared revision per tenant
  and affected resource URI. Envelopes contain only fixed template text, validated state, gateway resource IDs
  and authorized tool names. Per-connection JWS signatures bind the audience, tenant and resource, including on
  MCP and in A2A notification task metadata. Domain events retain their IDs across channels.
- MCP uses native SDK sessions for resource updates and log messages, the SDK resource/template registration
  from spec 60, and schema-generated tools with SDK annotations/structured output. A session facade is created
  per request by this SDK, so stream registration deduplicates on the underlying SDK connection. Modern logging
  still honors the SDK's per-request log-level opt-in; the durable inbox works without a stream or logging.
- A2A stores a notification task even without an active push destination. Task-bound pushes attach the same signed
  notification; unrelated events get a separate notification task. OAuth grants retain the original A2A protocol
  despite their stored `type=oauth`. Procurement task projection echoes are suppressed in favor of the domain
  event ID, and physical delivery waits for the outbox projection pass to finish. Outbound task status text is replaced with
  the safe notification template. Original task ownership and tenant are checked before using it.
- Default limits: six response hops, 50 cumulative fan-out recipients per cause, 30 delivery attempts per minute (including failures),
  a 60-second coalescing window for non-urgent events, five HTTP attempts with exponential backoff/jitter,
  24-hour notification expiry, and five-minute ownership deadlines/leases. Quiet hours use an IANA timezone;
  equal start/end means the entire day. Approval, delivery and pause alerts are urgent. Delivery ordering is
  maintained for each connection/channel queue, so an absent MCP stream cannot block a bound webhook.
- Coalescing occurs only before an item has been attempted or delivered; the latest revision, cause and hop are
  signed into the buffered item. Cursor reads count as delivery; `mark_read` separately records reading.
  Health reports sent attempts, successful channel deliveries, read items and failed deliveries. Existing webhook
  delivery-history and redelivery endpoints delegate to the new durable queue for connected subscriptions.
- Resolving REST/A2A calls pass `X-Notification-Id` and `X-Cause-Id`; MCP uses `notification_id` and `cause_id`
  in request `_meta`. The gateway derives and inherits lineage from the stored notification, never trusts a
  caller-supplied hop, and refuses expired/superseded actions with `409 STALE_EVENT` plus current state and next
  action. Over-budget chains produce a single durable `EVENT_STORM` digest/audit record and are refused.
- Claims use the SQLite writer transaction as the compare-and-set boundary. Claiming implicitly acknowledges
  delivery. Leases are exclusive, bounded and expiring. Handoff offers the event to the next eligible connection,
  then to active tenant approvers followed by tenant administrators. Human offers are available through the
  authenticated notification inbox and use the same claim/resolve action; unattended rungs expire in order.
  Notification ownership never grants purchasing authority. Approval notifications resolve automatically when
  the order is decided; resolving an inbox item cannot substitute for human approval.
- The pre-existing spec 63 contract builder passes `full_surface=True` to the MCP factory, although its signature
  did not accept that keyword in this tree. The factory now accepts it; it already constructs the full surface. Its schema is initialized during gateway startup,
  before nested command transactions, to preserve savepoints.
  Contract acknowledgement and driving-test checks remain in place. Tests use acknowledged connections and
  trusted internal domain event generation where purchasing graduation is outside this notification capability.
- No new dependencies or environment variables are required. Live state, frontend files and generated static
  discovery/OpenAPI artifacts are untouched. The latter must be regenerated at merge.

## Acceptance mapping
Exactly ten focused tests live in `backend/tests/test_spec61_connection_notifications.py`.

| AC | Test |
|---|---|
| AC-1 | `test_AC_1_default_consent_sdk_mcp_approval_link_and_agent_actions` |
| AC-2 | `test_AC_2_a2a_task_push_and_standard_webhook_share_event_id` |
| AC-3 | `test_AC_3_consent_changes_apply_to_next_event_only` |
| AC-4 | `test_AC_4_sdk_cursor_inbox_and_mark_read_without_stream` |
| AC-5 | `test_AC_5_coalescing_quiet_hours_rate_order_and_retry` |
| AC-6 | `test_AC_6_revoke_and_template_only_tenant_isolation` |
| AC-7 | `test_AC_7_destination_safety_and_destination_recipient_reconsent` |
| AC-8 | `test_AC_8_worker_audits_cross_tenant_and_reference_rejects_wrong_audience` |
| AC-9 | `test_AC_9_feedback_chain_budget_single_digest_and_stale_action_409` |
| AC-10 | `test_AC_10_expiring_exclusive_claim_handoff_and_human_ladder` |

Run from `backend/`:
`/Users/daption-ciray/Desktop/junction/.claude/worktrees/selam-junction-challenge-44cf24/backend/.venv/bin/python -m pytest tests/test_spec61_connection_notifications.py -q`


## Validation and remaining merge work
- Latest spec 61 acceptance run: **10 passed, 1 warning in 31.35 s**. Exactly one focused test covers each AC.
  The checks include standalone and procurement-task-bound A2A delivery, OAuth A2A polling, native SDK metadata
  propagation/refusals, recipient changes during re-consent, both hop and fan-out budgets, and the human ladder.
- Focused MCP/webhook compatibility run: **42 passed, 1 warning in 99.34 s** across this acceptance file,
  `test_spec60_agent_surface.py`, `test_webhooks.py`, and the nested-transaction regression from
  `test_shared_foundation.py`. The subsequent A2A echo, OAuth transport and metadata refinements were verified
  again in the latest ten acceptance tests. The warning is the installed SDK's deprecation notice for logging;
  spec 61 explicitly requests `notifications/message`, so the SDK method remains in use.
- Broader affected-module run: **184 passed, 30 failed in 491.81 s**. An additional API/A2A run returned
  **44 passed, 4 failed in 152.57 s**. Both preceded the corrected human-ladder fixture; the first also exposed
  lazy spec 63 schema initialization inside a nested writer. The latest acceptance and nested-writer recheck pass.
  The remaining compatibility failures are not represented as a green suite or assumed to be pre-existing:
  older key/OAuth tests do not perform the contract/graduation steps now required by spec 63; exact outbox and
  REST path inventories omit the new consumer/routes; spec 63 driving tools have a generated/custom registration
  collision; and two A2A tests disagree with current strict command arguments/admission ordering. Their existing
  tests and unrelated capability implementations were left unchanged. Merge reconciliation is still required.
- Python syntax was checked for the touched source/test files. No full suite, network access, live-state writes,
  dependency installation, frontend changes, git commands or commits were used.
- `backend/static/tools.json`, `backend/static/developers.html` and `contracts/openapi.yaml` must be regenerated
  at merge, as instructed; this capability does not hand-edit them. Detailed scratch logs stay in `/private/tmp/`.
