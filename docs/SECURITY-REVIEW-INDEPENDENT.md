# Independent security review — 2026-07-23 (remediated)

**Scope:** Live QuizOps codebase.  
**Method:** Structured review of auth, tenancy, integrity, config, admin, and
Celery surfaces per `audit_framework_saas.md`. Complements (does not replace)
an external firm engagement when a buyer requires one — see
`docs/PENTEST-ENGAGEMENT.md`.

## Verdict

Prior High (Django admin identity plane) and Medium (unscoped student FKs,
passwordless Redis host, global FeatureFlag/JobFailure admin) are **remediated**
in this tree. Residual risk is operational (commission external pen test for
enterprise buyers; host AV for roster CSV).

## Findings (current)

| Severity | Location | Finding | Status |
|---|---|---|---|
| — | `accounts/admin.py` | User / Organization / OrganizationMembership mutate gated to platform operators / superusers | Fixed |
| — | `assessments/admin.py` Enrollment / TimingAccommodation | `membership_user_fields=("student",)` scopes User pickers | Fixed |
| — | `assessments/admin.py` FeatureFlag / JobFailure | Platform-operator gated | Fixed |
| — | `config/settings.py` | `REDIS_PASSWORD` required with `REDIS_HOST` when `DEBUG=0` | Fixed |
| Info | External third-party pen test | Not commissioned; engagement brief ready | Accepted buyer gate / docs |

## Pen-tester priority paths (for external engagement)

1. Attempt to escalate via Django admin as non-operator `is_staff` (should fail)  
2. Cross-tenant IDOR on product routes  
3. Auth under Redis fail-open  
4. Answer-key / retake abuse  
5. Redis/Celery trust boundary
