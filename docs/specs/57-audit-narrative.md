# 57 — Audit narrative for reviewers

Status: in progress · Decided on 2026-11-14 with Gökalp from the business teammate's suggestion 4 (capability spec,
see spec 47). Builds on specs 00 (audit), 07 (order timeline), 11 (tamper-evident audit), 29 (evidence bundle) and 44
(governance report).

## Purpose
Anyone reviewing what agents did — a kitchen manager, a finance approver, an auditor, a judge — reads a plain-language
story of an order, an agent or a period, where every sentence points to the audit entries that prove it.

## Requirements
- **R-1 Scopes:** `GET /v1/narratives?scope=order|agent|customer|tenant&id=…&from=…&to=…&lang=en|fi` and MCP tool
  `get_audit_narrative`; access follows the audience rules of spec 43 (buyer sees its own, tenant its agents, operator
  all).
- **R-2 Deterministic story:** the narrative is built from audit entries by event templates (one sentence template per
  event type, grouped into episodes: request, search, decision, approval, order, delivery, exception), so the same
  entries always give the same text. Every sentence carries `evidence: [audit entry ids]`; unknown event types appear
  as a neutral sentence, never omitted.
- **R-3 Highlights:** the story marks what a reviewer should look at: human decisions and who made them, guardrail
  hits, anomalies, overrides and exceptions, amounts above thresholds, and anything still open.
- **R-4 Optional summary layer:** when an LLM is configured, a short summary is generated only from the deterministic
  sentences and must cite their ids; sentences without citations are dropped. Without an LLM the deterministic story
  is the output.
- **R-5 Verifiable:** the response includes the audit chain head it was built from; the evidence bundle (spec 29) can
  include the narrative with its citations, and the verifier checks that every cited entry exists in the bundle.
- **R-6 Formats:** JSON (episodes, sentences, citations, highlights) and Markdown for humans; Finnish and English
  templates.

## Acceptance criteria
- **AC-1** The demo order's narrative tells request → lookup → quote → approval → order → confirmation in order, each
  sentence citing its audit entries.
- **AC-2** An order with a guardrail hit, an anomaly and a human override highlights all three with the deciding person.
- **AC-3** An agent narrative for a day groups several orders into episodes and lists open items.
- **AC-4** The same entries give identical text twice; an unknown event type appears as a neutral sentence.
- **AC-5** With a fake LLM that adds an uncited claim, that sentence is dropped from the summary.
- **AC-6** A buyer cannot read another tenant's narrative.
