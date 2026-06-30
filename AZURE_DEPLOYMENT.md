# Azure Deployment Review & Action Plan

> **Purpose:** This document is the single source of truth for publishing the **OSED**
> Django app to **Azure App Service (Linux)**. Point Claude Code in VS Code at this file
> and ask it to apply the changes in the "Code changes required" section.
>
> **Reviewed:** 2026-06-13 · **Target platform:** Azure App Service for Linux (Python 3.13)
> **Current state:** The project is fully configured for **Render**. It has good Azure
> bones already (`dj-database-url`, `django-storages[azure]`, WhiteNoise), but a handful
> of changes are needed before it will run correctly on Azure.

---

## 1. Verdict at a glance

| Area | Status | Action |
|------|--------|--------|
| WSGI / gunicorn | ✅ Ready | `gunicorn` already in requirements (Linux marker) |
| Static files (WhiteNoise) | ✅ Ready | No change needed |
| Database driver | ✅ Ready | `psycopg[binary]` + `dj-database-url` already wired |
| Azure Blob media | ✅ Ready | `django-storages[azure]` already wired behind `USE_AZURE_MEDIA_STORAGE` |
| **ALLOWED_HOSTS on Azure** | ❌ **Broken** | Reads Render env var only — **must add `WEBSITE_HOSTNAME`** |
| **CSRF_TRUSTED_ORIGINS on Azure** | ❌ **Broken** | Same — must derive from `WEBSITE_HOSTNAME` |
| **Migrations / seed data** | ⚠️ Needs work | Currently in Render `buildCommand`; needs an Azure startup script |
| **Startup command** | ⚠️ Needs work | Must be set explicitly in Azure (see §5) |
| Database choice | ⚠️ Decision | **Recommend Postgres Flexible Server** — see §3 |
| Secrets / `SECRET_KEY` | ⚠️ Decision | Must set as App Setting (see §4) |

**Bottom line:** ~30 minutes of code changes (§2) + Azure portal config (§4–§6). The app
will not start on Azure until the `WEBSITE_HOSTNAME` change in §2 is applied, because
`DEBUG=0` with an empty `ALLOWED_HOSTS` rejects every request with a 400.

---

## 2. Code changes required

These are the edits Claude Code should make. File: `osed/settings.py`.

### 2.1 — Add Azure host + CSRF detection (CRITICAL)

The current code only recognises Render's hostname env vars:

```python
# osed/settings.py (current — lines ~53-60)
_render_external_hostname = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
if _render_external_hostname and _render_external_hostname not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(_render_external_hostname)

_render_external_url = os.getenv("RENDER_EXTERNAL_URL", "").strip()
if _render_external_url:
    CSRF_TRUSTED_ORIGINS = [_render_external_url]
```

Azure App Service exposes the public hostname as `WEBSITE_HOSTNAME` (e.g.
`osed.azurewebsites.net`). **Add the following directly after the Render block** so the
app works on either platform:

```python
# --- Azure App Service (Linux) host + CSRF detection ---
# Azure injects WEBSITE_HOSTNAME (e.g. "osed.azurewebsites.net").
_azure_hostname = os.getenv("WEBSITE_HOSTNAME", "").strip()
if _azure_hostname and _azure_hostname not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(_azure_hostname)
    # CSRF_TRUSTED_ORIGINS must be a full scheme+host; Azure terminates TLS at the front end.
    CSRF_TRUSTED_ORIGINS = list(globals().get("CSRF_TRUSTED_ORIGINS", []))
    CSRF_TRUSTED_ORIGINS.append(f"https://{_azure_hostname}")
```

> If you later put a **custom domain** in front (e.g. `osed.yourschool.org`), add it to the
> `ALLOWED_HOSTS` App Setting and to `CSRF_TRUSTED_ORIGINS` as `https://osed.yourschool.org`.

### 2.2 — Confirm the SSL proxy header (already present, just verify)

`SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")` is already set (line ~197)
and is correct for Azure too — Azure's front end sets `X-Forwarded-Proto`. No change needed.

### 2.3 — Add a startup script

Create a new file **`startup.sh`** in the project root (this becomes the Azure startup
command — see §5). It runs migrations + first-time seeding, then launches gunicorn:

```bash
#!/usr/bin/env bash
set -e

# Apply any pending schema migrations on every release.
python manage.py migrate --noinput

# Idempotent seed steps (each command is safe to re-run).
python manage.py ensure_schema
python manage.py load_indepth_blueprint
python manage.py seed_schools
python manage.py seed_branding

# Create the admin user once, if DJANGO_SUPERUSER_* env vars are set.
python manage.py createsuperuser --noinput || true

# Launch the app. Azure's built-in Python container injects PORT (and nginx
# forwards to it); default to 8000 if it's ever unset. Using ${PORT} keeps this
# script portable across Azure and Render with no edits.
# --access-logfile/--error-logfile '-' send gunicorn logs to stdout/stderr so
# they surface in the App Service Log Stream.
exec gunicorn osed.wsgi:application \
    --bind=0.0.0.0:${PORT:-8000} \
    --workers=3 \
    --timeout=120 \
    --access-logfile '-' \
    --error-logfile '-'
```

> **Do NOT run `collectstatic` here.** Azure's Oryx build runs `collectstatic`
> automatically during deployment when `DISABLE_COLLECTSTATIC` is not set to `1`
> (see §4). Running it at startup just slows boot.
>
> **`--workers=3`** is fine for Postgres. **If you stay on SQLite, set `--workers=1`**
> (see §3) to avoid database-locked errors.

### 2.4 — (Optional) `.deployment` / build settings

No `.deployment` file is required. Azure's Oryx builder auto-detects `requirements.txt`,
installs dependencies, and runs `collectstatic`. Just ensure the App Settings in §4 are
in place before first deploy.

---

## 3. Data storage: Postgres or SQLite?

**Recommendation: use Azure Database for PostgreSQL — Flexible Server (Burstable B1ms),
even for the initial build.** The app is already wired for it (`DATABASE_URL` +
`dj-database-url` + `psycopg`), so there is no extra code cost.

### Why not SQLite on Azure App Service?

| Concern | SQLite on Azure | Postgres Flexible Server |
|---------|-----------------|--------------------------|
| File location | Lives on `/home` (a mounted **network share**) | Managed service, separate from app |
| Concurrency | Network-share + multiple gunicorn workers → **`database is locked`** errors and corruption risk | Built for concurrent connections |
| Multi-instance scale-out | **Breaks** — each instance gets a different file | Works |
| Deploy/restart | Survives (on `/home`) but locking risk remains | Unaffected |
| Backups | Manual | Automated point-in-time restore |
| Cost | £0 | ~£10–13/mo (B1ms), or free credits / student tier |

This is a **multi-user app with Microsoft SSO and per-school data** — exactly the workload
SQLite is poor at on a network filesystem. SQLite is genuinely fine for *local development*
(as it is now), but on Azure App Service it is fragile.

### If you must start on SQLite anyway (pure demo, single evaluator)

It can work as a stopgap **only** if you:
1. **Do not set `DATABASE_URL`** (the app falls back to SQLite — but note line ~137 in
   `settings.py` raises `ImproperlyConfigured` if `DEBUG=0` and no `DATABASE_URL`). You would
   need to relax that guard, which is **not recommended**.
2. Place the DB on the persistent `/home` mount (default `BASE_DIR` is *not* on `/home`).
3. Run **`--workers=1`** in `startup.sh`.

Given the existing `DEBUG=0` guard already *forces* `DATABASE_URL` in production, the project
was clearly designed to use Postgres in production. **Follow that design — provision Postgres.**

---

## 4. Environment variables (Azure "App Settings")

