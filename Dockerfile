# DNB Books: one image for both the web app and the scheduler (see docker-compose.yml).
FROM python:3.12-slim-bookworm

# rclone: off-site backups. pango: PDF rendering (WeasyPrint, Phase 3).
RUN apt-get update \
 && apt-get install -y --no-install-recommends rclone ca-certificates libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv==0.11.32

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    DNB_DB_PATH=/data/dnb.sqlite3 \
    DNB_BACKUP_DIR=/data/backups \
    RCLONE_CONFIG=/data/rclone/rclone.conf

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
# collectstatic needs a key to import settings; this throwaway value is never used at runtime.
RUN DNB_SECRET_KEY=build-only-not-secret python manage.py collectstatic --noinput -v0

# The entrypoint starts as root only to fix ownership of the mounted ./data folder, then drops to this user.
RUN useradd --uid 1000 --create-home dnb && mkdir -p /data && chown dnb:dnb /data
VOLUME /data
EXPOSE 8000

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["python", "manage.py", "serve", "--host", "0.0.0.0", "--port", "8000"]
