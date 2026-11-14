# Independent review remediation

28 findings, 17 distinct defects. No commit, network request or write to backend/var was performed. Concurrent changes outside this work (docs/TASKS.md and docs/specs/README.md) were left alone.

## Verification

- Regression baseline before production edits: 47 failed, 5 passed (96 deselected); review-before.txt. The acceptance coverage defects had correct production behavior and were checked with mutations instead.
- Final command from backend/: `.venv/bin/python -m pytest tests -q` -> **315 passed, 1 skipped in 32.35s**; final-suite.txt.
- The only skip is spec21 local Anvil: this sandbox rejects binding 127.0.0.1 with PermissionError. The optional integration test now skips explicitly only for that permission failure; it still runs normally where listeners are available. Other anchoring tests use respx and pass.
- OpenAPI export: `.venv/bin/python scripts/export_openapi.py` (23 paths); contract equality tests passed.
- Strengthened AC-7, AC-11 and AC-16 each fail their corresponding injected regression: mutation-ac7.txt, mutation-ac11.txt, mutation-ac16.txt. The test harnesses mutate only their Python process, never production files.
- Fresh live AC-16 verification remains pending: no network is available. The fixture is a projection of the pre-existing live gateway lookup capture backend/var/l3.json, not a claim that raw upstream responses were captured. Only the ten captured candidates carry replayed fit/price/stock facts; absent candidates receive no fit answer. Times are rebased.

## Findings

