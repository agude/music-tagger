# music-tagger

Fix Navidrome album splits caused by inconsistent `MUSICBRAINZ_ALBUMID` tags.
This is an interactive tool — Claude Code is the operator, not the end user.

## How it works

Two CLI subcommands, driven by Claude Code in conversation:

1. `uv run music-tagger candidates <album-dir>` — reads audio file tags and
   durations, searches MusicBrainz for CD releases, prints everything.

2. `uv run music-tagger tag <album-dir> --release-id <uuid> [--dry-run]` —
   fetches the chosen release from MusicBrainz, computes a field-by-field
   diff against current tags, and writes if not `--dry-run`.

Claude Code reads the `candidates` output, picks the correct release by
comparing track durations (ground truth from audio stream) against MB
listings, then runs `tag` with the chosen UUID.

## Matching rules

- Track count must match (except partial albums / singles).
- Track durations are ground truth. A good match has most tracks within 2s.
- Existing metadata tags may be wrong — use them as search seeds and
  tiebreakers, not as matching criteria.
- For multi-disc releases, the tool reads disc numbers from existing tags.

## Architecture

```
src/music_tagger/
├── tags.py          # Read/write FLAC + MP3 tags via mutagen
├── musicbrainz.py   # MB API client with 1 req/sec rate limiting
├── tagger.py        # Orchestration: search → diff → write
└── cli.py           # Argparse entry point (candidates / tag)
```

- `tags.py` provides a unified interface over FLAC Vorbis Comments and MP3
  ID3v2 frames. Field names follow Navidrome's `mappings.yaml` conventions.
- `musicbrainz.py` searches and fetches releases. Rate-limited to 1 req/sec.
- `tagger.py` builds the target tags from an MB release and computes diffs.
  Handles multi-disc albums by parsing `discnumber` tags.
- `cli.py` wires the subcommands together.

## Tag mapping details

- `date` (FLAC `DATE`, MP3 `TDRC`) is the recording date — never overwritten
  by the MB release date.
- `releasedate` (FLAC `RELEASEDATE`, MP3 `TDRL`) gets the MB release's date.
- `releasestatus` is compared case-insensitively (`official` == `Official`).
- Unicode from MusicBrainz (e.g. U+2010 HYPHEN in titles) is accepted as
  canonical.

## Safety

**Always copy test files to /tmp before running against them.** Never run
destructive commands against the live library at `/mnt/synology/media/music`.

## Running tests

```
uv run pytest tests/ -v
```

Test fixtures are small FLAC/MP3 files in `tests/fixtures/` with known tags.
Tests copy them to `tmp_path` so mutations don't affect the fixtures.
