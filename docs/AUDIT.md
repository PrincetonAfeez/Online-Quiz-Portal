# QuizOps — Definitive Defect & Gap Audit

Scope: full source tree (`accounts`, `assessments`, `config`, `templates`, `static`, infra) audited against
observed runtime behaviour and against the product scope in `Online Quiz Portal.md`.

Method: static review plus 17 executable probes run against the real code on a throwaway test database.
Findings marked **[verified]** were reproduced empirically; the observed output is quoted.

**Result: 53 findings — 8 critical, 14 high, 12 medium, 8 low, 11 scope gaps.**

Baseline that already passes: `manage.py check` (clean), `makemigrations --check` (no drift),
`manage.py test` (5/5 pass). None of the defects below are caught by the existing suite.

---

## Remediation status (updated after the fix pass)

**Findings 1–44 are fixed and verified.** Findings 45–53 are product surfaces, not defects;
what was built for them is itemised at the end of this section.

| Verification | Before | After |
|---|---|---|
| `manage.py test` | 5 tests | **25 tests, all passing** |
| `python -m pytest` | could not run (no settings config) | **25 passed** |
| `manage.py check --deploy` | 5 security warnings | **0 issues** |
| `ruff check .` | no config, unenforced | **All checks passed** |
| Static assets under `DEBUG=0` | 404 (WhiteNoise unwired) | **collectstatic + manifest storage verified** |
| End-to-end browser + HTTP smoke | — | **login → attempt → save → conflict → submit → result gating verified** |

Two further defects were found *by running the fixed code* and are also fixed:

- **45a. Daily credit was awarded to ordinary attempts.** Keying participation on `quiz_version`
  conflated an assignment attempt with the daily challenge whenever both published the same
  version (which the seed data does). Attempts now carry an explicit `daily_challenge` FK.
  Regression test: `test_ordinary_attempt_sharing_a_version_earns_no_daily_credit`.
- **45b. Submit blocked for ~3s when the broker was down.** The post-commit streak refresh was
  queued through Celery, so a broker outage stalled the student's submit. The recompute is two
  indexed queries, so it now runs inline post-commit (still outside the finalization transaction,
  so it can never roll back a score). Measured: 2.98s → **0.055s** with no broker running.

Scope-gap work that was completed rather than deferred:

- `ResultRelease`, `IntegrityEvent`, `AdminActionLog` models exist and are written by the services
  that need them (findings 45 partially).
- Notifications are real: four `Notification.Kind` types, four idempotent Celery jobs with dedupe
  keys (finding 47).
- Celery Beat now has a schedule with 6 jobs, up from 1 unscheduled job (findings 13, 51).
- `cleanup_expired_exports` implemented against `ExportArtifact` (finding 49, partially).
- The regression suite covering tenancy, authorization, timing, sampling, release policy and the
  background sweep now exists (finding 53).

**Post-fix inventory (2026-07):** instructor authoring, item-pool/blueprint UI, gradebook /
analytics / exports, integrity dashboard, notifications + Beat jobs, retention jobs, erasure
tombstoning, and TOTP MFA for org admins / platform operators are implemented. Remaining open
product surfaces (not regressions): AI draft workflow, transactional outbox, `RandomAssemblyRun`
model, grade bands, product support console, async large-export pipeline, SSO/SAML/SCIM, and an
external penetration test. See README “Known gaps”.

---

## Severity index

| # | Finding | Sev |
|---|---|---|
| 1 | `is_staff` bypasses all tenant isolation | P0 |
| 2 | Instructor dashboard leaks every org the viewer belongs to | P0 |
| 3 | Manual-release feedback policy is never enforced | P0 |
| 4 | Answer key released while retakes are still open | P0 |
| 5 | Admin can mutate attempt snapshots, responses and scores; audit rows are deletable | P0 |
| 6 | Admin foreign-key widgets are not tenant-scoped | P0 |
| 7 | Insecure-by-default production settings | P0 |
| 8 | Secrets and a populated SQLite DB ship inside the image | P0 |
| 9 | `allow_late=False` is silently ignored | P1 |
| 10 | Daily Challenge is inert; streaks can never be non-zero | P1 |
| 11 | Finalization race fallback raises `TransactionManagementError` | P1 |
| 12 | Expiry sweep ignores the grace window | P1 |
| 13 | Expiry sweep is never scheduled | P1 |
| 14 | Sampled pools silently shrink the exam and vary `max_score` | P1 |
| 15 | Item weights / blueprint distribution ignored | P1 |
| 16 | Immediate feedback locks the answer but returns no feedback | P1 |
| 17 | Feedback policy is ignored entirely for sampled attempts | P1 |
| 18 | All timestamps render in UTC; org timezone never activated | P1 |
| 19 | `seed_demo` schedules the daily challenge on the UTC date | P1 |
| 20 | Accommodation selection is wrong in three ways | P1 |
| 21 | Finalized attempts still render an editable attempt page | P1 |
| 22 | Conflict/expiry fragments never reach the student | P1 |
| 23 | `submitted` / `expired` statuses are dead; `voided_by_admin` missing | P1 |
| 24 | `Response.saved_at` never updates | P1 |
| 25 | WhiteNoise installed but unwired — static 404 in production | P2 |
| 26 | No cache backend, no logging, no error reporting | P2 |
| 27 | `/readyz` under-checks; health endpoints unthrottled | P2 |
| 28 | Dockerfile: runtime `collectstatic` cannot write; no healthcheck | P2 |
| 29 | Compose: no dependency health gating, weak credentials, no beat store | P2 |
| 30 | SQLite is the default database; no connection health checks | P2 |
| 31 | No rate limiting anywhere | P2 |
| 32 | Missing indexes on the hot paths | P2 |
| 33 | N+1 queries and unbounded, unpaginated lists | P2 |
| 34 | Tooling declared but not configured; pinned Django ≠ installed Django | P2 |
| 35 | No password reset, registration, lockout, or session hardening | P2 |
| 36 | Unique blank email blocks a second user without an address | P2 |
| 37 | Dead statement in `publish_quiz` | P3 |
| 38 | `publish_quiz` version-number race is unhandled | P3 |
| 39 | Non-POST returns 400 instead of 405 | P3 |
| 40 | No-membership handling is inconsistent (200 vs 403) | P3 |
| 41 | Instructor link shown to students, guaranteeing a 403 | P3 |
| 42 | Demo credentials hardcoded in the login template | P3 |
| 43 | Primary-org selection relies on alphabetical role ordering | P3 |
| 44 | Unvalidated `Organization.timezone` can 500 the app | P3 |
| 45–53 | Scope gaps vs the product spec | — |

