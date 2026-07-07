# Azure Deployment Guide — LMPM

> **Purpose:** Single source of truth for publishing the **LMPM** (Line & Performance
> Management) Django app to **Azure App Service (Linux)**.
>
> **Target platform:** Azure App Service for Linux (Python 3.13), Azure Database for
> PostgreSQL — Flexible Server.
>
> **Current state:** The app is **already code-ready for Azure**. The `WEBSITE_HOSTNAME`
> host/CSRF detection, the `startup.sh` release script, WhiteNoise, `dj-database-url`, and
> `django-storages[azure]` are all wired up. What remains is **Azure portal provisioning**
> and enabling the deploy pipeline — no source changes are required for the app to run.

---

## 1. Verdict at a glance

| Area | Status | Notes |
|------|--------|-------|
| WSGI / gunicorn | ✅ Ready | `gunicorn` in requirements (Linux marker); launched by `startup.sh` |
| Static files (WhiteNoise) | ✅ Ready | `CompressedManifestStaticFilesStorage` in prod |
| Database driver | ✅ Ready | `psycopg[binary]` + `dj-database-url` wired |
| Azure Blob media | ✅ Ready | `django-storages[azure]` behind `USE_AZURE_MEDIA_STORAGE` |
| ALLOWED_HOSTS / CSRF on Azure | ✅ Ready | `settings.py` reads Azure's `WEBSITE_HOSTNAME` and derives `CSRF_TRUSTED_ORIGINS` |
| Startup script | ✅ Ready | `startup.sh` runs migrate + seed + collectstatic, then gunicorn |
| SSL proxy header | ✅ Ready | `SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")` |
| `SECRET_KEY` / `DATABASE_URL` guards | ✅ Ready | Import-time hard-fail when `DEBUG=0` and either is missing |
| **Postgres Flexible Server** | ⚠️ Provision | The `DEBUG=0` guard *forces* `DATABASE_URL` — see §3 |
| **App Settings** | ⚠️ Configure | Set in the portal — see §4 |
| **Startup Command** | ⚠️ Configure | Set to `bash startup.sh` — see §5 |
| **Deploy pipeline secret** | ⚠️ Configure | Add `AZURE_WEBAPP_PUBLISH_PROFILE` — see §6 |

**Bottom line:** No code changes. The remaining work is Azure portal config (§4–§6). The app
will 400 on every request until `WEBSITE_HOSTNAME` is present (Azure injects it automatically)
**and** `DEBUG=0` with a valid `DATABASE_URL` + `SECRET_KEY`.

---

## 2. What's already done in the codebase (no action needed)

- **Azure host + CSRF detection** — `lmpm/settings.py` reads `WEBSITE_HOSTNAME` (the hostname
  Azure injects, e.g. `lmpm.azurewebsites.net`), appends it to `ALLOWED_HOSTS`, and adds
  `https://<host>` to `CSRF_TRUSTED_ORIGINS`. A custom domain can be added via the
  `ALLOWED_HOSTS` App Setting.
- **SSL proxy header** — `SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")` is set;
  correct for Azure's TLS-terminating front end.
- **Production hardening** — when `DEBUG=0`, `settings.py` enables `SECURE_SSL_REDIRECT`,
  secure session/CSRF cookies, and HSTS.
- **`startup.sh`** — committed at the repo root; this is the Azure startup command (§5). It:
  1. activates the Oryx virtualenv (`antenv`),
  2. runs `python manage.py migrate --noinput`,
  3. seeds base data: `python manage.py seed_schools` and `python manage.py seed_branding`
     (both idempotent),
  4. runs `python manage.py collectstatic --noinput`,
  5. launches gunicorn on `lmpm.wsgi:application`, binding `0.0.0.0:${PORT:-8000}`.

> **collectstatic note:** `startup.sh` runs `collectstatic` at boot. You may instead let Oryx
> do it during the build by relying on `SCM_DO_BUILD_DURING_DEPLOYMENT=1`. Leaving it in
> `startup.sh` is harmless — it's idempotent and only adds a little boot time. No change needed.

---

## 3. Data storage: Postgres (required in production)

