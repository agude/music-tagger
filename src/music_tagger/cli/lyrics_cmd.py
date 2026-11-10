"""Lyric commands: lyrics, lyrics-batch, and lyrics-decide."""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from mutagen import MutagenError

from ..lrclib import (
    LrclibClient,
    Outcome,
    Selection,
    TrackEvidence,
    Verdict,
    fetch_explicit,
    lookup,
)
from ..lyrics import LyricsEntry, Source, inspect_track, lyric_sidecars
from ..lyrics_batch import (
    AUTO_LENGTH_SECS,
    HAS_LYRICS,
    INSPECTION_LABEL,
    NEEDS_LOOKUP,
    QUEUED_OUTCOMES,
    AlbumReport,
    Candidate,
    Decision,
    Decisions,
    PendingEntry,
    confident_choice,
    decision_key,
    find_decision,
    find_pending,
    load_decisions,
    load_pending,
    merge_pending,
    render_pending,
    save_decisions,
    save_pending,
    undecided,
)
from ..lyrics_embed import apply_embed, parse_lrc, plan_embed
from ..lyrics_write import SidecarPlan, WriteStatus, apply_plan, plan_sidecar_write
from ..tags import (
    AlbumTags,
    TrackTags,
    UnsupportedFileError,
    disc_number,
    parse_position,
    read_album,
)
from .common import _add_log_arguments, _BatchLog, _log_path
from .library import _find_album_dirs

EXIT_LYRICS_UNRESOLVED = 3  # A requested write waits on a choice only the operator can make.


_LYRICS_UNRESOLVED = frozenset({Outcome.AMBIGUOUS, Outcome.REVIEW, Outcome.MISSING_METADATA})


_LYRICS_FAILED = frozenset({Outcome.PROVIDER_FAILURE, Outcome.RATE_LIMITED})


def _lyrics_usage_error(args: argparse.Namespace) -> str | None:
    """Return why the lyrics flags conflict, or None. Checked before any work starts."""
    modes = [
        flag
        for flag, on in (
            ("--lookup", args.lookup),
            ("--fetch", args.fetch),
            ("--embed", args.embed),
        )
        if on
    ]
    writing = args.fetch or args.embed
    if len(modes) > 1:
        return f"{' and '.join(modes)} cannot be combined; choose one mode"
    if args.dry_run and not writing:
        return "--dry-run needs --fetch or --embed; inspect and lookup never write"
    if args.replace and not writing:
        return "--replace needs --fetch or --embed; there is nothing to replace"
    if args.lyrics_id is not None and not (args.fetch or args.lookup):
        return "--lyrics-id needs --fetch or --lookup; it selects an LRCLIB record"
    if args.lyrics_id is not None and args.embed:
        return "--embed reads local sidecars only; it cannot take --lyrics-id"
    if args.lyrics_id is not None and args.track is None:
        return "--lyrics-id needs --track; a record must not apply to the whole album"
    if args.disc is not None and args.track is None:
        return "--disc needs --track"
    return None


def _lyrics_tracks(album: AlbumTags, track_no: int | None, disc: int | None) -> list[TrackTags]:
    if track_no is None:
        return list(album.tracks)
    matches = [
        t
        for t in album.tracks
        if parse_position(t.tags.get("tracknumber", "")) == track_no
        and (disc is None or disc_number(t) == disc)
    ]
    if len(matches) != 1:
        where = f" on disc {disc}" if disc is not None else ""
        raise ValueError(f"track {track_no}{where} matches {len(matches)} tracks; need one")
    return matches


def _describe_lyric_entry(entry: LyricsEntry) -> str:
    if entry.source is Source.EMBEDDED:
        detail = " ".join(part for part in (entry.language, entry.description) if part)
        where = f"embedded ({detail})" if detail else "embedded"
    else:
        where = f"{entry.source.value} ({entry.path.name if entry.path else ''})"
    return f"{where}: {entry.kind.value}"


