"""Linux service-manager backend: systemd user units.

The only module besides the ``service`` facade that names the manager's
commands. It builds argv and hint text, reads unit properties, runs the
three control verbs (:func:`control`) the gateway lifecycle delegates to when
the user installed the supervised service, and the unit-file verbs of that
install (daemon-reload, enable/disable --now, reload); it never decides
gateway policy (ownership and the persistence hold are checked by its
callers). Every call takes an injected ``runner``. An unavailable manager
reads as ``None`` (unknown) or False, never as a stopped unit. It does not
import its facade.
"""

from __future__ import annotations

import datetime
import json
import math
import os
import re
import selectors
import subprocess
import time
from typing import Callable

from charmer.platform import observation

VERBS = ("start", "stop", "restart", "reload", "status", "reset-failed")
CONTROL_VERBS = ("start", "stop", "restart")
CONTROL_TIMEOUT = 120


# What a person runs to make the service manager read changed unit files.
RELOAD_HINT = "systemctl --user daemon-reload"


def hint(verb: str, *, unit: str) -> str:
    if verb in VERBS:
        return f"systemctl --user {verb} {unit}"
    if verb == "recover":
        return f"{hint('reset-failed', unit=unit)} && {hint('start', unit=unit)}"
    if verb == "why":
        return f"systemctl --user show -p ActiveState,Result,ExecMainStatus {unit}"
    raise ValueError(f"unsupported gateway service verb: {verb}")


def journal_argv(*, since: str, unit: str) -> tuple[str, ...]:
    return ("journalctl", "--user", "-u", unit, "--since", since, "-o", "cat")


def control(verb: str, unit: str, runner: Callable, *, timeout: float = CONTROL_TIMEOUT,
            no_block: bool = False):
    """One ``systemctl --user start|stop|restart UNIT`` (bounded, no prompt).

    ``no_block`` only enqueues the job (the caller observes the outcome
    within its own deadline).
    """

    if verb not in CONTROL_VERBS:
        raise ValueError(f"unsupported control verb: {verb}")
    return runner(
        ["systemctl", "--user", "--no-ask-password", *(["--no-block"] if no_block else []), verb, unit],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout,
        env={**os.environ, "LC_ALL": "C"},
    )


# The unit-file verbs of ``gateway service install|uninstall`` (each bounded,
# never prompting); a False result is a refusal, never an exception.
MANAGER_TIMEOUT = 60
UNIT_PROPERTIES = ("LoadState", "FragmentPath", "UnitFileState", "ActiveState")


def _manager_run(args: list[str], runner: Callable, *, timeout: float = MANAGER_TIMEOUT):
    return runner(
        ["systemctl", "--user", "--no-ask-password", *args],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout,
        env={**os.environ, "LC_ALL": "C"},
    )


