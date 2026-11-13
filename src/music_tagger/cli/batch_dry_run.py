"""Build resumable, read-only tag diffs for inventory-selected albums."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import time
import uuid
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from ..fileio import atomic_write_text
from ..musicbrainz import RETRY_BACKOFF_SECS, MBRelease, MusicBrainzClient
from ..tagger import (
    AlbumResult,
    CandidateMatch,
    DuplicateTrackError,
    build_diff,
    overwrites_needing_review,
    score_candidates,
)
from ..tags import AlbumTags, parse_position, read_album

_ASSESSMENT = "strict_dry_run_review_candidate"
_DEFAULT_OUTPUT = Path("/tmp/music-tagger-step2")
_READ_WORKERS = 32
_MAX_READ_WORKERS = 32
_MB_WORKERS = 32
_MAX_EXTRA_FETCH_ATTEMPTS = 3
_UUID_PATTERN = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


@dataclass
class _AlbumRead:
    album: AlbumTags
    fingerprint: str
    file_sizes: dict[Path, int]
    identity_issues: list[str]


@dataclass
class _ReleaseFetch:
    release: MBRelease | None
    metadata: dict[str, Any]


def _read_album_snapshot(row: dict[str, Any], root: Path) -> _AlbumRead:
    """Read tags and gather freshness/identity facts on the bounded NAS worker."""
    album = read_album(root / row["directory"])
    fingerprint, file_sizes, stat_issues = _input_fingerprint(album)
    identity_issues = _verify_identity(row, album, root, stat_issues)
    return _AlbumRead(album, fingerprint, file_sizes, identity_issues)


def _batch_dry_run(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger batch-dry-run",
        description=(
            "Create per-album tag diffs for the strict dry-run candidates in an inventory. "
            "Reads audio and MusicBrainz only; writes reports and caches outside the library."
        ),
    )
    parser.add_argument("--inventory", type=Path, required=True, help="Step 1 inventory JSON.")
    parser.add_argument(
        "--directory",
        help="Process only this exact selected album directory from the inventory.",
    )
    parser.add_argument(
        "--retry-errors",
        action="store_true",
        help="Retry every selected album without a completed checkpoint.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=_DEFAULT_OUTPUT,
        help=f"Report and cache directory outside the library root (default: {_DEFAULT_OUTPUT}).",
    )
    parser.add_argument(
        "--read-workers",
        type=int,
        default=_READ_WORKERS,
        help=f"Maximum concurrent album reads, from 1 to {_MAX_READ_WORKERS}.",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Regenerate summaries from completed checkpoints without rereading audio.",
    )
    parser.add_argument(
        "--keep-existing-credits",
        action="store_true",
        help="Keep existing personnel credits that the release omits, after its current credits.",
    )
    args = parser.parse_args(argv)

    try:
        inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
        rows = _select_inventory_rows(inventory)
        if args.directory is not None:
            rows = [row for row in rows if row["directory"] == args.directory]
            if not rows:
                raise ValueError(f"No selected inventory album has directory {args.directory!r}")
        if args.report_only and args.retry_errors:
            raise ValueError("--report-only and --retry-errors cannot be combined")
        output_dir = args.out_dir.resolve()
        library_root = Path(inventory["library_root"]).resolve()
        if not library_root.is_dir():
            raise ValueError(f"Library root is not a directory: {library_root}")
        if output_dir.is_relative_to(library_root):
            raise ValueError("--out-dir must not be inside the library root")
        if not 1 <= args.read_workers <= _MAX_READ_WORKERS:
            raise ValueError(f"--read-workers must be between 1 and {_MAX_READ_WORKERS}")
        output_dir.mkdir(parents=True, exist_ok=True)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)

    diffs_dir = output_dir / "diffs"
    checkpoints_dir = output_dir / "checkpoints"
    releases_dir = output_dir / "releases"
    for directory in (diffs_dir, checkpoints_dir, releases_dir):
        directory.mkdir(parents=True, exist_ok=True)
        if directory.resolve().is_relative_to(library_root):
            raise ValueError(f"Output directory resolves inside the library root: {directory}")
    _assert_output_tree_safe(output_dir, library_root)

    inventory_digest = hashlib.sha256(args.inventory.read_bytes()).hexdigest()
    if args.retry_errors:
        rows = _rows_needing_retry(
            rows, output_dir, inventory_digest, keep_existing_credits=args.keep_existing_credits
        )
        if not rows:
            print("No incomplete or error checkpoints need retrying.", flush=True)
            return
    release_ids = sorted({_inventory_release_id(row) for row in rows})
    if args.report_only:
        summaries, release_records = _summaries_from_checkpoints(
            rows,
            output_dir,
            library_root,
            inventory_digest,
            release_ids,
            keep_existing_credits=args.keep_existing_credits,
        )
        report = _build_report(
            summaries,
            release_ids,
            release_records,
            inventory_digest,
            args.inventory.resolve(),
            library_root,
            source_mode="saved_checkpoints_no_audio_reread",
        )
        _write_json(output_dir / "summary.json", report)
        _write_markdown(output_dir / "summary.md", report)
        print(
            f"Report regenerated from {report['counts']['completed']} completed checkpoints; "
            f"{report['counts']['errors']} errors. Reports: {output_dir / 'summary.md'} and "
            f"{output_dir / 'summary.json'}",
            flush=True,
        )
        if report["counts"]["errors"]:
            sys.exit(1)
        return

    print(
        f"Selected {len(rows)} albums and {len(release_ids)} release IDs; "
        f"reading with {args.read_workers} workers.",
        flush=True,
    )

    summaries, release_records = _run_batch(
        rows,
        release_ids,
        library_root,
        inventory_digest,
        output_dir,
        args.read_workers,
        keep_existing_credits=args.keep_existing_credits,
    )

    summaries.sort(key=lambda summary: summary["directory"].casefold())
    report = _build_report(
        summaries,
        release_ids,
        release_records,
        inventory_digest,
        args.inventory.resolve(),
        library_root,
    )
    _write_json(output_dir / "summary.json", report)
    _write_markdown(output_dir / "summary.md", report)
    print(
        f"Complete: {report['counts']['completed']} completed, "
        f"{report['counts']['errors']} errors. Reports: "
        f"{output_dir / 'summary.md'} and {output_dir / 'summary.json'}",
        flush=True,
    )
    if report["counts"]["errors"]:
        sys.exit(1)


def _run_batch(
    rows: list[dict[str, Any]],
    release_ids: list[str],
    library_root: Path,
    inventory_digest: str,
    output_dir: Path,
    read_workers: int,
    *,
    keep_existing_credits: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Read albums concurrently and overlap release fetches at one shared req/sec."""
    summaries: list[dict[str, Any]] = []
    release_records: dict[str, dict[str, Any]] = {}
    release_waiters: dict[str, list[tuple[dict[str, Any], _AlbumRead]]] = {}
    mb_client = MusicBrainzClient()
    read_pool = ThreadPoolExecutor(max_workers=read_workers)
    release_pool = ThreadPoolExecutor(max_workers=_MB_WORKERS)
    read_pending: dict[Future[_AlbumRead], dict[str, Any]] = {
        read_pool.submit(_read_album_snapshot, row, library_root): row for row in rows
    }
    release_pending: dict[Future[_ReleaseFetch], str] = {}
    release_future_by_id: dict[str, Future[_ReleaseFetch]] = {}
    read_count = 0
    release_count = 0

    try:
        while read_pending or release_pending:
            active: list[Future[Any]] = [*read_pending, *release_pending]
            completed, _ = wait(active, return_when=FIRST_COMPLETED)

            # Resolve completed network requests first so newly read albums can reuse them.
            for future in completed:
                if future not in release_pending:
                    continue
                release_id = release_pending.pop(future)
                release_future_by_id.pop(release_id, None)
                release_count += 1
                try:
                    fetch = future.result()
                except Exception as error:
                    fetch = _ReleaseFetch(
                        None,
                        {
                            "source": "musicbrainz",
                            "fetched_at_utc": datetime.now(UTC).isoformat(),
                            "retries": 0,
                            "error": f"{type(error).__name__}: {error}",
                        },
                    )
                record = {**fetch.metadata, "release": fetch.release}
                release_records[release_id] = record
                waiting = release_waiters.pop(release_id, [])
                print(
                    f"MusicBrainz {release_count}/{len(release_ids)}: {release_id} "
                    f"({fetch.metadata.get('source')}, retries={fetch.metadata.get('retries', 0)})",
                    flush=True,
                )
                for row, snapshot in waiting:
                    summary = _finish_album(
                        row,
                        snapshot,
                        fetch.release,
                        fetch.metadata,
                        output_dir,
                        inventory_digest,
                        library_root,
                        keep_existing_credits=keep_existing_credits,
                    )
                    summaries.append(summary)
                    _print_album_progress(summary, len(summaries), len(rows))

            for future in completed:
                if future not in read_pending:
                    continue
                row = read_pending.pop(future)
                read_count += 1
                try:
                    snapshot = future.result()
                except Exception as error:
                    summary = _error_summary(row, f"{type(error).__name__}: {error}")
                    _save_checkpoint(
                        row,
                        output_dir,
                        inventory_digest,
                        None,
                        summary,
                        keep_existing_credits=keep_existing_credits,
                    )
                    summaries.append(summary)
                    _print_album_progress(summary, len(summaries), len(rows))
                    continue

                print(f"Read {read_count}/{len(rows)}: {row['directory']}", flush=True)
                if snapshot.identity_issues:
                    summary = _error_summary(
                        row,
                        "identity drift: " + "; ".join(snapshot.identity_issues),
                        status="identity_drift",
                        release_id=_inventory_release_id(row),
                    )
                    _save_checkpoint(
                        row,
                        output_dir,
                        inventory_digest,
                        snapshot.fingerprint,
                        summary,
                        release_id=summary["release_id"],
                        keep_existing_credits=keep_existing_credits,
                    )
                    summaries.append(summary)
                    _print_album_progress(summary, len(summaries), len(rows))
                    continue

                release_id = _inventory_release_id(row)
                if release_id in release_records:
                    record = release_records[release_id]
                    summary = _finish_album(
                        row,
                        snapshot,
                        record.get("release"),
                        record,
                        output_dir,
                        inventory_digest,
                        library_root,
                        keep_existing_credits=keep_existing_credits,
                    )
                    summaries.append(summary)
                    _print_album_progress(summary, len(summaries), len(rows))
                    continue

                cache_path = output_dir / "releases" / f"{release_id}.json"
                cached = _load_release_cache(cache_path, release_id)
                if cached:
                    release, metadata = cached
                    release_records[release_id] = {**metadata, "release": release}
                    release_digest = hashlib.sha256(cache_path.read_bytes()).hexdigest()
                    summary = _finish_album(
                        row,
                        snapshot,
                        release,
                        {**metadata, "release_snapshot_sha256": release_digest},
                        output_dir,
                        inventory_digest,
                        library_root,
                        keep_existing_credits=keep_existing_credits,
                    )
                    summaries.append(summary)
                    _print_album_progress(summary, len(summaries), len(rows))
                    continue

                release_waiters.setdefault(release_id, []).append((row, snapshot))
                if release_id not in release_future_by_id:
                    release_future = release_pool.submit(
                        _fetch_release_to_cache,
                        release_id,
                        output_dir,
                        mb_client,
                    )
                    release_future_by_id[release_id] = release_future
                    release_pending[release_future] = release_id
    finally:
        read_pool.shutdown(wait=True, cancel_futures=False)
        release_pool.shutdown(wait=True, cancel_futures=False)
        _load_report_cache_records(release_ids, output_dir, release_records)
        mb_client.close()

    return summaries, release_records


