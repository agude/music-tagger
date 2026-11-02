from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
from mutagen.flac import FLAC
from mutagen.id3 import TXXX
from mutagen.mp3 import MP3

from music_tagger import cli as cli_module
from music_tagger.cli import (
    EXIT_BLOCKED,
    EXIT_REVIEW,
    _diff,
    _is_album_dir,
    _lock,
    _match,
    _mb_discid,
    _mb_release,
    _mb_search,
    _nd,
    _print_diff,
    _read,
    _rename,
    _scan,
    _toc,
    _unlock,
    _write_ratings,
    _write_stats,
    _write_tags,
)
from music_tagger.musicbrainz import MBRelease, MBTrack
from music_tagger.stats import SongStats
from music_tagger.tagger import AlbumResult, TagConflict, TrackDiff
from music_tagger.tags import TagChange, read_album, read_flac_comments, write_locks, write_tags


class TestIsAlbumDir:
    def test_flac_dir(self, flac_album: Path) -> None:
        assert _is_album_dir(flac_album) is True

    def test_mp3_dir(self, mp3_album: Path) -> None:
        assert _is_album_dir(mp3_album) is True

    def test_empty_dir(self, tmp_path: Path) -> None:
        assert _is_album_dir(tmp_path) is False

    def test_text_only_dir(self, tmp_path: Path) -> None:
        (tmp_path / "notes.txt").write_text("hello")
        assert _is_album_dir(tmp_path) is False


class TestPrintDiff:
    def test_reports_conflict_when_there_are_no_tag_changes(
        self, capsys: object, flac_album: Path
    ) -> None:
        album = read_album(flac_album)
        result = AlbumResult(
            album,
            MBRelease(id="r-1", title="Desperado"),
            [
                TrackDiff(
                    album.tracks[0], [], [TagConflict("isrc", "USIR19902220", ["USDW10110279"])]
                )
            ],
        )

        _print_diff(result)

        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "No tag changes needed" not in captured.out
        assert str(album.tracks[0].path) in captured.err
        assert "USIR19902220" in captured.err
        assert "USDW10110279" in captured.err
        assert "unresolved" in captured.err

    def test_reports_unverified_isrc(self, capsys: object, flac_album: Path) -> None:
        album = read_album(flac_album)
        result = AlbumResult(
            album,
            MBRelease(id="r-1", title="Desperado"),
            [TrackDiff(album.tracks[0], [], warnings=["ISRC USIR19902220 is unverified."])],
        )

        _print_diff(result)

        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "No tag changes needed" not in captured.out
        assert "USIR19902220 is unverified" in captured.err

    def test_no_changes(self, capsys: object, flac_album: Path) -> None:
        album = read_album(flac_album)
        result = AlbumResult(
            album=album,
            release=MBRelease(
                id="r-1", title="Desperado", date="1973", country="US", label="Asylum"
            ),
            diffs=[TrackDiff(track=t, changes=[]) for t in album.tracks],
        )
        _print_diff(result)
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "No tag changes needed" in captured.out

    def test_with_changes(self, capsys: object, flac_album: Path) -> None:
        album = read_album(flac_album)
        diffs = [
            TrackDiff(
                track=album.tracks[0],
                changes=[TagChange(field="title", old_value="Desperado", new_value="New")],
            ),
            TrackDiff(track=album.tracks[1], changes=[]),
            TrackDiff(track=album.tracks[2], changes=[]),
        ]
        result = AlbumResult(
            album=album,
            release=MBRelease(
                id="r-1", title="Desperado", date="1973", country="US", label="Asylum"
            ),
            diffs=diffs,
        )
        _print_diff(result)
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "title: Desperado" in captured.out
        assert "→ New" in captured.out


class TestReadCommand:
    def test_json_to_stdout(self, capsys: object, flac_album: Path) -> None:
        _read([str(flac_album)])
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        data = json.loads(captured.out)
        assert data["artist"] == "Eagles"
        assert data["album"] == "Desperado"
        assert data["track_count"] == 3
        assert len(data["tracks"]) == 3
        assert data["tracks"][0]["format"] == "flac"
        assert data["tracks"][0]["duration_secs"] > 0

    def test_json_to_file_with_digest(
        self, capsys: object, flac_album: Path, tmp_path: Path
    ) -> None:
        out = tmp_path / "evidence.json"
        _read([str(flac_album), "-o", str(out)])
        data = json.loads(out.read_text())
        assert data["artist"] == "Eagles"
        assert data["track_count"] == 3
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "Eagles" in captured.out
        assert "Desperado" in captured.out
        assert "3 tracks" in captured.out

    def test_mp3_album(self, capsys: object, mp3_album: Path) -> None:
        _read([str(mp3_album)])
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        data = json.loads(captured.out)
        assert data["tracks"][0]["format"] == "mp3"

    def test_empty_dir_exits(self, tmp_path: Path) -> None:
        import pytest

        with pytest.raises(SystemExit):
            _read([str(tmp_path)])


class TestScanCommand:
    def test_outputs_json(self, flac_album: Path, tmp_path: Path) -> None:
        out = tmp_path / "scan.json"
        _scan([str(flac_album.parent), "--all", "-o", str(out)])
        data = json.loads(out.read_text())
        assert len(data) == 1
        assert data[0]["artist"] == "Eagles"
        assert data[0]["album"] == "Desperado"
        assert data[0]["track_count"] == 3
        assert data[0]["status"] == "ok"
        assert data[0]["done"] is False
        assert data[0]["musicbrainz_albumid"] == "test-album-id-1"

    def test_skips_consistent_without_all(
        self, flac_album: Path, tmp_path: Path, capsys: object
    ) -> None:
        out = tmp_path / "scan.json"
        _scan([str(flac_album.parent), "-o", str(out)])
        assert not out.exists()
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "All albums have consistent" in captured.out

    def test_no_id_status(self, tmp_path: Path) -> None:
        album_dir = tmp_path / "noalbum"
        album_dir.mkdir()
        import shutil

        from tests.conftest import FIXTURES

        for f in sorted((FIXTURES / "album_flac").iterdir()):
            if f.suffix == ".flac":
                shutil.copy2(f, album_dir / f.name)
        # Strip the MB album ID from the copies
        from mutagen.flac import FLAC

        for f in album_dir.glob("*.flac"):
            audio = FLAC(f)
            if "MUSICBRAINZ_ALBUMID" in audio:
                del audio["MUSICBRAINZ_ALBUMID"]
                audio.save()
        out = tmp_path / "scan.json"
        _scan([str(tmp_path), "-o", str(out)])
        data = json.loads(out.read_text())
        no_id = [e for e in data if e["status"] == "no_id"]
        assert len(no_id) == 1
        assert "musicbrainz_albumid" not in no_id[0]

    def test_split_status(self, tmp_path: Path) -> None:
        album_dir = tmp_path / "splitalbum"
        album_dir.mkdir()
        import shutil

        from tests.conftest import FIXTURES

        for f in sorted((FIXTURES / "album_flac").iterdir()):
            if f.suffix == ".flac":
                shutil.copy2(f, album_dir / f.name)
        from mutagen.flac import FLAC

        files = sorted(album_dir.glob("*.flac"))
        audio = FLAC(files[0])
        audio["MUSICBRAINZ_ALBUMID"] = ["different-id"]
        audio.save()
        out = tmp_path / "scan.json"
        _scan([str(tmp_path), "-o", str(out)])
        data = json.loads(out.read_text())
        split = [e for e in data if e["status"] == "split"]
        assert len(split) == 1
        assert isinstance(split[0]["musicbrainz_albumid"], list)
        assert len(split[0]["musicbrainz_albumid"]) == 2

    def test_stdout_when_no_output(self, flac_album: Path, capsys: object) -> None:
        _scan([str(flac_album.parent), "--all"])
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        data = json.loads(captured.out)
        assert len(data) == 1
        assert data[0]["artist"] == "Eagles"


SAMPLE_CUE = """\
FILE "audio.flac" WAVE
  TRACK 01 AUDIO
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    INDEX 01 03:29:37
  TRACK 03 AUDIO
    INDEX 01 06:36:50
"""


class TestTocCommand:
    def test_json_to_stdout(self, capsys: object, tmp_path: Path) -> None:
        (tmp_path / "album.cue").write_text(SAMPLE_CUE)
        _toc([str(tmp_path)])
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        data = json.loads(captured.out)
        assert data["first_track"] == 1
        assert data["last_track"] == 3
        assert len(data["track_offsets"]) == 3

    def test_no_cue_exits(self, tmp_path: Path) -> None:
        import pytest

        with pytest.raises((SystemExit, ValueError)):
            _toc([str(tmp_path)])


class TestMbDiscidCommand:
    def test_by_discid(self, capsys: object) -> None:
        release = MBRelease(
            id="r-1",
            title="Desperado",
            date="1973",
            country="US",
            label="Asylum",
            track_count=11,
            format="CD",
        )
        with patch("music_tagger.cli.MusicBrainzClient") as mock_cls:
            mock_cls.return_value.lookup_discid.return_value = [release]
            _mb_discid(["abc123"])
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        data = json.loads(captured.out)
        assert len(data) == 1
        assert data[0]["id"] == "r-1"

    def test_by_toc(self, capsys: object) -> None:
        release = MBRelease(
            id="r-2",
            title="Album",
            track_count=5,
            format="CD",
        )
        with patch("music_tagger.cli.MusicBrainzClient") as mock_cls:
            mock_cls.return_value.lookup_toc.return_value = [release]
            _mb_discid(["--toc", "1 3 200000 150 15000 30000"])
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        data = json.loads(captured.out)
        assert len(data) == 1
        assert data[0]["id"] == "r-2"


