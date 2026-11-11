#!/bin/sh
# Back up, pull the new version, rebuild, restart. Database changes apply when the app starts.
# Stops on the first failure, so nothing is rebuilt if the backup fails.
set -e
export PATH="$HOME/bin:$PATH"
cd "$(dirname "$0")"
docker compose exec -T -u dnb app python manage.py backup_now
git pull --no-rebase --no-edit
docker compose build
docker compose up -d
docker compose ps
