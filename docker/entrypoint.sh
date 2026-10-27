#!/bin/sh
# Prepare /data, apply database changes, then run the given command (web server or scheduler) as user dnb.
# Starts as root only so a ./data folder created by Docker on the host becomes writable by dnb.
set -e
mkdir -p /data/backups /data/rclone
chown -R dnb:dnb /data
as_dnb() { setpriv --reuid=dnb --regid=dnb --init-groups "$@"; }
if [ "${DNB_SKIP_MIGRATE:-0}" != "1" ]; then
  as_dnb python manage.py migrate --noinput
  as_dnb python manage.py setup_roles
fi
exec setpriv --reuid=dnb --regid=dnb --init-groups "$@"
