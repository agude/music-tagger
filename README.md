# music-tagger

Fix Navidrome album splits caused by inconsistent `MUSICBRAINZ_ALBUMID` tags.

Navidrome groups tracks into albums by their MusicBrainz album ID. When files
in the same album have different IDs (or none at all), Navidrome splits them
into separate albums. This tool searches MusicBrainz for the correct release,
shows a field-by-field diff, and writes consistent tags across all tracks.

## How it works

music-tagger is an interactive tool designed to be driven by
[Claude Code](https://github.com/anthropics/claude-code). It provides three
CLI subcommands:

```
music-tagger scan <library-root> -o checklist.md
music-tagger candidates <album-dir>
music-tagger tag <album-dir> --release-id <uuid> [--dry-run] [--log changes.log]
```

**scan** walks the library and generates a markdown checklist of albums with
split or missing MusicBrainz IDs.

**candidates** reads audio file tags and durations, searches MusicBrainz for
matching CD releases, and prints the results with track listings and
durations.

**tag** fetches a chosen release from MusicBrainz, computes a field-by-field
diff against the current tags, and writes changes. Use `--dry-run` to preview
without writing. Use `--log` to append changes to a file for auditing.

## Matching approach

- Track durations (from the audio stream) are ground truth.
- Track count must match between files and the MusicBrainz release.
- Existing metadata tags may be wrong. They are used as search seeds, not as
  matching criteria.
- MB disambiguation suffixes like `(single edit)`, `(remix)`, and `(live)` are
  ignored when comparing titles.
- Per-track artist IDs are preserved on compilation and concert albums.
- The recording date (`DATE` / `TDRC`) is never overwritten. The MB release
  date goes to `RELEASEDATE` / `TDRL`.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)

## Installation

```
git clone <repo-url>
cd music-tagger
uv sync
```

## Usage

```bash
# Generate a checklist of albums needing attention
uv run music-tagger scan /path/to/music -o checklist.md

# Show MusicBrainz candidates for an album
uv run music-tagger candidates "/path/to/music/Artist/Album (Year)"

# Preview tag changes
uv run music-tagger tag "/path/to/music/Artist/Album (Year)" \
    --release-id <uuid> --dry-run

# Apply tag changes with audit log
uv run music-tagger tag "/path/to/music/Artist/Album (Year)" \
    --release-id <uuid> --log changes.log
```

## Supported formats

- FLAC (Vorbis Comments)
- MP3 (ID3v2)

Tag field names follow
[Navidrome's mapping conventions](https://github.com/navidrome/navidrome/blob/master/resources/mappings.yaml).

## Running tests

```
uv run pytest tests/ -v
```
