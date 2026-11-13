from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from music_tagger.cli import batch_dry_run
from music_tagger.cli.batch_dry_run import (
    _AlbumRead,
    _checkpoint_paths,
    _load_checkpoint,
    _load_release_cache,
    _rows_needing_retry,
    _run_batch,
    _save_checkpoint,
    _select_inventory_rows,
    _summaries_from_checkpoints,
    _summarize_album,
    _verify_identity,
    _write_json,
)
from music_tagger.musicbrainz import MBRelease, MBTrack
from music_tagger.tagger import (
    AlbumResult,
    CandidateMatch,
    MatchStats,
    TagConflict,
    TrackDiff,
)
from music_tagger.tags import AlbumTags, TagChange, TrackTags

RELEASE_ID = "f7890790-ff4b-4524-9298-5c3bc66af976"
OTHER_RELEASE_ID = "e6b4a290-2e81-4563-aed9-5e1af43d220d"


def make_inventory_row(root: Path, release_ids: list[str] | None = None) -> dict[str, Any]:
    directory = "Artist/Album"
    relative_file = f"{directory}/01. Song.flac"
    return {
        "directory": directory,
        "distinct_valid_release_uuids": release_ids or [RELEASE_ID],
        "files": [
            {
                "path": relative_file,
                "musicbrainz_albumid_raw_values": [RELEASE_ID],
            }
        ],
    }


def make_album(root: Path, *, release_ids: tuple[str, ...] = (RELEASE_ID,)) -> AlbumTags:
    directory = root / "Artist" / "Album"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "01. Song.flac"
    path.write_bytes(b"audio")
    track = TrackTags(
        path=path,
        tags={"title": "Song", "album": "Album", "albumartist": "Artist", "tracknumber": "1"},
        duration_secs=120,
        format="flac",
        release_id_values=release_ids,
    )
    return AlbumTags(directory=directory, tracks=[track])


def make_match(*, flags: list[str] | None = None) -> CandidateMatch:
    release = MBRelease(id=RELEASE_ID, title="Album", track_count=2)
    return CandidateMatch(
        release=release,
        stats=MatchStats(
            track_count_match=False,
            tracks_within_2s=1,
            tracks_compared=1,
            max_deviation_secs=0.2,
        ),
        flags=flags or [],
    )


def test_identity_check_validates_every_raw_release_id_value(tmp_path: Path) -> None:
    album = make_album(tmp_path)
    row = make_inventory_row(tmp_path)

    assert _verify_identity(row, album, tmp_path) == []

    album.tracks[0].release_id_values = (RELEASE_ID, "bad-release-id")
    issues = _verify_identity(row, album, tmp_path)
    assert any("release ID values changed" in issue for issue in issues)
    assert any("invalid raw release ID" in issue for issue in issues)


def test_partial_album_requires_review_without_candidate_partial_flag(tmp_path: Path) -> None:
    album = make_album(tmp_path / "library")
    release = MBRelease(id=RELEASE_ID, title="Album", track_count=2)
    result = AlbumResult(album=album, release=release, diffs=[TrackDiff(album.tracks[0], [])])

    summary = _summarize_album(
        make_inventory_row(tmp_path),
        album,
        result,
        make_match(),
        RELEASE_ID,
        tmp_path / "library",
        tmp_path / "reports",
        {album.tracks[0].path: album.tracks[0].path.stat().st_size},
    )

    assert summary["gate"] == "auto"
    assert summary["match_flags"] == []
    assert "partial_album" in summary["operator_review_flags"]
    assert summary["operator_review_required"] is True
    assert summary["match_method"] == "partial"


