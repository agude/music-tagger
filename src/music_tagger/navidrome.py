"""Navidrome / Subsonic API client."""

from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass
from typing import Any

import httpx

SUBSONIC_API_VERSION = "1.16.1"
CLIENT_NAME = "music-tagger"


def _get_config() -> tuple[str, str, str]:
    url = os.environ.get("NAVIDROME_URL", "")
    user = os.environ.get("NAVIDROME_USER", "")
    password = os.environ.get("NAVIDROME_PASSWORD", "")
    if not url or not user or not password:
        raise RuntimeError(
            "Set NAVIDROME_URL, NAVIDROME_USER, and NAVIDROME_PASSWORD environment variables."
        )
    return url, user, password


def _auth_params(user: str, password: str) -> dict[str, str]:
    salt = secrets.token_hex(8)
    token = hashlib.md5((password + salt).encode()).hexdigest()
    return {
        "u": user,
        "t": token,
        "s": salt,
        "v": SUBSONIC_API_VERSION,
        "c": CLIENT_NAME,
        "f": "json",
    }


@dataclass
class SongRating:
    """A song's rating and starred status from Navidrome."""

    id: str
    path: str
    title: str
    artist: str
    album: str
    rating: int = 0
    starred: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "path": self.path,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
        }
        if self.rating:
            d["rating"] = self.rating
        if self.starred:
            d["starred"] = self.starred
        return d

    @classmethod
    def from_native(cls, data: dict[str, Any]) -> SongRating:
        return cls(
            id=data.get("id", ""),
            path=data.get("path", ""),
            title=data.get("title", ""),
            artist=data.get("artist", ""),
            album=data.get("album", ""),
            rating=data.get("rating", 0),
            starred=data.get("starredAt", "") or "",
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SongRating:
        return cls(
            id=data.get("id", ""),
            path=data.get("path", ""),
            title=data.get("title", ""),
            artist=data.get("artist", ""),
            album=data.get("album", ""),
            rating=data.get("rating", 0),
            starred=data.get("starred", ""),
        )


class NavidromeClient:
    def __init__(
        self,
        url: str | None = None,
        user: str | None = None,
        password: str | None = None,
    ) -> None:
        if url and user and password:
            self._url = url.rstrip("/")
            self._user = user
            self._password = password
        else:
            self._url, self._user, self._password = _get_config()
        self._client = httpx.Client(timeout=30.0)

    def _request(self, endpoint: str, **params: Any) -> dict[str, Any]:
        all_params: dict[str, str] = _auth_params(self._user, self._password)
        for k, v in params.items():
            all_params[k] = str(v)
        resp = self._client.get(
            f"{self._url}/rest/{endpoint}",
            params=all_params,
        )
        resp.raise_for_status()
        data = resp.json()
        subsonic: dict[str, Any] = data.get("subsonic-response", {})
        if subsonic.get("status") != "ok":
            error = subsonic.get("error", {})
            raise RuntimeError(
                f"Subsonic error {error.get('code', '?')}: {error.get('message', 'unknown')}"
            )
        return subsonic

    def start_scan(self) -> dict[str, Any]:
        data = self._request("startScan")
        return dict(data.get("scanStatus", {}))

    def _native_headers(self) -> dict[str, str]:
        resp = self._client.post(
            f"{self._url}/auth/login",
            json={"username": self._user, "password": self._password},
        )
        resp.raise_for_status()
        return {"x-nd-authorization": f"Bearer {resp.json()['token']}"}

    def get_all_ratings(self, page_size: int = 500) -> list[SongRating]:
        """Get all rated and/or starred songs with real filesystem paths.

        Uses the Navidrome native API (not the Subsonic API) because the
        Subsonic path field is a virtual path constructed from tags, not
        the real filesystem path.
        """
        headers = self._native_headers()
        results: list[SongRating] = []
        offset = 0

        while True:
            resp = self._client.get(
                f"{self._url}/api/song",
                headers=headers,
                params={
                    "_sort": "path",
                    "_order": "ASC",
                    "_start": str(offset),
                    "_end": str(offset + page_size),
                },
            )
            resp.raise_for_status()
            songs = resp.json()
            if not songs:
                break
            for s in songs:
                if s.get("rating", 0) or s.get("starredAt"):
                    results.append(SongRating.from_native(s))
            if len(songs) < page_size:
                break
            offset += len(songs)

        return results

    def set_rating(self, song_id: str, rating: int) -> None:
        self._request("setRating", id=song_id, rating=rating)

    def star(self, song_id: str) -> None:
        self._request("star", id=song_id)

    def unstar(self, song_id: str) -> None:
        self._request("unstar", id=song_id)

    def close(self) -> None:
        self._client.close()