---

# P0 — Critical

## 1. `is_staff` bypasses all tenant isolation
**Where:** `assessments/models.py:14-15`, `assessments/services.py:64-65`, `assessments/services.py:155-156`, `assessments/views.py:117`, `assessments/views.py:191`

`TenantScopedQuerySet.for_user()` returns `self` — the unfiltered global queryset — for any user with
`is_staff=True`. `assert_instructor()`, `_eligible_for_assignment()` and both attempt views have the same
bypass. `is_staff` in Django means only "may open the admin site"; it is routinely granted to a single
tenant's operators.

**[verified]** A staff user with **no membership in any organization** saw another tenant's attempt:
`[PROBE 2] attempts visible to membership-less staff user: 1`

**Fix.** Delete the `is_staff` branch from the queryset and replace it with an explicit, auditable platform
role. Tenant isolation must not be a function of an admin-UI flag.

```python
# accounts/models.py
class User(AbstractUser):
    is_platform_operator = models.BooleanField(default=False)  # deliberate cross-tenant access

# assessments/models.py
class TenantScopedQuerySet(models.QuerySet):
    def for_user(self, user):
        if not user.is_authenticated:
            return self.none()
        if getattr(user, "is_platform_operator", False):
            return self          # and log the access — see finding 5
        return self.filter(
            organization_id__in=user.memberships.filter(is_active=True)
                                   .values("organization_id")
        )
```
Apply the same substitution at `services.py:64`, `services.py:155`, `views.py:117`, `views.py:191`.

---

## 2. Instructor dashboard leaks every organization the viewer belongs to
**Where:** `assessments/views.py:77-92`, `assessments/views.py:27-33`

`instructor_dashboard` authorises against `_primary_membership(...)` — a *single* membership — then queries
`QuizAssignment.objects.for_user()` / `Attempt.objects.for_user()`, which span **all** of the user's
memberships regardless of role. `_primary_membership` orders by `role` alphabetically, so `instructor`
outranks `student`. A user who instructs at org A and merely studies at org B passes the instructor check
via org A and is then shown org B's gradebook.

**[verified]**
```
[PROBE 1] instructor_dashboard status: 200
[PROBE 1] sees org-B assignment title: True
[PROBE 1] sees org-B student name: True
```

**Fix.** Bind the request to one organization and filter by that organization, not by the union of memberships.

```python
@login_required
def instructor_dashboard(request):
    org = get_active_organization(request)          # explicit selector, see finding 43
    assert_instructor(request.user, org)
    assignments = (QuizAssignment.objects.for_user(request.user)
                   .filter(organization=org)
                   .select_related("quiz_version", "item_pool", "course", "cohort")
                   .annotate(attempt_total=Count("attempts")))
    attempts = (Attempt.objects.for_user(request.user)
                .filter(organization=org)
                .select_related("student", "assignment")[:20])
```
Add a regression test asserting a dual-membership user sees zero org-B rows.

---

## 3. Manual-release feedback policy is never enforced
**Where:** `assessments/views.py:196`

