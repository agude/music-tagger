"""Plan and apply lyric sidecar writes: `<audio-stem>.lrc` and `<audio-stem>.txt`.

A plan is made from a fresh inspection (lyrics.inspect_track), so every existing
lyric, embedded or sidecar, is seen before anything is replaced. Planning
changes nothing. apply_plan performs a WRITE plan only. Navidrome reads sidecars
before embedded lyrics, so a stale sidecar would hide the new text; under
replace, the other-type sidecar is removed in the same plan.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .lyrics import LRC_SUFFIX, TXT_SUFFIX, LyricsKind, TrackLyrics, classify

SIDECAR_MODE = 0o644
# Kinds that hold lyric content. An empty sidecar holds none and is overwritten
# without replace; a malformed one holds content the operator must decide on.
_CONTENT_KINDS = frozenset({LyricsKind.SYNCHRONIZED, LyricsKind.PLAIN, LyricsKind.MALFORMED})


class WriteStatus(StrEnum):
    WRITE = "write"  # changes files: writes the target, removes a stale sidecar, or both
    UNCHANGED = "unchanged"  # the target already holds exactly this text
    PRESERVED = "preserved"  # existing lyrics are kept; replace would change them
    NO_TEXT = "no_text"  # no lyric text, such as an instrumental track; no file created
    REFUSED = "refused"  # invalid text, or existing lyrics could not be inventoried


@dataclass(frozen=True)
class SidecarPlan:
    status: WriteStatus
    reason: str
    target: Path | None = None
    content: bytes | None = None  # None when the target is not written
    remove: tuple[Path, ...] = ()  # stale other-type sidecars; only under replace
    replaces: bool = False  # True when the write overwrites existing lyric content


def _encode(text: str, kind: LyricsKind) -> bytes:
    """Return the UTF-8 bytes to write, or raise ValueError if the text does not fit.

    Text is validated against its kind before any byte is written, so a lyric
    that would not read back as the same kind is never saved.
    """
    if kind not in (LyricsKind.SYNCHRONIZED, LyricsKind.PLAIN):
        raise ValueError(f"only synchronized or plain text can be written, not {kind.value}")
    if classify(text, expect_timed=kind is LyricsKind.SYNCHRONIZED) != kind:
        raise ValueError(f"text does not read back as {kind.value}")
    normalized = text.rstrip("\r\n") + "\n"
    try:
        return normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"text is not valid UTF-8: {exc}") from exc


def plan_sidecar_write(
    track: TrackLyrics, kind: LyricsKind | None, text: str, *, replace: bool
) -> SidecarPlan:
    """Decide what to write for one track. Read-only.

    kind is the chosen lyric's kind, or None when there is no text to write.
    replace authorizes overwriting existing lyrics and removing a stale sidecar.
    """
    if track.errors:
        return SidecarPlan(
            WriteStatus.REFUSED,
            "could not read every lyric source: " + "; ".join(track.errors),
        )
    if kind is None:
        return SidecarPlan(WriteStatus.NO_TEXT, "no lyric text to write")
    try:
        content = _encode(text, kind)
    except ValueError as exc:
        return SidecarPlan(WriteStatus.REFUSED, str(exc))

    if kind is LyricsKind.SYNCHRONIZED:
        target = track.path.with_suffix(LRC_SUFFIX)
        other = track.path.with_suffix(TXT_SUFFIX)
    else:
        target = track.path.with_suffix(TXT_SUFFIX)
        other = track.path.with_suffix(LRC_SUFFIX)
    stale = (other,) if replace and other.is_file() else ()

    if target.is_file() and target.read_bytes() == content:
        if stale:
            return SidecarPlan(
                WriteStatus.WRITE,
                f"{target.name} already holds this text; remove stale {other.name}",
                target=target,
                remove=stale,
            )
        return SidecarPlan(
            WriteStatus.UNCHANGED, f"{target.name} already holds this text", target=target
        )

    has_content = any(entry.kind in _CONTENT_KINDS for entry in track.entries)
    if has_content and not replace:
        return SidecarPlan(
            WriteStatus.PRESERVED,
            "existing lyrics are kept; pass --replace to replace them",
            target=target,
        )

    reason = f"new file {target.name}"
    if has_content:
        reason = f"replace existing lyrics with {target.name}"
    if stale:
        reason += f"; remove stale {other.name}"
    return SidecarPlan(
        WriteStatus.WRITE,
        reason,
        target=target,
        content=content,
        remove=stale,
        replaces=has_content,
    )


def apply_plan(plan: SidecarPlan) -> None:
    """Carry out a WRITE plan: write the target atomically, then remove stale sidecars.

    Any other status raises ValueError, so nothing is written from a plan that
    was refused or preserved.
    """
    if plan.status is not WriteStatus.WRITE:
        raise ValueError(f"cannot apply a {plan.status.value} plan")
    if plan.content is not None:
        if plan.target is None:
            raise ValueError("write plan has content but no target")
        _atomic_write(plan.target, plan.content)
    for path in plan.remove:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


def _atomic_write(path: Path, data: bytes) -> None:
    """Write through a temporary file in the same directory, then rename it over path.

    The rename is atomic on the same filesystem, so an interrupted write leaves
    the old file whole. A failed write removes its temporary file.
    """
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, SIDECAR_MODE)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise
