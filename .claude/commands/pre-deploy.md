Run a pre-deployment checklist for this Django + Azure project.

Check the following and report a clear pass/fail for each:

1. **requirements.txt** — scan for any Windows-only packages (e.g. pywin32, winreg) that would break the Linux Azure build
2. **SECRET_KEY** — confirm settings.py reads it from an env var, not hardcoded
3. **DEBUG** — confirm it defaults to False (not True) when no env var is set
4. **Migrations** — run `python manage.py migrate --check` to confirm no unapplied migrations
5. **Static files** — confirm whitenoise is in INSTALLED_APPS and MIDDLEWARE
6. **ALLOWED_HOSTS** — confirm it's not empty and handles the WEBSITE_HOSTNAME env var
7. **Open TODOs** — grep for any TODO/FIXME/HACK comments in the myproject/ directory

Finish with a one-line verdict: **Ready to deploy** or **Fix before deploying**, listing any failures.