class TestMbSearchCommand:
    def test_json_to_stdout(self, capsys: object) -> None:
        release = MBRelease(
            id="r-1",
            title="Desperado",
            date="1973",
            country="US",
            label="Asylum",
            track_count=11,
            format="CD",
        )
        with patch("music_tagger.cli.MusicBrainzClient") as mock_cls:
            mock_cls.return_value.search_releases.return_value = [release]
            _mb_search(["--artist", "Eagles", "--album", "Desperado"])
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        data = json.loads(captured.out)
        assert len(data) == 1
        assert data[0]["id"] == "r-1"
        assert data[0]["title"] == "Desperado"

    def test_json_to_file_with_digest(self, capsys: object, tmp_path: Path) -> None:
        release = MBRelease(
            id="r-1",
            title="Desperado",
            date="1973",
            country="US",
            label="Asylum",
            track_count=11,
            format="CD",
        )
        out = tmp_path / "candidates.json"
        with patch("music_tagger.cli.MusicBrainzClient") as mock_cls:
            mock_cls.return_value.search_releases.return_value = [release]
            _mb_search(["--artist", "Eagles", "--album", "Desperado", "-o", str(out)])
        data = json.loads(out.read_text())
        assert len(data) == 1
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "1 candidates" in captured.out
        assert "r-1" in captured.out


class TestMbReleaseCommand:
    def test_json_to_stdout(self, capsys: object) -> None:
        release = MBRelease(
            id="r-1",
            title="Desperado",
            date="1973",
            country="US",
            label="Asylum",
            track_count=11,
            format="CD",
            discs={1: [MBTrack(number=1, title="Track 1", duration_ms=200000)]},
            artist_id="art-1",
            release_group_id="rg-1",
        )
        with patch("music_tagger.cli.MusicBrainzClient") as mock_cls:
            mock_cls.return_value.fetch_release.return_value = release
            _mb_release(["r-1"])
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        data = json.loads(captured.out)
        assert data["id"] == "r-1"
        assert data["title"] == "Desperado"
        assert "1" in data["discs"]
        assert len(data["discs"]["1"]) == 1

    def test_json_to_file_with_digest(self, capsys: object, tmp_path: Path) -> None:
        release = MBRelease(
            id="r-1",
            title="Desperado",
            date="1973",
            country="US",
            label="Asylum",
            track_count=11,
            format="CD",
            discs={1: [MBTrack(number=1, title="Track 1", duration_ms=200000)]},
        )
        out = tmp_path / "release.json"
        with patch("music_tagger.cli.MusicBrainzClient") as mock_cls:
            mock_cls.return_value.fetch_release.return_value = release
            _mb_release(["r-1", "-o", str(out)])
        assert out.exists()
        data = json.loads(out.read_text())
        assert data["id"] == "r-1"
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "Desperado" in captured.out
        assert "11 tracks" in captured.out


class TestMatchCommand:
    def test_scores_candidates(self, flac_album: Path, tmp_path: Path, capsys: object) -> None:
        from music_tagger.tags import read_album as _read_album

        album = _read_album(flac_album)
        evidence_path = tmp_path / "evidence.json"
        evidence_path.write_text(json.dumps(album.to_dict()))

        release = MBRelease(
            id="r-1",
            title="Desperado",
            date="1973",
            country="US",
            label="Asylum",
            track_count=3,
            format="CD",
            discs={
                1: [
                    MBTrack(
                        number=1,
                        title="Desperado",
                        duration_ms=int(album.tracks[0].duration_secs * 1000),
                    ),
                    MBTrack(
                        number=2,
                        title="Twenty-One",
                        duration_ms=int(album.tracks[1].duration_secs * 1000),
                    ),
                    MBTrack(
                        number=3,
                        title="Out of Control",
                        duration_ms=int(album.tracks[2].duration_secs * 1000),
                    ),
                ]
            },
        )
        candidates_path = tmp_path / "candidates.json"
        candidates_path.write_text(json.dumps([release.to_dict()]))

        _match(["--evidence", str(evidence_path), "--candidates", str(candidates_path)])
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        data = json.loads(captured.out)
        assert len(data) == 1
        assert data[0]["match"]["tracks_within_2s"] == 3
        assert data[0]["release"]["id"] == "r-1"

    def test_digest_output(self, flac_album: Path, tmp_path: Path, capsys: object) -> None:
        from music_tagger.tags import read_album as _read_album

        album = _read_album(flac_album)
        evidence_path = tmp_path / "evidence.json"
        evidence_path.write_text(json.dumps(album.to_dict()))

        release = MBRelease(
            id="r-1",
            title="Desperado",
            track_count=3,
            discs={
                1: [
                    MBTrack(
                        number=1, title="A", duration_ms=int(album.tracks[0].duration_secs * 1000)
                    ),
                    MBTrack(
                        number=2, title="B", duration_ms=int(album.tracks[1].duration_secs * 1000)
                    ),
                    MBTrack(
                        number=3, title="C", duration_ms=int(album.tracks[2].duration_secs * 1000)
                    ),
                ]
            },
        )
        candidates_path = tmp_path / "candidates.json"
        candidates_path.write_text(json.dumps([release.to_dict()]))

        out = tmp_path / "matched.json"
        _match(
            ["--evidence", str(evidence_path), "--candidates", str(candidates_path), "-o", str(out)]
        )
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "1 candidates scored" in captured.out
        assert "r-1" in captured.out
        assert out.exists()


class TestDiffCommand:
    def test_exports_conflicts_and_reports_both_codes(
        self, flac_album: Path, tmp_path: Path, capsys: object
    ) -> None:
        album = read_album(flac_album)
        album.tracks[0].tags["isrc"] = "USIR19902220"
        evidence = tmp_path / "evidence.json"
        evidence.write_text(json.dumps(album.to_dict()))
        release = MBRelease(
            id="r-1",
            title="Desperado",
            discs={1: [MBTrack(number=1, title="Desperado", isrcs=["USDW10110279"])]},
        )
        release_file = tmp_path / "release.json"
        release_file.write_text(json.dumps(release.to_dict()))
        out = tmp_path / "diff.json"

        _diff(["--evidence", str(evidence), "--release", str(release_file), "-o", str(out)])

        data = json.loads(out.read_text())
        assert data["tracks"][0]["conflicts"] == [
            {
                "field": "isrc",
                "old_value": "USIR19902220",
                "candidate_values": ["USDW10110279"],
                "resolution": None,
            }
        ]
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "1 unresolved conflict" in captured.out
        assert "USIR19902220" in captured.err
        assert "USDW10110279" in captured.err

    def test_json_to_stdout(self, flac_album: Path, tmp_path: Path, capsys: object) -> None:
        from music_tagger.tags import read_album as _read_album

        album = _read_album(flac_album)
        evidence_path = tmp_path / "evidence.json"
        evidence_path.write_text(json.dumps(album.to_dict()))

        release = MBRelease(
            id="r-1",
            title="Desperado",
            date="1973",
            country="US",
            label="Asylum",
            track_count=3,
            format="CD",
            discs={
                1: [
                    MBTrack(number=1, title="Doolin-Dalton"),
                    MBTrack(number=2, title="Twenty-One"),
                    MBTrack(number=3, title="Out of Control"),
                ]
            },
        )
        release_path = tmp_path / "release.json"
        release_path.write_text(json.dumps(release.to_dict()))

        _diff(["--evidence", str(evidence_path), "--release", str(release_path)])
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        data = json.loads(captured.out)
        assert data["release_id"] == "r-1"
        assert len(data["tracks"]) == 3
        changed_tracks = [t for t in data["tracks"] if t["changes"]]
        assert len(changed_tracks) > 0

    def test_digest_shows_changed_fields(
        self, flac_album: Path, tmp_path: Path, capsys: object
    ) -> None:
        from music_tagger.tags import read_album as _read_album

        album = _read_album(flac_album)
        evidence_path = tmp_path / "evidence.json"
        evidence_path.write_text(json.dumps(album.to_dict()))

        release = MBRelease(
            id="r-1",
            title="Desperado",
            track_count=3,
            discs={
                1: [
                    MBTrack(number=1, title="Doolin-Dalton"),
                    MBTrack(number=2, title="Twenty-One"),
                    MBTrack(number=3, title="Out of Control"),
                ]
            },
            barcode="075596066624",
        )
        release_path = tmp_path / "release.json"
        release_path.write_text(json.dumps(release.to_dict()))

        out = tmp_path / "diff.json"
        _diff(["--evidence", str(evidence_path), "--release", str(release_path), "-o", str(out)])
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "r-1" in captured.out
        assert "with changes" in captured.out
        assert out.exists()


