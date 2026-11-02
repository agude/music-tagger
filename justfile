set dotenv-load := true

default:
    @just --list

hooks-install:
    @echo "Installing pre-commit hook..."
    @mkdir -p .git/hooks
    @cp bin/pre-commit.sh .git/hooks/pre-commit
    @chmod +x .git/hooks/pre-commit
    @echo "Pre-commit hook installed."

# Install dependencies
sync:
    uv sync --dev

# All read-only static checks
lint:
    uv run ruff check src/ tests/
    uv run ruff format --check src/ tests/

# Apply formatting and safe lint fixes
format:
    uv run ruff format src/ tests/
    uv run ruff check --fix src/ tests/

# Type check
type-check:
    uv run mypy src/music_tagger/

# Run tests
test *args:
    uv run pytest tests/ -v {{ args }}

# Everything CI runs
check: lint type-check test

# Dump rated/starred songs from Navidrome into ratings.json
dump-ratings:
    uv run music-tagger nd ratings

# Write rating tags from ratings.json into audio files
sync-ratings *args:
    uv run music-tagger write-ratings {{ args }}

# Export ratings, play counts, and stars from Navidrome into stats.json
dump-stats *args:
    uv run music-tagger nd stats {{ args }}

# Write ratings and play counts from stats.json into audio files (preview with --dry-run)
sync-stats *args:
    uv run music-tagger write-stats {{ args }}