def _print_lyrics_inspection(tracks: list[TrackTags]) -> None:
    results = [inspect_track(track.path) for track in tracks]
    counts: dict[str, int] = {}
    for result in results:
        print(f"{result.path.name}: {result.status}")
        for entry in result.entries:
            print(f"    {_describe_lyric_entry(entry)}")
        for error in result.errors:
            print(f"    error: {error}")
        counts[result.status] = counts.get(result.status, 0) + 1
    print("\nCoverage: " + ", ".join(f"{s} {n}" for s, n in sorted(counts.items())))


def _print_lyrics_selection(track: TrackTags, selection: Selection) -> None:
    print(f"{track.path.name}: {selection.outcome.value}")
    print(f"    {selection.reason}")
    for verdict in selection.verdicts:
        mark = " (plausible)" if verdict.plausible else ""
        print(f"    record {verdict.record.id}{mark}: {'; '.join(verdict.notes)}")


def _fetch_lyrics_track(
    track: TrackTags,
    selection: Selection,
    *,
    replace: bool,
    dry_run: bool,
    log: _BatchLog,
) -> tuple[str, bool]:
    """Plan and, unless dry run, write one track's sidecar. Returns (label, failed).

    Only a SELECTED record reaches the writer. Any other outcome is labelled
    with that outcome and writes nothing.
    """
    if selection.outcome is not Outcome.SELECTED or selection.lyrics is None:
        return selection.outcome.value, False
    if selection.record is None:
        raise AssertionError("a selected lyric always has a record")

    plan: SidecarPlan = plan_sidecar_write(
        inspect_track(track.path),
        selection.lyrics.kind,
        selection.lyrics.text,
        replace=replace,
    )
    print(f"    {plan.status.value}: {plan.reason}")
    if plan.status is WriteStatus.REFUSED:
        return plan.status.value, True
    if plan.status is not WriteStatus.WRITE:
        return plan.status.value, False
    if dry_run:
        return "would write", False

    try:
        apply_plan(plan)
    except OSError as exc:
        print(f"    error: {exc}", file=sys.stderr)
        return "failed", True

    kind = selection.lyrics.kind.value
    if plan.content is not None and plan.target is not None:
        how = "replaced existing lyrics" if plan.replaces else "new file"
        log.add(
            f"- `{track.path.name}`: wrote `{plan.target.name}` from LRCLIB record "
            f"{selection.record.id} ({kind}, {how})"
        )
    for path in plan.remove:
        log.add(f"- `{track.path.name}`: removed stale `{path.name}`")
    return "wrote", False


def _embed_lyrics_track(
    track: TrackTags,
    *,
    replace: bool,
    dry_run: bool,
    log: _BatchLog,
) -> tuple[str, bool]:
    """Plan and, unless dry run, embed one track's sidecar. Returns (label, failed)."""
    plan = plan_embed(inspect_track(track.path), replace=replace)
    print(f"    {plan.status.value}: {plan.reason}")
    if plan.status is WriteStatus.REFUSED:
        return plan.status.value, True
    if plan.status is not WriteStatus.WRITE:
        return plan.status.value, False
    if dry_run:
        return "would write", False
    if plan.source is None:
        raise AssertionError("a write plan always has a source")

    try:
        apply_embed(plan)
    except (OSError, MutagenError, UnsupportedFileError) as exc:
        print(f"    error: {exc}", file=sys.stderr)
        return "failed", True

    how = "replaced existing lyrics" if plan.replaces else "new tag"
    log.add(f"- `{track.path.name}`: embedded `{plan.source.path.name}` as {plan.tag} ({how})")
    return "wrote", False


def _embed_lyrics(
    album: AlbumTags,
    tracks: list[TrackTags],
    *,
    replace: bool,
    dry_run: bool,
    log_path: Path | None,
) -> None:
    log = _BatchLog(log_path, _lyrics_log_header("## Lyrics embed", album))
    labels: dict[str, int] = {}
    failed = False
    try:
        for track in tracks:
            label, track_failed = _embed_lyrics_track(
                track, replace=replace, dry_run=dry_run, log=log
            )
            labels[label] = labels.get(label, 0) + 1
            failed = failed or track_failed
    finally:
        log.close()

    summary = ", ".join(f"{count} {label}" for label, count in sorted(labels.items()))
    print(f"\nSummary: {summary or 'no tracks'}")
    if dry_run:
        print("(dry run — nothing written)")
    if failed:
        sys.exit(1)


