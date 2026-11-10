"""The `lyrics-batch` command: fetch certain matches, save pending options, embed, stop rules."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from mutagen.flac import FLAC

from music_tagger.cli import lyrics_cmd
from music_tagger.cli.lyrics_cmd import _lyrics_batch
from music_tagger.lrclib import LrclibClient
from music_tagger.lyrics_batch import (
    AUTO_LENGTH_SECS,
    Candidate,
    Decision,
    PendingEntry,
    confident_choice,
    load_decisions,
    load_pending,
    render_pending,
    save_decisions,
    save_pending,
)
from music_tagger.tags import TagChange, read_album, write_tags
from tests.conftest import FIXTURES
from tests.lyrics_fakes import catalog, record, responder, use_client


def _add_album(root: Path, name: str) -> Path:
    album = root / "Eagles" / name
    shutil.copytree(FIXTURES / "album_flac", album)
    return album


def _sidecars(album: Path, suffix: str) -> list[str]:
    return sorted(p.name for p in album.glob(f"*{suffix}"))


def _run(argv: list[str]) -> int:
    try:
        _lyrics_batch(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def _recording(
    inner: Callable[[httpx.Request], httpx.Response], seen: list[httpx.Request]
) -> Callable[[httpx.Request], httpx.Response]:
    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return inner(request)

    return respond


def _searched(seen: list[httpx.Request]) -> list[str | None]:
    return [r.url.params.get("track_name") for r in seen if r.url.path == "/api/search"]


def _tag_release_track(album: Path, file: str, release_track_id: str) -> None:
    track = next(t for t in read_album(album).tracks if t.path.name == file)
    write_tags(track, [TagChange("musicbrainz_releasetrackid", "", release_track_id)])


def _write_decisions(path: Path, entries: list[dict[str, object]]) -> None:
    path.write_text(json.dumps({"version": 1, "entries": entries}), encoding="utf-8")


def _decision_json(
    album: Path, file: str, release_track_id: str | None = None, track: int = 1
) -> dict[str, object]:
    entry: dict[str, object] = {
        "album": str(album),
        "file": file,
        "track": track,
        "choice": None,
        "reason": "no lyric exists",
        "decided": STAMP,
    }
    if release_track_id is not None:
        entry["release_track_id"] = release_track_id
    return entry


class TestFetch:
    def test_certain_matches_are_written_and_nothing_is_pending(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "library"
        album = _add_album(root, "One")
        use_client(monkeypatch, responder(catalog(album)))
        pending = tmp_path / "pending.json"

        code = _run([str(root), "--pending", str(pending), "--no-log"])

        assert code == 0
        assert len(_sidecars(album, ".lrc")) == 3
        assert load_pending(pending) == []

    def test_ambiguous_track_is_saved_with_every_candidate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "library"
        album = _add_album(root, "One")
        records = catalog(album)
        album_name = read_album(album).tracks[0].tags["album"]
        records["Desperado"].append(record(900, "Desperado", album_name))
        use_client(monkeypatch, responder(records))
        pending = tmp_path / "pending.json"

        code = _run([str(root), "--pending", str(pending), "--no-log"])

        assert code == 3
        assert "01 - Track 1.lrc" not in _sidecars(album, ".lrc")
        assert len(_sidecars(album, ".lrc")) == 2
        [entry] = load_pending(pending)
        assert entry.album == str(album)
        assert entry.file == "01 - Track 1.flac"
        assert entry.track == 1
        assert entry.outcome == "ambiguous"
        assert sorted(c.id for c in entry.candidates) == [101, 900]
        assert all(c.album_matches and c.plausible for c in entry.candidates)
        assert all(c.offers == ("synced", "plain") for c in entry.candidates)

    def test_no_match_is_final_and_not_pending(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "library"
        album = _add_album(root, "One")
        use_client(monkeypatch, responder({}))
        pending = tmp_path / "pending.json"

        code = _run([str(root), "--pending", str(pending), "--no-log"])

        assert code == 0
        assert _sidecars(album, ".lrc") == []
        assert load_pending(pending) == []

    def test_track_with_lyrics_is_never_searched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "library"
        album = _add_album(root, "One")
        for path in album.glob("*.flac"):
            path.with_suffix(".lrc").write_text("[00:01.00]Already here\n", encoding="utf-8")
        seen: list[httpx.Request] = []
        use_client(monkeypatch, _recording(responder({}), seen))

        code = _run([str(root), "--pending", str(tmp_path / "p.json"), "--no-log"])

        assert code == 0
        assert seen == []

    def test_decided_track_is_never_searched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "library"
        album = _add_album(root, "One")
        decisions = tmp_path / "decisions.json"
        save_decisions(
            decisions,
            {
                (str(album), "01 - Track 1.flac"): Decision(
                    album=str(album),
                    file="01 - Track 1.flac",
                    track=1,
                    choice=None,
                    reason="no lyric exists",
                    decided="2026-11-03 12:00 UTC",
                )
            },
        )
        seen: list[httpx.Request] = []
        use_client(monkeypatch, _recording(responder(catalog(album)), seen))

        code = _run(
            [
                str(root),
                "--pending",
                str(tmp_path / "p.json"),
                "--decisions",
                str(decisions),
                "--no-log",
            ]
        )

        assert code == 0
        assert "Desperado" not in _searched(seen)
        assert "01 - Track 1.lrc" not in _sidecars(album, ".lrc")

    def test_no_match_with_an_album_match_is_saved_for_a_decision(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "library"
        album = _add_album(root, "One")
        album_name = read_album(album).tracks[0].tags["album"]
        # Same album, but 4.5 s longer than the audio: no plausible record. Its last
        # synced line falls 1.5 s after the 0.5 s audio ends.
        longer = record(
            700,
            "Desperado",
            album_name,
            duration=5.0,
            syncedLyrics="[00:01.00]Timed line\n[00:02.00]Last line",
        )
        use_client(monkeypatch, responder({"Desperado": [longer]}))
        pending = tmp_path / "pending.json"

        code = _run([str(root), "--pending", str(pending), "--no-log"])

        assert code == 3
        [entry] = [e for e in load_pending(pending) if e.file == "01 - Track 1.flac"]
        assert entry.outcome == "no_match"
        [candidate] = entry.candidates
        assert candidate.id == 700
        assert candidate.album_matches
        assert not candidate.plausible
        assert candidate.last_line_delta == 1.5

    def test_malformed_sidecar_is_saved_for_inspection_and_not_searched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "library"
        album = _add_album(root, "One")
        # Timed text in a .txt sidecar is malformed, so the batch must not search or overwrite.
        (album / "01 - Track 1.txt").write_text("[00:01.00]Timed\n", encoding="utf-8")
        seen: list[httpx.Request] = []
        use_client(monkeypatch, _recording(responder(catalog(album)), seen))
        pending = tmp_path / "pending.json"

        code = _run([str(root), "--pending", str(pending), "--no-log"])

        assert code == 3
        assert "Desperado" not in _searched(seen)
        assert "01 - Track 1.lrc" not in _sidecars(album, ".lrc")
        [entry] = load_pending(pending)
        assert entry.outcome == "needs_inspection"
        assert entry.candidates == ()

    def test_every_album_under_the_root_is_processed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "library"
        first = _add_album(root, "One")
        second = _add_album(root, "Two")
        records = catalog(first)
        records.update(catalog(second))
        use_client(monkeypatch, responder(records))

        code = _run([str(root), "--pending", str(tmp_path / "p.json"), "--no-log"])

        assert code == 0
        assert len(_sidecars(first, ".lrc")) == 3
        assert len(_sidecars(second, ".lrc")) == 3

    def test_provider_failure_is_reported_and_other_tracks_still_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "library"
        album = _add_album(root, "One")
        inner = responder(catalog(album))

        def flaky(request: httpx.Request) -> httpx.Response:
            if request.url.params.get("track_name") == "Twenty-One":
                return httpx.Response(503, json={})
            return inner(request)

        use_client(monkeypatch, flaky)
        pending = tmp_path / "pending.json"

        code = _run([str(root), "--pending", str(pending), "--no-log"])

        assert code == 1
        assert _sidecars(album, ".lrc") == ["01 - Track 1.lrc", "03 - Track 3.lrc"]
        assert load_pending(pending) == []

    def test_rate_limit_stops_the_run_and_keeps_other_albums_pending(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "library"
        first = _add_album(root, "One")
        second = _add_album(root, "Two")
        pending = tmp_path / "pending.json"
        saved = PendingEntry(
            album=str(second),
            file="01 - Track 1.flac",
            track=1,
            disc=1,
            outcome="review",
            reason="from an earlier run",
        )
        save_pending(pending, [saved], "2026-11-02 12:00 UTC")

        def limited(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"Retry-After": "120"}, json={})

        use_client(monkeypatch, limited)

        code = _run([str(root), "--pending", str(pending), "--no-log"])

        assert code == 1
        assert "Stopped on LRCLIB rate limit" in capsys.readouterr().err
        assert _sidecars(first, ".lrc") == []
        assert _sidecars(second, ".lrc") == []
        assert load_pending(pending) == [saved]


class TestReleaseTrackIdKeys:
    def test_renamed_file_with_its_release_track_id_is_not_searched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "library"
        album = _add_album(root, "One")
        _tag_release_track(album, "01 - Track 1.flac", "rt-1")
        decisions = tmp_path / "decisions.json"
        _write_decisions(decisions, [_decision_json(album, "01 - Track 1.flac", "rt-1")])
        (album / "01 - Track 1.flac").rename(album / "01 - Track 1 (Remaster).flac")
        seen: list[httpx.Request] = []
        use_client(monkeypatch, _recording(responder(catalog(album)), seen))

        code = _run(
            [
                str(root),
                "--pending",
                str(tmp_path / "p.json"),
                "--decisions",
                str(decisions),
                "--no-log",
            ]
        )

        assert code == 0
        assert "Desperado" not in _searched(seen)
        stored = json.loads(decisions.read_text(encoding="utf-8"))["entries"]
        assert [e["file"] for e in stored] == ["01 - Track 1 (Remaster).flac"]

    def test_moved_album_with_release_track_ids_is_not_searched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "library"
        album = _add_album(root, "One")
        files = ["01 - Track 1.flac", "02 - Track 2.flac", "03 - Track 3.flac"]
        for number, file in enumerate(files, start=1):
            _tag_release_track(album, file, f"rt-{number}")
        decisions = tmp_path / "decisions.json"
        _write_decisions(
            decisions,
            [_decision_json(album, f, f"rt-{n}", n) for n, f in enumerate(files, start=1)],
        )
        moved = root / "Eagles" / "Two"
        shutil.move(album, moved)
        seen: list[httpx.Request] = []
        use_client(monkeypatch, _recording(responder(catalog(moved)), seen))

        code = _run(
            [
                str(root),
                "--pending",
                str(tmp_path / "p.json"),
                "--decisions",
                str(decisions),
                "--no-log",
            ]
        )

        assert code == 0
        assert seen == []
        stored = json.loads(decisions.read_text(encoding="utf-8"))["entries"]
        assert {e["album"] for e in stored} == {str(moved)}

    def test_decision_without_id_still_matches_by_album_and_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "library"
        album = _add_album(root, "One")
        decisions = tmp_path / "decisions.json"
        _write_decisions(decisions, [_decision_json(album, "01 - Track 1.flac")])
        seen: list[httpx.Request] = []
        use_client(monkeypatch, _recording(responder(catalog(album)), seen))

        _run(
            [
                str(root),
                "--pending",
                str(tmp_path / "p.json"),
                "--decisions",
                str(decisions),
                "--no-log",
            ]
        )

        assert "Desperado" not in _searched(seen)

    def test_same_file_name_in_two_albums_with_different_ids_do_not_cross(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "library"
        one = _add_album(root, "One")
        two = _add_album(root, "Two")
        _tag_release_track(one, "01 - Track 1.flac", "rt-one")
        _tag_release_track(two, "01 - Track 1.flac", "rt-two")
        decisions = tmp_path / "decisions.json"
        _write_decisions(decisions, [_decision_json(one, "01 - Track 1.flac", "rt-one")])
        seen: list[httpx.Request] = []
        use_client(monkeypatch, _recording(responder(catalog(two)), seen))

        _run(
            [
                str(root),
                "--pending",
                str(tmp_path / "p.json"),
                "--decisions",
                str(decisions),
                "--no-log",
            ]
        )

        assert _searched(seen).count("Desperado") == 1


class TestReleaseTrackIdRecorded:
    def test_pending_entry_records_the_release_track_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "library"
        album = _add_album(root, "One")
        _tag_release_track(album, "01 - Track 1.flac", "rt-1")
        (album / "01 - Track 1.txt").write_text("[00:01.00]Timed\n", encoding="utf-8")
        use_client(monkeypatch, responder(catalog(album)))
        pending = tmp_path / "pending.json"

        _run([str(root), "--pending", str(pending), "--no-log"])

        [entry] = [e for e in load_pending(pending) if e.file == "01 - Track 1.flac"]
        assert entry.release_track_id == "rt-1"

    def test_auto_chosen_decision_records_the_release_track_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "library"
        album = _add_album(root, "One")
        _tag_release_track(album, "01 - Track 1.flac", "rt-1")
        records = catalog(album)
        album_name = read_album(album).tracks[0].tags["album"]
        records["Desperado"].append(record(900, "Desperado", album_name, duration=0.9))
        use_client(monkeypatch, responder(records))
        decisions = tmp_path / "decisions.json"

        _run(
            [
                str(root),
                "--auto",
                "--pending",
                str(tmp_path / "p.json"),
                "--decisions",
                str(decisions),
                "--no-log",
            ]
        )

        stored = json.loads(decisions.read_text(encoding="utf-8"))["entries"]
        assert [e["release_track_id"] for e in stored if e["file"].startswith("01")] == ["rt-1"]


class TestEmbed:
    def test_embed_writes_tags_from_the_sidecars_it_just_fetched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "library"
        album = _add_album(root, "One")
        use_client(monkeypatch, responder(catalog(album)))

        code = _run([str(root), "--embed", "--pending", str(tmp_path / "p.json"), "--no-log"])

        assert code == 0
        for path in sorted(album.glob("*.flac")):
            assert FLAC(path).get("lyrics"), path.name


class TestDryRun:
    def test_writes_no_sidecars_tags_log_or_pending_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "library"
        album = _add_album(root, "One")
        use_client(monkeypatch, responder(catalog(album)))
        log = tmp_path / "changes.log"
        pending = tmp_path / "pending.json"

        code = _run(
            [str(root), "--embed", "--dry-run", "--pending", str(pending), "--log", str(log)]
        )

        assert code == 0
        assert _sidecars(album, ".lrc") == []
        assert not log.exists()
        assert not pending.exists()
        assert all(not FLAC(p).get("lyrics") for p in album.glob("*.flac"))


class TestState:
    def test_pending_path_defaults_to_the_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "library"
        _add_album(root, "One")
        env_pending = tmp_path / "from-env.json"
        monkeypatch.setenv("MUSIC_TAGGER_LYRICS_PENDING", str(env_pending))
        use_client(monkeypatch, responder({}))

        _run([str(root), "--no-log"])

        assert env_pending.exists()

    def test_render_lists_each_track_with_its_candidates(self) -> None:
        entry = PendingEntry(
            album="/music/Eagles/One",
            file="01 - Track 1.flac",
            track=1,
            disc=1,
            outcome="review",
            reason="search was capped",
            candidates=(),
        )

        text = render_pending([entry])

        assert "## /music/Eagles/One" in text
        assert "### track 1 (01 - Track 1.flac): review" in text
        assert "(no candidates saved)" in text

    def test_render_states_where_the_last_line_falls(self) -> None:
        past = Candidate(
            id=1,
            title="Song",
            artist="Artist",
            album="Album",
            duration_delta=5.0,
            plausible=False,
            album_matches=True,
            offers=("synced",),
            notes=(),
            last_line_delta=10.0,
        )
        before = Candidate(
            id=2,
            title="Song",
            artist="Artist",
            album="Album",
            duration_delta=5.0,
            plausible=False,
            album_matches=True,
            offers=("synced",),
            notes=(),
            last_line_delta=-3.0,
        )
        entry = PendingEntry(
            album="/music/A",
            file="01 - Song.flac",
            track=1,
            disc=1,
            outcome="no_match",
            reason="no record",
            candidates=(past, before),
        )

        text = render_pending([entry])

        assert "last line 10.0s past the audio end" in text
        assert "last line 3.0s before the audio end" in text

    def test_render_says_so_when_nothing_is_pending(self) -> None:
        assert render_pending([]) == "No undecided lyric tracks.\n"


def test_client_constructed_once_per_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "library"
    _add_album(root, "One")
    _add_album(root, "Two")
    built: list[LrclibClient] = []

    def factory() -> LrclibClient:
        client = LrclibClient(
            transport=httpx.MockTransport(responder({})), sleep=lambda _s: None, min_interval=0.0
        )
        built.append(client)
        return client

    monkeypatch.setattr(lyrics_cmd, "LrclibClient", factory)

    _run([str(root), "--pending", str(tmp_path / "p.json"), "--no-log"])

    assert len(built) == 1


def _cand(
    candidate_id: int,
    *,
    plausible: bool = False,
    album: bool = True,
    offers: tuple[str, ...] = ("synced", "plain"),
    delta: float | None = 0.0,
    last: float | None = None,
    identity: bool = True,
) -> Candidate:
    return Candidate(
        id=candidate_id,
        title="Song",
        artist="Artist",
        album="Album",
        duration_delta=delta,
        plausible=plausible,
        album_matches=album,
        offers=offers,
        notes=(),
        last_line_delta=last,
        identity_matches=identity,
    )


def _pending_of(*candidates: Candidate) -> PendingEntry:
    return PendingEntry(
        album="/music/A",
        file="01 - Song.flac",
        track=1,
        disc=1,
        outcome="ambiguous",
        reason="reason",
        candidates=candidates,
    )


class TestConfidentChoice:
    def test_plausible_album_match_with_synced_text_is_taken_closest_first(self) -> None:
        far = _cand(1, plausible=True, delta=1.5)
        near = _cand(2, plausible=True, delta=-0.2)
        assert confident_choice(_pending_of(far, near)) == near

    def test_plain_text_is_never_taken(self) -> None:
        plain = _cand(1, plausible=True, offers=("plain",))
        assert confident_choice(_pending_of(plain)) is None

    def test_album_match_within_the_limit_with_last_line_inside_is_taken(self) -> None:
        fit = _cand(3, delta=4.0, last=-2.0)
        assert confident_choice(_pending_of(fit)) == fit

    def test_last_line_past_the_audio_end_is_not_taken(self) -> None:
        assert confident_choice(_pending_of(_cand(4, delta=2.0, last=1.0))) is None

    def test_length_beyond_the_limit_is_not_taken(self) -> None:
        far = _cand(5, delta=AUTO_LENGTH_SECS + 1, last=-1.0)
        assert confident_choice(_pending_of(far)) is None

    def test_album_differs_is_not_taken(self) -> None:
        assert confident_choice(_pending_of(_cand(6, plausible=True, album=False))) is None

    def test_fallback_is_not_taken_when_the_title_differs(self) -> None:
        other_title = replace(_cand(7, delta=1.0, last=-5.0, identity=False), title="Other Song")
        assert confident_choice(_pending_of(other_title)) is None

    def test_fallback_is_not_taken_when_the_artist_differs(self) -> None:
        other_artist = replace(
            _cand(8, delta=1.0, last=-5.0, identity=False), artist="Other Artist"
        )
        assert confident_choice(_pending_of(other_artist)) is None

    def test_fallback_is_taken_when_artist_and_title_match(self) -> None:
        matching = _cand(9, delta=1.0, last=-5.0)
        assert confident_choice(_pending_of(matching)) == matching


class TestIdentityField:
    def test_identity_is_written_to_the_pending_file(self, tmp_path: Path) -> None:
        pending = tmp_path / "pending.json"
        candidate = replace(_cand(2, plausible=True), identity_matches=True)

        save_pending(pending, [_pending_of(candidate)], "2026-11-10T00:00:00Z")

        [entry] = load_pending(pending)
        assert entry.candidates[0].identity_matches is True

    def test_pending_entry_without_identity_key_loads_as_false(self, tmp_path: Path) -> None:
        pending = tmp_path / "pending.json"
        save_pending(pending, [_pending_of(_cand(1, plausible=True))], "2026-11-10T00:00:00Z")
        data = json.loads(pending.read_text(encoding="utf-8"))
        for candidate in data["entries"][0]["candidates"]:
            candidate.pop("identity_matches", None)
        pending.write_text(json.dumps(data), encoding="utf-8")

        [entry] = load_pending(pending)
        assert entry.candidates[0].identity_matches is False


class TestAutoChoice:
    def test_takes_the_closest_plausible_match_and_clears_the_pending_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "library"
        album = _add_album(root, "One")
        records = catalog(album)
        album_name = read_album(album).tracks[0].tags["album"]
        records["Desperado"].append(record(900, "Desperado", album_name, duration=0.9))
        use_client(monkeypatch, responder(records))
        pending = tmp_path / "pending.json"
        decisions = tmp_path / "decisions.json"

        code = _run(
            [
                str(root),
                "--auto",
                "--pending",
                str(pending),
                "--decisions",
                str(decisions),
                "--no-log",
            ]
        )

        assert code == 0
        assert "01 - Track 1.lrc" in _sidecars(album, ".lrc")
        assert [
            d.choice for d in load_decisions(decisions).values() if d.file.startswith("01")
        ] == [101]
        assert [e.file for e in load_pending(pending)] == []

    def test_leaves_a_plain_text_only_ambiguity_pending(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "library"
        album = _add_album(root, "One")
        records = catalog(album, syncedLyrics="")
        album_name = read_album(album).tracks[0].tags["album"]
        records["Desperado"].append(record(900, "Desperado", album_name, syncedLyrics=""))
        use_client(monkeypatch, responder(records))
        pending = tmp_path / "pending.json"

        code = _run(
            [
                str(root),
                "--auto",
                "--pending",
                str(pending),
                "--decisions",
                str(tmp_path / "d.json"),
                "--no-log",
            ]
        )

        # Plain-only matches are reviewed, so no track is written and all three stay pending.
        assert code == 3
        assert _sidecars(album, ".txt") == []
        assert sorted(e.file for e in load_pending(pending)) == [
            "01 - Track 1.flac",
            "02 - Track 2.flac",
            "03 - Track 3.flac",
        ]

    def test_dry_run_records_and_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "library"
        album = _add_album(root, "One")
        records = catalog(album)
        album_name = read_album(album).tracks[0].tags["album"]
        records["Desperado"].append(record(900, "Desperado", album_name, duration=0.9))
        use_client(monkeypatch, responder(records))
        decisions = tmp_path / "decisions.json"

        code = _run(
            [
                str(root),
                "--auto",
                "--dry-run",
                "--pending",
                str(tmp_path / "p.json"),
                "--decisions",
                str(decisions),
                "--no-log",
            ]
        )

        assert code == 0
        assert "01 - Track 1.lrc" not in _sidecars(album, ".lrc")
        assert not decisions.exists()

    def test_embed_after_auto_tags_the_chosen_sidecar(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "library"
        album = _add_album(root, "One")
        records = catalog(album)
        album_name = read_album(album).tracks[0].tags["album"]
        records["Desperado"].append(record(900, "Desperado", album_name, duration=0.9))
        use_client(monkeypatch, responder(records))

        code = _run(
            [
                str(root),
                "--auto",
                "--embed",
                "--pending",
                str(tmp_path / "p.json"),
                "--decisions",
                str(tmp_path / "d.json"),
                "--no-log",
            ]
        )

        assert code == 0
        assert FLAC(album / "01 - Track 1.flac").get("lyrics")


STAMP = "2026-11-10T00:00:00Z"


class TestStateFileValidation:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("plausible", "false"),
            ("album_matches", 1),
            ("duration_delta", "NaN"),
            ("duration_delta", float("nan")),
            ("last_line_delta", "1.5"),
            ("identity_matches", "yes"),
            ("id", "12"),
            ("offers", "synced"),
        ],
    )
    def test_bad_candidate_field_raises_naming_it(
        self, tmp_path: Path, field: str, value: object
    ) -> None:
        pending = tmp_path / "pending.json"
        save_pending(pending, [_pending_of(_cand(1, plausible=True))], STAMP)
        data = json.loads(pending.read_text(encoding="utf-8"))
        data["entries"][0]["candidates"][0][field] = value
        pending.write_text(json.dumps(data), encoding="utf-8")

        with pytest.raises(ValueError, match=field):
            load_pending(pending)

    def test_file_without_entries_raises_value_error(self, tmp_path: Path) -> None:
        pending = tmp_path / "pending.json"
        pending.write_text(json.dumps({"version": 1, "generated": STAMP}), encoding="utf-8")

        with pytest.raises(ValueError, match="entries"):
            load_pending(pending)

    def test_decisions_without_entries_raises_value_error(self, tmp_path: Path) -> None:
        decisions = tmp_path / "decisions.json"
        decisions.write_text(json.dumps({"version": 1}), encoding="utf-8")

        with pytest.raises(ValueError, match="entries"):
            load_decisions(decisions)

    def test_release_track_id_must_be_a_string(self, tmp_path: Path) -> None:
        decisions = tmp_path / "decisions.json"
        _write_decisions(decisions, [_decision_json(tmp_path, "01.flac", 5)])  # type: ignore[arg-type]

        with pytest.raises(ValueError, match="release_track_id"):
            load_decisions(decisions)

    def test_decision_with_string_choice_raises_naming_it(self, tmp_path: Path) -> None:
        decisions = tmp_path / "decisions.json"
        decisions.write_text(
            json.dumps(
                {
                    "version": 1,
                    "entries": [
                        {
                            "album": "/music/A",
                            "file": "01.flac",
                            "track": 1,
                            "choice": "12",
                            "reason": "r",
                            "decided": STAMP,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="choice"):
            load_decisions(decisions)

    def test_save_ignores_a_stale_fixed_temp_name(self, tmp_path: Path) -> None:
        pending = tmp_path / "pending.json"
        stale = tmp_path / "pending.json.tmp"
        stale.write_text("stale", encoding="utf-8")

        save_pending(pending, [], STAMP)

        assert stale.read_text(encoding="utf-8") == "stale"
        assert load_pending(pending) == []

    def test_save_fsyncs_once_and_leaves_no_temp_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[int] = []
        real_fsync = os.fsync

        def recording_fsync(fd: int) -> None:
            calls.append(fd)
            real_fsync(fd)

        monkeypatch.setattr(os, "fsync", recording_fsync)

        save_pending(tmp_path / "pending.json", [], STAMP)

        assert len(calls) == 1
        assert sorted(p.name for p in tmp_path.iterdir()) == ["pending.json"]
