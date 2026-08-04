"""Export everything the product holds about one person, as JSON."""
import json

from django.core.management.base import BaseCommand, CommandError

from accounts.models import User
from accounts.privacy import export_user_data


class Command(BaseCommand):
    help = "Export a user's data as JSON (GDPR/portability request)."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--output", help="File to write; defaults to stdout.")

    def handle(self, *args, **options):
        try:
            user = User.objects.get(username=options["username"])
        except User.DoesNotExist as exc:
            raise CommandError(f"No user named {options['username']!r}.") from exc

        payload = json.dumps(export_user_data(user), indent=2)
        if options["output"]:
            with open(options["output"], "w", encoding="utf-8") as handle:
                handle.write(payload)
            self.stdout.write(self.style.SUCCESS(f"Wrote {options['output']}"))
        else:
            # self.stdout, not sys.stdout: the latter bypasses Django's output
            # wrapper, so redirection and call_command(stdout=...) see nothing.
            self.stdout.write(payload)