def _lyrics(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger lyrics",
        description="Inspect lyrics for an album, look up LRCLIB candidates, or write "
        "sidecar lyrics. With no mode, only inspects: no network, no writes.",
    )
    parser.add_argument("path", type=Path, help="Album directory.")
    parser.add_argument(
        "--lookup",
        action="store_true",
        help="Search LRCLIB and show each candidate with its evidence. "
        "Uses the network; writes nothing.",
    )
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="Choose LRCLIB lyrics and save them as <audio-stem>.lrc or .txt beside each track.",
    )
    parser.add_argument(
        "--embed",
        action="store_true",
        help="Write each track's sidecar lyrics into its tags: .lrc if present, else .txt. "
        "A backup for when sidecars are lost. No network.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="With --fetch or --embed, show what would be written.",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="With --fetch, replace existing lyrics and remove a stale sidecar of the other type. "
        "With --embed, replace a different value in the lyric tag.",
    )
    parser.add_argument("--track", type=int, metavar="N", help="Only track number N.")
    parser.add_argument(
        "--disc", type=int, metavar="D", help="Disc number D, with --track, on multi-disc albums."
    )
    parser.add_argument(
        "--lyrics-id",
        type=int,
        metavar="ID",
        help="Use LRCLIB record ID for the --track track instead of searching. Needs --track.",
    )
    _add_log_arguments(parser)
    args = parser.parse_args(argv)

    if message := _lyrics_usage_error(args):
        parser.error(message)

    target: Path = args.path.resolve()
    if not target.is_dir():
        print(f"Error: {target} is not a directory.", file=sys.stderr)
        sys.exit(1)
    album = read_album(target)
    if not album.tracks:
        print(f"Error: no audio files in {target}.", file=sys.stderr)
        sys.exit(1)
    try:
        tracks = _lyrics_tracks(album, args.track, args.disc)
    except ValueError as exc:
        print(f"Error: {exc}.", file=sys.stderr)
        sys.exit(1)

    if args.embed:
        _embed_lyrics(
            album,
            tracks,
            replace=args.replace,
            dry_run=args.dry_run,
            log_path=None if args.dry_run else _log_path(args),
        )
        return
    if not args.lookup and not args.fetch:
        _print_lyrics_inspection(tracks)
        return

    client = LrclibClient()
    log_path = None if args.dry_run else _log_path(args)
    log = _BatchLog(log_path, _lyrics_log_header("## Lyrics", album))
    labels: dict[str, int] = {}
    failed = unresolved = False
    try:
        for track in tracks:
            evidence = _track_evidence(track)
            if args.lyrics_id is not None:
                selection = fetch_explicit(client, args.lyrics_id, evidence)
            else:
                selection = lookup(client, evidence)
            _print_lyrics_selection(track, selection)
            if selection.outcome in _LYRICS_FAILED:
                failed = True
            if args.fetch and selection.outcome in _LYRICS_UNRESOLVED:
                unresolved = True
            if args.fetch:
                label, track_failed = _fetch_lyrics_track(
                    track,
                    selection,
                    replace=args.replace,
                    dry_run=args.dry_run,
                    log=log,
                )
                failed = failed or track_failed
            else:
                label = selection.outcome.value
            labels[label] = labels.get(label, 0) + 1
    finally:
        log.close()

    summary = ", ".join(f"{count} {label}" for label, count in sorted(labels.items()))
    print(f"\nSummary: {summary or 'no tracks'}")
    if args.dry_run:
        print("(dry run — nothing written)")
    if failed:
        sys.exit(1)
    if unresolved:
        sys.exit(EXIT_LYRICS_UNRESOLVED)


def _track_evidence(track: TrackTags) -> TrackEvidence:
    return TrackEvidence(
        title=track.tags.get("title", ""),
        artist=track.tags.get("artist", ""),
        album=track.tags.get("album", ""),
        duration_secs=track.duration_secs or None,
    )


def _lyrics_log_header(title: str, album: AlbumTags) -> list[str]:
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    return [
        title,
        "",
        f"- **Date:** {stamp}",
        f"- **Album:** {album.artist} — {album.album}",
        "",
    ]


