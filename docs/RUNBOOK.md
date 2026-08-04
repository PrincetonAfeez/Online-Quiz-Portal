# QuizOps Operations Runbook

Audience: whoever is on call. Everything here is meant to be executable under
pressure, without reading the codebase first.

---

## 1. Service map

| Component | What it does | Failure blast radius |
|---|---|---|
| `web` (gunicorn) | Serves students and instructors | Total outage |
| `worker` (Celery) | Expiry sweep, notifications, streak rebuilds, export cleanup | Attempts stay `in_progress` past their deadline; no notifications |
| `beat` (Celery Beat) | Schedules the periodic jobs | Same as worker, silently |
| `postgres` | Source of truth for every assessment fact | Total outage + data risk |
| `redis` | Celery broker + cache + rate limits | Degraded: limits fail open, cache misses |

Redis holds **no** unrecoverable business data. Losing it costs in-flight queued
jobs and rate-limit counters, nothing else.

## 2. Health and readiness

- `GET /healthz/` — process is up. Used by the container healthcheck and the
  load balancer. Never touches the database.
- `GET /readyz/` — returns a JSON component map and **503** if a required
  component is down:

```bash
curl -fsS https://<host>/readyz/
```

`{"database":"ok","cache":"ok","worker":"ok"}` is healthy. `worker` is only
required where `READYZ_REQUIRE_WORKER=1` (compose sets it for `web`).

## 3. Alerting

Configure uptime monitoring against `/readyz/` from outside the cluster:

- **Page immediately**: `/readyz/` failing for 2 consecutive checks, or `database` not `ok`.
- **Page during exam windows**: `worker` not `ok` for 10 minutes — expiry
  finalization is stalling and students' attempts will not close on time.
- **Ticket, not a page**: `cache` not `ok` (rate limits fail open; no data risk).

Errors are captured by Sentry when `SENTRY_DSN` is set. Every log line carries
`request_id`; the same value is returned to clients in the `X-Request-ID`
header, so a user-reported problem can be traced from a screenshot.

## 4. Common incidents

### 4.1 Attempts stuck `in_progress` past their deadline

Cause is almost always a stopped worker or beat.

```bash
docker compose ps
docker compose logs --tail=100 worker beat
docker compose restart worker beat
```

Then force one sweep and confirm it drains:

```bash
docker compose exec web python manage.py shell -c "from assessments.tasks import finalize_expired_attempts as f; print(f())"
```

The sweep is idempotent and honours each assignment's `grace_seconds`; running
it twice is safe.

### 4.2 A student reports "my answers are not saving"

1. Ask for the time and the assignment. Find their attempt:

```bash
docker compose exec web python manage.py shell -c "
from assessments.models import Attempt, IntegrityEvent
a = Attempt.objects.filter(student__username='<username>').latest('started_at')
print(a.id, a.status, a.deadline_at, a.revision)
print(list(IntegrityEvent.objects.filter(attempt=a).values('kind','created_at','detail')[:10]))
"
```

2. `late_save_rejected` means the deadline (plus grace) had passed — expected.
3. `revision_conflict` means two tabs or devices. The UI recovers on its own;
   tell them to use one tab.
4. Nothing logged means the request never arrived — network or CDN, not us.

### 4.3 An exam has a broken question

Do **not** edit the published version or the attempt snapshots; they are the
audit record and the admin refuses those edits. Instead:

1. Fix the draft question and republish, so future assignments are correct.
2. Void the affected attempts (audited) and let students retake:
   Django admin → Attempts → select → *Void selected attempts (audited)*.

### 4.4 Suspected credential stuffing

Login is rate limited per identity per `LOGIN_RATE_WINDOW_SECONDS`. Check for
`login blocked by throttle` in the logs. To tighten immediately, set
`LOGIN_RATE_LIMIT=3` and restart `web`. Rate limits live in Redis, so clearing
Redis resets every counter.

### 4.5 Redis is down

The app stays up. Rate limits fail open (logged as
`rate limit cache unavailable`), queued jobs stop running, and `/readyz/`
reports `cache`/`worker` failures. Restore Redis; no data reconciliation is
needed — streak caches rebuild on the hourly job, and the expiry sweep catches
up on its next run.

## 4.6 Behaviour that surprises people

Three deliberate decisions that read like bugs when you first meet them:

