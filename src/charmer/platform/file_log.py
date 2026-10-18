"""Per-instance gateway log files: the on-demand backend's log source.

Each gateway start writes stdout and stderr to its own file,
``logs/gateway-<UTC start>-<instance nonce>.log`` (0600, created exclusively,
never through a symlink). Readers are bounded: a window covers a byte range
of one instance's file and keeps the newest bytes when the range is larger
than the budget (then its coverage says ``truncated``). Records carry the
instance nonce as their gateway instance and a ``<nonce>:<offset>`` cursor,
so a position in the log survives other instances' files coming and going.
Timestamps come from the gateway's line envelope; the on-demand spawn pins
``TZ=UTC``, so they are UTC. A line still being written (no newline yet) is
never part of a window; :func:`read_evidence`, the reader the persistence
hold uses, also says the window is ``incomplete`` then (an unfinished line
may be a failed save cut short by a full disk), and :func:`scan_instance_logs`
reports a directory or file it could not examine instead of leaving it out.
In keep mode :func:`read_evidence` streams the whole range and keeps only
the lines of an evidence pattern, so its budgets bound the kept bytes, the
bytes examined and the time, and its ``cause`` says which one cut a read.

Logs are safety evidence (a failed credential save may be the only record
of an unpersisted credential): nothing here truncates or rewrites a log file.
The one deletion, :func:`remove_instance_log`, removes a whole file of a
finished instance; its caller (the pruning at an instance start) first
persists what that file still proves. Stdlib only.
"""

from __future__ import annotations

import collections
import datetime
import os
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from charmer.platform import observation

PREFIX, SUFFIX = "gateway-", ".log"
NONCE = re.compile(r"^[0-9a-f]{16}$")
_NAME = re.compile(r"^gateway-([0-9]{8}T[0-9]{6}Z)-([0-9a-f]{16})\.log$")
_STAMP = "%Y%m%dT%H%M%SZ"
_ENVELOPE_TIME = re.compile(rb"^\[([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2})\] ")
LINE_LIMIT = 1 << 20  # one record; longer lines make the window incomplete


@dataclass(frozen=True)
class InstanceLog:
    path: Path
    started: datetime.datetime
    nonce: str
    size: int


def instance_log_name(started: datetime.datetime, nonce: str) -> str:
    if not NONCE.fullmatch(nonce):
        raise ValueError("instance nonce must be 16 lower-case hex digits")
    moment = started.astimezone(datetime.timezone.utc)
    return f"{PREFIX}{moment.strftime(_STAMP)}-{nonce}{SUFFIX}"


def create_instance_log(logs_dir: Path, started: datetime.datetime, nonce: str) -> tuple[int, Path]:
    """Create the instance's log file exclusively (0600); returns ``(fd, path)``.

    The descriptor is non-inheritable; a spawner passes it as the child's
    stdout and stderr explicitly.
    """

    path = Path(logs_dir) / instance_log_name(started, nonce)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND | os.O_CLOEXEC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, path


def scan_instance_logs(logs_dir: Path) -> tuple[tuple[InstanceLog, ...], tuple[str, ...]]:
    """``(logs, problems)``: every instance log, newest first, and what could
    not be examined (an unlistable directory, an entry whose metadata cannot
    be read). A missing directory is no logs and no problem.

    Only regular files (never symlinks) with the instance name shape count.
    Safety evaluations treat any problem as missing evidence, never as an
    empty collection.
    """

    found: list[InstanceLog] = []
    problems: list[str] = []
    try:
        entries = list(os.scandir(logs_dir))
    except FileNotFoundError:
        return (), ()
    except OSError as exc:
        return (), (f"the gateway log directory cannot be listed ({exc.strerror or type(exc).__name__})",)
    for entry in entries:
        match = _NAME.fullmatch(entry.name)
        if match is None:
            continue
        try:
            info = entry.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue  # removed since the listing
        except OSError as exc:
            problems.append(f"instance {match.group(2)}: its log cannot be examined "
                            f"({exc.strerror or type(exc).__name__})")
            continue
        if not stat.S_ISREG(info.st_mode):
            continue
        started = datetime.datetime.strptime(match.group(1), _STAMP).replace(tzinfo=datetime.timezone.utc)
        found.append(InstanceLog(Path(entry.path), started, match.group(2), info.st_size))
    found.sort(key=lambda log: (log.started, log.path.name), reverse=True)
    return tuple(found), tuple(problems)


