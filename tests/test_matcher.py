from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from music_tagger.matcher import MatchResult, _build_candidate_data, _build_file_data, match_release
from music_tagger.musicbrainz import MBRelease, MBTrack
from music_tagger.tags import AlbumTags, TrackTags


def _make_album() -> AlbumTags:
    tracks = [
        TrackTags(
            path=Path("/music/01 - Desperado.flac"),
            tags={"title": "Desperado", "artist": "Eagles", "albumartist": "Eagles",
                  "album": "Desperado", "tracknumber": "1", "barcode": "075596066624"},
            duration_secs=213.5,
            format="flac",
        ),
        TrackTags(
            path=Path("/music/02 - Twenty-One.flac"),
            tags={"title": "Twenty-One", "artist": "Eagles", "albumartist": "Eagles",
                  "album": "Desperado", "tracknumber": "2"},
            duration_secs=187.3,
            format="flac",
        ),
    ]
    return AlbumTags(directory=Path("/music"), tracks=tracks)


def _make_candidates() -> list[MBRelease]:
    return [
        MBRelease(
            id="release-1",
            title="Desperado",
            date="1973-04-17",
            country="US",
            label="Asylum Records",
            catalognum="SD 5068",
            barcode="075596066624",
            format="CD",
            track_count=2,
            discs={
                1: [
                    MBTrack(number=1, title="Desperado", duration_ms=213000),
                    MBTrack(number=2, title="Twenty-One", duration_ms=187000),
                ]
            },
            artist_id="artist-1",
            release_group_id="rg-1",
        ),
    ]


class TestBuildFileData:
    def test_contains_track_count(self) -> None:
        result = _build_file_data(_make_album())
        assert "Track count: 2" in result

    def test_contains_filename(self) -> None:
        result = _build_file_data(_make_album())
        assert "01 - Desperado.flac" in result

    def test_contains_duration(self) -> None:
        result = _build_file_data(_make_album())
        assert "213.5s" in result

    def test_contains_existing_tags(self) -> None:
        result = _build_file_data(_make_album())
        assert "075596066624" in result


class TestBuildCandidateData:
    def test_contains_release_id(self) -> None:
        result = _build_candidate_data(_make_candidates())
        assert "release-1" in result

    def test_contains_track_titles(self) -> None:
        result = _build_candidate_data(_make_candidates())
        assert "Desperado" in result
        assert "Twenty-One" in result

    def test_contains_duration(self) -> None:
        result = _build_candidate_data(_make_candidates())
        assert "213.0s" in result


class TestMatchRelease:
    def test_returns_match_result(self) -> None:
        tool_block = SimpleNamespace(
            type="tool_use",
            name="select_release",
            input={
                "release_id": "release-1",
                "confidence": "high",
                "reasoning": "Track count and durations match perfectly.",
            },
        )
        mock_response = SimpleNamespace(content=[tool_block])

        with patch("music_tagger.matcher.anthropic") as mock_anthropic:
            mock_client = MagicMock()
            mock_anthropic.Anthropic.return_value = mock_client
            mock_client.messages.create.return_value = mock_response

            result = match_release(_make_album(), _make_candidates())

        assert isinstance(result, MatchResult)
        assert result.release_id == "release-1"
        assert result.confidence == "high"
        assert "durations match" in result.reasoning

    def test_raises_on_no_tool_call(self) -> None:
        text_block = SimpleNamespace(type="text", text="I can't decide.")
        mock_response = SimpleNamespace(content=[text_block])

        with patch("music_tagger.matcher.anthropic") as mock_anthropic:
            mock_client = MagicMock()
            mock_anthropic.Anthropic.return_value = mock_client
            mock_client.messages.create.return_value = mock_response

            import pytest
            with pytest.raises(RuntimeError, match="did not return"):
                match_release(_make_album(), _make_candidates())