def test_gate_summary_keeps_overwrite_and_isrc_verdicts(tmp_path: Path) -> None:
    album = make_album(tmp_path / "library")
    track = album.tracks[0]
    release = MBRelease(id=RELEASE_ID, title="Album", track_count=1)
    match = CandidateMatch(
        release=release,
        stats=MatchStats(True, 1, 1, 0.2),
    )
    overwrite = AlbumResult(
        album=album,
        release=release,
        diffs=[TrackDiff(track, [TagChange("title", "Song", "New song")])],
    )
    blocked = AlbumResult(
        album=album,
        release=release,
        diffs=[TrackDiff(track, [], conflicts=[TagConflict("isrc", "OLD", ["NEW"])])],
    )

    file_sizes = {track.path: track.path.stat().st_size}
    overwrite_summary = _summarize_album(
        make_inventory_row(tmp_path),
        album,
        overwrite,
        match,
        RELEASE_ID,
        album.directory,
        tmp_path,
        file_sizes,
    )
    blocked_summary = _summarize_album(
        make_inventory_row(tmp_path),
        album,
        blocked,
        match,
        RELEASE_ID,
        album.directory,
        tmp_path,
        file_sizes,
    )

    assert overwrite_summary["gate"] == "review"
    assert blocked_summary["gate"] == "blocked"
    assert blocked_summary["conflicts"][0]["field"] == "isrc"


def test_checkpoint_reuse_requires_matching_inventory_inputs_and_release_cache(
    tmp_path: Path,
) -> None:
    root = tmp_path / "library"
    album = make_album(root)
    row = make_inventory_row(root)
    output_dir = tmp_path / "reports"
    for directory in ("checkpoints", "diffs", "releases"):
        (output_dir / directory).mkdir(parents=True)
    checkpoint_path, diff_path = _checkpoint_paths(row, output_dir)
    _write_json(
        diff_path, {"release_id": RELEASE_ID, "tracks": [{"path": str(album.tracks[0].path)}]}
    )
    release_snapshot_sha256 = "snapshot-hash"
    summary = {"status": "completed", "release_id": RELEASE_ID}
    _save_checkpoint(
        row,
        output_dir,
        "inventory-hash",
        "input-hash",
        summary,
        release_id=RELEASE_ID,
        release_snapshot_sha256=release_snapshot_sha256,
        diff_path=diff_path,
    )

    assert _load_checkpoint(
        row,
        output_dir,
        root,
        "inventory-hash",
        "input-hash",
        RELEASE_ID,
        release_snapshot_sha256,
    ) == (json.loads(checkpoint_path.read_text()), "reused")
    assert (
        _load_checkpoint(
            row,
            output_dir,
            root,
            "inventory-hash",
            "input-hash",
            RELEASE_ID,
            release_snapshot_sha256,
            keep_existing_credits=True,
        )[1]
        == "stale_credit_policy"
    )
    assert _rows_needing_retry([row], output_dir, "inventory-hash", keep_existing_credits=True) == [
        row
    ]
    assert (
        _load_checkpoint(
            row,
            output_dir,
            root,
            "changed-inventory",
            "input-hash",
            RELEASE_ID,
            release_snapshot_sha256,
        )[1]
        == "stale_inventory"
    )
    assert (
        _load_checkpoint(
            row,
            output_dir,
            root,
            "inventory-hash",
            "changed-input",
            RELEASE_ID,
            release_snapshot_sha256,
        )[1]
        == "stale_input"
    )
    assert (
        _load_checkpoint(
            row,
            output_dir,
            root,
            "inventory-hash",
            "input-hash",
            RELEASE_ID,
            "changed-release-cache",
        )[1]
        == "stale_release_snapshot"
    )

    _write_json(diff_path, {"release_id": RELEASE_ID, "tracks": []})
    assert (
        _load_checkpoint(
            row,
            output_dir,
            root,
            "inventory-hash",
            "input-hash",
            RELEASE_ID,
            release_snapshot_sha256,
        )[1]
        == "damaged_diff"
    )