def _finish_album(
    row: dict[str, Any],
    snapshot: _AlbumRead,
    release: MBRelease | None,
    release_metadata: dict[str, Any],
    output_dir: Path,
    inventory_digest: str,
    library_root: Path,
    *,
    keep_existing_credits: bool = False,
) -> dict[str, Any]:
    release_id = _inventory_release_id(row)
    if release is None:
        error = release_metadata.get("error") or "MusicBrainz returned no release"
        summary = _error_summary(row, f"MusicBrainz fetch failed: {error}", release_id=release_id)
        _save_checkpoint(
            row,
            output_dir,
            inventory_digest,
            snapshot.fingerprint,
            summary,
            release_id=release_id,
            release_snapshot_sha256=None,
            keep_existing_credits=keep_existing_credits,
        )
        return summary

    cache_path = output_dir / "releases" / f"{release_id}.json"
    try:
        release_digest = release_metadata.get("release_snapshot_sha256")
        if release_digest is None:
            release_digest = hashlib.sha256(cache_path.read_bytes()).hexdigest()
        return _complete_album_diff(
            row,
            snapshot,
            release,
            release_metadata,
            release_digest,
            output_dir,
            inventory_digest,
            library_root,
            keep_existing_credits=keep_existing_credits,
        )
    except Exception as error:
        summary = _error_summary(
            row,
            f"{type(error).__name__}: {error}",
            release_id=release_id,
        )
        _save_checkpoint(
            row,
            output_dir,
            inventory_digest,
            snapshot.fingerprint,
            summary,
            release_id=release_id,
            keep_existing_credits=keep_existing_credits,
        )
        return summary


