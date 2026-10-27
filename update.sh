#!/bin/sh
# Back up, pull the new version, apply database changes. Stops on first failure.
set -e
cd "$(dirname "$0")"
if uv run python manage.py help backup_now >/dev/null 2>&1; then
  uv run python manage.py backup_now
else
  echo "Notice: backup_now is not available yet, skipping backup."
fi
git pull
uv sync
uv run python manage.py migrate
uv run python manage.py collectstatic --noinput