**Provision Azure Database for PostgreSQL — Flexible Server (Burstable B1ms).** The app is
already wired for it (`DATABASE_URL` + `dj-database-url` + `psycopg`), and `settings.py`
**raises `ImproperlyConfigured` at import if `DEBUG=0` and `DATABASE_URL` is unset** — so the
project is designed to run on Postgres in production. SQLite is used only for local dev.

### Why not SQLite on Azure App Service?

| Concern | SQLite on Azure | Postgres Flexible Server |
|---------|-----------------|--------------------------|
| File location | `/home` network share | Managed service, separate from app |
| Concurrency | Multiple gunicorn workers → `database is locked` / corruption risk | Built for concurrency |
| Multi-instance scale-out | Breaks (each instance gets a different file) | Works |
| Backups | Manual | Automated point-in-time restore |
| Cost | £0 | ~£10–13/mo (B1ms), or free/student credits |

LMPM is a multi-user SSO app with per-school data — exactly the workload SQLite is poor at on a
network filesystem. Provision Postgres.

---

## 4. Environment variables (Azure "App Settings")

Set under **App Service → Settings → Environment variables → App settings**. Azure injects
these as environment variables, which is exactly what `settings.py` reads.

### Required

| Name | Value | Notes |
|------|-------|-------|
| `DEBUG` | `0` | Enables production mode + the SECRET_KEY/DATABASE_URL guards |
| `SECRET_KEY` | *(long random string)* | Never reuse the dev default. Generate with the command below |
| `DATABASE_URL` | `postgres://USER:PASS@HOST:5432/DB?sslmode=require` | From the Postgres Flexible Server connection string |
| `SCM_DO_BUILD_DURING_DEPLOYMENT` | `1` | Tells Oryx to install deps (and optionally collectstatic) during deploy |

### Microsoft SSO (django-allauth)

| Name | Value |
|------|-------|
| `MICROSOFT_CLIENT_ID` | from the Entra app registration |
| `MICROSOFT_CLIENT_SECRET` | from the Entra app registration |
| `MICROSOFT_TENANT` | `organizations` or a tenant GUID |

### Recommended / situational

| Name | Value | When |
|------|-------|------|
| `ALLOWED_HOSTS` | `lmpm.azurewebsites.net` (+ custom domain, comma-separated) | Only if you use a custom domain — the Azure host is auto-added from `WEBSITE_HOSTNAME` |
| `WEBSITE_HOSTNAME` | *(auto-set by Azure — do not set manually)* | Azure provides it; `settings.py` reads it |

> **Do not set `WEBSITE_HOSTNAME` yourself** — Azure injects it. Setting a wrong value breaks host validation.

### Media

LMPM has two supported media strategies:

- **Committed demo/branding assets only (typical):** set `MEDIA_AS_STATIC=1`. WhiteNoise serves
  media from `/static/media/`; no Blob storage needed.
- **Persistent user uploads (e.g. school logos):** use Azure Blob Storage:

  | Name | Value |
  |------|-------|
  | `USE_AZURE_MEDIA_STORAGE` | `1` |
  | `SERVE_MEDIA` | `0` |
  | `AZURE_ACCOUNT_NAME` | your storage account name |
  | `AZURE_ACCOUNT_KEY` | your storage account key |
  | `AZURE_CONTAINER` | `media` |
  | `AZURE_CUSTOM_DOMAIN` | *(optional CDN/custom domain)* |

