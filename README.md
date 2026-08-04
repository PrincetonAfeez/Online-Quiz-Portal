# QuizOps

QuizOps is a Django + HTMX online assessment portal built from the product scope in
`Online Quiz Portal.md`. The engine is server-authoritative: timing, eligibility, scoring,
feedback release and streak credit are decided on the server, never by the browser.

## What is implemented

- Custom users, organizations, roles, courses, cohorts, enrollments, and timing accommodations
  (scoped, expiring, and selected by total accommodated time).
- Tenant-scoped model managers. Cross-tenant visibility requires an explicit
  `User.is_platform_operator` grant — never `is_staff`.
- Question banks with provenance/review fields, draft quizzes, immutable quiz versions,
  fixed-form assignments, and sampled-form assignments with weighted, blueprint-aware draws.
- Server-authoritative attempts: deadline computation with accommodations, availability-window
  enforcement, grace windows, persisted item snapshots, optimistic-concurrency saves, event-sourced
  response history, transactional idempotent finalization, and audited voiding.
- Feedback policies that actually differ: immediate (locks and returns per-question correctness),
  at-end, and manual release gated by a `ResultRelease` record. The answer key stays hidden while a
  retake of the same fixed form is still available.
- Daily Coding Challenge with per-organization local dates, one-attempt live participation,
  practice archive, and recomputable streaks.
- Read-only admin over all assessment evidence, tenant-scoped foreign-key pickers
  (including enrollment/accommodation students), identity admin gated to platform
  operators, and an
  `AdminActionLog` + `IntegrityEvent` trail.
- Celery Beat schedule covering the expiry sweep, streak rebuilds, three notification jobs and
  export cleanup. Health (`/healthz/`) and readiness (`/readyz/`: database, cache and worker).

## Local quick start

```powershell
python manage.py migrate
python manage.py seed_demo
python manage.py runserver 127.0.0.1:8000
```

Open `http://127.0.0.1:8000/`. `seed_demo` prints the demo accounts and password.

Local runs read `.env` if present; real environment variables always take precedence. With
`DEBUG=0` the app requires `SECRET_KEY`, `ALLOWED_HOSTS` and a PostgreSQL `DATABASE_URL`, and
refuses to start without them.

## Docker

```powershell
copy .env.example .env
docker compose up --build
docker compose exec web python manage.py seed_demo
```

Set `SECRET_KEY`, `POSTGRES_PASSWORD`, and `REDIS_PASSWORD` in `.env` first — compose will not
start without them. Compose pins `DEBUG=0` and passes `REDIS_HOST` + `REDIS_PASSWORD` so the app
can URL-encode the Redis password. A developer's local settings cannot follow the app into a
container.

## Operations

- `/healthz/` — process liveness (used by the container healthcheck).
- `/readyz/` — database, cache and Celery worker. Returns 503 when a required component is down;
  set `READYZ_REQUIRE_WORKER=1` where a worker is deployed.
- Expiry sweeping runs every minute via Celery Beat and honours each assignment's `grace_seconds`,
  so it agrees with the interactive save path.
- Logs are JSON in production, carry a request id, and scrub secrets and answer-key fields.
- Set `SENTRY_DSN` to enable error reporting.
- `TRUST_PROXY_SSL_HEADER` / `TRUST_X_FORWARDED_FOR` default to off; enable them only behind a proxy
  that strips client-supplied forwarding headers.

## Verification

```powershell
python manage.py test
python -m pytest
python manage.py check --deploy
python -m ruff check .
```

`check --deploy` is expected to report **no issues** when `DEBUG=0` with a real `SECRET_KEY`,
`ALLOWED_HOSTS` and `DATABASE_URL` set.

## Data rights

- `/accounts/me/` — download everything we hold about you, or delete your account.
- `python manage.py export_user_data <username>` / `delete_user <username> --yes`.
- Erasure tombstones the person and retains the anonymized assessment record;
  the reasoning is in `docs/PRIVACY.md`.

## Operations, security, and documentation

| Document | What it covers |
|---|---|
| `docs/RUNBOOK.md` | Service map, alerting, common incidents, backup/restore, deploy and rollback, escalation |
| `docs/OPERATIONS.md` | SLOs and error budget, **measured** capacity results, cache invalidation, queue health, feature flags, environments |
| `docs/SECURITY.md` | Exposed surfaces, control mapping with evidence, review status, known limitations |
| `docs/PRIVACY.md` | Personal-data inventory, export, erasure semantics, audit retention, residency |
| `docs/AUDIT.md` | Full defect audit: every finding, its fix, and how it was verified |
| `docs/adr/README.md` | Architecture decision records |

## Known gaps

Shipped today: instructor authoring, item pools/blueprints, gradebook/analytics/exports,
integrity dashboard, notifications + Beat jobs, retention jobs, self-service export/erasure,
and TOTP MFA for organization admins, platform operators, and Django staff (on when `DEBUG=0`).

Still open against `Online Quiz Portal.md` (tracked, not defects in shipped code):

- AI draft workflow with budget enforcement (`Organization.ai_monthly_budget` unused)
- Transactional outbox / domain-event bus
- `RandomAssemblyRun` model (sampling seed/criteria already live on attempts)
- Grade bands (pass mark is implemented; band labels are not)
- Product support console beyond Django admin `JobFailure`
- Async large-export pipeline (synchronous auth-gated CSV exports exist)
- SSO/SAML/SCIM and an external penetration test (see `docs/SECURITY.md`)

Historical defect inventory and fixes: `docs/AUDIT.md`.