_PENDING_ENV_VAR = "MUSIC_TAGGER_LYRICS_PENDING"


_DECISIONS_ENV_VAR = "MUSIC_TAGGER_LYRICS_DECISIONS"


def _state_path(override: Path | None, env_var: str, name: str) -> Path:
    if override is not None:
        return override
    value = os.environ.get(env_var)
    return Path(value) if value else Path.home() / "Projects/music-tagger" / name


def _add_state_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--pending",
        type=Path,
        default=None,
        help=f"Pending options file (default: ${_PENDING_ENV_VAR}, "
        "else ~/Projects/music-tagger/lyrics-pending.json).",
    )
    parser.add_argument(
        "--decisions",
        type=Path,
        default=None,
        help=f"Decisions file (default: ${_DECISIONS_ENV_VAR}, "
        "else ~/Projects/music-tagger/lyrics-decisions.json).",
    )


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


class _RateLimited(Exception):
    """LRCLIB refused requests for longer than the client waits. The batch stops."""


def _candidate(verdict: Verdict, evidence: TrackEvidence) -> Candidate:
    record = verdict.record
    delta: float | None = None
    if record.duration_secs is not None and evidence.duration_secs is not None:
        delta = round(record.duration_secs - evidence.duration_secs, 1)
    last_line_delta: float | None = None
    if record.synced_lyrics and evidence.duration_secs is not None:
        stamps = parse_lrc(record.synced_lyrics)
        if stamps:
            last_line_delta = round(max(ms for _, ms in stamps) / 1000 - evidence.duration_secs, 1)
    offers: tuple[str, ...]
    if record.instrumental:
        offers = ("instrumental",)
    else:
        offers = tuple(
            label
            for label, text in (("synced", record.synced_lyrics), ("plain", record.plain_lyrics))
            if text
        )
    return Candidate(
        id=record.id,
        title=record.track_name,
        artist=record.artist_name,
        album=record.album_name,
        duration_delta=delta,
        plausible=verdict.plausible,
        album_matches=verdict.album_matches,
        offers=offers,
        notes=verdict.notes,
        last_line_delta=last_line_delta,
        identity_matches=verdict.identity_matches,
    )


def _release_track_id(track: TrackTags) -> str:
    """The file's MusicBrainz release track ID, or "" when it has none."""
    return track.tags.get("musicbrainz_releasetrackid", "")


def _pending_entry(
    track: TrackTags,
    album_dir: Path,
    outcome: str,
    reason: str,
    candidates: tuple[Candidate, ...],
) -> PendingEntry:
    return PendingEntry(
        album=str(album_dir),
        file=track.path.name,
        track=parse_position(track.tags.get("tracknumber", "")),
        disc=disc_number(track),
        outcome=outcome,
        reason=reason,
        candidates=candidates,
        release_track_id=_release_track_id(track),
    )


def _batch_fetch_track(
    track: TrackTags,
    client: LrclibClient,
    report: AlbumReport,
    *,
    decisions: Decisions,
    dry_run: bool,
    log: _BatchLog,
) -> None:
    """Fetch one track's sidecar when exactly one record is certain; save the rest."""
    if find_decision(decisions, str(report.directory), track.path.name, _release_track_id(track)):
        report.counts["decided"] += 1
        return
    status = inspect_track(track.path).status
    if status in HAS_LYRICS:
        report.counts["has lyrics"] += 1
        return
    if status not in NEEDS_LOOKUP:
        report.counts[INSPECTION_LABEL] += 1
        report.pending.append(
            _pending_entry(
                track, report.directory, INSPECTION_LABEL, f"lyric source is {status}", ()
            )
        )
        return

    evidence = _track_evidence(track)
    selection = lookup(client, evidence)
    _print_lyrics_selection(track, selection)
    if selection.outcome is Outcome.RATE_LIMITED:
        raise _RateLimited(f"{track.path.name}: {selection.reason}")
    if selection.outcome is Outcome.NO_MATCH:
        # A record can match the album and differ in length from this file. Keep those
        # for a decision, with the last-line check as evidence; drop the rest.
        hits = tuple(_candidate(v, evidence) for v in selection.verdicts if v.album_matches)
        if hits:
            report.counts["no_match pending"] += 1
            report.pending.append(
                _pending_entry(
                    track,
                    report.directory,
                    "no_match",
                    f"{selection.reason}; album matches are listed for a decision",
                    hits,
                )
            )
            return
    if selection.outcome.value in QUEUED_OUTCOMES:
        report.counts[selection.outcome.value] += 1
        candidates = tuple(_candidate(v, evidence) for v in selection.verdicts)
        report.pending.append(
            _pending_entry(
                track, report.directory, selection.outcome.value, selection.reason, candidates
            )
        )
        return

    if selection.outcome is Outcome.SELECTED:
        label, failed = _fetch_lyrics_track(
            track, selection, replace=False, dry_run=dry_run, log=log
        )
    else:
        label, failed = selection.outcome.value, selection.outcome is Outcome.PROVIDER_FAILURE
    report.counts[label] += 1
    report.failed = report.failed or failed


