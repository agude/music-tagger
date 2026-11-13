# AGENTS.md

`GEMINI.md` is a symlink to this file.

# music-tagger

Fix Navidrome album splits caused by inconsistent `MUSICBRAINZ_ALBUMID` tags.
This is an interactive tool — Claude Code is the operator, not the end user.

## How it works

Key CLI subcommands, driven by Claude Code in conversation:

1. `uv run music-tagger scan <library-root> -o checklist.md` — finds albums
   with split or missing MusicBrainz IDs. A `.md` output adds them to the
   markdown checklist and keeps existing entries and their status; any
   other output is JSON. `checklist next | done <dir> | skip <dir> --reason
   <text> | reopen (<dir> | --matching <text>)` reads and updates it
   (default file `$MUSIC_TAGGER_CHECKLIST`, else
   `~/Projects/music-tagger/checklist.md`). Never edit the checklist by
   hand.

2. `uv run music-tagger candidates <album-dir>` — reads audio file tags and
   durations, searches MusicBrainz for CD releases, and prints every
   candidate as a ranked table: track counts, tracks within 2s, max
   deviation, country, date, label, barcode, flags (`on-files` when the
   files already carry the release ID, `partial` when the files are a
   subset of the release), and title. Rank order: exact track count,
   partial, mismatch; then tracks within 2s, `on-files`, CD, US, earliest
   date. The ranking is advice; the operator chooses. Prints
   `No candidates found.` with the search terms when MusicBrainz returns
   nothing. `--json` prints JSON instead; `-o FILE` also writes it.

3. `uv run music-tagger tag <album-dir> --release-id <uuid> [--dry-run] [--log changes.log]`
   — fetches the chosen release from MusicBrainz, computes a field-by-field
   diff against current tags, and writes if not `--dry-run`. Appends all
   changes to the log file for auditing.
   It ends with a safety-gate verdict and a matching exit code: 0
   `Gate: auto`, 3 `Gate: review` (an overwrite of a non-empty `title`,
   `artist`, `album`, `albumartist`, or `date` that is more than a case,
   Unicode-punctuation, or whitespace change), 4 `Gate: blocked`
   (unresolved ISRC conflicts). A real run refuses exit-3 changes unless
   `--approve-overwrites` is passed. On an unknown-rip file (it has
   `MUSICBRAINZ_DISCID` and no `MUSICBRAINZ_ALBUMID`), whipper's placeholders
   (`title` `Unknown Track`, `artist`/`albumartist` `Unknown Artist`, `album`
   `Unknown Album`, exact case) count as empty, so replacing them is `auto`. On
   any other file the same values still need review.

4. `uv run music-tagger genre <album-dir> [genre ...] [--dry-run] [--log changes.log]`
   — sets the genre meta-grouping tags on all tracks. Pass each genre as its
   own argument; they are written as separate tag values, never as one
   delimited string. Omit them to show the current genres. Use `--add` and
   `--remove` to edit incrementally without restating the rest,
   `genre --list` to scan the library for all genres in use, and
   `genre --artist "<name>"` to show the genres on each of that artist's
   album directories (`<root>/<artist>/<album>`) with a count per genre.
   Artist matching ignores case, a leading "The", and punctuation; a miss
   prints `Similar:` directory names. Skills run it before proposing genres. Multiple genres
   per album are encouraged when they fit. These are broad browsing
   categories, not musicological genres. Values are validated against
   `.claude/skills/rip-album/references/genre-list.md`, the canonical list;
   `--force` overrides for a deliberate addition. `--track N` (repeatable)
   limits `--add` or `--remove` to those tracks; a multi-disc album also
   needs `--disc D`. A number that matches no track exits 1 and writes
   nothing.

   ```bash
   genre <dir> "Pop Rock" "Y2K Rock"   # replace with exactly these two
   genre <dir> --add "Live"            # keep what is there, add one
   genre <dir> --add "Live" --disc 2 --track 10 --track 11   # two tracks only
   genre <dir> --remove "Live"         # drop one
   ```

5. `uv run music-tagger replaygain <album-dir> [--dry-run] [--skip-existing]`
   — computes ReplayGain 2.0 (EBU R128) loudness via `rsgain` and writes
   track + album gain/peak tags. Use `--dry-run` to preview values without
   writing. Use `--skip-existing` to skip files already tagged. Use
   `--track-only` when the directory holds files from more than one album: it
   writes each file's track gain and leaves album tags alone.
   Requires system package: `rsgain`.

6. `uv run music-tagger rename <album-dir> [--dry-run]` — renames audio
   files to `NN. Title.ext` (`D-NN. Title.ext` on multi-disc albums) from
   their tracknumber, discnumber, and title tags, the same names `place`
   uses. Sanitizes filesystem-unsafe characters. Skips files missing title
   or tracknumber tags. Plans every rename first: if two sources would get the
   same name (compared case-insensitively, sidecars included), it prints the
   collision, exits 1, and renames nothing. A target that exists blocks the
   rename (exit 1, nothing renamed) unless it is another file this rename moves,
   so two files may swap names, or the same file, so a case-only change works.
   Renames go through temporary names; a failure reverts the renames already
   made. A file the revert cannot move back is printed once, as `<old> is now
   <name> (<error>)`, and logged when it reached its planned name. Nothing is
   renamed onto a path that holds a stranded file: a source at such a path stays
   under its temporary name and is printed as `<old> is now <temp> (<old> is held
   by <other>)`, so a swap never replaces a file. `apply` stops at rename on exit 1. Rewrites the `FILE "…"` lines of every `.cue` in the
   album for the renamed files and warns about any FILE line naming a file
   that does not exist.

