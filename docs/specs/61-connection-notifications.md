# 61 — Notifications to connected agents and their humans

Status: in progress · Decided on 2026-11-14 with Gökalp: when people connect their agents, Luotsi may notify them.
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
  then to the tenant's approvers.

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
