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

## Terms

- **Disc ID**: a hash of the disc's exact track offsets. Two pressings of one
  release often differ by a few frames, so they have different disc IDs.
- "No release has this disc ID" does **not** mean "the album is not in
  MusicBrainz". It means no medium has this pressing's disc ID attached.
  `identify` finds the release by barcode and track lengths.

## 1. Identify the disc

Run this before every rip. It takes seconds; a rip takes 10 to 20 minutes.

```bash
uv run music-tagger identify --device <device> --artist "<artist>" --album "<album>"
```

Add `--barcode "<barcode>"` when the user gave one; otherwise `identify`
uses the disc's own catalog number (`MCN`), when the disc has one. For disc 2
and later of an album, pass the staging directory of disc 1 first:
`identify "$STAGING" ...`.

It ranks each release medium with the disc's track count by: disc ID
attached, all track lengths within 2 s, barcode match, then tracks within 2 s.
The `Disc ID` column is `attached` or `differs`; `Drift` is the largest
offset difference, in frames, from that medium's closest attached disc ID.

| Exit | Route line | Action |
| --- | --- | --- |
| 0 | `the disc ID is attached to medium N of M` | Run the printed `Rip:` command. The release is settled; skip step 3. |
| 3 | `... match medium N of M ..., but its disc IDs do not include this disc` | A different pressing of a listed release. Show the user the top row (barcode, `Within 2s`, any `off:` tracks), the attach URL, and the line `pick medium N (T tracks), and confirm the edit`. Ask with `AskUserQuestion`: attach now, or rip unknown. **Attach now (recommended):** wait for the user, then run step 1 again; it exits 0, so follow that row. If it still exits 3, the attach has not taken effect; ask again. **Rip unknown:** run the printed fallback `rip` command and use that release ID in step 6; keep the attach URL for step 8. |
| 1 | `no release medium matches this disc's track lengths` | Do not rip. Retry once with a different `--artist`/`--album` spelling or `--barcode`. If nothing matches, tell the user the searches tried and stop. |
| 1 | `cannot read the disc` | Report the error. Ask the user to check the disc and drive. |

When the user gave a release ID and `identify` ranks a different release
first, show both rows and ask.

When the user is not present to attach (for example a batch of discs left to
rip), take the fallback without asking.

## 2. Rip

Ripping is slow. Run the `Rip:` command from step 1 in the background and
wait for it to finish. Do not add or drop flags from it.

The first line is `Staging: <path>`; call that `$STAGING`. The output ends
with an AccurateRip summary. Whipper's Q-subchannel CRC warnings during the
track-information pass are harmless when that summary passes; do not report
them.

| Exit | Meaning | Action |
| --- | --- | --- |
| 0 | Rip verified, or tracks absent from the AccurateRip database | Tell the user the disc is safe to eject. Continue. Absent tracks are normal. |
| 3 | A track mismatched AccurateRip, did not copy OK, has no section in the log (`Missing from log:`), or the log has a section for a number outside the disc (`Unexpected in log:`) | Show the user the summary. For `Missing from log:` or `Unexpected in log:`, rip the disc again with `--replace`. For a mismatch or a status that is not `Copy OK`, ask whether to clean the disc and rip again, or continue. To rip again, rerun the same command with `--replace`. |
| 1 | `whipper failed` | Whipper found no release for the disc ID. Run step 1 if it was skipped, then use its command. |
| Other | Rip failed | Report the error and stop. |

**Multi-disc albums.** `rip` names tracks `D-NN. Title` when the release
has several discs and ends with `Disc N of M verified; safe to eject.` and
`Next: insert disc N+1, then run: ...`. Tell the user to swap discs, then run
step 1 for the new disc with `"$STAGING"`: each disc of a release can have its
own route. Every disc goes into the same `$STAGING`. Continue to step 3 after
the last disc. Do not `place` single discs or merge them by hand.

**A verified rip is final.** Never rip a disc again to fix its metadata, its
file names, or because a disc ID was attached to MusicBrainz after the rip.
`apply --release-id` sets the tags, renames the tracks, and renames whipper's
`Unknown Artist - <disc ID>` `.cue`, `.log`, and `.toc` files. Rip again only
for an AccurateRip problem (exit 3).

Report only these events to the user: each disc finished and safe to eject,
the AccurateRip result, and the album placed. Do not relay whipper's
per-track progress.

## 3. Check the release

Skip this step when every disc was ripped with `--release-id`.

```bash
uv run music-tagger candidates "$STAGING" --artist "<artist>" --album "<album>"
```

Add `--barcode "<barcode>"` when step 1 used one. The table lists every
candidate, ranked: exact track count, then `partial`, then mismatched
counts; within each group by tracks within 2s, `on-files` (the release
whipper matched), CD, US, and earliest date. The ranking is advice. Decide:

- **The release from step 1 is a clear row** (track counts equal, nearly
  every track within 2s): use it.
- **Several rows tie on durations** (editions that differ only in country,
  label, or pressing plant): use the release from step 1. Ask with
  `AskUserQuestion`, showing the tied rows, only when step 1 named none of
  them.
- **`No candidates found.`, or no row fits:** retry once with different
  `--artist` or `--album` spelling. If nothing fits, tell the user the
  search terms tried and stop. Do not place an unidentified album.

## 4. Choose the cover

```bash
uv run music-tagger art "$STAGING" --release-id <uuid> --preview
```

It prints `Candidate:` (a temp file) with dimensions. View it with Read.
If it shows this release's artwork, pass `--cover-file "<candidate path>"`.
If it is wrong, or exit 1 (`No cover art available`), ask the user for an
image path and pass `--cover-file "<path>"`.

Any JPEG or PNG works, at any size. The album folder keeps the original as
`cover.jpg` or `cover.png`; the tracks get a JPEG, converted when the
original is a PNG or too large to embed. Do not convert images yourself.

## 5. Choose genres

If the user gave genres, use them. Otherwise check the artist's albums
already in the library first:

```bash
uv run music-tagger genre --artist "<artist>"
```

- **The artist's albums agree** (a genre on every album): propose those
  genres. Change them only for a clear reason, such as a live album or a
  change of style.
- **No match, or `Similar:` names a directory:** rerun with that name. If
  the artist is not in the library, read
  `${CLAUDE_SKILL_DIR}/references/genre-list.md` and pick the 2–4 best fits.

Confirm with a multi-select `AskUserQuestion`; put the proposed genres first.
`apply` rejects genres that are not on the list. Pass `--force-genre` only
when the user deliberately adds a new genre.

## 6. Apply

```bash
uv run music-tagger apply "$STAGING" --release-id <uuid> \
  --cover-file "<image>" --genre "<Genre One>" --genre "<Genre Two>"
```

`apply` runs tag, art, genre, rename, and ReplayGain, and logs every change.
On an unknown rip, whipper's placeholders (`Unknown Track`, `Unknown Artist`,
`Unknown Album`) count as empty, so replacing them needs no approval.
Gates stop it before anything is written, so rerunning with added flags is
safe.

| Exit | Output | Action |
| --- | --- | --- |
| 0 | `Done.` | Continue to step 7. |
| 3 | `Gate: review` and the overwrites | Show the user the listed overwrites. If they approve, rerun with `--approve-overwrites`. If they reject them, the release is probably wrong: return to step 3. |
| 4 | `Gate: blocked` and `ISRC conflict` lines | Rerun with `--isrc keep`. Whipper read those codes from this disc, and absence from MusicBrainz does not prove them wrong. This needs no user confirmation. |
| 1 | `cover image cannot be embedded` | The image is not a readable JPEG or PNG. Ask the user for another image. Nothing was written. |
| 1 | `Warning: N unreadable file(s)` before `apply stopped at tag (exit 1)` | An audio file could not be read. Nothing was written. Report the file names. |
| 1 | `disc D track N: "a", "b"` before `apply stopped at tag (exit 1)` | Two files share a disc and track number. Nothing was written. Report the names; this needs the user's judgment. |
| 1 | `... already exists and is not part of this rename` before `apply stopped at rename (exit 1)` | A file outside the album plan holds a target name. Nothing was renamed. Report the file and the name; this needs the user's judgment. |
| Other | `apply stopped at <step>` | Report the error and the step. Do not work around it. Rerunning `apply` after the cause is fixed is safe. |

## 7. Place

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

It prints `[N/total] <file>: copied` as each file is verified by SHA-256 and
logs the copy. It removes only the files it copied and verified, plus
top-level `.m3u` files, and removes the directory only when that leaves it
empty. If it prints `Kept staging directory <path>: N uncopied entries`,
report those entries to the user and do not delete them. Copying to the NAS
takes minutes; wait for it.

Exit 1 means a copy failed, or `place` refused before copying. Report the
error and keep `$STAGING`:
- `failed:` lines: a copy failed.
- A destination name collision: two files map to one name, and the error
  names both. Nothing was copied.
- `Warning: N unreadable file(s)`: an audio file could not be read. Nothing
  was copied.
- `Error: destination ... is inside the source`. Nothing was copied.

## 8. Report

Summarize the AccurateRip result, the release, the cover, the genres, any
approved overwrites, kept ISRC codes, every `Warning: ... unverified` ISRC
line, and the library path. For each disc ripped by the fallback after `identify`
exited 3, give the attach URL and the line `pick medium N (T tracks), and
confirm the edit`, verbatim: the user attaches the disc ID so future rips of
this pressing match. Attaching is never needed to tag or place this album. Do not run `nd rescan`; the user triggers
rescans.