7. `uv run music-tagger rip [<output-dir>] [--device /dev/cdrom] [--release-id <uuid>] [--unknown] [--disc N --of M]`
   — rips a CD to FLAC via whipper with AccurateRip verification.
   Whipper handles disc ID lookup, MusicBrainz matching, and FLAC
   encoding. Produces .log and .cue files alongside the audio.
   Use `--release-id` to force a specific MB release, or `--unknown`
   to rip CDs not in MusicBrainz. An unknown multi-disc album needs
   `--disc N --of M` (with `--unknown`) on each disc: the position is
   written into the tags, so each disc gets its `N-` prefix and the discs
   do not collide. Without an output directory, it creates a
   temp staging directory and prints `Staging: <path>`. It parses whipper's
   log and prints an AccurateRip summary per track; it exits 3 when a track
   is a mismatch (`Found, NO exact match`), its status is not `Copy OK`, or
   the log has no section for a ripped track (`Missing from log`) or has one
   for a number outside 1..N (`Unexpected in log`). It prints `verified` only
   when none of these hold. When whipper rips audio hidden before track 1
   (`00. Hidden Track One Audio.flac`), N counts only the numbered tracks and the
   log's track 0 section is expected. Tracks absent from the AccurateRip database are normal. A whipper
   failure exits 1 with `Error: whipper failed (exit N)`, not a traceback.
   Whipper refuses an output directory that holds a `.log`, so `rip` runs
   it in a fresh `.whipper-*` subdirectory and moves the files up. When
   whipper's `DISCNUMBER`/`DISCTOTAL` tags show a multi-disc release, tracks
   get the `D-` prefix (and their CUE references with them), so every disc
   is ripped into the same directory: `rip <staging>` once per disc. It
   prints `Disc N of M verified; safe to eject.` and the command for the
   next disc. A name collision (the same disc ripped twice) is refused
   before any move; `--replace` overwrites. Whipper's `%d` already appends
   `(Disc N of M)`, so each disc's `.cue` and `.log` names are distinct.
   Requires system package: `whipper`.

8. `uv run music-tagger lock <album-dir> [field ...] [--track N] [--disc N] [--dry-run]`
   — locks fields so `tag` keeps their current value, including an empty
   one. Omit the fields to show current locks. `unlock` takes the same
   arguments and removes them. `tag` reports withheld changes as
   `field: old (locked; release has new)` and never as "no changes needed";
   `tag --ignore-locks` overwrites locked fields and leaves the locks in
   place. A locked `isrc` skips the ISRC conflict check.

9. `uv run music-tagger apply <album-dir> --release-id <uuid> (--cover-file <img> | --keep-cover) --genre <g> [--genre <g> ...]`
   — runs `tag`, `art`, `genre`, `rename`, and `replaygain` in order and
   stops at the first non-zero exit. Checks that the cover image can be
   embedded (a readable JPEG or PNG) and that the genres are canonical
   before any step writes. `--force-genre` lets an unlisted genre through, as
   `genre --force` does. Passes `--approve-overwrites`, `--ignore-locks`,
   `--isrc`, and `--isrc-code` through to `tag`, so a gate stop (exit 3 or
   4) happens before anything is written.
   An exception inside a step also prints `apply stopped at <step>` and
   exits 1. `main()` line-buffers stdout so progress lines interleave with
   whipper's and rsgain's output when piped.

10. `uv run music-tagger place <album-dir> [--root <dir>] [--artist-dir <name>] [--album-dir <name>] [--merge] [--dry-run]`
    — copies a finished album into the library with SHA-256 verification.
    Refuses an existing album directory unless `--merge` (existing files are
    kept). Prints a flushed `[N/total] <file>: copied` line as each file is
    verified. Refuses a destination inside the source, and refuses (exit 1, dry run
    included, nothing copied) when two sources map to one destination, compared
    case-insensitively, as `place`, `path-for`, and `copy` all do. When the source is under
    the temp directory (not the temp root itself), it deletes only the files it
    copied and verified, plus top-level `.m3u` files (whipper's playlists, never
    copied). Anything else stays in the source: uncopied files, subdirectories,
    and skipped files. It removes the directory only when that leaves it empty,
    and prints the kept entries. The placed
    `.cue` files are rewritten for the placed track names. Non-audio
    file names are sanitized like track names: whipper names its `.log`,
    `.cue`, and `.toc` after the disc title, and a `:` in it made the NAS
    store them as 8.3 names such as `SFYL6N~T.LOG`.

11. `uv run music-tagger lyrics <album-dir> [--track N [--disc D]] [--lookup | --fetch [--dry-run] [--replace] [--lyrics-id ID] | --embed [--dry-run] [--replace]]`
    — with no mode, inspects embedded lyrics and `.lrc`/`.txt` sidecars: no
    network, no writes. `--lookup` searches LRCLIB and shows each candidate's
    evidence. `--fetch` saves the chosen lyrics as sidecars. `--embed` writes
    each track's sidecar into its tags, with no network. Details are under
    "Lyrics" below.

12. `uv run music-tagger lyrics-batch <path> [--auto] [--embed] [--dry-run] [--pending FILE] [--decisions FILE]`
    — backfills lyrics for every album under `<path>` (a library root, an artist
    directory, or one album). Tracks with lyrics or a recorded decision are never
    searched. A track gets a sidecar only when its LRCLIB choice is certain. The rest
    are saved to the pending file with every candidate's record ID. `--embed` also
    writes sidecars into tags. Details are under "Batch" below.

13. `uv run music-tagger lyrics-decide list [--album DIR]`,
    `lyrics-decide choose <album> (--track N [--disc D] | --file NAME) --id ID [--dry-run]`,
    `lyrics-decide skip <album> (--track N [--disc D] | --file NAME) --reason TEXT`
    — judges pending tracks from their saved options. `list` reads the pending
    file with no network. `choose` accepts only a saved record ID and writes that
    record's lyrics as a sidecar. `skip` records that a track has no lyrics and
    writes no file. Details are under "Decisions" below.

## Library workflow

**For ripping a new CD**, use the `rip-album` skill: `rip` → `candidates`
→ `art --preview` → `apply` → `place`.

**For fixing existing library albums**, use the `fix-album` skill:
`checklist next` → `candidates` → `art --preview` → `apply`, in place.

**Design rule: anything that can be scripted is a CLI command.** The operator
exists for judgment: which release, whether a cover is right, which genres,
and approving what a gate stops on. Skills must not have the operator edit
tags, JSON, or the checklist by hand, classify diffs, apply tie-break rules,
or chain temp files. When a skill step needs mechanical work, add a command
or flag instead.

Processing the library is staged album by album across sessions:

1. Run `scan <library-root> -o checklist.md` to add albums to the checklist.
2. Each session: `checklist next` names the next album.
3. Per album: run the `fix-album` skill. `apply` marks the entry done;
   `checklist skip` records why an album was not fixed.
4. After a batch: `nd rescan` to trigger Navidrome library scan.
5. Repeat until done. Re-scan if needed to catch stragglers.

