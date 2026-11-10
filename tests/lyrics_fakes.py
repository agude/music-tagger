"""Fake LRCLIB server and client factories shared by the lyric command tests."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from music_tagger.cli import lyrics_cmd
from music_tagger.lrclib import LrclibClient
from music_tagger.tags import read_album

Responder = Callable[[httpx.Request], httpx.Response]


def use_client(monkeypatch: pytest.MonkeyPatch, responder: Responder) -> None:
    def factory() -> LrclibClient:
        return LrclibClient(
            transport=httpx.MockTransport(responder), sleep=lambda _s: None, min_interval=0.0
        )

    monkeypatch.setattr(lyrics_cmd, "LrclibClient", factory)


def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def factory() -> LrclibClient:
        raise AssertionError("this mode must not construct an LRCLIB client")

    monkeypatch.setattr(lyrics_cmd, "LrclibClient", factory)


def record(record_id: int, title: str, album: str, **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": record_id,
        "name": title,
        "trackName": title,
        "artistName": "Eagles",
        "albumName": album,
        "duration": 0.5,
        "instrumental": False,
        "plainLyrics": "Plain line",
        "syncedLyrics": "[00:01.00]Timed line",
        "hasWordSync": False,
        "lyricsfile": "",
    }
    data.update(overrides)
    return data


def catalog(album: Path, **overrides: Any) -> dict[str, list[dict[str, Any]]]:
    """One matching record per track, keyed by track title. Record ids are 101, 102, ..."""
    result: dict[str, list[dict[str, Any]]] = {}
    for number, track in enumerate(read_album(album).tracks, start=1):
        title = track.tags["title"]
        result[title] = [record(100 + number, title, track.tags["album"], **overrides)]
    return result


def responder(records_by_title: dict[str, list[dict[str, Any]]]) -> Responder:
    by_id = {r["id"]: r for records in records_by_title.values() for r in records}

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/search":
            title = request.url.params.get("track_name", "")
            return httpx.Response(200, json=records_by_title.get(title, []))
        if request.url.path == "/api/get":
            # Exact lookup by title, artist, and album, as LRCLIB matches it.
            params = request.url.params
            for r in by_id.values():
                if (
                    r["trackName"] == params["track_name"]
                    and r["artistName"] == params["artist_name"]
                    and r["albumName"] == params["album_name"]
                ):
                    return httpx.Response(200, json=r)
            return httpx.Response(404, json={"name": "TrackNotFound"})
        record_id = int(request.url.path.rsplit("/", 1)[1])
        if record_id in by_id:
            return httpx.Response(200, json=by_id[record_id])
        return httpx.Response(404, json={"name": "TrackNotFound"})

    return respond
