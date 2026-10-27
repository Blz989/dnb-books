@echo off
cd /d "%~dp0"
uv run python manage.py help backup_now >nul 2>&1
if errorlevel 1 (
  echo Notice: backup_now is not available yet, skipping backup.
) else (
  uv run python manage.py backup_now
  if errorlevel 1 exit /b 1
)
git pull
if errorlevel 1 exit /b 1
uv sync
if errorlevel 1 exit /b 1
uv run python manage.py migrate
if errorlevel 1 exit /b 1
uv run python manage.py collectstatic --noinput
if errorlevel 1 exit /b 1