**File locations:**
- Checklist: `~/Projects/music-tagger/checklist.md`
- Change log: `~/Projects/music-tagger/changes.log`
- Library root: `/mnt/synology/media/music`

## Matching rules

- Track count must match (except partial albums / singles).
- A hidden track (tracknumber 0, the audio whipper rips before track 1) is
  not counted. It gets album fields only, with the note `hidden track before
  track 1; album fields only`. Two hidden files on one disc are a duplicate.
- Partial albums: a file's tracknumber is trusted only when that release
  track's duration (within 2 s) or title agrees with the file. Otherwise the
  file matches the one release track on its disc that fits by duration, with a
  title breaking a tie. A file that fits nothing is warned about, and its
  title and track number are left unchanged. A release track goes to at most
  one file: when two files claim the same one, both are warned about and
  neither is matched.
- Track durations are ground truth. A good match has most tracks within 2s.
- Mixed media: DVD, Blu-ray, and other non-audio media (`NON_AUDIO_FORMATS` in
  `musicbrainz.py`) are left out of a release's `discs` and `track_count`. The
  `format` summary keeps them. `totaldiscs` is the highest audio disc position. A
  release with no audio media is an error for `tag`.
- Two files with the same (disc, track) key are an error: `tag`, `diff`, and
  `apply` exit 1 before writing, naming the files. `candidates` warns and flags
  the release `duplicate-keys`. Positional matching (no track numbers) uses each
  file's index within its own disc.
- A full-album file that matches no release track is warned about, and gets
  album fields only: its artist is not proposed. Its track-owned IDs are not
  cleared either, since only matched files lose another release's track data.
- Existing metadata tags may be wrong — use them as search seeds and
  tiebreakers, not as matching criteria.
- For multi-disc releases, the tool reads disc numbers from existing tags.

## Architecture

```
src/music_tagger/
├── tags.py          # Read FLAC, MP3, and M4A tags; write FLAC and MP3 via mutagen
├── fileio.py        # Atomic file replacement: temp file, fsync, rename
├── musicbrainz.py   # MB API client with 1 req/sec rate limiting
├── tagger.py        # Orchestration: search → diff → write
├── cue.py           # Keep CUE FILE lines in step with renamed audio
├── discid.py        # Whipper log TOC and MusicBrainz disc ID
├── placement.py     # Library path computation + verified copy
├── coverart.py      # Cover Art Archive client
├── navidrome.py     # Navidrome / Subsonic API client
├── stats.py         # Statistics snapshot: model, validation, atomic write
├── replaygain.py    # ReplayGain 2.0 tagging via rsgain
├── ripper.py        # CD ripping via whipper (AccurateRip)
├── lyrics.py        # Lyric readers, classification, inspection (read-only)
├── lrclib.py        # LRCLIB client (fetch only) and candidate policy
├── lyrics_write.py  # Lyric sidecar plans and atomic writes
├── lyrics_batch.py  # pending options and decisions: JSON files and rendering
├── lyrics_embed.py  # Embed sidecar lyrics into tags (backup; no network)
├── checklist.py     # Album checklist file: parse, update, and render entries
├── genres.py        # Canonical genre list and per-album genre reads
└── cli/             # Argparse commands, one package per command group
    ├── __init__.py   # main() and the command dispatch table only
    ├── common.py     # Logs, exit codes, gate and warning printers shared by modules
    ├── tagging.py    # read, match, diff, write-tags, tag, apply, lock, unlock, candidates
    ├── library.py    # scan, checklist, path-for, copy, place, rename, art, genre, replaygain
    ├── rip.py        # rip, toc
    ├── ratings.py    # write-ratings, write-stats, nd ratings, nd stats
    ├── services.py   # mb and nd subcommands (except ratings and stats)
    └── lyrics_cmd.py # lyrics, lyrics-batch, lyrics-decide
```

- `tags.py` provides a unified interface over FLAC Vorbis Comments, MP3
  ID3v2 frames, and MP4/M4A atoms. Field names follow Navidrome's
  `mappings.yaml` conventions. M4A is **read-only for general tags**. The
  writes that exist are: genre (`read_genres`/`write_genres`), the rating and
  play-count atoms (`write_stats_to_file`, `write_rating_to_file`), and the
  `©lyr` atom (`lyrics --embed`). `write_tags` raises `NotImplementedError`, and
  `write_locks` refuses M4A, rather than silently writing nothing. `replaygain`
  writes M4A through `rsgain`.
- **M4A ratings**: MP4 has no standard star field and the conventions
  collide — the freeform `RATING` atom is 0-100 in MusicBee/EZ CD, while
  this tool writes `RATING` as 1-5 for FLAC and MP3. Writing 1-5 into an
  M4A would read back as unrated. So M4A gets `FMPS_Rating` (0.0-1.0,
  scale-free) and `FMPS_Playcount` as `----:com.apple.iTunes:` freeform
  atoms, plus the native `rate` atom (0-100) for third-party readers, and
  deliberately no bare `RATING`. Favorites are not written to M4A.
- `stats.py` owns the statistics snapshot: `SongStats`, `StatsSnapshot`,
  `parse_snapshot` (validation, no I/O), and `write_snapshot_file`, which
  keeps the previous snapshot as `.previous` and applies the drop guard.
- `musicbrainz.py` searches and fetches releases. Rate-limited to 1 req/sec;
  retries 429 and 503 up to 4 attempts with exponential backoff.
- `tagger.py` builds the target tags from an MB release and computes diffs.
  Handles multi-disc albums by parsing `discnumber` tags.
- `discid.py` reads the TOC and disc ID from whipper's log. With a lookup URL the
  URL's `toc=` and the logged ID must agree; without one, the disc ID is computed
  from the TOC section and checked against the logged ID. A directory with several
  logs needs `toc --log FILE`.
- `placement.py` computes library destination paths and copies files with
  SHA-256 verification.
