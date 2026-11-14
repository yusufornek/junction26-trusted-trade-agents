# 27 — OpenTelemetry traces and metrics

Status: in progress · Decided on 2026-11-14 · Design reviewed with the external architect (round 4).

## Purpose
An operator can follow one purchase through the gateway and see how the gateway behaves under load, with standard
tooling.

## Requirements
- **R-1 Instrumentation:** FastAPI and httpx auto-instrumentation plus spans `catalog.search`, `fit.classify`,
  `supplier.price`, `supplier.availability`, `plan.build`, `policy.evaluate`, `quote.create`, `order.place`,
  `approval.review`, `webhook.deliver`, `audit.anchor`.
- **R-2 Attributes (allowlist):** transport (rest | mcp | a2a), supplier, environment, outcome, cache hit,
  candidate counts, rule codes, correlation id, order / quote references. Credentials, secrets, raw prompts and
  personal data stay out; a test asserts the allowlist.
- **R-3 Metrics:** request and upstream latency histograms, upstream call counter, cache hits, decision counts
  by outcome, approval-wait histogram, throttles, outbox backlog gauge, delivery attempts, expiries.
- **R-4 Async links:** approval and delivery spans link to the placing span (span links) instead of one long span.
- **R-5 Export:** OTLP/HTTP when `OTEL_EXPORTER_OTLP_ENDPOINT` is set; console exporter with
  `OTEL_CONSOLE=1`; with neither, instrumentation is a no-op. Exporter failure never fails a request.
- **R-6 Demo stack:** `backend/observability/docker-compose.yml` with a pinned Collector, Jaeger and
  Prometheus + Grafana with one dashboard; versions pinned in `/v1/meta`.

## Acceptance criteria
- **AC-1** One order placement produces a trace containing the R-1 spans in parent/child order (in-memory
  exporter in the test).
- **AC-2** The approval span carries a link to the placement span's context.
- **AC-3** No span or metric attribute outside the allowlist; a secret placed in the request never appears.
- **AC-4** Counters reflect one lookup: upstream calls 2, candidates screened and priced as in the lookup result.
- **AC-5** A failing exporter leaves the request successful.
- **AC-6** Without OTEL variables no exporter threads start.