def _choose_record(
    album_dir: Path,
    track: TrackTags,
    entry: PendingEntry,
    record_id: int,
    *,
    client: LrclibClient,
    decisions: Decisions,
    decisions_path: Path,
    dry_run: bool,
    log: _BatchLog,
    plain_only: bool = False,
) -> tuple[bool, str]:
    """Write one saved record's lyrics for a pending track and record the decision.

    Returns (ok, message). A decision is recorded only when the sidecar is written or
    already matches. A dry run records nothing. plain_only writes the record's plain
    text. A decision that replaces an earlier skip keeps that skip's reason.
    """
    selection = fetch_explicit(client, record_id, _track_evidence(track), plain_only=plain_only)
    if selection.outcome is not Outcome.SELECTED:
        reason = selection.outcome.value
        return False, f"{track.path.name}: record {record_id} cannot be written: {reason}"
    label, failed = _fetch_lyrics_track(track, selection, replace=False, dry_run=dry_run, log=log)
    if dry_run:
        return label == "would write", f"{track.path.name}: {label} (record {record_id})"
    if failed or label not in ("wrote", "unchanged"):
        return False, f"{track.path.name}: {label}; no decision recorded"
    release_track_id = _release_track_id(track)
    found = find_decision(decisions, str(album_dir), track.path.name, release_track_id)
    reason = f"chose record {record_id}" + (" (plain text)" if plain_only else "")
    if found is not None:
        reason += f"; replaces skip: {found[1].reason}"
    key = (
        found[0]
        if found is not None
        else decision_key(str(album_dir), track.path.name, release_track_id)
    )
    decisions[key] = Decision(
        album=str(album_dir),
        file=track.path.name,
        track=entry.track,
        choice=record_id,
        reason=reason,
        decided=_now(),
        release_track_id=release_track_id,
    )
    save_decisions(decisions_path, decisions)
    return True, f"{track.path.name}: recorded record {record_id}"


def _auto_choose(
    report: AlbumReport,
    album: AlbumTags,
    client: LrclibClient,
    *,
    decisions: Decisions,
    decisions_path: Path,
    dry_run: bool,
    log: _BatchLog,
) -> None:
    """Record the choices the rules accept without a judgment. The rest stay pending."""
    tracks = {track.path.name: track for track in album.tracks}
    for entry in list(report.pending):
        candidate = confident_choice(entry)
        if candidate is None:
            continue
        ok, message = _choose_record(
            report.directory,
            tracks[entry.file],
            entry,
            candidate.id,
            client=client,
            decisions=decisions,
            decisions_path=decisions_path,
            dry_run=dry_run,
            log=log,
        )
        print(f"    auto: {message}")
        if not ok:
            report.counts["auto not written"] += 1
            report.failed = True
        else:
            report.counts["auto would choose" if dry_run else "auto chosen"] += 1
            report.pending.remove(entry)


