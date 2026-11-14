# Review fixes — Junction trusted trade gateway

All 31 findings are addressed below. Findings 20–23 describe functionality that was already present in this checkout; it was verified and hardened rather than implemented a second time. Tests use isolated databases, synthetic HTTP responses and in-process services. No live demo state or frontend files were changed; no commit was created.

## Findings

| Finding | Status and change | Regression / acceptance proof |
|---|---|---|
| 1 | Fixed / verified: Current substitution profile re-evaluated under placement lock using signed comparison facts and locked prices. | [test_review_current_rules_after_quote](tests/test_hospital_substitution.py) |
| 2 | Fixed / verified: Exact rational ingredient demand and density conversions aggregate before one pack ceiling; AC-3 strengthened. | [test_review_shared_sku_exact_demand_rounds_once](tests/test_basket_recipe.py) |
| 3 | Fixed / verified: Signed physical pack content and cost remain independent of SALES_UNIT requests; historical missing evidence requires review. | [test_review_sales_unit_template_detects_physical_shrink](tests/test_recurring_drift.py) |
| 4 | Fixed / verified: Current substitution rules also refresh during approval; new evidence returns to the reviewer. | [test_review_profile_tightening_during_approval_revises_evidence](tests/test_hospital_substitution.py) |
| 5 | Fixed / verified: Known-stock basket shortages force review before signing and reserve none, even when PARTIAL_STOCK is ignored. Unknown-stock exception retained. | [test_review_basket_shortage_cannot_confirm_when_warning_ignored](tests/test_basket_recipe.py) |
| 6 | Fixed / verified: Shared policy evaluator enforces current substitution approval and preserves its rule hit. | [test_review_current_rules_after_quote](tests/test_hospital_substitution.py) |
| 7 | Fixed / verified: Recurring creation uses principal/payload-bound shared commands; REST/MCP replay rechecks current authority. | [test_review_recurring_create_retry_uses_shared_command](tests/test_recurring_drift.py) |
| 8 | Fixed / verified: Shared placement/approval evaluation reads the current profile, including price, pack and brand restrictions. | [test_review_current_rules_after_quote](tests/test_hospital_substitution.py) |
| 9 | Fixed / verified: SDK REQUEST_TIMEOUT uses bounded same-arguments retry after committed placement; other protocol errors propagate. | [test_ac_7_reference_run_prints_receipt_and_recovers_lost_placement](tests/test_exception_journey.py) |
| 10 | Fixed / verified: Function product type supplies eligible categories; preferred category remains a closeness ranking factor. | [test_review_cross_category_functional_substitute_is_screened](tests/test_hospital_substitution.py) |
| 11 | Fixed / verified: Strengthened AC-1 exercises OpenAIDecisions with synthetic HTTP responses and checks outgoing intended-use instructions. | [test_AC_1_stockout_offers_functional_substitute_with_allergen_evidence](tests/test_hospital_substitution.py) |
| 12 | Fixed / verified: Ingredient coverage divides available content by unrounded shared demand; offer coverage still describes purchased packs. | [test_review_ingredient_coverage_uses_unrounded_demand](tests/test_basket_recipe.py) |
| 13 | Fixed / verified: Catch-up retains the missed occurrence key but uses an execution-relative valid delivery date. | [test_review_overdue_weekly_catchup_executes_once](tests/test_recurring_drift.py) |
| 14 | Fixed / verified: Placement-refreshed delivery evidence drives drift policy, receipt and the exact persisted run comparison. | [test_review_placement_delivery_is_compared](tests/test_recurring_drift.py) |
| 15 | Fixed / verified: Strengthened AC-8 freezes actual scheduler time around DST, checks no early purchase, exact firing, retry deduplication and catch-up. | [test_ac_8_dst_preserves_local_time_and_catch_up_once](tests/test_recurring_drift.py) |
| 16 | Fixed / verified: Proposal webhooks require payload channel consent and current buyer preferences; AC-1 now tests the actual webhook consumer. | [test_review_proposal_webhook_requires_channel_consent](tests/test_supplier_proposals.py) |
| 17 | Fixed / verified: Equivalent campaigns reuse their ID and process new or previously held customers; delivered customers deduplicate individually. | [test_review_campaign_merge_delivers_to_new_targets](tests/test_supplier_proposals.py) |
| 18 | Fixed / verified: Decline and mute forward Agent-Mandate, including mandate-only buyer agents. | [test_review_proposal_actions_forward_signed_mandate](tests/test_supplier_proposals.py) |
| 19 | Fixed / verified: Strengthened AC-6 expires the original campaign before resubmission and verifies the explicit muted-campaign reason. | [test_ac_6_mute_and_equivalent_campaign_merge_and_decline_counts](tests/test_supplier_proposals.py) |
| 20 | Fixed / verified: Already implemented in the starting checkout; confirmed purchase aggregation, cadence/confidence, horizon listing and customer scope verified through AC-1–3. | [test_ac_1_six_monthly_purchases_form_high_confidence_calendar_pattern_and_demo](tests/test_purchase_memory.py) |
| 21 | Fixed / verified: Existing prepare/recurring/dismiss/snooze workflows verified through AC-4–6 and placed behind keyed atomic command/replay authorization. | [test_review_suggestion_action_replay_is_atomic_and_authorized](tests/test_purchase_memory.py) |
| 22 | Fixed / verified: All four MCP recurring tools already registered; actual denied creation recovery verified and shared keyed REST/MCP creation added. | [test_ac_7_mcp_create_recurring_requires_permission_and_returns_recovery](tests/test_purchase_memory.py) |
| 23 | Fixed / verified: Existing admin-only persisted memory controls and recorded forgetting verified through AC-6; agent reads/replays additionally obey current scope. | [test_ac_6_memory_controls_freeze_updates_and_persist_snooze_dismiss_exclusions_and_forget](tests/test_purchase_memory.py) |
| 24 | Fixed / verified: Existing five audit/event producers verified; suggestion event subscription types and buyer visibility added, with real signed webhook delivery in AC-8. | [test_ac_8_each_suggestion_action_is_audited_and_emitted_in_scoped_durable_outbox](tests/test_purchase_memory.py) |
| 25 | Fixed / verified: Same per-customer campaign-merge fix as finding 17, including newly opted-in previously held recipients. | [test_review_merged_campaign_rechecks_previously_held_customer](tests/test_supplier_proposals.py) |
| 26 | Fixed / verified: Advertised products explicitly enter normal constrained/mandated fit and live-evidence lookup before shortlist truncation. | [test_review_proposal_accept_includes_advertised_sku_before_truncation](tests/test_supplier_proposals.py) |
| 27 | Fixed / verified: Recurring templates retain every approved product/quantity; historical templates recover approved quantities from their stored baseline. | [test_review_approved_quantity_and_multiple_lines_are_retained](tests/test_recurring_drift.py) |
| 28 | Fixed / verified: Every webhook attempt, including retry/redelivery, rechecks current webhook consent as well as original event channels. | [test_review_proposal_retry_rechecks_current_webhook_consent](tests/test_supplier_proposals.py) |
| 29 | Fixed / verified: Target fan-out is capped at 100 distinct customers across merged submissions; identical held outcomes append no repeated logs/audits. | [test_review_duplicate_targets_are_bounded_without_repeat_log_writes](tests/test_supplier_proposals.py) |
| 30 | Fixed / verified: Ancient cadence history is skipped arithmetically outside the writer lock; cursor/cadence/baseline are revalidated under lock. | [test_review_ancient_schedule_does_not_enumerate_under_writer](tests/test_recurring_drift.py) |
| 31 | Fixed / verified: Same channel-specific consent enforcement as findings 16/28, while retaining the domain event for inbox/other consumers. | [test_review_proposal_webhook_requires_channel_consent](tests/test_supplier_proposals.py) |

