# music-tagger: Long-Term Plan

## Vision

One system covering the full life of a track, with Claude Code as the
operator at every judgment point:

```
CD ──whipper──▶ rip dir ──identify──▶ tagged FLAC ──place──▶ /mnt/synology/media/music ──▶ Navidrome
                              ▲                                                              │
                              │ (LLM picks the MB release)                                    │
                                                                    ratings sync ◀───────────┘
                                                                    (Navidrome ⇄ file tags)
```

The hard problem in every tagging tool is release identification: a rip
matches several MusicBrainz releases (US vs EU pressing, remaster, reissue)
and a human must weigh durations, barcodes, catalog numbers, and liner-note
knowledge. Here the LLM provides that judgment. The tool's job is to gather
and present evidence in machine-readable form, never to decide.

## Design Principles (the LLM interface)

**Unix philosophy: small orthogonal tools, composed by the operator.**
The unit of design is the primitive, not the pipeline. A monolithic
"ingest this directory and do everything" command only handles the
situations we anticipated; a set of primitives lets the LLM string
together workflows we never envisioned and fix problems mid-stream (bad
TOC file → fall back to text search; wrong release chosen last month →
re-fetch and re-diff; one track's tags mangled → read, inspect, write just
that field). Every workflow in this plan is a *composition* the LLM
performs in conversation, not a command the tool hardcodes.

Concretely:

1. **One job per command.** Each subcommand does one thing: read evidence,
   query one source, compute one diff, execute one write. If a command's
   description needs the word "then", split it.
2. **Files are the pipe; stdout is the digest.** Every evidence command
   writes its full JSON to a file (`--out`, defaulting into a per-album
   workdir) and prints a compact digest to stdout sized for
   decision-making — a few lines per candidate, not the payload.
   Downstream commands take `--from <file>`. Full JSON enters the LLM's
   context only when the digest is ambiguous and it chooses to look.
   Intermediates can still be inspected, edited, or hand-built when
   reality doesn't match the happy path — by reference, not by value.
   Side benefit: persisted MB responses are a cache; retrying a step
   doesn't re-fetch at 1 req/sec.
3. **Non-interactive.** All input via arguments/stdin; no prompts, no
   TUIs. Exit codes are meaningful.
4. **Evidence out, decision in.** Commands gather and present *everything*
   relevant (durations, disc IDs, barcodes, catalog numbers, existing
   tags, match scores). The LLM reads, judges, and passes its decision
   (e.g. `--release-id`) to the next command. Scores are hints to rank by,
   never auto-selection.
5. **Read and write are separate commands.** Reads are always safe to run
   against the live library. Writes take a computed plan (a diff, a move
   mapping) as input, support `--dry-run`, append to an audit log, and are
   idempotent.
6. **Never destroy.** Tag writes modify metadata in place. File moves are
   copy-verify-then-report; deleting the source is a separate explicit
   step. Never overwrite an existing library file.