def _refresh_decisions(
    album_dir: Path,
    album: AlbumTags,
    decisions: Decisions,
    decisions_path: Path,
    *,
    dry_run: bool,
) -> None:
    """Point each decision matched by release track ID at the file's current album and name.

    Saved on a real run only. A decision matched by its album and file name needs no update.
    """
    changed = False
    for track in album.tracks:
        found = find_decision(decisions, str(album_dir), track.path.name, _release_track_id(track))
        if found is None:
            continue
        key, decision = found
        if (decision.album, decision.file) == (str(album_dir), track.path.name):
            continue
        decisions[key] = replace(decision, album=str(album_dir), file=track.path.name)
        changed = True
    if changed and not dry_run:
        save_decisions(decisions_path, decisions)


def _batch_album(
    album_dir: Path,
    client: LrclibClient,
    *,
    decisions: Decisions,
    decisions_path: Path,
    auto: bool,
    embed: bool,
    dry_run: bool,
    log_path: Path | None,
) -> AlbumReport:
    """Fetch missing sidecars, record the confident choices, then embed when asked."""
    report = AlbumReport(album_dir)
    try:
        album = read_album(album_dir)
    except (OSError, MutagenError, ValueError) as exc:
        report.failed = True
        report.error = str(exc)
        return report

    _refresh_decisions(album_dir, album, decisions, decisions_path, dry_run=dry_run)
    fetch_log = _BatchLog(log_path, _lyrics_log_header("## Lyrics", album))
    try:
        for track in album.tracks:
            _batch_fetch_track(
                track, client, report, decisions=decisions, dry_run=dry_run, log=fetch_log
            )
        if auto:
            _auto_choose(
                report,
                album,
                client,
                decisions=decisions,
                decisions_path=decisions_path,
                dry_run=dry_run,
                log=fetch_log,
            )
    finally:
        fetch_log.close()

    if embed:
        # Embedding runs after the fetch and the choices, so new sidecars are embedded too.
        embed_log = _BatchLog(log_path, _lyrics_log_header("## Lyrics embed", album))
        try:
            for track in album.tracks:
                # A track without a sidecar has nothing to embed. In a dry run, a
                # sidecar that would be fetched is not on disk yet, so it is skipped too.
                if not lyric_sidecars(track.path):
                    continue
                label, failed = _embed_lyrics_track(
                    track, replace=False, dry_run=dry_run, log=embed_log
                )
                report.counts[f"embed {label}"] += 1
                report.failed = report.failed or failed
        finally:
            embed_log.close()
    return report


def _print_album_report(root: Path, report: AlbumReport) -> None:
    name = report.directory.name if report.directory == root else report.directory.relative_to(root)
    if report.error:
        print(f"{name}: error: {report.error}", file=sys.stderr)
        return
    summary = ", ".join(f"{n} {label}" for label, n in sorted(report.counts.items()))
    print(f"{name}: {summary or 'nothing to do'}")


