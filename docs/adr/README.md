# Architecture Decision Records

This build implements the decisions below in code or scaffolding. Each entry maps back to the product scope in `Online Quiz Portal.md`.

| ADR | Decision | Implementation |
|---|---|---|
| ADR-0001 | Assessment integrity is server-authoritative. | `assessments.services` owns eligibility, timing, saving, scoring, and finalization. |
| ADR-0002 | Published quiz content is immutable and versioned. | `QuizVersion`, `VersionQuestion`, and `VersionChoice` are publish snapshots. |
| ADR-0003 | Attempts reference immutable assessment content. | Attempts use `QuizVersion` for fixed forms and `AttemptItemSnapshot` for all rendered attempts. |
| ADR-0004 | Finalization is transactional and idempotent. | `finalize_attempt()` locks the attempt and `AttemptFinalization` is one-to-one. |
| ADR-0005 | The countdown is display-only. | Deadlines are checked in `save_response()` and expiry finalization. |
| ADR-0006 | Daily challenge is a delivery mode. | `DailyChallenge` points to `QuizVersion`; normal attempt machinery remains the engine. |
| ADR-0007 | No code execution. | Coding challenges are represented as quiz versions only. |
| ADR-0008 | External content requires provenance. | `QuestionBankItem` stores source, license, reuse, and review fields. |
| ADR-0009 | AI-generated questions require review. | Review/provenance fields and cost budget are modeled; provider integration remains a backend extension. |
| ADR-0010 | Question bank is reusable source; version is assessment truth. | Bank items can seed drafts; published versions are copied snapshots. |
| ADR-0011 | Audit records are immutable. | `AttemptAudit` and `ResponseEvent` append records rather than mutating history. |
| ADR-0012 | Celery + Redis handle background work. | Celery app and expiry sweep task are configured. |
| ADR-0013 | No public API. | The product exposes Django pages and internal HTMX endpoints only. |
| ADR-0014 | Streak cache is recomputable. | `DailyParticipation` is authoritative; `recompute_streak()` rebuilds `StreakCache`. |
| ADR-0015 | Tenancy governs content and reporting. | Organization-scoped models expose a tenant-aware `for_user()` queryset. |
| ADR-0016 | Fixed and sampled assignment forms are supported. | `QuizAssignment.form_mode` snapshots either a version or sampled pool items onto attempts. |
| ADR-0017 | Answer-key isolation is structural. | `student_read_model()` returns dataclasses without correctness attributes. |
| ADR-0018 | Timing accommodations are first-class. | `TimingAccommodation` scales attempt deadlines and records applied values. |
| ADR-0019 | Saves are event-sourced. | `ResponseEvent` records revisions and selected choice ids for every save. |
| ADR-0020 | Daily challenge time is per organization. | Daily release lookup uses `Organization.timezone`. |
| ADR-0021 | Exports are sensitive. | `ExportArtifact` models scoped, expiring artifacts and download audit fields. |
| ADR-0022 | Tenant isolation is deny-by-default. | Querysets require `.for_user(user)` at web boundaries. |
| ADR-0023 | Erasure preserves audit facts. | The immutable audit model is present; anonymization workflow is a next operations slice. |