- `coverart.py` fetches front cover images from the Cover Art Archive or
  installs a local image via `use_local_cover()`. The `art` subcommand
  accepts `--cover-file <path>` as an alternative to `--release-id`.
  The folder cover is named by its bytes, `cover.jpg` or `cover.png`, and
  installing one removes the other: Navidrome reads `cover.*` before
  embedded art, and which of two wins is unspecified. Every image is fully
  decoded (Pillow `load()`) before anything is written; a truncated or corrupt
  JPEG or PNG is refused, and an existing cover is kept. The new cover is
  written atomically and keeps the mode a plain write would give. `art --embed`
  embeds `embeddable_cover()`: a valid JPEG that fits unchanged; a PNG, or a JPEG over
  `MAX_EMBEDDED_BYTES`, re-encoded as quality-95 4:4:4 JPEG (Pillow) and
  downscaled until it fits. A FLAC picture block has a 24-bit length, so an
  18.6 MB PNG raised `mutagen.flac.error: block is too long to write`.
  `art --embed` prints `Skipped N file(s):` naming each M4A file (no cover
  support), each audio-suffixed entry that is not a regular file, and each audio
  file that mutagen cannot identify (`unreadable`).
  `art --release-id <uuid> --preview` downloads the full-size front cover to
  a temp file and prints its path and dimensions next to the album's
  current `cover.*`, writing nothing to the album, so the operator can
  look at it before passing it to `apply --cover-file`.
- `replaygain.py` subprocess wrapper around `rsgain` for ReplayGain 2.0 tagging.
- `navidrome.py` Subsonic API client for library scans, rating writes, and
  the native `/api/song` listing (`get_all_ratings`, `get_all_stats`).
- `cli/` wires the subcommands together in `__init__.py`; each command group
  lives in the module named in the tree above.

## Ratings and play counts

Navidrome is the source of truth. Files carry a copy of each song's rating
and play count, so other players and the NAS see them.

Commands (all through `just`, which loads `.env`):

- `just dump-stats` runs `nd stats`. It exports every song the user can see,
  with ratings, play counts, favorites, and timestamps, to
  `/mnt/synology/media/music/stats.json`. The whole listing is fetched and
  checked against the server's `x-total-count` before the destination is
  touched. A valid current snapshot is renamed `stats.json.previous`; one
  that does not parse is moved to `stats.json.invalid`, and a read error
  refuses the export. A new export that loses more
  than 10% of the songs, rated songs, or starred songs is refused with no
  file written, unless `--force` is passed.
- `just sync-stats --dry-run` runs `write-stats` as a preview. It lists each
  changed field with old and new values, and the number of files and bytes
  a real run would rewrite. `just sync-stats` writes.
- `write-stats --library NAME_OR_ID` selects the Navidrome library to write.
  The default is `Music Library` (`_DEFAULT_LIBRARY` in `cli/ratings.py`). Songs in
  other libraries, such as `Podcasts` (podcast episodes and audiobooks, whose
  paths are not under the music root), are counted as `other library` and do
  not affect the exit code. `--library all` writes every library. A numeric
  value matches `libraryId`; any other value matches `libraryName`.
- `just sync-ratings` runs `write-ratings`, which writes ratings only, from
  the legacy `ratings.json` written by `nd ratings`.

Snapshot (`stats.json`, version 1): `version`, `sourceUser`, `exportedAt`
(UTC), and `songs`. Each song has `id`, `path`, `title`, `artist`, `album`,
`rating`, `ratedAt`, `starred`, `starredAt`, `playCount`, `playDate`,
`libraryId` (int), and `libraryName` (string). The library fields are
optional: snapshots written before them still load. Without them,
`write-stats` cannot apply `--library` and warns about the songs it could not
filter. Navidrome's native API omits zero and false, so the exporter writes
`rating: 0`, `playCount: 0`, and `starred: false` explicitly. In a snapshot,
an absent key means "no instruction", and the writer leaves that value
alone. Timestamps stay in the JSON only.

Tag mapping, by container:

| Container | Rating | Play count |
| --- | --- | --- |
| FLAC | `FMPS_RATING` (0.0-1.0), `RATING` (1-5) | `FMPS_PLAYCOUNT` |
| MP3 | `TXXX:FMPS_Rating`, `TXXX:RATING`, POPM rating | `TXXX:FMPS_Playcount`, POPM count |
| M4A | `----:com.apple.iTunes:FMPS_Rating`, `rate` (0-100) | `----:com.apple.iTunes:FMPS_Playcount` |

- The writer owns only the POPM frame with an empty owner. Frames from other
  owners, and their counts, are never changed.
- MP3 TXXX descriptions and M4A freeform names are case-sensitive. The
  writer emits `FMPS_Rating` and `FMPS_Playcount`. Readers also accept
  `FMPS_RATING`, `FMPS_PlayCount`, and `FMPS_PLAYCOUNT`. Two accepted
  spellings with different values are a conflict, and the write is refused.
  FLAC Vorbis keys are case-insensitive and keep `FMPS_RATING`.

Policy:

- Favorites (`STARRED`, `STARRED_AT`) are never written to files. Existing
  values stay. Favorites are kept in the snapshot.
- A snapshot rating of 0 clears the file's rating only with `--clear`.
  Otherwise the file is left alone and reported as `clear withheld`.
- A snapshot play count lower than the file's count is written only with
  `--allow-decrease`. Otherwise it is reported as `decrease withheld`. The
  file's count is the larger of its FMPS and POPM counts.
- An explicit zero play count writes zero when the file has no count.
- A clear keeps the POPM count. A rating-only write keeps counts, and a
  count-only write keeps ratings.
- Unchanged files are never saved. A second run changes nothing. A dry run
  writes nothing and appends nothing to the change log.

Rewrite cost: play counts change with every listen, so each `sync-stats`
rewrites every file played since the last run, over the NAS mount. Each
rewrite changes the file's mtime, so Navidrome rescans it and a NAS backup
copies it again. Run `sync-stats` by hand, and preview first.

Recovery limits:

- `stats.json` and `stats.json.previous` are the only copies of favorites and
  timestamps outside Navidrome. Keep both.
- Drop guard: `nd stats` compares the new export with the current valid
  `stats.json`. If the song, rated (rating > 0), or starred count falls by more
  than 10% (`MAX_DROP_FRACTION`), it refuses and names the count, the old
  value, and the new value. Nothing is rotated or written. Pass `--force` to
  replace the snapshot after confirming the drop is real. A drop of exactly
  10% is allowed.
- Corrupt current file: a `stats.json` that does not parse as a snapshot is
  moved to `stats.json.invalid`, not rotated, so `stats.json.previous` keeps
  the last valid snapshot. A read error (for example a transient NAS failure)
  refuses the export and changes nothing.
