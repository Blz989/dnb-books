#!/bin/sh
cd "$(dirname "$0")"
exec uv run python manage.py serve "$@"
