# 14 — Demo reset (clean state for the demo and the video)

Status: built (verified live 2026-11-14: refused while the API answered, archived the old state, 17,546 products, empty orders and audit, default policy) · Decided on 2026-11-14.

## Purpose
The local database collects orders, approvals and audit entries from every verification run. The demo and the
video start from a clean, known state. The audit log is append-only by design, so a reset archives the old
state and builds a new one.

## Requirements
- **R-1 One command:** `backend/.venv/bin/python scripts/reset_demo.py` (run from `backend/`).
- **R-2 Archive:** the current database (`var/app.db` with its `-wal` / `-shm` files) and the runtime policy
  copy (`var/policy.yaml`) move to `var/archive/<UTC timestamp, YYYYMMDD-HHMMSS>/`. The archive keeps every
  earlier state; each run creates a new folder.
- **R-3 Rebuild:** the script builds a fresh database with the product catalog (same result as
  `python -m app.catalog build`) and an empty orders / quotes / lookups / approvals / audit state.
- **R-4 Default rules:** after the reset `GET /v1/policy` returns the defaults from `backend/policy.yaml`.
- **R-5 Running API:** the script checks whether the gateway answers on the API port (default 8000, `--port`
  to change). When it does, the script stops with exit code 1 and asks to stop the API first; `--force`
  continues anyway.
- **R-6 Report:** the script prints the archive folder, the number of products in the new catalog and the
  command to start the API. Exit code 0 on success.
- **R-7 Testable core:** the logic lives in a function that takes the paths (database, runtime policy, archive
  root, data sources) as arguments; the command-line entry passes the configured paths.

## Acceptance criteria
- **AC-1** Given a database with orders and audit entries and a changed runtime policy, when the reset runs,
  then the new database has the catalog, zero orders and zero audit entries, and the policy equals the
  defaults.
- **AC-2** The archive folder contains the previous database (orders still readable) and the previous runtime
  policy.
- **AC-3** Two resets create two archive folders; the first archive is unchanged.
- **AC-4** A first run without an existing database builds the catalog and creates an empty archive step
  without failing.
- **AC-5** When the API port answers, the reset exits with code 1 and leaves every file in place; with
  `--force` it proceeds.

Tests (`backend/tests/test_demo_reset.py`): AC-1 `test_ac_1_reset_clears_transaction_state_and_restores_default_policy` ·
AC-2 `test_ac_2_archive_preserves_database_sidecars_and_changed_policy` · AC-3
`test_ac_3_repeated_resets_keep_distinct_unchanged_archives` · AC-4
`test_ac_4_first_run_builds_catalog_and_an_empty_archive` · AC-5
`test_ac_5_cli_refuses_running_api_without_changes_and_force_proceeds`.

## For the review
Sample orders for the demo come from running `backend/demo/run_buyer_agent.sh` or the console after the reset
(they need live prices). A seeded scenario can be added once the demo script is fixed.
