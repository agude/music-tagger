# music-tagger

Fix Navidrome album splits caused by inconsistent `MUSICBRAINZ_ALBUMID` tags.
This is an interactive tool — Claude Code is the operator, not the end user.

## How it works

Key CLI subcommands, driven by Claude Code in conversation:

1. `uv run music-tagger scan <library-root> -o checklist.md` — finds albums
   with split or missing MusicBrainz IDs, writes a markdown checklist.

2. `uv run music-tagger candidates <album-dir>` — reads audio file tags and
   durations, searches MusicBrainz for CD releases, prints everything.

3. `uv run music-tagger tag <album-dir> --release-id <uuid> [--dry-run] [--log changes.log]`
   — fetches the chosen release from MusicBrainz, computes a field-by-field
   diff against current tags, and writes if not `--dry-run`. Appends all
   changes to the log file for auditing.

4. `uv run music-tagger genre <album-dir> [genre] [--dry-run] [--log changes.log]`
   — sets the genre meta-grouping tag on all tracks. Omit genre to show the
   current value. These are broad browsing categories (e.g. "Retro Rock",
   "Classical", "Rap", "Broadway"), not musicological genres.

5. `uv run music-tagger rename <album-dir> [--dry-run]` — renames audio
   files to `NN - Title.ext` based on their tracknumber and title tags.
   Sanitizes filesystem-unsafe characters. Skips files missing title or
   tracknumber tags.

6. `uv run music-tagger rip <output-dir> [--device /dev/cdrom] [--no-discid]`
   — rips a CD to FLAC using cdparanoia + flac. Reads the disc ID via
   libdiscid for MusicBrainz lookup. Sets tracknumber tags from position
   so `tag` can match tracks. Requires system packages: `cdparanoia`,
   `flac`, `libdiscid-dev`; Python package `discid` (optional, for disc
   ID lookup).

## Library workflow

Processing the library is staged album by album across sessions:

1. Run `scan` once to generate the checklist.
2. Each session: read the checklist, find the next `- [ ]` entry.
3. Per album:
   a. `candidates <dir>` — find MB release candidates, pick one.
   b. `tag <dir> --release-id <uuid> --dry-run` — review the diff.
   c. `tag <dir> --release-id <uuid> --log changes.log` — apply tags.
   d. `art <dir> --release-id <uuid>` — fetch cover art.
   e. `genre <dir> <group>` — set the meta-grouping tag.
   f. `rename <dir>` — rename files to `NN - Title.ext` from tags.
   g. Mark the checklist entry `- [x]`.
4. After a batch: `nd rescan` to trigger Navidrome library scan.
5. Repeat until done. Re-scan if needed to catch stragglers.

**File locations:**
- Checklist: `~/Projects/music-tagger/checklist.md`
- Change log: `~/Projects/music-tagger/changes.log`
- Library root: `/mnt/synology/media/music`

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
├── discid.py        # CUE sheet parser, disc ID computation
├── placement.py     # Library path computation + verified copy
├── coverart.py      # Cover Art Archive client
├── navidrome.py     # Navidrome / Subsonic API client
├── ripper.py        # CD ripping via cdparanoia + FLAC encoding
└── cli.py           # Argparse entry point (all subcommands)
```

- `tags.py` provides a unified interface over FLAC Vorbis Comments and MP3
  ID3v2 frames. Field names follow Navidrome's `mappings.yaml` conventions.
- `musicbrainz.py` searches and fetches releases. Rate-limited to 1 req/sec.
- `tagger.py` builds the target tags from an MB release and computes diffs.
  Handles multi-disc albums by parsing `discnumber` tags.
- `discid.py` parses CUE sheets and computes MusicBrainz disc IDs.
- `placement.py` computes library destination paths and copies files with
  SHA-256 verification.
- `coverart.py` fetches front cover images from the Cover Art Archive.
- `navidrome.py` Subsonic API client for library scans (and future ratings).
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
