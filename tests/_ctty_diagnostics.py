"""One disposable controlling-terminal child's Python-level observations only.

Installed through the card fixture's setup hook, never on import. No native
getch/profiler/signal interception, extra input, poll, terminal-mode request or
control FD. Read-only descriptor snapshots and bounded in-memory records can
still perturb timing: an instrumented pass is not root-cause or repair proof.
"""

from __future__ import annotations

import atexit
import fcntl
import json
import os
import select
import termios


_LIMIT = 4096  # Includes the prefix and newline of the one exit write.
_MAX_RECORDS = 24
_MAX_POLL_EVENTS = 4
_PREFIX = b"CTTY_DIAGNOSTICS="
_BAD_POLL = select.POLLHUP | select.POLLERR | select.POLLNVAL
_PIPE_LIMIT = 8192


def _exception(exc: BaseException) -> dict:
    # termios.error can carry errno only in args, not in an errno attribute.
    number = getattr(exc, "errno", None)
    if not isinstance(number, int):
        number = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
    name = type(exc).__name__
    return {"exception": name[:64], "errno": number,
            "exception_type_truncated": len(name) > 64}


def _fd_status(fd: int) -> dict:
    facts = {"fd": fd}
    try:
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        facts.update(flags=flags, access={os.O_RDONLY: "rdonly", os.O_WRONLY: "wronly",
                                         os.O_RDWR: "rdwr"}.get(flags & os.O_ACCMODE, "unknown"),
                     nonblock=bool(flags & os.O_NONBLOCK))
    except (OSError, ValueError) as exc:
        facts["getfl_error"] = _exception(exc)
    try:
        stat = os.fstat(fd)
        facts.update(dev=stat.st_dev, ino=stat.st_ino, rdev=stat.st_rdev, mode=stat.st_mode)
    except (OSError, ValueError) as exc:
        facts["fstat_error"] = _exception(exc)
    try:
        facts["isatty"] = os.isatty(fd)
    except (OSError, ValueError) as exc:
        facts["isatty_error"] = _exception(exc)
    return facts


def _mode_bits(attributes) -> dict | None:
    # Only the requested mode's bits and VMIN/VTIME, never control/input text.
    try:
        def cc(index):
            value = attributes[6][index]
            return value if isinstance(value, int) else value[0]

        return {"ixon": bool(attributes[0] & termios.IXON),
                "istrip": bool(attributes[0] & termios.ISTRIP),
                "cs8": attributes[2] & termios.CSIZE == termios.CS8,
                "echo": bool(attributes[3] & termios.ECHO),
                "icanon": bool(attributes[3] & termios.ICANON),
                "isig": bool(attributes[3] & termios.ISIG),
                "vmin": cc(termios.VMIN), "vtime": cc(termios.VTIME)}
    except (IndexError, KeyError, TypeError):
        return None  # The original tcsetattr still judges its original arguments.


class _ObservedPoll:
    def __init__(self, delegate, fd: int, observer: _Observer):
        self.delegate, self.fd, self.observer = delegate, fd, observer

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def poll(self, *args, **kwargs):
        observer = self.observer
        facts = dict(fd=self.fd, timeout=args[0] if args else kwargs.get("timeout"), **observer.context)
        try:
            result = self.delegate.poll(*args, **kwargs)
        except BaseException as exc:
            observer.record("poll-raise", failed=True, **facts, **_exception(exc), fds=observer.snapshot())
            raise
        bad = next((event for event in result if event[0] == self.fd and event[1] & _BAD_POLL), None)
        shown = list(result[:_MAX_POLL_EVENTS])
        if bad is not None and bad not in shown:
            shown[-1] = bad  # Keep the offending result even if the result list is capped.
        omitted = len(result) - len(shown)
        observer.poll_events_omitted += omitted
        facts.update(events=shown, events_omitted=omitted)
        if bad is not None:
            # Before returning to InputState's existing error predicate/FD cleanup.
            facts["fds"] = observer.snapshot()
        observer.record("poll", failed=bad is not None, **facts)
        return result  # Same object, flags and order; no extra poll.


