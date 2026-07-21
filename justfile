default:
    @just --list

hooks-install:
    @echo "Installing pre-commit hook..."
    @mkdir -p .git/hooks
    @cp bin/pre-commit.sh .git/hooks/pre-commit
    @chmod +x .git/hooks/pre-commit
    @echo "Pre-commit hook installed."

lint:
    uv run ruff check src/ tests/
    uv run ruff format --check src/ tests/

test:
    uv run pytest tests/ -v