7. **Convenience wrappers are thin.** If a common composition earns a
   shortcut (as today's `candidates` = read + search + score), it must be
   a transparent composition of primitives that also exist standalone —
   never the only way to reach the underlying capability.
8. **Context economy: big payloads are machine-to-machine.** The data the
   LLM judges with is small by nature (match stats, a diff, a move
   mapping); the data that is big (full release detail with credits, full
   tag dumps, library-wide ratings) exists only to feed other commands.
   Keep them on separate channels: digests in context, payloads in files.
   A command's digest must contain everything needed for the *usual*
   decision, so reading the full JSON is the exception. The two payloads
   the LLM should always read whole are `diff` output and `path-for`
   output — the write plans — and both are compact by construction.

## Current State (July 2026)

**Phase 1 complete.** The full primitive set from the plan is implemented and
tested. Every command emits JSON (to stdout or `--out`), prints a compact
digest when writing to a file, and accepts input from prior commands via
`--from`/`--evidence`/`--release`/`--diff` file arguments.

Implemented primitives:
- `read <album-dir>` — per-track tags, durations, formats as JSON evidence.
- `toc <rip-dir>` — CUE sheet parser, disc ID computation.
- `mb search`, `mb release`, `mb discid` — all MB query paths.
- `match --evidence --candidates` — duration-match scoring.
- `diff --evidence --release` — per-track field-level diff (the write plan).
- `write-tags --diff` — applies tag changes, supports `--dry-run` and
  `--log`.

Thin wrappers reimplemented as compositions of primitives:
- `candidates <dir>` ≡ `read` + `search_candidates` + `score_candidates`.
- `tag <dir> --release-id` ≡ `read` + `mb release` + `diff` + `write-tags`.

All previous composability gaps are resolved. LLM-edited diffs work:
`diff` → hand-edit the JSON → `write-tags --diff edited.json`.

In progress: working through `checklist.json` album by album to fix
Navidrome album splits. This continues as Phase 0 and is unaffected by the
rest of the plan.

Library conventions (observed, keep):
- Path: `Artist/Album Title (Year)/NN. Track Title.flac`
- Root: `/mnt/synology/media/music`, ~250 artist dirs
- Navidrome at `http://192.168.1.10:4533` (Subsonic API);
  SQLite at `/volume1/docker/navidrome/data/navidrome.db` on the NAS

## The Primitive Set

The target CLI, grouped by what they touch. Local reads and MB queries are
always safe; writes are explicit and logged.

### Local file evidence (read-only)

| Command | In | Out (JSON) |
|---|---|---|
| `read <album-dir>` | path | per-track tags, durations, formats — the full evidence dump |
| `toc <rip-dir>` | path | MB disc ID + TOC offsets parsed from whipper `.toc`/`.log` |
| `scan <root>` | path | library-wide report: split/missing MBIDs (later: missing art, disc ID, rating backup) |

### MusicBrainz queries (read-only, rate-limited)

| Command | In | Out (JSON) |
|---|---|---|
| `mb search --artist --album [--format]` | text seeds | candidate releases (search result depth) |
| `mb discid <discid> [--toc <offsets>]` | disc ID and/or TOC | exact or fuzzy-TOC matched releases |
| `mb release <uuid>` | release ID | full release detail (tracks, credits, everything `fetch_release` gets) |

### Pure computation (no I/O)

| Command | In | Out (JSON) |
|---|---|---|
| `match --evidence <json> --candidates <json>` | read output + any candidate list | candidates annotated with duration-match stats (tracks within 2s, max deviation, count match) |
| `diff --evidence <json> --release <json>` | read output + release detail | per-track field changes — the write plan |
| `path-for --evidence <json>` | tagged album evidence | computed library destination mapping (source → dest per file) |

### Writes (dry-run + audit log + idempotent)

| Command | In | Effect |
|---|---|---|
| `write-tags --diff <json>` | a diff (from `diff`, possibly LLM-edited) | applies tag changes to files |
| `copy --plan <json>` | a move mapping (from `path-for`, possibly edited) | copy to library, verify size+hash, refuse existing destinations |
| `art <album-dir> --release-id <uuid>` | release ID | fetch Cover Art Archive front image → `cover.jpg` |

### Navidrome (Subsonic API)

| Command | In | Out / Effect |
|---|---|---|
| `nd ratings` | — | JSON: all rated/starred songs → `{path, rating, starred, starred_at}` |
| `nd set-rating --from <json>` | ratings JSON | push ratings/stars to Navidrome (restore path) |
| `nd rescan` | — | trigger `startScan` |

Ratings land in files via the same tag primitives: `nd ratings --out
ratings.json` then `write-tags --from ratings.json --ratings` (a mode
mapping rating → `FMPS_RATING`/`RATING`/`POPM`). No dedicated "ratings
sync" monolith, and the library-scale JSON never transits the LLM —
digests report counts and a sample.

### Thin wrappers (compositions, kept for ergonomics)

- `candidates <dir>` ≡ `read` + (`toc`→`mb discid` if rip artifacts exist,
  else `mb search`) + `match`. Gains `--json`.
- `tag <dir> --release-id X` ≡ `mb release` + `diff` + `write-tags`.
  Current behavior, reimplemented over the primitives.

Example compositions the primitives enable *without new code* — the test
for whether the decomposition is right:

Each command below prints a short digest the LLM reads; the full JSON
lands in the workdir (`.mt/` beside the album, say) and later commands
reference it by path. Only the digests — and the write plans — enter
context.

```
# Normal rip ingest (LLM sees: disc ID found, 3 candidates w/ match stats, diff)
toc ~/rips/disc1                          → .mt/toc.json      (digest: disc ID, offsets)
mb discid --from .mt/toc.json             → .mt/cands.json    (digest: 1 line/release)
match --evidence .mt/read.json --candidates .mt/cands.json    (digest: stats/candidate)
→ LLM picks → mb release <uuid> → diff → LLM reviews diff → write-tags
→ art → path-for → LLM reviews mapping → copy → nd rescan

# Whipper's TOC file is corrupt
read ~/rips/disc1 (digest: titles+durations) → LLM eyeballs → mb search → as normal

# MB has the wrong duration for one track; LLM decides it's fine
diff … → LLM edits .mt/diff.json to drop the spurious change → write-tags --from it

# Re-check an album tagged last year against MB (data may have improved)
read <album>; mb release <its-mbid>; diff → review drift (only the diff read)

# Disaster recovery after server rebuild
read each album → extract rating tags → nd set-rating --from <file>
(library-scale JSON never enters context; LLM sees counts + spot checks)

# One-off fix: normalize a single field across an artist
scan --all → LLM filters digest → read each → LLM builds minimal diffs → write-tags
```

## Order of Work

1. **Phase 0** (ongoing): finish the split-fix checklist. No code changes.
2. ~~**Phase 1 — decompose and add disc ID path**~~: **Done.** All
   primitives implemented and tested. See Current State above.
3. **Phase 2 — placement**: `path-for`, `copy`, `art`, `nd rescan`.
   Completes rip → library.
4. **Phase 3 — ratings**: `nd ratings`, ratings→tag mapping in
   `write-tags`, `nd set-rating`. Scheduled export via cron/routine, with
   a state file so unchanged files aren't rewritten (mtime churn would
   retrigger Navidrome scans).