- `write-stats` cannot restore the snapshot to Navidrome. `nd set-rating`
  reads only the legacy `ratings.json`, and play counts are never written
  back to Navidrome.
- Ratings and counts that other players wrote are not imported into
  Navidrome. The writer keeps them unless `--clear` or `--allow-decrease` is
  passed.

## Lyrics

`lyrics` is independent of tagging. `apply`, `fix-album`, and `rip-album` do
not call it, and `apply` does not depend on LRCLIB. The `lyrics-album` skill
drives it. An LLM makes the per-track judgment; no person has to choose a
record.

```bash
uv run music-tagger lyrics <album-dir>                        # inspect; no network, no writes
uv run music-tagger lyrics <album-dir> --lookup               # candidates; read-only
uv run music-tagger lyrics <album-dir> --fetch --dry-run      # plan the writes
uv run music-tagger lyrics <album-dir> --fetch                # write sidecars
uv run music-tagger lyrics <album-dir> --fetch --replace      # replace existing lyrics
uv run music-tagger lyrics <album-dir> --track 3 --lookup --lyrics-id 12345
uv run music-tagger lyrics <album-dir> --track 3 --fetch --lyrics-id 12345
uv run music-tagger lyrics <album-dir> --embed --dry-run      # preview the tags
uv run music-tagger lyrics <album-dir> --embed                # write sidecar text into tags
uv run music-tagger lyrics <album-dir> --embed --replace      # replace a different tag value
```

Flags that conflict are rejected with exit 2 before any work starts:

| Flags | Rejected because |
| --- | --- |
| two of `--lookup`, `--fetch`, `--embed` | one mode per run |
| `--dry-run` without `--fetch` or `--embed` | inspect and lookup never write |
| `--replace` without `--fetch` or `--embed` | there is nothing to replace |
| `--lyrics-id` without `--fetch` or `--lookup` | it selects an LRCLIB record |
| `--lyrics-id` without `--track` | a record must not apply to the whole album |
| `--embed` with `--lyrics-id` | embedding reads local sidecars only |
| `--disc` without `--track` | the disc qualifies a track number |

### Embedding (`--embed`)

A backup of the sidecar text in the audio file, for when sidecars are lost. It
makes no network calls. Each track has one source: its `.lrc` when that holds
lyrics, else its `.txt`. Never both.

- FLAC `LYRICS` and M4A `©lyr`: the text as-is. LRC timestamps stay in the text.
- MP3 from `.lrc`: `SYLT`, language `eng`, empty descriptor, milliseconds.
- MP3 from `.txt`: `USLT`, language `eng`, empty descriptor.

Within an MP3, the managed `eng` frame is one of `SYLT` or `USLT`. Writing one
removes the other. Other languages and descriptors, every other tag, and the
ID3 version are kept. A different value in the managed slot needs `--replace`;
without it the track is `preserved`. An identical value is `unchanged`. A
malformed sidecar is `refused` and exits 1. A file the tool cannot read is
`refused`, and a write error is `failed`. An MP3 with an ID3v2.2 tag is `failed`
and left unchanged: mutagen writes only v2.3 and v2.4, and converting would rewrite
every frame. Embedded text is hidden in Navidrome
while a sidecar exists, and an edited sidecar drifts from its embedded copy
until `--embed --replace` runs again.

### Sources and classification

- Embedded: FLAC `LYRICS`, MP3 `USLT` (each language and description is
  reported separately), M4A `©lyr`.
- Sidecars: `<audio-stem>.lrc` (synchronized) and `<audio-stem>.txt` (plain),
  matched by exact basename, including the disc prefix and Unicode. Other
  `.txt` files in the album are never lyric sidecars.
- Each source is `synchronized`, `plain`, `empty`, or `malformed`. Malformed
  means: not UTF-8 (a BOM is accepted), a timestamp out of range, or a sidecar
  whose content does not match its extension (untimed text in `.lrc`, timed
  text in `.txt`). A track is `missing` when it has no lyric source, and
  `unreadable` when its audio or a sidecar cannot be read.
- Navidrome 0.63's default `LyricsPriority` is
  `.ttml,.yaml,.yml,.elrc,.lrc,.srt,.txt,embedded` (release notes for v0.63.0).
  So a sidecar wins over embedded lyrics, `.lrc` wins over `.txt`, and `.txt`
  is read. A sidecar is found by its audio path with the extension replaced
  (`sidecarRelPath := path[:len(ext)] + suffix`, an exact match in
  `core/lyrics/sources.go`). The server here is 0.64.2, so re-check the default
  when it changes.

### Selection

- LRCLIB is used for fetching only. Nothing is published back.
- Candidates come from `/api/search` with artist, title, and album. When that
  search reaches the 20-record cap, the same artist and title are searched
  without the album filter, and the two results are merged by record ID. The
  broader search only adds candidates; if it is not capped, the merged set is
  complete for that artist and title. If the merged search is still capped,
  `/api/get` is asked once for the record with the track's own title, artist,
  album, and duration, and that record is added as a candidate. It is never
  chosen automatically: the outcome stays `review`. `/api/get` is otherwise
  used only for an explicit `--lyrics-id`.
- A record is chosen automatically only when exactly one candidate matches the
  artist and title, lies within 2 s of the audio's duration, and matches the
  album, and whose record has synchronized text. A matching record with plain text
  only is `review`: its timing cannot be checked against the audio. Version terms
  are material: `Song (Live)` does not match `Song`.
- Otherwise the outcome is `ambiguous`, `no_match`, or `review`, and the
  evidence for each candidate is printed. The LLM operator judges these from
  that evidence and passes its chosen record with `--lyrics-id`. Each candidate
  line says what the record offers: synchronized text, plain text,
  instrumental, or no text. A search that returns the 20-record
  cap is `review`, because the match may have been cut off.
- The chosen record's synchronized text is preferred, then its plain text.
  Texts from different records never mix.
- An instrumental record creates no file (`instrumental`). A matched record
  with no usable text is `no_text`.
- Requests have explicit timeouts and are spaced 0.35 s apart (about 170/min, under the registered app's ~180/min
  allowance). HTTP 429 and
  5xx responses are retried, honoring `Retry-After` up to 60 s. A longer wait
  fails at once as `rate_limited`. The User-Agent names the project; the
  operator's email is not sent. Every request also sends the public LRCLIB
  Application ID (`Lrclib-App-Id`, `APP_ID` in `lrclib.py`), which the
  developer portal registers for this app. It is not a credential.

