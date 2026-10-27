@echo off
cd /d "%~dp0"
uv run python manage.py serve %*
