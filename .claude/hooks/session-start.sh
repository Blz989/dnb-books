#!/bin/bash
# Installs Python dependencies for Claude Code cloud sessions.
# No-op until pyproject.toml exists (Phase 1, Task 1 creates it).
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-.}"

if [ ! -f pyproject.toml ]; then
  echo "session-start: no pyproject.toml yet, skipping dependency install"
  exit 0
fi

if ! command -v uv >/dev/null 2>&1; then
  pip install --quiet uv
fi

uv sync
mkdir -p data
