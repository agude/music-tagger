---
name: rip-album
description: >
  Rips a CD to FLAC via whipper, tags it from MusicBrainz, embeds cover art,
  sets genre, renames files, computes ReplayGain, and copies the album into
  the music library. Use when the user asks to rip a CD or process a new
  album from disc.
allowed-tools: "Bash Read AskUserQuestion"
---

# Rip Album

Rip one CD and place it in the library. The CLI performs every mechanical
step and writes `changes.log`. The operator makes four decisions: the
release, the cover, the genres, and approval of anything a gate stops on.
Do not edit tags or JSON by hand.

## Inputs

Collect these before starting:

| Input | Required | Example |
| --- | --- | --- |
| Artist | yes | `Matchbox 20` |
| Album title | yes | `Yourself or Someone Like You` |
| Barcode | no | `075679272126` |
| Genres | no | `Y2K Rock` |
| Release ID | no | MusicBrainz release UUID |
| Device | no | `/dev/cdrom` (default) |

## 1. Rip

Ripping is slow. Run it in the background and wait for it to finish:

```bash
uv run music-tagger rip --device <device>
```

Add `--release-id <uuid>` when the user gave one. If whipper reports that
the disc is not in MusicBrainz, rerun with `--unknown`.

The first line is `Staging: <path>`; call that `$STAGING`. The output ends
with an AccurateRip summary.

| Exit | Meaning | Action |
| --- | --- | --- |
| 0 | Rip verified, or tracks absent from the AccurateRip database | Continue. Absent tracks are normal. |
| 3 | A track mismatched AccurateRip or did not copy OK | Show the user the summary. Ask whether to clean the disc and rip again, or continue. |
| Other | Rip failed | Report the error and stop. |

## 2. Choose the release

Skip this step if the user gave a release ID.

```bash
uv run music-tagger candidates "$STAGING" --artist "<artist>" --album "<album>"
```

Add `--barcode "<barcode>"` when the user gave one. The table lists every
candidate, ranked: exact track count, then `partial`, then mismatched
counts; within each group by tracks within 2s, `on-files` (the release
whipper matched), CD, US, and earliest date. The ranking is advice. Decide:

- **One clear row** (track counts equal, nearly every track within 2s, the
  next rows clearly worse): use it. Confirm with the user only when it is
  not `on-files`.
- **Several rows tie on durations** (editions that differ only in country,
  label, or barcode): prefer a barcode match, then `on-files`, then the
  ranking. Show the top 2–3 rows and ask with `AskUserQuestion`.
- **`No candidates found.`, or no row fits:** retry once with different
  `--artist` or `--album` spelling. If nothing fits, tell the user the
  search terms tried and stop. Do not place an unidentified album.

## 3. Choose the cover

```bash
uv run music-tagger art "$STAGING" --release-id <uuid> --preview
```

It prints `Candidate:` (a temp file) with dimensions. View it with Read.
If it shows this release's artwork, pass `--cover-file "<candidate path>"`.
If it is wrong, or exit 1 (`No cover art available`), ask the user for an
image path and pass `--cover-file "<path>"`.

## 4. Choose genres

If the user gave genres, use them. Otherwise read
`${CLAUDE_SKILL_DIR}/references/genre-list.md`, pick the 2–4 best fits for
the artist and album, and confirm with a multi-select `AskUserQuestion`.
`apply` rejects genres that are not on the list. Pass `--force-genre` only
when the user deliberately adds a new genre.

## 5. Apply

```bash
uv run music-tagger apply "$STAGING" --release-id <uuid> \
  --cover-file "<image>" --genre "<Genre One>" --genre "<Genre Two>"
```

`apply` runs tag, art, genre, rename, and ReplayGain, and logs every change.
Gates stop it before anything is written, so rerunning with added flags is
safe.

| Exit | Output | Action |
| --- | --- | --- |
| 0 | `Done.` | Continue to step 6. |
| 3 | `Gate: review` and the overwrites | Show the user the listed overwrites. If they approve, rerun with `--approve-overwrites`. If they reject them, the release is probably wrong: return to step 2. |
| 4 | `Gate: blocked` and `ISRC conflict` lines | Rerun with `--isrc keep`. Whipper read those codes from this disc, and absence from MusicBrainz does not prove them wrong. This needs no user confirmation. |
| Other | `apply stopped at <step>` | Report the error and the step. Do not work around it. |

## 6. Place

Preview the destination:

```bash
uv run music-tagger place "$STAGING" --dry-run
```

Check the artist and album directory names. Correct a wrong name with
`--artist-dir "<name>"` or `--album-dir "<name>"`. If the output says
`(exists)` or exits 1 because the destination exists, the library already
has this album: stop and ask the user whether to use a different
`--album-dir` (for example `<Album> (Remaster)`) or to `--merge`.

Then copy with the same flags:

```bash
uv run music-tagger place "$STAGING"
```

It verifies every file by SHA-256, logs the copy, and removes `$STAGING`.
Exit 1 means a copy failed; `$STAGING` is kept. Report the failed files.

## 7. Report

Summarize the AccurateRip result, the release, the cover, the genres, any
approved overwrites, kept ISRC codes, every `Warning: ... unverified` ISRC
line, and the library path. Do not run `nd rescan`; the user triggers
rescans.