class _Observer:
    def __init__(self, slave_fd: int):
        self.slave_fd = slave_fd
        self.records: list[tuple[int, dict]] = []
        self.sequence = 0
        self.read_sequence = 0
        self.context: dict = {}
        self.first_failure = False
        self.poll_events_omitted = 0
        self.emitted = False

    def snapshot(self) -> list[dict]:
        # Only the fixture's explicitly preserved slave, no /dev/fd scan/open.
        return [_fd_status(fd) for fd in (0, 1, self.slave_fd)]

    def record(self, event: str, *, failed: bool = False, initial: bool = False, **facts) -> None:
        self.sequence += 1
        # Retain the first failure and initial redirected-FD snapshot ahead of
        # later failures, and all failures ahead of routine idle/read records.
        priority = 3 if failed and not self.first_failure else 2 if initial else 1 if failed else 0
        self.first_failure |= failed
        self.records.append((priority, dict(seq=self.sequence, event=event, **facts)))
        if len(self.records) > _MAX_RECORDS:
            self.records.pop(min(range(len(self.records)), key=lambda i: self.records[i][0]))

    def install(self, input_state: type) -> None:
        original_init, original_read = input_state.__init__, input_state.read
        original_tcsetattr = termios.tcsetattr
        observer = self

        def init(state, *args, **kwargs):
            try:
                result = original_init(state, *args, **kwargs)
            except BaseException as exc:
                observer.record("input-init-raise", failed=True, **_exception(exc), fds=observer.snapshot())
                raise
            # Product has already dup2'ed its terminal streams onto 0/1 here.
            observer.record("input-init", initial=True, fd=state.fd, fds=observer.snapshot())
            state.poll = _ObservedPoll(state.poll, state.fd, observer)
            return result

        def read(state, *args, **kwargs):
            observer.read_sequence += 1
            previous = observer.context
            observer.context = {"read": observer.read_sequence,
                                "context": "peek" if kwargs.get("deadline") is not None else "ordinary"}
            try:
                result = original_read(state, *args, **kwargs)
            except BaseException as exc:
                observer.record("read-raise", failed=True, **observer.context, **_exception(exc))
                raise
            else:
                outcome = "esc" if isinstance(result, str) and result == "\x1b" else "none" if result is None else "other"
                observer.record("read", **observer.context, result=outcome)
                return result
            finally:
                observer.context = previous

        def tcsetattr(*args, **kwargs):
            facts = {"fd": args[0] if args and isinstance(args[0], int) else None,
                     "action": args[1] if len(args) > 1 and isinstance(args[1], int) else None,
                     "mode": _mode_bits(args[2]) if len(args) > 2 else None}
            try:
                result = original_tcsetattr(*args, **kwargs)
            except BaseException as exc:
                observer.record("tcsetattr", failed=True, ok=False, **facts, **_exception(exc), fds=observer.snapshot())
                raise
            observer.record("tcsetattr", ok=True, **facts)
            return result

        input_state.__init__, input_state.read = init, read
        termios.tcsetattr = tcsetattr
        atexit.register(self.emit)

    def emit(self) -> None:
        if self.emitted:
            return
        self.emitted = True
        records = list(self.records)
        while True:
            dropped = self.sequence - len(records)
            events = [record for _, record in records]
            report = {"version": 1, "records_seen": self.sequence, "records_dropped": dropped,
                      "poll_events_omitted": self.poll_events_omitted,
                      "truncated": bool(dropped or self.poll_events_omitted
                                        or any(record.get("exception_type_truncated") for record in events)),
                      "poll_bits": {"in": select.POLLIN, "hup": select.POLLHUP,
                                    "err": select.POLLERR, "nval": select.POLLNVAL},
                      "events": events}
            payload = _PREFIX + json.dumps(report, separators=(",", ":")).encode("ascii") + b"\n"
            if len(payload) <= _LIMIT:
                break
            records.pop(min(range(len(records)), key=lambda i: records[i][0]))
        try:
            os.write(2, payload)  # Existing captured stderr only, once, no retry/new FD.
        except OSError:
            pass  # Diagnostic output cannot turn a product result into a failure.


def install(slave_fd: int) -> None:
    from charmer import tui

    _Observer(slave_fd).install(tui.InputState)


def report_unready_pipe(pipe, *, tty_bytes: int) -> None:
    """After the existing reap only: one zero-wait pipe check and one raw read.

    Never BufferedReader.read/communicate: a descendant could still hold the
    writer. There is no other reader before readiness, so a ready raw pipe
    read returns available bytes without waiting to fill the request.
    """

    output = b""
    capture = "not-ready"
    error = None
    try:
        fd = pipe.fileno()
        ready, _, _ = select.select([fd], [], [], 0)
        if ready:
            output = os.read(fd, _PIPE_LIMIT + 1)
            capture = "at-limit" if len(output) == _PIPE_LIMIT + 1 else "available-prefix"
    except (OSError, ValueError) as exc:
        capture, error = "unavailable", _exception(exc)
    detail = pipe_summary(output, tty_bytes=tty_bytes)
    print(f"CTTY_DIAGNOSTIC before_ready=True; pipe_capture={capture}; "
          f"pipe_capture_limit={_PIPE_LIMIT + 1}; capture_error={json.dumps(error)}; {detail}", flush=True)


def pipe_summary(pipe_output: bytes, *, tty_bytes: int) -> str:
    """Escaped, bounded pipe/error + full bounded observer tail, never TTY bytes."""

    size = len(pipe_output)
    truncated = size > _PIPE_LIMIT
    if truncated:
        observer_at = pipe_output.rfind(_PREFIX)
        # Keep the complete <=4 KiB observer even if another exit handler
        # writes after it; retain the error head and nearby preceding errors.
        end = min(size, observer_at + _LIMIT) if observer_at >= 0 else size
        shown = (pipe_output[:2048] + b"\n...<pipe output truncated>...\n"
                 + pipe_output[max(0, end - 6144):end])
    else:
        shown = pipe_output
    return (f"tty_bytes={tty_bytes}; pipe_bytes={size}; pipe_truncated={truncated}; "
            f"pipe_output={shown!r}")