def _lyrics_batch(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger lyrics-batch",
        description="Backfill lyrics across a library. Each track without lyrics gets a "
        "sidecar only when exactly one LRCLIB record is certain. Every other track is "
        "saved to the pending file with its candidate records, for lyrics-decide. "
        "With --embed, sidecars are also written into tags. Tracks that already have "
        "lyrics, or a recorded decision, are never searched.",
    )
    parser.add_argument(
        "path", type=Path, help="Library root, artist directory, or album directory."
    )
    parser.add_argument(
        "--embed", action="store_true", help="Also write each track's sidecar into its tags."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be written. Writes no sidecars, tags, log, or pending file.",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Also record the choices the rules accept without a judgment: a plausible "
        "album match with synchronized text, or an album match within "
        f"{AUTO_LENGTH_SECS:.0f} s whose last line falls inside the audio. The rest stay pending.",
    )
    _add_state_arguments(parser)
    _add_log_arguments(parser)
    args = parser.parse_args(argv)

    root: Path = args.path.resolve()
    if not root.is_dir():
        print(f"Error: {root} is not a directory.", file=sys.stderr)
        sys.exit(1)
    album_dirs = _find_album_dirs(root)
    if not album_dirs:
        print(f"Error: no audio files under {root}.", file=sys.stderr)
        sys.exit(1)

    pending_path = _state_path(args.pending, _PENDING_ENV_VAR, "lyrics-pending.json")
    decisions_path = _state_path(args.decisions, _DECISIONS_ENV_VAR, "lyrics-decisions.json")
    try:
        decisions = load_decisions(decisions_path)
        previous = load_pending(pending_path)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    log_path = None if args.dry_run else _log_path(args)
    client = LrclibClient()
    reports: list[AlbumReport] = []
    stopped = ""
    try:
        for album_dir in album_dirs:
            report = _batch_album(
                album_dir,
                client,
                decisions=decisions,
                decisions_path=decisions_path,
                auto=args.auto,
                embed=args.embed,
                dry_run=args.dry_run,
                log_path=log_path,
            )
            reports.append(report)
            _print_album_report(root, report)
    except _RateLimited as exc:
        stopped = str(exc)

    # Albums this run could not read keep their saved entries; albums it processed
    # are replaced, so a decided or settled track leaves the pending file.
    processed = {str(r.directory) for r in reports if not r.error}
    fresh = [entry for r in reports if not r.error for entry in r.pending]
    queued = sum(len(r.pending) for r in reports)
    if not args.dry_run:
        merged = merge_pending(previous, fresh, processed)
        save_pending(pending_path, merged, _now())
        print(f"Pending: {len(merged)} tracks in {pending_path}")

    totals: Counter[str] = Counter()
    for report in reports:
        totals.update(report.counts)
    failed = any(report.failed for report in reports)
    print(f"\nAlbums: {len(reports)} of {len(album_dirs)} processed")
    print("Tracks: " + (", ".join(f"{n} {k}" for k, n in sorted(totals.items())) or "none"))
    print(f"Pending this run: {queued}")
    if args.dry_run:
        print("(dry run — nothing written)")
    if stopped:
        print(f"Stopped on LRCLIB rate limit at {stopped}. Rerun to finish.", file=sys.stderr)
    if stopped or failed:
        sys.exit(1)
    if queued:
        sys.exit(EXIT_LYRICS_UNRESOLVED)


def _track_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("path", type=Path, help="Album directory.")
    parser.add_argument(
        "--track",
        type=int,
        metavar="N",
        help="Track number N. Use --file when two files share a number.",
    )
    parser.add_argument(
        "--disc", type=int, metavar="D", help="Disc number D, on multi-disc albums."
    )
    parser.add_argument(
        "--file",
        metavar="NAME",
        help="Audio file name, as lyrics-decide list shows it. Selects the file directly.",
    )


def _select_track(album: AlbumTags, args: argparse.Namespace) -> TrackTags:
    """Return the one track a decision names: by file name when given, else by number."""
    if args.file is None:
        return _lyrics_tracks(album, args.track, args.disc)[0]
    named = [t for t in album.tracks if t.path.name == args.file]
    if len(named) != 1:
        raise ValueError(f"no audio file named {args.file!r} in this album")
    track = named[0]
    if args.track is not None and parse_position(track.tags.get("tracknumber", "")) != args.track:
        raise ValueError(f"{args.file!r} is not track {args.track}")
    if args.disc is not None and disc_number(track) != args.disc:
        raise ValueError(f"{args.file!r} is not on disc {args.disc}")
    return track


def _may_replace_skip(args: argparse.Namespace, decided: Decision) -> bool:
    """A choice with --replace-skip may replace a skip. Nothing replaces a choice."""
    return decided.choice is None and bool(getattr(args, "replace_skip", False))


def _resolve_pending_track(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    pending_path: Path,
    decisions: Decisions,
) -> tuple[Path, AlbumTags, TrackTags, PendingEntry]:
    """Find the one track a decision names. Refuses with exit 2 when it is not pending."""
    if args.track is None and args.file is None:
        parser.error("give --track N or --file NAME")
    target: Path = args.path.resolve()
    if not target.is_dir():
        print(f"Error: {target} is not a directory.", file=sys.stderr)
        sys.exit(1)
    album = read_album(target)
    try:
        track = _select_track(album, args)
    except ValueError as exc:
        print(f"Error: {exc}.", file=sys.stderr)
        sys.exit(2)
    release_track_id = _release_track_id(track)
    found = find_decision(decisions, str(target), track.path.name, release_track_id)
    if found is not None and not _may_replace_skip(args, found[1]):
        decided = found[1]
        what = "skipped" if decided.choice is None else f"record {decided.choice}"
        print(f"Error: {track.path.name} is already decided ({what}).", file=sys.stderr)
        sys.exit(2)
    entry = find_pending(load_pending(pending_path), str(target), track.path.name, release_track_id)
    if entry is None:
        print(
            f"Error: {track.path.name} has no pending options; run lyrics-batch first.",
            file=sys.stderr,
        )
        sys.exit(2)
    return target, album, track, entry