| Finding | Status | Change | Spec / AC | Regression or strengthened acceptance test (under backend/tests/) |
|---|---|---|---|---|
| 1 | Fixed | Standing customer_policy violations reject regardless of the generic toggle, at placement and approval. | 16 R-2; AC-2, AC-5 | test_standing_constraints.py::test_review_standing_constraints_cannot_be_approved |
| 2 | Fixed | Legacy unbound quotes evaluate the current customer constraints and retain recorded request violations. | 16 R-2/R-4; AC-4, AC-5 | test_standing_constraints.py::test_review_legacy_unbound_quote_checks_current_customer |
| 3 | Fixed | Compare all compatible name-size candidates; real 460086 keeps its 0.5 kg package (2 packs / EUR 4). | 05 R-2; 15 R-8 | test_size_reconciliation.py::test_review_portion_weight_preserves_package_weight |
| 4 | Fixed | Same legacy-quote correction as finding 2. | 16 R-2/R-4 | test_standing_constraints.py::test_review_legacy_unbound_quote_checks_current_customer |
| 5 | Fixed | Persisted stats lookups load with stored context after their delivery date. | 15 R-5 | test_control_plane.py::test_review_historical_lookup_stats |
| 6 | Fixed | Same historical stats correction as finding 5. | 03 R-9; AC-9 | test_control_plane.py::test_review_historical_lookup_stats |
| 7 | Fixed | Same legacy-quote correction as finding 2, including pending approval. | 16 AC-5 | test_standing_constraints.py::test_review_legacy_unbound_quote_checks_current_customer |
| 8 | Fixed | Strict numeric preference min/max/price and strict max_candidates over both REST and MCP. | 15 R-5; AC-5 | test_evidence_completeness.py::test_review_strict_preference_numbers |
| 9 | Fixed | Use valid CASE sales content independently of a base-unit multiplier (L and KG). | 15 R-8 | test_evidence_completeness.py::test_review_case_content_needs_no_multiplier |
| 10 | Coverage fixed; fresh live verification pending | Keep synthetic smoke separate; strengthen AC-16 with all 17,546 real catalog products and recorded demo gateway evidence. Raw upstream HTTP was not retained; replay responses are projections of the capture and time windows are rebased. No fresh live check is possible without network. | 15 AC-16 | test_evidence_completeness.py::test_ac_16_full_catalog_replays_recorded_demo |
| 11 | Fixed | Strengthen AC-7 with request-only, customer-only and allowed products; assert surviving IDs and both counts. | 16 AC-7 | test_standing_constraints.py::test_ac_7_request_and_customer_exclusion_union_counts_both_sources |
| 12 | Fixed | Remove singular/plural written kilogram, gram and millilitre/milliliter aliases before broadening. | 18 R-2 | test_lookup_fallback.py::test_review_written_units_are_not_search_terms |
| 13 | Fixed | Normalize unknown-size offers against their own valid unit prices, retaining warning/orderability restrictions. | 20 R-2 | test_total_cost_ranking.py::test_review_unknown_sizes_compare_unit_prices |
| 14 | Fixed | Evaluate PARTIAL_STOCK after split allocation; complete splits survive, truly partial plans still reject. | 08 R-1/R-2; AC-2, AC-3 | test_exceptions.py::test_review_reject_partial_stock_keeps_complete_split |
| 15 | Fixed | Independent volume/weight suspicion, nullable legacy fields and recomputation preserve real 466324 weight. | 15 R-8 | test_evidence_completeness.py::test_review_bad_volume_preserves_valid_weight |
| 16 | Fixed | Same CASE-content correction as finding 9. | 15 R-8 | test_evidence_completeness.py::test_review_case_content_needs_no_multiplier |
| 17 | Fixed | Same unknown-size price correction as finding 13. | 20 R-2 | test_total_cost_ranking.py::test_review_unknown_sizes_compare_unit_prices |
| 18 | Fixed | Only legacy documents lacking ignored_warnings acquire the evidence defaults; accepted current edits persist. | 15 R-14 | test_control_plane.py::test_review_current_policy_edits_survive_reload |
| 19 | Fixed | Use json_each term-hit aggregation instead of one UNION ALL SELECT per term, preserving all terms and ordering. | 18 R-2 | test_lookup_fallback.py::test_review_long_broadening_does_not_hit_sqlite_limit |
| 20 | Fixed | Same SQL aggregation correction as finding 19; no arbitrary truncation or new request limit. | 18 R-2 | test_lookup_fallback.py::test_review_long_broadening_does_not_hit_sqlite_limit |
| 21 | Fixed | Fill enumeration and cheapest search share the node budget, with bounded valid greedy seeds at budget zero. Direct preferred/mixed pools (25/40) and a real mocked 40-candidate lookup are covered. | 08 R-10; AC-11/AC-12 | test_exceptions.py::test_review_all_planning_searches_share_node_budget; test_review_expanded_lookup_pool_keeps_planning_bounded |
| 22 | Fixed | Same policy migration/update correction as finding 18; response, GET, reload and audit agree. | 03 R-6; AC-6 | test_control_plane.py::test_review_current_policy_edits_survive_reload |
| 23 | Fixed | Resolve credentials before cancellation-validation audit: missing/unknown credentials give 401; authenticated malformed JSON gives 422. | 19 R-3; AC-1 | test_authentication.py::test_review_malformed_cancellation_authenticates_before_audit |
| 24 | Fixed | AC-11 now calls get_order_status via the actual --agent-id agent-b subprocess, verifies agent-b ownership and refuses another agent order. | 19 R-7; AC-11 | test_authentication.py::test_ac_11_mcp_transports |
| 25 | Fixed | Same authentication and safe principal handling as finding 23; absent principal never crashes the audit helper. | 19 R-3 | test_authentication.py::test_review_malformed_cancellation_authenticates_before_audit |
| 26 | Fixed | Same actual-CLI ownership acceptance assertions as finding 24. | 19 AC-11 | test_authentication.py::test_ac_11_mcp_transports |
| 27 | Fixed | Same authenticated cancellation-validation correction as finding 23. | 19 R-3 | test_authentication.py::test_review_malformed_cancellation_authenticates_before_audit |
| 28 | Fixed | Same structured validation correction as finding 23; spec19 requires 401 for unauthenticated callers rather than the older proposed 422. | 04 R-7; 19 R-3 | test_authentication.py::test_review_malformed_cancellation_authenticates_before_audit |

## Decisions taken