**Revoking an accommodation does not shorten a running attempt.** `deadline_at`
is computed and stored when the attempt starts. Revoking afterwards affects the
*next* attempt only. This is intentional — retroactively shortening a student's
remaining time mid-exam would be indefensible — but if an accommodation was
granted in error, void the attempt (§4.3) rather than expecting the clock to
move.

**Revoking a course enrolment does not stop an attempt already in progress, but
deactivating an organization membership does.** Enrolment governs *eligibility to
start*; it is checked at `start_attempt`, not on every save, so a mid-exam
administrative change cannot eject a student. Membership governs *access to the
organization*, and is enforced on every request, so deactivating it locks the
student out immediately, including out of an attempt they are sitting. If you
need to stop a specific student mid-exam, void the attempt. If you need to stop
everyone, deactivate the assignment (§4.3) — that halts in-flight saves.

**A late submit is recorded as `expired`, not `submitted`.** If the submit
request arrives after the cutoff, the finalization reason is `expired` even
though the student pressed the button. The reason field is evidence about
*when*, not about *who acted*.

## 5. Backups

### 5.1 Taking a backup

Automated nightly logical backup (schedule this in your platform's scheduler):

```bash
docker compose exec -T postgres pg_dump -U quizops -d quizops --format=custom \
  | gzip > quizops-$(date +%Y%m%d-%H%M).dump.gz
```

Retain 30 dailies and 12 monthlies. Store off-host, encrypted at rest.

### 5.2 Restoring — and testing the restore

**An untested backup is not a backup.** Run this drill quarterly and record the
date and outcome in `ops/drills/` (see `ops/drills/last-drill.json`, or run
`ops/staging_soak.ps1`).

```bash
# 1. Restore into a scratch database, never over production.
gunzip -c quizops-<stamp>.dump.gz > /tmp/restore.dump
docker compose exec -T postgres createdb -U quizops quizops_restore_test
docker compose exec -T postgres pg_restore -U quizops -d quizops_restore_test --clean --if-exists /tmp/restore.dump

# 2. Verify integrity: row counts and one finalized attempt's score.
docker compose exec -T postgres psql -U quizops -d quizops_restore_test -c \
  "select (select count(*) from assessments_attempt) attempts,
          (select count(*) from assessments_attemptfinalization) finalizations,
          (select count(*) from accounts_user) users;"

# 3. Point a throwaway app container at it and run the checks.
DATABASE_URL=postgres://quizops:<pw>@postgres:5432/quizops_restore_test \
  docker compose run --rm web python manage.py check

# 4. Tear down.
docker compose exec -T postgres dropdb -U quizops quizops_restore_test
```

**RPO** 24 hours (nightly backup). **RTO** 1 hour (restore + verify + cut over).
Both are stated targets, not contractual SLAs.

## 6. Deploys and rollback

Migrations use Django's framework and every migration in this repo is
reversible (no `RunPython` without a reverse, no destructive column drops).

**Deploy**

```bash
docker build -t quizops:<sha> .
docker compose up -d --no-deps web worker beat
docker compose exec web python manage.py migrate --noinput
```

**Rollback** — expand/contract is the rule: never deploy a migration that a
previous release cannot run against in the same step.

1. Redeploy the previous image tag:
   `docker compose up -d --no-deps web worker beat` with the old tag.
2. Only if the bad release added a migration that the old code cannot tolerate,
   reverse it:

```bash
docker compose exec web python manage.py migrate assessments <previous_number>
```

To find the previous number: `python manage.py showmigrations assessments`.

3. If a migration is destructive and cannot be reversed, restore from backup
   (§5.2) — that is the gate, which is why destructive migrations require a
   fresh backup taken immediately beforehand.

## 7. Escalation

| Order | Who | When |
|---|---|---|
| 1 | On-call engineer | All pages |
| 2 | Engineering lead | Not acknowledged in 15 min, or data loss suspected |
| 3 | Product / support lead | Customer-visible for >30 min during an exam window |

Communicate status on the public status page. During an active exam window,
prefer restoring service over root-causing; capture logs and move on.

## 8. Data requests

- **Export** a user's data: account page → *Download my data*, or
  `python manage.py export_user_data <username>`.
- **Delete** an account: account page → *Delete my account*, or
  `python manage.py delete_user <username>`. This anonymizes the subject and
  preserves the immutable assessment record (see `docs/PRIVACY.md`).