class TestWriteTagsCommand:
    def test_blocks_unresolved_conflict_without_other_changes(
        self, flac_album: Path, tmp_path: Path, capsys: object
    ) -> None:
        album = read_album(flac_album)
        track = album.tracks[0]
        before = track.path.read_bytes()
        conflict = TagConflict("isrc", "USIR19902220", ["USDW10110279"])
        result = AlbumResult(
            album, MBRelease("r-1", "Desperado"), [TrackDiff(track, [], [conflict])]
        )
        diff_path = tmp_path / "diff.json"
        diff_path.write_text(json.dumps(result.to_dict()))
        log_path = tmp_path / "changes.log"

        with pytest.raises(SystemExit) as error:
            _write_tags(["--diff", str(diff_path), "--log", str(log_path)])

        assert error.value.code == 1
        assert track.path.read_bytes() == before
        assert not log_path.exists()
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "Unresolved tag conflicts" in captured.err
        assert "No tag changes to apply" not in captured.out

    def test_previews_unresolved_conflict_without_writing(
        self, flac_album: Path, tmp_path: Path, capsys: object
    ) -> None:
        data = self._make_diff_json(flac_album)
        data["tracks"][0]["conflicts"] = [
            TagConflict("isrc", "USIR19902220", ["USDW10110279"]).to_dict()
        ]
        diff_path = tmp_path / "diff.json"
        diff_path.write_text(json.dumps(data))
        before = {path: path.read_bytes() for path in flac_album.glob("*.flac")}

        _write_tags(["--diff", str(diff_path), "--dry-run"])

        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "unresolved" in captured.err
        assert "dry run" in captured.out
        assert {path: path.read_bytes() for path in before} == before

    @pytest.mark.parametrize("resolution", ["USIR19902220", "USDW10110279"])
    def test_logs_explicit_isrc_resolution(
        self, flac_album: Path, tmp_path: Path, resolution: str
    ) -> None:
        album = read_album(flac_album)
        track = album.tracks[0]
        write_tags(track, [TagChange("isrc", "", "USIR19902220")])
        conflict = TagConflict("isrc", "USIR19902220", ["USDW10110279"], resolution)
        result = AlbumResult(
            album, MBRelease("r-1", "Desperado"), [TrackDiff(track, [], [conflict])]
        )
        diff_path = tmp_path / "diff.json"
        diff_path.write_text(json.dumps(result.to_dict()))
        log_path = tmp_path / "changes.log"

        _write_tags(["--diff", str(diff_path), "--log", str(log_path)])

        assert read_album(flac_album).tracks[0].tags["isrc"] == resolution
        log = log_path.read_text()
        assert "file=USIR19902220" in log
        assert "MusicBrainz=[USDW10110279]" in log
        assert f"resolution={resolution}" in log

    def _make_diff_json(self, flac_album: Path) -> dict:
        from music_tagger.tags import read_album as _read_album

        album = _read_album(flac_album)
        return {
            "release_id": "r-1",
            "release_title": "Desperado",
            "tracks": [
                {
                    "path": str(album.tracks[0].path),
                    "format": "flac",
                    "changes": [
                        {"field": "title", "old_value": "Desperado", "new_value": "New Title"},
                        {"field": "label", "old_value": "", "new_value": "Asylum"},
                    ],
                },
                {
                    "path": str(album.tracks[1].path),
                    "format": "flac",
                    "changes": [],
                },
            ],
        }

    def test_dry_run(self, capsys: object, flac_album: Path, tmp_path: Path) -> None:
        diff_path = tmp_path / "diff.json"
        diff_path.write_text(json.dumps(self._make_diff_json(flac_album)))
        _write_tags(["--diff", str(diff_path), "--dry-run"])
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "dry run" in captured.out
        assert "title" in captured.out
        from music_tagger.tags import read_album as _read_album

        album = _read_album(flac_album)
        assert album.tracks[0].tags["title"] == "Desperado"

    def test_applies_changes(self, capsys: object, flac_album: Path, tmp_path: Path) -> None:
        diff_path = tmp_path / "diff.json"
        diff_path.write_text(json.dumps(self._make_diff_json(flac_album)))
        _write_tags(["--diff", str(diff_path)])
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "Updated 1 track" in captured.out
        from music_tagger.tags import read_album as _read_album

        album = _read_album(flac_album)
        assert album.tracks[0].tags["title"] == "New Title"
        assert album.tracks[0].tags["label"] == "Asylum"
        assert album.tracks[1].tags["title"] == "Twenty-One"

    def test_no_changes(self, capsys: object, tmp_path: Path) -> None:
        diff_path = tmp_path / "diff.json"
        diff_path.write_text(
            json.dumps(
                {
                    "release_id": "r-1",
                    "release_title": "X",
                    "tracks": [{"path": "/x.flac", "format": "flac", "changes": []}],
                }
            )
        )
        _write_tags(["--diff", str(diff_path)])
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "No tag changes" in captured.out

    def test_log_file(self, flac_album: Path, tmp_path: Path) -> None:
        diff_path = tmp_path / "diff.json"
        diff_path.write_text(json.dumps(self._make_diff_json(flac_album)))
        log_path = tmp_path / "changes.log"
        _write_tags(["--diff", str(diff_path), "--log", str(log_path)])
        log_content = log_path.read_text()
        assert "r-1" in log_content
        assert "title" in log_content


class TestCandidatesCommand:
    def test_json_to_stdout(self, capsys: object, flac_album: Path) -> None:
        from music_tagger.cli import _print_candidates

        release = MBRelease(
            id="r-1",
            title="Desperado",
            date="1973",
            country="US",
            label="Asylum",
            track_count=3,
            format="CD",
            discs={
                1: [
                    MBTrack(number=1, title="Track 1", duration_ms=200000),
                    MBTrack(number=2, title="Track 2", duration_ms=200000),
                    MBTrack(number=3, title="Track 3", duration_ms=200000),
                ]
            },
        )

        with (
            patch("music_tagger.cli.search_candidates") as mock_search,
            patch("music_tagger.cli.MusicBrainzClient"),
        ):
            album = read_album(flac_album)
            mock_search.return_value = (album, [release])
            _print_candidates([str(flac_album), "--json"])

        captured = capsys.readouterr()  # type: ignore[attr-defined]
        data = json.loads(captured.out)
        assert data["album"]["artist"] == "Eagles"
        assert len(data["matches"]) == 1
        assert data["matches"][0]["release"]["id"] == "r-1"
        assert "tracks_within_2s" in data["matches"][0]["match"]

    def test_digest_with_out(self, capsys: object, flac_album: Path, tmp_path: Path) -> None:
        from music_tagger.cli import _print_candidates

        release = MBRelease(
            id="r-1",
            title="Desperado",
            date="1973",
            country="US",
            label="Asylum",
            track_count=3,
            format="CD",
            discs={
                1: [
                    MBTrack(number=1, title="T1", duration_ms=200000),
                    MBTrack(number=2, title="T2", duration_ms=200000),
                    MBTrack(number=3, title="T3", duration_ms=200000),
                ]
            },
        )

        out = tmp_path / "candidates.json"
        with (
            patch("music_tagger.cli.search_candidates") as mock_search,
            patch("music_tagger.cli.MusicBrainzClient"),
        ):
            album = read_album(flac_album)
            mock_search.return_value = (album, [release])
            _print_candidates([str(flac_album), "-o", str(out)])

        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "Eagles" in captured.out
        assert "r-1" in captured.out
        assert "Release ID" in captured.out
        data = json.loads(out.read_text())
        assert data["album"]["artist"] == "Eagles"


def _run_candidates(album_dir: Path, releases: list[MBRelease], *args: str) -> None:
    from music_tagger.cli import _print_candidates

    with (
        patch("music_tagger.cli.search_candidates") as search,
        patch("music_tagger.cli.MusicBrainzClient"),
    ):
        search.return_value = (read_album(album_dir), releases)
        _print_candidates([str(album_dir), *args])


class TestCandidatesTable:
    def _release(self, release_id: str, **overrides: object) -> MBRelease:
        fields: dict = {
            "id": release_id,
            "title": "Desperado",
            "date": "1973-04-17",
            "country": "US",
            "label": "Asylum",
            "barcode": "075596066624",
            "format": "CD",
            "track_count": 3,
            "discs": {
                1: [
                    MBTrack(number=1, title="T1", duration_ms=200000),
                    MBTrack(number=2, title="T2", duration_ms=200000),
                    MBTrack(number=3, title="T3", duration_ms=200000),
                ]
            },
        }
        fields.update(overrides)
        return MBRelease(**fields)

    def test_prints_table_by_default(self, flac_album: Path, capsys: object) -> None:
        _run_candidates(flac_album, [self._release("test-album-id-1"), self._release("other")])

        lines = capsys.readouterr().out.splitlines()  # type: ignore[attr-defined]
        assert lines[0] == "Local: Eagles — Desperado (3 tracks)"
        assert lines[1] == 'Searched: artist="Eagles" album="Desperado"'
        header = next(line for line in lines if "Release ID" in line)
        for column in (
            "#",
            "Tracks",
            "Within 2s",
            "Max dev",
            "Country",
            "Date",
            "Label",
            "Barcode",
            "Flags",
            "Title",
        ):
            assert column in header
        rows = lines[lines.index(header) + 1 :]
        assert rows[0].split()[:2] == ["1", "test-album-id-1"]
        assert "on-files" in rows[0]
        assert rows[1].split()[:2] == ["2", "other"]
        assert "3/3" in rows[0]

    def test_reports_override_search_terms(self, flac_album: Path, capsys: object) -> None:
        _run_candidates(flac_album, [self._release("r-1")], "--artist", "The Eagles")

        out = capsys.readouterr().out  # type: ignore[attr-defined]
        assert 'Searched: artist="The Eagles" album="Desperado"' in out

    def test_reports_no_candidates(self, flac_album: Path, capsys: object) -> None:
        _run_candidates(flac_album, [], "--barcode", "123")

        out = capsys.readouterr().out  # type: ignore[attr-defined]
        assert 'Searched: artist="Eagles" album="Desperado" barcode="123"' in out
        assert "No candidates found." in out
        assert "Release ID" not in out


class TestTagCommand:
    def test_blocks_conflicts_before_changing_recording_ids(
        self, flac_album: Path, tmp_path: Path, capsys: object
    ) -> None:
        from music_tagger.cli import _tag

        album = read_album(flac_album)
        write_tags(album.tracks[0], [TagChange("isrc", "", "USIR19902220")])
        before = {path: path.read_bytes() for path in flac_album.glob("*.flac")}
        release = MBRelease(
            id="r-1",
            title="Desperado",
            discs={
                1: [
                    MBTrack(
                        number=1,
                        title="Desperado",
                        recording_id="new-recording",
                        isrcs=["USDW10110279"],
                    )
                ]
            },
        )
        log_path = tmp_path / "changes.log"
        with patch("music_tagger.cli.MusicBrainzClient") as client:
            client.return_value.fetch_release.return_value = release
            with pytest.raises(SystemExit) as error:
                _tag([str(flac_album), "--release-id", "r-1", "--log", str(log_path)])

        assert error.value.code == EXIT_BLOCKED
        assert {path: path.read_bytes() for path in before} == before
        assert not log_path.exists()
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "USIR19902220" in captured.err
        assert "USDW10110279" in captured.err

    def test_dry_run(self, capsys: object, flac_album: Path) -> None:
        from music_tagger.cli import _tag
        from music_tagger.musicbrainz import MBRelease, MBTrack

        release = MBRelease(
            id="r-1",
            title="Desperado",
            date="1973",
            country="US",
            label="Asylum",
            track_count=3,
            format="CD",
            discs={
                1: [
                    MBTrack(number=1, title="Desperado"),
                    MBTrack(number=2, title="Twenty-One"),
                    MBTrack(number=3, title="Out of Control"),
                ]
            },
        )

        with patch("music_tagger.cli.MusicBrainzClient") as mock_cls:
            mock_cls.return_value.fetch_release.return_value = release
            _tag(["--release-id", "r-1", "--dry-run", str(flac_album)])

        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "dry run" in captured.out


