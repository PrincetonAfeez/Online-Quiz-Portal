"""Send a branded password-reset email (staging SMTP / Mailpit drill)."""

import os
import sys

import django

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.contrib.auth import get_user_model  # noqa: E402
from django.contrib.auth.forms import PasswordResetForm  # noqa: E402

email = os.environ.get("DRILL_RESET_EMAIL", "student@example.com")
User = get_user_model()
if not User.objects.filter(email__iexact=email).exists():
    raise SystemExit(f"no user with email {email}")

form = PasswordResetForm({"email": email})
if not form.is_valid():
    raise SystemExit(form.errors)
form.save(use_https=False, domain_override="localhost", from_email=None, request=None)
print("password_reset_sent")