def _complete_album_diff(
    row: dict[str, Any],
    snapshot: _AlbumRead,
    release: MBRelease,
    release_metadata: dict[str, Any],
    release_digest: str,
    output_dir: Path,
    inventory_digest: str,
    library_root: Path,
    *,
    keep_existing_credits: bool = False,
) -> dict[str, Any]:
    release_id = _inventory_release_id(row)
    checkpoint, checkpoint_status = _load_checkpoint(
        row,
        output_dir,
        library_root,
        inventory_digest,
        snapshot.fingerprint,
        release_id,
        release_digest,
        resolved_release_id=release.id,
        keep_existing_credits=keep_existing_credits,
    )
    if checkpoint:
        summary = _refresh_partial_classification(dict(checkpoint["summary"]))
        summary["status"] = "completed"
        summary["checkpoint_reused"] = True
        summary["checkpoint_status"] = "reused"
        summary["release_cache_source"] = release_metadata.get("source")
        summary["release_snapshot_sha256"] = release_digest
        _, diff_path = _checkpoint_paths(row, output_dir)
        _save_checkpoint(
            row,
            output_dir,
            inventory_digest,
            snapshot.fingerprint,
            summary,
            release_id=release_id,
            release_snapshot_sha256=release_digest,
            diff_path=diff_path,
            keep_existing_credits=keep_existing_credits,
        )
        return summary

    if not release.discs:
        raise ValueError("MusicBrainz release has no audio media")
    album = snapshot.album
    try:
        result = build_diff(album, release, keep_existing_credits=keep_existing_credits)
    except (DuplicateTrackError, ValueError) as error:
        summary = _error_summary(row, str(error), release_id=release_id)
        _save_checkpoint(
            row,
            output_dir,
            inventory_digest,
            snapshot.fingerprint,
            summary,
            release_id=release_id,
            release_snapshot_sha256=release_digest,
            keep_existing_credits=keep_existing_credits,
        )
        return summary

    match = score_candidates(album, [release])[0]
    summary = _summarize_album(
        row,
        album,
        result,
        match,
        release_id,
        library_root,
        output_dir,
        snapshot.file_sizes,
    )
    summary["resolved_release_id"] = release.id
    if redirect := release_metadata.get("redirect"):
        review_flags = list(summary["operator_review_flags"])
        review_flags.append("release_id_redirected")
        summary["operator_review_flags"] = review_flags
        summary["operator_review_required"] = True
        summary["release_redirect"] = redirect
    summary["checkpoint_status"] = checkpoint_status
    summary["release_cache_source"] = release_metadata.get("source")
    summary["release_snapshot_sha256"] = release_digest
    _, diff_path = _checkpoint_paths(row, output_dir)
    _write_json(diff_path, result.to_dict())
    _save_checkpoint(
        row,
        output_dir,
        inventory_digest,
        snapshot.fingerprint,
        summary,
        release_id=release_id,
        release_snapshot_sha256=release_digest,
        diff_path=diff_path,
        keep_existing_credits=keep_existing_credits,
    )
    return summary


def _refresh_partial_classification(summary: dict[str, Any]) -> dict[str, Any]:
    """Use diff track count, not the candidate flag, to identify partial matching."""
    is_partial = summary.get("counted_track_count", 0) < summary.get("release_track_count", 0)
    review_flags = list(summary.get("operator_review_flags", []))
    if is_partial and "partial_album" not in review_flags:
        review_flags.append("partial_album")
    summary["operator_review_flags"] = review_flags
    summary["operator_review_required"] = bool(review_flags) or summary.get("gate") != "auto"
    if is_partial:
        summary["match_method"] = "partial"
    return summary


def _print_album_progress(summary: dict[str, Any], completed: int, total: int) -> None:
    print(
        f"[{completed}/{total}] {summary['status']}: {summary['directory']} — "
        f"{summary.get('gate', 'unavailable')}",
        flush=True,
    )


def _select_inventory_rows(inventory: dict[str, Any]) -> list[dict[str, Any]]:
    """Select only the assessed candidates and reject malformed inventory rows."""
    albums = inventory.get("albums")
    if not isinstance(albums, list):
        raise ValueError("Inventory has no albums list")

    rows = [
        row
        for row in albums
        if isinstance(row, dict)
        and row.get("dry_run_inventory_assessment") == _ASSESSMENT
        and not row.get("known_custom_edit")
    ]
    if not rows:
        raise ValueError(f"Inventory contains no {_ASSESSMENT} albums")
    for row in rows:
        _inventory_release_id(row)
        if not isinstance(row.get("files"), list):
            raise ValueError(f"Inventory row has no files list: {row.get('directory')!r}")
        invalid_files = any(
            not isinstance(file_row, dict) or not isinstance(file_row.get("path"), str)
            for file_row in row["files"]
        )
        if invalid_files:
            raise ValueError(f"Inventory row has invalid file paths: {row.get('directory')!r}")
        if not row.get("directory"):
            raise ValueError("Inventory row has no directory")
        relative_directory = Path(row["directory"])
        if relative_directory.is_absolute() or ".." in relative_directory.parts:
            raise ValueError(f"Inventory directory escapes library root: {row['directory']!r}")
    return rows


def _rows_needing_retry(
    rows: list[dict[str, Any]],
    output_dir: Path,
    inventory_digest: str,
    *,
    keep_existing_credits: bool = False,
) -> list[dict[str, Any]]:
    """Select rows without a completed checkpoint for this exact inventory."""
    retry_rows = []
    for row in rows:
        checkpoint_path, _ = _checkpoint_paths(row, output_dir)
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, json.JSONDecodeError):
            retry_rows.append(row)
            continue
        summary = checkpoint.get("summary")
        if (
            checkpoint.get("inventory_sha256") != inventory_digest
            or checkpoint.get("keep_existing_credits", False) != keep_existing_credits
            or checkpoint.get("complete") is not True
            or not isinstance(summary, dict)
            or summary.get("status") != "completed"
        ):
            retry_rows.append(row)
    return retry_rows


def _assert_output_tree_safe(output_dir: Path, library_root: Path) -> None:
    """Reject existing symlinks in the report tree that point into the library."""
    for path in output_dir.rglob("*"):
        if path.is_symlink() and path.resolve().is_relative_to(library_root):
            raise ValueError(f"Output path resolves inside the library root: {path}")


def _canonical_uuid(value: str) -> str:
    if not re.fullmatch(_UUID_PATTERN, value):
        raise ValueError(f"malformed MusicBrainz release UUID: {value!r}")
    return str(uuid.UUID(value))


def _inventory_release_id(row: dict[str, Any]) -> str:
    release_ids = row.get("distinct_valid_release_uuids", [])
    if not isinstance(release_ids, list) or len(release_ids) != 1:
        raise ValueError(f"Expected one existing release ID in {row.get('directory')!r}")
    return _canonical_uuid(release_ids[0])


def _album_key(directory: str) -> str:
    readable = "".join(char if char.isalnum() else "_" for char in directory).strip("_")
    digest = hashlib.sha256(directory.encode("utf-8")).hexdigest()[:12]
    return f"{readable[:72]}-{digest}"


def _input_fingerprint(album: AlbumTags) -> tuple[str, dict[Path, int], list[str]]:
    tracks = []
    file_sizes: dict[Path, int] = {}
    stat_issues = []
    for track in album.tracks:
        try:
            file_stat = track.path.lstat()
            is_symlink = stat.S_ISLNK(file_stat.st_mode)
            is_regular_file = stat.S_ISREG(file_stat.st_mode)
            stat_data: dict[str, int | str | bool] = {
                "size": file_stat.st_size,
                "mtime_ns": file_stat.st_mtime_ns,
                "is_symlink": is_symlink,
                "is_regular_file": is_regular_file,
            }
            file_sizes[track.path.absolute()] = file_stat.st_size
            if is_symlink:
                stat_issues.append(f"audio path became a symlink: {track.path}")
            elif not is_regular_file:
                stat_issues.append(f"audio path is not a regular file: {track.path}")
        except OSError as error:
            stat_data = {"error": str(error)}
            stat_issues.append(f"cannot stat audio path {track.path}: {error}")
        tracks.append(
            {
                "data": track.to_dict(),
                "release_id_values": list(track.release_id_values),
                "stat": stat_data,
            }
        )
    data = {
        "directory": str(album.directory),
        "tracks": tracks,
        "unreadable": [str(path) for path in album.unreadable],
    }
    encoded = json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), file_sizes, stat_issues


def _verify_identity(
    row: dict[str, Any],
    album: AlbumTags,
    root: Path,
    stat_issues: list[str] | None = None,
) -> list[str]:
    """Check file membership and every raw ID against the selected inventory row."""
    issues = list(stat_issues or [])
    album_directory = root / row["directory"]
    if album_directory.is_symlink() or album_directory.parent.is_symlink():
        issues.append(f"album directory or artist directory became a symlink: {album_directory}")
    try:
        if not album_directory.resolve().is_relative_to(root):
            issues.append(f"album directory resolves outside library root: {album_directory}")
    except OSError as error:
        issues.append(f"cannot resolve album directory {album_directory}: {error}")
    expected_paths = {
        Path(os.path.abspath(root / file_row["path"])): file_row
        for file_row in row["files"]
        if isinstance(file_row, dict) and isinstance(file_row.get("path"), str)
    }
    actual_paths = {track.path.absolute() for track in album.tracks}
    actual_paths.update(path.absolute() for path in album.unreadable)

    for path in expected_paths:
        if not path.is_relative_to(root):
            issues.append(f"inventory path escapes library root: {path}")

    missing = sorted(_relative_path(path, root) for path in expected_paths.keys() - actual_paths)
    unexpected = sorted(_relative_path(path, root) for path in actual_paths - expected_paths.keys())
    if missing:
        issues.append("missing inventory files: " + ", ".join(missing))
    if unexpected:
        issues.append("unexpected audio files: " + ", ".join(unexpected))
    if album.unreadable:
        issues.append(
            "unreadable audio files: "
            + ", ".join(_relative_path(path.absolute(), root) for path in album.unreadable)
        )

    expected_release_id = _inventory_release_id(row)
    tracks_by_path = {track.path.absolute(): track for track in album.tracks}
    for path, file_row in expected_paths.items():
        track = tracks_by_path.get(path)
        if track is None:
            continue
        expected_values = file_row.get("musicbrainz_albumid_raw_values", [])
        actual_values = list(track.release_id_values)
        if actual_values != expected_values:
            issues.append(
                f"release ID values changed for {_relative_path(path, root)}: "
                f"inventory={expected_values!r}, current={actual_values!r}"
            )
        malformed = []
        valid_values = []
        for value in actual_values:
            if not value:
                malformed.append("empty")
                continue
            try:
                valid_values.append(_canonical_uuid(value))
            except ValueError:
                malformed.append(value)
        if malformed:
            issues.append(
                f"invalid raw release ID value(s) for {_relative_path(path, root)}: {malformed!r}"
            )
        if not valid_values or any(value != expected_release_id for value in valid_values):
            issues.append(
                f"current release IDs for {_relative_path(path, root)} do not all match "
                f"selected ID {expected_release_id}"
            )
    return issues


def _relative_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return f"{path} (outside library root)"


def _load_release_cache(path: Path, release_id: str) -> tuple[MBRelease, dict[str, Any]] | None:
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        release = MBRelease.from_dict(cached["release"])
        requested_id = _canonical_uuid(cached.get("requested_release_id", release.id))
        resolved_id = _canonical_uuid(cached.get("resolved_release_id", release.id))
        redirect = cached.get("redirect")
        if requested_id != release_id or resolved_id != _canonical_uuid(release.id):
            return None
        if release_id != resolved_id:
            if (
                not isinstance(redirect, dict)
                or redirect.get("requested_release_id") != release_id
                or redirect.get("resolved_release_id") != resolved_id
            ):
                return None
        elif redirect is not None:
            return None
        return release, {
            "source": "disk_cache",
            "fetched_at_utc": cached.get("fetched_at_utc"),
            "retries": int(cached.get("retries", 0)),
            "error": None,
            "requested_release_id": requested_id,
            "resolved_release_id": resolved_id,
            "redirect": redirect,
        }
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _fetch_release(client: MusicBrainzClient, release_id: str) -> _ReleaseFetch:
    """Retry transient MusicBrainz failures and keep request-local retry counts."""
    retries = 0
    last_error: Exception | None = None
    for attempt in range(_MAX_EXTRA_FETCH_ATTEMPTS + 1):
        try:
            release = client.fetch_release(release_id)
            retries += _client_retry_count(client)
            resolved_id = _canonical_uuid(release.id)
            redirect = getattr(client, "last_release_redirect", None)
            if resolved_id != release_id and (
                not redirect
                or redirect.get("requested_release_id") != release_id
                or redirect.get("resolved_release_id") != resolved_id
            ):
                raise ValueError(f"MusicBrainz returned {release.id} for requested {release_id}")
            metadata = {
                "source": "musicbrainz",
                "fetched_at_utc": datetime.now(UTC).isoformat(),
                "retries": retries,
                "error": None,
                "requested_release_id": release_id,
                "resolved_release_id": resolved_id,
                "redirect": redirect,
            }
            return _ReleaseFetch(release, metadata)
        except httpx.HTTPStatusError as error:
            retries += _client_retry_count(client)
            if error.response.status_code < 500 or error.response.status_code in {501, 505}:
                last_error = error
                break
            last_error = error
        except httpx.TransportError as error:
            retries += _client_retry_count(client)
            last_error = error
        except Exception as error:
            last_error = error
            break

        if attempt == _MAX_EXTRA_FETCH_ATTEMPTS:
            break
        retries += 1
        time.sleep(RETRY_BACKOFF_SECS * 2**attempt)

    metadata = {
        "source": "musicbrainz",
        "fetched_at_utc": datetime.now(UTC).isoformat(),
        "retries": retries,
        "error": f"{type(last_error).__name__}: {last_error}" if last_error else "Unknown error",
    }
    return _ReleaseFetch(None, metadata)


def _client_retry_count(client: MusicBrainzClient) -> int:
    release_retry_count = getattr(client, "last_release_retry_count", None)
    return (
        release_retry_count if release_retry_count is not None else client.last_request_retry_count
    )


def _fetch_release_to_cache(
    release_id: str,
    output_dir: Path,
    client: MusicBrainzClient,
) -> _ReleaseFetch:
    result = _fetch_release(client, release_id)
    if result.release is not None:
        cache_path = output_dir / "releases" / f"{release_id}.json"
        _write_json(
            cache_path,
            {
                "schema_version": 1,
                **result.metadata,
                "requested_release_id": release_id,
                "release": result.release.to_dict(),
            },
        )
    return result


def _load_report_cache_records(
    release_ids: list[str],
    output_dir: Path,
    records: dict[str, dict[str, Any]],
) -> None:
    """Add disk-cache provenance for releases reused through album checkpoints."""
    for release_id in release_ids:
        if release_id in records:
            continue
        cache_path = output_dir / "releases" / f"{release_id}.json"
        if cached := _load_release_cache(cache_path, release_id):
            release, metadata = cached
            records[release_id] = {**metadata, "release": release}
        else:
            records[release_id] = {
                "source": "unavailable",
                "fetched_at_utc": None,
                "retries": 0,
                "error": "No valid release snapshot is available.",
                "release": None,
            }


def _checkpoint_paths(row: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    key = _album_key(row["directory"])
    return output_dir / "checkpoints" / f"{key}.json", output_dir / "diffs" / f"{key}.json"


def _load_checkpoint(
    row: dict[str, Any],
    output_dir: Path,
    root: Path,
    inventory_digest: str,
    fingerprint: str,
    release_id: str,
    release_snapshot_sha256: str | None,
    *,
    resolved_release_id: str | None = None,
    keep_existing_credits: bool = False,
) -> tuple[dict[str, Any] | None, str]:
    checkpoint_path, diff_path = _checkpoint_paths(row, output_dir)
    try:
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        diff_bytes = diff_path.read_bytes()
        diff = json.loads(diff_bytes)
        if checkpoint.get("inventory_sha256") != inventory_digest:
            return None, "stale_inventory"
        if checkpoint.get("keep_existing_credits", False) != keep_existing_credits:
            return None, "stale_credit_policy"
        if checkpoint.get("input_fingerprint") != fingerprint:
            return None, "stale_input"
        if checkpoint.get("release_id") != release_id:
            return None, "stale_release_id"
        if checkpoint.get("release_snapshot_sha256") != release_snapshot_sha256:
            return None, "stale_release_snapshot"
        if checkpoint.get("diff_sha256") != hashlib.sha256(diff_bytes).hexdigest():
            return None, "damaged_diff"
        if (
            checkpoint.get("complete") is not True
            or checkpoint.get("summary", {}).get("status") != "completed"
        ):
            return None, "incomplete_checkpoint"

        expected_paths = {
            str(Path(os.path.abspath(root / file_row["path"]))) for file_row in row["files"]
        }
        diff_tracks = diff.get("tracks", [])
        actual_paths = {
            str(Path(track["path"]).absolute())
            for track in diff_tracks
            if isinstance(track, dict) and isinstance(track.get("path"), str)
        }
        if (
            diff.get("release_id") == (resolved_release_id or release_id)
            and len(actual_paths) == len(diff_tracks)
            and actual_paths == expected_paths
        ):
            return checkpoint, "reused"
        return None, "invalid_diff"
    except (OSError, AttributeError, TypeError, json.JSONDecodeError):
        if checkpoint_path.exists() or diff_path.exists():
            return None, "invalid_checkpoint"
        return None, "new"


def _summarize_album(
    row: dict[str, Any],
    album: AlbumTags,
    result: AlbumResult,
    match: CandidateMatch,
    release_id: str,
    root: Path,
    output_dir: Path,
    file_sizes: dict[Path, int],
) -> dict[str, Any]:
    unresolved = [
        {
            "file": diff.track.path.name,
            "field": conflict.field,
            "old_value": conflict.old_value,
            "candidate_values": list(conflict.candidate_values),
        }
        for diff in result.diffs
        for conflict in diff.conflicts
        if not conflict.is_resolved
    ]
    overwrites = overwrites_needing_review(result)
    gate = "blocked" if unresolved else "review" if overwrites else "auto"

    counted_files = [
        track for track in album.tracks if parse_position(track.tags.get("tracknumber", "")) != 0
    ]
    unmatched_names = []
    hidden_count = 0
    warning_lines = list(result.warnings)
    for diff in result.diffs:
        warning_lines.extend(f"{diff.track.path.name}: {warning}" for warning in diff.warnings)
        if any("hidden track before track 1" in warning for warning in diff.warnings):
            hidden_count += 1
        if (
            any(
                phrase in warning
                for warning in diff.warnings
                for phrase in ("no release track", "matches the same release track")
            )
            and parse_position(diff.track.tags.get("tracknumber", "")) != 0
        ):
            unmatched_names.append(diff.track.path.name)

    matched_count = max(0, len(counted_files) - len(unmatched_names))
    is_partial_album = len(counted_files) < result.release.track_count
    duration = match.stats.to_dict()
    review_flags: list[str] = []
    if unmatched_names:
        review_flags.append("unmatched_tracks")
    if duration["tracks_compared"] == 0:
        review_flags.append("no_duration_comparisons")
    elif duration["tracks_within_2s"] < duration["tracks_compared"]:
        review_flags.append("duration_outliers_over_2s")
    if is_partial_album:
        review_flags.append("partial_album")
    if any("matching " in warning and " by position" in warning for warning in result.warnings):
        review_flags.append("positional_match")
    if (
        duration["tracks_compared"] < matched_count
        and "no_duration_comparisons" not in review_flags
    ):
        review_flags.append("incomplete_duration_data")

    changed_field_counts: Counter[str] = Counter()
    changed_paths: set[Path] = set()
    locked_changes: list[dict[str, str]] = []
    for diff in result.diffs:
        if diff.changes:
            changed_paths.add(diff.track.path)
            changed_field_counts.update(change.field for change in diff.changes)
        locked_changes.extend(
            {
                "file": diff.track.path.name,
                "field": change.field,
                "old_value": change.old_value,
                "new_value": change.new_value,
            }
            for change in diff.locked_changes
        )
    changed_bytes = 0
    size_errors = []
    for path in changed_paths:
        size = file_sizes.get(path.absolute())
        if size is None:
            size_errors.append(f"{path.name}: no file size was captured during the read")
        else:
            changed_bytes += size
    if size_errors:
        review_flags.append("file_size_estimate_incomplete")
        warning_lines.extend(f"File size estimate unavailable: {error}" for error in size_errors)

    proposed_changes = sum(len(diff.changes) for diff in result.diffs)
    return {
        "directory": row["directory"],
        "artist": album.artist,
        "album": album.album,
        "release_id": release_id,
        "release_title": result.release.title,
        "status": "completed",
        "gate": gate,
        "operator_review_required": bool(review_flags) or gate != "auto",
        "operator_review_flags": review_flags,
        "match_method": (
            "positional"
            if "positional_match" in review_flags
            else "partial"
            if is_partial_album
            else "disc_track_number"
        ),
        "match_flags": list(match.flags),
        "track_count": len(album.tracks),
        "counted_track_count": len(counted_files),
        "release_track_count": result.release.track_count,
        "matched_track_count": matched_count,
        "unmatched_track_count": len(unmatched_names),
        "unmatched_files": unmatched_names,
        "hidden_track_count": hidden_count,
        "duration": duration,
        "proposed_change_count": proposed_changes,
        "changed_file_count_estimate": len(changed_paths),
        "changed_bytes_estimate": changed_bytes,
        "changed_bytes_estimate_complete": not size_errors,
        "changed_field_counts": dict(sorted(changed_field_counts.items())),
        "locked_changes": locked_changes,
        "conflicts": unresolved,
        "warnings": warning_lines,
        "diff_file": str(_checkpoint_paths(row, output_dir)[1]),
        "checkpoint_reused": False,
        "checkpoint_status": "new",
    }


def _error_summary(
    row: dict[str, Any],
    error: str,
    *,
    status: str = "error",
    release_id: str | None = None,
) -> dict[str, Any]:
    return {
        "directory": row.get("directory", "(unknown)"),
        "artist": row.get("artist_tag_sample", ""),
        "album": row.get("album_tag_sample", ""),
        "release_id": release_id,
        "status": status,
        "gate": "unavailable",
        "operator_review_required": True,
        "operator_review_flags": [status],
        "error": error,
        "checkpoint_reused": False,
        "checkpoint_status": "new",
    }


def _save_checkpoint(
    row: dict[str, Any],
    output_dir: Path,
    inventory_digest: str,
    fingerprint: str | None,
    summary: dict[str, Any],
    *,
    release_id: str | None = None,
    release_snapshot_sha256: str | None = None,
    diff_path: Path | None = None,
    keep_existing_credits: bool = False,
) -> None:
    checkpoint_path, _ = _checkpoint_paths(row, output_dir)
    _write_json(
        checkpoint_path,
        {
            "schema_version": 1,
            "complete": True,
            "inventory_sha256": inventory_digest,
            "keep_existing_credits": keep_existing_credits,
            "input_fingerprint": fingerprint,
            "release_id": release_id,
            "release_snapshot_sha256": release_snapshot_sha256,
            "diff_sha256": (
                hashlib.sha256(diff_path.read_bytes()).hexdigest()
                if diff_path is not None and diff_path.is_file()
                else None
            ),
            "summary": summary,
        },
    )


def _write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _build_report(
    summaries: list[dict[str, Any]],
    release_ids: list[str],
    release_records: dict[str, dict[str, Any]],
    inventory_digest: str,
    inventory_path: Path,
    library_root: Path,
    *,
    source_mode: str = "fresh_audio_reads",
) -> dict[str, Any]:
    status_counts = Counter(summary["status"] for summary in summaries)
    gate_counts = Counter(summary["gate"] for summary in summaries)
    flag_counts = Counter(
        flag for summary in summaries for flag in summary.get("operator_review_flags", [])
    )
    field_counts: Counter[str] = Counter()
    for summary in summaries:
        field_counts.update(summary.get("changed_field_counts", {}))
    errors = [
        {"directory": summary["directory"], "error": summary["error"]}
        for summary in summaries
        if summary.get("error")
    ]
    release_fetches = {
        release_id: {
            "source": record.get("source"),
            "fetched_at_utc": record.get("fetched_at_utc"),
            "retries": record.get("retries", 0),
            "error": record.get("error"),
            "requested_release_id": record.get("requested_release_id", release_id),
            "resolved_release_id": record.get("resolved_release_id", release_id),
            "redirect": record.get("redirect"),
        }
        for release_id, record in sorted(release_records.items())
    }
    return {
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "inventory": str(inventory_path),
        "inventory_sha256": inventory_digest,
        "library_root": str(library_root),
        "source_mode": source_mode,
        "selection": _ASSESSMENT,
        "release_ids_selected": release_ids,
        "release_fetches": release_fetches,
        "counts": {
            "selected": len(summaries),
            "completed": status_counts["completed"],
            "errors": status_counts["error"] + status_counts["identity_drift"],
            "identity_drift": status_counts["identity_drift"],
            "checkpoint_reused": sum(s.get("checkpoint_reused", False) for s in summaries),
            "checkpoint_status": dict(
                sorted(Counter(s.get("checkpoint_status", "new") for s in summaries).items())
            ),
            "gate": dict(sorted(gate_counts.items())),
            "operator_review_required": sum(
                summary.get("operator_review_required", False) for summary in summaries
            ),
            "review_flags": dict(sorted(flag_counts.items())),
            "proposed_changes": sum(s.get("proposed_change_count", 0) for s in summaries),
            "changed_files_estimate": sum(
                s.get("changed_file_count_estimate", 0) for s in summaries
            ),
            "changed_bytes_estimate": sum(s.get("changed_bytes_estimate", 0) for s in summaries),
            "changed_bytes_estimate_complete": all(
                s.get("changed_bytes_estimate_complete", True) for s in summaries
            ),
            "unresolved_conflicts": sum(len(s.get("conflicts", [])) for s in summaries),
            "changed_fields": dict(sorted(field_counts.items())),
            "release_ids": len(release_ids),
            "release_cache_hits": sum(
                record.get("source") == "disk_cache" for record in release_records.values()
            ),
            "network_release_fetches": sum(
                record.get("source") == "musicbrainz" and not record.get("error")
                for record in release_records.values()
            ),
            "fetch_retries": sum(record.get("retries", 0) for record in release_records.values()),
            "redirected_releases": sum(
                record.get("redirect") is not None for record in release_records.values()
            ),
        },
        "errors": errors,
        "assumptions": [
            (
                "Release IDs come only from the inventory and are revalidated against every raw "
                "tag value."
            ),
            (
                "A duration outlier is a compared track more than 2.0 seconds from MusicBrainz, "
                "matching tagger.py."
            ),
            (
                "Partial, positional, unmatched, unmeasured, or duration-outlier albums require "
                "operator review even when Gate is auto; a canonical MusicBrainz ID redirect also "
                "requires review."
            ),
            (
                "Changed bytes estimate is the current file size for each file with at least one "
                "proposed writable change."
            ),
            (
                "Reports, checkpoints, and release snapshots go only to --out-dir, which must "
                "resolve outside the library root. The command does not write audio, logs, or "
                "checklists."
            ),
            (
                "--report-only verifies inventory, checkpoint, diff, and release-snapshot hashes; "
                "it does not reread current audio files or revalidate their input fingerprints."
            ),
        ],
        "albums": summaries,
    }


def _summaries_from_checkpoints(
    rows: list[dict[str, Any]],
    output_dir: Path,
    library_root: Path,
    inventory_digest: str,
    release_ids: list[str],
    *,
    keep_existing_credits: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Regenerate the final report from saved diffs without rereading the library."""
    summaries = []
    release_records: dict[str, dict[str, Any]] = {}
    for row in rows:
        checkpoint_path, diff_path = _checkpoint_paths(row, output_dir)
        cached_release: tuple[MBRelease, dict[str, Any]] | None = None
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            if checkpoint.get("inventory_sha256") != inventory_digest:
                raise ValueError("checkpoint inventory hash does not match")
            if checkpoint.get("keep_existing_credits", False) != keep_existing_credits:
                raise ValueError("checkpoint credit-preservation policy does not match")
            summary = dict(checkpoint["summary"])
            if summary.get("status") == "completed":
                diff_bytes = diff_path.read_bytes()
                diff = json.loads(diff_bytes)
                if checkpoint.get("diff_sha256") != hashlib.sha256(diff_bytes).hexdigest():
                    raise ValueError("saved diff hash does not match checkpoint")
                release_id = _canonical_uuid(summary["release_id"])
                release_path = output_dir / "releases" / f"{release_id}.json"
                release_bytes = release_path.read_bytes()
                if (
                    checkpoint.get("release_snapshot_sha256")
                    != hashlib.sha256(release_bytes).hexdigest()
                ):
                    raise ValueError("release snapshot hash does not match checkpoint")
                cached_release = _load_release_cache(release_path, release_id)
                if cached_release is None:
                    raise ValueError("release snapshot is invalid for the checkpoint release ID")
                expected_resolved_id = _canonical_uuid(
                    summary.get("resolved_release_id", release_id)
                )
                if _canonical_uuid(cached_release[0].id) != expected_resolved_id:
                    raise ValueError("release snapshot resolved ID does not match checkpoint")
                if diff.get("release_id") != summary.get(
                    "resolved_release_id", summary.get("release_id")
                ):
                    raise ValueError("saved diff release ID does not match checkpoint")
                expected_paths = {
                    str(Path(os.path.abspath(library_root / file_row["path"])))
                    for file_row in row["files"]
                }
                diff_tracks = diff.get("tracks", [])
                actual_paths = {
                    str(Path(track["path"]).absolute())
                    for track in diff_tracks
                    if isinstance(track, dict) and isinstance(track.get("path"), str)
                }
                if len(actual_paths) != len(diff_tracks) or actual_paths != expected_paths:
                    raise ValueError("saved diff track paths do not match inventory")

                is_partial_album = summary.get("counted_track_count", 0) < summary.get(
                    "release_track_count", 0
                )
                review_flags = list(summary.get("operator_review_flags", []))
                if is_partial_album and "partial_album" not in review_flags:
                    review_flags.append("partial_album")
                summary["operator_review_flags"] = review_flags
                summary["operator_review_required"] = (
                    bool(review_flags) or summary.get("gate") != "auto"
                )
                if is_partial_album:
                    summary["match_method"] = "partial"
                elif "positional_match" in review_flags:
                    summary["match_method"] = "positional"
                summary["checkpoint_status"] = "report_recomputed"
                summary["report_recomputed_from_checkpoint"] = True
                checkpoint["summary"] = summary
                _write_json(checkpoint_path, checkpoint)
            else:
                summary["report_recomputed_from_checkpoint"] = True
            summaries.append(summary)
            reported_release_id = summary.get("release_id")
            if reported_release_id:
                release_id = _canonical_uuid(reported_release_id)
                if release_id not in release_records:
                    cached = cached_release
                    if cached is None:
                        cache_path = output_dir / "releases" / f"{release_id}.json"
                        cached = _load_release_cache(cache_path, release_id)
                    if cached:
                        release, metadata = cached
                        metadata["source"] = summary.get("release_cache_source", metadata["source"])
                        release_records[release_id] = {**metadata, "release": release}
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            summaries.append(
                _error_summary(
                    row, f"saved checkpoint invalid: {error}", release_id=_inventory_release_id(row)
                )
            )

    _load_report_cache_records(release_ids, output_dir, release_records)
    return summaries, release_records


def _write_markdown(path: Path, report: dict[str, Any]) -> None:
    counts = report["counts"]
    lines = [
        "# Music tagger batch dry run",
        "",
        f"Generated: {report['generated_at_utc']}",
        f"Inventory: `{report['inventory']}`",
        f"Inventory SHA-256: `{report['inventory_sha256']}`",
        f"Library root: `{report['library_root']}`",
        (
            f"Selection: `{report['selection']}` ({counts['selected']} albums, "
            f"{counts['release_ids']} unique releases)"
        ),
        "",
        "## Summary",
        "",
        f"- Completed: {counts['completed']}; errors or identity drift: {counts['errors']}",
        f"- Gate: {json.dumps(counts['gate'], sort_keys=True)}",
        f"- Albums requiring operator review: {counts['operator_review_required']}",
        f"- Review flags: {json.dumps(counts['review_flags'], sort_keys=True)}",
        (
            f"- Proposed changes: {counts['proposed_changes']} across "
            f"{counts['changed_files_estimate']} files"
        ),
        f"- Estimated rewritten audio bytes: {counts['changed_bytes_estimate']:,}",
        f"- Byte estimate complete: {str(counts['changed_bytes_estimate_complete']).lower()}",
        f"- Checkpoint provenance: {json.dumps(counts['checkpoint_status'], sort_keys=True)}",
        f"- Changed fields: {json.dumps(counts['changed_fields'], sort_keys=True)}",
        f"- Unresolved ISRC conflicts: {counts['unresolved_conflicts']}",
        (
            f"- MusicBrainz release fetches: {counts['network_release_fetches']}; "
            f"cache hits: {counts['release_cache_hits']}; retries: {counts['fetch_retries']}; "
            f"redirects: {counts['redirected_releases']}"
        ),
        "",
        (
            "Gate reports the existing tag safety gate. Gate auto does not approve a refresh: "
            "partial albums, positional matching, unmatched tracks, missing duration comparisons, "
            "and duration deviations over 2 seconds are separately flagged for operator review."
        ),
        "",
        (
            "The byte estimate sums current sizes for files with proposed writable tag changes. "
            "It estimates future tag writes; this dry run wrote no audio files."
        ),
        "",
        "## Album results",
        "",
        (
            "| Album directory | Gate | Track coverage | Duration match | Changed fields | "
            "Review flags | Diff |"
        ),
        "|---|---:|---:|---:|---|---|---|",
    ]
    for summary in report["albums"]:
        duration = summary.get("duration", {})
        matched = summary.get("matched_track_count", 0)
        track_count = summary.get("counted_track_count", summary.get("track_count", 0))
        within = duration.get("tracks_within_2s", 0)
        compared = duration.get("tracks_compared", 0)
        max_deviation = duration.get("max_deviation_secs")
        duration_text = f"{within}/{compared} ≤2s; max {max_deviation}s"
        fields = ", ".join(summary.get("changed_field_counts", {})) or "—"
        flags = ", ".join(summary.get("operator_review_flags", [])) or "—"
        diff_path = summary.get("diff_file", "—")
        diff_link = f"[`diff`]({diff_path})" if diff_path != "—" else "—"
        album_directory = summary["directory"].replace("|", "\\|")
        lines.append(
            f"| {album_directory} | {summary.get('gate', 'unavailable')} | "
            f"{matched}/{track_count} | {duration_text} | {fields} | {flags} | {diff_link} |"
        )
    if report["errors"]:
        lines.extend(["", "## Errors", ""])
        for error in report["errors"]:
            lines.append(f"- `{error['directory']}`: {error['error']}")
    lines.append("")
    atomic_write_text(path, "\n".join(lines))