class TestRenameCommand:
    def test_uses_disc_prefix_for_multi_disc_album(self, flac_album: Path) -> None:
        last = read_album(flac_album).tracks[2]
        write_tags(last, [TagChange("discnumber", "1", "2"), TagChange("tracknumber", "3", "1")])

        _rename([str(flac_album)])

        names = sorted(path.name for path in flac_album.glob("*.flac"))
        assert names == [
            "1-01. Desperado.flac",
            "1-02. Twenty-One.flac",
            "2-01. Out of Control.flac",
        ]

    def test_rewrites_cue_references(
        self, flac_album: Path, default_log: Path, capsys: object
    ) -> None:
        cue = flac_album / "Eagles - Desperado.cue"
        cue.write_text(
            "".join(f'FILE "{f.name}" WAVE\n' for f in sorted(flac_album.glob("*.flac")))
        )

        _rename([str(flac_album)])

        assert cue.read_text() == (
            'FILE "01. Desperado.flac" WAVE\n'
            'FILE "02. Twenty-One.flac" WAVE\n'
            'FILE "03. Out of Control.flac" WAVE\n'
        )
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "Updated CUE: Eagles - Desperado.cue" in captured.out
        assert "Warning" not in captured.err
        assert "- CUE updated: `Eagles - Desperado.cue`" in default_log.read_text()

    def test_warns_about_dangling_cue_reference(self, flac_album: Path, capsys: object) -> None:
        (flac_album / "album.cue").write_text('FILE "gone.flac" WAVE\n')

        _rename([str(flac_album)])

        err = capsys.readouterr().err  # type: ignore[attr-defined]
        assert "album.cue references missing file 'gone.flac'" in err

    def test_dry_run(self, capsys: object, flac_album: Path) -> None:
        for f in sorted(flac_album.glob("*.flac")):
            f.rename(f.parent / f"track{f.stem[:2]}.flac")

        _rename([str(flac_album), "--dry-run"])
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "track01.flac -> 01. Desperado.flac" in captured.out
        assert "dry run" in captured.out
        assert (flac_album / "track01.flac").exists()

    def test_renames_files(self, flac_album: Path, capsys: object) -> None:
        for f in sorted(flac_album.glob("*.flac")):
            f.rename(f.parent / f"track{f.stem[:2]}.flac")

        _rename([str(flac_album)])
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "Renamed 3 file(s)" in captured.out
        assert (flac_album / "01. Desperado.flac").exists()
        assert (flac_album / "02. Twenty-One.flac").exists()
        assert (flac_album / "03. Out of Control.flac").exists()

    def test_skips_already_correct(self, flac_album: Path, capsys: object) -> None:
        # Fixtures are named "NN - Track N.flac" but title tag is different,
        # so they do get renamed. Rename them to match their title tags first.
        for f in sorted(flac_album.glob("*.flac")):
            from mutagen.flac import FLAC

            audio = FLAC(str(f))
            title = audio["TITLE"][0]
            num = int(audio["TRACKNUMBER"][0])
            correct_name = f"{num:02d}. {title}.flac"
            if f.name != correct_name:
                f.rename(f.parent / correct_name)

        _rename([str(flac_album)])
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "Renamed 0 file(s)" in captured.out

    def test_skips_missing_title(self, flac_album: Path, capsys: object) -> None:
        from mutagen.flac import FLAC

        target = sorted(flac_album.glob("*.flac"))[0]
        audio = FLAC(str(target))
        del audio["TITLE"]
        audio.save()
        _rename([str(flac_album)])
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "skipped (missing title or tracknumber)" in captured.out

    def test_sanitizes_unsafe_chars(self, flac_album: Path, capsys: object) -> None:
        from mutagen.flac import FLAC

        target = sorted(flac_album.glob("*.flac"))[0]
        target.rename(flac_album / "track01.flac")
        audio = FLAC(str(flac_album / "track01.flac"))
        audio["TITLE"] = ["What:Is/This?"]
        audio.save()
        _rename([str(flac_album)])
        capsys.readouterr()  # type: ignore[attr-defined]
        assert (flac_album / "01. What-Is-This-.flac").exists()


class _FakeNavidromeClient:
    user = "alex"

    def __init__(
        self, songs: list[SongStats] | None = None, error: Exception | None = None
    ) -> None:
        self._songs = songs or []
        self._error = error

    def get_all_stats(self, page_size: int = 500) -> list[SongStats]:
        if self._error is not None:
            raise self._error
        return self._songs

    def close(self) -> None:
        pass


class TestNdStats:
    def test_malformed_native_statistics_preserve_both_snapshots(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = tmp_path / "stats.json"
        previous = tmp_path / "stats.json.previous"
        out.write_text("current snapshot")
        previous.write_text("previous snapshot")

        def malformed_stats(self: _FakeNavidromeClient, page_size: int = 500) -> list[SongStats]:
            return [SongStats.from_native({"id": "s1", "path": "a.flac", "playCount": False})]

        monkeypatch.setattr(_FakeNavidromeClient, "get_all_stats", malformed_stats)
        monkeypatch.setattr("music_tagger.cli.NavidromeClient", _FakeNavidromeClient)

        with pytest.raises(SystemExit) as exit_info:
            _nd(["stats", "--out", str(out)])

        assert exit_info.value.code == 1
        assert "export failed; no snapshot written" in capsys.readouterr().err
        assert out.read_text() == "current snapshot"
        assert previous.read_text() == "previous snapshot"

    def _songs(self) -> list[SongStats]:
        return [
            SongStats(id="s1", path="a.flac", rating=5, play_count=3, starred=True),
            SongStats(id="s2", path="b.flac", rating=0, play_count=0, starred=False),
        ]

    def test_writes_snapshot_with_explicit_zeros_and_reports_counts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = tmp_path / "stats.json"
        monkeypatch.setattr(
            "music_tagger.cli.NavidromeClient", lambda: _FakeNavidromeClient(self._songs())
        )

        _nd(["stats", "--out", str(out)])

        data = json.loads(out.read_text())
        assert data["sourceUser"] == "alex"
        assert data["songs"][1]["rating"] == 0
        assert data["songs"][1]["playCount"] == 0
        assert data["songs"][1]["starred"] is False
        assert data["exportedAt"].endswith("Z")
        assert "Exported 2 song(s): 1 rated, 1 played, 1 starred" in capsys.readouterr().out

    def test_rotates_previous_snapshot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        out = tmp_path / "stats.json"
        old = _snapshot(tmp_path / "old.json", [])
        out.write_text(old.read_text())
        monkeypatch.setattr(
            "music_tagger.cli.NavidromeClient", lambda: _FakeNavidromeClient(self._songs())
        )

        _nd(["stats", "--out", str(out)])

        assert (tmp_path / "stats.json.previous").read_text() == old.read_text()
        assert len(json.loads(out.read_text())["songs"]) == 2

    def test_large_drop_is_refused_until_forced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = tmp_path / "stats.json"
        old = _snapshot(
            tmp_path / "old.json",
            [{"id": f"s{i}", "path": f"{i}.flac", "starred": True} for i in range(2)],
        )
        out.write_text(old.read_text())
        before = out.read_bytes()
        monkeypatch.setattr(
            "music_tagger.cli.NavidromeClient", lambda: _FakeNavidromeClient(self._songs())
        )

        with pytest.raises(SystemExit) as exit_info:
            _nd(["stats", "--out", str(out)])

        assert exit_info.value.code == 1
        assert "starred count would fall from 2 to 1" in capsys.readouterr().err
        assert out.read_bytes() == before
        assert not (tmp_path / "stats.json.previous").exists()

        _nd(["stats", "--out", str(out), "--force"])

        assert len(json.loads(out.read_text())["songs"]) == 2
        assert (tmp_path / "stats.json.previous").read_bytes() == before

    def test_export_failure_keeps_existing_snapshot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = tmp_path / "stats.json"
        out.write_text('{"old": true}')
        failing = _FakeNavidromeClient(error=RuntimeError("count mismatch"))
        monkeypatch.setattr("music_tagger.cli.NavidromeClient", lambda: failing)

        with pytest.raises(SystemExit) as exit_info:
            _nd(["stats", "--out", str(out)])

        assert exit_info.value.code == 1
        assert out.read_text() == '{"old": true}'
        assert not (tmp_path / "stats.json.previous").exists()
        assert "no snapshot written: count mismatch" in capsys.readouterr().err

    def test_missing_directory_fails_before_connecting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def unexpected() -> _FakeNavidromeClient:
            pytest.fail("connected to Navidrome for a missing output directory")

        monkeypatch.setattr("music_tagger.cli.NavidromeClient", unexpected)

        with pytest.raises(SystemExit) as exit_info:
            _nd(["stats", "--out", str(tmp_path / "absent" / "stats.json")])

        assert exit_info.value.code == 1


class TestWriteRatings:
    @pytest.mark.parametrize("dry_run", [False, True])
    def test_alias_conflict_continues_and_preserves_audit_log(
        self,
        mp3_album: Path,
        tmp_path: Path,
        default_log: Path,
        capsys: pytest.CaptureFixture[str],
        dry_run: bool,
    ) -> None:
        tracks = sorted(mp3_album.glob("*.mp3"))
        audio = MP3(tracks[1])
        assert audio.tags is not None
        audio.tags.add(TXXX(desc="FMPS_Rating", text=["0.4"]))
        audio.tags.add(TXXX(desc="FMPS_RATING", text=["0.6"]))
        audio.save()
        before = [track.read_bytes() for track in tracks]
        ratings = tmp_path / "ratings.json"
        ratings.write_text(
            json.dumps(
                [
                    {"id": str(index), "path": str(track), "rating": 4}
                    for index, track in enumerate(tracks)
                ]
            )
        )
        args = ["--from", str(ratings), "--root", str(tmp_path)]
        if dry_run:
            args.append("--dry-run")

        with pytest.raises(SystemExit) as exit_info:
            _write_ratings(args)

        captured = capsys.readouterr()
        assert exit_info.value.code == 1
        assert "1 failed" in captured.out
        assert "FMPS rating stored values disagree" in captured.err
        assert tracks[1].read_bytes() == before[1]
        if dry_run:
            assert [track.read_bytes() for track in tracks] == before
            assert not default_log.exists()
        else:
            for track in (tracks[0], tracks[2]):
                assert MP3(track).tags["TXXX:RATING"].text == ["4"]
                assert str(track) in default_log.read_text()
            assert str(tracks[1]) not in default_log.read_text()

    def test_writes_to_flac(self, flac_album: Path, capsys: object) -> None:
        ratings = [
            {
                "id": "s1",
                "path": "album_flac/01 - Track 1.flac",
                "title": "T1",
                "artist": "A",
                "album": "Al",
                "rating": 4,
                "starred": "2024-01-01T00:00:00Z",
            },
        ]
        ratings_file = flac_album.parent / "ratings.json"
        ratings_file.write_text(json.dumps(ratings))

        _write_ratings(["--from", str(ratings_file), "--root", str(flac_album.parent)])

        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "1 file(s)" in captured.out

        album = read_album(flac_album)
        assert album.tracks[0].tags["fmps_rating"] == "0.8"
        assert album.tracks[0].tags["rating"] == "4"
        assert "starred" not in album.tracks[0].tags

    def test_dry_run(self, flac_album: Path, capsys: object) -> None:
        ratings = [
            {
                "id": "s1",
                "path": "album_flac/01 - Track 1.flac",
                "title": "T1",
                "artist": "A",
                "album": "Al",
                "rating": 3,
            },
        ]
        ratings_file = flac_album.parent / "ratings.json"
        ratings_file.write_text(json.dumps(ratings))

        _write_ratings(["--from", str(ratings_file), "--root", str(flac_album.parent), "--dry-run"])

        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "dry run" in captured.out

        album = read_album(flac_album)
        assert "rating" not in album.tracks[0].tags

    def test_skips_missing_files_exits_nonzero(self, flac_album: Path, capsys: object) -> None:
        ratings = [
            {
                "id": "s1",
                "path": "nonexistent/track.flac",
                "title": "T1",
                "artist": "A",
                "album": "Al",
                "rating": 5,
            },
        ]
        ratings_file = flac_album.parent / "ratings.json"
        ratings_file.write_text(json.dumps(ratings))

        with pytest.raises(SystemExit, match="1"):
            _write_ratings(["--from", str(ratings_file), "--root", str(flac_album.parent)])

        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "Skipped 1" in captured.err


def _desperado_release() -> MBRelease:
    return MBRelease(
        id="r-1",
        title="Desperado",
        discs={
            1: [
                MBTrack(number=1, title="Doolin-Dalton"),
                MBTrack(number=2, title="Twenty-One"),
                MBTrack(number=3, title="Out of Control"),
            ]
        },
    )


def _snapshot(path: Path, songs: list[dict[str, object]]) -> Path:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "sourceUser": "alex",
                "exportedAt": "2026-11-02T12:00:00Z",
                "songs": songs,
            }
        )
    )
    return path


TRACK_1 = "album_flac/01 - Track 1.flac"
TRACK_2 = "album_flac/02 - Track 2.flac"
TRACK_3 = "album_flac/03 - Track 3.flac"


def _flac_keys(path: Path) -> dict[str, list[str]]:
    comments = read_flac_comments(path)
    assert comments is not None
    return comments


class TestWriteStatsCommand:
    def test_stat_failure_continues_and_logs_completed_writes(
        self,
        flac_album: Path,
        tmp_path: Path,
        default_log: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        tracks = sorted(flac_album.glob("*.flac"))
        before = tracks[1].read_bytes()
        snap = _snapshot(
            tmp_path / "stats.json",
            [
                {"id": str(index), "path": str(track), "rating": 4}
                for index, track in enumerate(tracks)
            ],
        )
        original_stat = Path.stat

        def failing_stat(path: Path, *, follow_symlinks: bool = True) -> object:
            if path == tracks[1]:
                raise PermissionError("simulated NAS failure")
            return original_stat(path, follow_symlinks=follow_symlinks)

        with patch.object(Path, "stat", failing_stat), pytest.raises(SystemExit) as exit_info:
            _write_stats(["--from", str(snap), "--root", str(tmp_path)])

        captured = capsys.readouterr()
        assert exit_info.value.code == 1
        assert "0 missing, 0 unsupported, 1 failed" in captured.out
        assert "simulated NAS failure" in captured.err
        assert tracks[1].read_bytes() == before
        for track in (tracks[0], tracks[2]):
            assert _flac_keys(track)["RATING"] == ["4"]
            assert str(track) in default_log.read_text()
        assert str(tracks[1]) not in default_log.read_text()

    def test_mixed_outcomes_summarize_and_exit_nonzero(
        self, flac_album: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "notes.flac").write_text("not audio")
        snap = _snapshot(
            tmp_path / "stats.json",
            [
                {"id": "s1", "path": TRACK_1, "rating": 4, "playCount": 9},
                {"id": "s2", "path": TRACK_2},
                {"id": "s3", "path": TRACK_3},
                {"id": "s4", "path": "missing.flac", "rating": 3},
                {"id": "s5", "path": "notes.flac", "rating": 3},
            ],
        )

        with pytest.raises(SystemExit) as exit_info:
            _write_stats(["--from", str(snap), "--root", str(tmp_path), "--no-log"])

        captured = capsys.readouterr()
        assert exit_info.value.code == 1
        assert (
            "1 changed, 2 unchanged, 0 clear withheld, 0 decrease withheld, "
            "1 missing, 1 unsupported, 0 failed." in captured.out
        )
        assert "rewrote 1 file(s)" in captured.out
        comments = _flac_keys(flac_album / "01 - Track 1.flac")
        assert comments["FMPS_RATING"] == ["0.8"]
        assert comments["FMPS_PLAYCOUNT"] == ["9"]
        assert "unsupported: notes.flac" in captured.err
        assert "missing: missing.flac" in captured.err

    def test_clean_run_exits_zero(
        self, flac_album: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        snap = _snapshot(
            tmp_path / "stats.json",
            [{"id": "s1", "path": TRACK_1, "rating": 4, "playCount": 9}],
        )

        _write_stats(["--from", str(snap), "--root", str(tmp_path), "--no-log"])

        assert "1 changed, 0 unchanged" in capsys.readouterr().out

    def test_withheld_clear_and_decrease_are_reported_and_keep_values(
        self, flac_album: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        first = _snapshot(
            tmp_path / "first.json",
            [{"id": "s1", "path": TRACK_1, "rating": 4, "playCount": 9}],
        )
        _write_stats(["--from", str(first), "--root", str(tmp_path), "--no-log"])
        capsys.readouterr()
        second = _snapshot(
            tmp_path / "second.json",
            [{"id": "s1", "path": TRACK_1, "rating": 0, "playCount": 5}],
        )

        _write_stats(["--from", str(second), "--root", str(tmp_path), "--no-log"])

        captured = capsys.readouterr()
        assert "clear withheld: " + TRACK_1 in captured.out
        assert "decrease withheld: " + TRACK_1 in captured.out
        assert "0 changed, 1 unchanged, 1 clear withheld, 1 decrease withheld" in captured.out
        comments = read_flac_comments(flac_album / "01 - Track 1.flac")
        assert comments is not None
        assert comments["RATING"] == ["4"]
        assert comments["FMPS_PLAYCOUNT"] == ["9"]

    def test_clear_and_allow_decrease_apply(
        self, flac_album: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        first = _snapshot(
            tmp_path / "first.json",
            [{"id": "s1", "path": TRACK_1, "rating": 4, "playCount": 9}],
        )
        _write_stats(["--from", str(first), "--root", str(tmp_path), "--no-log"])
        second = _snapshot(
            tmp_path / "second.json",
            [{"id": "s1", "path": TRACK_1, "rating": 0, "playCount": 5}],
        )

        _write_stats(
            [
                "--from",
                str(second),
                "--root",
                str(tmp_path),
                "--no-log",
                "--clear",
                "--allow-decrease",
            ]
        )

        comments = read_flac_comments(flac_album / "01 - Track 1.flac")
        assert comments is not None
        assert "RATING" not in comments
        assert comments["FMPS_PLAYCOUNT"] == ["5"]

    def test_dry_run_previews_changes_and_writes_nothing(
        self,
        flac_album: Path,
        tmp_path: Path,
        default_log: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        snap = _snapshot(
            tmp_path / "stats.json",
            [{"id": "s1", "path": TRACK_1, "rating": 4, "playCount": 9}],
        )
        track = flac_album / "01 - Track 1.flac"
        before = track.read_bytes()

        _write_stats(["--from", str(snap), "--root", str(tmp_path), "--dry-run"])

        captured = capsys.readouterr()
        assert f"  {TRACK_1}:" in captured.out
        assert "fmps_playcount: (empty) → 9" in captured.out
        assert "1 would change" in captured.out
        assert "would rewrite 1 file(s), " in captured.out
        assert track.read_bytes() == before
        assert not default_log.exists()

    def test_logs_real_changes_to_default_log(
        self, flac_album: Path, tmp_path: Path, default_log: Path
    ) -> None:
        snap = _snapshot(
            tmp_path / "stats.json",
            [{"id": "s1", "path": TRACK_1, "rating": 4, "playCount": 9}],
        )

        _write_stats(["--from", str(snap), "--root", str(tmp_path)])

        log = default_log.read_text()
        assert "## Statistics" in log
        assert f"  {TRACK_1}: " in log
        assert "fmps_playcount (empty) → 9" in log

    def test_no_log_writes_nothing(
        self, flac_album: Path, tmp_path: Path, default_log: Path
    ) -> None:
        snap = _snapshot(
            tmp_path / "stats.json",
            [{"id": "s1", "path": TRACK_1, "rating": 4, "playCount": 9}],
        )

        _write_stats(["--from", str(snap), "--root", str(tmp_path), "--no-log"])

        assert not default_log.exists()

    def test_invalid_snapshot_writes_nothing(
        self, flac_album: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        snap = _snapshot(
            tmp_path / "stats.json",
            [
                {"id": "s1", "path": TRACK_1, "rating": 4},
                {"id": "s2", "path": TRACK_2, "rating": 9},
            ],
        )

        with pytest.raises(SystemExit) as exit_info:
            _write_stats(["--from", str(snap), "--root", str(tmp_path), "--no-log"])

        assert exit_info.value.code == 1
        assert "RATING" not in _flac_keys(flac_album / "01 - Track 1.flac")
        assert "Nothing written." in capsys.readouterr().err

    def test_outside_root_path_writes_nothing(self, flac_album: Path, tmp_path: Path) -> None:
        lib = tmp_path / "lib"
        lib.mkdir()
        snap = _snapshot(
            lib / "stats.json",
            [
                {"id": "s1", "path": "../" + TRACK_1, "rating": 4},
            ],
        )

        with pytest.raises(SystemExit) as exit_info:
            _write_stats(["--from", str(snap), "--root", str(lib), "--no-log"])

        assert exit_info.value.code == 1
        assert "RATING" not in _flac_keys(flac_album / "01 - Track 1.flac")

    def test_per_file_failure_does_not_stop_later_files(
        self, flac_album: Path, tmp_path: Path
    ) -> None:
        (tmp_path / "notes.flac").write_text("not audio")
        snap = _snapshot(
            tmp_path / "stats.json",
            [
                {"id": "s1", "path": TRACK_1, "rating": 4},
                {"id": "s2", "path": "notes.flac", "rating": 4},
                {"id": "s3", "path": TRACK_3, "rating": 2},
            ],
        )

        with pytest.raises(SystemExit):
            _write_stats(["--from", str(snap), "--root", str(tmp_path), "--no-log"])

        assert _flac_keys(flac_album / "01 - Track 1.flac")["FMPS_RATING"] == ["0.8"]
        assert _flac_keys(flac_album / "03 - Track 3.flac")["FMPS_RATING"] == ["0.4"]

    def test_missing_snapshot_is_reported(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exit_info:
            _write_stats(["--from", str(tmp_path / "absent.json"), "--root", str(tmp_path)])

        assert exit_info.value.code == 1
        assert "cannot read snapshot" in capsys.readouterr().err


class TestStatsEndToEnd:
    """Export, write, and repeat over fixture files, with a fake Navidrome."""

    @staticmethod
    def _export(monkeypatch: pytest.MonkeyPatch, out: Path, songs: list[SongStats]) -> Path:
        monkeypatch.setattr("music_tagger.cli.NavidromeClient", lambda: _FakeNavidromeClient(songs))
        _nd(["stats", "--out", str(out)])
        return out

    def test_export_write_repeat_keeps_favorites_and_withheld_values(
        self,
        flac_album: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        track_1 = flac_album / "01 - Track 1.flac"
        track_2 = flac_album / "02 - Track 2.flac"
        track_3 = flac_album / "03 - Track 3.flac"
        # A favorite another player wrote must survive the write.
        audio = FLAC(track_1)
        audio["STARRED"] = "2020-01-01T00:00:00Z"
        audio.save()
        songs = [
            SongStats(
                id="s1",
                path=TRACK_1,
                rating=4,
                starred=True,
                starred_at="2026-01-01T00:00:00Z",
                play_count=9,
            ),
            SongStats(id="s2", path=TRACK_2, rating=0, starred=False, play_count=0),
            SongStats(id="s3", path=TRACK_3, rating=0, starred=False, play_count=3),
        ]

        stats = self._export(monkeypatch, tmp_path / "stats.json", songs)
        exported = json.loads(stats.read_text())
        assert exported["songs"][0]["starred"] is True
        assert exported["songs"][1]["rating"] == 0
        assert exported["songs"][1]["playCount"] == 0

        _write_stats(["--from", str(stats), "--root", str(tmp_path), "--no-log"])

        first = _flac_keys(track_1)
        assert first["RATING"] == ["4"]
        assert first["FMPS_RATING"] == ["0.8"]
        assert first["FMPS_PLAYCOUNT"] == ["9"]
        assert first["STARRED"] == ["2020-01-01T00:00:00Z"]
        assert _flac_keys(track_2)["FMPS_PLAYCOUNT"] == ["0"]
        assert "RATING" not in _flac_keys(track_2)
        assert _flac_keys(track_3)["FMPS_PLAYCOUNT"] == ["3"]
        before = {path: path.read_bytes() for path in (track_1, track_2, track_3)}

        _write_stats(["--from", str(stats), "--root", str(tmp_path), "--no-log"])

        assert "0 changed, 3 unchanged" in capsys.readouterr().out
        assert {path: path.read_bytes() for path in (track_1, track_2, track_3)} == before

        # A later export with a zero rating and a lower count changes nothing.
        lower = [
            SongStats(id="s1", path=TRACK_1, rating=0, starred=False, play_count=2),
        ]
        stats = self._export(monkeypatch, tmp_path / "lower.json", lower)
        _write_stats(["--from", str(stats), "--root", str(tmp_path), "--no-log"])

        out = capsys.readouterr().out
        assert "clear withheld" in out
        assert "decrease withheld" in out
        kept = _flac_keys(track_1)
        assert kept["RATING"] == ["4"]
        assert kept["FMPS_PLAYCOUNT"] == ["9"]


class TestWriteRatingsLogging:
    @staticmethod
    def _ratings_file(tmp_path: Path) -> Path:
        ratings_file = tmp_path / "ratings.json"
        ratings_file.write_text(
            json.dumps([{"id": "s1", "path": "album_flac/01 - Track 1.flac", "rating": 4}])
        )
        return ratings_file

    def test_logs_to_default_log_by_default(
        self, flac_album: Path, tmp_path: Path, default_log: Path
    ) -> None:
        _write_ratings(["--from", str(self._ratings_file(tmp_path)), "--root", str(tmp_path)])

        log = default_log.read_text()
        assert "## Rating tags" in log
        assert "album_flac/01 - Track 1.flac: fmps_rating, rating" in log

    def test_log_flag_redirects_the_log(
        self, flac_album: Path, tmp_path: Path, default_log: Path
    ) -> None:
        custom = tmp_path / "custom.log"

        _write_ratings(
            [
                "--from",
                str(self._ratings_file(tmp_path)),
                "--root",
                str(tmp_path),
                "--log",
                str(custom),
            ]
        )

        assert "## Rating tags" in custom.read_text()
        assert not default_log.exists()

    def test_no_log_writes_nothing(
        self, flac_album: Path, tmp_path: Path, default_log: Path
    ) -> None:
        _write_ratings(
            ["--from", str(self._ratings_file(tmp_path)), "--root", str(tmp_path), "--no-log"]
        )

        assert not default_log.exists()

    def test_dry_run_appends_no_log(
        self, flac_album: Path, tmp_path: Path, default_log: Path
    ) -> None:
        _write_ratings(
            ["--from", str(self._ratings_file(tmp_path)), "--root", str(tmp_path), "--dry-run"]
        )

        assert not default_log.exists()


class TestAuditLogSurvivesInterrupt:
    """Entries for files already changed are kept when a run is interrupted."""

    def test_write_stats_keeps_completed_entries(
        self,
        flac_album: Path,
        tmp_path: Path,
        default_log: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tracks = sorted(flac_album.glob("*.flac"))
        snap = _snapshot(
            tmp_path / "stats.json",
            [
                {"id": str(index), "path": str(track), "rating": 4}
                for index, track in enumerate(tracks[:2])
            ],
        )
        original = cli_module.write_stats_to_file
        calls = 0

        def interrupt_second(path: Path, **kwargs: object) -> object:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt
            return original(path, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(cli_module, "write_stats_to_file", interrupt_second)

        with pytest.raises(KeyboardInterrupt):
            _write_stats(["--from", str(snap), "--root", str(tmp_path)])

        log = default_log.read_text()
        assert "## Statistics" in log
        assert f"  {tracks[0]}: " in log
        assert str(tracks[1]) not in log
        assert log.endswith("\n---\n")

    def test_write_ratings_keeps_completed_entries(
        self,
        flac_album: Path,
        tmp_path: Path,
        default_log: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tracks = sorted(flac_album.glob("*.flac"))
        ratings_file = tmp_path / "ratings.json"
        ratings_file.write_text(
            json.dumps(
                [
                    {"id": str(index), "path": str(track), "rating": 4}
                    for index, track in enumerate(tracks[:2])
                ]
            )
        )
        original = cli_module.write_rating_to_file
        calls = 0

        def interrupt_second(path: Path, rating: int, dry_run: bool = False) -> object:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt
            return original(path, rating, dry_run=dry_run)

        monkeypatch.setattr(cli_module, "write_rating_to_file", interrupt_second)

        with pytest.raises(KeyboardInterrupt):
            _write_ratings(["--from", str(ratings_file), "--root", str(tmp_path)])

        log = default_log.read_text()
        assert log.startswith("## Rating tags\n\n- **Date:** ")
        assert f"  {tracks[0]}: fmps_rating, rating" in log
        assert str(tracks[1]) not in log
        assert log.endswith("\n---\n")

    def test_completed_run_writes_header_once_and_one_footer(
        self, flac_album: Path, tmp_path: Path, default_log: Path
    ) -> None:
        tracks = sorted(flac_album.glob("*.flac"))
        ratings_file = tmp_path / "ratings.json"
        ratings_file.write_text(
            json.dumps(
                [
                    {"id": str(index), "path": str(track), "rating": 4}
                    for index, track in enumerate(tracks[:2])
                ]
            )
        )

        _write_ratings(["--from", str(ratings_file), "--root", str(tmp_path)])

        log = default_log.read_text()
        assert log.count("## Rating tags") == 1
        assert log.count("\n---\n") == 1
        assert log.endswith("\n\n---\n")
        assert f"  {tracks[1]}: fmps_rating, rating\n" in log


class TestMissingFileReporting:
    def test_missing_lines_are_capped_with_a_count(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        snap = _snapshot(
            tmp_path / "stats.json",
            [
                {"id": str(index), "path": f"album_flac/absent {index:02d}.flac", "rating": 4}
                for index in range(25)
            ],
        )

        with pytest.raises(SystemExit) as exit_info:
            _write_stats(["--from", str(snap), "--root", str(tmp_path), "--no-log"])

        captured = capsys.readouterr()
        assert exit_info.value.code == 1
        assert captured.err.count("  missing: ") == 20
        assert "  … and 5 more missing" in captured.err
        assert "0 changed" in captured.out
        assert "25 missing" in captured.out

    def test_file_not_found_after_exists_counts_as_failed(
        self, flac_album: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        tracks = sorted(flac_album.glob("*.flac"))
        snap = _snapshot(
            tmp_path / "stats.json",
            [
                {"id": str(index), "path": str(track), "rating": 4}
                for index, track in enumerate(tracks)
            ],
        )
        original_exists = Path.exists
        original_stat = Path.stat

        def exists(path: Path, *, follow_symlinks: bool = True) -> bool:
            if path == tracks[1]:
                return True
            return original_exists(path, follow_symlinks=follow_symlinks)

        def vanishing_stat(path: Path, *, follow_symlinks: bool = True) -> object:
            if path == tracks[1]:
                raise FileNotFoundError("vanished after the existence check")
            return original_stat(path, follow_symlinks=follow_symlinks)

        with (
            patch.object(Path, "exists", exists),
            patch.object(Path, "stat", vanishing_stat),
            pytest.raises(SystemExit),
        ):
            _write_stats(["--from", str(snap), "--root", str(tmp_path), "--no-log"])

        captured = capsys.readouterr()
        assert "0 missing, 0 unsupported, 1 failed" in captured.out
        assert "vanished after the existence check" in captured.err

    def test_dry_run_summary_counts_missing(
        self, flac_album: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ratings_file = tmp_path / "ratings.json"
        ratings_file.write_text(
            json.dumps(
                [
                    {"id": "s1", "path": "album_flac/01 - Track 1.flac", "rating": 4},
                    {"id": "s2", "path": "album_flac/absent.flac", "rating": 4},
                ]
            )
        )

        _write_ratings(
            ["--from", str(ratings_file), "--root", str(tmp_path), "--dry-run", "--no-log"]
        )

        assert "1 would change, 0 unchanged, 1 missing, 0 unsupported, 0 failed" in (
            capsys.readouterr().out
        )


class TestWriteRatingsUnsupported:
    def test_unsupported_file_is_counted_separately(
        self, flac_album: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "notes.flac").write_text("not audio")
        ratings_file = tmp_path / "ratings.json"
        ratings_file.write_text(
            json.dumps(
                [
                    {"id": "s1", "path": "album_flac/01 - Track 1.flac", "rating": 4},
                    {"id": "s2", "path": "notes.flac", "rating": 4},
                ]
            )
        )

        with pytest.raises(SystemExit) as exit_info:
            _write_ratings(["--from", str(ratings_file), "--root", str(tmp_path)])

        captured = capsys.readouterr()
        assert exit_info.value.code == 1
        assert read_album(flac_album).tracks[0].tags["rating"] == "4"
        assert "1 unsupported" in captured.out
        assert "unsupported: notes.flac" in captured.err

    def test_already_correct_file_is_reported_as_unchanged(
        self, flac_album: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ratings_file = tmp_path / "ratings.json"
        ratings_file.write_text(
            json.dumps([{"id": "s1", "path": "album_flac/01 - Track 1.flac", "rating": 4}])
        )
        _write_ratings(["--from", str(ratings_file), "--root", str(tmp_path)])
        capsys.readouterr()

        _write_ratings(["--from", str(ratings_file), "--root", str(tmp_path)])

        assert "0 file(s); 1 unchanged" in capsys.readouterr().out


class TestWriteRatingsContainment:
    @pytest.mark.parametrize("kind", ["traversal", "absolute", "root"])
    def test_refuses_path_outside_root_before_any_write(
        self,
        kind: str,
        flac_album: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        lib = tmp_path / "lib"
        lib.mkdir()
        shutil.copy(sorted(flac_album.glob("*.flac"))[0], lib / "inside.flac")
        track = sorted(flac_album.glob("*.flac"))[0]
        outside = {
            "traversal": f"../album_flac/{track.name}",
            "absolute": str(track),
            "root": ".",
        }[kind]
        ratings = [
            {"id": "s1", "path": "inside.flac", "rating": 4},
            {"id": "s2", "path": outside, "rating": 4},
        ]
        ratings_file = tmp_path / "ratings.json"
        ratings_file.write_text(json.dumps(ratings))

        with pytest.raises(SystemExit) as exit_info:
            _write_ratings(["--from", str(ratings_file), "--root", str(lib)])

        assert exit_info.value.code == 1
        assert FLAC(lib / "inside.flac").get("RATING") is None
        assert FLAC(track).get("RATING") is None
        assert "outside --root" in capsys.readouterr().err

    def test_refuses_symlink_out_of_root(self, flac_album: Path, tmp_path: Path) -> None:
        lib = tmp_path / "lib"
        lib.mkdir()
        track = sorted(flac_album.glob("*.flac"))[0]
        (lib / "link.flac").symlink_to(track)
        ratings_file = tmp_path / "ratings.json"
        ratings_file.write_text(json.dumps([{"id": "s1", "path": "link.flac", "rating": 4}]))

        with pytest.raises(SystemExit) as exit_info:
            _write_ratings(["--from", str(ratings_file), "--root", str(lib)])

        assert exit_info.value.code == 1
        assert FLAC(track).get("RATING") is None

    def test_writes_path_inside_root(self, flac_album: Path, tmp_path: Path) -> None:
        ratings_file = tmp_path / "ratings.json"
        ratings_file.write_text(
            json.dumps([{"id": "s1", "path": "album_flac/01 - Track 1.flac", "rating": 4}])
        )

        _write_ratings(["--from", str(ratings_file), "--root", str(flac_album.parent)])

        assert read_album(flac_album).tracks[0].tags["rating"] == "4"


class TestLockCommand:
    def test_shows_locks_per_track(self, flac_album: Path, capsys: object) -> None:
        write_locks(sorted(flac_album.glob("*.flac"))[0], ["title", "isrc"])

        _lock([str(flac_album)])

        out = capsys.readouterr().out  # type: ignore[attr-defined]
        assert "01 - Track 1.flac: isrc, title" in out
        assert "02 - Track 2.flac: (none)" in out

    def test_locks_every_track(self, flac_album: Path) -> None:
        _lock([str(flac_album), "isrc"])

        assert [track.locked_fields for track in read_album(flac_album).tracks] == [
            ["isrc"],
            ["isrc"],
            ["isrc"],
        ]

    def test_adds_to_existing_locks(self, flac_album: Path) -> None:
        write_locks(sorted(flac_album.glob("*.flac"))[0], ["title"])

        _lock([str(flac_album), "isrc"])

        assert read_album(flac_album).tracks[0].locked_fields == ["isrc", "title"]

    def test_locks_one_track(self, flac_album: Path) -> None:
        _lock([str(flac_album), "title", "--track", "2"])

        assert [track.locked_fields for track in read_album(flac_album).tracks] == [
            [],
            ["title"],
            [],
        ]

    def test_unmatched_track_writes_nothing(self, flac_album: Path, capsys: object) -> None:
        before = {path: path.read_bytes() for path in flac_album.glob("*.flac")}

        with pytest.raises(SystemExit) as error:
            _lock([str(flac_album), "title", "--track", "9"])

        assert error.value.code == 1
        assert {path: path.read_bytes() for path in before} == before
        assert "9" in capsys.readouterr().err  # type: ignore[attr-defined]

    def test_unknown_field_writes_nothing(self, flac_album: Path, capsys: object) -> None:
        before = {path: path.read_bytes() for path in flac_album.glob("*.flac")}

        with pytest.raises(SystemExit) as error:
            _lock([str(flac_album), "titel"])

        assert error.value.code == 1
        assert {path: path.read_bytes() for path in before} == before
        assert "titel" in capsys.readouterr().err  # type: ignore[attr-defined]

    def test_dry_run_writes_nothing(self, flac_album: Path, capsys: object) -> None:
        before = {path: path.read_bytes() for path in flac_album.glob("*.flac")}

        _lock([str(flac_album), "title", "--dry-run"])

        assert {path: path.read_bytes() for path in before} == before
        assert "dry run" in capsys.readouterr().out  # type: ignore[attr-defined]

    def test_logs_locked_files(self, flac_album: Path, tmp_path: Path) -> None:
        log_path = tmp_path / "changes.log"

        _lock([str(flac_album), "title", "--track", "1", "--log", str(log_path)])

        log = log_path.read_text()
        assert "## Lock: title" in log
        assert "01 - Track 1.flac" in log
        assert "02 - Track 2.flac" not in log

    def test_dry_run_does_not_log(self, flac_album: Path, tmp_path: Path) -> None:
        log_path = tmp_path / "changes.log"

        _lock([str(flac_album), "title", "--dry-run", "--log", str(log_path)])

        assert not log_path.exists()

    def test_m4a_album_fails_before_writing(self, m4a_album: Path, capsys: object) -> None:
        before = {path: path.read_bytes() for path in m4a_album.glob("*.m4a")}

        with pytest.raises(SystemExit) as error:
            _lock([str(m4a_album), "title"])

        assert error.value.code == 1
        assert {path: path.read_bytes() for path in before} == before
        assert "not supported" in capsys.readouterr().err  # type: ignore[attr-defined]


class TestUnlockCommand:
    def test_removes_one_field(self, flac_album: Path) -> None:
        write_locks(sorted(flac_album.glob("*.flac"))[0], ["title", "isrc"])

        _unlock([str(flac_album), "title"])

        assert read_album(flac_album).tracks[0].locked_fields == ["isrc"]

    def test_logs_unlocked_files(self, flac_album: Path, tmp_path: Path) -> None:
        write_locks(sorted(flac_album.glob("*.flac"))[0], ["title"])
        log_path = tmp_path / "changes.log"

        _unlock([str(flac_album), "title", "--log", str(log_path)])

        log = log_path.read_text()
        assert "## Unlock: title" in log
        assert "01 - Track 1.flac" in log

    def test_requires_fields(self, flac_album: Path) -> None:
        with pytest.raises(SystemExit) as error:
            _unlock([str(flac_album)])

        assert error.value.code == 2


class TestTagCommandLocks:
    def test_keeps_locked_field_and_reports_it(
        self, flac_album: Path, tmp_path: Path, capsys: object
    ) -> None:
        from music_tagger.cli import _tag

        write_locks(sorted(flac_album.glob("*.flac"))[0], ["title"])
        log_path = tmp_path / "changes.log"

        with patch("music_tagger.cli.MusicBrainzClient") as client:
            client.return_value.fetch_release.return_value = _desperado_release()
            _tag([str(flac_album), "--release-id", "r-1", "--log", str(log_path)])

        updated = read_album(flac_album).tracks[0].tags
        assert updated["title"] == "Desperado"
        assert updated["musicbrainz_albumid"] == "r-1"
        out = capsys.readouterr().out  # type: ignore[attr-defined]
        assert "title: Desperado (locked; release has Doolin-Dalton)" in out
        assert "title: Desperado (locked; release has Doolin-Dalton)" in log_path.read_text()

    def test_only_locked_changes_are_not_reported_as_correct(
        self, flac_album: Path, capsys: object
    ) -> None:
        from music_tagger.cli import _tag

        release = _desperado_release()
        release.id = "test-album-id-1"
        _tag_album_to_match(flac_album, release)
        first_track = read_album(flac_album).tracks[0]
        write_tags(first_track, [TagChange("title", "Doolin-Dalton", "Desperado")])
        write_locks(first_track.path, ["title"])

        with patch("music_tagger.cli.MusicBrainzClient") as client:
            client.return_value.fetch_release.return_value = release
            _tag([str(flac_album), "--release-id", release.id])

        out = capsys.readouterr().out  # type: ignore[attr-defined]
        assert "No tag changes needed" not in out
        assert "1 locked field(s) kept" in out

    def test_ignore_locks_overwrites_locked_field(self, flac_album: Path) -> None:
        from music_tagger.cli import _tag

        write_locks(sorted(flac_album.glob("*.flac"))[0], ["title"])

        with patch("music_tagger.cli.MusicBrainzClient") as client:
            client.return_value.fetch_release.return_value = _desperado_release()
            _tag([str(flac_album), "--release-id", "r-1", "--ignore-locks", "--approve-overwrites"])

        updated = read_album(flac_album).tracks[0]
        assert updated.tags["title"] == "Doolin-Dalton"
        assert updated.locked_fields == ["title"]


class TestWriteTagsCommandLocks:
    def test_refuses_change_to_locked_field(
        self, flac_album: Path, tmp_path: Path, capsys: object
    ) -> None:
        album = read_album(flac_album)
        track = album.tracks[0]
        result = AlbumResult(
            album,
            MBRelease("r-1", "Desperado"),
            [TrackDiff(track, [TagChange("title", "Desperado", "Doolin-Dalton")])],
        )
        diff_path = tmp_path / "diff.json"
        diff_path.write_text(json.dumps(result.to_dict()))
        write_locks(track.path, ["title"])
        before = track.path.read_bytes()

        with pytest.raises(SystemExit) as error:
            _write_tags(["--diff", str(diff_path)])

        assert error.value.code == 1
        assert track.path.read_bytes() == before
        assert "Locked fields" in capsys.readouterr().err  # type: ignore[attr-defined]


def _tag_album_to_match(album_dir: Path, release: MBRelease) -> None:
    """Apply `release` so a second `tag` run proposes no changes."""
    from music_tagger.tagger import apply_changes, build_diff

    apply_changes(build_diff(read_album(album_dir), release))


def _gate_release(first_title: str) -> MBRelease:
    """A release that matches the FLAC fixture except, possibly, track 1's title."""
    return MBRelease(
        id="r-1",
        title="Desperado",
        artist="Eagles",
        discs={
            1: [
                MBTrack(number=1, title=first_title),
                MBTrack(number=2, title="Twenty-One"),
                MBTrack(number=3, title="Out of Control"),
            ]
        },
    )


def _run_tag(album_dir: Path, release: MBRelease, *extra: str) -> None:
    from music_tagger.cli import _tag

    with patch("music_tagger.cli.MusicBrainzClient") as client:
        client.return_value.fetch_release.return_value = release
        _tag([str(album_dir), "--release-id", release.id, *extra])


class TestTagCommandGate:
    def test_dry_run_passes_gate_for_id_and_fill_changes(
        self, flac_album: Path, capsys: object
    ) -> None:
        _run_tag(flac_album, _gate_release("Desperado"), "--dry-run")

        assert "Gate: auto" in capsys.readouterr().out  # type: ignore[attr-defined]

    def test_dry_run_passes_gate_for_style_only_overwrite(
        self, flac_album: Path, capsys: object
    ) -> None:
        _run_tag(flac_album, _gate_release("DESPERADO"), "--dry-run")

        assert "Gate: auto" in capsys.readouterr().out  # type: ignore[attr-defined]

    def test_dry_run_exits_review_for_overwrite(self, flac_album: Path, capsys: object) -> None:
        with pytest.raises(SystemExit) as error:
            _run_tag(flac_album, _gate_release("Doolin-Dalton"), "--dry-run")

        assert error.value.code == EXIT_REVIEW
        out = capsys.readouterr().out  # type: ignore[attr-defined]
        assert "Gate: review" in out
        assert "01 - Track 1.flac: title: Desperado → Doolin-Dalton" in out

    def test_dry_run_exits_blocked_for_unresolved_conflict(
        self, flac_album: Path, capsys: object
    ) -> None:
        write_tags(read_album(flac_album).tracks[0], [TagChange("isrc", "", "USIR19902220")])
        release = _gate_release("Desperado")
        release.discs[1][0].isrcs = ["USDW10110279"]

        with pytest.raises(SystemExit) as error:
            _run_tag(flac_album, release, "--dry-run")

        assert error.value.code == EXIT_BLOCKED
        assert "Gate: blocked" in capsys.readouterr().out  # type: ignore[attr-defined]

    def test_refuses_unapproved_overwrite(self, flac_album: Path, tmp_path: Path) -> None:
        before = {path: path.read_bytes() for path in flac_album.glob("*.flac")}
        log_path = tmp_path / "changes.log"

        with pytest.raises(SystemExit) as error:
            _run_tag(flac_album, _gate_release("Doolin-Dalton"), "--log", str(log_path))

        assert error.value.code == EXIT_REVIEW
        assert {path: path.read_bytes() for path in before} == before
        assert not log_path.exists()

    def test_writes_approved_overwrite(self, flac_album: Path) -> None:
        _run_tag(flac_album, _gate_release("Doolin-Dalton"), "--approve-overwrites")

        assert read_album(flac_album).tracks[0].tags["title"] == "Doolin-Dalton"

    def test_writes_without_approval_when_gate_passes(self, flac_album: Path) -> None:
        _run_tag(flac_album, _gate_release("Desperado"))

        assert read_album(flac_album).tracks[0].tags["musicbrainz_albumid"] == "r-1"


def _isrc_conflict_album(album_dir: Path, *candidate_lists: list[str]) -> MBRelease:
    """Give the first tracks file ISRC USIR19902220 and a release that disagrees."""
    tracks = read_album(album_dir).tracks
    release = _gate_release("Desperado")
    for index, candidates in enumerate(candidate_lists):
        write_tags(tracks[index], [TagChange("isrc", "", "USIR19902220")])
        release.discs[1][index].isrcs = candidates
    return release


class TestTagCommandIsrc:
    def test_blocked_gate_names_the_flags(self, flac_album: Path, capsys: object) -> None:
        release = _isrc_conflict_album(flac_album, ["USDW10110279"])

        with pytest.raises(SystemExit):
            _run_tag(flac_album, release, "--dry-run")

        assert "--isrc keep" in capsys.readouterr().out  # type: ignore[attr-defined]

    def test_keep_writes_other_tags_and_locks_isrc(
        self, flac_album: Path, default_log: Path
    ) -> None:
        release = _isrc_conflict_album(flac_album, ["USDW10110279"])

        _run_tag(flac_album, release, "--isrc", "keep")

        updated = read_album(flac_album).tracks[0]
        assert updated.tags["isrc"] == "USIR19902220"
        assert updated.tags["musicbrainz_albumid"] == "r-1"
        assert updated.locked_fields == ["isrc"]
        assert "resolution=USIR19902220" in default_log.read_text()

    def test_keep_dry_run_passes_gate_without_writing(
        self, flac_album: Path, capsys: object
    ) -> None:
        release = _isrc_conflict_album(flac_album, ["USDW10110279"])
        before = {path: path.read_bytes() for path in flac_album.glob("*.flac")}

        _run_tag(flac_album, release, "--isrc", "keep", "--dry-run")

        assert "Gate: auto" in capsys.readouterr().out  # type: ignore[attr-defined]
        assert {path: path.read_bytes() for path in before} == before

    def test_musicbrainz_replaces_single_candidate(self, flac_album: Path) -> None:
        release = _isrc_conflict_album(flac_album, ["USDW10110279"])

        _run_tag(flac_album, release, "--isrc", "musicbrainz")

        assert read_album(flac_album).tracks[0].tags["isrc"] == "USDW10110279"

    def test_musicbrainz_blocks_on_several_candidates(
        self, flac_album: Path, capsys: object
    ) -> None:
        release = _isrc_conflict_album(flac_album, ["USDW10110279", "USIR19701434"])

        with pytest.raises(SystemExit) as error:
            _run_tag(flac_album, release, "--isrc", "musicbrainz")

        assert error.value.code == EXIT_BLOCKED
        assert "--isrc-code" in capsys.readouterr().out  # type: ignore[attr-defined]

    def test_code_picks_one_candidate(self, flac_album: Path) -> None:
        release = _isrc_conflict_album(flac_album, ["USDW10110279", "USIR19701434"])

        _run_tag(flac_album, release, "--isrc-code", "1=USIR19701434")

        assert read_album(flac_album).tracks[0].tags["isrc"] == "USIR19701434"

    def test_code_accepts_disc_prefix(self, flac_album: Path) -> None:
        release = _isrc_conflict_album(flac_album, ["USDW10110279", "USIR19701434"])

        _run_tag(flac_album, release, "--isrc-code", "1-1=USIR19701434")

        assert read_album(flac_album).tracks[0].tags["isrc"] == "USIR19701434"

    def test_invalid_code_value_exits_without_writing(
        self, flac_album: Path, capsys: object
    ) -> None:
        release = _isrc_conflict_album(flac_album, ["USDW10110279"])
        before = {path: path.read_bytes() for path in flac_album.glob("*.flac")}

        with pytest.raises(SystemExit) as error:
            _run_tag(flac_album, release, "--isrc-code", "1=USXX00000000")

        assert error.value.code == 1
        assert {path: path.read_bytes() for path in before} == before
        assert "USXX00000000" in capsys.readouterr().err  # type: ignore[attr-defined]

    @pytest.mark.parametrize("spec", ["USIR19701434", "x=USIR19701434", "1="])
    def test_malformed_code_is_a_usage_error(self, flac_album: Path, spec: str) -> None:
        with pytest.raises(SystemExit) as error:
            _run_tag(flac_album, _gate_release("Desperado"), "--isrc-code", spec)

        assert error.value.code == 2
