# Immediate Plan: Workflow Gaps from CD Rip Session

Discovered during the Nelly — Country Grammar rip (2026-07-13).

## 1. `tag` silently partial-tags untagged files — DONE

`build_diff` now falls back to positional matching when no tracks have
tracknumber tags. Warns to stderr. Mixed case (some tagged, some not)
still applies album-level only to untagged files.

Commit: `Add positional fallback for build_diff on untagged files`

## 2. No CD rip support — DONE

Added `rip` subcommand: reads disc ID via libdiscid, rips with
cdparanoia, encodes to FLAC, sets tracknumber tags from position.
New module `ripper.py`.

Commit: `Add rip subcommand for CD ripping to FLAC`

## 3. No file renaming after tagging — DONE

Added `rename` subcommand: renames files to `NN - Title.ext` from
tags. Uses placement module's filesystem sanitization. Supports
`--dry-run`.

Commit: `Add rename subcommand for tag-based file naming`

## 4. CLAUDE.md library workflow is incomplete — DONE

Updated workflow section with all per-album steps: tag, art, genre,
rename, nd rescan.

Commit: `Update CLAUDE.md workflow to include all per-album steps`

## 5. Genre not connected to release workflow — DONE

Genre subcommand added and documented in the workflow. MB has no
usable genre data; the operator picks from the existing vocabulary
(32 values as of July 2026).

Commit: `Add genre subcommand for meta-grouping tags`

---

# Round 2: Gaps from Christopher Denny rip (2026-07-14)

## 6. `tag` never sets `artist` or `albumartist` text fields — DONE

`_build_new_tags` sets `musicbrainz_albumartistid` and
`musicbrainz_artistid` but never the human-readable `artist` or
`albumartist` tags. Root cause: `MBRelease` and `MBTrack` carry
`artist_id` but never extract the artist name from the MB API
response. `fetch_release` reads `artist-credit[0].artist.id` but
ignores `.name`.

Also added `totaltracks` and `totaldiscs` tags — previously omitted.

Commits:
- `Extract artist name from MusicBrainz API responses`
- `Set artist, albumartist, totaltracks, and totaldiscs tags`

## 7. `rip` → `candidates` path is broken for freshly ripped files — DONE

Added `--artist` and `--album` overrides to `candidates` subcommand
and `search_candidates()`. Updated `rip`'s "Next" message to show
the override syntax.

Commit: `Add --artist/--album overrides to candidates subcommand`

## 8. `--help` broken on all subcommands — DONE

Replaced argparse subparsers with manual dict-based dispatch in
`main()`, `_mb()`, and `_nd()`. Each subcommand's own parser now
handles `--help` with its full argument list.

Commit: `Fix --help on all subcommands`

## 9. No progress feedback during CD rip — DONE

Dropped `capture_output=True` from the cdparanoia subprocess so
stderr streams per-track progress to the terminal.

Commit: `Stream cdparanoia progress to terminal during rip`

## 10. `discid` Python package not installed — DONE

Ran `uv add discid`. The `libdiscid-dev` system package must be
installed separately (`sudo apt-get install libdiscid-dev`).

Commit: `Add discid package dependency`

## 11. Navidrome credentials not configured — DONE

Documented `NAVIDROME_URL`, `NAVIDROME_USER`, and
`NAVIDROME_PASSWORD` in the new CLAUDE.md environment setup section.
Actual credential values are set in the shell profile (not in repo).

## 12. CLAUDE.md workflow gaps (round 2) — DONE

Updated CLAUDE.md: added environment setup section, copy step in
workflow, --artist/--album note for freshly ripped files, marked
discid as non-optional.

Commit (for #11 and #12): `Document environment setup and workflow gaps in CLAUDE.md`
