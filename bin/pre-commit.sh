#!/bin/bash
#
# Pre-commit hook: auto-formats and lints staged Python files with ruff.

STAGED_PY=$(git diff --cached --name-only --diff-filter=ACM | grep -E '\.py$' || true)

if [ -z "$STAGED_PY" ]; then
    exit 0
fi

uv run ruff format $STAGED_PY
uv run ruff check --fix $STAGED_PY

git add $STAGED_PY
exit 0