def test_report_only_repairs_partial_album_review_flag(tmp_path: Path) -> None:
    root = tmp_path / "library"
    album = make_album(root)
    row = make_inventory_row(root)
    output_dir = tmp_path / "reports"
    for directory in ("checkpoints", "diffs", "releases"):
        (output_dir / directory).mkdir(parents=True)
    release = MBRelease(id=RELEASE_ID, title="Album", track_count=2)
    release_path = output_dir / "releases" / f"{RELEASE_ID}.json"
    _write_json(
        release_path,
        {"fetched_at_utc": "2026-11-10T00:00:00Z", "retries": 0, "release": release.to_dict()},
    )
    release_digest = hashlib.sha256(release_path.read_bytes()).hexdigest()
    _, diff_path = _checkpoint_paths(row, output_dir)
    _write_json(
        diff_path, {"release_id": RELEASE_ID, "tracks": [{"path": str(album.tracks[0].path)}]}
    )
    old_summary = {
        "directory": row["directory"],
        "release_id": RELEASE_ID,
        "status": "completed",
        "gate": "auto",
        "track_count": 1,
        "counted_track_count": 1,
        "release_track_count": 2,
        "operator_review_flags": [],
        "operator_review_required": False,
        "changed_field_counts": {},
        "changed_bytes_estimate": 0,
        "diff_file": str(diff_path),
        "release_cache_source": "musicbrainz",
    }
    _save_checkpoint(
        row,
        output_dir,
        "inventory-hash",
        "input-hash",
        old_summary,
        release_id=RELEASE_ID,
        release_snapshot_sha256=release_digest,
        diff_path=diff_path,
    )

    summaries, records = _summaries_from_checkpoints(
        [row], output_dir, root, "inventory-hash", [RELEASE_ID]
    )

    assert summaries[0]["match_method"] == "partial"
    assert summaries[0]["operator_review_required"] is True
    assert "partial_album" in summaries[0]["operator_review_flags"]
    assert summaries[0]["report_recomputed_from_checkpoint"] is True
    assert records[RELEASE_ID]["source"] == "musicbrainz"

    mismatched_summaries, _ = _summaries_from_checkpoints(
        [row],
        output_dir,
        root,
        "inventory-hash",
        [RELEASE_ID],
        keep_existing_credits=True,
    )
    assert mismatched_summaries[0]["status"] == "error"
    assert "credit-preservation policy" in mismatched_summaries[0]["error"]

    release_path.write_text("{}\n", encoding="utf-8")
    stale_summaries, _ = _summaries_from_checkpoints(
        [row], output_dir, root, "inventory-hash", [RELEASE_ID]
    )
    assert stale_summaries[0]["status"] == "error"
    assert "release snapshot hash" in stale_summaries[0]["error"]


def test_inventory_selection_excludes_custom_edits_and_rejects_unsafe_paths() -> None:
    candidate = {
        "directory": "Artist/Album",
        "distinct_valid_release_uuids": [RELEASE_ID],
        "dry_run_inventory_assessment": "strict_dry_run_review_candidate",
        "files": [],
    }
    custom = {**candidate, "directory": "Artist/Custom", "known_custom_edit": True}
    assert _select_inventory_rows({"albums": [candidate, custom]}) == [candidate]

    with pytest.raises(ValueError, match="escapes library root"):
        _select_inventory_rows({"albums": [{**candidate, "directory": "../escape"}]})


def test_retry_errors_selects_all_missing_failed_or_stale_inventory_checkpoints(
    tmp_path: Path,
) -> None:
    rows = [make_inventory_row(tmp_path) for _ in range(3)]
    rows[1]["directory"] = "Artist/Failed"
    rows[2]["directory"] = "Artist/Stale"
    checkpoints_dir = tmp_path / "reports" / "checkpoints"
    checkpoints_dir.mkdir(parents=True)
    checkpoint_path, _ = _checkpoint_paths(rows[0], tmp_path / "reports")
    _write_json(
        checkpoint_path,
        {
            "inventory_sha256": "inventory-hash",
            "complete": True,
            "summary": {"status": "completed"},
        },
    )
    failed_path, _ = _checkpoint_paths(rows[1], tmp_path / "reports")
    _write_json(
        failed_path,
        {
            "inventory_sha256": "inventory-hash",
            "complete": True,
            "summary": {"status": "error"},
        },
    )
    stale_path, _ = _checkpoint_paths(rows[2], tmp_path / "reports")
    _write_json(
        stale_path,
        {
            "inventory_sha256": "older-inventory",
            "complete": True,
            "summary": {"status": "completed"},
        },
    )

    retries = _rows_needing_retry(rows, tmp_path / "reports", "inventory-hash")

    assert [row["directory"] for row in retries] == ["Artist/Failed", "Artist/Stale"]