## Class-level checks

- Current authority: `test_review_current_rules_after_quote` covers post-quote profile approval/price/brand/pack, policy limit, standing constraint and clinical review changes. `test_review_profile_tightening_during_approval_revises_evidence` proves spec 17 refresh. `test_review_proposal_quote_rechecks_revoked_buyer_consent` covers consent withdrawal on a replacement quote. Existing mandate revocation and standing-constraint acceptance tests remain intact.
- Shared command framework: recurring create/baseline/run/pause, proposal submit/decisions/preferences, memory actions/preferences and REST/MCP basket lookup forward keys. Cross-transport recurring replay, proposal decision replay and suggestion replay tests prove one persisted result and current authority checks. Existing basket/quote/order command tests remain. `test_review_recurring_replay_uses_bound_signed_mandate` proves headerless replay from a bound buyer VC without a policy agent row; `test_review_preparation_replay_survives_a_new_purchase` proves a new pattern ID does not invalidate the original saved preparation.
- Historical data: `test_review_historical_substitution_evidence_loads_and_fails_closed`, `test_review_historical_sales_unit_template_loads_without_inventing_content`, `test_review_approved_quantity_and_multiple_lines_are_retained`, and `test_review_historical_long_lookup_and_quote_keep_loading`. Missing historical evidence is never reconstructed from current supplier facts.
- Input/failure bounds: `test_review_request_text_fields_are_bounded` and `test_review_failure_audit_bounds_entire_request`; failure summaries remain inside the hash-chained audit, verified in the regression.

