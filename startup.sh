#!/usr/bin/env bash
set -euo pipefail

# Oryx starts this script with CWD already set to the extracted app path.
APP_ROOT="$(pwd)"

# 1) Activate the virtualenv created by Oryx (relative path)
VENV_PATH="$APP_ROOT/antenv"
if [ -d "$VENV_PATH" ]; then
  echo "Activating virtualenv: $VENV_PATH"
  # shellcheck disable=SC1091
  source "$VENV_PATH/bin/activate"
else
  echo "Warning: virtualenv not found at $VENV_PATH (Oryx may have set PYTHONPATH already). Continuing..."
fi

# 2) Python path + settings
export PYTHONPATH="$APP_ROOT:${PYTHONPATH:-}"
: "${DJANGO_SETTINGS_MODULE:=lmpm.settings}"
WSGI_PATH="${WSGI_PATH:-lmpm.wsgi:application}"

# 3) Migrations + static. These two MUST succeed: booting without them gives a
#    running app that silently fails every save.
#
#    These used to be `|| echo "... (continuing)"`, which defeated the `set -e`
#    at the top of this file on purpose. The result was a deploy that went green
#    in GitHub Actions (which reports on package upload, not on boot) while the
#    app ran against a stale schema — every write raising UndefinedColumn, users
#    losing whatever they had typed to a bare 500 page, and one line in a
#    container log as the only evidence. A container that refuses to start is
#    visible in Azure within a minute; one serving a broken schema is not.
#
#    collectstatic is the more dangerous of the two: DEBUG=0 selects
#    CompressedManifestStaticFilesStorage and staticfiles/ is gitignored, so a
#    missing staticfiles.json makes every {% static %} tag raise and takes the
#    whole site down — while still answering the port, so Azure's warm-up probe
#    passes and the deploy looks healthy.
if [ -f "$APP_ROOT/manage.py" ]; then
  echo "Running migrations..."
  python manage.py migrate --noinput

  echo "Collecting static..."
  python manage.py collectstatic --noinput

  # Seeds are genuinely optional — they only top up reference data (schools,
  # branding) and the app is fully usable without them, so a transient failure
  # here should not hold up a boot that is otherwise sound.
  echo "Seeding base data..."
  python manage.py seed_schools  || echo "seed_schools failed (continuing)."
  python manage.py seed_branding || echo "seed_branding failed (continuing)."
fi

# 4) Start gunicorn. Production is Postgres, so multiple workers are safe;
#    override via the WEB_CONCURRENCY app setting (use 1 if ever run on SQLite).
echo "Starting gunicorn..."
exec gunicorn \
  --chdir "$APP_ROOT" \
  --bind "0.0.0.0:${PORT:-8000}" \
  --workers "${WEB_CONCURRENCY:-2}" \
  --timeout 600 \
  --env "DJANGO_SETTINGS_MODULE=$DJANGO_SETTINGS_MODULE" \
  "$WSGI_PATH"
