# Online Quiz Portal / Assessment Engine + Daily Coding Challenge (Enterprise Full-Product Scope v3)

**Working name:** QuizOps  
**Product:** Online Quiz Portal — a server-authoritative assessment engine, instructor authoring workspace, question-bank platform, and Daily Coding Challenge product.  
**Type:** Standalone capstone product.  
**Delivery posture:** Full product scope, not an MVP. The build sequence is an implementation order only; every major surface below is required for the finished application.  
**Stack:** Python · Django · HTMX · PostgreSQL · Celery + Celery Beat + Redis · server-rendered web UI.  
**Boundary:** DB + Web. No public JSON API. Internal HTMX endpoints and operational endpoints are allowed. No Channels/WebSockets. No SPA framework. No online judge. No student-submitted code execution.

> **Changes in v3 (assessment-depth pass).** Scope is *not* reduced. v2 was already mature — no hard self-contradiction to fix — so this revision adds assessment-domain depth and pins a few implicit decisions. The integrity model in v2 defends well against *client tampering*; v3 also addresses *answer sharing*, *accommodations*, and *multi-tenant time*, which are the threats and requirements that distinguish a real assessment engine.
> - **Added — sampled-form mode (answer-sharing defense):** an assignment may bind to an item *pool*/blueprint and sample a per-attempt equivalent form, snapshotting the sampled `VersionQuestion` set onto the attempt. The single-frozen-form mode remains for low-stakes use (§3.5, §3.6, §4.7, ADR-0016).
> - **Added — structural answer-key isolation:** the in-progress student read model never loads the correctness column; leakage becomes impossible by construction, with the existing checker as backstop (§4.4, §6.4, §12, ADR-0017).
> - **Added — timing accommodations:** per-student extended-time multipliers scale `deadline_at` at attempt start (legal/ADA requirement) (§3.1, §4.1, ADR-0018).
> - **Added — `ResponseEvent` append-only log:** backs time-spent, change-frequency, and optimistic concurrency; v2's analytics outran its model (§3.6, §4.2, §9, ADR-0019).
> - **Changed — per-organization timezone:** daily-challenge release and streak math compute in the org's timezone, not one global app timezone (§3.7, §4, ADR-0020).
> - **Added:** deadline grace window + frozen-response-set semantics (§4.1, §4.3); due-window vs availability vs attempt-timer precedence (§3.5); export download authorization (§9.4, ADR-0021); streak-cache update moved post-commit (§4.3); concurrency-safe attempt creation and DB-level one-finalization constraint (§3.6, §4.3); tenant-scoped querysets (§3.1a, ADR-0022); scoring-policy surface defined (§4.4); AI cost quota + content-safety review (§3.3, §12).

---

## 0. Product Thesis

The product is not merely a quiz app. It is a **trustworthy assessment operating system** for education, bootcamps, internal training teams, and coding-practice programs.

The architectural claim is:

> Every integrity-sensitive fact is owned by the server: eligibility, timing, attempt limits, shuffling, answer persistence, scoring, feedback visibility, daily challenge release, streak credit, audit, and finalization.

The product must demonstrate mastery of Python and system architecture through explicit domain modeling, immutable publishing, idempotent transactional finalization, rigorous testing, operational observability, and clean Django + HTMX web interactions.

---

## 1. Locked Decisions

| # | Area | Decision |
|---|---|---|
| 1 | Product category | Education/training assessment portal with daily coding challenge delivery mode. |
| 2 | Interface | Django templates + HTMX. No SPA framework. Minimal JavaScript only for countdown display, HTMX hooks, and small UX helpers. |
| 3 | API boundary | DB + Web only. No public API. Internal HTMX endpoints are allowed and must enforce the same server-side rules as full-page views. |
| 4 | Roles | Student, Instructor, Organization Admin, Platform Admin. |
| 5 | Tenancy | Organization → Program/Course/Cohort. Users may belong to multiple organizations through memberships. |
| 6 | Question types | Single-correct multiple choice, multi-select, true/false. |
| 7 | Grading | Auto-graded only. No essay, short-answer, manual grading, or subjective rubric engine. |
| 8 | Multi-select scoring | All-or-nothing exact set match. Partial credit is a documented future extension, not built. |
| 9 | Code execution | Explicitly out of scope. Coding challenges are coding-themed quizzes, not an online judge. |
| 10 | Timing | Server-authoritative timed attempts. Untimed quizzes allowed when no time limit is configured. |
| 11 | Attempt limits | Server-enforced per student + quiz + access scope. Daily live challenges enforce one live attempt per student per challenge. |
| 12 | Shuffling | Question and choice order may be shuffled per attempt; the generated order is persisted and auditable. |
| 13 | Answer persistence | Incremental per-question save is required for timed attempts and audit integrity. |
| 14 | Feedback policies | Immediate-per-question, at-end-of-attempt, manual-release. Immediate feedback locks that response. |
| 15 | Versioning | Published content is immutable. Attempts reference QuizVersion, VersionQuestion, and VersionChoice only. |
| 16 | Finalization | Transactional, idempotent, row-locked, and safe under submit/expiry/sweep races. |
| 17 | Daily Coding Challenge | Delivery mode on the same engine: one coding-themed published QuizVersion scheduled per date. |
| 18 | Daily release | Server-authoritative release moment in the configured application timezone. Client clocks do not matter. |
| 19 | Streaks | Server-computed from immutable DailyParticipation records. Cache is recomputable and never the source of truth. |
| 20 | Content source | Questions must be original, instructor-authored, AI-drafted then human-approved, licensed, public-domain, or explicitly permitted for reuse. External coding sites are inspiration/category references only unless reuse permission exists. |
| 21 | AI content | AI may generate drafts only. Human review and approval are required before a question can enter the publish pipeline. |
| 22 | Background work | Celery + Redis are required for expiry sweeps, notifications, async exports, analytics aggregation, streak recomputation, and AI draft generation. |
| 23 | Production posture | PostgreSQL, Docker, CI, Sentry, structured logs, backups, restore drill, HTTPS settings, and operational runbook. |
| 24 | Form mode | Two assignment modes: **fixed form** (one frozen QuizVersion, low-stakes/practice) and **sampled form** (per-attempt equivalent form sampled from a pool/blueprint, snapshotted onto the attempt) to defeat answer sharing in higher-stakes use. |
| 25 | Answer-key isolation | The in-progress student read path uses a projection that excludes the correctness column entirely; leakage is impossible by construction, not only test-checked. |
| 26 | Accommodations | Per-student timing accommodations (extended-time multipliers) scale the server deadline. Treated as a first-class assessment + accessibility requirement. |
| 27 | Response history | Saves are recorded in an append-only `ResponseEvent` log (server-timestamped); the current answer is a derived/latest view. Backs analytics and optimistic concurrency. |
| 28 | Time authority | Daily-challenge release dates and streak windows are computed in the **organization's** timezone (DST-aware), not a single global app timezone. |
| 29 | Deadline tolerance | An explicit, small grace window absorbs network/processing latency; all finalization paths score the identical response set frozen at the deadline. |
| 30 | Export delivery | Report exports (containing student PII) are served auth-gated, org-scoped, expiring, and download-audited — never via guessable or long-lived URLs. |