`show_detail = attempt.status == Attempt.Status.FINALIZED` — the quiz's `feedback_policy` is never consulted.
`FeedbackPolicy.MANUAL` ("score and audit immediately, hide student-facing results until ResultRelease is
opened", spec §4.5) therefore behaves identically to `AT_END`. `result_detail.html:43-48` even renders an
"Results are not released yet" branch that is unreachable.

**[verified]**
```
[PROBE 3] feedback_policy: manual
[PROBE 3] answer key rendered anyway: True
```

**Fix.** Introduce the missing `ResultRelease` record and gate on policy:

```python
def _results_visible(attempt, viewer) -> bool:
    if attempt.status != Attempt.Status.FINALIZED:
        return False
    version = attempt.quiz_version
    policy = version.feedback_policy if version else attempt.assignment.feedback_policy
    if policy == Quiz.FeedbackPolicy.MANUAL:
        return ResultRelease.objects.filter(
            assignment=attempt.assignment, is_open=True).exists()
    return True
```
Release/withhold actions must be audited (spec §3.5).

---

## 4. The answer key is released while retakes are still open
**Where:** `assessments/views.py:186-223`, `assessments/services.py:265-270`

`result_detail` renders every choice with its `is_correct` flag as soon as attempt 1 finalizes, while
`attempt_limit` may still permit attempt 2 of the *same fixed form*. The student reads the key, then retakes.
This defeats the product's stated thesis; the seed data ships exactly this configuration
(`attempt_limit: 2` with a fixed form, `seed_demo.py:164`).

**Fix.** Withhold per-question correctness until no further attempt is possible, or until the availability
window closes:

```python
attempts_used = Attempt.objects.filter(student=attempt.student,
                                       assignment=attempt.assignment).count()
window_closed = bool(a.closes_at and timezone.now() > a.closes_at)
show_key = _results_visible(attempt, request.user) and (
    window_closed or attempts_used >= effective_attempt_limit(a))
```
Show score-only until then. For fixed-form multi-attempt assignments, prefer sampled form (finding 14).

---

## 5. Admin can mutate attempt snapshots, responses and scores; audit rows are deletable
**Where:** `assessments/admin.py:122-158`

`AttemptChoiceSnapshotInline` exposes `is_correct` as an **editable** field on a live attempt; `ResponseAdmin`
exposes `selected_choices` via `filter_horizontal`; `AttemptFinalizationAdmin` exposes `score`. All three are
fully mutable with no `AdminActionLog` entry. `ResponseEventAdmin` and `AttemptAuditAdmin` inherit default
add/change/delete permissions, so the append-only audit trail can be edited and deleted — directly
contradicting ADR-0011 and spec §8.

**Fix.**

```python
class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request, obj=None): return False
    def has_change_permission(self, request, obj=None): return False
    def has_delete_permission(self, request, obj=None): return False

@admin.register(models.AttemptAudit)
class AttemptAuditAdmin(ReadOnlyAdmin): ...
# same for ResponseEvent, AttemptFinalization, AttemptItemSnapshot,
# AttemptChoiceSnapshot, Response, QuizVersion, VersionQuestion, VersionChoice.
```
Score correction must go through a service that voids and re-finalizes with an audit row
(`voided_by_admin`, finding 23), never through direct field edits. Add DB-level protection with a
`post_save`/`pre_delete` guard or a Postgres rule on the audit tables.

---

## 6. Admin foreign-key widgets are not tenant-scoped
**Where:** `assessments/admin.py:6-8` and every `OrganizationFilterAdmin` subclass

`OrganizationFilterAdmin` sets `list_filter` only. It never overrides `get_queryset`,
`formfield_for_foreignkey` or `formfield_for_manytomany`. Any org-admin with admin access can list every
tenant's rows and can attach org A's `QuizVersion`, `ItemPool`, `Course` or explicit students to org B's
assignment. Nothing in the model layer rejects the cross-tenant graft — `QuizAssignment` has no `clean()`.

**Fix.**

```python
class OrganizationFilterAdmin(admin.ModelAdmin):
    org_fields = ()   # FK names to constrain

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs if request.user.is_platform_operator else qs.for_user(request.user)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name in self.org_fields and not request.user.is_platform_operator:
            kwargs["queryset"] = db_field.remote_field.model.objects.for_user(request.user)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)
```
And enforce it in the domain, not only in the UI:

```python
class QuizAssignment(OrganizationScopedModel):
    def clean(self):
        if self.form_mode == self.FormMode.FIXED and not self.quiz_version_id:
            raise ValidationError({"quiz_version": "Fixed forms require a quiz version."})
        if self.form_mode == self.FormMode.SAMPLED and not self.item_pool_id:
            raise ValidationError({"item_pool": "Sampled forms require an item pool."})
        for name in ("quiz_version", "item_pool", "course", "cohort"):
            related = getattr(self, name, None)
            if related and related.organization_id != self.organization_id:
                raise ValidationError({name: "Cross-organization reference."})
```
Note this also converts finding 51's runtime `ValidationError` at attempt-start into an authoring-time error.

---

## 7. Insecure-by-default production settings
**Where:** `config/settings.py:26-31`, `config/settings.py:138-141`

- `DEBUG` defaults to **on** (`os.environ.get("DEBUG", "1") == "1"`). A missing env var yields a debug
  server in production, exposing tracebacks and settings.
- `SECRET_KEY` falls back to the literal `"dev-only-change-me"`. Forged session cookies and password-reset
  tokens follow.
- No `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`, `SECURE_SSL_REDIRECT`, or `SECURE_HSTS_SECONDS`.
- `SECURE_PROXY_SSL_HEADER` is set **unconditionally**, so any client can assert `X-Forwarded-Proto: https`
  when the app is not behind a header-stripping proxy.

**[verified]** `check --deploy` reports W004, W008, W009, W012, W016; probe: `SESSION_COOKIE_SECURE: False | CSRF_COOKIE_SECURE: False | SECURE_SSL_REDIRECT: False | SECURE_HSTS_SECONDS: 0`.

**Fix.**

```python
DEBUG = os.environ.get("DEBUG", "0") == "1"          # secure default

SECRET_KEY = os.environ["SECRET_KEY"] if not DEBUG else os.environ.get(
    "SECRET_KEY", "dev-only-change-me")              # hard failure in production

if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_SSL_REDIRECT = True
    SECURE_HSTS_SECONDS = 31_536_000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_REFERRER_POLICY = "same-origin"
    if os.environ.get("TRUST_PROXY_SSL_HEADER") == "1":
        SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
```
Add `check --deploy` to CI as a gate (spec §16.8).

---

## 8. Secrets and a populated SQLite database ship inside the image
**Where:** `.env`, `db.sqlite3`, `Dockerfile:13`

There is no `.dockerignore` and no `.gitignore`. `COPY . .` therefore bakes into the image:
`.env` (containing `SECRET_KEY=dev-only-change-me` and `DEBUG=1`), `db.sqlite3` (741 KB of real data),
`runserver.log`, `runserver.err`, and `.ruff_cache`. Compose additionally loads that same `.env` as
`env_file`, so `docker compose up` runs the stack with `DEBUG=1` and the dev secret key. The Postgres
password is the hardcoded literal `quizops`.

**Fix.**
1. Delete `.env`, `db.sqlite3`, `runserver.log`, `runserver.err`, `.ruff_cache` from the tree.
2. Add `.gitignore` and `.dockerignore` covering `.env*`, `*.sqlite3`, `__pycache__/`, `.ruff_cache/`,
   `staticfiles/`, `*.log`, `*.err`.
3. Rotate the secret key; source secrets from the orchestrator, not a file baked into the image.
4. Generate the Postgres password per environment.

---

# P1 — High

## 9. `allow_late=False` is silently ignored
**Where:** `assessments/models.py:315`, `assessments/services.py:291`

`allow_late` appears in exactly one place in the codebase — its own field declaration. `start_attempt`
computes `is_late` and stores it, but no code path ever reads `allow_late`. Spec §3.5 requires late attempts
to be "accepted and flagged late per policy, not blocked, **unless the assignment disallows late attempts**".

**[verified]** `[PROBE 4] allow_late: False | attempt created anyway: True | flagged late: True`

**Fix.** In `start_attempt`, after the window check:

```python
is_late = bool(assignment.due_at and now > assignment.due_at)
if is_late and not assignment.allow_late:
    raise PermissionDenied("This assignment no longer accepts late attempts.")
```
Also reject late *finalization* if policy requires it, and surface "late" in the gradebook.

---

## 10. The Daily Challenge is inert; streaks can never be non-zero
**Where:** `assessments/services.py:417-455`, `assessments/views.py:197`, `templates/assessments/daily.html:21`

`DailyParticipation` is never created anywhere in the codebase — grep finds only the model definition, an
admin registration, and two read-only queries. Consequently `recompute_streak()` always sees an empty date
list, `StreakCache` is always 0/0, and the dashboard's streak tiles are permanently zero. The daily page's
only call to action links back to `/` — there is no way to *start* the challenge at all. Spec §4.3 requires
the participation record to be written **inside the finalization transaction**; ADR-0006/0014 claim this is
implemented.

**Fix.** Write participation during finalization, and update the cache post-commit:

```python
@transaction.atomic
def finalize_attempt(attempt, reason="submitted", actor=None):
    ...
    challenge = DailyChallenge.objects.filter(
        organization=attempt.organization,
        quiz_version=attempt.quiz_version,
        is_archived=False).first()
    if challenge:
        local_today = today_for_org(attempt.organization)
        earned = (challenge.release_date == local_today
                  and reason == "submitted")          # finalized inside the live window
        DailyParticipation.objects.get_or_create(
            organization=attempt.organization, student=attempt.student,
            challenge=challenge,
            defaults={"attempt": attempt, "credit_date": challenge.release_date,
                      "earned_credit": earned})
    transaction.on_commit(
        lambda: recompute_streak_cache.delay(attempt.student_id, attempt.organization_id))
```
Add a `daily_challenge` start view (attempt limit 1, live window only), and an archive practice mode that
never earns credit (spec §3.7).

---

## 11. Finalization race fallback raises `TransactionManagementError`
**Where:** `assessments/services.py:425-430`

```python
try:
    finalization = AttemptFinalization.objects.create(...)
except IntegrityError:
    return AttemptFinalization.objects.get(attempt=attempt)   # <-- unreachable in practice
```
The whole function is `@transaction.atomic`. Once the INSERT fails, the transaction is poisoned; any
subsequent query raises `TransactionManagementError`, so the recovery `SELECT` cannot run. The lost-race
path — which spec §4.3 designates as the actual guarantee ("the constraint is the guarantee, the lock is the
optimization") — fails with a 500 instead of returning the existing record.

**[verified]**
```
[PROBE 6] insert raised: IntegrityError
[PROBE 6] fallback SELECT FAILED: TransactionManagementError An error occurred in the current transaction.
```

**Fix.** Wrap the failing statement in its own savepoint:

```python
try:
    with transaction.atomic():                # savepoint
        finalization = AttemptFinalization.objects.create(
            attempt=attempt, reason=reason, score=score, max_score=max_score)
except IntegrityError:
    return AttemptFinalization.objects.get(attempt=attempt)
```

---

## 12. The expiry sweep ignores the grace window
**Where:** `assessments/tasks.py:11-12` vs `assessments/services.py:344-347`

The interactive path rejects saves only after `deadline_at + grace_seconds`, but the sweep finalizes at
`deadline_at__lt=now()` with no grace. A student saving inside the grace window can have their attempt
finalized out from under them, and the three convergent expiry paths do not score an identical frozen
response set as spec §4.1/§4.3 require.

**[verified]** With `grace=30s` and the deadline 5 s in the past:
`interactive save-path finalizes: False, sweep finalizes: True`

**Fix.**

```python
@shared_task
def finalize_expired_attempts(batch_size: int = 500) -> int:
    now = timezone.now()
    expired = (Attempt.objects
        .filter(status=Attempt.Status.IN_PROGRESS, deadline_at__isnull=False)
        .annotate(cutoff=ExpressionWrapper(
            F("deadline_at") + F("assignment__grace_seconds") * timedelta(seconds=1),
            output_field=DateTimeField()))
        .filter(cutoff__lt=now)
        .values_list("id", flat=True)[:batch_size])
    count = 0
    for attempt_id in list(expired):
        finalize_attempt(Attempt.objects.get(id=attempt_id), reason="expired")
        count += 1
    return count
```
Note `reason="sweep"` is also inconsistent with the `expired` state; standardise the vocabulary.

---

## 13. The expiry sweep is never scheduled
**Where:** `config/celery.py`, `docker-compose.yml:22-31`

A `beat` service is started, but no `CELERY_BEAT_SCHEDULE` / `app.conf.beat_schedule` exists anywhere, so
Beat runs with an empty schedule and `finalize_expired_attempts` never executes. Abandoned attempts remain
`in_progress` forever. ADR-0012 claims this is configured.

**Fix.**

```python
# config/celery.py
app.conf.beat_schedule = {
    "finalize-expired-attempts": {
        "task": "assessments.tasks.finalize_expired_attempts",
        "schedule": crontab(minute="*"),
    },
    "recompute-streak-cache": {
        "task": "assessments.tasks.recompute_streak_cache_all",
        "schedule": crontab(minute=5, hour="*"),
    },
}
app.conf.task_acks_late = True
app.conf.broker_transport_options = {"visibility_timeout": 3600}
```
Run beat with a durable scheduler (`django-celery-beat`) so schedule state survives restarts, and add
a smoke test asserting the schedule is non-empty.

---

## 14. Sampled pools silently shrink the exam and vary `max_score`
**Where:** `assessments/services.py:229-231`

```python
required_count = min(pool.required_question_count, len(items))
```
If the pool holds fewer approved items than the form requires, the exam quietly shrinks. Different students
sit different-length exams with different `max_score` values, and the raw scores are then compared in one
gradebook. Spec §4.7 requires publish-time validation that warns when the pool is too small relative to the
form.

**[verified]** `[PROBE 9] required=20, pool has 2 -> questions delivered: 2 | max_score: 2.00 (no error raised)`

**Fix.** Refuse to sample an under-sized pool, and validate at authoring time:

```python
if len(items) < pool.required_question_count:
    raise ValidationError(
        f"Pool '{pool.name}' has {len(items)} approved items but the form requires "
        f"{pool.required_question_count}.")
sampled = rng.sample(items, pool.required_question_count)
```
Report scores as percentages of the attempt's own `max_score` wherever forms may differ.

---

## 15. Item weights and blueprint distribution are ignored
**Where:** `assessments/services.py:233-234`, `assessments/models.py:185-191`

`ItemPoolItem.weight` exists but `rng.sample()` draws uniformly. There is no blueprint model at all, so the
topic/difficulty mix promised by spec §4.7/§5.2 is not enforced — two attempts at the same assignment can
have entirely different difficulty profiles while being graded on the same scale.

**Fix.** Implement weighted, stratified sampling keyed to a blueprint
(`{difficulty|tag: count}`), draw per stratum, and record the criteria alongside the seed in
`AttemptAudit` so the form is reproducible.

---

## 16. Immediate feedback locks the answer but returns no feedback
**Where:** `assessments/services.py:382-384`, `assessments/services.py:316-341`

Under `FeedbackPolicy.IMMEDIATE` the response is locked after the first save, but `student_read_model()`
never exposes correctness and `save_status.html` renders only "Saved". The student loses the ability to
change the answer and receives nothing in exchange — strictly worse than `AT_END`.

**[verified]** `[PROBE 5] response locked: True | any correctness exposed to student: False`

**Fix.** Return correctness for the just-locked question only, from the save endpoint (spec §4.4 permits
exactly this narrow exception):

```python
feedback = None
if policy == Quiz.FeedbackPolicy.IMMEDIATE:
    response.is_locked = True
    response.save(update_fields=["is_locked", "saved_at"])
    correct = set(item.choice_snapshots.filter(is_correct=True)
                                       .values_list("id", flat=True))
    feedback = {"is_correct": set(valid_choices) == correct,
                "explanation": item.explanation}
return response, after, feedback
```
Render it in `save_status.html` and disable the inputs in place.

---

## 17. Feedback policy is ignored entirely for sampled attempts
**Where:** `assessments/services.py:382`

```python
if attempt.quiz_version and attempt.quiz_version.feedback_policy == ...
```
Sampled-form attempts have `quiz_version = None`, so the policy check short-circuits and **no** feedback
policy applies. Spec §4.7 is explicit that "scoring, finalization, feedback, and audit are identical to
fixed-form — only the *source of the item set* differs."

**Fix.** Move `feedback_policy` (and `attempt_limit`, `pass_mark_percent`) onto `QuizAssignment` as the
single resolution point, with the version as fallback:

```python
def effective_feedback_policy(assignment) -> str:
    if assignment.feedback_policy:
        return assignment.feedback_policy
    if assignment.quiz_version_id:
        return assignment.quiz_version.feedback_policy
    return Quiz.FeedbackPolicy.AT_END
```

---

## 18. All timestamps render in UTC; the org timezone is never activated
**Where:** `config/settings.py:112`, `templates/assessments/attempt_detail.html:11`, `dashboard.html:32-33`

`TIME_ZONE = "UTC"` and nothing ever calls `timezone.activate()`. Every deadline, open time and close time is
rendered in UTC with no timezone label, to students whose organization is configured
`America/Los_Angeles`. A student reading "Closes Jul 22, 12:00 AM" loses seven hours of the window.
ADR-0020 claims per-organization time.

**[verified]** `[PROBE 10] settings.TIME_ZONE: UTC | org tz: America/Los_Angeles`

**Fix.** Activate the organization's timezone per request and label every rendered time:

```python
class OrganizationTimezoneMiddleware:
    def __init__(self, get_response): self.get_response = get_response
    def __call__(self, request):
        org = getattr(request, "organization", None)
        if org:
            timezone.activate(ZoneInfo(org.timezone))
        try:
            return self.get_response(request)
        finally:
            timezone.deactivate()
```
Render as `{{ attempt.deadline_at|date:"M j, g:i A T" }}`, and add the display-only countdown that ADR-0005
describes but that no template implements.

---

## 19. `seed_demo` schedules the daily challenge on the UTC date
**Where:** `assessments/management/commands/seed_demo.py:196`

`release_date=now.date()` takes the **UTC** date while `current_daily_challenge()` filters against
`today_for_org()` — the organization-local date. Seeding after 17:00 Pacific creates a challenge dated
"tomorrow" in org terms, which the lookup then hides.

**Fix.** `release_date=today_for_org(org)`.

---

## 20. Accommodation selection is wrong in three ways
**Where:** `assessments/services.py:180-191`

a. **Scope leak.** When an assignment has neither `course` nor `cohort`, neither `if` fires and *every*
   accommodation for that student matches — including one granted for an unrelated course.
   **[verified]** `[P2-2] course-1-scoped accommodation applied to a course-less assignment: multiplier=2.00`

b. **"Best" is ill-defined.** `order_by("-multiplier", "-extra_minutes")` picks the largest multiplier, so a
   1.0× + 120-minute grant loses to a 2.0× grant on a 10-minute quiz (20 min vs 130 min).
   **[verified]** `[P2-2] with a 1.0x+120min row present, chosen: multiplier=2.00, extra=0 (120 min ignored)`

c. **`QuerySet | QuerySet`** produces an OR join without `distinct()`, admitting duplicate rows.

There is also no `is_active` / effective-date field on `TimingAccommodation`, so a lapsed accommodation
applies forever.

**Fix.** Select on the actual objective — total accommodated minutes — with explicit Q scoping:

```python
def _best_accommodation(user, assignment):
    scope = Q(course__isnull=True) if not assignment.course_id else (
        Q(course__isnull=True) | Q(course_id=assignment.course_id))
    scope &= Q(cohort__isnull=True) if not assignment.cohort_id else (
        Q(cohort__isnull=True) | Q(cohort_id=assignment.cohort_id))
    base = _effective_time(assignment) or 0
    rows = (TimingAccommodation.objects
            .filter(Q(organization=assignment.organization, student=user) & scope)
            .distinct())
    best = max(rows, key=lambda a: base * a.multiplier + a.extra_minutes, default=None)
    return (best.multiplier, best.extra_minutes) if best else (Decimal("1"), 0)
```

---

## 21. Finalized attempts still render an editable attempt page
**Where:** `assessments/views.py:112-127`

`attempt_detail` redirects only when `maybe_finalize_expired()` returns `True`. An attempt that is already
`FINALIZED` — or that has no deadline at all — renders the full question form with live Save and Submit
buttons. Every save then fails server-side with `AttemptExpired`, whose fragment the browser discards
(finding 22), so the student sees a working form that silently does nothing.

**[verified]** `[PROBE 8] status: 200 | finalized attempt still shows Save button: True | Submit button: True`

**Fix.**

```python
if attempt.status != Attempt.Status.IN_PROGRESS:
    return redirect("result_detail", attempt_id=attempt.id)
if maybe_finalize_expired(attempt):
    ...
```

---

## 22. Conflict and expiry fragments never reach the student
**Where:** `assessments/views.py:144-159`, `templates/assessments/attempt_detail.html:58-66`

`save_answer` renders `save_status.html` with HTTP **409** and **410**. htmx 2.x does not swap non-2xx
responses by default (`responseHandling` swaps 2xx only), so both fragments are discarded and the student
sees no status change whatsoever. Worse: the `htmx:afterSwap` handler is the only thing that resyncs
`.revision-input`, so after a conflict every subsequent save posts the same stale revision and 409s forever —
the attempt becomes permanently unsavable until a manual page reload. This violates spec §4.2
("Save response returns a small accessible fragment: saved, unsaved, expired, locked, or conflict.
Network failure must be visible to the student; the UI must not imply an answer is saved when it is not.").

The `revision` echoed on the 409 path is also the pre-fetch value from the outer `attempt` object, not the
authoritative one read under the row lock.

**Fix.** Either configure htmx to swap error responses,

```js
htmx.config.responseHandling = [
  {code: "204", swap: false},
  {code: "[23]..", swap: true},
  {code: "409", swap: true, error: false},
  {code: "410", swap: true, error: false},
  {code: "[45]..", swap: true, error: true},
];
document.body.addEventListener("htmx:sendError", showOfflineBanner);
```
or return 200 with a status field in the fragment and signal state via `HX-Trigger`. In both cases send the
**current** revision (`attempt.refresh_from_db()` before rendering) so the client can recover without a
reload, and add `role="status" aria-live="polite"` to `save_status.html` (accessibility is a stated product
requirement).

---

## 23. `submitted` / `expired` statuses are dead; `voided_by_admin` is missing
**Where:** `assessments/models.py:327-331`, `assessments/services.py:435`

`finalize_attempt` sets `status = FINALIZED` for every reason, so `SUBMITTED` and `EXPIRED` are unreachable
and the state machine collapses to two states. The `voided_by_admin` state required by spec §3.6 does not
exist, so there is no legitimate way to undo a bad attempt (see finding 5). `dashboard.html:53` string-compares
`attempt.status == 'in_progress'`, which will silently break if the enum value changes.

**Fix.** Model the states the spec defines, transition explicitly, and add a `void_attempt()` service that
writes an `AttemptAudit` row.

---

## 24. `Response.saved_at` never updates
**Where:** `assessments/services.py:381-384`

`saved_at` is `auto_now=True`, but changing the answer only calls `response.selected_choices.set(...)` — an
M2M write that does not touch the `Response` row. `response.save()` runs only under the IMMEDIATE policy. The
"Saved at" the student sees is therefore the time the row was first created, which for the default policy is
the moment the question was first touched, not the moment the current answer was stored.

**Fix.** `response.save(update_fields=["saved_at"])` after every accepted `.set()`.

---

# P2 — Medium (production readiness)

## 25. WhiteNoise is installed but unwired — static 404 in production
`whitenoise==6.11.0` is pinned; it appears in no middleware and no `STORAGES` entry.
**[verified]** `[P2-6] whitenoise in MIDDLEWARE: False`. With `DEBUG=0` under gunicorn, Django serves no
static files, so `app.css` 404s and the product renders unstyled — despite the Dockerfile running
`collectstatic`.
**Fix:** insert `whitenoise.middleware.WhiteNoiseMiddleware` directly after `SecurityMiddleware` and set
`STORAGES["staticfiles"] = {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"}`.

## 26. No cache backend, no logging, no error reporting
`django-redis` is pinned and unused; `CACHES` is unset, so the default LocMemCache is per-process and
useless for rate limits or shared state. `LOGGING` is empty (**[verified]** `LOGGING configured: False`),
so there is no structured logging with request/user/org/attempt ids and no Sentry, both required by spec §15.
Sessions are DB-backed by default.
**Fix:** configure a Redis `CACHES` entry, a JSON `LOGGING` dict with a request-id filter, and Sentry with
PII scrubbing (never log answer keys or export URLs, spec §12).

## 27. `/readyz` under-checks; health endpoints are unthrottled
`readyz` checks only the database; spec §15 requires database, Redis, **and** worker expectations. Both
endpoints are unauthenticated and open a DB connection per request, making `/readyz` a cheap amplification
target.
**Fix:** probe broker/worker (`app.control.ping(timeout=1)`), cache the result for a few seconds, return 503
with a component map on failure, and restrict or rate-limit the endpoints.

## 28. Dockerfile: runtime `collectstatic` cannot write; no healthcheck
`collectstatic` runs as **root** at build time, creating a root-owned `/app/staticfiles`; the container then
switches to `appuser`, and `docker-compose.yml:5` re-runs `collectstatic` at startup — as `appuser`, into a
root-owned directory. There is no `HEALTHCHECK`, no gunicorn worker/timeout tuning, and no
`--max-requests` recycling.
**Fix:** `RUN chown -R appuser:appuser /app` before `USER appuser`; drop the redundant runtime
`collectstatic`; add `HEALTHCHECK CMD curl -f http://localhost:8000/healthz/`; set
`--workers`, `--timeout`, `--access-logfile -`.

## 29. Compose: no dependency health gating, weak credentials, no beat store
`depends_on` lists services without `condition: service_healthy`, so `web` runs `migrate` before Postgres
accepts connections; the first `docker compose up` is a coin flip. Postgres has no healthcheck and a
hardcoded `quizops/quizops` credential pair. No named volume for static, no `restart:` policy, no
`django-celery-beat` schedule store, and gunicorn is exposed directly on `:8000` with no TLS terminator.
**Fix:** add healthchecks + `condition: service_healthy`, env-supplied credentials, `restart: unless-stopped`,
and a reverse proxy in front.

## 30. SQLite is the default database; no connection health checks
`dj_database_url.config(default="sqlite:///…")` makes SQLite the fallback for any environment that forgets
`DATABASE_URL` — spec §15 says "PostgreSQL for dev and production. No SQLite." SQLite also cannot honour the
`select_for_update()` the finalization design depends on for real concurrency. `conn_max_age=60` is set
without `conn_health_checks=True`, so pooled connections go stale after a database restart.
**Fix:** require `DATABASE_URL`, reject non-postgres schemes when `DEBUG=0`, and add `conn_health_checks=True`.

## 31. No rate limiting anywhere
Spec §12 requires rate limits on login, attempt creation, answer saves, and exports. None exist; the login
view is Django's stock `LoginView` with no throttling or lockout.
**Fix:** add `django-axes` (or `django-ratelimit`) for login, and a Redis-backed limiter on
`save_answer` / `start_assignment`.

## 32. Missing indexes on the hot paths
`Attempt(status, deadline_at)` — the sweep's filter — is unindexed, as are
`QuizAssignment(organization, is_active)` and `DailyParticipation(organization, student, credit_date)`.
The sweep degrades to a full table scan as attempt volume grows.
**Fix:**
```python
class Meta:
    indexes = [
        models.Index(fields=["status", "deadline_at"], name="attempt_sweep_idx"),
        models.Index(fields=["organization", "student", "-started_at"]),
    ]
```

## 33. N+1 queries and unbounded, unpaginated lists
`instructor_dashboard.html:25` calls `{{ assignment.attempts.count }}` per row (one query each);
`result_detail` issues a `choice_snapshots.filter(is_correct=True)` query per item plus another per row in
the template; `dashboard` fetches every active assignment in every org the user belongs to with no
`LIMIT` and no pagination.
**Fix:** `annotate(Count("attempts"))`, prefetch choices once and partition in Python, and paginate.

## 34. Tooling declared but not configured; pinned Django ≠ installed Django
`pytest` and `pytest-django` are pinned with no `pytest.ini`/`pyproject.toml`/`setup.cfg`, so
`python -m pytest` cannot find `DJANGO_SETTINGS_MODULE` and fails immediately. `ruff` is pinned with no
configuration (a `.ruff_cache/` is nonetheless committed). `requirements.txt` pins `Django==5.2.15` while
the environment this audit ran in has **Django 6.0.7** — the delivered pin set is not what was tested.
**Fix:** add `[tool.pytest.ini_options] DJANGO_SETTINGS_MODULE = "config.settings"` and a `[tool.ruff]`
section to `pyproject.toml`; reconcile the Django pin; add a lockfile.

## 35. No password reset, registration, lockout, or session hardening
**[verified]** `/accounts/password_reset/ -> 404`. Only `login`/`logout` are wired; `accounts/views.py` is
empty. There is no invite or self-registration flow, no email verification, no MFA, and no re-authentication
before sensitive actions. `SESSION_COOKIE_AGE` is the 14-day default with
`SESSION_EXPIRE_AT_BROWSER_CLOSE = False` — a long-lived cookie on shared exam machines.
**Fix:** `path("accounts/", include("django.contrib.auth.urls"))` with real templates and a configured email
backend; shorten the session; add lockout.

## 36. Unique blank email blocks a second user without an address
`accounts/models.py:8` sets `email = EmailField(unique=True)` while `AbstractUser` keeps `email` optional and
`USERNAME_FIELD = "username"`. Any second user created without an email violates the constraint.
**[verified]** `[P2-3] second blank-email user REJECTED: UNIQUE constraint failed: accounts_user.email`
**Fix:** either make email genuinely required (`blank=False` plus form validation) or replace the column
constraint with `UniqueConstraint(fields=["email"], condition=~Q(email=""))`.

---

# P3 — Low (quality & consistency)

## 37. Dead statement in `publish_quiz`
`assessments/services.py:133` — `Choice.objects.filter(question=question).order_by("sort_order", "id")`
builds a queryset and discards it. Delete it (the following loop uses the prefetched `question.choices.all()`).

## 38. `publish_quiz` version-number race is unhandled
`services.py:105-108` computes `max(version_number) + 1` then inserts. Two concurrent publishes raise an
unhandled `IntegrityError` (500). Retry on conflict or use a DB sequence per quiz.

## 39. Non-POST returns 400 instead of 405
`views.py:98`, `133`, `176`. **[verified]** `GET /assignments/<id>/start/ -> 400 (expected 405)`.
Use `@require_POST`, which returns 405 with a correct `Allow` header.

## 40. No-membership handling is inconsistent
`dashboard` renders a friendly 200 page; `instructor_dashboard` and `daily_challenge` raise
`PermissionDenied` (403) for the identical condition. Pick one and centralise it in a decorator.

## 41. Instructor link is shown to students, guaranteeing a 403
`templates/base.html:17` renders the Instructor link for every authenticated user.
**[verified]** `nav shows Instructor link to a student: True | student hitting /instructor/ gets: 403`.
Gate on a context-processor-supplied `is_instructor`.

## 42. Demo credentials are hardcoded in the login template
`templates/registration/login.html:8,22` prints `student`/`instructor`/`admin` and the password
`quizops123` on the production login page. Move behind `{% if debug %}` or delete.

## 43. Primary-org selection relies on alphabetical role ordering
`views.py:27-33` — `order_by("role", ...)` happens to rank instructor > org_admin > student alphabetically.
This is accidental, undocumented, and breaks the moment a role is renamed. There is also no organization
switcher, so a multi-org user cannot reach their other organizations at all.
**Fix:** an explicit role-precedence map plus a session-stored `active_organization_id` and a switcher UI.

## 44. Unvalidated `Organization.timezone` can 500 the app
`accounts/models.py:17` is a free `CharField`; `today_for_org()` calls `ZoneInfo()` on it unguarded, so a
typo raises `ZoneInfoNotFoundError` on the dashboard for the entire organization.
**Fix:** `choices=sorted(zoneinfo.available_timezones())` plus a model validator.

Also in this tier: `runserver.log` / `runserver.err` are committed build artefacts;
`accounts/views.py` and `accounts/tests.py` are empty; `assessments/admin.py:71`
(`filter_horizontal = []`) is a no-op; `README.md` documents `/healthz/` and the Celery sweep as operational
when the sweep is unscheduled (finding 13).

---

# Scope gaps vs `Online Quiz Portal.md`

These are not defects in the written code — they are surfaces the spec marks as required for the finished
product ("Full product scope, not an MVP") that have no implementation. Listed for completeness of the
inventory.

| # | Spec area | Status (current) |
|---|---|---|
| 45 | Domain models / pass mark / grade bands | `ResultRelease`, `AdminActionLog`, `IntegrityEvent` shipped. Pass mark on quiz/version/assignment. **Still open:** `RandomAssemblyRun`, grade bands. |
| 46 | Instructor authoring / pools | Instructor quiz + assignment + **item pool/blueprint** UI shipped. |
| 47 | Notifications | Four kinds + Beat jobs + in-app inbox shipped. |
| 48 | Transactional outbox | **Still open.** Post-commit side effects run inline/on_commit today. |
| 49 | Gradebook / analytics / integrity / exports | Shipped (CSV export auth-gated; cleanup job present). |
| 50 | Support console | **Still open** as a product surface; `JobFailure` + Django admin cover ops DLQ. |
| 51 | Background jobs | Expiry, streaks, notifications, export cleanup, **retention** scheduled. |
| 52 | AI draft workflow | **Still open.** `Organization.ai_monthly_budget` unused. |
| 53 | Retention / erasure / ops / tests | Erasure + retention jobs + runbook/CI shipped. |

---

## Recommended order of work

1. **Findings 1, 2, 5, 6** — tenant isolation. Everything else is moot while cross-tenant reads are possible.
2. **Findings 7, 8** — rotate the secret, fix the settings defaults, purge secrets and the SQLite DB from the tree/image.
3. **Findings 3, 4, 22, 21** — assessment integrity: release policy, answer-key timing, and the save path the student actually depends on.
4. **Findings 11, 12, 13, 9, 14** — the engine's correctness invariants and the background sweep.
5. **Findings 10, 16, 17, 18** — the features the README claims are working but are not.
6. **Finding 53** — write the authorization, timing, and tenancy tests that would have caught 1–24, and gate CI on them.
