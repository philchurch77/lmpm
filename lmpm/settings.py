"""
Django settings for lmpm project (Line & Performance Management).

Adapted from the OSED project: same Microsoft SSO, branding, and Azure
deployment model, with the review-specific app removed. The shared platform
layer (auth restriction, branding, schools) lives in the `core` app.
"""

from pathlib import Path
import os

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Load a local .env file (for local development only). On Azure the real
# environment variables are set by the platform, so the .env file is absent
# there and this is a no-op.
try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.getenv(
    "SECRET_KEY",
    "django-insecure-CHANGE-ME-for-local-dev-only-set-real-key-in-prod",
)

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = _env_bool("DEBUG", default=False)

if not DEBUG and not os.getenv("SECRET_KEY"):
    raise ImproperlyConfigured(
        "SECRET_KEY is required when DEBUG=0 (production). "
        "Set the SECRET_KEY environment variable."
    )

_raw_allowed_hosts = os.getenv("ALLOWED_HOSTS", "")
ALLOWED_HOSTS = [h.strip() for h in _raw_allowed_hosts.split(",") if h.strip()]
if DEBUG and not ALLOWED_HOSTS:
    ALLOWED_HOSTS = ["localhost", "127.0.0.1"]

# Azure App Service exposes the public hostname via WEBSITE_HOSTNAME.
_azure_hostname = os.getenv("WEBSITE_HOSTNAME", "").strip()
if _azure_hostname:
    if _azure_hostname not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(_azure_hostname)
    _azure_origin = f"https://{_azure_hostname}"
    CSRF_TRUSTED_ORIGINS = list(set(globals().get("CSRF_TRUSTED_ORIGINS", []) + [_azure_origin]))


# Application definition

INSTALLED_APPS = [
    'django.contrib.sites',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    # WhiteNoise: must come before staticfiles so its dev static handler is used.
    'whitenoise.runserver_nostatic',
    'django.contrib.staticfiles',

    'allauth',
    'allauth.account',
    'allauth.socialaccount',
    'allauth.socialaccount.providers.microsoft',

    'core',
    'appraisals',
    'line_management',
    'team',
    'overview',
    'data_import',
]


USE_AZURE_MEDIA_STORAGE = os.getenv("USE_AZURE_MEDIA_STORAGE", "").lower() in ("1", "true", "yes")
if USE_AZURE_MEDIA_STORAGE:
    INSTALLED_APPS.append("storages")

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'allauth.account.middleware.AccountMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'lmpm.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'core.context_processors.branding',
                'appraisals.context_processors.appraisal_nav',
                'line_management.context_processors.line_nav',
            ],
        },
    },
]

WSGI_APPLICATION = 'lmpm.wsgi.application'


# Database
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if DATABASE_URL:
    DATABASES["default"] = dj_database_url.parse(
        DATABASE_URL,
        conn_max_age=600,
        ssl_require=not DEBUG,
    )
elif not DEBUG:
    raise ImproperlyConfigured(
        "DATABASE_URL is required when DEBUG=0 (production). "
        "Set DATABASE_URL to your Postgres connection string."
    )


AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]


# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True


# Static files (CSS, JavaScript, Images)
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

if not DEBUG:
    STORAGES["staticfiles"]["BACKEND"] = "whitenoise.storage.CompressedManifestStaticFilesStorage"

# Respect proxy headers for https detection (Azure App Service).
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Production-only transport security. Azure terminates TLS at its proxy; the
# header above lets Django see the original scheme so the redirect can't loop.
if not DEBUG:
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 3600  # raise (e.g. 31536000) once the deployment is proven
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True  # the app host has no subdomains; safe


SITE_ID = 1

# Auth: use Microsoft SSO for sign-in, while we keep authorization in-app.
AUTHENTICATION_BACKENDS = (
    'django.contrib.auth.backends.ModelBackend',
    'allauth.account.auth_backends.AuthenticationBackend',
)

