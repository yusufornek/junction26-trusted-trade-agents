# 34 — A2A service next to MCP

Status: in progress · Decided on 2026-11-14 · Design reviewed with the external architect (round 4).

## Purpose
Agent frameworks that speak the Agent2Agent protocol reach the same gateway core without MCP.

## Requirements
- **R-1 Version and endpoints:** A2A 1.0.0 (wire version "1.0"), Agent Card at
  `/.well-known/agent-card.json`, JSON-RPC 2.0 endpoint at `/a2a`. Methods: `SendMessage`, `GetTask`,
  `ListTasks`, `CancelTask`, `SendStreamingMessage`, `SubscribeToTask`, and the task push-notification
  configuration methods of 1.0.
- **R-2 Agent Card:** name, description, provider, version, skills (`find_offers`, `find_basket_offers`,
  `quote_and_order`, `order_status`, `cancel_order`, `get_evidence`), supported interfaces, bearer security
  scheme, streaming and push capabilities — generated from the same schemas as the MCP tools.
- **R-3 Procurement task:** one task owns references to its request, offers, selected plan, quote, order and
  evidence artifacts. `SendMessage` carries a versioned structured command in a data part
  (`{command: lookup | select_plan | place_order | cancel | get_evidence, ...}`); later messages reference the
  task. Results are artifacts with data parts (and a text summary part).
- **R-4 States:** `TASK_STATE_WORKING` while executing; `TASK_STATE_INPUT_REQUIRED` when a plan selection or
  missing request detail is needed; `TASK_STATE_AUTH_REQUIRED` while human purchasing approval is pending;
  `TASK_STATE_COMPLETED` on a confirmed order; `TASK_STATE_REJECTED` / `TASK_STATE_FAILED` /
  `TASK_STATE_CANCELED` accordingly. The approval itself happens in the gateway's authenticated approval flow.
- **R-5 Same core:** principal, mandate, idempotency (message id), rate limits, recovery payload (in the task
  status message and error data) and events are those of REST and MCP.
- **R-6 Streaming and push:** task updates stream from the event outbox; push configurations are delivered by
  the webhook service (spec 33) in A2A's event format.
- **R-7 Cancel:** `CancelTask` maps to the order cancellation rules; cancelling after confirmation is a new
  cancel command against the order.
- **R-8 Reference client:** `backend/demo/a2a_client.py` completes the split-order journey over A2A.

## Acceptance criteria
- **AC-1** The Agent Card validates against the 1.0 schema and lists the bearer scheme and skills.
- **AC-2** `SendMessage` lookup → task with offers artifact; `select_plan` + `place_order` → order; states
  follow R-4.
- **AC-3** A pending approval shows `TASK_STATE_AUTH_REQUIRED`; after the operator approves, `GetTask` shows
  `TASK_STATE_COMPLETED` with the receipt artifact.
- **AC-4** A repeated message id returns the same task without a second order.
- **AC-5** Another agent's `GetTask` on the task → not found.
- **AC-6** `SendStreamingMessage` emits status updates in order; `SubscribeToTask` after a reconnect resumes.
- **AC-7** The same purchase over REST, MCP and A2A yields equal totals, policy outcome, stock effect and audit
  records (cross-interface acceptance suite).
- **AC-8** A throttled A2A call returns the recovery payload with the retry interval.
