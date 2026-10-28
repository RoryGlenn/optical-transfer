"""Small filesystem adapters for Windows and POSIX hosts."""

from __future__ import annotations

import contextlib
import errno
import os
import stat
import sys
from pathlib import Path
from typing import BinaryIO

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

from .errors import TransferError


def lock_exclusive(stream: BinaryIO) -> None:
    """Take a nonblocking process lock, released when the stream closes.

    Parameters
    ----------
    stream : BinaryIO
        Open lock file. Windows always locks byte zero, even for an empty file.
    """
    if sys.platform == "win32":
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise BlockingIOError("Recovery state is locked.") from error
            raise
    else:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def is_link(info: os.stat_result) -> bool:
    """Identify symlinks and Windows name-surrogate reparse points.

    Ordinary cloud-file reparse points are not name surrogates. The bit mask is
    the Windows IsReparseTagNameSurrogate flag, including junctions and symlinks.
    """
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_reparse_tag", 0) & 0x20000000)


def open_regular(path: Path) -> BinaryIO:
    """Open a regular file in binary mode while preserving link rejection.

    Parameters
    ----------
    path : Path
        File to inspect and open. The caller owns the returned stream.

    Returns
    -------
    BinaryIO
        An open binary stream whose identity matches the inspected path.
    """
    before = path.lstat()
    if is_link(before) or not stat.S_ISREG(before.st_mode):
        raise TransferError(f"Unsupported link or special file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        current = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or is_link(current)
            or not os.path.samestat(before, opened)
            or not os.path.samestat(current, opened)
        ):
            raise TransferError(f"Input changed while opening: {path}")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        raise