## Acceptance criterion mapping

### Spec 22 — test_shared_foundation.py

| AC | Test |
|---|---|
| AC-1 | [test_ac_1_rest_mcp_same_policy_and_single_events](tests/test_shared_foundation.py) |
| AC-2 | [test_ac_2_cross_transport_principal_scoped_payload_binding](tests/test_shared_foundation.py) |
| AC-3 | [test_ac_3_audit_failure_rolls_back_state_events_and_command](tests/test_shared_foundation.py) |
| AC-4 | [test_ac_4_revisions_outbox_delivery_and_retry](tests/test_shared_foundation.py) |
| AC-5 | [test_ac_5_correlation_and_linked_identifiers](tests/test_shared_foundation.py) |
| AC-6 | [test_ac_6_public_pinned_compatibility_manifest](tests/test_shared_foundation.py) |
| AC-7 | [test_ac_7_buyer_and_supplier_principals_and_key_provisioning](tests/test_shared_foundation.py) |

### Spec 30 — test_exception_journey.py

| AC | Test |
|---|---|
| AC-1 | [test_ac_1_split_plan_waits_with_reason_interval_and_shortage](tests/test_exception_journey.py) |
| AC-2 | [test_ac_2_approval_increases_revision_and_receipt_verifies](tests/test_exception_journey.py) |
| AC-3 | [test_ac_3_rejection_keeps_comment_and_stops](tests/test_exception_journey.py) |
| AC-4 | [test_ac_4_expiry_while_waiting_requires_lookup](tests/test_exception_journey.py) |
| AC-5 | [test_ac_5_receipt_before_decision_returns_not_decided](tests/test_exception_journey.py) |
| AC-6 | [test_ac_6_changed_evidence_increases_revision_and_stays_pending](tests/test_exception_journey.py) |
| AC-7 | [test_ac_7_reference_run_prints_receipt_and_recovers_lost_placement](tests/test_exception_journey.py) |
| AC-8 | [test_ac_8_restarted_client_polls_same_order_to_same_outcome](tests/test_exception_journey.py) |

### Spec 31 — test_hospital_substitution.py

| AC | Test |
|---|---|
| AC-1 | [test_AC_1_stockout_offers_functional_substitute_with_allergen_evidence](tests/test_hospital_substitution.py) |
| AC-2 | [test_AC_2_cheaper_may_contain_candidate_excluded_and_counted](tests/test_hospital_substitution.py) |
| AC-3 | [test_AC_3_lactose_free_does_not_prove_milk_allergen_absence](tests/test_hospital_substitution.py) |
| AC-4 | [test_AC_4_missing_allergen_data_is_not_offered](tests/test_hospital_substitution.py) |
| AC-5 | [test_AC_5_clinical_review_requires_scoped_buyer_approver](tests/test_hospital_substitution.py) |
| AC-6 | [test_AC_6_available_preferred_product_is_best_without_substitution](tests/test_hospital_substitution.py) |
| AC-7 | [test_AC_7_excess_price_increase_is_flagged_and_requires_approval](tests/test_hospital_substitution.py) |

### Spec 32 — test_basket_recipe.py

| AC | Test |
|---|---|
| AC-1 | [test_AC_1_five_ingredients_one_basket_two_supplier_calls_and_transport_parity](tests/test_basket_recipe.py) |
| AC-2 | [test_AC_2_servings_scale_rounding_overbuy_total_and_policy](tests/test_basket_recipe.py) |
| AC-3 | [test_AC_3_shared_sku_sums_quantities_before_pack_rounding](tests/test_basket_recipe.py) |
| AC-4 | [test_AC_4_no_compliant_product_is_uncovered_partial_basket_needs_approval](tests/test_basket_recipe.py) |
| AC-5 | [test_AC_5_standing_allergen_exclusion_applies_to_every_ingredient](tests/test_basket_recipe.py) |
| AC-6 | [test_AC_6_basket_quote_order_reserves_all_lines_or_none](tests/test_basket_recipe.py) |
| AC-7 | [test_AC_7_grams_to_litres_without_density_is_uncovered](tests/test_basket_recipe.py) |
| AC-1 | [test_ac_1_five_ingredients_two_supplier_calls](tests/test_basket_recipe.py) |
| AC-2 | [test_ac_2_servings_scale_rounding_overbuy_total_and_policy](tests/test_basket_recipe.py) |
| AC-3 | [test_ac_3_duplicate_skus_share_summed_line](tests/test_basket_recipe.py) |
| AC-4 | [test_ac_4_uncovered_requires_approval](tests/test_basket_recipe.py) |
| AC-5 | [test_ac_5_standing_exclusions_apply_to_every_ingredient](tests/test_basket_recipe.py) |
| AC-6 | [test_ac_6_basket_quote_order_reserves_all_or_none](tests/test_basket_recipe.py) |
| AC-7 | [test_ac_7_grams_to_litres_without_density_uncovered](tests/test_basket_recipe.py) |

### Spec 35 — test_recurring_drift.py

| AC | Test |
|---|---|
| AC-1 | [test_ac_1_identical_runs_confirm_without_drift](tests/test_recurring_drift.py) |
| AC-2 | [test_ac_2_third_price_step_exceeds_cumulative_tolerance](tests/test_recurring_drift.py) |
| AC-3 | [test_ac_3_smaller_pack_shows_conversion_and_requires_review](tests/test_recurring_drift.py) |
| AC-4 | [test_ac_4_duplicate_occurrence_creates_one_order_even_after_restart](tests/test_recurring_drift.py) |
| AC-5 | [test_ac_5_missing_comparison_evidence_needs_approval](tests/test_recurring_drift.py) |
| AC-6 | [test_ac_6_buyer_baseline_reset_clears_cumulative_drift_agent_forbidden](tests/test_recurring_drift.py) |
| AC-7 | [test_ac_7_revoked_mandate_and_suspension_skip_with_event](tests/test_recurring_drift.py) |
| AC-8 | [test_ac_8_dst_preserves_local_time_and_catch_up_once](tests/test_recurring_drift.py) |

### Spec 36 — test_supplier_proposals.py

| AC | Test |
|---|---|
| AC-1 | [test_ac_1_opt_in_delivery_inbox_mcp_and_outbox_unconsented_recorded](tests/test_supplier_proposals.py) |
| AC-2 | [test_ac_2_standing_allergen_exclusion_filters_before_delivery](tests/test_supplier_proposals.py) |
| AC-3 | [test_ac_3_accept_uses_buyer_limits_and_revalidates_stock](tests/test_supplier_proposals.py) |
| AC-4 | [test_ac_4_expired_refused_changed_price_requoted_without_purchase](tests/test_supplier_proposals.py) |
| AC-5 | [test_ac_5_third_weekly_proposal_held_and_policy_quotas_quiet_hours](tests/test_supplier_proposals.py) |
| AC-6 | [test_ac_6_mute_and_equivalent_campaign_merge_and_decline_counts](tests/test_supplier_proposals.py) |
| AC-7 | [test_ac_7_supplier_cannot_read_buyer_spend_limits_preferences_or_inbox](tests/test_supplier_proposals.py) |
| AC-8 | [test_ac_8_buyer_agent_cannot_submit_supplier_proposals](tests/test_supplier_proposals.py) |

### Spec 41 — test_purchase_memory.py

| AC | Test |
|---|---|
| AC-1 | [test_ac_1_six_monthly_purchases_form_high_confidence_calendar_pattern_and_demo](tests/test_purchase_memory.py) |
| AC-2 | [test_ac_2_two_orders_have_no_pattern_irregular_intervals_explain_low_confidence](tests/test_purchase_memory.py) |
| AC-3 | [test_ac_3_horizon_scopes_agents_buyers_operator_and_mcp_without_cross_org_leaks](tests/test_purchase_memory.py) |
| AC-4 | [test_ac_4_prepare_only_looks_up_then_normal_quote_and_order_apply_mandate_and_approval](tests/test_purchase_memory.py) |
| AC-5 | [test_ac_5_make_recurring_uses_pattern_baseline_normal_runs_and_drift_and_mcp_pause](tests/test_purchase_memory.py) |
| AC-6 | [test_ac_6_memory_controls_freeze_updates_and_persist_snooze_dismiss_exclusions_and_forget](tests/test_purchase_memory.py) |
| AC-7 | [test_ac_7_mcp_create_recurring_requires_permission_and_returns_recovery](tests/test_purchase_memory.py) |
| AC-8 | [test_ac_8_each_suggestion_action_is_audited_and_emitted_in_scoped_durable_outbox](tests/test_purchase_memory.py) |

### Spec 33 — test_webhooks.py

| AC | Test |
|---|---|
| AC-1 | [test_ac_1_pending_order_one_valid_signed_delivery](tests/test_webhooks.py) |
| AC-2 | [test_ac_2_retry_grows_recovery_and_receiver_deduplicates](tests/test_webhooks.py) |
| AC-3 | [test_ac_3_tampering_fails_published_key_verification](tests/test_webhooks.py) |
| AC-4 | [test_ac_4_unsafe_destinations_rebinding_challenge_and_redirects](tests/test_webhooks.py) |
| AC-5 | [test_ac_5_exhausted_failed_manual_redelivery_and_crud](tests/test_webhooks.py) |
| AC-6 | [test_ac_6_customer_and_subscription_ownership_isolation](tests/test_webhooks.py) |
| AC-7 | [test_ac_7_revision_bound_authenticated_review](tests/test_webhooks.py) |

## Decisions taken

1. Follow spec 39 R-2 over the broad proposed fix in finding 5: unknown-stock lines may confirm without local reservations. Known-stock shortages always require basket review and hold no partial reservation.
2. Follow spec 17 R-1: evidence revision starts at 1 at placement. Current rules apply immediately; approval-time changes increment the revision and require renewed review.
3. Preserve signed price promises. Current substitution policy uses structured quote-time comparison evidence; missing historical evidence requires review instead of parsing descriptive text or fetching a new comparison price. New request text bounds do not invalidate persisted lookup text.
4. Compare signed physical content and physical-unit cost independently of requested sales units. Historical SALES_UNIT quotes without physical content remain readable, with a missing-evidence review condition.
5. Retain approved purchased quantities and all product lines, including overbuy, rather than reconstructing quantities from the original lookup need. Historical recurring rows use the baseline quantities when approved_lines is absent.
6. Catch-up uses the execution date plus the approved delivery lead, while retaining the missed occurrence key. Cadence phase and local DST time survive bounded recent-window calculation outside the writer lock. Monthly lookback retains eight cadence periods so skipped month days and the Gregorian century leap gap do not discard the latest missed occurrence (`test_review_bounded_monthly_schedule_preserves_skipped_calendar_dates`). RRULE starts round fractional seconds upward to its supported second precision.
7. Bound free-text inputs to 4,000 characters and text collections to 100 entries (existing narrower bounds remain). Target fan-out is limited to 100 distinct buyers per campaign across merges; keys to 200 characters; revocation JWTs to 32,768. Failure audit summaries are bounded separately.
8. Keep proposal domain events even for inbox-only consent. Only webhook dispatch is suppressed, with consent checked on every delivery attempt. Previously held campaign targets may be re-evaluated after consent changes without repeatedly logging an identical refusal.
9. Preserve the webhook ownership AC-6 assertions, and give its synthetic proposal fixture explicit current buyer consent and payload.channels. Its previous expectation to send a consent-less proposal conflicts with spec 36.
10. Keep the required existing REST route inventory: pausing remains available through the specified MCP tool; no extra REST pause endpoint was introduced.

## Validation

- Original implementation baseline: **1203 passed, 1 skipped**. New regressions reproduced the original defects in an isolated temporary copy.
- Final complete run: `.venv/bin/python -m pytest tests -q` — **1263 passed, 1 skipped in 921.13s (15:21)**, exit 0.
- Final `.venv/bin/python scripts/export_openapi.py` — **78 paths exported** to `contracts/openapi.yaml`.
- Generated MCP documentation is current; its schema-equivalence acceptance test passed in the complete suite.
- Focused command/replay group: 152 passed; webhook/proposal group: 44 passed; recurring group: 31 passed. These groups overlap with the complete suite.
- `git diff --check`: clean. No requested work remains. No commit, frontend change or live demo state write was performed.
- Scratch reproduction logs remain outside the repository in the system temporary directory; no `_probe_*` files or `backend/review_evidence/` remain.

## Files changed

- `backend/README.md`
- `backend/REVIEW_FIXES.md`
- `backend/app/api.py`
- `backend/app/audit.py`
- `backend/app/baskets.py`
- `backend/app/buyer.py`
- `backend/app/commands.py`
- `backend/app/lookup.py`
- `backend/app/mcp_server.py`
- `backend/app/models.py`
- `backend/app/orders.py`
- `backend/app/policy.py`
- `backend/app/proposals.py`
- `backend/app/purchase_memory.py`
- `backend/app/recurring.py`
- `backend/app/service.py`
- `backend/app/substitution.py`
- `backend/app/visibility.py`
- `backend/app/webhooks.py`
- `backend/demo/run_exception_journey.py`
- `backend/static/developers.html`
- `backend/static/tools.json`
- `backend/tests/test_basket_recipe.py`
- `backend/tests/test_exception_journey.py`
- `backend/tests/test_hospital_substitution.py`
- `backend/tests/test_purchase_memory.py`
- `backend/tests/test_recurring_drift.py`
- `backend/tests/test_shared_foundation.py`
- `backend/tests/test_supplier_proposals.py`
- `backend/tests/test_webhooks.py`
- `contracts/openapi.yaml`
