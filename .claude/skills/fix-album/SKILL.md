---
name: fix-album
description: >
  Fix tags on an existing library album: identify the MusicBrainz release,
  apply tags, embed cover art, set genre, rename files, and compute
  ReplayGain. Use when processing albums from the scan checklist.
allowed-tools: "Bash Read Edit"
---

# Fix Album

Process a single album already in the music library. The album directory
exists at its current library path — files are edited in-place, no copy
step.

Collect all inputs before starting, then execute each step without pausing
for user input (unless a safety gate fires).

## Inputs

Collect these from the user (or read from the checklist entry) before
starting:

| Input | Required | Default |
|---|---|---|
| Album directory | yes | from checklist |
| Genre | yes | — |
| Release ID | no | found via candidates |

**Validate genre** against `${CLAUDE_SKILL_DIR}/references/genre-list.md`
before proceeding. If the value is not on the list, stop and tell the user.

Multiple genres are allowed (space-separated in the genre command). Validate
each one individually.

## Step 1: Identify release

**If the user provided a release ID:** skip to Step 2.

**Otherwise:**

```bash
uv run music-tagger candidates "<album-dir>"
```

Existing tags provide the search seeds automatically. If results are poor,
retry with explicit `--artist` and `--album` flags.

### Selection logic

1. Count the tracks in the album directory.
2. Filter candidates to releases with a matching track count.
3. Compare track durations — a good match has most tracks within 2 seconds.
4. If existing files already have a `musicbrainz_albumid` tag, prefer that
   release if it appears in the filtered results.
5. Among remaining candidates, prefer CD media, then US releases.
6. Break ties by earliest release date.

Present the chosen release to the user for confirmation. If multiple strong
candidates remain, show the top 2-3 and ask.

**MusicBrainz 503 errors:** retry once after 5 seconds.

## Step 2: Tag

### Dry run

```bash
uv run music-tagger tag "<album-dir>" --release-id <uuid> --dry-run
```

### Safety gate

Inspect the diff output:

- **Auto-proceed** if all changes either fill empty fields or update
  `performer` / `musicbrainz_albumid` / `musicbrainz_artistid` /
  `musicbrainz_albumartistid` / `musicbrainz_trackid` /
  `releasestatus` / `releasetype` / `releasedate` / `barcode` /
  `media` / `catalognumber` / `label` / `asin` fields.
- **Stop and show the diff** if any change overwrites a non-empty `title`,
  `artist`, `album`, `albumartist`, or `date` field with a different value.
  Wait for user confirmation before proceeding.

### Apply

```bash
uv run music-tagger tag "<album-dir>" --release-id <uuid> --log ~/Projects/music-tagger/changes.log
```

## Step 3: Cover art

```bash
uv run music-tagger art "<album-dir>" --release-id <uuid> --full --force --embed
```

## Step 4: Genre

```bash
uv run music-tagger genre "<album-dir>" <genre> --log ~/Projects/music-tagger/changes.log
```

If multiple genres, pass them all:

```bash
uv run music-tagger genre "<album-dir>" "Genre One" "Genre Two" --log ~/Projects/music-tagger/changes.log
```

## Step 5: Rename

```bash
uv run music-tagger rename "<album-dir>" --log ~/Projects/music-tagger/changes.log
```

## Step 6: ReplayGain

```bash
uv run music-tagger replaygain "<album-dir>"
```

## Step 7: Checklist

If `~/Projects/music-tagger/checklist.md` contains an entry for this album,
mark it `- [x]`.
