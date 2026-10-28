import os
import sys
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

DEBUG = os.environ.get("DNB_DEBUG", "") == "1"
SECRET_KEY = os.environ.get("DNB_SECRET_KEY", "")
if SECRET_KEY in ("", "change-me"):
    if DEBUG or "pytest" in sys.modules:
        SECRET_KEY = "dev-and-test-only-key"
    else:
        raise ImproperlyConfigured(
            "DNB_SECRET_KEY is not set. Copy .env.example to .env and put a long random value there."
        )
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("DNB_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]

DNB_DB_PATH = Path(os.environ.get("DNB_DB_PATH") or BASE_DIR / "data" / "dnb.sqlite3")
DNB_BACKUP_DIR = Path(os.environ["DNB_BACKUP_DIR"]) if os.environ.get("DNB_BACKUP_DIR") else BASE_DIR / "data" / "backups"
DNB_BACKUP_KEY = os.environ.get("DNB_BACKUP_KEY", "")
# Off-site copy through rclone, e.g. "gdrive:dnb-backups". Blank keeps backups local only.
DNB_BACKUP_REMOTE = os.environ.get("DNB_BACKUP_REMOTE", "").strip()
DNB_RCLONE = [os.environ.get("DNB_RCLONE", "rclone")]
# Scheduler (docker compose "scheduler" service), local time, HH:MM.
DNB_BACKUP_TIME = os.environ.get("DNB_BACKUP_TIME", "01:15")
DNB_DRILL_TIME = os.environ.get("DNB_DRILL_TIME", "02:15")

# Banking. Plaid keys come only from .env; DNB_FIELD_KEY encrypts the stored Plaid access tokens.
PLAID_CLIENT_ID = os.environ.get("PLAID_CLIENT_ID", "")
PLAID_SECRET = os.environ.get("PLAID_SECRET", "")
PLAID_ENV = os.environ.get("PLAID_ENV", "sandbox")
DNB_FIELD_KEY = os.environ.get("DNB_FIELD_KEY", "")
DNB_BANK_SYNC_TIMES = os.environ.get("DNB_BANK_SYNC_TIMES", "06:00,10:00,14:00,18:00")

# data/ is not in git, so a fresh install needs it created.
DNB_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
(BASE_DIR / "data").mkdir(exist_ok=True)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "auditlog",
    "core",
    "ledger",
    "items",
    "contacts",
    "sales",
    "purchasing",
    "handling",
    "banking",
    "backup",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "auditlog.middleware.AuditlogMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "dnb.urls"
WSGI_APPLICATION = "dnb.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.nav",
            ],
        },
    },
]

# WAL plus IMMEDIATE transactions and a 20 second timeout let two people save
# at the same moment: the second save waits instead of failing.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": DNB_DB_PATH,
        "OPTIONS": {
            "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
            "transaction_mode": "IMMEDIATE",
            "timeout": 20,
        },
        # A real file (not :memory:) so threaded and backup tests work.
        "TEST": {"NAME": BASE_DIR / "data" / "test.sqlite3"},
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/accounts/login/"

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("DNB_TIME_ZONE", "America/Los_Angeles")
USE_I18N = False
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# Uploaded files (the invoice logo). Not served publicly; an authenticated view hands them out.
MEDIA_ROOT = DNB_DB_PATH.parent / "media"
MEDIA_URL = "/media/"
