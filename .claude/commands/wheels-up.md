# Wheels Up — Pre-Deploy

You are running Wheels Up: the pre-deploy check before pushing to Azure.

Work through each stage in order. Stop and surface any failures before continuing.

---

## Stage 1 — Final tidy with Les

Spawn Les on the files changed since the last commit. Ask Les to flag anything obviously messy — oversized views, duplicated logic, or code that will be painful to debug in production. Fix any High findings before moving on.

---

## Stage 2 — Deployment checklist

Check each item and report pass/fail:

1. **requirements.txt** — no Windows-only packages (e.g. `pywin32`, `winreg`) that would break the Linux Azure build
2. **SECRET_KEY** — confirm `settings.py` reads it from an env var, not hardcoded
3. **DEBUG** — confirm it defaults to `False` when no env var is set
4. **Migrations** — run `python manage.py migrate --check` to confirm no unapplied migrations
5. **Static files** — confirm WhiteNoise is in `INSTALLED_APPS` and `MIDDLEWARE`
6. **ALLOWED_HOSTS** — confirm it is not empty and handles the `WEBSITE_HOSTNAME` env var
7. **Open TODOs** — grep `lmpm/` and the feature apps for any `TODO`, `FIXME`, or `HACK` comments

---

## Stage 3 — Commit message

When the checklist passes, confirm with the user that they want to commit, then draft a clean commit message following the project's commit style: present tense, concise, focused on the why rather than the what.

---

## Finish

Final verdict: **Ready to deploy** or **Fix before deploying**, with a list of any failures.
