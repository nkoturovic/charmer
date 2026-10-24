"""POSIX filesystem, lock and exec primitives (Linux/POSIX only).

Stdlib only, no charmer imports, no policy: callers own policy errors,
messages, transaction order and recovery. Atomic replacement distinguishes
committed failures; nothing here retries an operation, sleeps or falls back.
"""

from __future__ import annotations

import errno
import fcntl
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

# Group/other permission bits that make a private file or directory shared.
SHARED_MODE_BITS = 0o077


def fsync_directory(path: Path) -> None:
    """Durably record a directory's entries; raises ``OSError`` on failure.

    The raising variant of ``state._fsync_directory``. Callers
    with best-effort semantics keep their own copies (``lineup_log``).
    """
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def replace_file(source: str | Path, target: str | Path) -> None:
    """One atomic same-filesystem rename over ``target``; never retried."""
    os.replace(source, target)


class CommittedReplaceError(OSError):
    """The target was replaced, but directory durability was not confirmed."""


def atomic_replace(path: Path | str, data: bytes, *, mode: int, durable: bool = True) -> None:
    """Replace a file through an exclusive same-directory temporary file.

    The caller owns directory policy and the target mode. Symlink targets
    are refused, including a link noticed just before replacement. Only a
    temporary file created by this call is cleaned up on failure. With
    ``durable=False`` neither file nor directory is fsynced; otherwise a
    post-replace sync failure is distinguished by ``CommittedReplaceError``.
    """
    target = Path(path)
    if target.is_symlink():
        raise OSError(errno.ELOOP, "refusing to replace a symlink", str(target))
    descriptor, temporary = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None  # the handle now owns the descriptor
            os.fchmod(handle.fileno(), mode)
            handle.write(data)
            handle.flush()
            if durable:
                os.fsync(handle.fileno())
        if target.is_symlink():
            raise OSError(errno.ELOOP, "refusing to replace a symlink", str(target))
        replace_file(temporary, target)
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    if durable:
        try:
            fsync_directory(target.parent)
        except OSError as exc:
            raise CommittedReplaceError(
                exc.errno or errno.EIO,
                "file was written; durability unconfirmed (directory fsync failed)",
                str(target),
            ) from exc


def lock_descriptor(descriptor: int, *, shared: bool, blocking: bool) -> None:
    """``flock`` an open descriptor shared or exclusive.

    Non-blocking contention raises ``BlockingIOError``. The lock belongs to
    the open file description, so a second descriptor on the same file
    conflicts like another process would (never re-entrant).
    """
    mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
    fcntl.flock(descriptor, mode | (0 if blocking else fcntl.LOCK_NB))


def unlock_descriptor(descriptor: int) -> None:
    fcntl.flock(descriptor, fcntl.LOCK_UN)


def hold_exclusive(path: Path | str) -> int | None:
    """Take an exclusive ``flock`` on ``path`` without waiting; the fd survives exec.

    Returns the open descriptor (inheritable, so a following ``execve`` keeps
    the lock with the new program image until it exits), or None when another
    open file description holds the lock. The file is created 0600 and never
    opened through a symlink.
    """
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        return None
    except BaseException:
        os.close(descriptor)
        raise
    os.set_inheritable(descriptor, True)
    return descriptor


def lock_held(path: Path | str) -> bool | None:
    """Whether another open file description holds a ``flock`` on ``path``.

    A momentary non-blocking probe (taken and released at once); an absent
    file is free. Anything that prevents the probe is unknown (None).
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return False
    except OSError:
        return None
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return None
    finally:
        os.close(descriptor)  # closing releases a probe lock that was taken
    return False


def owned_by_caller(info: os.stat_result) -> bool:
    """The entry belongs to this process's effective user."""
    return info.st_uid == os.geteuid()


def shared_mode_bits(info: os.stat_result) -> int:
    """The group/other permission bits of ``info`` (0 for a private entry)."""
    return stat.S_IMODE(info.st_mode) & SHARED_MODE_BITS


def exec_replace(path: str, argv: list[str], env: dict[str, str]) -> Any:
    """Replace this process image (``execve``); returns only on test doubles."""
    return os.execve(path, argv, env)


def move_aside_command(path: str, backup: str) -> str:
    """Shell text that renames ``path`` to an absent ``backup`` without following it.

    GNU coreutils only: ``mv -T`` (a known portability limit).
    """
    return f"test ! -e {backup} && test ! -L {backup} && mv -T -- {path} {backup}"