---

## 2. Product Surfaces

### 2.1 Student Surface

Students can:

- View assigned quizzes and open daily challenges.
- Start eligible attempts only when the server says they may.
- Save answers incrementally through HTMX.
- See a display-only timer for timed attempts.
- Submit attempts or be auto-finalized on expiry.
- View results according to feedback policy.
- See their own attempt history.
- See Daily Coding Challenge current streak and longest streak.
- Practice archived daily challenges without earning streak credit.
- Receive in-app/email notifications for assignments, due-soon reminders, daily challenge availability, and result releases.

Students cannot:

- See answer keys for in-progress attempts.
- See other students' results.
- Bypass attempt limits, release windows, timing, manual-release states, or assignment gates.
- Earn streak credit from archived practice or late finalization.

### 2.2 Instructor Surface

Instructors can:

- Manage question banks.
- Author draft questions and quizzes.
- Generate AI draft questions for review.
- Define assessment blueprints.
- Assemble quizzes manually or through blueprint-based randomized assembly.
- Publish immutable quiz versions.
- Assign quizzes to courses/cohorts/students.
- Schedule Daily Coding Challenges.
- Preview as student before publishing.
- Review gradebook analytics.
- Export CSV/PDF reports.
- Inspect attempt audits and integrity events.
- Release or withhold manually released results.

Instructors cannot:

- Manage other organizations' content.
- See unrelated instructors' private quizzes unless organization policy allows shared banks.
- Alter historical attempts by editing draft content.

### 2.3 Organization Admin Surface

Organization Admins can:

- Manage courses, cohorts, enrollments, instructor memberships, and organization settings.
- View organization-wide gradebook and challenge analytics.
- Manage shared question-bank libraries.
- Audit instructor publishing, assignment, and result-release actions.
- Configure notification defaults and retention policies.

### 2.4 Platform Admin / Support Surface

Platform Admins can:

- Look up organizations, users, quizzes, attempts, exports, and scheduled jobs.
- Inspect attempt timelines and finalization status.
- Debug access/eligibility decisions.
- Recompute streak caches.
- Inspect Celery job failures and expiry sweep results.
- Review answer-key leakage checks.
- Review audit trails and support actions.
- Perform carefully audited recovery operations.

---

## 3. Core Domain Model

### 3.1 Identity, Tenancy, and Enrollment

| Model | Purpose | Notes |
|---|---|---|
| `User` | Authenticated human. | Django custom user from day one. |
| `Organization` | Tenant boundary. | Schools, bootcamps, companies, internal training groups. |
| `OrganizationMembership` | User's role inside an organization. | Student, Instructor, Org Admin. Platform Admin is global staff. |
| `Program` | Optional grouping under Organization. | E.g. Python Bootcamp, Internal Security Training. |
| `Course` | Instructional container. | Owned by organization; may have one or more instructors. |
| `Cohort` | Student group for assignment and reporting. | Drives eligibility and gradebook filters. |
| `Enrollment` | Student membership in course/cohort. | Active/inactive state gates access. |
| `TimingAccommodation` | Per-student timing adjustment. | Extended-time multiplier (e.g. 1.5×, 2.0×) and/or fixed extra minutes, scoped to student × (course/cohort or specific assignment); audited; applied at attempt start. |

Business authorization is always contextual. A user's global identity does not imply access to a quiz, bank, cohort, or result.

### 3.1a Tenant Isolation (systematic, not per-view)

With this many organization-scoped surfaces (banks, quizzes, attempts, gradebooks, exports, analytics, daily challenges, notifications), per-view authorization checks are fragile. Isolation is enforced at the data layer:

- Organization-scoped models expose a `TenantScopedQuerySet`/manager whose default queryset is filtered to the requester's accessible organizations, **deny-by-default**. Retrieving across the boundary requires an explicit, audited platform-admin selector.
- A single stated invariant — *a member of org A receives identical deny behavior when targeting any object of org B, on web and HTMX endpoints, regardless of role within A* — is covered by a generated test over all org-scoped surfaces, so a newly added surface cannot silently skip isolation (ADR-0022).

### 3.2 Question Bank and Content Governance

| Model | Purpose | Notes |
|---|---|---|
| `QuestionBank` | Reusable question collection. | Organization-owned or instructor-private. |
| `QuestionBankItem` | Draft/reusable question entity. | Not used directly in attempts. Must be published into QuizVersion. |
| `QuestionChoice` | Editable choice for bank/draft question. | Correctness remains server-only. |
| `QuestionTag` | Topic/skill labels. | Python, Big O, arrays, SQL, Django, security, etc. |
| `LearningObjective` | What the question measures. | Used by blueprints and analytics. |
| `DifficultyLevel` | Normalized difficulty. | Beginner, easy, medium, hard, expert, or numeric scale. |
| `QuestionSource` | Provenance/licensing metadata. | Original, instructor-authored, AI-drafted, licensed, public-domain, permitted external source. |
| `ContentReview` | Human review state. | Required before AI-generated or imported questions can be published. |

Required provenance fields:

- `source_type`
- `source_name`
- `source_url` when applicable
- `license_type`
- `reuse_allowed`
- `attribution_required`
- `created_by`
- `reviewed_by`
- `reviewed_at`
- `review_status`
- `internal_notes`

Content policy:

- Do not copy problems from third-party coding platforms unless the content is licensed, public-domain, or explicitly permitted for reuse.
- External platforms may be used as inspiration for categories, difficulty expectations, or skill taxonomy only.
- AI-generated questions are drafts and must pass human review before publishing.
- Published assessments must be traceable to approved content records.

### 3.3 AI Draft Workflow

AI is a content-assistance workflow, not an assessment authority.

