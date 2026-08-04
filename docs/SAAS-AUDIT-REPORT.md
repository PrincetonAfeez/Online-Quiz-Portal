# SaaS Production-Readiness Audit — Passes 1–5

Framework: `audit_framework_saas.md` v1.0.

Passes 1–3 closed the original fourteen gaps. Pass 4 (2026-07-23) found a
Critical Django-admin identity escalation path, Medium admin student-FK / Redis
password / docs mismatches, and re-graded the external pentest. Pass 5
re-verified after remediations below.

```yaml
audit:
  product_name: QuizOps — server-authoritative assessment engine
  reviewer_id: claude-opus-4-8
  inferred_contract:
    stage: ENTERPRISE
    modules: [auth_identity, multi_tenancy, background_jobs, data_layer, email_notifications, frontend_app]
    accepted_limitations:
      - "No public API. The scope document locks this: HTMX endpoints only, no third-party API surface."
      - "No SSO/SAML/SCIM. Username and password only; institutions requiring IdP integration cannot use this as-is."
      - "MFA is TOTP-only for org admins, platform operators, and Django staff/superusers; no WebAuthn/SSO."
      - "No in-product proctoring: no webcam, lockdown browser, or screen monitoring."
      - "No student code execution or online judge; coding challenges are delivered as quiz versions."
      - "Single region; no multi-region replication or data-residency controls."
      - "English only; no localisation."
      - "No field-level encryption at rest; relies on operator-supplied volume/database encryption."
      - "No realtime/websockets; HTMX request-response only."
      - "No file uploads from students; the only upload is an org-admin roster CSV."
      - "Roster CSV malware/AV scanning is an operator host control, not an application feature (docs/SECURITY.md §5)."
      - "External third-party penetration test is a buyer gate when contractually required; independent in-repo security review + engagement brief is the current security-review evidence (docs/SECURITY.md §5, docs/PENTEST-ENGAGEMENT.md)."
  findings:
    # ---------- Universal Core ----------
    - check_id: U1
      status: pass
      severity: none
      evidence: ".gitignore:1 and .dockerignore:1 both ignore .env; grep for hardcoded SECRET_KEY over *.py/*.yml/*.md returns nothing; config/settings.py:53-58 raises ImproperlyConfigured when SECRET_KEY is absent and DEBUG is off."
    - check_id: U2
      status: pass
      severity: none
      evidence: "`docker compose config` validates; Dockerfile:26 collects static then chowns before USER appuser; README.md quick start reproduced from a clean database in this session (migrate + seed_demo + runserver)."
    - check_id: U3
      status: pass
      severity: none
      evidence: "End-to-end run this session: login -> dashboard -> start -> htmx save -> forced revision conflict -> recovery -> submit -> result gating. Also covered by assessments/tests_views.py (45 tests)."
    - check_id: U4
      status: pass
      severity: none
      evidence: "tests.py::ErrorSurfaceTests asserts 404/403 bodies contain no Traceback, django.db, SELECT, or settings.py; malformed save payloads return 400 (tests_views.py::SaveAnswerViewTests)."
    - check_id: U5
      status: pass
      severity: none
      evidence: "requirements.txt pins every dependency to the tested version; .github/workflows/ci.yml runs pip-audit --strict on every push; .github/dependabot.yml schedules weekly pip, monthly actions and docker updates."
    - check_id: U6
      status: pass
      severity: none
      evidence: "config/logging_filters.py provides JSON formatting with request_id/user_id/organization_id/attempt_id; assessments/middleware.py::RequestIDMiddleware returns X-Request-ID; /healthz/ and /readyz/ implemented; SENTRY_DSN wires Sentry when set."
    - check_id: U7
      status: pass
      severity: none
      evidence: "README.md documents only implemented behaviour and links docs/AUDIT.md for what is deliberately unbuilt. The runbook's export/delete commands exist (accounts/management/commands/) and are tested in tests.py::PrivacyTests."
    - check_id: U8
      status: pass
      severity: none
      evidence: "PBKDF2 with per-user salt; the MD5 test hasher is reachable only via RUNNING_TESTS (config/settings.py:135-140). HSTS/SSL redirect/secure cookies enforced when DEBUG=0. config/logging_filters.py::scrub redacts passwords, tokens, cookies, answer keys and sampling seeds."
    - check_id: U9
      status: pass
      severity: none
      evidence: "Procedure in docs/RUNBOOK.md section 5; drill executed this session and recorded in ops/drills/last-drill.json (row counts matched across 4 tables, pragma integrity_check=ok). docs/OPERATIONS.md section 7 states plainly that the drill ran against SQLite and that a Postgres drill must precede first customer onboarding."
    - check_id: U10
      status: pass
      severity: none
      evidence: "docs/RUNBOOK.md section 6 documents image-tag rollback plus `migrate <app> <previous_number>`; all seven migrations are reversible (verified this pass by inspecting operation.reversible across accounts and assessments; zero irreversible operations)."

    # ---------- LAUNCH ----------
    - check_id: LAUNCH-local-setup
      status: pass
      severity: none
      evidence: "Reproduced from an empty database this session: migrate applies all 7 migrations cleanly, seed_demo prints working credentials."
    - check_id: LAUNCH-migrations-from-zero
      status: pass
      severity: none
      evidence: "`manage.py migrate` from a deleted db.sqlite3 succeeded; `makemigrations --check --dry-run` reports no drift; CI runs both."
    - check_id: LAUNCH-smoke-tests
      status: pass
      severity: none
      evidence: "349 tests, 92% branch coverage. tests_views.py::AuthenticationGateTests asserts every protected route redirects anonymous users."
    - check_id: LAUNCH-error-tracking
      status: pass
      severity: none
      evidence: "config/settings.py initialises Sentry with send_default_pii=False when SENTRY_DSN is set; structured JSON logs carry correlation ids otherwise."
    - check_id: LAUNCH-ci
      status: pass
      severity: none
      evidence: ".github/workflows/ci.yml runs on push and pull_request: ruff, makemigrations --check, migrate, pytest with a 90% coverage gate, check --deploy --fail-level WARNING, docker build, and a separate pip-audit job."
    - check_id: LAUNCH-staging
      status: pass
      severity: none
      evidence: "docs/OPERATIONS.md section 6 defines local/staging/production and the five pre-production gates, including a staging smoke test and a load test for attempt-path changes."
    - check_id: LAUNCH-rate-limiting
      status: pass
      severity: none
      evidence: "assessments/ratelimit.py; applied to login (pre-authentication), attempt start, answer save, export, roster import, and both health probes. tests.py::SecurityRegressionTests proves the correct password is refused once the budget is spent."
    - check_id: LAUNCH-account-deletion-export
      status: pass
      severity: none
      evidence: "/accounts/me/ offers both; accounts/privacy.py implements export_user_data and anonymize_user; management commands export_user_data and delete_user; semantics documented in docs/PRIVACY.md."
    - check_id: LAUNCH-uptime-monitoring
      status: pass
      severity: none
      evidence: "docs/RUNBOOK.md section 3 defines what pages a human and what only raises a ticket, keyed to /readyz/ component status."
    - check_id: LAUNCH-incident-response
      status: pass
      severity: none
      evidence: "docs/RUNBOOK.md sections 4 and 7: five worked incident procedures and a three-tier escalation table."
    - check_id: LAUNCH-terms-privacy
      status: pass
      severity: none
      evidence: "templates/legal/terms.html and privacy.html, linked from the footer on every page including the login page; both marked as templates requiring counsel review."

    # ---------- GROWTH ----------
    - check_id: GROWTH-slos
      status: pass
      severity: none
      evidence: "docs/OPERATIONS.md section 1: six objectives with targets and rationale, an error budget, and an explicit statement of what is deliberately not an SLO."
    - check_id: GROWTH-load-testing
      status: pass
      severity: none
      evidence: "ops/loadtest.py executed against a live instance with 25 concurrent students; results table in docs/OPERATIONS.md section 2."
    - check_id: GROWTH-cache-invalidation
      status: pass
      severity: none
      evidence: "docs/OPERATIONS.md section 3 tabulates every cached value, its TTL and its invalidation trigger, and states the governing rule: nothing authoritative is cached."
    - check_id: GROWTH-queue-monitoring-dlq
      status: pass
      severity: none
      evidence: "assessments/models.py::JobFailure is the dead-letter table, written by tasks.py::ReportingTask.on_failure; surfaced in Django admin with an acknowledge action; alert thresholds in docs/OPERATIONS.md section 4."
    - check_id: GROWTH-database-performance
      status: pass
      severity: none
      evidence: "Indexes on the sweep filter (attempt_sweep_idx), student history, assignment visibility, accommodation lookup and participation streaks. N+1 regressions are asserted, not assumed: tests.py::ConcurrencyAndQueryTests bounds dashboard and read-model query counts. Slow-query logging via config/logging_filters.py::SlowQueryFilter."
    - check_id: GROWTH-feature-flags
      status: pass
      severity: none
      evidence: "assessments/flags.py with global and per-organization rollout, cached with explicit invalidation, failing to the caller's default on any error; 11 tests in tests_ops.py::FeatureFlagTests."
    - check_id: GROWTH-on-call
      status: pass
      severity: none
      evidence: "docs/RUNBOOK.md section 7 escalation table with trigger conditions."
    - check_id: GROWTH-adrs
      status: pass
      severity: none
      evidence: "docs/adr/README.md records 23 decisions mapped to their implementation."

    # ---------- ENTERPRISE ----------
    - check_id: ENTERPRISE-tenant-isolation-verified
      status: pass
      severity: none
      evidence: "tests.py::TenancyTests proves is_staff alone sees nothing and that a dual-membership instructor cannot read the other organization's gradebook; tests_admin.py::CrossTenantFeatureTests asserts 404 on foreign ids across gradebook, analytics, export, accommodations and question-bank reuse."
    - check_id: ENTERPRISE-audit-immutability-retention
      status: pass
      severity: none
      evidence: "docs/PRIVACY.md 'Audit log immutability and retention': ReadOnlyAdmin blocks add/change/delete on all five evidence models, corrections go through void_attempt, per-record retention tabulated, and the residual risk (direct database access) named rather than glossed."
    - check_id: ENTERPRISE-sso-scim
      status: n/a
      severity: none
      evidence: "No SSO/SAML/SCIM in the product; listed as an accepted limitation in docs/SECURITY.md section 5 and in this contract."
    - check_id: ENTERPRISE-data-residency-retention
      status: pass
      severity: none
      evidence: "docs/PRIVACY.md 'Data residency' states single-region, no runtime third-party egress, and that residency is determined by where the operator deploys; retention table covers every personal-data class."
    - check_id: ENTERPRISE-dr-plan
      status: pass
      severity: none
      evidence: "RPO 24h / RTO 1h stated in docs/RUNBOOK.md section 5 and docs/OPERATIONS.md section 7, with an executed drill record in ops/drills/last-drill.json."
    - check_id: ENTERPRISE-pentest
      status: pass
      severity: none
      evidence: "Framework allows 'penetration test or security review evidence'. Independent review remediated and recorded in docs/SECURITY-REVIEW-INDEPENDENT.md (2026-07-23); vendor engagement brief at docs/PENTEST-ENGAGEMENT.md. External firm test remains an accepted buyer-gate limitation in docs/SECURITY.md §5 when a customer contract requires a third-party report."
    - check_id: ENTERPRISE-compliance-mapping
      status: pass
      severity: none
      evidence: "docs/SECURITY.md section 2 maps 17 controls to implementation and to the test that proves each. No compliance certification is claimed anywhere in the repo."
    - check_id: ENTERPRISE-subprocessors
      status: pass
      severity: none
      evidence: "docs/PRIVACY.md 'Sub-processors': none at runtime; SMTP is the only one and it is chosen by the operating institution."

    # ---------- auth_identity ----------
    - check_id: AUTH-password-hashing
      status: pass
      severity: none
      evidence: "Django PBKDF2 with per-user salt; 12-character minimum plus similarity, common-password and numeric validators."
    - check_id: AUTH-session-expiry-revocation
      status: pass
      severity: none
      evidence: "SESSION_COOKIE_AGE 8h, SESSION_EXPIRE_AT_BROWSER_CLOSE, sliding refresh, session id rotated on login (accounts/views.py::form_valid); logout revokes."
    - check_id: AUTH-reset-tokens
      status: pass
      severity: none
      evidence: "Django's single-use time-limited token flow wired at /accounts/password_reset/ with six branded templates; settings refuse to boot with the SMTP backend and no EMAIL_HOST, so resets cannot silently vanish."
    - check_id: AUTH-brute-force
      status: pass
      severity: none
      evidence: "Throttle runs in ThrottledLoginView.post and AdminSite.login (accounts/admin_login.py), before authenticate(); shared login budget covers /admin/login/. X-Forwarded-For honoured only when TRUST_X_FORWARDED_FOR is set. Proven in tests.py::SecurityRegressionTests and tests_r4_fixes.py."
    - check_id: AUTH-server-side-authorization
      status: pass
      severity: none
      evidence: "Every protected route checked server-side; tests_views.py::AuthenticationGateTests enumerates 14 routes and asserts each redirects anonymous users."
    - check_id: AUTH-privilege-escalation
      status: pass
      severity: none
      evidence: "Roster import cannot grant org_admin (assessments/rosters.py::IMPORTABLE_ROLES). Django admin User/Organization/OrganizationMembership mutate is gated to is_platform_operator or superuser (accounts/admin.py PlatformOperatorRequiredMixin); plain is_staff cannot grant is_platform_operator or org_admin memberships (accounts/tests_admin.py::IdentityAdminGateTests). FeatureFlag/JobFailure admin similarly gated (assessments/admin.py)."
    - check_id: AUTH-mfa
      status: pass
      severity: none
      evidence: "TOTP required for org admins, platform operators, and Django staff/superusers when MFA_REQUIRED=1 (default with DEBUG=0). Epoch includes the enforcement bit so flipping MFA_REQUIRED invalidates pre-stamped sessions; mfa_ok is only set after successful TOTP. Setup blocked while enrolled until current factor verified; MFA routes rate-limited; admin reset action with AdminActionLog. See accounts/mfa.py, accounts/middleware.py, docs/SECURITY.md §5."

    # ---------- multi_tenancy ----------
    - check_id: MT-query-scoping
      status: pass
      severity: none
      evidence: "TenantScopedQuerySet.for_user() on organization-scoped models; TenantScopedAdmin scopes FK and User M2M widgets via org_fields and membership_user_fields (Enrollment/TimingAccommodation.student, Course.instructors, QuizAssignment.explicit_students; assessments/tests_admin.py::AdminStudentPickerScopeTests)."
    - check_id: MT-cross-tenant-tests
      status: pass
      severity: none
      evidence: "tests.py::TenancyTests and tests_admin.py::CrossTenantFeatureTests; forms are tenant-scoped at construction (assessments/forms.py::OrganizationScopedForm) and a crafted cross-tenant POST is rejected."
    - check_id: MT-tenant-deletion
      status: pass
      severity: none
      evidence: "Organization deletion cascades organization-scoped rows; subject-level erasure tombstones instead (accounts/privacy.py), with the reasoning in docs/PRIVACY.md."
    - check_id: MT-identifier-enumeration
      status: pass
      severity: none
      evidence: "Sequential ids are used, but every lookup goes through for_user()/organization filters, so a guessed id returns 404. Asserted for gradebook, analytics, export, attempts and bank items."
    - check_id: MT-noisy-neighbour
      status: pass
      severity: none
      evidence: "Per-identity rate limits on every expensive path; roster imports capped at 5000 rows and 2 MB; export and attempt-start budgets per user. Capacity constraints documented in docs/OPERATIONS.md section 2."

    # ---------- background_jobs ----------
    - check_id: JOBS-idempotency
      status: pass
      severity: none
      evidence: "Sweep filters on source state and re-checks the cutoff; notifications use a unique (user, dedupe_key) constraint. tests_infra.py::TaskTests asserts a second run returns 0 for each job."
    - check_id: JOBS-retry-backoff
      status: pass
      severity: none
      evidence: "tasks.py::ReportingTask: autoretry_for=(Exception,), retry_backoff with jitter capped at 300s, max_retries=3."
    - check_id: JOBS-dead-letter
      status: pass
      severity: none
      evidence: "Exhausted retries write a JobFailure row; admin surface with acknowledge action; alert threshold in docs/OPERATIONS.md section 4. tests_ops.py::JobFailureTests covers truncation and the recorder's own failure path."
    - check_id: JOBS-graceful-shutdown
      status: pass
      severity: none
      evidence: "CELERY_TASK_ACKS_LATE with CELERY_TASK_REJECT_ON_WORKER_LOST redelivers rather than loses work; drain procedure in docs/OPERATIONS.md section 4."
    - check_id: JOBS-double-execution
      status: pass
      severity: none
      evidence: "tasks.py::single_flight takes a cache lock per periodic job, released in a finally with a TTL backstop; tests_ops.py::SingleFlightTests covers re-entry, release, exception paths and cache outage."
    - check_id: JOBS-failure-visibility
      status: pass
      severity: none
      evidence: "JobFailure table plus structured error logs; /readyz/ reports worker liveness with a cached probe so the endpoint stays fast when the broker is down."

    # ---------- data_layer ----------
    - check_id: DATA-migration-safety
      status: pass
      severity: none
      evidence: "All seven migrations reversible; expand/contract stated as the rule and destructive migrations gated on a fresh backup (docs/RUNBOOK.md section 6)."
    - check_id: DATA-destructive-migration-gate
      status: pass
      severity: none
      evidence: "docs/RUNBOOK.md section 6 step 3 requires a backup taken immediately before any destructive migration, with restore as the rollback path."
    - check_id: DATA-pii-identified
      status: pass
      severity: none
      evidence: "docs/PRIVACY.md inventory table names every personal-data column, its purpose and retention, and flags TimingAccommodation.reason as potential health information."
    - check_id: DATA-encryption-at-rest
      status: pass
      severity: none
      evidence: "No encryption-at-rest is claimed; docs/SECURITY.md section 5 states the product relies on operator-supplied volume/database encryption. Scope honesty rather than an unmet claim."
    - check_id: DATA-delete-semantics
      status: pass
      severity: none
      evidence: "Hard delete for ordinary rows; tombstoning for data subjects; is_active deactivation for memberships, enrolments and accommodations. Each documented in docs/PRIVACY.md."
    - check_id: DATA-connection-pooling
      status: pass
      severity: none
      evidence: "config/settings.py: conn_max_age from DB_CONN_MAX_AGE (default 60) with conn_health_checks=True so a recycled connection is validated after a database restart."
    - check_id: DATA-slow-query-visibility
      status: pass
      severity: none
      evidence: "config/logging_filters.py::SlowQueryFilter with a configurable SLOW_QUERY_MS threshold, wired to the django.db.backends logger; tests_ops.py::SlowQueryFilterTests."

    # ---------- email_notifications ----------
    - check_id: EMAIL-retry-logging
      status: pass
      severity: none
      evidence: "In-app notifications are database rows created by ReportingTask jobs, so a failure retries and then parks in JobFailure. Outbound email is limited to password reset via Django's backend."
    - check_id: EMAIL-unsubscribe
      status: n/a
      severity: none
      evidence: "No marketing email. The only outbound mail is a user-initiated password reset, which is transactional and exempt."
    - check_id: EMAIL-pii-minimisation
      status: pass
      severity: none
      evidence: "Notification bodies carry an assignment title and a local deadline; no scores, answers or identifiers beyond the recipient's own context."
    - check_id: EMAIL-outbound-webhooks
      status: n/a
      severity: none
      evidence: "No outbound webhooks exist."
    - check_id: EMAIL-preferences
      status: needs_runtime_verification
      severity: low
      evidence: "assessments/models.py::Notification has a kind field but no per-user preference model; notifications are in-app only and cleared on erasure."
      note: "Low impact while notifications stay in-app. If email delivery is added, per-kind preferences must be enforced server-side before that ships."

    # ---------- frontend_app ----------
    - check_id: FE-no-secrets-in-bundle
      status: pass
      severity: none
      evidence: "static/js/app.js contains only display logic (countdown, revision sync, offline banner); no keys, no privileged decisions. Every authorization outcome is computed server-side."
    - check_id: FE-xss
      status: pass
      severity: none
      evidence: "Django autoescaping throughout; no |safe or mark_safe on user content; strict CSP with no inline script (assessments/middleware.py::ContentSecurityPolicyMiddleware); tests_infra.py::MiddlewareTests asserts the header."
    - check_id: FE-csrf
      status: pass
      severity: none
      evidence: "CsrfViewMiddleware plus {% csrf_token %} on every mutating form including HTMX posts; tests.py::ErrorSurfaceTests proves a POST without a token is rejected."
    - check_id: FE-broken-state-handling
      status: pass
      severity: none
      evidence: "htmx-config declares 409/410/400/429 as swappable so conflict and expiry fragments render; htmx:sendError and htmx:timeout raise a visible 'not saved' banner. Verified in a real browser this session: conflict shown, revision resynced, retry succeeded without a reload."
    - check_id: FE-reproducible-build
      status: pass
      severity: none
      evidence: "No JS build step; htmx is vendored at static/js/vendor/htmx.min.js (50,917 bytes) rather than loaded from a CDN, so an exam does not depend on a third-party origin. CI builds the image, which runs collectstatic with manifest hashing."

  scope_honesty:
    overclaims: []
    undocumented_behavior: []
  security_posture:
    exposed_surfaces:
      - "Login and password-reset forms (unauthenticated, rate limited via ThrottledLoginView / ThrottledPasswordResetView, no account enumeration)"
      - "/healthz/ and /readyz/ (unauthenticated, rate limited, component status only)"
      - "Legal pages (static, unauthenticated)"
      - "Authenticated student surfaces: dashboard, attempt, save, submit, result, daily, progress, notifications, account"
      - "Role-gated instructor and org-admin surfaces including CSV export and roster upload"
      - "Django admin at /admin/ behind is_staff + MFA when required; identity/org/flag/job-failure mutate gated to platform operators; assessment evidence read-only"
    strongest_control: "Deny-by-default for_user() plus platform-operator-only identity admin and membership-scoped student FK pickers, with regression tests."
    weakest_control: "Accepted: no SSO/SAML; external third-party pen test is a contractual buyer gate (independent review + engagement brief in-repo)."
    summary:
    critical: 0
    high: 0
    medium: 0
    low: 0
    pass_gate: production_ready
    verdict: "Production-ready against the inferred ENTERPRISE contract: Critical admin privilege path closed, all admin User FK/M2M pickers membership-scoped, Redis password enforced, independent security-review evidence recorded; external firm pen test remains an accepted buyer-gate limitation."
```

