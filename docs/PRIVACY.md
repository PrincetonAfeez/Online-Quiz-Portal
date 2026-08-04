# Privacy, Retention, and Erasure

The user-facing notice lives at `/legal/privacy/`. This document is the
engineering contract behind it.

## Personal data inventory

| Data | Where | Why | Retention |
|---|---|---|---|
| Username, email, name | `accounts_user` | Identity, sign-in, gradebook display | Life of account |
| Organization membership, role | `accounts_organizationmembership` | Authorization, tenancy | Life of account |
| Course/cohort enrolment | `assessments_enrollment` | Assignment eligibility | Per institution policy |
| Timing accommodation + reason | `assessments_timingaccommodation` | Legal accessibility obligation (ADA/504) | Per institution policy |
| Attempts, responses, scores | `assessments_attempt`, `_response`, `_attemptfinalization` | The product's core record | Per institution policy |
| IP address, user-agent | `assessments_responseevent` | Assessment-integrity evidence | 24 months, then cleared |
| Notification bodies | `assessments_notification` | Reminders | Deleted on erasure |

`TimingAccommodation.reason` may contain health information. It is visible only
to instructors and org admins of the owning organization, is never rendered to
students, and is excluded from exports shared with anyone but the subject.

## Export

`accounts.privacy.export_user_data(user)` returns the whole inventory as JSON.
Reachable two ways:

- Self-service: `/accounts/me/` → *Download my data* (rate limited, 3/hour).
- Operator: `python manage.py export_user_data <username> --output file.json`.

## Erasure

`accounts.privacy.anonymize_user(user)` tombstones the subject:

1. Username replaced with `erased-<tombstone>`, email/first/last cleared,
   password made unusable, account deactivated, all privilege flags and MFA
   secrets cleared.
2. `ResponseEvent.ip_address` and `user_agent` cleared for every attempt.
3. `TimingAccommodation.reason` cleared (may contain health-adjacent text).
4. Notifications deleted.
5. Memberships deactivated, each with an `AdminActionLog` entry.

**Deliberately retained**: `Attempt`, `AttemptFinalization`, `AttemptAudit`,
`ResponseEvent` rows, and `DailyParticipation`. These are immutable assessment
facts, usually subject to institutional retention obligations. They remain
foreign-keyed to the anonymized user row (`erased-<hash>`); the returned
tombstone string is the opaque identifier written to `AdminActionLog.target_id`.
There is no separate subject table.

Self-service erasure requires password re-authentication and refuses to run
while an attempt is in progress, so a student cannot erase themselves mid-exam
to discard a bad result.

Erasure is **not** the same path as retention expiry or export. Each is
separately implemented and separately documented.

## Sub-processors

None. QuizOps makes no outbound third-party calls at runtime. Email delivery
uses the SMTP provider the operating institution configures; that provider is
the only sub-processor, and it is chosen by the customer.


## Audit log immutability and retention

**Immutability (application path).** Assessment facts are append-only in the
product UI and admin:

- `AttemptAudit`, `AttemptFinalization`, `ResponseEvent`, `IntegrityEvent` and
  `AdminActionLog` are registered with `ReadOnlyAdmin` (no add/change/delete UI).
- Corrections happen forward, never in place: `services.void_attempt()` marks an
  attempt void and writes *new* audit and admin-action rows.

**Retention exceptions (automated).** These are deliberate TTL paths, not
ad-hoc edits:

- `ResponseEvent.ip_address` / `user_agent` are cleared after 24 months (row kept).
- `IntegrityEvent` rows older than 24 months are deleted.
- `JobFailure` rows older than 90 days are deleted.
- `AdminActionLog` is not auto-purged (operator-managed, typically 7 years).

This is enforcement by construction and convention, not by database privilege.
An operator with direct database access can still alter rows; that is a
deployment control (restrict production database credentials), not something the
application can prevent.

**Retention.**

| Record | Retention | Rationale |
|---|---|---|
| `AttemptAudit`, `AttemptFinalization` | Life of the assessment record, per institution policy | Grade provenance; usually a statutory obligation |
| `ResponseEvent` | Full row for the life of the attempt; IP and user-agent cleared at 24 months or on erasure | Revision history stays verifiable without retaining network identifiers indefinitely |
| `IntegrityEvent` | 24 months | Long enough for an academic-misconduct process, short enough to bound growth |
| `AdminActionLog` | 7 years | Administrative accountability (operator-managed; not auto-purged) |
| `JobFailure` | 90 days | Operational, not a business record |

**Automation.** Celery Beat runs `assessments.tasks.apply_retention_policies`
daily: clears `ResponseEvent` IP/UA past 24 months, deletes `IntegrityEvent`
rows past 24 months, and deletes `JobFailure` rows past 90 days.
`AdminActionLog` retention is left to the operating institution.

Erasure does **not** delete assessment facts; it severs identifiers, as
described above. Retention expiry, export, and erasure are three distinct paths.

## Data residency

Single region: the application runs wherever the operating institution deploys
it, and there is no cross-region replication. QuizOps makes no outbound call to
any third-party service at runtime, so no personal data leaves the deployment
except transactional email, which goes to the SMTP provider the institution
configures. There is no data-residency control inside the product — residency is
determined entirely by where the operator runs it.
