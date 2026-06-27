"""LLM-assisted release matching via Claude."""

from __future__ import annotations

from dataclasses import dataclass

import anthropic

from .musicbrainz import MBRelease
from .tags import AlbumTags

MODEL = "claude-opus-4-8"

MATCH_TOOL = {
    "name": "select_release",
    "description": "Select the MusicBrainz release that best matches the audio files.",
    "input_schema": {
        "type": "object",
        "properties": {
            "release_id": {
                "type": "string",
                "description": "MusicBrainz release UUID of the best match.",
            },
            "confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"],
                "description": "Confidence in the match.",
            },
            "reasoning": {
                "type": "string",
                "description": "Explanation of why this release was chosen and any tag conflicts noted.",
            },
        },
        "required": ["release_id", "confidence", "reasoning"],
    },
}

SYSTEM_PROMPT = """\
You are a music librarian matching audio files to MusicBrainz releases.

You will receive:
1. File-intrinsic data: track count, audio durations (from the audio stream), and filenames.
2. Existing metadata tags (which may be WRONG — they were typed years ago).
3. MusicBrainz candidate releases with their track listings.

MATCHING RULES (in priority order):
1. Track count MUST match exactly. Reject any candidate with a different track count.
2. Track durations from the audio stream are ground truth. Compare them against \
MusicBrainz durations — a good match has most tracks within 2 seconds. \
Durations that differ by more than 5 seconds are a red flag.
3. Track order (from filenames) should match the candidate's track listing.
4. Use existing tags ONLY as tiebreakers when multiple candidates match equally \
on the above criteria. Note any conflicts between existing tags and the chosen release.

Call the select_release tool with your pick. If no candidate is a credible match, \
pick the closest one with confidence "low" and explain the problems.\
"""


@dataclass
class MatchResult:
    release_id: str
    confidence: str
    reasoning: str


def _build_file_data(album: AlbumTags) -> str:
    lines = [
        f"Artist (from tags, may be wrong): {album.artist}",
        f"Album (from tags, may be wrong): {album.album}",
        f"Track count: {album.track_count}",
        "",
        "Files (ground truth — durations are from the audio stream):",
    ]
    for i, track in enumerate(album.tracks, 1):
        filename = track.path.name
        dur = f"{track.duration_secs:.1f}s" if track.duration_secs else "unknown"
        existing_title = track.tags.get("title", "")
        lines.append(f"  {i}. {filename} | duration: {dur} | tagged title: {existing_title}")

    existing_tags = album.tracks[0].tags if album.tracks else {}
    tag_lines = []
    for field in ("barcode", "catalognumber", "country", "media", "date", "label", "musicbrainz_albumid"):
        val = existing_tags.get(field, "")
        if val:
            tag_lines.append(f"  {field}: {val}")
    if tag_lines:
        lines.append("")
        lines.append("Existing metadata tags (may be wrong — use only as tiebreakers):")
        lines.extend(tag_lines)

    return "\n".join(lines)


def _build_candidate_data(candidates: list[MBRelease]) -> str:
    sections = []
    for c in candidates:
        lines = [
            f"Release: {c.title}",
            f"  ID: {c.id}",
            f"  Date: {c.date}",
            f"  Country: {c.country}",
            f"  Label: {c.label}",
            f"  Catalog#: {c.catalognum}",
            f"  Barcode: {c.barcode}",
            f"  Format: {c.format}",
            f"  Track count: {c.track_count}",
        ]
        for disc_num in sorted(c.discs):
            tracks = c.discs[disc_num]
            if len(c.discs) > 1:
                lines.append(f"  Disc {disc_num}:")
            for t in tracks:
                dur = f"{t.duration_secs:.1f}s" if t.duration_secs is not None else "unknown"
                lines.append(f"    {t.number}. {t.title} ({dur})")
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def match_release(
    album: AlbumTags,
    candidates: list[MBRelease],
) -> MatchResult:
    """Ask Claude to pick the best MusicBrainz release for the given album."""
    file_data = _build_file_data(album)
    candidate_data = _build_candidate_data(candidates)

    user_message = f"## Audio Files\n\n{file_data}\n\n## MusicBrainz Candidates\n\n{candidate_data}"

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        thinking={"type": "adaptive"},
        system=SYSTEM_PROMPT,
        tools=[MATCH_TOOL],
        tool_choice={"type": "tool", "name": "select_release"},
        messages=[{"role": "user", "content": user_message}],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "select_release":
            return MatchResult(
                release_id=block.input["release_id"],
                confidence=block.input["confidence"],
                reasoning=block.input["reasoning"],
            )

    raise RuntimeError("Claude did not return a select_release tool call")
