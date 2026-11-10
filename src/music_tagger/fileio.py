"""Atomic file writes: a reader sees the old file or the new one, never a partial one."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


def _default_mode() -> int:
    """The permissions a plain file write creates: 0666 less the umask."""
    mask = os.umask(0)
    os.umask(mask)
    return 0o666 & ~mask


def write_temp_file(path: Path, data: bytes, *, mode: int | None = None) -> Path:
    """Write data, fsynced, to a unique temporary file beside path, and return its name.

    The caller renames it into place. The mode is `mode` when given; otherwise the
    existing file's mode, so an in-place write keeps the permissions it had, or the
    plain-write default for a new file. A failed write removes the temporary file.
    """
    if mode is None:
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else _default_mode()
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # mkstemp creates 0600, so set the mode explicitly.
        os.chmod(temp, mode)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return temp


def atomic_write_bytes(path: Path, data: bytes, *, mode: int | None = None) -> None:
    """Replace path with data in one rename. A failed write leaves the old file whole.

    A symlink is followed, so the link keeps pointing at the new content, as an
    in-place write would.
    """
    target = path.resolve()
    temp = write_temp_file(target, data, mode=mode)
    try:
        os.replace(temp, target)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def atomic_write_text(
    path: Path,
    text: str,
    *,
    encoding: str = "utf-8",
    errors: str = "strict",
    mode: int | None = None,
) -> None:
    """atomic_write_bytes for text, encoded with the given codec and error handler."""
    atomic_write_bytes(path, text.encode(encoding, errors), mode=mode)
