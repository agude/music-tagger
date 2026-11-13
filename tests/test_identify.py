"""Identifying the disc in the drive before ripping."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from music_tagger.cli.rip import _identify
from music_tagger.discid import _compute_disc_id
from music_tagger.identify import (
    attach_url,
    identify_disc,
    match_medium,
    rip_command,
)
from music_tagger.musicbrainz import (
    DiscTOC,
    MBRelease,
    MediumInfo,
    MusicBrainzClient,
)
from music_tagger.ripper import read_drive_toc

# Disc 1 of Little Feat, Dixie Chicken (2023 deluxe), as whipper logged it.
DISC = DiscTOC(
    disc_id="Q_e8TdUj6JRttcRaQtDkZNqD.XU-",
    offsets=[150, 17822, 31841, 43368, 68382, 81793, 96608, 112780, 133254, 148970],
    leadout=165143,
)
RELEASE = MBRelease(id="rel-1", title="Dixie Chicken", artist="Little Feat", barcode="603497837465")


def _lengths_ms(toc: DiscTOC, shift_ms: int = 0) -> list[int | None]:
    return [round(frames / 75 * 1000) + shift_ms for frames in toc.track_frames()]


def _drifted(toc: DiscTOC, frames: int, disc_id: str = "attached-id") -> DiscTOC:
    return DiscTOC(
        disc_id=disc_id,
        offsets=[toc.offsets[0], *(o + frames for o in toc.offsets[1:])],
        leadout=toc.leadout + frames,
    )


class TestDiscTOC:
    def test_whipper_toc_gives_the_logged_disc_id(self) -> None:
        computed = _compute_disc_id(1, 10, DISC.leadout, DISC.offsets)
        assert computed == DISC.disc_id

    def test_track_frames_end_at_the_leadout(self) -> None:
        frames = DISC.track_frames()
        assert len(frames) == 10
        assert frames[0] == 17822 - 150
        assert frames[-1] == 165143 - 148970

    def test_attach_url_matches_whippers(self) -> None:
        assert attach_url(DISC) == (
            "https://musicbrainz.org/cdtoc/attach?toc=1+10+165143+150+17822+31841+43368"
            "+68382+81793+96608+112780+133254+148970&tracks=10&id=Q_e8TdUj6JRttcRaQtDkZNqD.XU-"
        )

    def test_attach_url_with_release_filters_to_it_in_lowercase(self) -> None:
        url = attach_url(DISC, "14A0146A-F028-40B0-BB52-960FBE54DB31")
        assert url == attach_url(DISC) + (
            "&filter-release.query=14a0146a-f028-40b0-bb52-960fbe54db31"
        )


class TestMatchMedium:
    def test_exact_disc_id(self) -> None:
        medium = MediumInfo(position=1, track_lengths_ms=_lengths_ms(DISC), tocs=[DISC])
        match = match_medium(DISC, RELEASE, medium, 2, "0603497837465")
        assert match is not None
        assert match.exact
        assert match.lengths_match
        assert match.max_drift_frames == 0
        assert match.barcode_match

    def test_other_pressing_reports_drift(self) -> None:
        attached = _drifted(DISC, 14)
        medium = MediumInfo(position=1, track_lengths_ms=_lengths_ms(attached), tocs=[attached])
        match = match_medium(DISC, RELEASE, medium, 2, "")
        assert match is not None
        assert not match.exact
        assert match.lengths_match
        assert match.max_drift_frames == 14
        assert not match.barcode_match

    def test_track_count_mismatch_is_no_match(self) -> None:
        medium = MediumInfo(position=1, track_lengths_ms=[1000] * 9, tocs=[])
        assert match_medium(DISC, RELEASE, medium, 1, "") is None

    def test_lengths_off_by_more_than_2s_do_not_match(self) -> None:
        medium = MediumInfo(position=1, track_lengths_ms=_lengths_ms(DISC, 3000), tocs=[])
        match = match_medium(DISC, RELEASE, medium, 1, "")
        assert match is not None
        assert not match.lengths_match
        assert match.tracks_within_2s == 0
        assert match.max_drift_frames is None

    def test_missing_length_is_not_compared(self) -> None:
        lengths = _lengths_ms(DISC)
        lengths[3] = None
        medium = MediumInfo(position=1, track_lengths_ms=lengths, tocs=[])
        match = match_medium(DISC, RELEASE, medium, 1, "")
        assert match is not None
        assert match.tracks_compared == 9
        assert match.lengths_match


class TestIdentifyDisc:
    def _client(self, *, by_discid: list[MBRelease], by_barcode: list[MBRelease]) -> MagicMock:
        client = MagicMock(spec=MusicBrainzClient)
        client.lookup_discid.return_value = by_discid
        client.search_releases.side_effect = lambda artist, album, barcode=None: (
            by_barcode if barcode else [RELEASE]
        )
        disc2 = MediumInfo(position=2, track_lengths_ms=[60000] * 16, tocs=[])
        attached = _drifted(DISC, 14)
        disc1 = MediumInfo(position=1, track_lengths_ms=_lengths_ms(attached), tocs=[attached])
        client.fetch_media.return_value = [disc1, disc2]
        return client

    def test_mcn_finds_the_release_when_the_disc_id_does_not(self) -> None:
        client = self._client(by_discid=[], by_barcode=[RELEASE])
        result = identify_disc(DISC, client, mcn="0603497837465")
        client.search_releases.assert_called_once_with("", "", barcode="0603497837465")
        best = result.best
        assert best is not None
        assert best.release.id == "rel-1"
        assert (best.position, best.medium_count) == (1, 2)
        assert not best.exact
        assert best.lengths_match
        assert best.barcode_match
        assert rip_command(best, "/dev/cdrom") == (
            "uv run music-tagger rip --device /dev/cdrom --unknown --disc 1 --of 2"
        )

    def test_given_barcode_overrides_the_mcn(self) -> None:
        client = self._client(by_discid=[], by_barcode=[RELEASE])
        result = identify_disc(DISC, client, mcn="0000", barcode="603497837465")
        assert result.barcode == "603497837465"
        client.search_releases.assert_called_once_with("", "", barcode="603497837465")

    def test_releases_found_twice_are_fetched_once(self) -> None:
        client = self._client(by_discid=[RELEASE], by_barcode=[RELEASE])
        identify_disc(DISC, client, mcn="0603497837465", artist="Little Feat", album="Dixie")
        client.fetch_media.assert_called_once_with("rel-1")

    def test_exact_match_ranks_first_and_rips_with_the_release_id(self) -> None:
        other = MBRelease(id="rel-2", title="Dixie Chicken", barcode="603497837465")
        client = MagicMock(spec=MusicBrainzClient)
        client.lookup_discid.return_value = [other]
        client.search_releases.return_value = [RELEASE]
        exact = MediumInfo(position=1, track_lengths_ms=_lengths_ms(DISC), tocs=[DISC])
        near = MediumInfo(position=1, track_lengths_ms=_lengths_ms(DISC), tocs=[])
        client.fetch_media.side_effect = lambda rid: [exact] if rid == "rel-2" else [near]
        result = identify_disc(DISC, client, mcn="0603497837465")
        best = result.best
        assert best is not None
        assert best.release.id == "rel-2"
        assert rip_command(best, "/dev/sr0", "/tmp/rip x") == (
            "uv run music-tagger rip '/tmp/rip x' --device /dev/sr0 --release-id rel-2"
        )

    def test_no_barcode_and_no_title_searches_the_disc_id_only(self) -> None:
        client = self._client(by_discid=[], by_barcode=[])
        result = identify_disc(DISC, client)
        client.search_releases.assert_not_called()
        assert result.best is None


class TestMusicBrainzClient:
    def _client(self, payload: dict[str, object]) -> MusicBrainzClient:
        response = MagicMock(status_code=200)
        response.json.return_value = payload
        client = MusicBrainzClient()
        client._client = MagicMock()
        client._client.get.return_value = response
        client._rate_limit = MagicMock()  # type: ignore[method-assign]
        return client

    def test_fetch_media_reads_lengths_and_disc_ids(self) -> None:
        client = self._client(
            {
                "media": [
                    {
                        "position": 1,
                        "format": "CD",
                        "discs": [{"id": "d1", "offsets": [150, 1000], "sectors": 2000}],
                        "tracks": [{"length": 11000}, {"recording": {"length": 13000}}],
                    },
                    {"position": 2, "format": "DVD-Video", "discs": [], "tracks": [{}]},
                    {"position": 3, "format": None, "tracks": [{"length": None}]},
                ]
            }
        )
        media = client.fetch_media("rel-1")
        assert [m.position for m in media] == [1, 3]
        assert media[0].track_lengths_ms == [11000, 13000]
        assert media[0].tocs == [DiscTOC(disc_id="d1", offsets=[150, 1000], leadout=2000)]
        assert media[1].track_lengths_ms == [None]
        assert media[1].tocs == []


class TestReadDriveToc:
    def _discid(self, mcn: str | None) -> MagicMock:
        tracks = [MagicMock(offset=o) for o in DISC.offsets]
        disc = MagicMock(id=DISC.disc_id, tracks=tracks, sectors=DISC.leadout, mcn=mcn)
        module = MagicMock()
        module.read.return_value = disc
        return module

    def test_reads_toc_and_mcn(self) -> None:
        module = self._discid("0603497837465")
        with patch.dict("sys.modules", {"discid": module}):
            toc, mcn = read_drive_toc("/dev/sr0")
        module.read.assert_called_once_with("/dev/sr0", features=["mcn"])
        assert toc == DISC
        assert mcn == "0603497837465"

    @pytest.mark.parametrize("raw", ["0000000000000", "", None])
    def test_absent_mcn_is_empty(self, raw: str | None) -> None:
        with patch.dict("sys.modules", {"discid": self._discid(raw)}):
            _, mcn = read_drive_toc()
        assert mcn == ""

    def test_missing_discid_package(self) -> None:
        with (
            patch.dict("sys.modules", {"discid": None}),
            pytest.raises(RuntimeError, match="discid"),
        ):
            read_drive_toc()


class TestIdentifyCommand:
    def _run(
        self,
        argv: list[str],
        media: list[MediumInfo],
        capsys: pytest.CaptureFixture[str],
        mcn: str = "0603497837465",
    ) -> tuple[int, str]:
        client = MagicMock(spec=MusicBrainzClient)
        client.lookup_discid.return_value = []
        client.search_releases.return_value = [RELEASE]
        client.fetch_media.return_value = media
        with (
            patch("music_tagger.ripper.read_drive_toc", return_value=(DISC, mcn)),
            patch("music_tagger.cli.rip.MusicBrainzClient", return_value=client),
            pytest.raises(SystemExit) as error,
        ):
            _identify(argv)
        code = error.value.code
        return (code if isinstance(code, int) else 0), capsys.readouterr().out

    def test_other_pressing_routes_to_attach_then_unknown_rip_fallback(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        attached = _drifted(DISC, 14)
        media = [
            MediumInfo(position=1, track_lengths_ms=_lengths_ms(attached), tocs=[attached]),
            MediumInfo(position=2, track_lengths_ms=[1] * 16, tocs=[]),
        ]
        code, out = self._run([], media, capsys)
        assert code == 3
        assert "MCN 0603497837465" in out
        assert "603497837465 (match)" in out
        assert "offsets drift up to 14 frames" in out
        assert "rip --device /dev/cdrom --unknown --disc 1 --of 2" in out
        assert "apply <staging> --release-id rel-1" in out
        assert attach_url(DISC, "rel-1") in out
        assert "pick medium 1 (10 tracks), and confirm the edit" in out
        # Attaching is offered first; the unknown rip is the fallback.
        assert out.index("Attach (preferred)") < out.index("Fallback") < out.index("--unknown")
        assert "run identify again" in out

    def test_barcode_match_with_one_track_off_routes(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        lengths = _lengths_ms(DISC)
        lengths[2] = (lengths[2] or 0) + 5000
        media = [MediumInfo(position=1, track_lengths_ms=lengths, tocs=[])]
        code, out = self._run([], media, capsys)
        assert code == 3
        assert "9/10 (off: 3)" in out
        assert "the barcode and all track lengths but track 3 match" in out

    def test_one_track_off_without_a_barcode_match_does_not_route(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        lengths = _lengths_ms(DISC)
        lengths[2] = (lengths[2] or 0) + 5000
        media = [MediumInfo(position=1, track_lengths_ms=lengths, tocs=[])]
        code, out = self._run(["--album", "Dixie Chicken"], media, capsys, mcn="")
        assert code == 1
        assert "Do not rip yet" in out

    def test_barcode_match_with_two_tracks_off_does_not_route(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        lengths = _lengths_ms(DISC)
        lengths[2] = (lengths[2] or 0) + 5000
        lengths[5] = (lengths[5] or 0) + 5000
        media = [MediumInfo(position=1, track_lengths_ms=lengths, tocs=[])]
        code, _ = self._run([], media, capsys)
        assert code == 1

    def test_musicbrainz_failure_exits_one_without_traceback(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client = MagicMock(spec=MusicBrainzClient)
        client.lookup_discid.side_effect = httpx.ConnectError("connection refused")
        with (
            patch("music_tagger.ripper.read_drive_toc", return_value=(DISC, "")),
            patch("music_tagger.cli.rip.MusicBrainzClient", return_value=client),
            pytest.raises(SystemExit) as error,
        ):
            _identify([])
        assert error.value.code == 1
        assert "MusicBrainz request failed: connection refused" in capsys.readouterr().err
        client.close.assert_called_once()

    def test_exact_match_returns_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        media = [MediumInfo(position=1, track_lengths_ms=_lengths_ms(DISC), tocs=[DISC])]
        with (
            patch("music_tagger.ripper.read_drive_toc", return_value=(DISC, "")),
            patch("music_tagger.cli.rip.MusicBrainzClient") as client_class,
        ):
            client = client_class.return_value
            client.lookup_discid.return_value = [RELEASE]
            client.fetch_media.return_value = media
            _identify(["/tmp/rip-1"])
        out = capsys.readouterr().out
        assert "MCN none" in out
        assert "rip /tmp/rip-1 --device /dev/cdrom --release-id rel-1" in out

    def test_no_match_exits_one(self, capsys: pytest.CaptureFixture[str]) -> None:
        media = [MediumInfo(position=1, track_lengths_ms=_lengths_ms(DISC, 5000), tocs=[])]
        code, out = self._run([], media, capsys)
        assert code == 1
        assert "Do not rip yet" in out

    def test_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        attached = _drifted(DISC, 14)
        media = [MediumInfo(position=1, track_lengths_ms=_lengths_ms(attached), tocs=[attached])]
        code, out = self._run(["--json"], media, capsys)
        data = json.loads(out)
        assert code == 3
        assert data["disc_id"] == DISC.disc_id
        assert data["matches"][0]["max_drift_frames"] == 14
        assert data["rip_command"] == "uv run music-tagger rip --device /dev/cdrom --unknown"

    def test_unreadable_drive_exits_one(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch("music_tagger.ripper.read_drive_toc", side_effect=OSError("no medium")),
            pytest.raises(SystemExit) as error,
        ):
            _identify([])
        assert error.value.code == 1
        assert "no medium" in capsys.readouterr().err

    def test_artist_without_album_is_a_usage_error(self) -> None:
        with pytest.raises(SystemExit) as error:
            _identify(["--artist", "Little Feat"])
        assert error.value.code == 2


class TestReleaseMissing:
    """A barcode that no release has: the CD is not in MusicBrainz yet."""

    OTHER = MBRelease(id="rel-2007", title="Little Feat", date="2007", barcode="821797077064")

    def _run(
        self, release: MBRelease, argv: list[str], capsys: pytest.CaptureFixture[str]
    ) -> tuple[int, str]:
        client = MagicMock(spec=MusicBrainzClient)
        client.lookup_discid.return_value = []
        client.search_releases.side_effect = lambda artist, album, barcode=None: (
            [] if barcode else [release]
        )
        attached = _drifted(DISC, 428)
        client.fetch_media.return_value = [
            MediumInfo(position=1, track_lengths_ms=_lengths_ms(DISC), tocs=[attached])
        ]
        with (
            patch("music_tagger.ripper.read_drive_toc", return_value=(DISC, "0603497808397")),
            patch("music_tagger.cli.rip.MusicBrainzClient", return_value=client),
            pytest.raises(SystemExit) as error,
        ):
            _identify(["--album", "Little Feat", *argv])
        code = error.value.code
        return (code if isinstance(code, int) else 0), capsys.readouterr().out

    def test_lengths_match_under_another_barcode_is_a_missing_release(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out = self._run(self.OTHER, [], capsys)
        assert code == 4
        assert "no MusicBrainz release has barcode 0603497808397" in out
        assert "rel-2007 (2007, barcode 821797077064), is a different release" in out
        assert "Do not attach this disc ID to another release" in out
        assert "filter-release.query" not in out
        assert "rip --device /dev/cdrom --unknown\n" in out
        assert f"mb discid {DISC.disc_id}" in out

    def test_candidate_without_a_barcode_keeps_the_normal_route(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        bare = MBRelease(id="rel-bare", title="Little Feat")
        code, out = self._run(bare, [], capsys)
        assert code == 3
        assert "Attach (preferred)" in out

    def test_json_reports_the_missing_release(self, capsys: pytest.CaptureFixture[str]) -> None:
        code, out = self._run(self.OTHER, ["--json"], capsys)
        data = json.loads(out)
        assert code == 4
        assert data["release_missing"] is True
        assert data["attach_url"] is None
        assert data["rip_command"] == "uv run music-tagger rip --device /dev/cdrom --unknown"


class TestMediumWithoutLengths:
    def test_barcode_and_count_match_routes_to_attach(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client = MagicMock(spec=MusicBrainzClient)
        client.lookup_discid.return_value = []
        client.search_releases.return_value = [RELEASE]
        client.fetch_media.return_value = [
            MediumInfo(position=1, track_lengths_ms=[1000] * 16, tocs=[]),
            MediumInfo(position=2, track_lengths_ms=[None] * 10, tocs=[]),
        ]
        with (
            patch("music_tagger.ripper.read_drive_toc", return_value=(DISC, "0603497837465")),
            patch("music_tagger.cli.rip.MusicBrainzClient", return_value=client),
            pytest.raises(SystemExit) as error,
        ):
            _identify(["/tmp/rip-1"])
        out = capsys.readouterr().out
        assert error.value.code == 3
        assert "barcode and track count match (MusicBrainz has no lengths to compare)" in out
        assert "pick medium 2 (10 tracks)" in out
        assert "rip /tmp/rip-1 --device /dev/cdrom --unknown --disc 2 --of 2" in out

    def test_no_lengths_without_a_barcode_match_does_not_route(self) -> None:
        bare = MBRelease(id="rel-x", title="Little Feat")
        medium = MediumInfo(position=1, track_lengths_ms=[None] * 10, tocs=[])
        match = match_medium(DISC, bare, medium, 1, "0603497837465")
        assert match is not None
        assert not match.routable
