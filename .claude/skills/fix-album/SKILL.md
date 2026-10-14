---
name: fix-album
description: >
  Fix tags on an existing library album: identify the MusicBrainz release,
  apply tags, embed cover art, set genre, rename files, and compute
  ReplayGain. Use when processing albums from the scan checklist.
allowed-tools: "Bash Read Edit AskUserQuestion"
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
| Genre | no | — |
| Release ID | no | found via candidates |

**Genre selection:** If the user provided genres, validate each one against
`${CLAUDE_SKILL_DIR}/references/genre-list.md`. If they did not, read the
genre list, pick the 2–4 best fits for the artist and album, and present
them as a multi-select `AskUserQuestion` for confirmation. The user can
deselect bad picks or add their own via "Other."

Multiple genres are allowed — pass each as its own quoted argument, e.g.
`genre "<dir>" "Pop Rock" "Y2K Rock"`. They are stored as separate tag
values; never join them into one string.

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

The dry run ends with a gate verdict, and its exit code matches:

| Exit | Verdict | Action |
| --- | --- | --- |
| 0 | `Gate: auto` | Apply. |
| 3 | `Gate: review` | Show the user the listed overwrites. After they confirm, apply with `--approve-overwrites`. If they reject them, the release is probably wrong: return to release selection. |
| 4 | `Gate: blocked` | Unresolved ISRC conflicts. See below. |

The gate passes changes that fill empty fields, change only IDs or release
metadata, or change only case, Unicode punctuation, or whitespace. It stops
only for real changes to a non-empty `title`, `artist`, `album`,
`albumartist`, or `date`.

`Warning: ... unverified` lines on stderr do not block writes. List each
unverified code in the final summary.

**Exit 4 (`Gate: blocked`):** unresolved ISRC conflicts. The source of an
existing library code is unknown: it may have been read from the CD or set
by another tagger. Show the user a table of each conflicting file, its code,
and the MusicBrainz codes. Then ask with `AskUserQuestion`:

- **Keep file codes (Recommended):** absence from MusicBrainz does not
  prove a code is wrong. Add `--isrc keep`. Kept codes are locked, so later
  `tag` runs do not raise them again.
- **Use MusicBrainz codes:** add `--isrc musicbrainz`. It resolves tracks
  where MusicBrainz lists exactly one code. For a track with several, ask
  which code to use and add `--isrc-code [DISC-]TRACK=CODE`.

Rerun the dry run with the chosen flags and handle its exit code.

### Apply

Run this step only when the final dry run exited 0, or exited 3 and the user
approved. Pass the same `--isrc`, `--isrc-code`, and `--approve-overwrites`
flags as that dry run.

```bash
uv run music-tagger tag "<album-dir>" --release-id <uuid>
```

## Step 3: Cover art

```bash
uv run music-tagger art "<album-dir>" --release-id <uuid> --full --force --embed
```

## Step 4: Genre

```bash
uv run music-tagger genre "<album-dir>" <genre>
```

If multiple genres, pass them all:

```bash
uv run music-tagger genre "<album-dir>" "Genre One" "Genre Two"
```

## Step 5: Rename

```bash
uv run music-tagger rename "<album-dir>"
```

## Step 6: ReplayGain

```bash
uv run music-tagger replaygain "<album-dir>"
```

## Step 7: Checklist

If `~/Projects/music-tagger/checklist.md` contains an entry for this album,
mark it `- [x]`.