### Writes (`--fetch`)

- Synchronized text goes to `.lrc`, plain text to `.txt`. Files are UTF-8
  without a BOM, end with one newline, are validated before writing, and are
  replaced atomically.
- Existing lyrics are kept by default. That includes a plain lyric when a
  synchronized candidate becomes available. An empty sidecar is overwritten; a
  malformed one is kept.
- `--replace` overwrites the target. It also removes the other-type sidecar of
  the same basename. The plan shows that removal, and the log records it. No
  other `.txt` file is ever removed. Embedded lyrics are not changed in the
  file, but Navidrome shows the sidecar in their place, so they are hidden
  while the sidecar exists.
- Identical text is `unchanged`, and no file is touched.
- The first release writes line-timed LRC only. It does not write word timing,
  translate, or synthesize lyrics.
- `--dry-run` shows each plan and writes nothing.

### Outcomes and exit codes

- Track labels: `selected`, `instrumental`, `no_text`, `no_match`,
  `ambiguous`, `review`, `missing_metadata`, `rate_limited`,
  `provider_failure`. With `--fetch`, each selected track is also labelled
  `wrote`, `would write`, `preserved`, `unchanged`, `refused`, or `failed`.
- Exit 0: every requested step completed. No-match and instrumental outcomes
  are reported and do not fail the run.
- Exit 1: provider failure, rate limit, unreadable input, refused text, or a
  write error.
- Exit 2: a usage error (see the table above).
- Exit 3: `--fetch` left a track for judgment (`ambiguous`, `review`, or
  `missing_metadata`). Other tracks are still written. The LLM operator
  decides those tracks with `--lyrics-id` or leaves them without lyrics.

### Batch (`lyrics-batch`)

```bash
uv run music-tagger lyrics-batch "/mnt/synology/media/music/<Artist>" --dry-run
uv run music-tagger lyrics-batch "/mnt/synology/media/music/<Artist>"           # sidecars + pending
uv run music-tagger lyrics-batch "/mnt/synology/media/music/<Artist>" --auto --embed  # the usual run
```

- Per track, using the same selection rules as `lyrics --fetch`:
  - Status `synchronized` or `plain` (sidecar or embedded): kept, not searched.
  - A recorded decision: not searched.
  - Status `missing` or `empty`: searched. `selected` is written as a sidecar.
    `instrumental` and `no_text` are final. `no_match` is final unless a record
    matches the album; those records are saved as pending. `ambiguous`, `review`,
    and `missing_metadata` are saved as pending, with every candidate.
  - Status `malformed` or `unreadable`: saved as pending with outcome
    `needs_inspection` and no candidates. Never overwritten.
- `--auto` records the choices the rules accept, after each album's fetch and before
  its embed. The rules are `confident_choice` in `lyrics_batch.py`: (1) a plausible
  album match with synchronized text, closest length first; (2) an album match with
  matching artist and title, synchronized text, length within 5 s, and last synced
  line inside the audio. A pending entry saved before the `identity_matches` field
  reads as not matching, so rule (2) skips it. Plain text is never auto-chosen.
  Everything else stays pending for `lyrics-decide`.
- `--embed` runs after the fetch pass, for each track that has a sidecar. It never
  replaces a different tag value.
- Pending file: `~/Projects/music-tagger/lyrics-pending.json`, or
  `$MUSIC_TAGGER_LYRICS_PENDING`, or `--pending`. Each real run replaces the entries
  of the albums it processed. Albums it could not read, or did not reach after a
  rate-limit stop, keep their earlier entries. Only commands write the file.
- A track with lyrics on disk is never searched again. `no_match` tracks are
  searched on every rerun.
- One LRCLIB client serves the run. A rate limit that LRCLIB will not clear within
  the client's wait stops the run at that track. The run exits 1.
- Exit: 0 when nothing is pending and nothing failed; 3 when tracks are pending and
  nothing failed; 1 on any failure (provider outage, refused or failed write,
  unreadable album, rate-limit stop). A dry run exits the same way and writes no
  sidecars, tags, log lines, or pending file.
- Log: a `## Lyrics` section and, with `--embed`, a `## Lyrics embed` section per album.

### Decisions (`lyrics-decide`)

```bash
uv run music-tagger lyrics-decide list --album "<album-dir>"
uv run music-tagger lyrics-decide choose "<album-dir>" --track 1 --id 18895421 --dry-run
uv run music-tagger lyrics-decide choose "<album-dir>" --track 1 --id 18895421
uv run music-tagger lyrics-decide skip "<album-dir>" --track 1 --reason "no record exists"
uv run music-tagger lyrics-decide skip "<album-dir>" --file "01. Star Wars_ Main Title.flac" --reason "no record exists"
```

- Decisions and pending entries are keyed by the file's MusicBrainz release track
  ID (`musicbrainz_releasetrackid`) when the file has one, else by album and file
  name. An entry with an ID matches the track with that ID after a rename or a move;
  a batch run then updates its stored album and file (real runs only). Entries
  written before IDs match by album and file, with no separate migration.
- `list` prints each undecided pending track with its candidates: ID, title,
  artist, album, duration difference, flags, and what the record offers. Each
  header names the audio file.
- `--file NAME` selects the track by its audio file name, exactly as `list`
  prints it, and works when two files share a track number or a file has no
  track tag. `--track` and `--disc`, when also given, must agree with the file.
  Decisions are keyed by file, so a shared number never hides another file.
- `choose` writes the record's lyrics through the `lyrics --fetch --lyrics-id`
  path. It refuses, with exit 2, an ID that was not saved, a track with no
  pending entry, and a track already decided. `--replace-skip` lets a choice replace
  an earlier skip for that track; a recorded choice is never replaced. `--plain`
  writes the record's plain text as a `.txt`, even when the record also has synchronized
  text, for a track whose audio no longer matches the record's timing. It records the
  decision only when the sidecar was written or already matched. A failed write exits 1
  and records nothing.
- Each candidate shows where its last synced line falls against the audio end. A
  record whose last line runs past the end does not fit this file. One whose last
  line falls inside the audio may fit, even when its total length differs.
