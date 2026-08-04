"""
Django settings for config project.

Environment-driven. Every production-sensitive default fails closed: DEBUG is
off unless asked for, and a missing SECRET_KEY is a startup error rather than a
silent fallback to a published constant (finding 7).
"""

import os
import sys
from pathlib import Path
from urllib.parse import quote

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def load_env_file(path: Path) -> None:
    """Load a local .env for development.

    Real environment variables always win, so an orchestrator's configuration is
    never shadowed by a file that happened to be copied into the image.
    """
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "1" if default else "0")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


# Secure default: an unset DEBUG means production, not a debug server.
DEBUG = env_bool("DEBUG", False)

SECRET_KEY = os.environ.get("SECRET_KEY", "")
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = "dev-only-change-me"
    else:
        raise ImproperlyConfigured("SECRET_KEY must be set when DEBUG is off.")

ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", "127.0.0.1,localhost")
if not DEBUG and not ALLOWED_HOSTS:
    raise ImproperlyConfigured("ALLOWED_HOSTS must be set when DEBUG is off.")


# Application definition

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "accounts.apps.AccountsConfig",
    "assessments",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Serves collected static files under gunicorn, where Django itself will not
    # (finding 25).
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "assessments.middleware.RequestIDMiddleware",
    "assessments.middleware.ContentSecurityPolicyMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "accounts.middleware.MFAEnforcementMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "assessments.middleware.OrganizationTimezoneMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "assessments.context_processors.navigation",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


# Deliberately narrow: only when a test runner is the entry point. Production
# never takes this branch, and a weak hasher must never be reachable by config.
RUNNING_TESTS = "test" in sys.argv[:2] or "PYTEST_CURRENT_TEST" in os.environ or bool(
    os.environ.get("PYTEST_VERSION")
)

# Database. Postgres is the supported engine (spec 15); SQLite remains available
# for local runs only, and is refused outright in production. Tests always use
# in-memory SQLite so a staging .env DATABASE_URL cannot hijack the suite.
if RUNNING_TESTS:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:",
        }
    }
    # PBKDF2 dominates suite runtime; the suite creates hundreds of users.
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
else:
    DATABASES = {
        "default": dj_database_url.config(
            default=os.environ.get("DATABASE_URL", f"sqlite:///{BASE_DIR / 'db.sqlite3'}"),
            conn_max_age=int(os.environ.get("DB_CONN_MAX_AGE", "60")),
            conn_health_checks=True,
        )
    }
    if not DEBUG and "postgresql" not in DATABASES["default"]["ENGINE"]:
        raise ImproperlyConfigured(
            "PostgreSQL is required when DEBUG is off; set DATABASE_URL to a postgres:// URL."
        )

# Password validation

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# Internationalization

LANGUAGE_CODE = "en-us"
# Storage stays UTC; presentation is switched per organization by
# assessments.middleware.OrganizationTimezoneMiddleware (finding 18).
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True


# Static files

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"] if (BASE_DIR / "static").exists() else []
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        # Manifest storage needs a collectstatic run; only require it where the
        # image has performed one. Dev and the test runner use plain storage.
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )
    },
}
WHITENOISE_MAX_AGE = 3600

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"
AUTH_USER_MODEL = "accounts.User"


# Cache. Redis in production; LocMem only when no URL is configured. Rate limits
# and readiness checks both depend on this being shared across processes.


def build_redis_url(db: int) -> str:
    """Build a redis:// URL with a URL-encoded password (audit R2-08)."""
    host = os.environ.get("REDIS_HOST", "").strip()
    if not host:
        return ""
    password = os.environ.get("REDIS_PASSWORD", "")
    if password:
        return f"redis://:{quote(password, safe='')}@{host}:6379/{db}"
    return f"redis://{host}:6379/{db}"


REDIS_URL = os.environ.get("REDIS_URL", "") or build_redis_url(1)
if not DEBUG and not RUNNING_TESTS and not REDIS_URL:
    # Multi-worker gunicorn + LocMem silently breaks rate limits and job locks
    # (audit F11). Compose and production must always share Redis.
    raise ImproperlyConfigured(
        "REDIS_URL or REDIS_HOST must be set when DEBUG is off."
    )
if (
    not DEBUG
    and not RUNNING_TESTS
    and (os.environ.get("REDIS_HOST") or "").strip()
    and not (os.environ.get("REDIS_PASSWORD") or "").strip()
):
    # Prefer REDIS_HOST+REDIS_PASSWORD; bare host URLs leave the broker open.
    # Operators who embed credentials in REDIS_URL should omit REDIS_HOST.
    raise ImproperlyConfigured(
        "REDIS_PASSWORD must be set when REDIS_HOST is used with DEBUG=0 "
        "(or set REDIS_URL with credentials and leave REDIS_HOST unset)."
    )
# Tests ignore staging Redis from .env — LocMem keeps the suite hermetic.
if RUNNING_TESTS or not REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "quizops-local",
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": REDIS_URL,
            "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
        }
    }

