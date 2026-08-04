# Service Levels, Capacity, and Rollout

Companion to `docs/RUNBOOK.md` (incidents) and `docs/PRIVACY.md` (data).
Everything below is measured or configured, not aspirational.

---

## 1. Service level objectives

Measured over a rolling 30 days, excluding announced maintenance.

| Objective | Target | Why this number |
|---|---|---|
| `POST /attempts/*/save/*` availability | 99.9% | Losing a save loses a student's work mid-exam. This is the one path with no acceptable failure. |
| `POST /attempts/*/save/*` latency p95 | < 400 ms | Above this, the "Saving…" state is visible long enough to make students doubt the save landed. |
| `GET /attempts/*/` latency p95 | < 800 ms | Attempt load happens once per sitting; a slower budget is tolerable. |
| `POST /assignments/*/start/` latency p95 | < 1 s | Form generation writes a snapshot; it is inherently write-heavy. |
| Overall web availability | 99.5% | Business hours weighted; exam windows are the periods that matter. |
| Expiry finalization lag | < 120 s past cutoff | Two beat ticks. Beyond this, students see "in progress" on work that has closed. |

**Error budget.** 99.9% over 30 days is 43 minutes. Spending more than half the
budget in a week freezes non-essential deploys until the cause is fixed.

**What is deliberately not an SLO**: the daily challenge, analytics, and exports.
They are valuable but not exam-critical; degradation there is a ticket, not a page.

---

## 2. Capacity: measured, not assumed

`ops/loadtest.py` drives the real student path (login → dashboard → start →
save ×N → submit → result) with concurrent threads.

### Run of 2026-07-22 — 25 concurrent students, dev hardware

```
endpoint             n    p50 ms    p95 ms    max ms
----------------------------------------------------
attempt_page         1        62        62        62
dashboard           25       565      2888      3238
login               25      4404      6919      7746
save_answer          6        30        33        59
start_attempt       25       508       651       781
submit               1        60        60        60
```

Two findings came out of this run, both material to capacity planning:

**2.1 SQLite collapses under concurrent writers.** 24 of 25 attempt starts failed
with `database is locked`. `start_attempt` takes `select_for_update()` on the
assignment row, and SQLite permits a single writer. This is not a defect to fix
in the application — it is why `config/settings.py` refuses to start with
SQLite when `DEBUG=0`. Capacity numbers above therefore describe the *dev*
configuration; Postgres is the only supported production engine and does not
have this ceiling. **Re-run this test against staging on Postgres before
committing to a concurrency figure.**

**2.2 Login is CPU-bound at roughly 0.9 s per attempt.** A single, uncontended
login measured 889 ms on this hardware — that is PBKDF2 doing its job, not a
performance bug. It is a sizing input:

> sustained logins/second ≈ (gunicorn workers × cores available) ÷ 0.9

Three gunicorn workers therefore support roughly 3 logins/second sustained. A
cohort of 300 students arriving in a five-minute window is ~1 login/second, well
inside that. A whole institution starting at 09:00 is not: scale workers ahead
of a known exam start, or stagger start times. Do **not** reduce the hasher's
iteration count to buy throughput.

### Running it yourself

```bash
python ops/seed_loadtest_users.py          # staging only, creates load* accounts
python ops/loadtest.py --base https://staging.example.com --students 50 --saves 10
```

Exits non-zero when the worst p95 exceeds one second, so it can gate a release.

---

## 3. Caching and invalidation

| Cached | Where | TTL | Invalidation |
|---|---|---|---|
| Rate-limit counters | Redis, `ratelimit:*` | Window length (60–3600 s) | Expiry only; counters are meant to decay |
| Worker readiness probe | Redis, `readyz:worker` | 10 s | Expiry only |
| Feature flags | Redis, `flag:*` | 30 s | Explicit on save, plus TTL as a backstop |
| Job locks | Redis, `joblock:*` | 600 s | Released in a `finally`; TTL covers a killed worker |
| Streak totals | **Postgres** (`StreakCache`) | n/a | Recomputed post-commit and hourly by beat |

The rule this follows: **nothing authoritative is cached.** Every cached value is
either derivable (streaks, flags) or intentionally lossy (rate limits). A total
Redis loss costs in-flight jobs and counters; it cannot corrupt or lose an
assessment record. That is what makes the fail-open behaviour in
`assessments/ratelimit.py` a safe choice rather than a hole.

