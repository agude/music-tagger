---
name: rip-album
description: >
  Rip a CD to FLAC via whipper, tag from MusicBrainz, embed cover art, set
  genre, rename, compute ReplayGain, and copy to the music library. Use when
  the user asks to rip a CD or process a new album from disc.
allowed-tools: "Bash Read Edit"
---

# Rip Album

Rip a single CD and process it into the music library. Collect all inputs
before starting, then execute each step without pausing for user input
(unless a safety gate fires).

## Inputs

Collect these from the user before starting:

| Input | Required | Example |
|---|---|---|
| Artist | yes | `Matchbox 20` |
| Album title | yes | `Yourself or Someone Like You` |
| Barcode | no | `075679272126` |
| Genre | yes | `Y2K Rock` |
| Release ID | no | MB release UUID if already known |
| Device | no | `/dev/cdrom` (default) |

**Validate genre** against `${CLAUDE_SKILL_DIR}/references/genre-list.md`
before proceeding. If the value is not on the list, stop and tell the user.

Multiple genres are allowed (space-separated in the genre command). Validate
each one individually.

## Step 1: Rip

Create a temp staging directory:

```bash
STAGING=$(mktemp -d /tmp/rip-XXXXXX)
```

Rip the CD (this is slow — run in background and wait for completion):

```bash
uv run music-tagger rip "$STAGING" --device <device>
```

If the user provided a `--release-id`, pass it through. If the CD is not in
MusicBrainz, use `--unknown`.

**Gate:** If the rip exits non-zero, stop and report the error. Do not
proceed to tagging.

Note AccurateRip mismatches in the output but continue — some CDs are not
in the AR database.

## Step 2: Identify release

**If the user provided a release ID:** skip this step entirely, use that ID.

**Otherwise:**

```bash
uv run music-tagger candidates "$STAGING" --artist '<artist>' --album '<album>'
```

If a barcode was provided, add `--barcode '<barcode>'` to narrow results.

### Selection logic

1. Count the tracks in the staging directory.
2. Filter candidates to CD releases with a matching track count.
3. If whipper tagged the files with an MB release ID (check the file tags),
   prefer that release if it appears in the filtered results.
4. Among remaining candidates, prefer US releases.
5. Break ties by earliest release date.

Present the chosen release to the user for confirmation. If multiple strong
candidates remain, show the top 2-3 and ask.

**MusicBrainz 503 errors:** retry once after 5 seconds.

## Step 3: Tag

### Dry run

```bash
uv run music-tagger tag "$STAGING" --release-id <uuid> --dry-run
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
uv run music-tagger tag "$STAGING" --release-id <uuid> --log ~/Projects/music-tagger/changes.log
```

## Step 4: Cover art

```bash
uv run music-tagger art "$STAGING" --release-id <uuid> --full --force --embed
```

## Step 5: Genre

```bash
uv run music-tagger genre "$STAGING" <genre> --log ~/Projects/music-tagger/changes.log
```

If multiple genres, pass them all:

```bash
uv run music-tagger genre "$STAGING" "Genre One" "Genre Two" --log ~/Projects/music-tagger/changes.log
```

## Step 6: Rename

```bash
uv run music-tagger rename "$STAGING" --log ~/Projects/music-tagger/changes.log
```

## Step 7: ReplayGain

```bash
uv run music-tagger replaygain "$STAGING"
```

## Step 8: Copy to library

Build evidence, compute placement, then copy:

```bash
uv run music-tagger read "$STAGING" -o "$STAGING/evidence.json"
uv run music-tagger path-for --evidence "$STAGING/evidence.json" -o "$STAGING/plan.json"
uv run music-tagger copy --plan "$STAGING/plan.json" --dry-run
```

Review the dry-run output. Confirm the artist/album directory names are
correct and the destination does not collide with an existing album (unless
replacing it intentionally). Then apply:

```bash
uv run music-tagger copy --plan "$STAGING/plan.json" --log ~/Projects/music-tagger/changes.log
```

## Step 9: Cleanup

Remove the staging directory:

```bash
rm -rf "$STAGING"
```

## Step 10: Checklist

If `~/Projects/music-tagger/checklist.md` exists and contains an entry for
this album, mark it `- [x]`.
