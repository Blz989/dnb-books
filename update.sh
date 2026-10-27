#!/bin/sh
# Back up, pull the new version, rebuild, restart. Database changes apply when the app starts.
# Stops on the first failure, so nothing is rebuilt if the backup fails.
set -e
cd "$(dirname "$0")"
docker compose exec -T -u dnb app python manage.py backup_now
git pull
docker compose build
docker compose up -d
docker compose ps