def test_load_release_cache_validates_release_identity(tmp_path: Path) -> None:
    release = MBRelease(id=RELEASE_ID, title="Album", track_count=1)
    cache_path = tmp_path / "release.json"
    _write_json(
        cache_path,
        {"fetched_at_utc": "2026-11-10T00:00:00Z", "retries": 2, "release": release.to_dict()},
    )

    cached = _load_release_cache(cache_path, RELEASE_ID)
    assert cached is not None
    assert cached[0].title == "Album"
    assert cached[1]["retries"] == 2
    assert _load_release_cache(cache_path, OTHER_RELEASE_ID) is None

    cache_path.write_text("{broken", encoding="utf-8")
    assert _load_release_cache(cache_path, RELEASE_ID) is None

    canonical_release = MBRelease(id=OTHER_RELEASE_ID, title="Album", track_count=1)
    redirect = {
        "requested_release_id": RELEASE_ID,
        "resolved_release_id": OTHER_RELEASE_ID,
        "url": f"https://musicbrainz.org/ws/2/release/{OTHER_RELEASE_ID}?fmt=json",
    }
    _write_json(
        cache_path,
        {
            "requested_release_id": RELEASE_ID,
            "resolved_release_id": OTHER_RELEASE_ID,
            "redirect": redirect,
            "release": canonical_release.to_dict(),
        },
    )
    redirected = _load_release_cache(cache_path, RELEASE_ID)
    assert redirected is not None
    assert redirected[0].id == OTHER_RELEASE_ID
    assert redirected[1]["redirect"] == redirect


def test_run_batch_writes_diff_then_reuses_checkpoint_and_release_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "library"
    album = make_album(root)
    row = make_inventory_row(root)
    release = MBRelease(
        id=OTHER_RELEASE_ID,
        title="Album",
        artist="Artist",
        track_count=1,
        discs={1: [MBTrack(number=1, title="Song", duration_ms=120_000, artist="Artist")]},
    )
    snapshot = _AlbumRead(
        album=album,
        fingerprint="input-hash",
        file_sizes={album.tracks[0].path: album.tracks[0].path.stat().st_size},
        identity_issues=[],
    )

    class FakeClient:
        def __init__(self) -> None:
            self.last_request_retry_count = 0
            self.fetch_calls = 0
            self.last_release_redirect = {
                "requested_release_id": RELEASE_ID,
                "resolved_release_id": OTHER_RELEASE_ID,
                "url": f"https://musicbrainz.org/ws/2/release/{OTHER_RELEASE_ID}?fmt=json",
            }

        def fetch_release(self, release_id: str) -> MBRelease:
            self.fetch_calls += 1
            assert release_id == RELEASE_ID
            return release

        def close(self) -> None:
            pass

    client = FakeClient()
    monkeypatch.setattr(batch_dry_run, "MusicBrainzClient", lambda: client)
    monkeypatch.setattr(batch_dry_run, "_read_album_snapshot", lambda _row, _root: snapshot)
    output_dir = tmp_path / "reports"
    for directory in ("checkpoints", "diffs", "releases"):
        (output_dir / directory).mkdir(parents=True)

    first_summaries, first_releases = _run_batch(
        [row],
        [RELEASE_ID],
        root,
        "inventory-hash",
        output_dir,
        read_workers=1,
        keep_existing_credits=True,
    )

    assert first_summaries[0]["status"] == "completed"
    assert first_summaries[0]["checkpoint_status"] == "new"
    assert first_summaries[0]["release_id"] == RELEASE_ID
    assert first_summaries[0]["resolved_release_id"] == OTHER_RELEASE_ID
    assert "release_id_redirected" in first_summaries[0]["operator_review_flags"]
    assert first_summaries[0]["diff_file"]
    assert first_releases[RELEASE_ID]["source"] == "musicbrainz"
    assert client.fetch_calls == 1
    checkpoint_path, diff_path = _checkpoint_paths(row, output_dir)
    assert checkpoint_path.is_file()
    assert json.loads(checkpoint_path.read_text())["keep_existing_credits"] is True
    assert json.loads(diff_path.read_text(encoding="utf-8"))["release_id"] == OTHER_RELEASE_ID

    second_summaries, second_releases = _run_batch(
        [row],
        [RELEASE_ID],
        root,
        "inventory-hash",
        output_dir,
        read_workers=1,
        keep_existing_credits=True,
    )

    assert second_summaries[0]["checkpoint_reused"] is True
    assert second_summaries[0]["checkpoint_status"] == "reused"
    assert second_releases[RELEASE_ID]["source"] == "disk_cache"
    assert client.fetch_calls == 1
    checkpoint_summaries, checkpoint_releases = _summaries_from_checkpoints(
        [row],
        output_dir,
        root,
        "inventory-hash",
        [RELEASE_ID],
        keep_existing_credits=True,
    )
    assert checkpoint_summaries[0]["resolved_release_id"] == OTHER_RELEASE_ID
    assert checkpoint_summaries[0]["operator_review_required"] is True
    assert checkpoint_releases[RELEASE_ID]["redirect"] == client.last_release_redirect