Set these under **App Service → Settings → Environment variables → App settings**.
(Azure App Settings are injected as environment variables — exactly what `settings.py` reads.)

### Required

| Name | Value | Notes |
|------|-------|-------|
| `DEBUG` | `0` | Turns on production mode + the SECRET_KEY/DATABASE_URL guards |
| `SECRET_KEY` | *(generate a long random string)* | **Never reuse the dev key in `settings.py`.** See command below |
| `DATABASE_URL` | `postgres://USER:PASSWORD@HOST:5432/DBNAME?sslmode=require` | From your Postgres Flexible Server connection string |
| `SCM_DO_BUILD_DURING_DEPLOYMENT` | `1` | Tells Oryx to install deps + run collectstatic during deploy |

### Recommended / situational

| Name | Value | When |
|------|-------|------|
| `ALLOWED_HOSTS` | `osed.azurewebsites.net` (+ custom domain, comma-separated) | Optional — §2.1 auto-adds the Azure host, but set this if you use a custom domain |
| `WEBSITE_HOSTNAME` | *(auto-set by Azure — do not set manually)* | Azure provides this; §2.1 reads it |
| `DJANGO_SUPERUSER_USERNAME` | e.g. `admin` | For first-run `createsuperuser` |
| `DJANGO_SUPERUSER_EMAIL` | your email | Same |
| `DJANGO_SUPERUSER_PASSWORD` | *(strong password)* | Same — **remove after first successful boot** |
| `MICROSOFT_CLIENT_ID` | from Entra app registration | Microsoft SSO |
| `MICROSOFT_CLIENT_SECRET` | from Entra app registration | Microsoft SSO |
| `MICROSOFT_TENANT` | `organizations` or a tenant GUID | Microsoft SSO |

### Media storage (only if you need persistent user uploads — e.g. school logos)

App Service local disk under `/home` *is* persistent, but for production-grade media use
Azure Blob Storage (already supported in code):

| Name | Value |
|------|-------|
| `USE_AZURE_MEDIA_STORAGE` | `1` |
| `SERVE_MEDIA` | `0` |
| `AZURE_ACCOUNT_NAME` | your storage account name |
| `AZURE_ACCOUNT_KEY` | your storage account key |
| `AZURE_CONTAINER` | `media` |
| `AZURE_CUSTOM_DOMAIN` | *(optional CDN/custom domain)* |

> If you're only relying on the **committed demo branding/logo assets** (not user uploads),
> you can instead set `MEDIA_AS_STATIC=1` and skip Blob storage entirely — WhiteNoise will
> serve them from `/static/media/`. This matches the current Render setup.

