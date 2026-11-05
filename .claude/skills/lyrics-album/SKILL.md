---
name: lyrics-album
description: >
  Decides the lyric tracks that lyrics-batch could not settle: chooses one of the
  saved LRCLIB records for each track, or records that a track has no lyric.
  Use when the user asks for lyrics for an album, asks to resolve pending lyrics,
  or when lyrics-batch reports pending tracks.
allowed-tools: "Bash Read"
---

# Lyrics Album

Settle the lyric decisions for one album. The CLI does the lookups, the writes,
the pending file, and the decisions file. This skill makes the per-track
judgment: which saved record, if any, is the track's lyric. Make that judgment
yourself. Do not ask the user to pick a record. Do not edit sidecars, the pending
file, or the decisions file by hand.

## 1. Run the batch for the album

```bash
uv run music-tagger lyrics-batch "<album-dir>"
```

With `--auto`, it also records the choices the rules accept. It writes the sidecars
it can settle and saves the rest to the pending file. Judge only what is left.

## 2. List the options

```bash
uv run music-tagger lyrics-decide list --album "<album-dir>"
```

No network. Each pending track shows its candidate records: ID, title, artist,
album, duration difference from the audio, whether the record is plausible
(artist, title, and duration match), whether its album matches, and what the
record offers (synced text, plain text, or instrumental).

## 3. Judge each track

A candidate is chosen only if it passes one of these rules. They match the
automatic rule in `lyrics-batch` (`confident_choice`), with two more cases for
judgment: other-album matches and plain text.

- **Synced, album matches, within 2 s:** the record matches the track's title
  and artist, its album matches, its duration is within 2 s of the audio, and
  it has synchronized text. When several qualify, choose the closest duration.
- **Synced, album matches, within 5 s:** the same, but the duration is within
  5 s of the audio, and its last synced line falls inside the audio. A last line
  past the end means a different cut or length; do not choose it.
- **Synced, other album, within 2 s:** the record matches the track's title and
  artist, has synchronized text, and its duration is within 2 s of the audio. The
  album name may differ. Use this only when no album-matching record qualifies.
- **Plain only, album matches, within 2 s:** the record matches the title,
  artist, and album, its duration is within 2 s, and it has plain text. Plain
  text has no timing to check, so use this only when no synchronized record
  qualifies. If several plain records qualify, choose the closest duration only
  when it is at least 1 s closer than the next. Otherwise skip with the reason
  "multiple plain matches."
- **Trimmed live track:** the user trims intro, outro, and applause from live
  albums. A track on a live album whose audio is shorter than a record with the same
  title and artist fits that record's plain text. The rule applies when the record
  is 2 s to 60 s longer than the audio and the record has plain text. Choose it with
  `--plain`, which writes a `.txt`, and with `--replace-skip` when the earlier skip
  was "no plausible record" or "length differs from record". If several records
  qualify, choose the closest only when it is at least 1 s closer than the next;
  otherwise skip with the reason "multiple plain matches."
- **Version terms are material:** live, remix, remaster, mono, stereo,
  alternate, acoustic, edit, and different-album titles are not the studio
  track. "Song (Live)" does not match "Song." A record qualifies only when its
  version terms match the track's title.
- **Skip** when no candidate passes these rules. Give the reason:
  "no plausible record," "length differs from record," "multiple plain matches,"
  or the specific cause. Never choose a record that fails a rule because it is
  the closest one available.
- **Merged or split file:** a file that holds two songs, or one part of a song,
  may match no single record. First look for a record whose title names the whole
  file, such as "The Load-Out / Stay", and choose it when it fits. Skip with the
  reason "merged file" or "split file" only when no such record fits. Do not choose
  a whole-song record for a part.
- **Do not skip** `needs_inspection` or `missing_metadata` tracks. Report them.
  A malformed sidecar or a missing tag is the user's file to fix.

## 4. Record each decision

Run a dry run before each choice:

```bash
uv run music-tagger lyrics-decide choose "<album-dir>" --track <N> --id <ID> --dry-run
uv run music-tagger lyrics-decide choose "<album-dir>" --track <N> --id <ID>
uv run music-tagger lyrics-decide choose "<album-dir>" --track <N> --id <ID> --plain --replace-skip
uv run music-tagger lyrics-decide skip "<album-dir>" --track <N> --reason "<why>"
```

`--plain` writes the record's plain text even when it has synchronized text.
`--replace-skip` replaces an earlier skip for that track. A recorded choice is never
replaced.

When two files share a track number, or a file has no track tag, pass
`--file "<audio file name>"` in place of `--track <N>`, using the name in the
`list` header.

Exit 2 means the ID was not among the saved options, or the track is not
pending, or it is already decided. Stop on that and report it. Exit 1 means the
write failed. Report it. Do not retry with a different record.

## 5. Tags (only when asked)

Sidecars are not tags. Embed only when the user asked for tags:

```bash
uv run music-tagger lyrics "<album-dir>" --embed --dry-run
uv run music-tagger lyrics "<album-dir>" --embed
```

## 6. Report

For each track: the decision and its reason, or the skip and its reason. List
every `needs_inspection` and `missing_metadata` track. Do not run `nd rescan`;
the user triggers rescans after a batch.
