# Security Posture and Control Mapping

Scope: the QuizOps application and its container configuration. Infrastructure
below the container (host, network, cloud IAM) is the operator's responsibility
and is out of scope here.

---

## 1. Exposed surfaces

| Surface | Auth | Protection |
|---|---|---|
| `/accounts/login/` | none (entry point) | Pre-authentication throttle by identity, session rotation on success |
| `/accounts/password_reset/` | none | Pre-auth throttle by client identity; single-use time-limited token (Django); no account enumeration in the response |
| `/legal/*`, `/healthz/`, `/readyz/` | none | Rate limited; `/readyz/` exposes component status only, no versions or paths |
| Student pages | session | `login_required`, per-object ownership checks, tenant-scoped querysets |
| Instructor and management pages | session + role | Active organization resolved first, then authorized against **that** organization |
| `/admin/` | session + `is_staff` + MFA when required | Login shares the `/accounts/login/` throttle budget; staff/superusers complete TOTP when `MFA_REQUIRED`; assessment evidence read-only; FK pickers tenant-scoped |

There is no public API, no third-party JavaScript, and no outbound call to any
external service at runtime.

## 2. Control mapping

| Control | Implementation | Evidence |
|---|---|---|
| Password storage | Django PBKDF2, per-user salt | `settings.AUTH_PASSWORD_VALIDATORS`; weak hasher reachable **only** under a test runner (`RUNNING_TESTS`) |
| Password policy | 12-character minimum, similarity, common-password and numeric checks | `config/settings.py` |
| Brute-force protection | Throttle budget consumed **before** authentication on `/accounts/login/` and `/admin/login/` (fail-open if cache down) | `accounts/views.py::ThrottledLoginView.post`, `accounts/admin_login.py`; tests in `tests.py` / `tests_r4_fixes.py` |
| MFA (privileged) | TOTP required for org admins, platform operators, and Django staff/superusers when `MFA_REQUIRED` | `accounts/mfa.py`, `accounts/middleware.py`; off under DEBUG/tests |
| Brute-force (reset) | Password reset + confirm consume throttle budget before processing | `accounts/views.py::ThrottledPasswordResetView` |
| Session management | 8-hour cap, expire on browser close, sliding, HttpOnly, SameSite=Lax, Secure in production, id rotated on login | `config/settings.py` |
| Transport | HSTS 1 year with subdomains and preload, SSL redirect, secure cookies — all when `DEBUG=0` | `manage.py check --deploy` reports zero issues |
| CSRF | Django middleware on every state change | `tests.py::ErrorSurfaceTests.test_csrf_is_enforced_on_state_change` |
| XSS | Django autoescaping plus a strict CSP (`default-src 'self'`, no inline script) | `assessments/middleware.py::ContentSecurityPolicyMiddleware` |
| Clickjacking | `X-Frame-Options: DENY`, `frame-ancestors 'none'` | settings + CSP |
| Tenant isolation | Deny-by-default `for_user()`; cross-tenant access requires the explicit `is_platform_operator` grant | `tests.py::TenancyTests`, `tests_admin.py::CrossTenantFeatureTests` |
| Authorization | Server-side on every protected route; no client-side gating | `tests_views.py::AuthenticationGateTests` covers every route |
| Privilege escalation | Admin rights cannot be granted by roster import | `tests_admin.py::RosterHardeningTests` |
| Rate limiting | Login, password reset, MFA, attempt start, answer save, export, roster import, health probes | `assessments/ratelimit.py` |
| Audit immutability | Attempt/audit/integrity/finalization records read-only in admin; corrections go through the audited `void_attempt` | `assessments/admin.py::ReadOnlyAdmin` |
| Secret handling | No secrets in source; `.env` git- and docker-ignored; startup fails without `SECRET_KEY` when `DEBUG=0` | `config/settings.py`, `.gitignore`, `.dockerignore` |
| Log hygiene | Passwords, tokens, cookies, answer keys and sampling seeds redacted | `config/logging_filters.py::scrub` |
| Dependency risk | Pinned versions, `pip-audit` in CI, Dependabot weekly | `.github/workflows/ci.yml`, `.github/dependabot.yml` |
| Spreadsheet injection | Formula-leading characters neutralised in CSV exports | `assessments/exports.py::_csv_safe` |
| Data rights | Self-service export and erasure; erasure tombstones the subject | `accounts/privacy.py`, `docs/PRIVACY.md` |