def instance_logs(logs_dir: Path) -> tuple[InstanceLog, ...]:
    """Every instance log that could be examined, newest first (for display;
    safety evaluations use :func:`scan_instance_logs`)."""

    return scan_instance_logs(logs_dir)[0]


def find_instance(logs_dir: Path, nonce: str) -> InstanceLog | None:
    return next((log for log in instance_logs(logs_dir) if log.nonce == nonce), None)


def _open_regular(path: Path) -> int:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("not a regular file")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _timestamp(line: bytes) -> datetime.datetime | None:
    match = _ENVELOPE_TIME.match(line)
    if match is None:
        return None
    try:
        moment = datetime.datetime.strptime(match.group(1).decode("ascii"), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return moment.replace(tzinfo=datetime.timezone.utc)


@dataclass(frozen=True)
class Evidence:
    """One strict read of an instance log for a safety evaluation.

    ``end`` is the offset after the last complete line (a cursor position);
    ``through`` is how far the bytes were examined, an unfinished last line
    included, which is what a user's clear acknowledges.
    """

    window: observation.LogWindow
    end: int
    through: int
    # Why the window is not bounded (keep-mode reads), else "".
    cause: str = ""


def read_evidence(log: InstanceLog, *, start: int = 0, max_bytes: int = 32 << 20,
                  keep: re.Pattern[bytes] | None = None, scan_limit: int | None = None,
                  deadline: float | None = None) -> Evidence:
    """The complete lines from byte ``start`` to the end, for the persistence hold.

    Like :func:`read_window`, but the window is ``incomplete`` whenever the
    range could not be covered completely: an unfinished last line (a record
    cut short, for example by a full disk) or a read that ended early.

    With ``keep`` (the evidence pattern) the read streams every byte from
    ``start`` and keeps only the lines ``keep`` matches; ``max_bytes`` then
    bounds the kept bytes (the newest are kept), ``scan_limit`` the bytes
    examined (the newest), and ``deadline`` (``time.monotonic``) the time.
    Each limit makes the window ``truncated`` or ``incomplete`` and says why
    in ``cause``.
    """

    if keep is not None:
        return _scan(log, start=start, max_bytes=max_bytes, keep=keep, scan_limit=scan_limit,
                     deadline=deadline)
    return _read(log, start=start, max_bytes=max_bytes, strict=True)


_SCAN_CHUNK = 1 << 20


def _scan(log: InstanceLog, *, start: int, max_bytes: int, keep: re.Pattern[bytes],
          scan_limit: int | None, deadline: float | None) -> Evidence:
    """:func:`read_evidence` in keep mode: one streamed pass, kept lines only."""

    try:
        descriptor = _open_regular(log.path)
    except OSError:
        return Evidence(observation.LogWindow(), start, start, "the log could not be opened")
    coverage, cause, timed_out = "bounded", "", False

    def lower(to: str, why: str) -> None:
        nonlocal coverage, cause
        if coverage == "bounded" or (to == "truncated" and coverage != "truncated"):
            coverage, cause = to, why

    kept: collections.deque[observation.LogRecord] = collections.deque()
    sizes: collections.deque[int] = collections.deque()
    kept_bytes = 0
    try:
        size = os.fstat(descriptor).st_size
        if start < 0 or start > size:
            return Evidence(observation.LogWindow(coverage="incomplete"), start, start,
                            "the position cleared last is beyond the log's end")
        begin, skipping = start, False
        if scan_limit is not None and size - start > scan_limit:
            begin, skipping = size - max(0, scan_limit), True  # drop the partial first line
            lower("truncated", "the logs are larger than one evaluation reads")
        os.lseek(descriptor, begin, os.SEEK_SET)
        offset = begin  # the next byte to read
        line_start = begin  # where the carried line starts
        end = begin  # after the last complete line
        carry = bytearray()
        while offset < size:
            if deadline is not None and time.monotonic() >= deadline:
                timed_out = True
                break
            chunk = os.read(descriptor, min(_SCAN_CHUNK, size - offset))
            if not chunk:
                break
            position = 0
            while True:
                newline = chunk.find(b"\n", position)
                if newline < 0:
                    if not skipping:
                        carry += chunk[position:]
                        if len(carry) > LINE_LIMIT:
                            lower("incomplete", "a log line longer than 1 MiB")
                            carry.clear()
                            skipping = True
                    break
                here = line_start
                line_start = offset + newline + 1
                end = line_start
                piece = chunk[position:newline]
                position = newline + 1
                if skipping:
                    skipping = False
                    continue
                line = bytes(carry) + piece if carry else piece
                carry.clear()
                if len(line) > LINE_LIMIT:
                    lower("incomplete", "a log line longer than 1 MiB")
                    continue
                if keep.search(line) is None:
                    continue
                kept.append(observation.LogRecord(
                    line.decode("utf-8", "replace"), _timestamp(line), log.nonce, f"{log.nonce}:{here}"))
                sizes.append(len(line) + 1)
                kept_bytes += len(line) + 1
                while kept and (kept_bytes > max_bytes or len(kept) > observation.LOG_MAX_RECORDS):
                    kept.popleft()
                    kept_bytes -= sizes.popleft()
                    lower("truncated", "more credential-save lines than one read keeps")
            offset += len(chunk)
    except OSError:
        return Evidence(observation.LogWindow(), start, start, "the log could not be read")
    finally:
        os.close(descriptor)
    through = offset
    if timed_out:
        coverage, cause = "incomplete", "the log read did not finish in time"
    elif offset < size:
        lower("incomplete", "the log changed while it was read")  # it shrank, or a read ended early
    elif carry or skipping:
        lower("incomplete", "an unfinished last line (a record cut short, for example by a full disk)")
    return Evidence(observation.LogWindow(tuple(kept), coverage, timed_out), end, through, cause)


def _read(log: InstanceLog, *, start: int, max_bytes: int, strict: bool) -> Evidence:
    try:
        descriptor = _open_regular(log.path)
    except OSError:
        return Evidence(observation.LogWindow(), start, start)
    try:
        size = os.fstat(descriptor).st_size
        if start < 0 or start > size:
            return Evidence(observation.LogWindow(coverage="incomplete"), start, start)
        coverage = "bounded"
        begin = start
        if size - start > max_bytes:
            begin, coverage = size - max_bytes, "truncated"
        os.lseek(descriptor, begin, os.SEEK_SET)
        raw = b""
        while len(raw) < size - begin:
            chunk = os.read(descriptor, min(1 << 20, size - begin - len(raw)))
            if not chunk:
                break
            raw += chunk
    except OSError:
        return Evidence(observation.LogWindow(), start, start)
    finally:
        os.close(descriptor)
    through = begin + len(raw)
    if strict and len(raw) < size - begin and coverage == "bounded":
        coverage = "incomplete"  # the file shrank or the read ended early
    if coverage == "truncated":
        # The cut may fall inside a line: drop the partial first line.
        newline = raw.find(b"\n")
        if newline < 0:
            return Evidence(observation.LogWindow(coverage="truncated"), start, through)
        begin, raw = begin + newline + 1, raw[newline + 1:]
    complete = raw.rfind(b"\n") + 1
    end = begin + complete
    if strict and complete < len(raw) and coverage == "bounded":
        coverage = "incomplete"  # an unfinished last line
    records = []
    offset = begin
    for line in raw[:complete].split(b"\n")[:-1]:
        here = offset
        offset += len(line) + 1
        if len(line) > LINE_LIMIT:
            coverage = "incomplete" if coverage == "bounded" else coverage
            continue
        if len(records) >= observation.LOG_MAX_RECORDS:
            coverage = "truncated"
            break
        records.append(observation.LogRecord(
            line.decode("utf-8", "replace"), _timestamp(line), log.nonce, f"{log.nonce}:{here}"))
    return Evidence(observation.LogWindow(tuple(records), coverage), end, through)


def read_window(log: InstanceLog, *, start: int = 0,
                max_bytes: int = 32 << 20) -> tuple[observation.LogWindow, int]:
    """The complete lines from byte ``start`` to the end, as one window.

    Returns ``(window, end)``: ``end`` is the offset after the last complete
    line read (a cursor position). When the range exceeds ``max_bytes`` the
    oldest part is dropped and the window is ``truncated``; an unreadable file
    is ``unavailable``. A line still being written is left out without
    reducing the coverage (a live view; the persistence hold reads with
    :func:`read_evidence`).
    """

    evidence = _read(log, start=start, max_bytes=max_bytes, strict=False)
    return evidence.window, evidence.end


def tail_lines(path: Path, *, lines: int, max_bytes: int = 256 << 10) -> list[str]:
    """The last ``lines`` complete lines (bounded read from the end)."""

    if lines <= 0:
        return []
    try:
        descriptor = _open_regular(path)
    except OSError:
        return []
    try:
        size = os.fstat(descriptor).st_size
        begin = max(0, size - max_bytes)
        os.lseek(descriptor, begin, os.SEEK_SET)
        raw = os.read(descriptor, size - begin) if size > begin else b""
    except OSError:
        return []
    finally:
        os.close(descriptor)
    text = raw.decode("utf-8", "replace").split("\n")
    if begin > 0:
        text = text[1:]  # the first piece may be a partial line
    if text and text[-1] == "":
        text = text[:-1]
    return text[-lines:]


def remove_instance_log(log: InstanceLog) -> None:
    """Delete one finished instance's whole log file (never through a symlink).

    Callers decide which instance is finished and persist its unresolved
    evidence first; a file that is no longer the listed regular file is left
    alone (``OSError``).
    """

    if _NAME.fullmatch(log.path.name) is None:
        raise ValueError("not an instance log name")
    info = os.lstat(log.path)
    if not stat.S_ISREG(info.st_mode):
        raise OSError(f"{log.path.name} is not a regular file")
    os.unlink(log.path)


def total_size(logs: tuple[InstanceLog, ...]) -> int:
    return sum(log.size for log in logs)


def since_instant(spec: str, *, now: datetime.datetime) -> datetime.datetime:
    """``-24h`` / ``-90m`` / ``-2d`` relative to ``now`` (24 hours otherwise)."""

    match = re.fullmatch(r"-([0-9]{1,5})([smhd])", spec.strip())
    if match is None:
        return now - datetime.timedelta(hours=24)
    amount = int(match.group(1))
    unit = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[match.group(2)]
    return now - datetime.timedelta(**{unit: amount})


def read_recent(logs_dir: Path, *, since: datetime.datetime,
                max_bytes: int = 32 << 20) -> observation.LogWindow:
    """One chronological window over every instance log that may hold records
    newer than ``since``, newest instances first within ``max_bytes``.

    Records with an envelope time older than ``since`` are dropped; lines
    without one are kept (unknown age). A budget cut drops the oldest part
    and says ``truncated``; no logs at all is an empty bounded window, and a
    directory or log that cannot be examined makes the window ``incomplete``.
    """

    windows: list[observation.LogWindow] = []
    logs, problems = scan_instance_logs(logs_dir)
    coverage = "incomplete" if problems else "bounded"
    remaining = max_bytes
    for log in logs:
        try:
            modified = datetime.datetime.fromtimestamp(os.lstat(log.path).st_mtime, datetime.timezone.utc)
        except OSError:
            coverage = "incomplete"
            continue
        if modified < since:
            break  # older instances wrote nothing inside the window
        if remaining <= 0:
            coverage = "truncated"
            break
        window, end = read_window(log, start=0, max_bytes=remaining)
        remaining -= max(0, end)
        windows.append(window)
        if window.coverage != "bounded":
            coverage = window.coverage if coverage == "bounded" else coverage
        if log.started < since:
            break
    records: list[observation.LogRecord] = []
    for window in reversed(windows):
        records.extend(record for record in window.records
                       if record.timestamp is None or record.timestamp >= since)
    if len(records) > observation.LOG_MAX_RECORDS:
        records = records[-observation.LOG_MAX_RECORDS:]
        coverage = "truncated"
    return observation.LogWindow(tuple(records), coverage)