**Generate a SECRET_KEY:**
```bash
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

---

## 5. Startup command (the tricky bit)

In **App Service → Settings → Configuration → General settings → Startup Command**, set:

```
bash startup.sh
```

This points Azure at the script created in §2.3. Why this approach:

- Azure's default Python startup auto-detects `wsgi.py` and runs gunicorn, **but it will
  not run your migrations or seed commands.** A startup script is the cleanest way to do both.
- **Port:** Azure's built-in Python (Oryx) container runs gunicorn behind an nginx reverse
  proxy and exposes the expected port via the `PORT` environment variable (per the
  [App Service on Linux FAQ](https://learn.microsoft.com/troubleshoot/azure/app-service/faqs-app-service-linux-new#other-questions)).
  The script binds to `--bind=0.0.0.0:${PORT:-8000}`, which uses Azure's injected `PORT`
  and falls back to `8000` if it's unset. This is also the Render convention, so the same
  script works unchanged on either platform. (Don't hard-code `:8000` — it works today only
  because 8000 is the current default.)
- `exec` replaces the shell with gunicorn so signals (restart/stop) are handled correctly.

**Alternative (no script file):** you can paste a one-liner directly into the Startup
Command box instead of using `startup.sh`:

```
python manage.py migrate --noinput && python manage.py ensure_schema && python manage.py load_indepth_blueprint && python manage.py seed_schools && python manage.py seed_branding && gunicorn osed.wsgi:application --bind=0.0.0.0:${PORT:-8000} --workers=3 --timeout=120 --access-logfile '-' --error-logfile '-'
```

The script (§2.3) is preferred — it's readable, version-controlled, and includes the
superuser bootstrap.

---

## 6. Step-by-step Azure setup

1. **Create resources** (Azure Portal or CLI):
   - Resource Group (e.g. `osed-rg`)
   - **App Service Plan** — Linux, B1 tier is fine to start
   - **Web App** — runtime stack **Python 3.13**
   - **Azure Database for PostgreSQL — Flexible Server** (Burstable B1ms)
     - During creation, allow Azure services to connect, or add a firewall rule.
     - Note the connection string for `DATABASE_URL` (append `?sslmode=require`).
2. **Configure App Settings** (§4) — set `DEBUG=0`, `SECRET_KEY`, `DATABASE_URL`,
   `SCM_DO_BUILD_DURING_DEPLOYMENT=1`, superuser vars, SSO vars.
3. **Set the Startup Command** (§5) → `bash startup.sh`.
4. **Apply code changes** (§2) and commit.
5. **Deploy** — via GitHub Actions, `az webapp up`, or VS Code Azure extension. Oryx will
   `pip install -r requirements.txt` and run `collectstatic`.
6. **Watch the Log Stream** (App Service → Monitoring → Log stream) on first boot to confirm
   migrations ran and gunicorn started.
7. **Microsoft SSO redirect URI:** in your Entra app registration, add the redirect URI
   `https://osed.azurewebsites.net/accounts/microsoft/login/callback/` (and the custom-domain
   equivalent if used).
8. **After first successful boot:** delete `DJANGO_SUPERUSER_PASSWORD` from App Settings.

---

## 7. Pre-publish checklist

- [ ] §2.1 `WEBSITE_HOSTNAME` host/CSRF block added to `settings.py`
- [ ] `startup.sh` created (§2.3) and committed
- [ ] `DEBUG=0` set in App Settings
- [ ] `SECRET_KEY` generated and set (not the dev default)
- [ ] Postgres Flexible Server provisioned; `DATABASE_URL` set with `?sslmode=require`
- [ ] `SCM_DO_BUILD_DURING_DEPLOYMENT=1` set
- [ ] Startup Command set to `bash startup.sh`
- [ ] Microsoft SSO: client ID/secret/tenant set; redirect URI registered in Entra
- [ ] Media strategy chosen (`MEDIA_AS_STATIC=1` for demo assets, or Azure Blob for uploads)
- [ ] First deploy log shows: `migrate` OK → seed commands OK → gunicorn listening on the
      injected `$PORT` (8000 by default)
- [ ] App loads over HTTPS without a 400 (confirms `ALLOWED_HOSTS` is correct)
- [ ] Superuser password env var removed after first boot
- [ ] (Optional) Run `python manage.py check --deploy` locally with `DEBUG=0` to catch
      remaining security warnings

---

## 8. Notes & known-good facts about this project

- **Django 6.0.5**, Python 3.13 — supported on Azure App Service Linux.
- `requirements.txt` already gates `gunicorn` and `psycopg[binary]` to non-Windows, so they
  install correctly on Azure Linux and stay out of your way on local Windows dev.
- WhiteNoise (`CompressedManifestStaticFilesStorage`) handles static files in production —
  no separate static host needed.
- The `DEBUG=0` guards in `settings.py` (lines ~42 and ~137) **require** `SECRET_KEY` and
  `DATABASE_URL` to be set, so a misconfigured deploy fails loudly rather than silently
  running insecure — this is good, just be aware of it.
- `render.yaml` can stay in the repo (harmless on Azure) or be deleted once you've fully
  migrated off Render.