| Model | Purpose |
|---|---|
| `ContentGenerationRequest` | Instructor request: topic, difficulty, count, question type, learning objective, style. |
| `GenerationPromptVersion` | Versioned prompt template used for generation. |
| `GeneratedQuestionDraft` | AI output captured as editable draft content. |
| `AIReviewStatus` | pending_review, needs_revision, approved, rejected. |
| `InstructorApproval` | Human approval event before bank insertion or publishing. |

Rules:

- Generated drafts cannot be assigned or published directly.
- AI-generated correctness must be reviewed by a human.
- The system should show warnings when an AI-generated item lacks review.
- Prompt/version metadata is retained for auditability and debugging.
- If an AI provider is configured, secrets come from environment variables and responses are logged only in redacted/structured form.
- Generation runs against a **per-organization cost/usage budget** (token or request budget) in addition to rate limits, so a runaway or abusive instructor cannot turn a paid provider into a cost-DoS; budget exhaustion is a clean, surfaced state.
- Human review explicitly covers **content safety and bias**, not only correctness; drafts containing unsafe, offensive, or injected content are rejected at review.
- The provider is accessed behind a thin abstraction so the model/vendor is swappable without touching the review/publish pipeline.

### 3.4 Quiz Authoring, Publishing, and Versioning

| Model | Purpose | Required invariants |
|---|---|---|
| `Quiz` | Draft/authoring root. | Organization/instructor scoped; settings live here before publish. |
| `Question` | Draft question attached to Quiz. | Editable until publish. |
| `Choice` | Draft answer choice. | Editable until publish. |
| `AssessmentBlueprint` | Desired skill/difficulty distribution. | Optional but first-class for serious assessments. |
| `BlueprintSection` | Section/topic rules. | Defines topic, difficulty, question count, points. |
| `RandomAssemblyRun` | Snapshot of blueprint-based quiz generation. | Auditable; records selected questions and seed/config. |
| `QuizVersion` | Immutable publish-time snapshot. | Attempts point here. Draft edits create new versions. |
| `VersionQuestion` | Immutable question as published. | Prompt, type, points, grading metadata. |
| `VersionChoice` | Immutable choice as published. | Text, ordering, correctness flag server-only. |

Publishing rules:

- A draft quiz cannot be assigned until published.
- Publishing validates question completeness and correctness configuration.
- A quiz version is immutable after publication.
- Edits after publication create a new draft/version and never mutate historical attempt content.
- Published versions store settings: timing, attempt limit, shuffle flags, feedback policy, availability, scoring policy.

### 3.5 Assignment, Access, and Release

| Model | Purpose |
|---|---|
| `QuizAssignment` | Grants access to a Quiz/QuizVersion (fixed form) or an `ItemPool`/blueprint (sampled form) through cohort, course, or explicit student. |
| `ItemPool` | Approved-item pool an assignment can sample from for per-attempt form generation. |
| `OpenAvailabilityScope` | Optional open access within an organization or for Daily Challenge. |
| `ResultRelease` | Manual result visibility state. |
| `DueWindow` | Availability and due-date semantics (open/close vs due-date; see precedence below). |
| `AdminActionLog` | Sensitive administrative actions. |

Access rules:

- Attempt creation must pass active account, membership, enrollment, assignment/open scope, availability window, and attempt-limit checks.
- Direct URL guessing cannot start an attempt.
- Manual-release result visibility is server-enforced.
- Release/withhold actions are audited.

**Form mode.** An assignment is either:
- **Fixed form** — points at one immutable `QuizVersion`; every eligible student sees the same item set (right for low-stakes/practice).
- **Sampled form** — points at an `ItemPool` (optionally under a blueprint distribution); each attempt samples its own equivalent form at start, and the sampled `VersionQuestion`/`VersionChoice` set is **snapshotted onto the attempt** (same immutability guarantee as fixed form — attempts never re-sample, audit reproduces exactly what the student saw). This defeats answer sharing in higher-stakes use. Sampling is recorded with seed/criteria/actor, mirroring `RandomAssemblyRun`.

**Window precedence (explicit).** Three distinct concepts, resolved in this order:
1. **Availability window** (open/close) — outside it, no attempt may start or continue.
2. **Attempt time limit** — per-attempt `deadline_at`; if it would extend past the availability close, the attempt deadline is clamped to close (and the student is shown the effective deadline).
3. **Due date** — a soft marker; attempts permitted between due and close are accepted and flagged *late* per policy, not blocked, unless the assignment disallows late attempts.

### 3.6 Attempts, Responses, Scoring, and Audit

| Model | Purpose | Required invariants |
|---|---|---|
| `Attempt` | Concrete student attempt. | References immutable QuizVersion (fixed form) or its own snapshotted item set (sampled form); stores deadline, status, score, attempt number, applied accommodation, optimistic-concurrency revision. |
| `AttemptItemSet` | Per-attempt snapshot of sampled `VersionQuestion`/`VersionChoice` for sampled-form assignments. | Frozen at attempt start; attempts never re-sample; reproduced in audit. |
| `AttemptQuestionOrder` | Persisted per-attempt question order. | Stable on reload; reproduced in audit. |
| `AttemptChoiceOrder` | Persisted per-attempt choice order. | Stable on reload; reproduced in audit. |
| `Response` | Current saved answer (latest view). | References VersionQuestion and selected VersionChoice records; carries a revision for optimistic concurrency. |
| `ResponseEvent` | Append-only log of every save/change. | Server-timestamped; one row per save; backs time-spent, change-frequency, and audit; never mutated. |
| `AttemptFinalization` | Final scoring record. | **DB unique constraint on `attempt`** — one finalization per attempt enforced by the database, not only by service logic. |
| `AttemptAudit` | Immutable audit snapshot. | Presented questions, answers, timestamps, score, policy context, applied accommodation, sampling seed. |
| `IntegrityEvent` | Assessment integrity diagnostics. | Late answer rejected, double-submit prevented, expiry sweep finalized, concurrent-session conflict, etc. |

Attempt states:

```text
not_started
in_progress
submitted
expired
finalized
voided_by_admin
```

Finalization is the only path to score and audit creation. It must be transactional, idempotent, and safe to execute repeatedly.

Concurrency invariants:

- **Attempt creation is concurrency-safe:** a unique constraint on `(student, assignment, attempt_number)` (and `(student, daily_challenge)` for live challenges) prevents two near-simultaneous starts from exceeding the attempt limit; the second loses the race cleanly.
- **Responses use optimistic concurrency:** each save carries the revision it read; a stale revision (a second tab/device) returns a conflict fragment instead of silently overwriting. The `ResponseEvent` log records both attempts for integrity review.

