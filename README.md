# LMPM — Line & Performance Management

OXLIP line management and performance management app. Built on the same
platform as OSED (Django 6, Microsoft SSO via django-allauth, WhiteNoise
static, Azure App Service + Postgres deployment), with a fresh `core` app and
the review-specific functionality removed.

## Local development

```bash
python -m venv .venv
source .venv/Scripts/activate    # Windows (Git Bash);  use .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
cp .env.example .env             # then set DEBUG=1
python manage.py migrate
python manage.py seed_schools
python manage.py createsuperuser
python manage.py runserver
```

## Layout

- `lmpm/` — project package (settings, urls, wsgi/asgi)
- `core/` — shared platform layer: `School`, `SchoolProfile` (SSO authorisation
  gate), `Branding`, the Microsoft login adapter, branding context processor,
  base template, styling, and the home page.
- New line-management / performance features should each be their own Django
  app, mounted in `lmpm/urls.py`.

## Deployment

See `AZURE_DEPLOYMENT.md` (carried over from OSED). This app needs its **own**
Azure App Service, its **own** Postgres database, and its **own** environment
variables. Point Microsoft SSO at the same Entra tenant as OSED but register a
separate redirect URI for this app.
