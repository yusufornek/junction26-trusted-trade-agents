# Independent review fixes

All 23 findings are fixed. Duplicate findings share one correction and regression test.

Validation: `.venv/bin/python -m pytest tests -q` from `backend/`: **356 passed, 1 skipped in 34.64s**. The optional local Anvil test is skipped because sandbox loopback sockets cannot be opened; all mocked RPC tests pass. OpenAPI export succeeded with 23 paths. `git diff --check` passed. No commit was created, no frontend files changed, and no command wrote to `backend/var/`.

## Findings

| Finding | Resolution | Test (under backend/tests/) |
|---|---|---|
| 1 | Fixed: authenticate currency, line money, fit, warnings and complete immutable decision/export evidence. | `test_signed_quotes.py::test_review_quote_immutable_inputs_authenticated` |
| 2 | Fixed: authenticate owner, need binding, policy evidence and currency; validate referenced need and currency consistency. | `test_signed_quotes.py::test_review_quote_immutable_inputs_authenticated` |
| 3 | Fixed: skip timezone-naive expiry during the sweep; placement refuses and audits the changed signature. AC-2 now includes past/future naive timestamps. | `test_signed_quotes.py::test_ac_2_stored_tampering_refused_and_audited` |
| 4 | Fixed: AuditEntry exports the exact hashed timestamp string, including migrated records. | `test_audit_chain.py::test_review_rest_audit_hash_reproduced_from_export; test_existing_rows_migrated_once_with_start_event` |
| 5 | Fixed: AC-2 requires an explicit inventory of mapping rows, including both document identifiers; a mutation test removes the ID rows. | `test_ubl_export.py::test_ac_2_peppol_mapping_and_missing_data_are_explicit; test_review_ac_2_requires_document_id_mapping` |
| 6 | Fixed: owned existing-order replay precedes quote signature verification; retired keys do not block replay. | `test_orders_policy.py::test_review_existing_order_replay_survives_quote_upgrade[retired_key]` |
| 7 | Fixed: same timezone-naive expiry correction as finding 3, with audited HTTP 409 and no upstream calls. | `test_signed_quotes.py::test_ac_2_stored_tampering_refused_and_audited` |
| 8 | Fixed: same exact exported timestamp correction as finding 4; independent REST digest recomputation succeeds. | `test_audit_chain.py::test_review_rest_audit_hash_reproduced_from_export` |
| 9 | Fixed: stream one SQLite read snapshot, retain only requested checkpoint/hash/count/first break, and serialize full scans per gateway. | `test_audit_chain.py::test_review_audit_verify_bounded_memory` |
| 10 | Fixed: existing unsigned/pre-upgrade orders replay without a quote signature; retain ownership, quote/agent identity and replay auditing. | `test_orders_policy.py::test_review_existing_order_replay_survives_quote_upgrade[unsigned]` |
| 11 | Fixed: verify revocations separately against each customer trust set; store/check customer scope. Distinct and colliding key IDs are covered. | `test_verifiable_mandate.py::test_review_revocation_respects_customer_signing_key` |
| 12 | Fixed: persist computed transaction hash and signed bytes before broadcasting; reconcile or rebroadcast identical bytes after uncertainty/restart. | `test_audit_anchoring.py::test_review_ambiguous_broadcast_reuses_signed_transaction` |
| 13 | Fixed: replay revalidates the order-bound mandate against stored customer, agent and lines even without a header or quote-bound mandate. | `test_verifiable_mandate.py::test_review_order_bound_mandate_checked_on_headerless_replay` |
| 14 | Fixed: AC-8 makes a fresh stdio catalog search and asserts mandate ID/limits in audit evidence; removing the CLI argument makes it fail. | `test_verifiable_mandate.py::test_ac_8_stdio_cli_mandate_and_http_header; test_review_ac_8_requires_cli_mandate_file` |
| 15 | Fixed: retain triggers while busy and recheck outstanding work/threshold under synchronization before worker exit. | `test_audit_anchoring.py::test_review_busy_anchor_preserves_new_commit` |
| 16 | Fixed: unresolved failures/receipts bypass unchanged-entry suppression. AC-4 retries via list_orders without adding another lookup. Superseded failures do not retrigger. | `test_audit_anchoring.py::test_review_unresolved_anchor_retries_without_new_entry; test_ac_4_automatic_failure_isolated_and_next_trigger_retries` |
| 17 | Fixed: apply categories in catalog retrieval before counting, truncation and search limits. | `test_verifiable_mandate.py::test_review_category_filter_precedes_limits_and_counts` |
| 18 | Fixed: same durable hash/signed-byte broadcast recovery as finding 12; a fresh worker does not allocate a new nonce. | `test_audit_anchoring.py::test_review_ambiguous_broadcast_reuses_signed_transaction` |
| 19 | Fixed: poll pending receipts in the background independently of business entries/triggers; confirmation updates latest and appends audit evidence once. | `test_audit_anchoring.py::test_review_pending_receipt_confirmed_in_background` |
| 20 | Fixed: stats derives registration and current mandate ID from current policy or a verified trusted/unrevoked/unexpired stored JWT. | `test_verifiable_mandate.py::test_review_stats_registration_uses_current_authority` |
| 21 | Fixed: numeric validation returns false on integer overflow, yielding an audited mandate rejection rather than HTTP 500. | `test_verifiable_mandate.py::test_review_oversized_jwt_integer_is_audited_refusal` |
| 22 | Fixed: same category-before-pool correction as finding 17; normal and broadened lookup counts include only authorized matches. | `test_verifiable_mandate.py::test_review_category_filter_precedes_limits_and_counts` |
| 23 | Fixed: same current-authority stats correction as finding 20; revocation/expiry/issuer removal clear current mandate metadata while policy mandates still register. | `test_verifiable_mandate.py::test_review_stats_registration_uses_current_authority` |