1. Spec16 standing buyer constraints take precedence over spec15 generic request-constraint approval toggle. Request-only violations retain the documented approval option.
2. Legacy quotes remain readable and usable subject to current customer constraints, rather than requiring blanket replacement; original unbound request violations are retained. Signature checks are preserved.
3. Existing approval.ignored_warnings identifies a current policy document; absent field identifies a legacy evidence policy for default-code migration. Current explicit warning/rejection edits are never silently restored.
4. Spec18 R-2 requires meaningful complete deterministic retrieval. json_each aggregation removes the SQLite compound-select limit while preserving every term and distinct-word ordering; the suggested arbitrary 422 limit was unnecessary.
5. Per-dimension suspicion fields are additive and nullable. Old JSON remains readable; missing flags trigger dimension-specific recalculation from retained evidence.
6. Spec19 requires authentication for cancellation even if JSON parsing precedes dependencies. Missing/unknown credentials return 401; valid credentials return structured 422. This follows spec19 over the older finding28 proposal of unauthenticated 422.
7. Preserve a valid, bounded greedy fallback outside combinatorial search; all recursive fill/cheapest visits share one budget, including preferred/mixed groups.
8. An offline replay proves retained evidence, real ingestion, retrieval and ordering; it cannot replace a new live acceptance run.

## Files changed

- Production: backend/app/{api,catalog,db,lookup,mcp_server,models,orders,plans,policy,stats}.py.
- Tests: backend/tests/{conftest,test_authentication,test_control_plane,test_evidence_completeness,test_exceptions,test_lookup_fallback,test_size_reconciliation,test_standing_constraints,test_total_cost_ranking}.py.
- Environment-only optional-integration handling: backend/tests/test_audit_anchoring.py.
- Replay: backend/tests/fixtures/demo_lookup_capture.json.
- Documentation/contract: backend/README.md, contracts/openapi.yaml.
- Review evidence: backend/review_evidence/.

## Acceptance criteria to tests

Mappings below retain existing tests, add review regressions and identify live-only criteria explicitly.

### Spec 03

| AC | Tests |
|---|---|
| AC-1 | `test_orders_policy.py::test_policy_all_hits` |
| AC-2 | `test_orders_policy.py::test_no_fit_needs_approval_and_no_override_of_sale_block` |
| AC-3 | `test_orders_policy.py::test_hard_constraint_violation_rejected` |
| AC-4 | `test_orders_policy.py::test_approval_transitions` |
| AC-5 | `test_orders_policy.py::test_pending_expiry_releases_stock` |
| AC-6 | `test_control_plane.py::test_policy_update_reset_audit_and_reload` |
| AC-7 | `test_control_plane.py::test_policy_validation_no_side_effects`; `test_control_plane.py::test_policy_zero_weights_and_blank_human`; `test_control_plane.py::test_policy_persistence_failure_rolls_back_audit` |
| AC-8 | `test_orders_policy.py::test_audit_is_append_only` |
| AC-9 | `test_control_plane.py::test_stats_small_flow_and_since`; `test_control_plane.py::test_stats_rejections_legacy_placements_and_timezone`; `test_control_plane.py::test_stats_top_five_warnings_and_rules` |

Review additions for AC-6 and AC-9: `test_review_current_policy_edits_survive_reload`, `test_review_historical_lookup_stats` in test_control_plane.py.

### Spec 04

File: `backend/tests/test_order_cancel.py`.

| AC | Tests |
|---|---|
| AC-1 | `test_cancel_ac1_human_confirmed_order`; `test_cancel_ac1_missing_cutoffs_do_not_block` |
| AC-2 | `test_cancel_ac2_stock_reservation_released` |
| AC-3 | `test_cancel_ac3_cutoff_passed` |
| AC-4 | `test_cancel_ac4_invalid_transition` |
| AC-5 | `test_cancel_ac5_unknown_order` |
| AC-6 | `test_cancel_ac6_blank_cancelled_by` |
| AC-7 | `test_cancel_ac7_success_and_failure_audit` |
| AC-8 | `test_cancel_ac8_mcp_owner_and_other_agent`; `test_cancel_ac8_mcp_transport_exposes_owner_error` |
| AC-9 | `test_cancel_ac9_stats_exclude_cancelled_spend` |
| AC-10 | `test_cancel_ac10_generated_openapi_contract` |

