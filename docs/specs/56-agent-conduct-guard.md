# 56 — Agent conduct guard and protected capacity

Status: in progress · Decided on 2026-11-14 with Gökalp (capability spec, see spec 47; drafted by Fable from Astra's
unexpected-situation review). Builds on specs 06, 24, 41, 42, 44 and 45.

## Purpose
Valid credentials and in-limit single calls are weak evidence of buyer intent. The gateway judges the sequence of an
agent's behaviour and keeps capacity free for the actions that protect the buyer.

## Requirements
- **R-1 Priority classes:** every supplier call has a class: `protective` (cancel, order status, commitment checks),
  `transactional` (quote, order), `interactive` (lookup), `background` (watches, polling). Each supplier budget,
  including account-wide limits declared in adapter capabilities, reserves a share for `protective`. `background` sheds
  first and pauses on a supplier 429; calls with a cut-off are queued by deadline.
- **R-2 Progress guard:** equivalent calls (same normalised payload digest) repeated beyond N in a window without a
  state change answer `429 NO_PROGRESS` with the last result and `next_action: STOP`; at 2N the agent is paused
  (spec 44) and the owner alerted.
- **R-3 Cumulative baseline:** per agent, rolling day, week and month order count, spend, distinct delivery points and
  products are compared with its history band and the tenant's demand signals (recurring schedule, purchase memory,
  on-hand stock). A sustained excess raises `CUMULATIVE_ANOMALY`: approval required and owner alerted.
- **R-4 Credential context:** a key records the contexts it is used from (network, protocol, client id). A new context
  on an order-capable key raises `NEW_KEY_CONTEXT`; the next order needs approval.
- **R-5 Buyer digest:** a daily signed digest per customer lists everything agents ordered autonomously; "not mine"
  pauses the agent and opens cancellations where the cut-off allows.

## Acceptance criteria
- **AC-1** With a stock-watch burst exhausting the supplier allowance, a cancel issued before the cut-off still reaches
  the supplier; background checks are deferred.
- **AC-2** An agent refreshing the same offer 10 times in a minute gets `NO_PROGRESS` with `STOP`; the legitimate basket
  still orders.
- **AC-3** Twenty small in-limit orders in a week against a history of five raise `CUMULATIVE_ANOMALY` although each
  order passes the per-order checks.
- **AC-4** The same key used from a new network gets `NEW_KEY_CONTEXT` and its next order waits for approval; "not mine"
  in the digest pauses the agent.