## Decisions taken

- Retain the spec 10 R-1 promise fields and add an `integrity` object over every immutable resource field. Exclude only signature, lifecycle status and live-call counters. Normalize all numeric values in this object to decimal strings. This satisfies R-3/AC-2 without trusting omitted decision/export fields. The standalone verifier uses the same reconstruction.
- Narrower historical signatures and unsigned quotes remain readable, but a new purchase requires re-quoting. Existing orders replay after ownership/identity and current bound buyer-mandate checks, without requiring retired quote keys. An authenticated historical absence of need_ref remains supported with recorded violations and current customer constraints, preserving the existing spec 16 regression; deleting an existing signed binding fails verification.
- Preserve ownership concealment (404) before replay conflict disclosure, consistent with spec 19 AC-4.
- Scope all new revocations by customer and actual trusted signing key. Historical unscoped revocations lack signer scope; migrate them to a conservative legacy scope, retaining their previous denial rather than restoring revoked authority. Migration is idempotent and does not rewrite stored object data.
- Add a nullable signed-transaction column; historical anchors with a hash but no signed bytes continue reconciling by hash. New uncertain sends reconcile/rebroadcast the exact durable bytes. Never retry an uncertain send with a fresh nonce.
- Pending receipts poll every second in the background; RPC failures wait for the next trigger, including a trigger without additional audit entries. Ignore historical failures superseded by a confirmed checkpoint.
- Audit verification still recomputes the complete global chain and remains available to any authenticated principal, as spec 11 R-2/R-8 require; use streaming and per-gateway serialization rather than removing this access.
- The mandatory Peppol field inventory and stdio credential dependence are strengthened in their existing AC tests. No spec files were edited.

## Files changed

- `backend/README.md`
- `backend/app/anchoring.py`
- `backend/app/api.py`
- `backend/app/audit.py`
- `backend/app/catalog.py`
- `backend/app/db.py`
- `backend/app/lookup.py`
- `backend/app/mandates.py`
- `backend/app/mcp_server.py`
- `backend/app/models.py`
- `backend/app/orders.py`
- `backend/app/signing.py`
- `backend/app/stats.py`
- `backend/tests/test_audit_anchoring.py`
- `backend/tests/test_audit_chain.py`
- `backend/tests/test_orders_policy.py`
- `backend/tests/test_signed_quotes.py`
- `backend/tests/test_ubl_export.py`
- `backend/tests/test_verifiable_mandate.py`
- `contracts/openapi.yaml`
- `backend/REVIEW_FIXES.md` (this report)

## AC to test mapping

Existing AC tests remain intact except for the requested strengthening of spec 10 AC-2, spec 12 AC-8, spec 13 AC-2 and spec 21 AC-4. Spec 10 AC-1 additionally recognizes the authenticated integrity object.

### Spec 10

| AC | Test in `test_signed_quotes.py` |
|---|---|
| AC-1 | `test_ac_1_quote_jwk_and_each_signed_field` |
| AC-2 | `test_ac_2_stored_tampering_refused_and_audited` |
| AC-3 | `test_ac_3_rotation_retains_old_kid_seven_days` |
| AC-4 | `test_ac_4_contract_readme_and_mcp_document_signatures` |
| AC-5 | `test_ac_5_receipts_bind_status_total_and_all_decisions` |
| AC-6 | `test_ac_6_offline_cli_reports_signature_expiry_customer` |
| AC-7 | `test_ac_7_commitments_are_signed_and_exposed` |