5. AcoustID fingerprinting (`fpcalc` → `mb` equivalent for
   `api.acoustid.org`) and `scan` extensions as need arises.

## Placement: design and implementation notes (Phase 2 detail)

Phase 2 takes a tagged rip directory and places it into the library. Four
new commands, each independent:

### `path-for --evidence <json>`

Pure computation. Reads the album evidence JSON (from `read`) and computes
the destination path for each file using the library convention:

```
/mnt/synology/media/music/Artist/Album Title (Year)/NN. Track Title.flac
```

**Inputs:** evidence JSON (has tags, paths, formats).

**Output:** JSON move mapping — a list of `{source, dest}` pairs. The LLM
reviews this before `copy` runs.

**Rules:**
- Artist: `albumartist` tag, falling back to `artist`. Sanitize filesystem
  characters (`/`, `\0`; `:` → ` -`; leading/trailing dots/spaces stripped).
- Album dir: `album (date)` where `date` is the 4-digit year from the
  `date` tag. If no date, omit the parenthetical.
- Filename: `NN. Title.ext` where NN is zero-padded `tracknumber`. For
  multi-disc albums (any track has `discnumber` > 1): `D-NN. Title.ext`.
- Extension preserved from source file.
- `--root` flag to override the default library root (for testing).
- Non-audio files in the source dir (`.cue`, `.log`, `.toc`, `cover.jpg`)
  are included in the mapping as-is, placed into the same album dir.

**Digest (stdout):** Artist — Album (Year), N files → dest dir.

### `copy --plan <json>`

Copies files from source to destination per the move mapping.

**Safety:**
- Refuses to overwrite existing destination files (exit code 1, report
  which files already exist). The LLM resolves conflicts.
- Creates destination directories as needed.
- Verifies each copy: compare file size and SHA-256 hash of source and
  dest after copying. If verification fails, delete the bad copy and
  report the failure.
- `--dry-run`: print the plan without copying.
- Appends to `--log` if provided.
- Does NOT delete source files. Source cleanup is a separate explicit
  step the LLM decides.

**Digest:** N files copied (M bytes), dest dir.

### `art <album-dir> --release-id <uuid>`

Fetches the front cover image from the Cover Art Archive and writes it to
`<album-dir>/cover.jpg`.

**Implementation:**
- Hit `https://coverartarchive.org/release/<uuid>/front-500` (500px is
  enough for Navidrome thumbnails; full-size `front` as a `--full` flag).
- If no cover art exists (404), report cleanly and exit 0.
- If `cover.jpg` already exists, skip unless `--force`.
- Rate-limit: CAA has no documented rate limit, but be polite — 1 req/sec
  reuse the existing rate limiter.

