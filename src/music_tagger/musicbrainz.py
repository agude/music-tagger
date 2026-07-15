"""MusicBrainz API client with rate limiting."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

BASE_URL = "https://musicbrainz.org/ws/2"
USER_AGENT = "music-tagger/0.1 (alex.public.account@gmail.com)"
RATE_LIMIT_SECS = 1.0


@dataclass
class MBTrack:
    number: int
    title: str
    duration_ms: int | None = None
    artist: str = ""
    artist_id: str = ""
    recording_id: str = ""
    track_id: str = ""
    isrc: str = ""
    work_id: str = ""
    composer: str = ""
    composer_id: str = ""
    lyricist: str = ""
    lyricist_id: str = ""
    producer: str = ""
    producer_id: str = ""
    engineer: str = ""
    engineer_id: str = ""
    mixer: str = ""
    mixer_id: str = ""
    conductor: str = ""
    conductor_id: str = ""
    remixer: str = ""
    remixer_id: str = ""
    performers: str = ""
    performer_ids: str = ""

    @property
    def duration_secs(self) -> float | None:
        return self.duration_ms / 1000.0 if self.duration_ms is not None else None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"number": self.number, "title": self.title}
        if self.duration_ms is not None:
            d["duration_ms"] = self.duration_ms
        for attr in (
            "artist", "artist_id",
            "recording_id", "track_id", "isrc", "work_id",
            "composer", "composer_id", "lyricist", "lyricist_id",
            "producer", "producer_id", "engineer", "engineer_id",
            "mixer", "mixer_id", "conductor", "conductor_id",
            "remixer", "remixer_id", "performers", "performer_ids",
        ):
            val = getattr(self, attr)
            if val:
                d[attr] = val
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MBTrack:
        return cls(
            number=data["number"],
            title=data["title"],
            duration_ms=data.get("duration_ms"),
            artist=data.get("artist", ""),
            artist_id=data.get("artist_id", ""),
            recording_id=data.get("recording_id", ""),
            track_id=data.get("track_id", ""),
            isrc=data.get("isrc", ""),
            work_id=data.get("work_id", ""),
            composer=data.get("composer", ""),
            composer_id=data.get("composer_id", ""),
            lyricist=data.get("lyricist", ""),
            lyricist_id=data.get("lyricist_id", ""),
            producer=data.get("producer", ""),
            producer_id=data.get("producer_id", ""),
            engineer=data.get("engineer", ""),
            engineer_id=data.get("engineer_id", ""),
            mixer=data.get("mixer", ""),
            mixer_id=data.get("mixer_id", ""),
            conductor=data.get("conductor", ""),
            conductor_id=data.get("conductor_id", ""),
            remixer=data.get("remixer", ""),
            remixer_id=data.get("remixer_id", ""),
            performers=data.get("performers", ""),
            performer_ids=data.get("performer_ids", ""),
        )


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
    artist: str = ""
    artist_id: str = ""
    release_group_id: str = ""
    release_group_type: str = ""
    secondary_types: list[str] = field(default_factory=list)
    first_release_date: str = ""
    asin: str = ""
    script: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "date": self.date,
            "country": self.country,
            "status": self.status,
            "label": self.label,
            "catalognum": self.catalognum,
            "barcode": self.barcode,
            "format": self.format,
            "track_count": self.track_count,
        }
        if self.discs:
            d["discs"] = {
                str(k): [t.to_dict() for t in v] for k, v in self.discs.items()
            }
        for attr in (
            "artist", "artist_id", "release_group_id", "release_group_type",
            "first_release_date", "asin", "script",
        ):
            val = getattr(self, attr)
            if val:
                d[attr] = val
        if self.secondary_types:
            d["secondary_types"] = list(self.secondary_types)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MBRelease:
        discs: dict[int, list[MBTrack]] = {}
        for k, v in data.get("discs", {}).items():
            discs[int(k)] = [MBTrack.from_dict(t) for t in v]
        return cls(
            id=data["id"],
            title=data.get("title", ""),
            date=data.get("date", ""),
            country=data.get("country", ""),
            status=data.get("status", ""),
            label=data.get("label", ""),
            catalognum=data.get("catalognum", ""),
            barcode=data.get("barcode", ""),
            format=data.get("format", ""),
            track_count=data.get("track_count", 0),
            discs=discs,
            artist=data.get("artist", ""),
            artist_id=data.get("artist_id", ""),
            release_group_id=data.get("release_group_id", ""),
            release_group_type=data.get("release_group_type", ""),
            secondary_types=data.get("secondary_types", []),
            first_release_date=data.get("first_release_date", ""),
            asin=data.get("asin", ""),
            script=data.get("script", ""),
        )


def _build_artist_credit(credit_list: list[dict]) -> str:
    parts = []
    for credit in credit_list:
        name = credit.get("name", credit.get("artist", {}).get("name", ""))
        joinphrase = credit.get("joinphrase", "")
        parts.append(name + joinphrase)
    return "".join(parts)


_CREDIT_ROLES = {
    "producer": "producer",
    "engineer": "engineer",
    "mix": "mixer",
    "conductor": "conductor",
    "remixer": "remixer",
}


def _parse_recording_credits(recording: dict) -> dict[str, str]:
    """Extract personnel credits from recording relationships.

    Returns a flat dict of field_name -> value (names joined with "; ").
    """
    # role -> {artist_id: artist_name}
    role_artists: dict[str, dict[str, str]] = {}
    # performer -> {artist_id: (name, [attributes])}
    performer_map: dict[str, tuple[str, list[str]]] = {}
    work_id = ""

    for rel in recording.get("relations", []):
        target_type = rel.get("target-type", "")
        rtype = rel.get("type", "")
        attrs = rel.get("attributes", [])

        if target_type == "artist":
            artist = rel.get("artist", {})
            name = artist.get("name", "")
            aid = artist.get("id", "")
            if not name:
                continue

            if rtype in _CREDIT_ROLES:
                role = _CREDIT_ROLES[rtype]
                role_artists.setdefault(role, {})[aid] = name
            elif rtype in ("instrument", "vocal", "performer"):
                if aid in performer_map:
                    performer_map[aid][1].extend(attrs)
                else:
                    performer_map[aid] = (name, list(attrs))

        elif target_type == "work" and rtype == "performance":
            work = rel.get("work", {})
            if work.get("id"):
                work_id = work["id"]
            for wr in work.get("relations", []):
                if wr.get("target-type") != "artist":
                    continue
                wa = wr.get("artist", {})
                wname = wa.get("name", "")
                waid = wa.get("id", "")
                wr_type = wr.get("type", "")
                if wr_type == "composer" and wname:
                    role_artists.setdefault("composer", {})[waid] = wname
                elif wr_type == "lyricist" and wname:
                    role_artists.setdefault("lyricist", {})[waid] = wname

    result: dict[str, str] = {}

    for role, artists in role_artists.items():
        names = list(artists.values())
        ids = list(artists.keys())
        result[role] = "; ".join(names)
        result[f"{role}_id"] = "; ".join(ids)

    if performer_map:
        perf_names = []
        perf_ids = []
        for aid, (name, attrs) in performer_map.items():
            if attrs:
                perf_names.append(f"{name} ({', '.join(attrs)})")
            else:
                perf_names.append(name)
            perf_ids.append(aid)
        result["performers"] = "; ".join(perf_names)
        result["performer_ids"] = "; ".join(perf_ids)

    if work_id:
        result["work_id"] = work_id

    return result


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
        return [self._parse_search_release(r) for r in data.get("releases", [])]

    def fetch_release(self, release_id: str) -> MBRelease:
        self._rate_limit()
        resp = self._client.get(
            f"/release/{release_id}",
            params={
                "fmt": "json",
                "inc": (
                    "recordings+artist-credits+labels+release-groups"
                    "+isrcs+recording-level-rels+work-level-rels"
                    "+work-rels+artist-rels"
                ),
            },
        )
        resp.raise_for_status()
        r = resp.json()

        label_info = r.get("label-info", [])
        label = (label_info[0].get("label") or {}).get("name", "") if label_info else ""
        catnum = ""
        for li in label_info:
            if li.get("catalog-number"):
                catnum = li["catalog-number"]
                break

        artist_credit = r.get("artist-credit", [])
        artist_name = _build_artist_credit(artist_credit)
        artist_id = ""
        if artist_credit:
            artist_obj = artist_credit[0].get("artist", {})
            artist_id = artist_obj.get("id", "")

        release_group = r.get("release-group", {})
        release_group_id = release_group.get("id", "")
        release_group_type = release_group.get("primary-type", "")
        secondary_types = release_group.get("secondary-types", [])
        first_release_date = release_group.get("first-release-date", "")

        text_rep = r.get("text-representation", {})
        script = text_rep.get("script", "")

        discs: dict[int, list[MBTrack]] = {}
        total_tracks = 0
        media_formats = []
        for medium in r.get("media", []):
            disc_num = medium.get("position", 1)
            media_formats.append(medium.get("format", ""))
            tracks = []
            for t in medium.get("tracks", []):
                recording = t.get("recording", {})
                track_artist_credit = (
                    t.get("artist-credit")
                    or recording.get("artist-credit")
                    or []
                )
                track_artist_name = _build_artist_credit(track_artist_credit)
                track_artist_id = ""
                if track_artist_credit:
                    track_artist_id = (
                        track_artist_credit[0].get("artist", {}).get("id", "")
                    )

                isrcs = recording.get("isrcs", [])
                credits = _parse_recording_credits(recording)

                raw_number = t.get("number", 0)
                try:
                    track_number = int(raw_number)
                except (ValueError, TypeError):
                    track_number = t.get("position", 0)

                tracks.append(
                    MBTrack(
                        number=track_number,
                        title=t.get("title", recording.get("title", "")),
                        duration_ms=t.get("length") or recording.get("length"),
                        artist=track_artist_name,
                        artist_id=track_artist_id,
                        recording_id=recording.get("id", ""),
                        track_id=t.get("id", ""),
                        isrc=isrcs[0] if isrcs else "",
                        work_id=credits.get("work_id", ""),
                        composer=credits.get("composer", ""),
                        composer_id=credits.get("composer_id", ""),
                        lyricist=credits.get("lyricist", ""),
                        lyricist_id=credits.get("lyricist_id", ""),
                        producer=credits.get("producer", ""),
                        producer_id=credits.get("producer_id", ""),
                        engineer=credits.get("engineer", ""),
                        engineer_id=credits.get("engineer_id", ""),
                        mixer=credits.get("mixer", ""),
                        mixer_id=credits.get("mixer_id", ""),
                        conductor=credits.get("conductor", ""),
                        conductor_id=credits.get("conductor_id", ""),
                        remixer=credits.get("remixer", ""),
                        remixer_id=credits.get("remixer_id", ""),
                        performers=credits.get("performers", ""),
                        performer_ids=credits.get("performer_ids", ""),
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
            artist=artist_name,
            artist_id=artist_id,
            release_group_id=release_group_id,
            release_group_type=release_group_type,
            secondary_types=secondary_types,
            first_release_date=first_release_date,
            asin=r.get("asin") or "",
            script=script,
        )

    def lookup_discid(self, discid: str) -> list[MBRelease]:
        self._rate_limit()
        resp = self._client.get(
            f"/discid/{discid}",
            params={
                "fmt": "json",
                "inc": "recordings+artist-credits+labels+release-groups",
            },
        )
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        data = resp.json()
        releases = data.get("releases", [])
        return [self._parse_search_release(r) for r in releases]

    def lookup_toc(self, toc: str) -> list[MBRelease]:
        self._rate_limit()
        resp = self._client.get(
            "/discid/-",
            params={
                "toc": toc,
                "fmt": "json",
                "inc": "recordings+artist-credits+labels+release-groups",
            },
        )
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        data = resp.json()
        releases = data.get("releases", [])
        return [self._parse_search_release(r) for r in releases]

    @staticmethod
    def _parse_search_release(r: dict) -> MBRelease:
        media_formats = [m.get("format", "") for m in r.get("media", [])]
        label_info = r.get("label-info", [])
        label = (label_info[0].get("label") or {}).get("name", "") if label_info else ""
        catnum = ""
        for li in label_info:
            if li.get("catalog-number"):
                catnum = li["catalog-number"]
                break
        track_count = sum(m.get("track-count", 0) for m in r.get("media", []))
        artist_credit = r.get("artist-credit", [])
        artist_name = _build_artist_credit(artist_credit)
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
            track_count=track_count,
            artist=artist_name,
        )

    def close(self) -> None:
        self._client.close()
