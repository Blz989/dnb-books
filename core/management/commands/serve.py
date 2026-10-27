from django.core.management.base import BaseCommand
from django.core.wsgi import get_wsgi_application


class Command(BaseCommand):
    help = "Run the app with the production web server (waitress)."

    def add_arguments(self, parser):
        parser.add_argument("--host", default="0.0.0.0")
        parser.add_argument("--port", type=int, default=8000)

    def handle(self, *args, host, port, **options):
        import waitress

        self.stdout.write(f"DNB Books running on http://{host}:{port}/ (Ctrl+C to stop)")
        waitress.serve(get_wsgi_application(), host=host, port=port)