CELERY_BROKER_URL = (
    os.environ.get("CELERY_BROKER_URL", "")
    or build_redis_url(0)
    or REDIS_URL
    or "redis://redis:6379/0"
)
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", "") or CELERY_BROKER_URL
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
# A broker outage must never hold a web request open: publishing fails fast and
# the caller logs and continues. Workers keep their own longer retry policy.
CELERY_BROKER_CONNECTION_TIMEOUT = 2
CELERY_BROKER_CONNECTION_MAX_RETRIES = 0
CELERY_BROKER_TRANSPORT_OPTIONS = {"max_retries": 0, "socket_connect_timeout": 2}
CELERY_TASK_IGNORE_RESULT = True
CELERY_TASK_EAGER_PROPAGATES = False
if RUNNING_TESTS:
    # Keep task side-effects in-process; staging broker host is unreachable.
    CELERY_TASK_ALWAYS_EAGER = True
    CELERY_TASK_EAGER_PROPAGATES = True

READYZ_REQUIRE_WORKER = env_bool("READYZ_REQUIRE_WORKER", False)


# Sessions: an exam portal is often used on shared machines.

SESSION_COOKIE_AGE = int(os.environ.get("SESSION_COOKIE_AGE", 60 * 60 * 8))
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_SAVE_EVERY_REQUEST = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS")
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"

# Only trust forwarding headers when a proxy that strips client-supplied copies
# is actually in front of us.
TRUST_PROXY_SSL_HEADER = env_bool("TRUST_PROXY_SSL_HEADER", False)
TRUST_X_FORWARDED_FOR = env_bool("TRUST_X_FORWARDED_FOR", False)
if TRUST_PROXY_SSL_HEADER:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

if not DEBUG and not RUNNING_TESTS:
    # Defaults secure; set SESSION_COOKIE_SECURE=0 / CSRF_COOKIE_SECURE=0 for
    # local HTTP staging so drills and seed demos can complete without TLS.
    SESSION_COOKIE_SECURE = env_bool("SESSION_COOKIE_SECURE", True)
    CSRF_COOKIE_SECURE = env_bool("CSRF_COOKIE_SECURE", True)
    SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", True)
    SECURE_HSTS_SECONDS = int(os.environ.get("SECURE_HSTS_SECONDS", 31_536_000))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True


# Login throttling (spec 12).
LOGIN_RATE_LIMIT = int(os.environ.get("LOGIN_RATE_LIMIT", 10))
LOGIN_RATE_WINDOW_SECONDS = int(os.environ.get("LOGIN_RATE_WINDOW_SECONDS", 300))

# TOTP for org admins and platform operators (audit F17). Off in DEBUG/tests
# so the suite and local demos are not blocked; on by default in production.
# Tests ignore MFA_REQUIRED from staging .env; opt in with @override_settings.
if RUNNING_TESTS:
    MFA_REQUIRED = False
else:
    MFA_REQUIRED = env_bool("MFA_REQUIRED", not DEBUG)
MFA_ISSUER_NAME = os.environ.get("MFA_ISSUER_NAME", "QuizOps")
MFA_RATE_LIMIT = int(os.environ.get("MFA_RATE_LIMIT", 5))
MFA_RATE_WINDOW_SECONDS = int(os.environ.get("MFA_RATE_WINDOW_SECONDS", 300))


# Email: password reset is unusable without a backend.
if RUNNING_TESTS:
    EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    EMAIL_HOST = ""
else:
    EMAIL_BACKEND = os.environ.get(
        "EMAIL_BACKEND",
        "django.core.mail.backends.console.EmailBackend"
        if DEBUG
        else "django.core.mail.backends.smtp.EmailBackend",
    )
    EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", 587))
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "no-reply@quizops.local")
if (
    not DEBUG
    and not RUNNING_TESTS
    and EMAIL_BACKEND.endswith("smtp.EmailBackend")
    and not EMAIL_HOST
):
    # Password reset silently drops mail otherwise: users get "check your
    # email" and nothing arrives.
    raise ImproperlyConfigured(
        "EMAIL_HOST must be set for the SMTP backend, or set EMAIL_BACKEND explicitly."
    )


# Structured logging with correlation ids (spec 15). Answer keys, secrets and
# tokens must never reach these handlers.

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "request_id": {"()": "config.logging_filters.RequestIDFilter"},
        "slow_query": {
            "()": "config.logging_filters.SlowQueryFilter",
            "threshold_ms": float(os.environ.get("SLOW_QUERY_MS", "500")),
        },
    },
    "formatters": {
        "json": {
            "()": "config.logging_filters.JSONFormatter",
        },
        "plain": {
            "format": "[{asctime}] {levelname} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "filters": ["request_id"],
            "formatter": "plain" if DEBUG else "json",
        },
    },
    "root": {"handlers": ["console"], "level": os.environ.get("LOG_LEVEL", "INFO")},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        # Slow-query visibility: DEBUG on django.db.backends logs every query,
        # so it is opt-in via SQL_DEBUG and paired with the timing filter below.
        "django.db.backends": {
            "handlers": ["console"],
            "level": "DEBUG" if env_bool("SQL_DEBUG", False) else "WARNING",
            "propagate": False,
            "filters": ["slow_query"],
        },
        "assessments": {
            "handlers": ["console"],
            "level": os.environ.get("LOG_LEVEL", "INFO"),
            "propagate": False,
        },
    },
}

SENTRY_DSN = os.environ.get("SENTRY_DSN", "")
if SENTRY_DSN:  # pragma: no cover - optional dependency
    try:
        import sentry_sdk
        from sentry_sdk.integrations.django import DjangoIntegration

        sentry_sdk.init(
            dsn=SENTRY_DSN,
            integrations=[DjangoIntegration()],
            send_default_pii=False,
            traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.0")),
        )
    except ImportError:
        pass

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