`StreakCache` lives in Postgres rather than Redis precisely because it is
user-visible: a cache miss showing "0-day streak" reads as data loss to a
student, even when it is not.

---

## 4. Queue health

| Signal | Where | Alert |
|---|---|---|
| Failed jobs | `assessments_jobfailure` (admin → Job failures) | Any unacknowledged row older than 1 hour |
| Backlog depth | `redis-cli llen celery` | > 1000 for 10 minutes |
| Worker liveness | `/readyz/` → `worker` | Not `ok` for 10 minutes during an exam window |
| Sweep progress | Attempts `in_progress` past cutoff | > 0 for 5 minutes |

**Retry policy.** Every task uses `ReportingTask`: exponential backoff with
jitter, capped at 300 s, 3 retries. Exhausted retries write a `JobFailure` row —
that table *is* the dead-letter queue. Nothing fails silently.

**Double-execution.** Periodic tasks hold a `single_flight` cache lock, so two
beat containers or an overrun cannot double-process. The jobs are idempotent
anyway; the lock avoids wasted work rather than incorrectness.

**Draining on deploy.** `CELERY_TASK_ACKS_LATE` with
`CELERY_TASK_REJECT_ON_WORKER_LOST` means a task killed mid-flight is redelivered
rather than lost. Stop workers with `SIGTERM` and allow 30 s to drain.

---

## 5. Safe rollout

Feature flags live in `assessments/flags.py`:

```python
from assessments.flags import is_enabled

if is_enabled("new_scoring_path", organization):
    ...
```

- `enabled=False` with specific `enabled_organizations` — pilot with one tenant.
- `enabled=True` — on everywhere.
- Unknown key — returns the supplied `default`, so a flag that was never created
  cannot break a deploy.
- Any lookup error also returns `default`: a flag must never be why a student
  cannot sit an exam.

Toggle in Django admin → Feature flags; the cache is invalidated on save, so a
switch takes effect immediately rather than after the 30-second TTL.

---

## 6. Environments and pre-production verification

| Environment | Database | Purpose |
|---|---|---|
| Local | SQLite | Development only. `DEBUG=0` refuses to start on SQLite. |
| Staging | Postgres | Mirrors production configuration. Every release goes here first. |
| Production | Postgres | Customer data. |

**Required before a production deploy:**

1. CI green — lint, migration check, migrate-from-zero, full suite with the
   coverage gate, `check --deploy`, image build, `pip-audit`.
2. Deployed to staging and migrated cleanly against a restored production-shaped
   dataset.
3. Smoke test on staging: sign in, start an attempt, save an answer, submit, view
   the result, export a gradebook.
4. `ops/loadtest.py` against staging if the release touches the attempt path.
5. Rollback plan confirmed — see `docs/RUNBOOK.md` §6.

---

## 7. Backup and restore drills

Procedure: `docs/RUNBOOK.md` §5. Drill records: `ops/drills/`.

**Most recent drill — 2026-07-23, staging (Docker Compose / Postgres 17): PASS.**

Executed via `ops/staging_soak.ps1`:

| Step | Result |
|---|---|
| `migrate --noinput` | Pass (through `assessments.0007`) |
| `check --deploy` | Pass; expected warnings only because local HTTP staging disables SSL redirect / secure cookies |
| `/readyz/` | `database` / `cache` / `worker` all `ok` |
| `pg_dump` → `pg_restore` into `quizops_restore_test` | Pass; row counts verified |
| Celery sweep ×5 + `inspect ping` | Pass (`pong`) |
| Password reset → Mailpit SMTP | Pass (branded subject received) |
| Privileged login → MFA | Pass (`/accounts/mfa/setup/`) |

Machine-readable record: `ops/drills/last-drill.json`. Re-run with
`powershell -File ops/staging_soak.ps1`.

**Still required before first customer:** the same drill against the *production*
Postgres instance (or a refreshed prod clone), recorded with off-host backup
storage. Local Compose proves the procedure on the production engine; it is not
a substitute for a prod-account restore test.

**Targets:** RPO 24 hours (nightly backup), RTO 1 hour (restore, verify, cut
over). Both are stated engineering targets, not contractual SLAs.