### Spec 05

File: `backend/tests/test_size_reconciliation.py`.

| AC | Tests |
|---|---|
| AC-1 | `test_size_ac1_name_exact_confirmed_200_units` |
| AC-2 | `test_size_ac2_novobox_exact_50_units` |
| AC-3 | `test_size_ac3_catalog_beats_name` |
| AC-4 | `test_size_ac4_mismatch_visible_and_requires_approval` |
| AC-5 | `test_size_ac5_ratio_only_unchanged` |
| AC-6 | `test_size_ac6_price_error_keeps_unconfirmed_name` |
| AC-7 | `test_size_ac7_policy_and_generated_contract` |
| AC-9 | `test_size_ac9_kg_sold_is_one_kg_per_unit` |
| AC-10 | `test_size_ac10_pack_count_in_name` |
| AC-8 | Live-only criterion from existing spec; not reverified offline. |

### Spec 08

File: `backend/tests/test_exceptions.py`.

| AC | Tests |
|---|---|
| AC-1 | `test_exceptions_ac_1_single_preview_identity_no_side_effects_and_legacy` |
| AC-2 | `test_exceptions_ac_2_split_whole_units_cheapest_and_bounded` |
| AC-3 | `test_exceptions_ac_3_partial_retains_uncovered_risk_through_order` |
| AC-4 | `test_exceptions_ac_4_preview_uses_shared_limits_and_placement_is_authoritative` |
| AC-5 | `test_exceptions_ac_5_brand_substitute_ranking_approval_and_old_policy` |
| AC-6 | `test_exceptions_ac_6_pack_price_and_label_differences_are_explained` |
| AC-7 | `test_exceptions_ac_7_plan_ref_rest_mcp_and_validation` |
| AC-8 | `test_exceptions_ac_8_no_hard_match_explains_constraint_counts` |
| AC-9 | `test_exceptions_ac_9_plans_use_only_priced_offers_without_extra_calls` |
| AC-11 | `test_exceptions_ac_11_stock_limited_search_is_fast_and_valid` |
| AC-12 | `test_exceptions_ac_12_tied_fractional_prices_are_fast_and_valid` |
| AC-13 | `test_exceptions_ac_13_preferred_split_precedes_cheaper_substitute` |
| AC-14 | `test_exceptions_ac_14_rejects_customer_outside_mandate_before_preview` |
| AC-15 | `test_exceptions_ac_15_kg_plans_follow_fractional_lots` |
| AC-16 | `test_exceptions_ac_16_planning_does_not_hold_writer_lock` |
| AC-10 | Live-only criterion from existing spec; not reverified offline. |

### Spec 15

File: `backend/tests/test_evidence_completeness.py`.

| AC | Tests |
|---|---|
| AC-1 | `test_ac_1_exclusion_requires_declaration_or_uncontradicted_free_from` |
| AC-2 | `test_ac_2_x99_is_only_a_declaration_marker` |
| AC-3 | `test_ac_3_claim_contradictions_are_not_proof` |
| AC-4 | `test_ac_4_codes_normalize_and_unknowns_have_valid_values_rest_and_mcp` |
| AC-5 | `test_ac_5_request_bounds_rest_and_mcp_match` |
| AC-6 | `test_ac_6_invalid_prices_and_missing_currency_cannot_be_quoted` |
| AC-7 | `test_ac_7_unconfirmed_size_requires_plan_approval` |
| AC-8 | `test_ac_8_suspect_catalog_size_uses_name_and_case_units_convert` |
| AC-9 | `test_ac_9_schedule_unit_mismatch_counts_zero` |
| AC-10 | `test_ac_10_unknown_stock_is_finite_and_no_stock_rejects` |
| AC-11 | `test_ac_11_missing_cutoff_needs_approval_and_naive_time_is_helsinki` |
| AC-12 | `test_ac_12_unorderable_offers_have_no_rank_label_and_plans_use_full_pool` |
| AC-13 | `test_ac_13_missing_ranking_data_uses_global_median_and_fit_missing_sorts_last` |
| AC-14 | `test_ac_14_policy_fails_closed_and_protected_warnings_cannot_be_ignored` |
| AC-15 | `test_ac_15_build_fails_loudly_skips_deleted_and_reports_coverage` |
| AC-16 | `test_ac_16_full_catalog_replays_recorded_demo` |