- `skip` requires a non-empty reason and writes no file.
- Decisions: `~/Projects/music-tagger/lyrics-decisions.json`, or
  `$MUSIC_TAGGER_LYRICS_DECISIONS`, or `--decisions`. A choice cannot be changed
  through `lyrics-decide`; a skip can be replaced by `choose --replace-skip`.
  `lyrics --track N --fetch --lyrics-id ID --replace` still replaces a sidecar.
- Decisions are not written to `changes.log`. A `choose` that writes a sidecar
  logs that write, as `lyrics --fetch` does.

### Logging

`--fetch` and `--embed` append to the change log by default, one line per write or
removal: the LRCLIB record ID, the destination, and whether existing lyrics
were replaced. It never logs lyric text. `--dry-run`, `--lookup`, and
inspection write no log. `lyrics-batch` follows the same rule.

### Limits

- Navidrome's built-in web player does not show external `.lrc` sidecars
  (navidrome/navidrome#4148, open; the maintainer says it will not be fixed in
  the current UI). Whether it shows external `.txt` is not confirmed. Users
  report that it shows only embedded lyrics. Accepted for now: sidecar lyrics
  are checked in other clients (Symfonium), not the web player.
- The server serves structured lyrics through the OpenSubsonic v2 lyrics
  extensions (release notes for v0.63.0). A test on the live server returned
  synchronized lines for tracks whose lyrics come from another source, but not
  yet for a sidecar: the sidecar entry was not reachable through the API.
- Symfonium displays the sidecar lyrics on the live library (confirmed by the
  user, 2026-11-03). Feishin is not tested. Offline behavior is not verified.
  Do not claim offline lyrics until B6 records a result.

## Tag mapping details

- **Multi-valued fields.** `MULTI_VALUE_FIELDS` (`tags.py`) holds one value per
  credited person: `artists`, `albumartists`, `musicbrainz_artistid`,
  `musicbrainz_albumartistid`, the role names (`composer`, `lyricist`, `producer`,
  `engineer`, `mixer`, `conductor`, `remixer`) and their `musicbrainz_*id` fields, and
  `releasetype`. `TrackTags.multi` holds every value in file order; `tags[field]` holds
  the first. `ARTIST` and `ALBUMARTIST` stay the single display credit, and the review
  gate does not cover multi fields. A multi change compares whole lists, in order; its
  `old_value` and `new_value` are the values joined with `; ` for display. An empty
  list deletes the field. Containers:

  | Container | How values are stored | Keys |
  | --- | --- | --- |
  | FLAC (Vorbis) | The field repeated | `ARTISTS`, `ALBUMARTISTS`, `MUSICBRAINZ_ARTISTID`, … |
  | MP3, ID3v2.4 | One frame, values null-separated | `TXXX:ARTISTS`, `TXXX:ALBUM ARTISTS`, `TXXX:MusicBrainz Artist Id`, role frames such as `TCOM` |
  | MP3, ID3v2.3 | No multi-value support | Saved as v2.4 on write, so v2.3 files are upgraded |
  | M4A | Not read | `write_tags` does not support M4A |

  `TXXX:ALBUM ARTISTS` follows the spelling in the design table; it could not be checked
  against Navidrome's mapping offline.
- `date` (FLAC `DATE`, MP3 `TDRC`) is the recording date — never overwritten
  by the MB release date. When empty, populated from the release group's
  `first_release_date`.
- FLAC `totaldiscs` is read from `DISCTOTAL`, else `TOTALDISCS` (whipper writes
  only `DISCTOTAL`, counting every medium of the release, DVDs included; a stale
  `TOTALDISCS` never hides it). A write
  also sets `DISCTOTAL` when the file has one, and a clear removes it, so the two
  never disagree. Navidrome's `mappings.yaml` lists `disctotal` before
  `totaldiscs`. A multi-disc album, for `rename` and `place`, is one whose
  tracks state a disc number above 1, a total above 1, or a `n/total` disc number.
- `releasedate` (FLAC `RELEASEDATE`, MP3 `TDRL`) gets the MB release's date.
- Retagging a file that carries another release's `musicbrainz_albumid` clears
  the release-owned fields (`RELEASE_OWNED_FIELDS`, and for a matched track
  `TRACK_OWNED_FIELDS`) that the new release does not supply. A clear is a
  `TagChange` with an empty `new_value`; `write_tags` deletes the field (FLAC key,
  every matching MP3 frame). `tag` shows it as `field: old → (cleared)`. Clears
  honor locks. A clear of a reviewed field (title, artist, album, albumartist,
  date) is never automatic: those fields are in neither owned set, and
  `overwrites_needing_review` flags any clear of a non-empty reviewed value.
- `releasestatus` is compared case-insensitively (`official` == `Official`).
- Unicode from MusicBrainz (e.g. U+2010 HYPHEN in titles) is accepted as
  canonical.
- `MUSIC_TAGGER_LOCKED` (FLAC comment, MP3 `TXXX`) holds the locked field
  names, one value each. It is read into `TrackTags.locked_fields`, not
  `tags`. `apply_changes` re-reads the locks from each file before writing,
  so a stale or hand-edited diff JSON cannot bypass them. M4A raises
  `NotImplementedError`, because `tag` cannot write M4A either.
- `isrc` identifies a recording. MusicBrainz can list several ISRCs for
  that recording; their order does not identify the code on this CD.
  The client retains the full `MBTrack.isrcs` list. The tagger applies this
  policy:

  | File ISRC | MusicBrainz list | Action |
  | --- | --- | --- |
  | Present in the list | Any length | Preserve it |
  | Missing or empty | Exactly one code | Fill it |
  | Missing or empty | Several codes or none | Leave it empty |
  | Present, absent from a non-empty list | One or several codes | Report a conflict and block writes |
  | Present | Empty list | Preserve it and report it as unverified |

  Absence from MusicBrainz does not prove a code read from the CD is wrong.
  Unresolved conflicts block **every track in the album**, including
  recording-ID changes. Unverified codes produce warnings and do not block
  writes. Conflicts and warnings survive diff JSON export and import.

### Resolve an ISRC conflict

`tag` resolves conflicts from flags:

- `--isrc keep` keeps every file code and locks `isrc` on those tracks, so
  the conflict does not return on the next run.
- `--isrc musicbrainz` takes MusicBrainz's code where it lists exactly one.
  Tracks with several codes stay unresolved.
