# music-tagger

Fix Navidrome album splits caused by inconsistent `MUSICBRAINZ_ALBUMID` tags.

Navidrome groups tracks into albums by their MusicBrainz album ID. When files
in the same album have different IDs (or none at all), Navidrome splits them
into separate albums. This tool searches MusicBrainz for the correct release,
shows a field-by-field diff, and writes consistent tags across all tracks.

## How it works

music-tagger is an interactive tool designed to be driven by
[Claude Code](https://github.com/anthropics/claude-code). It is a set of CLI
subcommands with flags, exit codes, and gates. AGENTS.md has the full list. The
core flow is three of them:

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
without writing. Changes are appended to `$MUSIC_TAGGER_LOG`, or
`~/Projects/music-tagger/changes.log` when that is unset, for auditing. Use
`--log PATH` to write to another file, or `--no-log` to skip the log.

## Matching approach

Use `--keep-existing-credits` on `diff`, `tag`, `apply`, or `batch-dry-run` to
accept MusicBrainz personnel additions while retaining existing contributor
names and IDs that it omits. This is opt-in; intentional removal of a bogus
credit remains an explicit `edit-tags` operation. Performers are merged per
person, so an existing performer gains missing roles instead of being listed
twice. A role repeated within one performer is always written once.

- Track durations (from the audio stream) are ground truth.
- Track count must match between files and the MusicBrainz release.
- Existing metadata tags may be wrong. They are used as search seeds, not as
  matching criteria.
- MB disambiguation suffixes like `(single edit)`, `(remix)`, and `(live)` are
  ignored when comparing titles.
- Each track's artist credits and MusicBrainz artist IDs come from the track's
  own credit when MusicBrainz has one, so compilation and concert albums keep
  each track's artist.
- The recording date (`DATE` / `TDRC`) is never overwritten. The MB release
  date goes to `RELEASEDATE` / `TDRL`.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- System packages for `rip` and `replaygain`: `whipper`, `libdiscid-dev`, and
  `rsgain`. AGENTS.md lists them under "Environment setup".

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

## Ratings and play counts

Navidrome is the source of truth. Export a snapshot, then write it into the
audio files:

```bash
just dump-stats               # nd stats: writes stats.json, keeps stats.json.previous
just sync-stats --dry-run     # preview changed fields and the rewrite cost
just sync-stats               # write ratings and play counts into the files
```

Favorites are never written to files. A snapshot rating of 0 clears the file's
rating only with `--clear`. A play count lower than the file's is written only
with `--allow-decrease`. Both withheld changes are reported. Only the
`Music Library` Navidrome library is written by default; `--library all`
includes others such as `Podcasts`. The tag mapping,
policy, and recovery limits are in AGENTS.md under "Ratings and play counts".

## Lyrics

Look up LRCLIB lyrics for an album and save them as `.lrc` (synchronized) or
`.txt` (plain) sidecars beside each track. With no mode, `lyrics` only
inspects: it makes no network calls and writes nothing.

```bash
uv run music-tagger lyrics "/path/to/Artist/Album (Year)"                  # inspect
uv run music-tagger lyrics "/path/to/Artist/Album (Year)" --lookup         # candidates
uv run music-tagger lyrics "/path/to/Artist/Album (Year)" --fetch --dry-run
uv run music-tagger lyrics "/path/to/Artist/Album (Year)" --fetch
```

Existing lyrics are kept unless `--replace` is given. The selection rules,
outcomes, exit codes, and the limits of Navidrome's display are in AGENTS.md
under "Lyrics".

## Supported formats

- FLAC (Vorbis Comments): read and written.
- MP3 (ID3v2): read and written.
- M4A: read. The writes are limited to genre, ratings and play counts, and
  embedded lyrics. `tag` does not write M4A tags. See AGENTS.md.

Tag field names follow
[Navidrome's mapping conventions](https://github.com/navidrome/navidrome/blob/master/resources/mappings.yaml).

## Running tests

```
just check
```

`just check` runs lint, the type check, and the test suite with its coverage
gate. For the tests alone, run `uv run pytest tests/ -v`.