### 3.7 Daily Coding Challenge

| Model | Purpose |
|---|---|
| `DailyChallenge` | Published QuizVersion scheduled to a release date. |
| `DailyParticipation` | Immutable record of completion and streak eligibility. |
| `StreakState` | Recomputable display cache per student. |
| `DailyChallengeArchiveView` | Practice access to past challenges. |

Rules:

- One active DailyChallenge per calendar date **in the organization's configured timezone** (DST-aware), not a single global app timezone. A multi-region SaaS serves a Lagos cohort and a California cohort with correct local midnights.
- Challenge state is derived from server time evaluated in the org timezone: scheduled, live, archived.
- Live challenge attempts reuse the normal attempt engine with attempt limit = 1.
- Archived attempts are practice only and never affect streaks.
- Streak credit requires finalization inside the server-defined active window (org-local).
- Starting before close but finalizing after close does not earn streak credit.
- Streak "consecutive days" are computed in the org timezone; DST transitions and a student who travels across zones resolve against the org's local calendar, not the device's.
- Streak cache is recomputable from DailyParticipation.

---

## 4. Server-Authoritative Assessment Engine

### 4.1 Timing

- At attempt start, the server computes `deadline_at` and stores it.
- **Accommodations applied at start:** if the student has an active `TimingAccommodation` for this assignment/scope, the base limit is scaled (e.g. ×1.5/×2.0) and/or extended by fixed minutes before `deadline_at` is stored. The applied accommodation is recorded on the attempt and in the audit. This is both an accessibility/legal (ADA/504) requirement and an assessment-fairness requirement.
- Client countdown is display-only and reflects the accommodated `deadline_at`.
- Every answer save checks server time.
- **Grace window:** a small, explicit, configurable tolerance (e.g. a few seconds) absorbs network/processing latency — a save or submit arriving within the grace window of `deadline_at` is honored; beyond it, rejected. The tolerance is a stated policy, not an accident of which code path checks the clock first.
- Late responses (past deadline + grace) are rejected.
- Expired attempts finalize through one of three convergent paths, all scoring the identical frozen response set (§4.3):
  1. client countdown triggers submit;
  2. next server request notices expiry and finalizes;
  3. Celery Beat expiry sweep finalizes abandoned attempts.

### 4.2 Incremental Answer Save

- Each answer change posts to the server through HTMX.
- CSRF is required.
- Each accepted save appends a `ResponseEvent` (server-timestamped) and updates the current `Response` with an incremented revision; a stale revision returns a conflict fragment (optimistic concurrency for multi-tab/device).
- Save response returns a small accessible fragment: saved, unsaved, expired, locked, or conflict.
- The server persists the answer before showing immediate feedback.
- Network failure must be visible to the student; the UI must not imply an answer is saved when it is not.

### 4.3 Finalization

Finalization service responsibilities:

- `select_for_update()` the Attempt row.
- Confirm attempt is not already finalized; rely on the **DB unique constraint on `AttemptFinalization.attempt`** so a duplicate finalize cannot create a second record even under a lost race — the constraint is the guarantee, the lock is the optimization.
- Reject or ignore duplicate finalize requests safely.
- Pull the **frozen response set** — the responses as of `deadline_at` (saves past deadline + grace were already rejected, so the set is stable regardless of which path finalizes). All three convergent paths therefore score identical input.
- Score against immutable VersionQuestion / VersionChoice records (the attempt's snapshotted set for sampled forms).
- Create one AttemptFinalization.
- Create one immutable AttemptAudit.
- Write IntegrityEvents.
- For a live DailyChallenge, write the immutable `DailyParticipation` record **inside this transaction** (it is the streak source of truth).
- Emit domain events after transaction commit; the **streak-cache (`StreakState`) update is a post-commit/outbox side effect, not part of the finalization transaction** — since the cache is recomputable and never authoritative (locked decision #19), a streak-cache failure must never roll back a student's score.

### 4.4 Scoring

- Single-correct MC: selected choice equals correct choice.
- True/false: selected choice equals correct choice.
- Multi-select: selected set exactly equals correct set.
- Points are read from VersionQuestion.
- Correctness is never sent to the client during in-progress attempts except locked immediate-feedback response for the just-saved question.

**Scoring-policy surface (defined, not implied).** A published version's scoring policy specifies: raw points per question (always), an optional **pass mark / threshold**, and optional **grade bands** (label + score range) used by the gradebook and result page. Negative marking is **not** in scope (documented exclusion alongside partial credit). If an org needs only raw scores, bands/threshold are simply unset.

**Structural answer-key isolation.** The in-progress student path loads questions/choices through a projection (`.only()`/`.defer()` or a dedicated read model) that **excludes the correctness column entirely** — the correct-answer flag is never present in the object graph the student-facing template or HTMX fragment can render. Leakage is impossible by construction; the answer-key leakage checker (§9.3) is a backstop, not the primary control. Correctness is loaded only inside the finalization service and the locked immediate-feedback response.

### 4.5 Feedback Policies

- **Immediate-per-question:** return correctness after saving and locking that response.
- **At-end-of-attempt:** show results after finalization.
- **Manual-release:** score and audit immediately, hide student-facing results until ResultRelease is opened.

### 4.6 Shuffling

- On attempt start, generate question and choice order using server-side randomness.
- Persist the generated order.
- Use the same order on reload.
- Store order in audit so the instructor can reproduce exactly what the student saw.

### 4.7 Per-Attempt Form Generation (sampled-form mode)

For sampled-form assignments, the answer-sharing weakness of a single shared form is addressed by generating an equivalent form per attempt:

- At attempt start, the engine samples a form from the assignment's `ItemPool` under the blueprint distribution (topic/difficulty mix), drawing only from approved, published items.
- The sampled `VersionQuestion`/`VersionChoice` set is **snapshotted onto the attempt** (`AttemptItemSet`) and is immutable thereafter — attempts never re-sample, exactly as fixed-form attempts never mutate their `QuizVersion`.
- Sampling records seed, criteria, and actor (mirroring `RandomAssemblyRun`) so the form is fully reproducible in audit.
- Item-exposure controls (e.g. avoid drawing the same items for many concurrent attempts in a cohort where the pool allows) are policy-configurable; the pool must be large enough relative to the form size for sampling to be meaningful, and publish-time validation warns when it is not.
- Scoring, finalization, feedback, and audit are identical to fixed-form — only the *source of the item set* differs. This keeps the engine format-blind: the rest of §4 does not care whether the item set was frozen at publish or sampled at start.

---

## 5. Instructor Content Operations

### 5.1 Authoring Workspace

Required surfaces:

- Question bank browser.
- Draft quiz builder.
- Inline HTMX question add/edit/reorder.
- Choice add/edit/reorder.
- Correct answer configuration.
- Blueprint builder.
- Randomized assembly preview.
- Duplicate question warning.
- Missing correct answer warning.
- Invalid multi-select setup warning.
- Preview as student.
- Publish checklist.
- Clone quiz.
- Archive quiz.
- Compare versions.

### 5.2 Assessment Blueprinting

Instructors can define a target distribution:

```text
Example blueprint:
- 40% Python fundamentals
- 25% data structures
- 20% Big O analysis
- 15% debugging
- difficulty mix: 30% easy, 50% medium, 20% hard
```

Blueprints can drive randomized assembly from approved question-bank items. The selected set is frozen when published.

### 5.3 Randomized Assembly

Rules:

- Assembly reads from approved QuestionBankItems only.
- Assembly records selected items, criteria, random seed/config, and actor.
- Instructor can review/edit before publishing.
- Published QuizVersion is immutable.
- Historical attempts never re-run assembly.

### 5.4 Content Quality Checks

Before publishing, system checks:

- Every question has at least one correct answer where required.
- True/false questions have exactly two choices or a normalized representation.
- Multi-select questions have at least one correct choice and one distractor.
- Points are positive.
- No empty prompts or empty choices.
- AI-generated questions are approved.
- Licensing/provenance is present for imported or external-inspired content.
- No duplicate items within the same quiz unless explicitly allowed.

---

## 6. Web Layer: Django + HTMX

The UI is server-rendered. HTMX adds responsiveness without moving integrity rules to the browser.

### 6.1 Student HTMX Workflows

- Start attempt.
- Navigate questions in persisted shuffle order.
- Save answer incrementally.
- Show saved/unsaved state.
- Show display-only countdown.
- Submit attempt.
- Render expired/finalized state.
- View results according to release policy.
- Take Today's Challenge.
- Browse challenge archive as practice.
- View private streak state.

### 6.2 Instructor HTMX Workflows

- Build quizzes inline.
- Add/edit/reorder questions.
- Add/edit/reorder choices.
- Search question bank.
- Generate AI drafts.
- Review/approve AI drafts.
- Build blueprint.
- Preview randomized assembly.
- Publish version.
- Assign quiz.
- Release results.
- Filter gradebook.
- Export gradebook.

### 6.3 Admin/Support HTMX Workflows

- User lookup.
- Organization/course/cohort lookup.
- Attempt timeline.
- Eligibility debugger.
- Result release debugger.
- Daily challenge calendar.
- Streak recomputation action.
- Expiry sweep dashboard.
- Export run status.
- Audit log viewer.

### 6.4 HTMX Quality Rules

- HTMX fragments never include raw answer keys for in-progress attempts.
- Every mutating HTMX request uses POST and CSRF.
- Disabled buttons are UX only; server policies enforce truth.
- Expired attempt saves return an expired/finalization fragment.
- Stale state returns a conflict fragment.
- Focus is restored after swaps.
- Accessible status regions use `aria-live`.
- Countdown is display only and must say so in implementation comments.
- Full-page fallback exists for critical flows where practical.

---

## 7. Notifications

Notification channels:

- In-app notifications.
- Email notifications.

Notification events:

- Quiz assigned.
- Quiz due soon.
- Quiz closing soon.
- Daily challenge live.
- Streak at risk.
- Result released.
- Manual release pending.
- Attempt auto-finalized.
- Export ready.
- AI draft generation completed.

Notification rules:

- Notification preferences are stored per user.
- Email delivery is async through Celery.
- In-app notification creation occurs inside the domain transaction or through post-commit outbox processing.
- Failed deliveries are visible in support tools.

---

## 8. Domain Events, Audit, and Outbox

Separate three concepts clearly:

1. **Audit records** — immutable facts for compliance and review.
2. **Domain events** — business events emitted by domain services.
3. **Outbox events** — durable post-commit side effects to deliver notifications, exports, analytics, or integrations.

Required domain events:

```text
QuizDraftCreated
QuizPublished
QuizAssigned
AttemptStarted
ResponseSaved
ResponseLocked
LateResponseRejected
AttemptSubmitted
AttemptExpired
AttemptFinalized
ResultReleased
DailyChallengeScheduled
DailyChallengeCompleted
StreakUpdated
QuestionGeneratedByAI
QuestionApproved
ExportRequested
ExportCompleted
```

Outbox rules:

- External side effects are represented durably before delivery.
- Workers are at-least-once; handlers are idempotent.
- Use `transaction.on_commit()` before enqueueing tasks dependent on committed state.
- Outbox failures do not corrupt assessment state.

---

## 9. Analytics and Reporting

### 9.1 Gradebook

Required:

- completion status
- attempt count
- best score / latest score
- score distribution
- item difficulty
- distractor/choice counts
- CSV export
- audit drill-down
- manual release status

### 9.2 Advanced Analytics

Required:

- median score
- time-spent distribution
- first-attempt vs best-attempt comparison
- attempt drop-off rate
- late finalization count
- expiry-finalized count
- sweep-finalized count
- response change frequency before lock
- question discrimination indicator
- topic mastery by cohort
- daily challenge completion rate
- streak retention
- AI-generated question performance compared with human-authored items

### 9.3 Result Integrity Dashboard

Shows:

- attempts finalized by normal submit
- attempts finalized by expiry-on-next-request
- attempts finalized by sweep
- double-submit conflicts prevented
- late responses rejected
- immediate-feedback locks applied
- manual-release pending count
- suspicious attempt-start rate
- answer-key leakage check status

> Time-spent distribution and response-change-frequency are computed from the append-only `ResponseEvent` log (§3.6); they are not reconstructable from the latest-answer `Response` alone, which is why the event log is a model requirement, not just an analytics feature.

Analytics may be materialized asynchronously. Raw attempt/audit records remain the source of truth.

### 9.4 Export Delivery and Authorization

Gradebook/report exports (CSV/PDF) contain other students' scores and identities — PII. They are therefore not served by guessable or long-lived links:

- Exports are stored in app-controlled storage and served **auth-gated and org/role-scoped**, or via short-lived signed URLs; access re-checks the requester's authorization at download time.
- Every download writes an audit record (who, what, when).
- The "export ready" notification links to the gated download, never to a raw artifact URL; export URLs are scrubbed from logs (§12).
- Exports expire and are deleted on a configured schedule (§13).

---

## 10. Support Console

Required support tools:

- Organization lookup.
- User lookup.
- Quiz lookup.
- Attempt timeline.
- Finalization status viewer.
- Attempt audit viewer.
- Eligibility/access debugger.
- ResultRelease viewer.
- DailyChallenge calendar.
- Streak recomputation tool.
- Expiry sweep status.
- Celery job failure viewer.
- Export run history.
- AI generation run history.
- Answer-key leakage checker.
- Admin/support action audit trail.

Support actions:

- Void attempt with reason.
- Recompute streak cache.
- Re-run analytics aggregation.
- Re-send export-ready notification.
- Release/withhold manual results when authorized.

All support actions require staff permission and create AdminActionLog entries.

---

## 11. Background Jobs: Celery + Redis

Celery is required because the finished product includes more than a simple expiry sweep.

Required jobs:

```text
finalize_expired_attempts
send_due_soon_notifications
send_daily_challenge_notifications
send_streak_at_risk_notifications
generate_gradebook_export
generate_pdf_report
aggregate_item_analytics
aggregate_daily_challenge_analytics
recompute_streak_cache
generate_ai_question_drafts
process_outbox_events
cleanup_expired_exports
```

Rules:

- PostgreSQL is the durable source of truth.
- Redis is broker/cache only; it must not hold unrecoverable business data.
- Jobs must be idempotent.
- Use row locks/source-state filters for finalization sweeps.
- Long exports run async and surface progress/status in the web UI.
- Failed jobs are visible in support console.

---

## 12. Security and Integrity Requirements

- Server owns timing, scoring, feedback, attempt limits, eligibility, release dates, and streak credit.
- Answer keys never reach the client during in-progress attempts.
- Correctness can be returned only after immediate-feedback response is locked.
- CSRF required on all mutating views and HTMX requests.
- Rate limit attempt creation, answer saves, login, result export, and AI generation requests.
- Students cannot see other students' results.
- Instructors cannot see other organizations' content unless explicitly shared by policy.
- Manual-release states are enforced server-side.
- Admin/support actions are audited.
- Published attempts are never recomputed from mutable drafts.
- Sensitive logs are scrubbed: no secrets, cookies, CSRF tokens, raw prompt secrets, answer keys in unsafe logs, or export URLs.
- Production settings: `DEBUG=False`, env secrets, HTTPS, secure cookies, HSTS, real `ALLOWED_HOSTS`, Sentry scrubbing.

---

## 13. Privacy, Retention, and Data Governance

Required policies:

- Attempt/audit retention policy.
- Export artifact retention policy.
- AI generation draft retention policy.
- Student account deactivation behavior.
- Student data export behavior.
- Organization offboarding procedure.
- Admin/support access logging.
- Deletion/anonymization rules where compatible with assessment audit requirements.
- License/provenance retention for published content.

Default posture:

- Attempts and audits are retained for compliance unless organization policy explicitly archives/deletes.
- Generated exports expire and are deleted after a configured period.
- AI drafts rejected by instructors may be deleted or retained according to organization policy.

**Right-to-erasure vs. immutable assessment audit.** A data-subject erasure request does **not** hard-delete `AttemptAudit`, `AttemptFinalization`, `ResponseEvent`, or `DailyParticipation` — those are immutable assessment facts and (for grades/certificates) often legally retained. Erasure instead **tombstones/anonymizes the subject**: identifying fields (name, email, IP/user-agent on events) are severed or hashed and the user is detached, while the assessment and audit records survive as anonymized facts referencing an opaque subject id. Export, retention-based deletion, and erasure are distinct, individually documented paths; erasure ≠ ordinary retention expiry. (ADR-0023.)

---

## 14. Performance and Reliability Targets

Target behavior for normal production-sized quizzes:

```text
Quiz start p95: < 400ms
Answer save p95: < 200ms
Attempt submit/finalize p95: < 500ms for normal quiz sizes
Quiz authoring page p95: < 400ms
Question bank search p95: < 400ms
Gradebook list p95: < 500ms with pagination
Daily challenge page p95: < 300ms
Result page p95: < 400ms
HTMX saved-state fragment p95: < 150ms
Expiry sweep lag: < 2 minutes
CSV export: async for large gradebooks
AI generation: async only, never blocking request/response
```

Reliability requirements:

- Finalization must be safe to retry.
- Expiry sweep must be safe to run concurrently.
- Export generation must be resumable or safely retryable.
- Outbox processing must be idempotent.
- Streak cache must be recomputable from DailyParticipation.

---

## 15. Production Scaffolding

Required infrastructure:

- PostgreSQL for dev and production. No SQLite.
- Redis for Celery broker/cache.
- Celery worker.
- Celery Beat.
- Dockerfile with slim base, non-root user, Gunicorn, static collection.
- docker-compose for web, postgres, redis, worker, beat.
- WhiteNoise or host-native static serving.
- Sentry for errors.
- Structured logging with request id, user id, organization id, quiz id, attempt id where relevant.
- `/healthz` for process/database health.
- `/readyz` for database, Redis, and worker expectations.
- Env-driven settings and `.env.example`.
- Automated backups.
- Restore drill documentation.
- Migration and rollback procedure.
- Production runbook.

---

## 16. Testing Strategy

Testing is a product requirement.

### 16.1 Domain and Service Tests

- Scoring for all question types.
- Multi-select exact-set behavior.
- Attempt limit enforcement by scope.
- Assignment/enrollment eligibility.
- Availability windows.
- QuizVersion immutability.
- Response references immutable VersionQuestion/VersionChoice.
- Randomized assembly chooses from approved bank items only.
- AI-generated drafts cannot publish without review.
- Provenance/licensing checks before publish.

### 16.2 Timing and Finalization Tests

- Deadline calculation.
- Late answer rejection.
- Client clock manipulation has no effect.
- Auto-finalization on next request.
- Sweep finalization.
- Double submit.
- Submit at expiry.
- Sweep vs submit race.
- Repeated finalization creates one score and one audit.

### 16.3 Daily Challenge Tests

- Pre-release start rejected.
- One live attempt per student per DailyChallenge.
- Archived practice attempts separate from live attempts.
- Streak increments across consecutive days.
- Missing a day breaks streak.
- Start-before-close/finalize-after-close earns no credit.
- Archived practice earns no credit.
- Streak cache recomputation matches DailyParticipation.
- Timezone boundary tests.

### 16.4 Authorization Tests

- Student cannot access authoring.
- Student cannot access other students' results.
- Instructor cannot access another organization's private quiz.
- Organization Admin can see organization-wide content.
- Platform Admin support actions require staff and are audited.
- Direct URL attempts fail safely.

### 16.5 HTMX and Web Tests

- Incremental answer save.
- Immediate feedback locks response.
- Manual release withheld state.
- CSRF required.
- Answer keys absent from rendered HTML and HTMX fragments.
- Expired attempt fragment behavior.
- Focus and accessible status fragments.

### 16.6 Background Job Tests

- Expiry sweep idempotency.
- Outbox processing idempotency.
- Export generation status.
- Analytics aggregation correctness.
- Streak recomputation.
- AI draft generation job captures review-required state.

### 16.7 v3 Assessment-Depth Tests

- Sampled form: each attempt gets an equivalent form, snapshotted and stable on reload; two students rarely get identical sets; audit reproduces the sampled set and seed.
- Timing accommodation scales the deadline and is recorded on the attempt/audit.
- Structural key isolation: the in-progress student read model/queryset cannot surface the correctness column (assert at the query layer, not only the template).
- `ResponseEvent` backs time-spent and change-frequency; latest `Response` matches the last event.
- Optimistic concurrency: a stale-revision save from a second tab returns conflict, not overwrite.
- Concurrency-safe attempt creation: two simultaneous starts cannot exceed the attempt limit.
- DB unique constraint guarantees one finalization even if the lock is bypassed in a test.
- Frozen response set: a save attempted past deadline+grace is rejected; all three finalization paths score the same set.
- Grace window honored at the boundary; beyond it rejected.
- Per-org timezone: daily challenge release and streak windows compute in the org timezone across a DST boundary and across two orgs in different zones.
- Window precedence: timer clamps to availability close; late-but-before-close attempts are flagged, not blocked (unless disallowed).
- Export download is auth-gated/scoped/expiring and audited; a foreign-org user cannot fetch an export.
- Right-to-erasure anonymizes the subject while audit/finalization/participation rows survive.
- AI cost budget exhaustion is a clean, surfaced state.

### 16.8 CI Gates

- ruff format/lint.
- type checks for domain/services where practical.
- pytest against PostgreSQL.
- migration check.
- dependency vulnerability scan.
- Django deploy check.
- Docker build check.

---

## 17. Definition of Done

1. Organizations, courses, cohorts, enrollments, and roles enforce server-side access boundaries.
2. Instructors can create question banks, tag content, track provenance, and review AI-generated drafts before use.
3. Instructors can build quizzes manually or through blueprint-based randomized assembly.
4. Publishing creates immutable QuizVersion, VersionQuestion, and VersionChoice records.
5. Historical attempts never change when draft quizzes or bank items are edited.
6. Students can start only eligible attempts through assignment/open/daily scopes; attempt creation is concurrency-safe (no double-start exceeds the limit).
7. Attempts save answers incrementally, append a `ResponseEvent` per save, and visibly report saved/unsaved/conflict/expired states; a second tab/device produces a clean conflict, not a silent overwrite.
8. Timed attempts use server deadlines (with per-student accommodations applied and an explicit grace window); client clock manipulation cannot extend time.
9. Expired attempts finalize through client submit, next-request finalization, or Celery sweep, all scoring the identical response set frozen at the deadline.
10. Finalization is transactional, idempotent (one finalization enforced by a DB unique constraint), concurrency-safe, and creates one score/audit; the streak cache is updated post-commit, never inside the finalization transaction.
11. All scoring is server-side; answer keys cannot reach in-progress clients because the student read model excludes the correctness column by construction.
12. Immediate feedback locks the response before revealing correctness.
13. Manual-release results remain hidden until ResultRelease is opened.
14. Sampled-form assignments generate a per-attempt equivalent form from a pool, snapshotted and reproducible; fixed-form assignments remain available for low-stakes use.
15. Timing accommodations (extended time) are configurable per student and applied at attempt start, recorded in the audit.
16. Daily Coding Challenge releases exactly one challenge per calendar date in the organization's timezone and reuses the normal quiz engine.
17. Streaks are computed from immutable participation records in the org timezone (DST-aware); archived practice never earns streak credit.
18. Tenant isolation holds for every org-scoped surface on web and HTMX endpoints (queryset-level, deny-by-default), proven by a systematic isolation test.
19. Report exports are served auth-gated, scoped, expiring, and download-audited; an export notification never exposes a raw artifact URL.
20. A right-to-erasure request anonymizes the subject while preserving immutable assessment audit.
21. Gradebook includes completion, scores, distribution, item difficulty, distractor counts, exports, and audit drill-down.
22. Advanced analytics (time-spent, change-frequency from `ResponseEvent`) and result integrity dashboard exist.
23. Notifications exist for assignment, due-soon, daily challenge, result release, streak risk, export ready, and AI completion.
24. Support console can inspect attempts, access decisions, finalization, challenge schedule, streaks, exports, and job failures.
25. Celery/Redis process expiry sweeps, notifications, exports, analytics, outbox, streak recomputation, and AI generation; AI generation is bounded by a per-org cost budget.
26. Content source/provenance rules prevent unapproved copied or unreviewed AI content from publishing; human review covers content safety, not only correctness.
27. Security posture is verifiable: CSRF, rate limits, secure cookies, HTTPS, env secrets, Sentry scrubbing, no answer-key leakage.
28. Privacy and retention rules are documented and implemented for attempts, audits, exports, AI drafts, and deactivated users.
29. Performance targets are measured or tested where practical.
30. `docker-compose up` runs web, Postgres, Redis, worker, and beat locally.
31. CI runs lint, tests, migration checks, deploy checks, vulnerability scan, and Docker build.
32. Production deployment has backups, restore procedure, rollback notes, health/readiness checks, and runbook.
33. The UI is polished, accessible, mobile-friendly, and does not feel like a tutorial page.

---

## 18. Required Build Sequence

This is build order, not a reduced scope.

### Phase 0 — Foundation

- Django project and settings.
- PostgreSQL + Redis + Celery + Celery Beat via docker-compose.
- Custom user model and roles.
- Organization/course/cohort/enrollment models, **per-organization timezone**, and the `TimingAccommodation` model.
- Authorization policies and **tenant-scoped querysets/managers (deny-by-default)**.
- Base templates and UI shell.
- Audit/event/outbox substrate.
- Tests for tenancy and authorization, including the systematic cross-org isolation test (§3.1a).

### Phase 1 — Question Bank and Authoring Core

- QuestionBank, QuestionBankItem, tags, difficulty, learning objectives.
- Provenance/licensing model.
- Instructor authoring screens.
- Quiz draft builder.
- Publish validation.
- QuizVersion snapshot generation.
- `ItemPool` model and pool-size validation (for sampled-form mode).
- Tests for immutability and content validation.

### Phase 2 — Assessment Engine

- Assignment/access checks; window precedence (availability / timer / due).
- Concurrency-safe attempt creation (unique constraint) and accommodation-applied `deadline_at`.
- Persisted shuffle order.
- Per-attempt sampled-form generation and `AttemptItemSet` snapshot (sampled-form mode).
- Structural answer-key isolation (correctness-free student read model).
- Incremental response saves with `ResponseEvent` log and optimistic-concurrency conflict handling.
- Server-side scoring and scoring-policy surface (pass mark / grade bands).
- Feedback policies.
- Transactional finalization with DB one-finalization constraint, frozen-response set, and grace window.
- AttemptAudit.
- Timing and expiry sweep.
- Concurrency tests (double-start, double-submit, sweep-vs-submit, multi-tab conflict).

### Phase 3 — Daily Coding Challenge

- DailyChallenge scheduling.
- Server-authoritative release gate **in org timezone**.
- Live attempt scope.
- Archived practice mode.
- DailyParticipation (in finalization transaction); streak cache updated post-commit.
- Streak computation and cache.
- Timezone (per-org, DST) and streak tests.

### Phase 4 — Content Operations

- AI draft generation workflow.
- Human review and approval (correctness + content safety).
- Per-organization AI cost/usage budget and provider abstraction.
- AssessmentBlueprint.
- Randomized assembly.
- Clone/archive/compare versions.
- Publish checklist and quality warnings.

### Phase 5 — Reporting and Notifications

- Gradebook.
- CSV/PDF exports, served auth-gated/scoped/expiring with download audit.
- Item analytics (incl. time-spent and change-frequency from `ResponseEvent`).
- Distractor analysis.
- Result integrity dashboard.
- In-app/email notifications.
- Async export jobs.
- Analytics aggregation jobs.

### Phase 6 — Support and Operations

- Support console.
- Eligibility debugger.
- Attempt timeline.
- Streak recomputation.
- Job failure views.
- AdminActionLog coverage.
- Privacy/retention controls, including the right-to-erasure (tombstone/anonymize) path.

### Phase 7 — Production Hardening

- Sentry and structured logs.
- Health/readiness endpoints.
- Security headers and production settings.
- Rate limiting.
- CI gates.
- Docker production profile.
- Backup/restore docs.
- Deployment runbook.
- Accessibility and mobile polish.

---

## 19. ADRs to Write

- ADR-0001: Assessment integrity is server-authoritative.
- ADR-0002: Published quiz content is immutable and versioned.
- ADR-0003: Attempts reference QuizVersion/VersionQuestion/VersionChoice only.
- ADR-0004: Finalization is transactional, idempotent, and row-locked.
- ADR-0005: HTMX countdown is display-only; server deadline is truth.
- ADR-0006: Daily Coding Challenge is delivery mode, not a second grading engine.
- ADR-0007: No code execution or sandboxing in this product.
- ADR-0008: External platform content is not copied without license or permission.
- ADR-0009: AI-generated questions require human approval before publishing.
- ADR-0010: Question Bank is the reusable content source; QuizVersion is the assessment truth.
- ADR-0011: Audit records are immutable; outbox handles side effects.
- ADR-0012: Celery + Redis are required for expiry, notifications, exports, analytics, and AI draft work.
- ADR-0013: No public API; HTMX/internal endpoints serve the web product only.
- ADR-0014: Streak cache is recomputable and not authoritative.
- ADR-0015: Organization-scoped tenancy governs content, enrollment, and reporting.
- ADR-0016: Assignments support fixed-form and sampled-form modes; sampled forms are per-attempt, snapshotted, and reproducible, to defeat answer sharing without breaking immutability.
- ADR-0017: Answer-key isolation is structural — the in-progress student read model excludes the correctness column; the leakage checker is a backstop.
- ADR-0018: Timing accommodations (extended-time multipliers) are first-class and scale the server deadline at attempt start (accessibility/legal requirement).
- ADR-0019: Saves are event-sourced via an append-only `ResponseEvent` log; the current answer is a derived view. Backs analytics and optimistic concurrency.
- ADR-0020: Daily-challenge release and streak math compute in the organization's timezone (DST-aware), not a single global app timezone.
- ADR-0021: Report exports contain PII and are served auth-gated, scoped, expiring, and download-audited.
- ADR-0022: Tenant isolation is enforced at the queryset layer (deny-by-default) with a systematic cross-org isolation test, not per-view diligence.
- ADR-0023: Right-to-erasure tombstones/anonymizes the subject while preserving immutable assessment audit; erasure, retention expiry, and export are distinct paths.

---

## 20. Risks and Mitigations

### Product Risks

- Scope creep into online judge/code execution.
- Content-source legal risk from copying external platform questions.
- AI-generated questions may be wrong or low quality.
- Instructors may need faster authoring than rigorous governance suggests.
- Analytics can become noisy if not tied to clear instructor decisions.

### Technical Risks

- Race conditions in finalization.
- Answer-key leakage through templates or HTMX fragments.
- **Answer sharing / item harvesting across students when a single fixed form is reused for a whole cohort.**
- Timezone bugs in daily challenge/streak windows.
- Celery jobs duplicating work under retry.
- Large gradebook queries becoming slow.
- Instructor authorization leaking cross-organization data.

### Mitigations

- Keep no-code-execution boundary explicit.
- Offer sampled-form mode (per-attempt forms from a pool) for higher-stakes assessments to defeat answer sharing; reserve fixed-form for low-stakes/practice.
- Make answer-key isolation structural (correctness-free student read model), with the leakage checker as a backstop.
- Require source/provenance metadata and human approval.
- Test finalization with row locks and concurrent paths.
- Treat server time as truth everywhere.
- Use selectors/querysets for read-heavy pages.
- Use async exports and analytics materialization.
- Write direct leakage tests for rendered HTML and HTMX responses.
- Build eligibility debugger and support console early enough to validate operations.