- `--isrc-code [DISC-]TRACK=CODE` picks a code for one track and overrides
  `--isrc` for it. The code must be the file's code or a MusicBrainz code.

The diff JSON path (`read` → `diff` → `write-tags`) enforces the same gate as
`tag`: review overwrites exit 3 unless `write-tags --approve-overwrites`, and
unresolved ISRC conflicts exit 4. Dry runs print the verdict and exit with its
code. Before any write it re-reads each file the diff changes; if a change's
`old_value` no longer matches the file, the whole run exits 1 and writes nothing.
The diff JSON path still honors a `resolution` set on each conflict, and a kept resolution locks `isrc` there
too. Do not add a duplicate ISRC entry to `changes`; the resolution
generates the required change. Old release JSON containing only a scalar
`isrc` must be fetched again: that value may have been selected from a
longer list.

## Format differences that bite

- **Multi-value semantics differ by container.** MP4's `©gen` is natively
  list-valued, same as Vorbis comments. MP3 is the awkward one: a single
  `TCON` frame with null-separated values, which only works in ID3v2.4.
- **Types, not keys, are the trap.** MP4 `trkn` is `[(1, 3)]` and `----`
  freeform atoms return `MP4FreeForm` bytes; neither fits the `dict[str, str]`
  contract. `MP4_MAP` therefore covers only string-valued atoms. `trkn` and
  `disk` are decoded separately into `tracknumber`/`totaltracks` and
  `discnumber`/`totaldiscs`, skipping zero numbers.
- **Audit every consumer before widening `AUDIO_EXTENSIONS`.** Adding `.m4a`
  reached `replaygain.py` (fine — `rsgain` supports M4A) and `embed_cover_art`
  (skips M4A, no `covr` support). Anything that globs the shared constant
  inherits the new format whether or not it can handle it.

## Never return an empty change list for an unhandled case

Three bugs of the same shape surfaced in a single session: a code path returns
an empty result for a case it does not handle, and the caller counts that
identically to "nothing needed doing."

- `write_rating_to_file` returned `[]` for M4A, so a full export reported
  success having written nothing.
- `read_album` accepting M4A made `tag` *appear* to work while `write_tags`
  fell through. It now raises.

**Report unsupported input explicitly** — "N files skipped, unsupported
format" — and never let "unsupported" and "already correct" produce the same
output. `write_tags` raising `NotImplementedError` for M4A is the pattern.

## Safety

- **Whipper's `.m3u` must never reach the library.** `compute_placement()`
  once copied it because `.m3u` was in `_NON_AUDIO_EXTENSIONS`, and Navidrome
  imported each one as a junk playlist. It was dropped from that set in commit
  `85f2584` with a comment explaining why; do not re-add it.
- **Every mutating album command logs by default.** For a long time the
  skills had to pass `--log` and did not: `changes.log` had 448 sections and
  zero `## Genre:` entries, so no genre change had an audit trail. `tag`,
  `write-tags`, `write-ratings`, `write-stats`, `genre`, `rename`, `lock`,
  `unlock`, `art`, `replaygain`, `copy`, `place`, `lyrics --fetch` or `--embed`,
  `lyrics-batch` (without `--dry-run`), and `lyrics-decide choose` now append to
  `$MUSIC_TAGGER_LOG`, else `~/Projects/music-tagger/changes.log`. `--log PATH` redirects; `--no-log`
  disables. Tests point `MUSIC_TAGGER_LOG` at a temp file (autouse fixture
  in `tests/conftest.py`); never remove that fixture.
  A command that fails partway still logs what it changed. `tag` and
  `write-tags` log each track right after it is written. `genre`, `art`,
  `lock`, `unlock`, and `copy` write their summary from a `finally` with the
  count done so far (`art` adds a `Failed:` line). `rename` reverts a failed
  rename, so it logs nothing; if a revert itself fails, it logs what is on disk.
- `scan` and `candidates` are read-only. Safe to run directly against the
  live library.
  An audio file that cannot be read is listed in `AlbumTags.unreadable` and
  never dropped. So is an audio-suffixed entry that is not a regular file, such
  as a directory or a broken symlink. `tag`, `apply`, and `place` print
  `Warning: N unreadable file(s): …` and exit 1; `rename`, `candidates`, and
  `scan` only warn.
- `tag --dry-run` is read-only. Safe to run directly against the live library.
- `tag` (without `--dry-run`) writes metadata into files. It does not move,
  rename, or delete files. Use `--dry-run` first, review the diff, then run
  without it.

## Environment setup

**System packages:**
- `whipper` — CD ripping with AccurateRip verification (for `rip`)
- `libdiscid-dev` — disc ID computation (required by Python `discid` package)
- `rsgain` — ReplayGain 2.0 loudness scanning and tagging (for `replaygain`)

**Navidrome credentials** (for `nd rescan`):
Set these environment variables (e.g. in `.env` or shell profile):
- `NAVIDROME_URL` — Navidrome server URL (e.g. `http://localhost:4533`)
- `NAVIDROME_USER` — Navidrome username
- `NAVIDROME_PASSWORD` — Navidrome password

Store the password **unquoted** in `.env` — most loaders treat quotes as
literal characters. The plaintext never goes over the wire: `_auth_params`
generates a random salt per request and sends `t=md5(password+salt)&s=<salt>`,
hashing fresh each call.

**The CLI does not load `.env` itself.** Only the `just` recipes do
(`set dotenv-load := true`), so a bare `music-tagger nd ratings` fails with
"Set NAVIDROME_URL…". Use the recipes.

## Operations

All checks go through `just` (see the project-standards skill for the verb
contract). The pre-commit hook and CI call these same recipes.

```bash
just sync         # install dependencies
just lint         # ruff check + ruff format --check (read-only)
just format       # ruff format + ruff check --fix (mutating)
just type-check   # mypy strict over the package
just test         # pytest with the coverage gate
just check        # everything CI runs: lint + type-check + test
just hooks-install  # install bin/pre-commit.sh into this clone
```

The coverage gate sits at 93%, the current floor, so coverage can only
ratchet upward. The standard is 90%; raise the gate as coverage rises.

Test fixtures are small FLAC/MP3 files in `tests/fixtures/` with known tags.
Tests copy them to `tmp_path` so mutations don't affect the fixtures.