**Generate a SECRET_KEY:**
```bash
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

---

## 5. Startup command

In **App Service → Settings → Configuration → General settings → Startup Command**, set:

```
bash startup.sh
```

Why: Azure's default Python startup auto-detects `wsgi.py` and runs gunicorn, **but it will not
run your migrations or seed commands.** `startup.sh` (§2) handles migrate + seed + collectstatic
and then `exec`s gunicorn so restart/stop signals are handled correctly. It binds
`0.0.0.0:${PORT:-8000}`, using Azure's injected `PORT`.

---

## 6. Deploy pipeline (GitHub Actions)

The repo includes `.github/workflows/azure-deploy.yml`, which deploys on every push to `main`
(and on manual `workflow_dispatch`).

- **`app-name: lmpm`** — matches the Web App name. If you name the Web App something other than
  `lmpm`, update this value.
- **Add the repo secret `AZURE_WEBAPP_PUBLISH_PROFILE`** (Settings → Secrets and variables →
  Actions). Download the publish profile from the new Web App → **Get publish profile** /
  Deployment Center. **The workflow no-ops (or fails auth) until this secret exists.**

Alternatives to the GitHub workflow: `az webapp up` or the VS Code Azure extension.

---

## 7. Step-by-step Azure setup

1. **Create resources** (Portal or CLI):
   - Resource Group (e.g. `lmpm-rg`)
   - **App Service Plan** — Linux, B1 tier is fine to start
   - **Web App** — runtime stack **Python 3.13**, name `lmpm` (matches the workflow)
   - **Azure Database for PostgreSQL — Flexible Server** (Burstable B1ms); allow Azure services
     to connect (or add a firewall rule). Note the connection string; append `?sslmode=require`.
2. **Configure App Settings** (§4): `DEBUG=0`, `SECRET_KEY`, `DATABASE_URL`,
   `SCM_DO_BUILD_DURING_DEPLOYMENT=1`, the three `MICROSOFT_*` vars, and your media choice.
3. **Set the Startup Command** (§5) → `bash startup.sh`.
4. **Enable the pipeline** (§6): add `AZURE_WEBAPP_PUBLISH_PROFILE`.
5. **Deploy** — push to `main` (or `az webapp up`). Oryx runs `pip install -r requirements.txt`.
6. **Watch the Log Stream** (App Service → Monitoring → Log stream) on first boot: confirm
   `migrate` OK → `seed_schools`/`seed_branding` OK → gunicorn listening on `$PORT`.
7. **Microsoft SSO redirect URI:** in the Entra app registration, add
   `https://lmpm.azurewebsites.net/accounts/microsoft/login/callback/` (and the custom-domain
   equivalent if used).
8. **Create a superuser:** SSH into the App Service (or use the console) and run
   `python manage.py createsuperuser`, or pre-provision the admin `User` another way. There is
   **no self-service signup** — accounts must be pre-provisioned (see CLAUDE.md → Auth).

---

## 8. Pre-publish checklist

- [ ] Postgres Flexible Server provisioned; `DATABASE_URL` set with `?sslmode=require`
- [ ] `DEBUG=0` set in App Settings
- [ ] `SECRET_KEY` generated and set (not the dev default)
- [ ] `SCM_DO_BUILD_DURING_DEPLOYMENT=1` set
- [ ] Startup Command set to `bash startup.sh`
- [ ] Microsoft SSO: client ID/secret/tenant set; redirect URI registered in Entra
- [ ] Media strategy chosen (`MEDIA_AS_STATIC=1` for demo assets, or Azure Blob for uploads)
- [ ] `AZURE_WEBAPP_PUBLISH_PROFILE` secret added to the GitHub repo (and `app-name` matches the Web App)
- [ ] First deploy log shows: `migrate` OK → `seed_*` OK → gunicorn on `$PORT`
- [ ] App loads over HTTPS without a 400 (confirms `ALLOWED_HOSTS`/`WEBSITE_HOSTNAME`)
- [ ] Superuser created and SSO sign-in verified end-to-end
- [ ] (Optional) `python manage.py check --deploy` locally with `DEBUG=0` + a real `SECRET_KEY`

---

## 9. Notes & known-good facts

- **Django 6.0.5**, Python 3.13 — supported on Azure App Service Linux.
- `requirements.txt` gates `gunicorn` and `psycopg[binary]` to non-Windows, so they install on
  Azure Linux and stay out of the way of local Windows dev.
- WhiteNoise (`CompressedManifestStaticFilesStorage`) serves static files in production — no
  separate static host needed.
- The `DEBUG=0` guards in `settings.py` **require** `SECRET_KEY` and `DATABASE_URL`, so a
  misconfigured deploy fails loudly rather than running insecurely.
- Authorization is in-app (see CLAUDE.md → Auth & authorization): a user needs a Django `User`
  with the right email, and non-superusers also need a `SchoolProfile`. There is no auto-signup.