**Digest:** Saved cover.jpg (NxN, M KB) or No cover art available.

### `nd rescan`

Triggers a Navidrome library scan via the Subsonic API.

**Implementation:**
- `POST /rest/startScan` with Subsonic auth params.
- Auth: `u=<user>&t=md5(password+salt)&s=<salt>`, credentials from
  `NAVIDROME_URL`, `NAVIDROME_USER`, `NAVIDROME_PASSWORD` env vars.
- Report success/failure. No output file needed.

**New module:** `src/music_tagger/navidrome.py` for Subsonic API client.
Shared by `nd rescan` (Phase 2) and `nd ratings`/`nd set-rating` (Phase 3).

### Implementation order within Phase 2

1. `path-for` — pure computation, no I/O, easy to test.
2. `copy` — file I/O with verification.
3. `art` — HTTP + file write, simple.
4. `nd rescan` — Subsonic client scaffold.

Each is a standalone commit with tests. The `navidrome.py` module is
introduced with `nd rescan` and extended in Phase 3.

### Example composition (rip → library)

```
read ~/rips/eagles-desperado                    → .mt/read.json
toc ~/rips/eagles-desperado                     → .mt/toc.json
mb discid --from .mt/toc.json                   → .mt/cands.json
match --evidence .mt/read.json --candidates .mt/cands.json
→ LLM picks release → mb release <uuid>        → .mt/release.json
diff --evidence .mt/read.json --release .mt/release.json → .mt/diff.json
→ LLM reviews diff → write-tags --diff .mt/diff.json
art ~/rips/eagles-desperado --release-id <uuid>
path-for --evidence .mt/read.json               → .mt/plan.json
→ LLM reviews mapping → copy --plan .mt/plan.json
nd rescan
```

## Ratings: research findings and policy (Phase 3 detail)

Verified July 2026:

- Navidrome **never writes to files** and has **no native import** of
  embedded rating tags into user ratings (open discussion navidrome#4957).
- The plugin system (WASM, read-only file sandbox) allows file→Navidrome
  sync — the community `nd-rating-sync` plugin does this via internal
  Subsonic `setRating` — but Navidrome→file must be an **external tool**.
  That is this project.
- Subsonic API: `getStarred2` (all starred items), `setRating`, `star`/
  `unstar`. Token auth: `u=<user>&t=md5(password+salt)&s=<salt>`. Env:
  `NAVIDROME_URL`, `NAVIDROME_USER`, `NAVIDROME_PASSWORD`.
- Bulk read alternative: Navidrome SQLite `annotation` table (rating,
  starred, starred_at, play count) joined to `media_file` for paths.
  Prefer the API; fall back to the DB only if the API can't enumerate
  ratings efficiently (open question below).

**Direction policy:** Navidrome is the source of truth for ratings; file
tags are the durable backup that survives server rebuilds and library
moves. Export runs periodically; import exists for disaster recovery. No
two-way merge — that buys conflict-resolution complexity for no real use
case.

**Tag formats:** FLAC: `FMPS_RATING` (0.0–1.0, canonical) + `RATING`
(1–5). MP3: `POPM` frame (0–255 scale) + `TXXX:FMPS_RATING`. Also
`STARRED`/`STARRED_AT` as custom tags.

## Open Questions

- Does the Subsonic API enumerate *all* rated songs cleanly (`getStarred2`
  covers starred; ratings without stars may need `search3` pagination or
  the SQLite fallback)? Test against the live server before building
  `nd ratings`.
- Multi-disc placement convention: the library has no existing pattern (no
  `CD1/` subdirs, no `1-01` filename prefixes anywhere — checked July
  2026). Free choice; suggest single album dir with `D-NN. Title.flac`
  filenames plus correct `discnumber` tags, since Navidrome groups by
  tags, not paths.
- Whipper's unknown-disc flow: verify what artifacts exist when MB lookup
  fails at rip time (the `.toc` should still be there, which is all the
  `toc` primitive needs).
- Where do rips land before placement? Suggest `~/rips/` as the whipper
  output/staging area; confirm disk space on the desktop.
- Rip provenance: keep whipper `.log`/`.cue`/`.toc` — copy into the album
  dir (small, proves rip quality, Navidrome ignores them) or archive
  separately. Default: copy into the album dir.
