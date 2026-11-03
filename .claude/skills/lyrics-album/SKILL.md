---
name: lyrics-album
description: >
  Saves LRCLIB lyrics for an album as .lrc (synchronized) or .txt (plain)
  sidecars beside each track. The LLM chooses the LRCLIB record for each track
  from the evidence the CLI prints. Use when the user asks for lyrics for an
  album, or when an album has tracks without lyrics.
allowed-tools: "Bash Read"
---

# Lyrics Album

Save lyrics for one album. The CLI performs the lookup, the write plan, and the
file writes, and writes `changes.log`. This skill makes the per-track
judgment: which LRCLIB record, if any, is the track's lyric. Make that judgment
yourself. Do not ask the user to pick a record. Do not edit sidecars by hand.

## 1. Inspect

```bash
uv run music-tagger lyrics "<album-dir>"
```

It prints each track's existing lyrics and the album coverage. Existing lyrics
are kept by default. Change a track that already has lyrics only with
`--replace`, and only for a stated reason: the lyric is for another song, or it
is malformed.

## 2. Look up

```bash
uv run music-tagger lyrics "<album-dir>" --lookup
```

Each track prints its outcome, the reason, and one line per candidate record.
A candidate marked `(plausible)` matches the artist, the title, and the
duration within 2 s. Each candidate also lists its album match and what it
offers: synchronized text, plain text, instrumental, or nothing.

## 3. Judge each track

- **`selected`:** the CLI found exactly one plausible record on the matching
  album. Fetch it (step 4) without `--lyrics-id`.
- **`ambiguous`, `review`, or `no_match`:** choose from the candidates.
  Prefer a record whose title, artist, album, and duration match the track.
  Treat version terms as material: live, remix, remaster, mono, stereo,
  alternate, and different-album titles are not the studio track. When two
  records fit equally, prefer the one with synchronized text. If no record
  fits, leave the track without lyrics and record why.
- **`instrumental`:** correct. No file is written.
- **`no_text`:** the matching record has no lyric text. Leave the track without
  lyrics and record why.
- **`rate_limited` or `provider_failure`:** LRCLIB is unavailable. Do not guess.
  Rerun that track later and say so in the report.
- **`missing_metadata`:** the track lacks a title, artist, or duration tag.
  Report it. Do not search with guessed values.

## 4. Fetch

For a track you chose by judgment, check the record and then write it:

```bash
uv run music-tagger lyrics "<album-dir>" --track <N> --lookup --lyrics-id <ID>
uv run music-tagger lyrics "<album-dir>" --track <N> --fetch --lyrics-id <ID> --dry-run
uv run music-tagger lyrics "<album-dir>" --track <N> --fetch --lyrics-id <ID>
```

For a `selected` track, use the same sequence without `--lyrics-id`. Run the
dry run first. Write only when the dry run shows the intended file.

Writing the whole album with `--fetch` and no `--track` writes every `selected`
track. Judged tracks are then left unresolved, and the command exits 3. Do the
judged tracks one at a time after that.

## 5. Replacement

Use `--replace` only for the reasons in step 1. It also removes a stale sidecar
of the other type, so show that removal in the dry run before writing.

## 6. Report

For each track, give the outcome, the record ID used, and the file written or
left alone. For each judgment, give the reason for the choice. List every
track left without lyrics and why. Do not run `nd rescan`; the user triggers
rescans after a batch.
