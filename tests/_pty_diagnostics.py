"""Test-only, content-free input phases and timeout observations for one PTY child.

The profile callback observes the first getch C-call boundary, not entry to
its kernel wait. Instrumentation can perturb signal timing; it is not a
readiness handshake or evidence that an unobserved failure has been fixed.
"""

from __future__ import annotations

import faulthandler
import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


_LIMIT = 16384
_OBSERVE_SECONDS = 0.2  # diagnostic-only, after the original exit deadline


def _event(fd: int, phase: str, **facts) -> None:
    try:
        os.write(fd, (json.dumps(dict(phase=phase, at_ns=time.monotonic_ns(), **facts)) + "\n").encode())
    except OSError:
        pass  # diagnostic I/O must not change the child's input/exit behaviour


def install(events_fd: int, stack_fd: int) -> None:
    """Called only inside the disposable fixture child; never installs a SIGINT handler."""

    from charmer import tui

    def emit(phase: str, **facts) -> None:
        handler = signal.getsignal(signal.SIGINT)
        disposition = ("default-int-handler" if handler is signal.default_int_handler else
                       "ignored" if handler == signal.SIG_IGN else
                       "default" if handler == signal.SIG_DFL else "other-handler")
        mask = (sorted(map(int, signal.pthread_sigmask(signal.SIG_BLOCK, [])))
                if hasattr(signal, "pthread_sigmask") else None)  # empty set: query only
        _event(events_fd, phase, sigint=disposition, blocked_signals=mask, **facts)

    dump_signal = None
    candidate = getattr(signal, "SIGUSR1", None)
    if candidate is not None and signal.getsignal(candidate) == signal.SIG_DFL:
        try:
            faulthandler.register(candidate, file=stack_fd, all_threads=True, chain=False)
            dump_signal = int(candidate)
        except (OSError, RuntimeError):
            pass
    emit("installed", pid=os.getpid(), dump_signal=dump_signal)
    original = tui.read_key
    first = True

    def read_key(win):
        nonlocal first
        if not first:
            return original(win)
        first = False
        emit("read-key-enter")
        previous = sys.getprofile()

        def profile(frame, event, function):
            if event in ("c_call", "c_return", "c_exception") and getattr(function, "__name__", "") == "getch":
                emit({"c_call": "getch-enter", "c_return": "getch-return",
                      "c_exception": "getch-raise"}[event])
                if event != "c_call":
                    sys.setprofile(previous)

        # Leave an existing profiler alone rather than changing its semantics.
        if previous is None:
            sys.setprofile(profile)
        else:
            emit("profile-unavailable")
        try:
            return original(win)
        except BaseException as exc:
            emit("read-key-raise", exception=type(exc).__name__)
            raise
        finally:
            if previous is None:
                sys.setprofile(previous)

    tui.read_key = read_key


class InputDiagnostics:
    """Private sideband files, inherited only by the PTYProcess that owns them."""

    def __init__(self):
        self.events = tempfile.TemporaryFile(mode="a+b")
        try:
            self.stack = tempfile.TemporaryFile(mode="a+b")
        except BaseException:
            self.events.close()
            raise

    def child_setup(self) -> str:
        return ("from _pty_diagnostics import install as _install_input_diagnostics\n"
                f"_install_input_diagnostics({self.events.fileno()}, {self.stack.fileno()})\n")

    def pass_fds(self) -> tuple[int, int]:
        return self.events.fileno(), self.stack.fileno()

    def mark_sigint(self) -> None:
        _event(self.events.fileno(), "parent-sigint-send")

    def phases(self) -> list[dict]:
        # pread leaves the inherited writer's offset alone; no key values or
        # input buffers are ever recorded, including on a successful return.
        data = os.pread(self.events.fileno(), _LIMIT, 0)
        return [json.loads(line) for line in data.splitlines() if line.endswith(b"}")]

    def snapshot(self, process: subprocess.Popen) -> dict:
        phases = self.phases()
        native = [event["phase"] for event in phases if event["phase"].startswith("getch-")]
        result = {"at_ns": time.monotonic_ns(), "returncode": process.poll(),
                  "native_phase": native[-1] if native else "not-observed"}
        if result["returncode"] is not None or not sys.platform.startswith("linux"):
            result["proc"] = "exited-or-unavailable"
            return result
        # A live, unreaped direct child cannot have its PID reused. No process
        # scan, cmdline, environ, memory, kernel stack or ptrace access.
        proc = Path("/proc") / str(process.pid)
        try:
            with (proc / "status").open() as stream:
                fields = dict(line.split(":", 1) for line in stream.read(_LIMIT).splitlines() if ":" in line)
            if int(fields["PPid"]) != os.getpid():
                result["proc"] = "not-owned"
                return result
            result["proc"] = {name: fields[name].strip() for name in
                              ("State", "SigPnd", "ShdPnd", "SigBlk", "SigIgn", "SigCgt", "Threads")}
            with (proc / "wchan").open() as stream:
                wait = stream.read(128).strip()
            result["wchan"] = wait if wait.replace("_", "").isalnum() else "unavailable"
        except (OSError, KeyError, ValueError):
            result["proc_unavailable"] = True
        return result

    @staticmethod
    def _observe(process: subprocess.Popen) -> None:
        try:
            process.wait(timeout=_OBSERVE_SECONDS)
        except subprocess.TimeoutExpired:
            pass

    def on_timeout(self, child) -> dict:
        report = {"before_diagnostics": self.snapshot(child.process)}
        phases = self.phases()
        registered = next((event.get("dump_signal") for event in phases
                           if event["phase"] == "installed" and event.get("pid") == child.process.pid), None)
        report["dump_signal"] = "not-registered"
        if registered is not None and child.process.poll() is None:
            child.process.send_signal(registered)
            report["dump_signal"] = registered
            self._observe(child.process)
        report["after_dump_signal"] = self.snapshot(child.process)
        report["stack"] = os.pread(self.stack.fileno(), _LIMIT, 0).decode("utf-8", "replace")
        report["wake"] = "not-sent"
        if child.process.poll() is None and select.select([], [child.master], [], 0)[1]:
            # NUL has no launch/exit action on the card. This is an observation
            # after failure, never a retry: even an ensuing 130 must still fail.
            child.send(b"\x00")
            report["wake"] = "one-nul-byte"
            self._observe(child.process)
        report["after_wake"] = self.snapshot(child.process)
        report["phases"] = self.phases()
        return report

    def close(self) -> None:
        self.events.close()
        self.stack.close()