def test_batch_dry_run_command_writes_report_outside_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "library"
    album = make_album(root)
    row = make_inventory_row(root)
    row.update(
        dry_run_inventory_assessment="strict_dry_run_review_candidate",
        known_custom_edit=False,
    )
    other_row = {
        **row,
        "directory": "Artist/Other",
        "files": [{"path": "Artist/Other/01. Song.flac"}],
    }
    custom_row = {
        "dry_run_inventory_assessment": "strict_dry_run_review_candidate",
        "known_custom_edit": True,
    }
    inventory_path = tmp_path / "inventory.json"
    _write_json(
        inventory_path,
        {"library_root": str(root), "albums": [row, other_row, custom_row]},
    )

    received_policies: list[bool] = []

    def fake_run_batch(rows: list[dict[str, Any]], *_args: Any, **kwargs: Any) -> tuple[Any, Any]:
        received_policies.append(kwargs["keep_existing_credits"])
        return (
            [
                {
                    "directory": rows[0]["directory"],
                    "status": "completed",
                    "gate": "auto",
                    "operator_review_required": False,
                    "operator_review_flags": [],
                    "proposed_change_count": 0,
                    "changed_file_count_estimate": 0,
                    "changed_bytes_estimate": 0,
                    "changed_bytes_estimate_complete": True,
                    "changed_field_counts": {},
                    "conflicts": [],
                    "checkpoint_status": "new",
                }
            ],
            {RELEASE_ID: {"source": "disk_cache", "retries": 0, "release": None}},
        )

    monkeypatch.setattr(batch_dry_run, "_run_batch", fake_run_batch)
    output_dir = tmp_path / "reports"

    batch_dry_run._batch_dry_run(
        [
            "--inventory",
            str(inventory_path),
            "--directory",
            row["directory"],
            "--out-dir",
            str(output_dir),
            "--keep-existing-credits",
        ]
    )

    report = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    markdown = (output_dir / "summary.md").read_text(encoding="utf-8")
    assert report["counts"]["selected"] == 1
    assert report["counts"]["release_cache_hits"] == 1
    assert len(report["release_ids_selected"]) == 1
    assert report["inventory_sha256"] == hashlib.sha256(inventory_path.read_bytes()).hexdigest()
    assert "Gate auto does not approve a refresh" in markdown
    assert str(output_dir) not in str(root / "Album")
    assert album.directory.is_dir()
    assert "Complete: 1 completed, 0 errors" in capsys.readouterr().out
    assert received_policies == [True]