def _manager_ok(args: list[str], runner: Callable) -> bool:
    try:
        return _manager_run(args, runner).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def manager_available(runner: Callable) -> bool:
    """Whether a user service manager answers (a read; nothing changes)."""

    try:
        completed = _manager_run(["show", "--property=Version", "--value"], runner, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0 and bool(str(completed.stdout).strip())


def daemon_reload(runner: Callable) -> bool:
    return _manager_ok(["daemon-reload"], runner)


def enable_now(unit: str, runner: Callable) -> bool:
    return _manager_ok(["enable", "--now", f"{unit}.service"], runner)


def disable_now(unit: str, runner: Callable) -> bool:
    return _manager_ok(["disable", "--now", f"{unit}.service"], runner)


def enable(unit: str, runner: Callable, *, runtime: bool = False) -> bool:
    """Enable without starting (``runtime``: until the next reboot only)."""

    return _manager_ok(["enable", *(["--runtime"] if runtime else []), f"{unit}.service"], runner)


def start(unit: str, runner: Callable) -> bool:
    return _manager_ok(["start", f"{unit}.service"], runner)


def reload_unit(unit: str, runner: Callable) -> bool:
    return _manager_ok(["reload", f"{unit}.service"], runner)


def unit_properties(unit: str, runner: Callable) -> dict[str, str] | None:
    """The unit's load, fragment, enablement and activity (None when unreadable)."""

    try:
        completed = runner(
            ["systemctl", "--user", "show", *(f"--property={name}" for name in UNIT_PROPERTIES),
             f"{unit}.service"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10,
            env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    result = {}
    for line in str(completed.stdout).splitlines():
        key, separator, value = line.partition("=")
        if separator and key in UNIT_PROPERTIES:
            result[key] = value.strip()
    return result


def unit_dir(environ) -> str:
    """Where the user's own unit files live (``$XDG_CONFIG_HOME/systemd/user``,
    else ``~/.config/systemd/user``)."""

    config = environ.get("XDG_CONFIG_HOME") or ""
    if not config.startswith("/"):
        config = os.path.join(environ.get("HOME") or os.path.expanduser("~"), ".config")
    return os.path.join(config, "systemd", "user")


def journal_tail(*, unit: str, lines: int, runner: Callable, max_bytes: int = 256 << 10) -> list[str] | None:
    """The unit's last ``lines`` messages (``-o cat``), or None when unreadable.

    Records end at a line feed only: a carriage return, a vertical tab or a
    Unicode line separator inside a message stays in its line, where the
    redactor removes it before matching (split there, a header name and its
    value would land on two lines)."""

    try:
        completed = runner(
            ["journalctl", "--user", "-u", unit, "-n", str(max(1, lines)), "--no-pager", "-o", "cat"],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
            env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    raw = completed.stdout if isinstance(completed.stdout, bytes) else str(completed.stdout).encode()
    records = raw[-max_bytes:].decode("utf-8", "replace").split("\n")
    if len(raw) > max_bytes:
        records = records[1:]  # the first piece may be a partial line
    if records and records[-1] == "":
        records = records[:-1]
    return records[-lines:]


QUERY_TIMEOUT = 10


def manager_query(name: str, unit: str, runner: Callable, *, timeout: float = QUERY_TIMEOUT):
    # Keep subsecond ordering against Python's success stamps; the default
    # human-readable format truncates a later pre-exec failure to whole seconds.
    timestamp = ["--timestamp=us+utc"] if name == "ExecReload" else []
    return runner(
        ["systemctl", "--user", "show", f"--property={name}", "--value", *timestamp, unit],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout,
        env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
    )


def manager_property(name: str, unit: str, runner: Callable, *, timeout: float = QUERY_TIMEOUT) -> str | None:
    try:
        completed = manager_query(name, unit, runner, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def manager_time(value: str) -> datetime.datetime:
    # manager_query pins the command's locale and timezone, not the manager's.
    form = "%a %Y-%m-%d %H:%M:%S" + (".%f" if "." in value else "") + " UTC"
    return datetime.datetime.strptime(value, form).replace(tzinfo=datetime.timezone.utc)


def last_reload_result(*, unit: str, runner: Callable) -> observation.ExecReloadResult | None:
    """Read the last completed ExecReload, including failures before Python ran.

    systemctl's ExecReload record carries wall-clock start/stop times. A boolean
    alone cannot distinguish a new pre-exec failure from a failure superseded
    by a successful config-only init. Unset or malformed records are unavailable.
    """
    raw = manager_property("ExecReload", unit, runner)
    if raw is None:
        return None
    results = []
    for record in re.findall(r"\{([^{}]*)\}", raw):
        match = re.search(
            r"start_time=\[([^\]]+)\]\s*;\s*stop_time=\[([^\]]+)\]"
            r"\s*;\s*pid=[0-9]+\s*;\s*code=(exited|killed|dumped|[123])"
            r"\s*;\s*status=([0-9]+)(?:/[A-Za-z0-9+_-]+)?\s*$", record,
        )
        if match is None:
            return None
        start, stop, code, status = match.groups()
        try:
            started_at, stopped_at = manager_time(start), manager_time(stop)
            failed = code not in ("exited", "1") or int(status) != 0
        except ValueError:
            return None
        if stopped_at < started_at:
            return None
        results.append(observation.ExecReloadResult(started_at, failed))
    return max(results, key=lambda result: (result.started_at, result.failed), default=None)


def _normalize(item: dict, message: str) -> observation.LogRecord:
    """One journald entry as a record: its timestamp, instance and cursor
    (each unknown when its field is missing or malformed)."""

    timestamp = None
    micros = item.get("__REALTIME_TIMESTAMP")
    if isinstance(micros, str) and re.fullmatch(r"[0-9]{1,18}", micros):
        try:
            timestamp = datetime.datetime.fromtimestamp(int(micros) / 1_000_000, datetime.timezone.utc)
        except (ValueError, OverflowError, OSError):
            pass
    ids = (item.get("_BOOT_ID"), item.get("_SYSTEMD_INVOCATION_ID"))
    instance = ":".join(ids) if all(
        isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) for value in ids
    ) else None
    cursor = item.get("__CURSOR")
    if not isinstance(cursor, str) or not re.fullmatch(r"[A-Za-z0-9;=_.-]{1,512}", cursor):
        cursor = None
    return observation.LogRecord(message, timestamp, instance, cursor)


def journal_records(raw: bytes, *, coverage: str = "bounded",
                    newest_first: bool = False) -> observation.LogWindow:
    """Normalize journald fields; raw JSON, paths and messages are never facts.

    boot/invocation is the process-generation identity, not a binary attestation.
    Missing fields remain unknown, never inferred from the current gateway PID.
    ``newest_first`` input (``journalctl --reverse``) keeps the newest records
    under the record cap; the window is always returned in chronological order.
    """
    records = []
    for line in raw.splitlines():
        if len(records) >= observation.LOG_MAX_RECORDS:
            coverage = "truncated"
            break
        if len(line) > 1 << 20:
            coverage = "incomplete"
            continue
        try:
            item = json.loads(line)
        except (ValueError, UnicodeDecodeError, RecursionError):
            coverage = "incomplete"
            continue
        if not isinstance(item, dict) or not isinstance(item.get("MESSAGE"), str):
            coverage = "incomplete"
            continue
        records.append(_normalize(item, item["MESSAGE"]))
    if newest_first:
        records.reverse()
    return observation.LogWindow(tuple(records), coverage)


def read_gateway_journal(*, unit: str, max_bytes: int, timeout: float = 10,
                         popen: Callable = subprocess.Popen, since: str = "-24h") -> observation.LogWindow:
    """One hard-bounded journald pass: bytes, records and wall time.

    Never buffer unbounded stdout before slicing it. stderr is discarded; on
    timeout/overflow kill and reap the reader, not the gateway. Even successful
    reads are bounded observations, not journal-continuity attestations.
    The reader runs newest first, so any cap drops the oldest part of the 24 h
    window (the window then says so), never the newest events.
    """
    argv = ["journalctl", "--user", "-u", unit, "--since", since, "--no-pager", "--reverse",
            "-o", "json",
            "--output-fields=MESSAGE,__CURSOR,__REALTIME_TIMESTAMP,_BOOT_ID,_SYSTEMD_INVOCATION_ID"]
    raw = bytearray()
    coverage = "bounded"
    timed_out = False
    deadline = time.monotonic() + timeout
    try:
        with popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                   stderr=subprocess.DEVNULL) as process:
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or not selector.select(remaining):
                            coverage = "incomplete"
                            timed_out = True
                            break
                        chunk = os.read(process.stdout.fileno(), min(65536, max_bytes + 1 - len(raw)))
                        if not chunk:
                            break
                        raw.extend(chunk)
                        if len(raw) > max_bytes:
                            coverage = "truncated"
                            break
                if coverage == "bounded":
                    try:
                        if process.wait(timeout=max(0.001, deadline - time.monotonic())) != 0:
                            return observation.LogWindow()
                    except subprocess.TimeoutExpired:
                        coverage, timed_out = "incomplete", True
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
    except (OSError, subprocess.SubprocessError):
        return observation.LogWindow()
    if coverage != "bounded":
        # The last record may have been cut in the middle of JSON or UTF-8.
        raw = raw[:max_bytes].rsplit(b"\n", 1)[0] if b"\n" in raw[:max_bytes] else b""
    window = journal_records(bytes(raw), coverage=coverage, newest_first=True)
    if len(window.records) >= observation.LOG_MAX_RECORDS:
        return observation.LogWindow(window.records, "truncated", timed_out)
    return observation.LogWindow(window.records, window.coverage, timed_out)


# The persistence hold's journal reads (:func:`read_hold_journal`,
# :func:`journal_reaches`, :func:`journal_drops`). They read only what the
# hold needs over the gateway's whole run: its credential-save lines and the
# running instance's start line. One deadline bounds a read and the caps count
# kept evidence only, so how long the gateway ran and how much it logged do
# not matter. ``--all`` keeps a field over 4096 bytes from reading as null.
HOLD_TIMEOUT = 30.0
HOLD_LINE_LIMIT = 1 << 20
PROBE_TIMEOUT = 10.0
# SD_MESSAGE_JOURNAL_DROPPED: journald suppressed messages (its rate limit).
DROPPED_MESSAGE_ID = "a596d6fe7bfa4994828e72309e95d61e"
DROPS_MAX = 256
_HOLD_FIELDS = "--output-fields=MESSAGE,__CURSOR,__REALTIME_TIMESTAMP,_BOOT_ID,_SYSTEMD_INVOCATION_ID"
# Only literal-safe text joins a --grep pattern: printable ASCII for the
# evidence pattern (the caller's constant) and a start line of this shape.
_SAFE_PATTERN = re.compile(r"[ -~]+")
_SAFE_MARKER = re.compile(r"[a-z][a-z0-9-]*: gateway instance [0-9a-f]{16} starting")
_MICROS = re.compile(r"[0-9]{1,18}")


def hold_grep_pattern(evidence: str, marker: str | None) -> str | None:
    """The ``--grep`` pattern for ``evidence`` (a regular expression that
    means the same in PCRE2 and Python) or the start line ``marker``; None
    when either cannot be passed safely (the read then filters in Python
    only). Every character the marker shape allows is literal in PCRE2."""

    if not _SAFE_PATTERN.fullmatch(evidence):
        return None
    if marker is None:
        return evidence
    if _SAFE_MARKER.fullmatch(marker):
        return f"{evidence}|^{marker}"
    return None


def _message_text(item: dict) -> str | bool | None:
    """MESSAGE as text: a string, or journald's byte array decoded (UTF-8,
    "replace"); False when the entry has none, None when its shape cannot
    be read."""

    if "MESSAGE" not in item:
        return False
    message = item["MESSAGE"]
    if isinstance(message, str):
        return message
    if isinstance(message, list) and all(type(value) is int and 0 <= value <= 255 for value in message):
        return bytes(message).decode("utf-8", "replace")
    return None


def _stream(argv: list[str], keep: Callable[[str], bool], max_bytes: int, deadline: float,
            popen: Callable) -> tuple[int | None, observation.LogWindow]:
    """One streamed ``journalctl -o json`` read, newest first, keeping only
    the entries ``keep`` accepts: ``(exit status, window)``.

    The status is None when the read was stopped (a cap, the deadline) or
    could not start. Kept records over ``max_bytes`` of JSON, or over the
    record cap, drop the oldest kept and stop the read ("truncated"); invalid
    JSON, an unreadable MESSAGE shape, a line over :data:`HOLD_LINE_LIMIT`
    or an unfinished last line make it "incomplete"; so does the deadline,
    with ``timed_out``. stderr is discarded; only the reader is ever killed.
    """

    records: list[observation.LogRecord] = []
    kept_bytes = 0
    coverage = "bounded"
    timed_out = False
    code = None

    def incomplete() -> None:
        nonlocal coverage
        if coverage == "bounded":
            coverage = "incomplete"

    def take(line: bytes) -> bool:
        """One complete line; False once a cap stops the read."""

        nonlocal kept_bytes, coverage
        if len(line) > HOLD_LINE_LIMIT:
            incomplete()
            return True
        try:
            item = json.loads(line)
        except (ValueError, UnicodeDecodeError, RecursionError):
            incomplete()
            return True
        if not isinstance(item, dict):
            incomplete()
            return True
        text = _message_text(item)
        if text is False:
            return True  # no MESSAGE: never evidence
        if text is None:
            incomplete()
            return True
        if not keep(text):
            return True
        records.append(_normalize(item, text))
        kept_bytes += len(line)
        if len(records) > observation.LOG_MAX_RECORDS or kept_bytes > max_bytes:
            records.pop()  # newest first: the last one kept is the oldest
            coverage = "truncated"
            return False
        return True

    try:
        with popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                   stderr=subprocess.DEVNULL) as process:
            try:
                carry = bytearray()
                skipping = False  # inside a line over the limit, up to its newline
                stopped = False
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while not stopped:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or not selector.select(remaining):
                            coverage, timed_out, stopped = "incomplete", True, True
                            break
                        chunk = os.read(process.stdout.fileno(), 65536)
                        if not chunk:
                            if carry or skipping:
                                incomplete()  # an unfinished last line
                            break
                        position = 0
                        while not stopped:
                            newline = chunk.find(b"\n", position)
                            if newline < 0:
                                if not skipping:
                                    carry += chunk[position:]
                                    if len(carry) > HOLD_LINE_LIMIT:
                                        incomplete()
                                        carry.clear()
                                        skipping = True
                                break
                            piece = chunk[position:newline]
                            position = newline + 1
                            if skipping:
                                skipping = False
                                continue
                            if carry:
                                carry += piece
                                line, carry = bytes(carry), bytearray()
                            else:
                                line = piece
                            stopped = not take(line)
                if not stopped:
                    try:
                        code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired:
                        coverage, timed_out = "incomplete", True
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
    except (OSError, subprocess.SubprocessError):
        return None, observation.LogWindow()
    records.reverse()
    return code, observation.LogWindow(tuple(records), coverage, timed_out)


def read_hold_journal(*, unit: str, since: str, evidence: str, marker: str | None, max_bytes: int,
                      timeout: float = HOLD_TIMEOUT, popen: Callable = subprocess.Popen) -> observation.LogWindow:
    """The persistence hold's evidence in the unit's journal from ``since``:
    the entries whose MESSAGE matches the regular expression ``evidence`` or
    starts with the start line ``marker``, in chronological order.

    Pass 1 lets journalctl filter (``--case-sensitive=yes --grep``). Any exit
    there other than 0 (no match, no PCRE2, an older or failing journalctl,
    no journalctl) discards what it read, and pass 2 reads every entry and
    applies the same predicate in Python; a non-zero pass 2 is
    "unavailable", never an empty journal. One deadline covers both passes,
    and a pass cut by a cap or the deadline is returned as it is.
    """

    predicate = re.compile(evidence)

    def keep(text: str) -> bool:
        return predicate.search(text) is not None or (marker is not None and text.startswith(marker))

    base = ["journalctl", "--user", "-u", unit, "--since", since, "--no-pager", "--reverse", "--all",
            "-o", "json", _HOLD_FIELDS]
    deadline = time.monotonic() + timeout
    pattern = hold_grep_pattern(evidence, marker)
    if pattern is not None:
        code, window = _stream([*base, "--case-sensitive=yes", f"--grep={pattern}"], keep, max_bytes,
                               deadline, popen)
        if window.timed_out or window.coverage == "truncated" or code == 0:
            return window
    code, window = _stream(base, keep, max_bytes, deadline, popen)
    if window.timed_out or window.coverage == "truncated":
        return window
    return window if code == 0 else observation.LogWindow()


def _probe(argv: list[str], runner: Callable, timeout: float) -> list[bytes] | None:
    """A bounded journalctl query's non-empty stdout lines (None when it fails)."""

    try:
        completed = runner(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    raw = completed.stdout if isinstance(completed.stdout, bytes) else str(completed.stdout or "").encode()
    return [line for line in raw.split(b"\n") if line.strip()]


def _entry(line: bytes) -> dict | None:
    try:
        item = json.loads(line)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    return item if isinstance(item, dict) else None


def _epoch_micros(at: datetime.datetime) -> int:
    return (at - datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)) // datetime.timedelta(microseconds=1)


def journal_reaches(*, unit: str, at: datetime.datetime, timeout: float = PROBE_TIMEOUT,
                    runner: Callable = subprocess.run) -> bool | None:
    """Whether the unit's journal still holds an entry at or before ``at``:
    True, False (none: it was rotated or vacuumed past ``at``) or None
    (cannot tell). journald removes the oldest entries first, so True asked
    after a read means that read saw everything after ``at``."""

    lines = _probe(["journalctl", "--user", "-u", unit, "--until", f"@{math.floor(at.timestamp())}",
                    "-n", "1", "--no-pager", "-o", "json", "--output-fields=__REALTIME_TIMESTAMP"],
                   runner, timeout)
    if lines is None:
        return None
    if not lines:
        return False
    item = _entry(lines[0]) if len(lines) == 1 else None
    micros = item.get("__REALTIME_TIMESTAMP") if item is not None else None
    if not isinstance(micros, str) or not _MICROS.fullmatch(micros) or int(micros) > _epoch_micros(at):
        return None
    return True


def journal_drops(*, since: datetime.datetime, uid: int, timeout: float = PROBE_TIMEOUT,
                  runner: Callable = subprocess.run) -> tuple[tuple[datetime.datetime, int | None], ...] | None:
    """journald's rate-limit drop notices for the user's services since
    ``since``: ``(time, lines dropped or None)`` each, the newest
    :data:`DROPS_MAX` at most. journald limits all of a user's services
    together, as ``user@<uid>.service``, so a notice naming another of the
    user's units may stand for gateway lines too. None when the system
    journal, where journald writes them, cannot be read here (an ordinary
    user's account) or the query fails."""

    probe = _probe(["journalctl", "--system", "-n", "1", "--no-pager", "-o", "json",
                    "--output-fields=__REALTIME_TIMESTAMP"], runner, timeout)
    if probe is None or len(probe) != 1 or _entry(probe[0]) is None:
        return None
    lines = _probe(["journalctl", "--system", f"MESSAGE_ID={DROPPED_MESSAGE_ID}", "_UID=0", f"OBJECT_UID={uid}",
                    f"OBJECT_SYSTEMD_UNIT=user@{uid}.service", "--since", f"@{math.floor(since.timestamp())}",
                    "-n", str(DROPS_MAX), "--no-pager", "-o", "json",
                    "--output-fields=__REALTIME_TIMESTAMP,N_DROPPED"], runner, timeout)
    if lines is None:
        return None
    notices = []
    for line in lines[-DROPS_MAX:]:
        item = _entry(line)
        micros = item.get("__REALTIME_TIMESTAMP") if item is not None else None
        if not isinstance(micros, str) or not _MICROS.fullmatch(micros):
            return None
        try:
            moment = datetime.datetime.fromtimestamp(int(micros) / 1_000_000, datetime.timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
        count = item.get("N_DROPPED")
        notices.append((moment, int(count) if isinstance(count, str) and _MICROS.fullmatch(count) else None))
    return tuple(notices)
