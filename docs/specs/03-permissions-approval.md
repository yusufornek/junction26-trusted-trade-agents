# 03 — Permissions & human approval (Core 3)

Status: built · Challenge text: "what the agent may decide on its own, and what must go to a human for
sign-off." Judged as "Autonomy and controls" (20%): guardrail quality, logging, auditability.

## Purpose
People set the rules; the gateway applies them to every order and records the outcome and the reasons.

## Requirements
- **R-1 Policy as data:** `backend/policy.yaml` (defaults) and a runtime copy; readable and editable over
  `GET` / `PUT /v1/policy`, `POST /v1/policy/reset`. Sections: `approval` (`max_auto_total_eur`,
  `min_fit_probability`, `warnings_need_approval`), `reject` (`on_hard_constraint_violation`, `on`),
  `ranking_weights`, `shortlist`.
- **R-2 Outcomes:** `REJECTED` (hard-constraint violation or a reject warning) takes precedence over
  `PENDING_APPROVAL` (total above the limit, fit below the floor, or an approval warning); otherwise `CONFIRMED`.
- **R-3 All reasons:** every matching rule is returned as `policy.hits[] {rule, detail}`.
- **R-4 Human sign-off:** `GET /v1/approvals` lists pending orders with quote evidence;
  `POST /v1/approvals/{order_id}/approve | reject` with `{approver, comment}`; only from `PENDING_APPROVAL`.
  A human cannot approve what the policy rejects (sales block, unknown product, hard constraint).
- **R-5 Time box:** a pending order expires with its quote and releases its reservation.
- **R-6 Policy changes are governed:** validated, effective immediately, audited as `POLICY_UPDATED` by
  `human:<name>` with a before/after diff.
- **R-7 Audit trail:** append-only (`UPDATE` / `DELETE` blocked in the database); every event has time, actor
  (`agent:<id>`, `human:<name>`, `system`), action, summary and data. `GET /v1/audit`.
- **R-8 Agent identity:** REST header `X-Agent-Id`; the MCP server is launched per agent with `--agent-id`, so an
  agent cannot rename itself per call.
- **R-9 Overview:** `GET /v1/stats` — autonomous vs. human decisions, spend, live-call averages, top warnings
  and rule hits, agents seen.

## Acceptance criteria → tests
| AC | Given / when / then | Test |
|---|---|---|
| AC-1 | All matching rules are reported | `test_policy_all_hits` |
| AC-2 | Missing fit → approval; a sales block cannot be overridden | `test_no_fit_needs_approval_and_no_override_of_sale_block` |
| AC-3 | Hard-constraint violation → rejected | `test_hard_constraint_violation_rejected` |
| AC-4 | Approve / reject only from pending, by a named human | `test_approval_transitions` |
| AC-5 | Pending order expires and releases stock | `test_pending_expiry_releases_stock` |
| AC-6 | Policy update, reset, audit diff, reload | `test_policy_update_reset_audit_and_reload` |
| AC-7 | Invalid policy has no side effects | `test_policy_validation_no_side_effects`, `test_policy_zero_weights_and_blank_human`, `test_policy_persistence_failure_rolls_back_audit` |
| AC-8 | Audit is append-only | `test_audit_is_append_only` |
| AC-9 | Stats reflect the flow | `test_stats_small_flow_and_since`, `test_stats_rejections_legacy_placements_and_timezone`, `test_stats_top_five_warnings_and_rules` |

Verified live 2026-11-13: lowering `max_auto_total_eur` 1,500 → 1,000 turned the same 1,010 € order from
`CONFIRMED` into `PENDING_APPROVAL`; both changes are in the audit trail.

## Open points for the review
- One policy for every agent, and a per-order limit only: an agent could split a large order into small ones →
  [06 — Agent mandate](06-agent-mandate.md).
- A human can stop an order before the commitment but cannot undo a confirmed one → [04](04-order-cancel.md).
- The order page shows only part of the story (no lookup / quote events) → [07 — Order timeline](07-order-timeline.md).
- Approver identity is a typed name (no login); two approver roles; mandate as a verifiable credential.
