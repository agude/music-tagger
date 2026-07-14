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
