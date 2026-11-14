# 45 — Guardrail profiles and anomaly detection

Status: in progress · Decided on 2026-11-14 with Gökalp in the guardrail discussion. Builds on specs 30
(exception journey), 41 (purchase memory), 43 (accounts) and 44 (agent governance).

## Purpose
A buyer business gets guardrails that fit it from the first minute: it says what kind of kitchen it runs and how
big it is, and its policies, approval rules and anomaly sensitivity are set up for it. When an order looks unusual,
the gateway stops it for a human and proposes how to fix it, so the unhappy path ends in a solution rather than a
refusal.

## Requirements
- **R-1 Sign-up with a business profile:** `POST /v1/auth/signup` creates a tenant and its first `tenant_admin`
  (email + password, spec 43) and asks: business type (`single_restaurant`, `restaurant_group`, `hotel_chain`,
  `hospital_or_school_kitchen`, `catering`), number of sites, number of people who buy or approve, monthly purchase
  volume band (`<5k`, `5–25k`, `25–100k`, `>100k` €), meals per day band, and optional standing constraints
  (required claims, excluded allergens). `GET /v1/auth/signup/options` returns the choices with labels. The profile
  is stored on the tenant and editable by a `tenant_admin` (`PUT /v1/tenant/profile`).
- **R-2 Profile presets:** the profile selects a preset that fills the tenant guardrails (spec 44 layer 2):
  per-order ceiling, daily and monthly budget (scaled by the volume band), approver amount threshold, four-eyes
  rule, substitutes policy, anomaly sensitivity and the starting autonomy of new agents. Presets live in one data
  file (`backend/guardrail_presets.yaml`) and the response shows which preset produced each value. Defaults:
  - `single_restaurant`: four-eyes off (the admin may approve its own agent's orders, recorded with a warning),
    low ceilings, substitutes need approval.
  - `restaurant_group`, `hotel_chain`: four-eyes on, approver threshold, per-site budgets when sites > 1.
  - `hospital_or_school_kitchen`: required allergen exclusions and claims are mandatory before the first order,
    four-eyes on, every substitute needs approval, strict anomaly sensitivity.
  - `catering`: higher volume limits, velocity anomaly sensitivity high.
  The tenant may change any preset value inside the platform policy; changes follow spec 44 change control.
- **R-3 Approver amount threshold:** a tenant sets an amount above which only a `tenant_admin` can approve; an
  `approver` approving above it answers `403 APPROVAL_LIMIT` with the threshold and who can approve.
- **R-4 Four-eyes rule:** when on, the person who created an agent's connection, owns the agent, or approved its
  last autonomy or policy loosening cannot approve that agent's orders (`403 FOUR_EYES`); the inbox shows who can.
  When off, the self-approval is recorded with `self_approved: true` in the order and the audit entry.
- **R-5 Anomaly checks (deterministic, always on):** before an order is placed the gateway compares it with the
  agent's and customer's purchase history (spec 41) and raises warnings that require approval:
  - `QUANTITY_ANOMALY` — quantity outside the expected band for this product or category (median ± k·MAD of past
    order quantities, weekday-aware; k from the sensitivity).
  - `PRICE_ANOMALY` — unit price ≥ 20 % above the last paid price for the same product (threshold from the preset).
  - `VELOCITY_ANOMALY` — more orders in a rolling hour than the expected band; at twice the band the agent is
    paused (spec 44 R-9) and the owner is alerted.
  - `NEW_CATEGORY` — first order of the agent in a category it never bought.
  With fewer than 4 past data points for a series the check reports `insufficient_history` and raises no warning.
- **R-6 Explanations and fixes:** each anomaly carries the expected band, the observed value, the history window
  and one or more proposed fixes that the agent or approver can apply in one step through the exception journey
  (spec 30): unit confusion (the quantity equals the expected quantity times a case or unit multiplier → proposed
  corrected quantity), a cheaper ranked alternative for a price anomaly, splitting an unusual quantity into the usual
  quantity now and the rest for approval, and pause/resume for velocity. Optional plain-language wording from the
  configured LLM is added as `explanation_text`; the LLM never decides, approves or changes values.
- **R-7 Forecast layer (optional):** when `FORECAST_ENABLED=true` a forecaster predicts the expected quantity band
  per series from history (quantiles 0.05–0.95) and the anomaly check uses that band instead of median ± k·MAD,
  recording `band_source: forecast`. The forecaster is Mistral's Timestral (official `mistralai/Timestral-28M`
  weights, package `timestral` pinned to commit `a534bdbd244d64f3d9d743f46c7bf6fc330ed4ff`), installed from an
  optional requirements file (`backend/requirements-forecast.txt`) and run on CPU; weights load once at start-up
  from a configured local path. Without it, or on any forecaster error, the deterministic band is used and
  `band_source: statistical` is recorded. The model licence (modified MIT with a revenue threshold) is recorded in
  `docs/DECISIONS.md` as a production prerequisite.
- **R-8 Demo history:** `scripts/reset_demo.py` seeds 90 days of synthetic purchase history per demo tenant
  (weekday pattern, a few price changes), marked `synthetic: true`, so every anomaly can be demonstrated; the demo
  milk order of 1,000 L stays inside the expected band for the hotel chain.
- **R-9 Surfaces and audit:** profiles, presets, thresholds and anomalies appear on every tenant surface (spec 43
  R-17); anomaly warnings, fixes applied, `FOUR_EYES` and `APPROVAL_LIMIT` refusals and self-approvals are audited.

## Acceptance criteria
- **AC-1** Sign-up as `hospital_or_school_kitchen` with 1 site creates the tenant, its admin and guardrails from
  the preset; an order before allergen constraints are set answers `409 CONSTRAINTS_REQUIRED`.
- **AC-2** Each of the five profiles yields its preset values, and the response names the preset per value.
- **AC-3** An approver above the threshold → `403 APPROVAL_LIMIT`; a tenant admin approves.
- **AC-4** Four-eyes on: the connection creator cannot approve (`403 FOUR_EYES`), another approver can; four-eyes
  off: self-approval succeeds with `self_approved: true` in order and audit.
- **AC-5** An order of 10× the usual milk quantity raises `QUANTITY_ANOMALY` with the band and a unit-confusion fix;
  applying the fix re-quotes the corrected quantity.
- **AC-6** A price 25 % above the last paid price raises `PRICE_ANOMALY` with a cheaper ranked alternative.
- **AC-7** A burst of orders raises `VELOCITY_ANOMALY`; at twice the band the agent is paused and alerted.
- **AC-8** First order in a new category raises `NEW_CATEGORY`; with under 4 data points the check reports
  `insufficient_history` and no warning.
- **AC-9** With `FORECAST_ENABLED=true` and a stub forecaster the band comes from it (`band_source: forecast`); a
  forecaster error falls back to `statistical`.
- **AC-10** The demo path (customer 1, 1000 L lactose-free milk → 464306, 1,010 €, CONFIRMED for
  `hotel-chain-agent`) raises no anomaly and stays unchanged.
