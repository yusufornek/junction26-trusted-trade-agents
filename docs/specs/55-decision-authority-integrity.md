# 55 — Decision authority integrity

Status: in progress · Decided on 2026-11-14 with Gökalp (capability spec, see spec 47; drafted by Fable from Astra's
unexpected-situation review). Builds on specs 03, 16, 17, 33, 43, 44 and 45.

## Purpose
A human decision counts when the right natural person, with authority for that scope and site, decided on the evidence
actually shown, under the policy in force at commitment.

## Requirements
- **R-1 Scoped decision set:** a pending order lists its required decisions by scope (`spend`, `substitution`,
  `safety`, `policy_exception`) with eligible roles. It confirms when every scope has an approval on the current
  evidence revision. A rejection stands for its scope and returns the order for change or rejects it. Decisions are
  compare-and-set on the order revision; a contradicting decision in the same scope answers `409 DECISION_CONFLICT` and
  escalates to the tenant admin.
- **R-2 Natural person:** every account maps to a `person_id` (identity-provider subject, verified email or phone,
  admin-declared links). Four-eyes and multi-step rules count distinct persons; the same person answers
  `403 FOUR_EYES`.
- **R-3 Delegation and escalation:** `POST /v1/tenant/delegations {from, to, scopes, valid_from, valid_until}`. Cycles
  are refused at creation (`409 DELEGATION_CYCLE`) and re-checked at resolution; the chain is bounded; a delegate's
  authority is the intersection of both parties'. When nobody resolves, the request moves up an escalation ladder at a
  set fraction of the time left to the order cut-off, with an `APPROVER_UNAVAILABLE` alert.
- **R-4 Shown-evidence binding:** every approval surface (page, notification, MCP, chat) renders from one presentation
  `{order_id, evidence_revision, summary_digest, site}`. Approve carries the revision and digest it rendered; a mismatch
  answers `409 EVIDENCE_CHANGED` with `REVIEW_UPDATED_EVIDENCE`.
- **R-5 Site context:** the presentation leads with site, delivery point and date. When the approver has a similar
  pending basket (spec 47 demand fingerprint) for another site, approve requires `site_confirmation`; a wrong value is
  refused without effect.
- **R-6 Policy epoch:** each decision records the policy version in force. Approval and supplier submission re-evaluate
  the current policy; a tightening after approval returns the order to `PENDING_APPROVAL` with `POLICY_CHANGED`. An
  exception is a `policy_exception` decision by a role the policy layer names, citing the rule and current version,
  valid for one order. The timeline shows one ordered account.
- **R-7 Surfaces and audit:** REST and MCP for delegations and decision sets; every decision, delegation resolution,
  escalation and exception is audited.

## Acceptance criteria
- **AC-1** A delegates to B while B already delegates to A: the second delegation answers `DELEGATION_CYCLE`; with both
  absent the request escalates to the tenant admin before the cut-off.
- **AC-2** Two accounts linked to one person approve the two steps of a four-eyes order: the second answers `FOUR_EYES`.
- **AC-3** Finance approves `spend` and the kitchen lead rejects `substitution` seconds apart in either order: the order
  is not confirmed and shows both decisions.
- **AC-4** With similar baskets pending for two sites, approving without the matching `site_confirmation` is refused.
- **AC-5** A notification rendered at revision 1 is tapped after revision 2 exists: `EVIDENCE_CHANGED`, no confirmation.
- **AC-6** A category freeze after approval and before submission returns the order to pending; a shift manager's
  exception under the old version is refused, and one by a named role citing the current version confirms it.