AC-16 is now full-catalog recorded replay; current live acceptance is pending. The separate `test_demo_smoke_still_buys_464306_for_1010` retains the synthetic smoke. Review strictness extends AC-5; CASE and independent dimensions extend AC-7/8.

### Spec 16

File: `backend/tests/test_standing_constraints.py`.

| AC | Tests |
|---|---|
| AC-1 | `test_ac_1_lookup_unions_standing_claims_and_shows_sources` |
| AC-2 | `test_ac_2_explicit_line_cannot_escape_standing_claim` |
| AC-3 | `test_ac_3_every_quote_requires_stored_request_and_plan_ref_binds_it` |
| AC-4 | `test_ac_4_current_policy_added_after_quote_rejects_at_placement` |
| AC-5 | `test_ac_5_current_policy_blocks_pending_approval_but_allows_rejection` |
| AC-6 | `test_ac_6_customer_policy_get_put_preservation_vocabulary_and_audit` |
| AC-7 | `test_ac_7_request_and_customer_exclusion_union_counts_both_sources` |

AC-2/5 add `test_review_standing_constraints_cannot_be_approved`; AC-4/5 add `test_review_legacy_unbound_quote_checks_current_customer`. AC-7 was strengthened in place.

### Spec 18

File: `backend/tests/test_lookup_fallback.py`.

| AC | Tests |
|---|---|
| AC-1 | `test_ac_1_intent_broadens_and_preserves_original_fit_question` |
| AC-2 | `test_ac_2_explicit_terms_keep_and_retrieval` |
| AC-3 | `test_ac_3_matching_intent_uses_original_and_mode` |
| AC-4 | `test_ac_4_pool_truncation_reports_all_hard_filtered_matches` |
| AC-5 | `test_ac_5_empty_broadening_names_tried_words_and_suggests_terms` |
| AC-6 | `test_ac_6_broadening_keeps_claim_and_allergen_hard_constraints` |

### Spec 19

File: `backend/tests/test_authentication.py`.

| AC | Tests |
|---|---|
| AC-1 | `test_ac_1_credentials_and_startup` |
| AC-2 | `test_ac_2_identity_and_budget` |
| AC-3 | `test_ac_3_operator_scope` |
| AC-4 | `test_ac_4_object_ownership` |
| AC-5 | `test_ac_5_customer_scope_before_upstream` |
| AC-6 | `test_ac_6_mandate_required` |
| AC-7 | `test_ac_7_honest_operator_actor` |
| AC-8 | `test_ac_8_operator_buyer_identity` |
| AC-9 | `test_ac_9_no_secret_leaks` |
| AC-10 | `test_ac_10_idempotent_key_generation` |
| AC-11 | `test_ac_11_mcp_transports` |

AC-1 adds `test_review_malformed_cancellation_authenticates_before_audit`; AC-11 was strengthened in place and mutation-checked. The malformed cancellation test also covers spec04 R-7.

### Spec 20

File: `backend/tests/test_total_cost_ranking.py`.

| AC | Tests |
|---|---|
| AC-1 | `test_ac_1_purchase_totals_and_overbuy_use_whole_packs` |
| AC-2 | `test_ac_2_price_ranking_follows_total_when_per_litre_order_differs` |
| AC-3 | `test_ac_3_score_breakdown_sums_to_score_and_repeated_ranking_is_stable` |
| AC-4 | `test_ac_4_mocked_demo_best_is_464306_at_1010` |

R-2 adds `test_review_unknown_sizes_compare_unit_prices`. AC-4 remains the existing synthetic smoke; the real catalog replay also runs in spec15 AC-16.
