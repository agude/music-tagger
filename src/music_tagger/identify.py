"""Identify the MusicBrainz release and medium of the disc in the drive, before ripping."""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field

from .musicbrainz import DiscTOC, MBRelease, MediumInfo, MusicBrainzClient, barcode_variants

FRAMES_PER_SECOND = 75
MATCH_TOLERANCE_SECS = 2.0
# Releases fetched in detail, at one MusicBrainz request each.
MAX_CANDIDATES = 10


@dataclass
class MediumMatch:
    """How well the disc fits one medium of a release."""

    release: MBRelease
    position: int
    medium_count: int
    tracks_within_2s: int
    tracks_compared: int
    max_deviation_secs: float
    exact: bool  # the disc's ID is attached to this medium
    max_drift_frames: int | None  # vs the closest attached TOC; None when none has this track count
    barcode_match: bool
    # Track numbers whose length is more than 2 s from the medium's.
    outliers: list[int] = field(default_factory=list)

    @property
    def lengths_match(self) -> bool:
        return self.tracks_compared > 0 and not self.outliers

    @property
    def routable(self) -> bool:
        """The disc can be ripped as this medium.

        Its disc ID is attached; or every track length matches; or the barcode
        matches and at most one track (a remastered or edited one) is off.
        """
        if self.exact or self.lengths_match:
            return True
        return self.barcode_match and self.tracks_compared > 1 and len(self.outliers) <= 1


@dataclass
class Identification:
    disc: DiscTOC
    mcn: str
    barcode: str
    matches: list[MediumMatch] = field(default_factory=list)

    @property
    def best(self) -> MediumMatch | None:
        return self.matches[0] if self.matches else None


def attach_url(disc: DiscTOC, release_id: str | None = None) -> str:
    """The MusicBrainz page that attaches this disc ID to a medium.

    With `release_id`, the page lists only that release's mediums: the user picks
    the medium and confirms the edit. MusicBrainz matches only a lowercase MBID
    there. A link straight to the confirmation needs the medium's database row
    ID, which the web service does not expose.
    """
    count = len(disc.offsets)
    toc = "+".join(str(value) for value in [1, count, disc.leadout, *disc.offsets])
    url = f"https://musicbrainz.org/cdtoc/attach?toc={toc}&tracks={count}&id={disc.disc_id}"
    if release_id:
        url += f"&filter-release.query={release_id.lower()}"
    return url


def match_medium(
    disc: DiscTOC, release: MBRelease, medium: MediumInfo, medium_count: int, barcode: str
) -> MediumMatch | None:
    """Compare the disc with one medium, or None when the track counts differ."""
    disc_frames = disc.track_frames()
    if len(medium.track_lengths_ms) != len(disc_frames):
        return None
    by_track = {
        number: abs(frames / FRAMES_PER_SECOND - length / 1000)
        for number, (frames, length) in enumerate(
            zip(disc_frames, medium.track_lengths_ms, strict=True), 1
        )
        if length is not None
    }
    deviations = list(by_track.values())
    drifts = [
        max(abs(a - b) for a, b in zip(disc.offsets, toc.offsets, strict=True))
        for toc in medium.tocs
        if len(toc.offsets) == len(disc.offsets)
    ]
    wanted = set(barcode_variants(barcode)) if barcode else set()
    return MediumMatch(
        release=release,
        position=medium.position,
        medium_count=medium_count,
        tracks_within_2s=sum(1 for d in deviations if d <= MATCH_TOLERANCE_SECS),
        tracks_compared=len(deviations),
        max_deviation_secs=max(deviations, default=0.0),
        exact=any(toc.disc_id == disc.disc_id for toc in medium.tocs),
        max_drift_frames=min(drifts) if drifts else None,
        barcode_match=bool(release.barcode) and release.barcode in wanted,
        outliers=[n for n, d in by_track.items() if d > MATCH_TOLERANCE_SECS],
    )


def _rank_key(match: MediumMatch) -> tuple[bool, bool, bool, bool, int, float]:
    return (
        not match.exact,
        not match.lengths_match,
        not match.routable,
        not match.barcode_match,
        -match.tracks_within_2s,
        match.max_deviation_secs,
    )


def identify_disc(
    disc: DiscTOC,
    client: MusicBrainzClient,
    *,
    mcn: str = "",
    barcode: str | None = None,
    artist: str | None = None,
    album: str | None = None,
) -> Identification:
    """Find releases for the disc and rank each medium that has its track count.

    Candidates come from the disc ID, then the barcode (the given one, else the
    disc's MCN), then artist and album. Each candidate costs one request.
    """
    chosen_barcode = barcode or mcn
    result = Identification(disc=disc, mcn=mcn, barcode=chosen_barcode)

    found: dict[str, MBRelease] = {}
    for release in client.lookup_discid(disc.disc_id):
        found.setdefault(release.id, release)
    if chosen_barcode:
        for release in client.search_releases("", "", barcode=chosen_barcode):
            found.setdefault(release.id, release)
    if album:
        for release in client.search_releases(artist or "", album):
            found.setdefault(release.id, release)

    for release in list(found.values())[:MAX_CANDIDATES]:
        media = client.fetch_media(release.id)
        for medium in media:
            match = match_medium(disc, release, medium, len(media), chosen_barcode)
            if match is not None:
                result.matches.append(match)
    result.matches.sort(key=_rank_key)
    return result


def rip_command(match: MediumMatch, device: str, staging: str | None = None) -> str:
    """The rip command for the disc, given the medium it matches.

    `staging` is the directory earlier discs of the album were ripped into.
    """
    command = "uv run music-tagger rip"
    if staging:
        command += f" {shlex.quote(staging)}"
    command += f" --device {device}"
    if match.exact:
        return f"{command} --release-id {match.release.id}"
    command += " --unknown"
    if match.medium_count > 1:
        command += f" --disc {match.position} --of {match.medium_count}"
    return command