### Spec 11

| AC | Test in `test_audit_chain.py` |
|---|---|
| AC-1 | `test_ac_1_fresh_chain_detects_edit_and_deletion` |
| AC-2 | `test_ac_2_rest_and_mcp_processes_share_one_valid_chain` |
| AC-3 | `test_ac_3_earlier_checkpoint_survives_appends` |
| AC-4 | `test_ac_4_head_signature_and_authenticated_access` |
| AC-5 | `test_ac_5_rewritten_chain_cannot_match_external_checkpoint` |

### Spec 12

| AC | Test in `test_verifiable_mandate.py` |
|---|---|
| AC-1 | `test_ac_1_valid_buyer_authority_without_policy_row` |
| AC-2 | `test_ac_2_invalid_checks_are_named_audited_on_every_step` |
| AC-3 | `test_ac_3_customer_binding` |
| AC-4 | `test_ac_4_effective_limits_only_tighten` |
| AC-5 | `test_ac_5_signed_revocation_blocks_quote_replay_and_pending_approval` |
| AC-6 | `test_ac_6_category_restriction_on_every_line` |
| AC-7 | `test_ac_7_demo_issuer_and_revocation_with_temporary_keys` |
| AC-8 | `test_ac_8_stdio_cli_mandate_and_http_header` |

### Spec 13

| AC | Test in `test_ubl_export.py` |
|---|---|
| AC-1 | `test_ac_1_confirmed_order_validates_official_ubl_schema` |
| AC-2 | `test_ac_2_peppol_mapping_and_missing_data_are_explicit` |
| AC-3 | `test_ac_3_gtins_quantities_prices_and_all_catalog_units` |
| AC-4 | `test_ac_4_decision_response_validates_schema_and_code` |
| AC-5 | `test_ac_5_mcp_exports_identical_owned_document` |

### Spec 21

| AC | Test in `test_audit_anchoring.py` |
|---|---|
| AC-1 | `test_ac_1_cli_signs_type2_self_transaction_and_records_receipt` |
| AC-2 | `test_ac_2_external_prefix_checkpoint_detects_edited_history` |
| AC-3 | `test_ac_3_unconfigured_is_off_cli_names_missing_variables` |
| AC-4 | `test_ac_4_automatic_failure_isolated_and_next_trigger_retries` |
| AC-5 | `test_ac_5_key_never_appears_in_responses_logs_audit_or_errors` |
| AC-6 | `test_ac_6_local_anvil_real_transaction_and_verification` |

### Spec 02

| AC | Test in `test_orders_policy.py` |
|---|---|
| AC-1 | `test_quote_live_recheck_and_fit_carry` |
| AC-2 | `test_quote_expiry_earliest_bound` |
| AC-3 | `test_expired_quote_persists_and_audits` |
| AC-4 | `test_idempotency_concurrent_retries_and_fixed_price` |
| AC-5 | `test_multiline_quote_sums_and_reserves_each_line` |
| AC-6 | `test_stock_reservations_and_release` |
| AC-7 | `test_stock_recheck_changes_policy_after_quote` |
| AC-8 | `test_unknown_upstream_rejected_and_bad_price_cannot_quote` |
| AC-9 | `test_api_mcp.py::test_full_rest_flow_1000_l`; `test_api_mcp.py::test_mcp_tools_use_shared_core` |
| AC-4 regression | `test_review_existing_order_replay_survives_quote_upgrade` |

### Cross-spec regression coverage

| Spec / criterion | Test |
|---|---|
| 06 R-8; 12 R-6/R-7 | `test_verifiable_mandate.py::test_review_stats_registration_uses_current_authority` |
| 16 AC-4/AC-5, historical binding | Existing `test_standing_constraints.py::test_review_legacy_unbound_quote_checks_current_customer` (unchanged) |
| 18 AC-4, category-aware counts | `test_verifiable_mandate.py::test_review_category_filter_precedes_limits_and_counts` |
| 19 AC-4, ownership on replay | Existing `test_authentication.py::test_ac_4_object_ownership` (unchanged) |

## Regression and migration evidence

Pre-fix regression run: 32 failures, 14 passes; corrected category fixtures were re-run against original retrieval code to prove false-empty lookup failures, and distinct-key revocation was re-run against the original implementation to prove cross-customer revocation. Strengthened AC mutation tests verify that removing document ID mappings or --mandate is detected. Final tests also cover idempotent historical revocation migration and receipt reconciliation for old anchor rows without signed bytes.

Nothing remains undone in the requested fixes. Live network/mainnet behavior was not exercised; the optional local-node proof is blocked by sandbox socket permissions.