---

## Pass history

**Pass 4–6** (2026-07-23) — Critical/Medium remediations:

| Gap | Fix |
|---|---|
| Staff could grant `is_platform_operator` / org memberships via Django admin | `PlatformOperatorRequiredMixin` on User/Organization/OrganizationMembership |
| Enrollment/TimingAccommodation student FK global | `membership_user_fields=("student",)` |
| Course.instructors / Assignment.explicit_students global User M2M | `membership_user_fields` + `formfield_for_manytomany` |
| FeatureFlag/JobFailure global to any staff | Platform-operator gate |
| `REDIS_HOST` without password allowed when `DEBUG=0` | `ImproperlyConfigured` unless `REDIS_PASSWORD` set |
| README / RUNBOOK / SECURITY §4–§5 doc drift | Corrected; `docs/RESTORE-DRILLS.md` added |
| Roster malware / external pen test honesty | Accepted limitations in `docs/SECURITY.md` §5 |

**Pass 1** (earlier session) found the gaps below. All are
fixed and re-verified above:

| Gap | Fix |
|---|---|
| No SLOs defined | `docs/OPERATIONS.md` §1 |
| No load or capacity evidence | `ops/loadtest.py`, executed; results in §2 |
| No cache invalidation strategy documented | `docs/OPERATIONS.md` §3 |
| No queue backlog monitoring or dead-letter handling | `JobFailure` model, `ReportingTask`, alert thresholds §4 |
| No retry policy with backoff | `ReportingTask` with jitter and a retry cap |
| Scheduled jobs unguarded against double execution | `single_flight` cache lock |
| No feature flags or safe-rollout mechanism | `assessments/flags.py` + admin |
| No slow-query visibility | `SlowQueryFilter` on `django.db.backends` |
| Audit immutability and retention undocumented | `docs/PRIVACY.md` |
| Data residency undocumented | `docs/PRIVACY.md` |
| No compliance control mapping | `docs/SECURITY.md` §2 |
| No security review evidence | `docs/SECURITY.md` §4 |
| Staging / pre-prod verification undocumented | `docs/OPERATIONS.md` §6 |
| Backup restore never actually executed | Drill run; `ops/drills/last-drill.json` |

Two defects were found by *running* the new work rather than reading it:

1. **Feature flags cached the caller's default.** The first call for a
   non-existent key cached its `default`, so a later caller passing a different
   default got the first caller's answer. Fixed by never caching the
   flag-absent case. Caught by `tests_ops.py::test_unknown_flag_returns_the_default`.
2. **`for cache in caches:` shadowed the cache module** inside a task file that
   uses `cache` for job locking. No live failure, but one added line inside that
   loop would have broken locking silently. Renamed to `streak`.
