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

# 3) Migrations + seed data + static. Each step is idempotent and tolerant of
#    failure so a transient error doesn't block the app from booting.
if [ -f "$APP_ROOT/manage.py" ]; then
  echo "Running migrations..."
  python manage.py migrate --noinput || echo "Migrations failed (continuing)."

  echo "Seeding base data..."
  python manage.py seed_schools  || echo "seed_schools failed (continuing)."
  python manage.py seed_branding || echo "seed_branding failed (continuing)."

  echo "Collecting static..."
  python manage.py collectstatic --noinput || echo "Collectstatic failed (continuing)."
fi

# 4) Start gunicorn. --workers=1 is safe for SQLite; bump to 3 on Postgres.
echo "Starting gunicorn..."
exec gunicorn \
  --chdir "$APP_ROOT" \
  --bind "0.0.0.0:${PORT:-8000}" \
  --timeout 600 \
  --env "DJANGO_SETTINGS_MODULE=$DJANGO_SETTINGS_MODULE" \
  "$WSGI_PATH"