def _lyrics_decide(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-tagger lyrics-decide",
        description="Show the lyric options lyrics-batch saved, and record a choice or a "
        "skip for each track. A choice must be one of the saved record IDs and is written "
        "as a sidecar through the lyrics --lyrics-id path.",
    )
    actions = parser.add_subparsers(dest="action", required=True)

    listing = actions.add_parser(
        "list", help="Show undecided tracks and their options. No network."
    )
    listing.add_argument("--album", type=Path, default=None, help="Only this album directory.")
    _add_state_arguments(listing)

    choose = actions.add_parser("choose", help="Write one saved record's lyrics for a track.")
    _track_arguments(choose)
    choose.add_argument(
        "--id",
        dest="record_id",
        type=int,
        required=True,
        metavar="ID",
        help="A record ID from the saved options.",
    )
    choose.add_argument(
        "--plain",
        action="store_true",
        help="Write the record's plain text as a .txt, even when it has synchronized text.",
    )
    choose.add_argument(
        "--replace-skip",
        action="store_true",
        help="Replace an earlier skip for this track. A recorded choice is never replaced.",
    )
    choose.add_argument(
        "--dry-run", action="store_true", help="Show what would be written; record nothing."
    )
    _add_state_arguments(choose)
    _add_log_arguments(choose)

    skip = actions.add_parser("skip", help="Record that a track gets no lyrics. Writes no file.")
    _track_arguments(skip)
    skip.add_argument("--reason", required=True, help="Why the track has no lyrics.")
    _add_state_arguments(skip)

    args = parser.parse_args(argv)
    pending_path = _state_path(args.pending, _PENDING_ENV_VAR, "lyrics-pending.json")
    decisions_path = _state_path(args.decisions, _DECISIONS_ENV_VAR, "lyrics-decisions.json")
    try:
        decisions = load_decisions(decisions_path)
        if args.action == "list":
            entries = undecided(load_pending(pending_path), decisions)
            if args.album is not None:
                wanted = str(args.album.resolve())
                entries = [e for e in entries if e.album == wanted]
            print(render_pending(entries), end="")
            return
        target, album, track, entry = _resolve_pending_track(parser, args, pending_path, decisions)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.action == "skip":
        reason = args.reason.strip()
        if not reason:
            parser.error("--reason must not be empty")
        release_track_id = _release_track_id(track)
        decisions[decision_key(str(target), track.path.name, release_track_id)] = Decision(
            album=str(target),
            file=track.path.name,
            track=entry.track,
            choice=None,
            reason=reason,
            decided=_now(),
            release_track_id=release_track_id,
        )
        save_decisions(decisions_path, decisions)
        print(f"{track.path.name}: skipped. {reason}")
        return

    saved = {c.id: c for c in entry.candidates}
    if args.record_id not in saved:
        ids = ", ".join(str(i) for i in sorted(saved)) or "none"
        print(
            f"Error: record {args.record_id} is not a saved option for {track.path.name} "
            f"(saved: {ids}).",
            file=sys.stderr,
        )
        sys.exit(2)

    client = LrclibClient()
    log = _BatchLog(
        None if args.dry_run else _log_path(args), _lyrics_log_header("## Lyrics", album)
    )
    try:
        ok, message = _choose_record(
            target,
            track,
            entry,
            args.record_id,
            client=client,
            decisions=decisions,
            decisions_path=decisions_path,
            dry_run=args.dry_run,
            log=log,
            plain_only=args.plain,
        )
    finally:
        log.close()
    print(message, file=sys.stdout if ok else sys.stderr)
    if args.dry_run:
        print("(dry run — nothing written)")
    if not ok:
        sys.exit(1)
