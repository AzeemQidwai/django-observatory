"""Minimal project showing the documented three-step installation."""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = "demo-only-not-secret"
DEBUG = True
ALLOWED_HOSTS = ["*"]
ROOT_URLCONF = "demo.urls"
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
STATIC_URL = "static/"
LOGIN_URL = "/admin/login/"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "demo",
    "django_observatory",
]

MIDDLEWARE = [
    "django_observatory.middleware.ObservabilityMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3",
                         "OPTIONS": {"timeout": 20}}}

OBSERVABILITY = {
    "ENVIRONMENT": "development",
    "SERVICE_NAME": "project-api",
    "RELEASE": "2026.10.04.1",
    "EXTERNAL_HTTP": {"SERVICES": {"127.0.0.1": "SAP", "localhost": "SAP"}},
    "DATABASE": {"SLOW_QUERY_MS": 200},
    "METRICS": {"FLUSH_INTERVAL": 10},
    "SCHEDULER": {"INTERVAL": 15},
    "INCIDENTS": {"MIN_REQUESTS": 10},
    "REQUESTS": {"IGNORE_PATHS": ["/static/", "/favicon.ico", "/sap/"]},  # /sap/ plays the external system
}
