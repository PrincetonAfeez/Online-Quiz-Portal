"""Confirm privileged login redirects into MFA when MFA_REQUIRED is on."""

import os
import sys

import django

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.conf import settings  # noqa: E402
from django.test import Client  # noqa: E402

if not settings.MFA_REQUIRED:
    raise SystemExit("MFA_REQUIRED is off; refuse to call this a pass")

client = Client(HTTP_HOST="localhost")
response = client.post(
    "/accounts/login/",
    {"username": "admin", "password": "quizops-demo-2026"},
)
location = response.get("Location") or ""
print(response.status_code, location)
if response.status_code not in (301, 302) or "/accounts/mfa/" not in location:
    raise SystemExit(f"expected MFA redirect, got {response.status_code} {location}")
print("mfa_ok")
