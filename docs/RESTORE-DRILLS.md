# Restore drill records

Drill procedure: `docs/RUNBOOK.md` §5.  
Machine-readable results live under `ops/drills/` (not duplicated here).

| Date (UTC) | Environment | Result | Record |
|---|---|---|---|
| 2026-07-23 | Staging (Docker Compose / Postgres 17) | PASS | `ops/drills/last-drill.json` |

Re-run: `powershell -File ops/staging_soak.ps1` (or the `pg_dump`/`pg_restore` steps in the runbook).
