"""Right-to-erasure: anonymize the subject, retain the assessment record."""
from django.core.management.base import BaseCommand, CommandError

from accounts.models import User
from accounts.privacy import anonymize_user


class Command(BaseCommand):
    help = (
        "Anonymize a user (right to erasure). Assessment facts are retained as "
        "anonymous records; identifying fields are severed."
    )

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--reason", default="subject request")
        parser.add_argument(
            "--yes", action="store_true", help="Skip the confirmation prompt."
        )

    def handle(self, *args, **options):
        try:
            user = User.objects.get(username=options["username"])
        except User.DoesNotExist as exc:
            raise CommandError(f"No user named {options['username']!r}.") from exc

        if not options["yes"]:
            raise CommandError(
                f"This will irreversibly anonymize {user.username!r}. "
                "Re-run with --yes to proceed."
            )

        tombstone = anonymize_user(user, reason=options["reason"])
        self.stdout.write(
            self.style.SUCCESS(f"Anonymized. Subject tombstone: {tombstone}")
        )
