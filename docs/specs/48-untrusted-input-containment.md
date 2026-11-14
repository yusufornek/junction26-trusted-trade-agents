# 48 — Untrusted input containment

Status: in progress · Decided on 2026-11-14 with Gökalp (capability spec, see spec 47). Builds on specs 06 (agent
mandate), 36 (supplier proposals) and 44 (agent governance).

## Purpose
Text the buyer did not write — product listings, supplier proposals, invoices, delivery notes, messages from other
agents — can inform an agent but can never widen what the buyer authorised.

## Requirements
- **R-1 Provenance:** every free-text field the gateway returns that comes from a supplier, catalog, document or another
  agent is wrapped as `{text, provenance: {source, source_id, trust: "untrusted"}}` in REST, MCP and A2A responses, and
  MCP tool descriptions tell agents that such content is data.
- **R-2 Authorised envelope:** each intent or quote carries the envelope it was created under: customer, permitted
  categories, permitted actions, product set or functional needs, quantity and amount limits, delivery point. Every
  state-changing call is checked against the envelope of the intent it acts on.
- **R-3 Diversion detection:** a call that changes the product, supplier, category, delivery point or amount beyond the
  envelope is refused with `403 OUTSIDE_AUTHORISED_ENVELOPE`, the offending fields and the provenance of the content the
  agent read just before (last untrusted sources returned to that agent in the session), and an audit entry.
- **R-4 Content screening:** catalog and proposal text is scanned at ingest and on read for instruction-like patterns
  (imperatives addressed to agents, tool names, URLs to act on, hidden characters); matches set
  `content_flags: ["INSTRUCTION_LIKE"]` and raise a `SUSPICIOUS_CONTENT` alert to the operator.
- **R-5 Quarantine:** the operator (and the tenant admin for its own view) can quarantine a listing or proposal
  (`POST /v1/content/{kind}/{id}/quarantine`); quarantined items are excluded from lookup and ranking with the reason,
  and the legitimate purchase continues with the remaining candidates.
- **R-6 Sandbox fixture:** a poisoned listing and a poisoned proposal in the sandbox make the path demonstrable.
- **R-7 Offer reference:** quote and order lines cite an `offer_ref` issued by a lookup, RFQ or proposal for that intent and tenant; a bare product id answers `422 OFFER_REF_REQUIRED` with `LOOKUP_REQUIRED`.
- **R-8 Ranking isolation:** an offer's untrusted text is input only to its own fit score, scored in isolation; `INSTRUCTION_LIKE` offers are scored from structured fields only, and the ranking breakdown shows this.

## Acceptance criteria
- **AC-1** Every untrusted text field in lookup, proposal and order responses carries provenance; buyer-entered fields
  do not.
- **AC-2** After reading the poisoned listing, an agent's attempt to swap the basket to an unrelated expensive product
  is refused with `OUTSIDE_AUTHORISED_ENVELOPE` naming the fields; the original basket still orders.
- **AC-3** The same refusal happens for a delivery-point change and an amount above the envelope, whatever the source.
- **AC-4** The poisoned listing is flagged `INSTRUCTION_LIKE` at ingest and raises `SUSPICIOUS_CONTENT`.
- **AC-5** A quarantined listing disappears from lookup with its reason and reappears after release.
- **AC-6** A quote line with a real product id the agent never received from a lookup answers `OFFER_REF_REQUIRED`.
- **AC-7** A listing whose text tries to boost itself or demote others does not change any other offer's score, and a flagged listing is scored from structured fields only.
