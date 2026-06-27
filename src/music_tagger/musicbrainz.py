"""MusicBrainz API client with rate limiting."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx

BASE_URL = "https://musicbrainz.org/ws/2"
USER_AGENT = "music-tagger/0.1 (alex.public.account@gmail.com)"
RATE_LIMIT_SECS = 1.0


@dataclass
class MBTrack:
    number: int
    title: str
    duration_ms: int | None = None

    @property
    def duration_secs(self) -> float | None:
        return self.duration_ms / 1000.0 if self.duration_ms is not None else None


@dataclass
class MBRelease:
    id: str
    title: str
    date: str = ""
    country: str = ""
    status: str = ""
    label: str = ""
    catalognum: str = ""
    barcode: str = ""
    format: str = ""
    track_count: int = 0
    discs: dict[int, list[MBTrack]] = field(default_factory=dict)
    artist_id: str = ""
    release_group_id: str = ""


class MusicBrainzClient:
    def __init__(self) -> None:
        self._client = httpx.Client(
            base_url=BASE_URL,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=30.0,
        )
        self._last_request: float = 0.0

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < RATE_LIMIT_SECS:
            time.sleep(RATE_LIMIT_SECS - elapsed)
        self._last_request = time.monotonic()

    def search_releases(
        self, artist: str, album: str, format: str = "CD", limit: int = 10
    ) -> list[MBRelease]:
        self._rate_limit()
        query = f'artist:"{artist}" release:"{album}"'
        if format:
            query += f" AND format:{format}"
        resp = self._client.get(
            "/release/", params={"query": query, "fmt": "json", "limit": limit}
        )
        resp.raise_for_status()
        data = resp.json()
        releases = []
        for r in data.get("releases", []):
            media_formats = [
                m.get("format", "") for m in r.get("media", [])
            ]
            label_info = r.get("label-info", [])
            label = label_info[0].get("label", {}).get("name", "") if label_info else ""
            catnum = ""
            for li in label_info:
                if li.get("catalog-number"):
                    catnum = li["catalog-number"]
                    break
            track_count = sum(
                m.get("track-count", 0) for m in r.get("media", [])
            )
            releases.append(
                MBRelease(
                    id=r["id"],
                    title=r.get("title", ""),
                    date=r.get("date", ""),
                    country=r.get("country", ""),
                    status=r.get("status", ""),
                    label=label,
                    catalognum=catnum,
                    barcode=r.get("barcode", ""),
                    format=", ".join(f for f in media_formats if f),
                    track_count=track_count,
                )
            )
        return releases

    def fetch_release(self, release_id: str) -> MBRelease:
        self._rate_limit()
        resp = self._client.get(
            f"/release/{release_id}",
            params={
                "fmt": "json",
                "inc": "recordings+artist-credits+labels+release-groups",
            },
        )
        resp.raise_for_status()
        r = resp.json()

        label_info = r.get("label-info", [])
        label = label_info[0].get("label", {}).get("name", "") if label_info else ""
        catnum = ""
        for li in label_info:
            if li.get("catalog-number"):
                catnum = li["catalog-number"]
                break

        artist_credit = r.get("artist-credit", [])
        artist_id = ""
        if artist_credit:
            artist_obj = artist_credit[0].get("artist", {})
            artist_id = artist_obj.get("id", "")

        release_group = r.get("release-group", {})
        release_group_id = release_group.get("id", "")

        discs: dict[int, list[MBTrack]] = {}
        total_tracks = 0
        media_formats = []
        for medium in r.get("media", []):
            disc_num = medium.get("position", 1)
            media_formats.append(medium.get("format", ""))
            tracks = []
            for t in medium.get("tracks", []):
                recording = t.get("recording", {})
                tracks.append(
                    MBTrack(
                        number=int(t.get("number", 0)),
                        title=t.get("title", recording.get("title", "")),
                        duration_ms=t.get("length") or recording.get("length"),
                    )
                )
            total_tracks += len(tracks)
            discs[disc_num] = tracks

        return MBRelease(
            id=r["id"],
            title=r.get("title", ""),
            date=r.get("date", ""),
            country=r.get("country", ""),
            status=r.get("status", ""),
            label=label,
            catalognum=catnum,
            barcode=r.get("barcode", ""),
            format=", ".join(f for f in media_formats if f),
            track_count=total_tracks,
            discs=discs,
            artist_id=artist_id,
            release_group_id=release_group_id,
        )

    def close(self) -> None:
        self._client.close()