## 3. Assessment-integrity controls

These are security controls specific to this product:

- Answer keys never enter the student object graph during an attempt — the read
  model is a projection without the correctness column.
- Correctness is disclosed only for a question already locked under the
  immediate-feedback policy.
- Deadlines, attempt limits, eligibility, scoring and result release are all
  decided server-side; the browser countdown is display-only.
- The answer key stays hidden while a retake of the same fixed form remains
  available, so results cannot be used as a study aid for the next attempt.
- Published versions and attempt snapshots are immutable; the draft can change
  without altering any paper a student has already been served.
- Every rejection (late save, revision conflict, locked write) writes an
  `IntegrityEvent` **after** the failed transaction unwinds, so the record
  survives the rollback that discards the attempted write.

## 4. Security review status

**Internal review: performed.** Nine structured passes across this codebase —
four general audits, one per feature round, a dedicated review of newly added
features, and this framework pass. Findings and their fixes are recorded in
`docs/AUDIT.md`. The regression suite carries a test for every security finding,
so a reintroduction fails CI rather than reaching production.

**Security review evidence.** An independent in-repo review is recorded in
`docs/SECURITY-REVIEW-INDEPENDENT.md` (Critical/High remediations applied). A
third-party firm penetration test has not been commissioned; that is an
**accepted buyer gate** when a customer contract requires an external report
(see §5 and `docs/PENTEST-ENGAGEMENT.md`). Priority paths for an external
engagement: privilege escalation via Django admin as non-operator staff,
cross-tenant IDOR, auth under Redis fail-open, answer-key/retake abuse,
Redis/Celery trust boundary.

**Automated scanning:** `pip-audit` runs on every push and fails the build on a
known-vulnerable dependency. There is no SAST/DAST tooling beyond `ruff` and the
test suite.

## 5. Known limitations

These are deliberate boundaries, documented so nobody mistakes them for oversights:

- **No SSO/SAML/SCIM.** Authentication is username and password plus optional
  TOTP. Institutions requiring IdP integration cannot use this as-is.
- **MFA is TOTP-only for privileged roles.** When `MFA_REQUIRED=1` (the default
  with `DEBUG=0`), organization admins, platform operators, and Django
  staff/superusers must enroll and complete an authenticator-app challenge
  after password login (so `/admin/` is not password-only). Students and
  instructors without those flags are not challenged. There is no
  WebAuthn/hardware-key path yet. Setup cannot replace an enrolled factor
  without a current TOTP code; rotating the secret invalidates other sessions.
  TOTP verify/setup and password-reset are rate-limited. **Break-glass:** Django
  admin action “Reset MFA enrollment” clears the factor, writes a Django admin
  `LogEntry`, and `AdminActionLog` (`user.mfa_reset`) per membership; there are
  no recovery codes.
- **Rate limits and job locks fail open on cache outage.** If Redis is
  unreachable, `assessments.ratelimit` and `single_flight` log a warning and
  allow the request/job so an exam is not taken offline by a cache blip.
  Operators must alert on those warnings; do not treat fail-open as "no
  throttle needed".
- **No in-product proctoring.** No webcam, lockdown browser, or screen
  monitoring. Integrity relies on server-authoritative timing, per-attempt
  sampled forms, and the integrity event log.
- **No field-level encryption at rest.** Protection relies on database and
  volume encryption supplied by the operator. `TimingAccommodation.reason` may
  contain health information and is stored in plaintext in the database
  (cleared on erasure).
- **Single region.** No multi-region replication or data-residency controls.
- **English only.** No localisation.
- **Roster CSV malware scanning is out of scope.** Org-admin roster uploads are
  size-, type-, and UTF-8-validated server-side and never written under a
  user-supplied path. Antivirus/malware scanning of CSV content is an operator
  host/network control, not an application feature.
- **External third-party penetration test** is recommended before onboarding a
  customer with a contractual security-review requirement. An independent
  in-repo security review (`docs/SECURITY-REVIEW-INDEPENDENT.md`) plus the
  vendor engagement brief (`docs/PENTEST-ENGAGEMENT.md`) is the current
  security-review evidence; commissioning an external firm remains the buyer
  gate for that class of deal.

## 6. Reporting a vulnerability

Email the maintainers with a description, affected version, and reproduction
steps. Please do not open a public issue for an unpatched vulnerability. There
is no bug bounty.
