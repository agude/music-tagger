---
name: fix-album
description: >
  Fixes tags on an existing library album: identifies the MusicBrainz
  release, applies tags, embeds cover art, sets genre, renames files, and
  computes ReplayGain. Use when processing albums from the scan checklist or
  when the user asks to fix, retag, or clean up an album in the library.
allowed-tools: "Bash Read AskUserQuestion"
---

# Fix Album

Process one album in place in the library. The CLI performs every
mechanical step and writes `changes.log`. The operator makes four
decisions: the release, the cover, the genres, and approval of anything a
gate stops on. Do not edit tags, JSON, or the checklist by hand.

## 1. Pick the album

If the user named an album directory, use it. Otherwise:

```bash
uv run music-tagger checklist next
```

`Next:` is the album directory. `Checklist complete.` means there is
nothing left; stop.

## 2. Choose the release

Skip this step if the user gave a release ID.

```bash
uv run music-tagger candidates "<album-dir>"
```

The table lists every candidate, ranked: exact track count, then `partial`,
then mismatched counts; within each group by tracks within 2s, `on-files`,
CD, US, and earliest date. The ranking is advice. Decide:

- **One clear row** (track counts equal, nearly every track within 2s, the
  next rows clearly worse): use it. Confirm with the user only when it is
  not `on-files`.
- **Several rows tie on durations** (editions that differ only in country,
  label, or barcode): use the `on-files` row without asking. Ask with
  `AskUserQuestion`, showing the tied rows, only when none of them is
  `on-files`.
- **Only `partial` rows fit** (the files are a subset of the release): use
  the best one when every local track is within 2s.
- **`No candidates found.`, or no row fits:** retry once with `--artist` and
  `--album` (alternate spelling, leading "The", soundtrack subtitle). If
  nothing fits, do not guess. Tell the user the search terms tried, record
  the skip, and stop:

  ```bash
  uv run music-tagger checklist skip "<album-dir>" --reason "<why>"
  ```

## 3. Choose the cover

```bash
uv run music-tagger art "<album-dir>" --release-id <uuid> --preview
```

It prints `Candidate:` (a temp file, not in the album) and `Current:`, each
with dimensions. View both images with Read, then pick one:

- **Candidate shows this release's artwork** and is not lower quality than
  the current cover: `--cover-file "<candidate path>"`.
- **Current cover is correct** and the candidate is wrong, a different
  edition, or lower quality: `--keep-cover`.
- **Neither is right, or exit 1 (`No cover art available`):** ask the user
  for an image path and pass `--cover-file "<path>"`.

Any JPEG or PNG works, at any size. The album folder keeps the original as
`cover.jpg` or `cover.png` and drops the other; the tracks get a JPEG,
converted when the original is a PNG or too large to embed. Do not convert
images yourself.

## 4. Choose genres

If the user gave genres, use them. Otherwise check the artist's other
albums first:

```bash
uv run music-tagger genre --artist "<artist>"
```

- **The artist's other albums agree** (a genre on every album other than
  this one): propose those genres. Change them only for a clear reason,
  such as a live album or a change of style.
- **No match, or `Similar:` names a directory:** rerun with that name. If
  the artist is not in the library, read
  `${CLAUDE_SKILL_DIR}/references/genre-list.md` and pick the 2–4 best fits.

Confirm with a multi-select `AskUserQuestion`; put the proposed genres first.
`apply` rejects genres that are not on the list. Pass `--force-genre` only
when the user deliberately adds a new genre.

## 5. Apply

```bash
uv run music-tagger apply "<album-dir>" --release-id <uuid> \
  --cover-file "<image>" --genre "<Genre One>" --genre "<Genre Two>"
```

Use `--keep-cover` in place of `--cover-file` when step 3 chose it. `apply`
runs tag, art, genre, rename, and ReplayGain; logs every change; and marks
the checklist entry done. Gates stop it before anything is written, so
rerunning with added flags is safe.

| Exit | Output | Action |
| --- | --- | --- |
| 0 | `Done.` | Report. |
| 3 | `Gate: review` and the overwrites | Show the user the listed overwrites. If they approve, rerun with `--approve-overwrites`. If they reject them, the release is probably wrong: return to step 2. |
| 4 | `Gate: blocked` and `ISRC conflict` lines | Resolve the ISRC conflicts below, then rerun. |
| 1 | `cover image cannot be embedded` | The image is not a readable JPEG or PNG. Choose the other cover or ask the user for one. Nothing was written. |
| Other | `apply stopped at <step>` | Report the error and the step. Do not work around it. Rerunning `apply` after the cause is fixed is safe. |

**ISRC conflicts.** The source of an existing library code is unknown: it
may have been read from the CD or set by another tagger. Show the user a
table of each conflicting file, its code, and the MusicBrainz codes. Then
ask with `AskUserQuestion`:

- **Keep file codes (Recommended):** absence from MusicBrainz does not prove
  a code is wrong. Rerun with `--isrc keep`. Kept codes are locked, so later
  runs do not raise them again.
- **Use MusicBrainz codes:** rerun with `--isrc musicbrainz`. It resolves
  tracks where MusicBrainz lists exactly one code. For a track with several,
  ask which code to use and add `--isrc-code [DISC-]TRACK=CODE`.

**Locked fields.** Lines such as `title: X (locked; release has Y)` mean an
earlier decision is protected. They are expected. Do not pass
`--ignore-locks` unless the user asks.

## 6. Report

Summarize the release, the cover decision, the genres, any approved
overwrites, kept ISRC codes, and every `Warning: ... unverified` ISRC line.
Do not run `nd rescan`; the user triggers rescans after a batch.
