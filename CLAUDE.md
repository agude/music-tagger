# music-tagger

Fix Navidrome album splits caused by inconsistent `MUSICBRAINZ_ALBUMID` tags.
This is an interactive tool — Claude Code is the operator, not the end user.

## How it works

Three CLI subcommands, driven by Claude Code in conversation:

1. `uv run music-tagger scan <library-root> -o checklist.md` — finds albums
   with split or missing MusicBrainz IDs, writes a markdown checklist.

2. `uv run music-tagger candidates <album-dir>` — reads audio file tags and
   durations, searches MusicBrainz for CD releases, prints everything.

3. `uv run music-tagger tag <album-dir> --release-id <uuid> [--dry-run] [--log changes.log]`
   — fetches the chosen release from MusicBrainz, computes a field-by-field
   diff against current tags, and writes if not `--dry-run`. Appends all
   changes to the log file for auditing.

## Library workflow

Processing the library is staged album by album across sessions:

1. Run `scan` once to generate `checklist.md`.
2. Each session: read `checklist.md`, find the next unchecked album.
3. Run `candidates` for that album, pick the release, run `tag --dry-run`,
   review, then `tag --log changes.log`.
4. Mark the album `[x]` in the checklist.
5. Repeat until done. Re-scan if needed to catch stragglers.

The checklist and log file persist across sessions. The checklist tracks
what's left; the log tracks what was changed for auditing.

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
└── cli.py           # Argparse entry point (scan / candidates / tag)
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

- `scan` and `candidates` are read-only. Safe to run directly against the
  live library.
- `tag --dry-run` is read-only. Safe to run directly against the live library.
- `tag` (without `--dry-run`) writes metadata into files. It does not move,
  rename, or delete files. Use `--dry-run` first, review the diff, then run
  without it.

## Running tests

```
uv run pytest tests/ -v
```

Test fixtures are small FLAC/MP3 files in `tests/fixtures/` with known tags.
Tests copy them to `tmp_path` so mutations don't affect the fixtures.