ACCOUNT_LOGIN_METHODS = {'email'}
ACCOUNT_SIGNUP_FIELDS = ['email*', 'username*', 'password1*', 'password2*']
ACCOUNT_EMAIL_VERIFICATION = 'none'
# Accounts are pre-provisioned only: no self-signup (identity is by email, so
# open signup would let anyone claim a staff email). The dangerous allauth
# endpoints (signup, email management, password reset) are also 404'd in urls.py.
ACCOUNT_ADAPTER = 'core.allauth_adapters.NoSignupAccountAdapter'

SOCIALACCOUNT_QUERY_EMAIL = True
SOCIALACCOUNT_AUTO_SIGNUP = False
SOCIALACCOUNT_ADAPTER = 'core.allauth_adapters.RestrictMicrosoftLoginAdapter'

MICROSOFT_CLIENT_ID = os.getenv('MICROSOFT_CLIENT_ID', '')
MICROSOFT_CLIENT_SECRET = os.getenv('MICROSOFT_CLIENT_SECRET', '')
# Use "organizations" for Entra org accounts, or a tenant ID to lock to one tenant.
MICROSOFT_TENANT = os.getenv('MICROSOFT_TENANT', 'organizations')


SOCIALACCOUNT_PROVIDERS = {
    'microsoft': {
        'APPS': [
            {
                'client_id': MICROSOFT_CLIENT_ID,
                'secret': MICROSOFT_CLIENT_SECRET,
                'settings': {
                    'tenant': MICROSOFT_TENANT,
                },
            }
        ]
    }
}


LOGIN_REDIRECT_URL = "/"
LOGIN_URL = "/accounts/login/"
LOGOUT_REDIRECT_URL = "/accounts/login/"

# Logging — always emit Django errors to stderr so they appear in Azure logs.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {"class": "logging.StreamHandler"},
    },
    "root": {"handlers": ["console"], "level": "WARNING"},
    "loggers": {
        "django": {"handlers": ["console"], "level": "ERROR", "propagate": False},
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
    },
}


MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

SERVE_MEDIA = _env_bool("SERVE_MEDIA", default=False)

# Serve committed demo media via WhiteNoise under STATIC_URL when enabled.
# collectstatic gathers media/ into staticfiles/media/ (via STATICFILES_DIRS
# below) so WhiteNoise can serve it at /static/media/ — the URL MEDIA_URL points
# at. Without the STATICFILES_DIRS entry the URL resolves to nothing (404).
MEDIA_AS_STATIC = _env_bool("MEDIA_AS_STATIC", default=False)
if MEDIA_AS_STATIC and not USE_AZURE_MEDIA_STORAGE:
    MEDIA_URL = f"{STATIC_URL}media/"
    SERVE_MEDIA = False
    STATICFILES_DIRS = [("media", MEDIA_ROOT)]

if USE_AZURE_MEDIA_STORAGE:
    AZURE_ACCOUNT_NAME = os.getenv("AZURE_ACCOUNT_NAME", "").strip()
    AZURE_ACCOUNT_KEY = os.getenv("AZURE_ACCOUNT_KEY", "").strip()
    AZURE_CONTAINER = os.getenv("AZURE_CONTAINER", "media").strip() or "media"
    AZURE_CUSTOM_DOMAIN = os.getenv("AZURE_CUSTOM_DOMAIN", "").strip()

    if not AZURE_ACCOUNT_NAME or not AZURE_ACCOUNT_KEY:
        raise ImproperlyConfigured(
            "USE_AZURE_MEDIA_STORAGE is enabled but AZURE_ACCOUNT_NAME/AZURE_ACCOUNT_KEY are missing."
        )

    STORAGES["default"]["BACKEND"] = "storages.backends.azure_storage.AzureStorage"

    if AZURE_CUSTOM_DOMAIN:
        MEDIA_URL = f"https://{AZURE_CUSTOM_DOMAIN.rstrip('/')}/"
    else:
        MEDIA_URL = f"https://{AZURE_ACCOUNT_NAME}.blob.core.windows.net/{AZURE_CONTAINER}/"

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
