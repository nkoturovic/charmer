"""Deterministic PTY smoke tests for quick confirm, curses form, and dumb mode."""

from __future__ import annotations

import copy
import errno
import fcntl
import json
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import types
import unittest
from pathlib import Path

from _catalog import FIXTURE_ROOT
from _layout import REPO_ROOT
from _pty_diagnostics import InputDiagnostics


# Child scripts load the frozen fixture asset root; their PYTHONPATH is the
# package src (never the fixture).
CATALOG_ROOT = FIXTURE_ROOT
SRC_ROOT = REPO_ROOT / "src"
TIMEOUT = 6.0
# Exit waits are load-tolerant: the full suite starves PTY children.
FINISH_TIMEOUT = 30.0
PLATFORMS = ("linux", "darwin")

# Darwin's pending-input state, not Linux's differently numbered PENDIN.
_DARWIN_PENDIN = 0x20000000


def _same_terminal_attrs(before, after) -> bool:
    """Compare the entire native vector without Python's numeric/type coercions."""

    if type(before) is not list or type(after) is not list or len(before) != 7 or len(after) != 7:
        return False
    if any(type(a) is not int or type(b) is not int or a != b for a, b in zip(before[:6], after[:6])):
        return False
    a, b = before[6], after[6]
    return (type(a) is list and type(b) is list and len(a) == len(b)
            and all(type(x) in (bytes, int) and type(x) is type(y) and x == y for x, y in zip(a, b)))


def _terminal_readback(before, after, *, platform: str = sys.platform) -> str:
    if _same_terminal_attrs(before, after):
        return "exact"
    if (platform == "darwin" and _same_terminal_attrs(before, before)
            and before[3] & termios.ICANON and not before[3] & _DARWIN_PENDIN):
        canonical = before.copy()
        canonical[3] |= _DARWIN_PENDIN
        if _same_terminal_attrs(canonical, after):
            return "darwin-pendin-added"
    return "mismatch"


class _TerminalRestoration:
    """Observe only the existing product save/restore calls, never query a tty.

    Caller code identities exclude admission/health getters and the IXON setter.
    Copies freeze observations; original arguments, results and exceptions pass
    through unchanged. Verification and metadata emission happen after teardown.
    """

    def __init__(self, before):
        self.before = copy.deepcopy(before)
        self.snapshots = []
        self.requests = []

    def install(self, tui) -> None:
        self._tui = tui
        self._get_attrs = tui._terminal_attrs
        self._set_attrs = tui.termios.tcsetattr
        self._save_code = tui.run_curses_on_streams.__code__
        restores = [code for code in self._save_code.co_consts
                    if isinstance(code, types.CodeType) and code.co_name == "restore_attrs"]
        assert len(restores) == 1, "product restoration caller identity"
        self._restore_code = restores[0]
        tui._terminal_attrs = self._snapshot
        tui.termios.tcsetattr = self._request

    def uninstall(self) -> None:
        self._tui._terminal_attrs = self._get_attrs
        self._tui.termios.tcsetattr = self._set_attrs

    def _snapshot(self, fd):
        attrs = self._get_attrs(fd)
        if sys._getframe(1).f_code is self._save_code:
            self.snapshots.append((fd, copy.deepcopy(attrs)))
        return attrs

    def _request(self, fd, when, attrs):
        if sys._getframe(1).f_code is not self._restore_code:
            return self._set_attrs(fd, when, attrs)
        request = [fd, when, copy.deepcopy(attrs), None]
        self.requests.append(request)
        try:
            return self._set_attrs(fd, when, attrs)
        except BaseException as error:
            request[3] = error
            raise

    @staticmethod
    def _outcome(error) -> str:
        if error is None:
            return "ok"
        if isinstance(error, termios.error) and error.args and type(error.args[0]) is int:
            return {errno.EBADF: "failed-ebadf", errno.EIO: "failed-eio",
                    errno.ENXIO: "failed-enxio"}.get(error.args[0], "failed-other")
        return "failed-other"

    def check_requests(self, *, expected=("ok", "ok")) -> None:
        assert len(self.before) == 2, "fixture baseline count"
        assert [fd for fd, _ in self.snapshots] == [0, 1], "product snapshot count/order"
        assert [request[0] for request in self.requests] == [0, 1], "restoration attempt count/order"
        assert len(expected) == 2, "expected setter outcome count"
        for slot, ((fd, snapshot), request, outcome) in enumerate(zip(self.snapshots, self.requests, expected)):
            assert type(fd) is int, "product snapshot descriptor type"
            assert _same_terminal_attrs(self.before[slot], snapshot), "fixture baseline/product snapshot mismatch"
            assert type(request[0]) is int and type(request[1]) is int, "restoration descriptor/timing type"
            assert request[1] == termios.TCSANOW, "original restoration timing"
            assert _same_terminal_attrs(self.before[slot], request[2]), "fixture baseline/restoration request mismatch"
            actual = self._outcome(request[3])
            if outcome == "ok":
                assert actual == "ok", "unexpected restoration setter failure"
            elif outcome == "closed-fd":
                assert actual == "failed-ebadf", "closed fd0 restoration must fail with EBADF"
                assert slot == 0, "only fd0 is deliberately closed"
            else:
                assert outcome == "hangup", "unknown expected setter outcome"
                # A hung-up tty can accept the request or reject it as unavailable;
                # neither a successful setter nor a readback is assumed here.
                assert actual in ("ok", "failed-eio", "failed-enxio"), "unexpected hung-up setter outcome"

    def check(self, after, *, platform: str = sys.platform, expected=("ok", "ok")) -> None:
        self.check_requests(expected=expected)
        assert type(after) is list and len(after) == 2, "readback slot structure"
        assert all(_terminal_readback(before, attrs, platform=platform) != "mismatch"
                   for before, attrs in zip(self.before, after)), "terminal configuration readback mismatch"

    def metadata(self, after=None, *, platform: str = sys.platform) -> dict:
        # Fixed-size classifications only: no vectors, c_cc values or exception text.
        return {
            "snapshot_count": len(self.snapshots),
            "attempt_count": len(self.requests),
            "snapshots": ["exact" if _same_terminal_attrs(before, snapshot) else "mismatch"
                          for before, (_, snapshot) in zip(self.before, self.snapshots[:2])],
            "requests": ["exact" if _same_terminal_attrs(before, request[2]) else "mismatch"
                         for before, request in zip(self.before, self.requests[:2])],
            "setters": [self._outcome(request[3]) for request in self.requests[:2]],
            "readback": (["not-observed"] * 2 if after is None else
                         [_terminal_readback(before, attrs, platform=platform)
                          for before, attrs in zip(self.before, after[:2])]),
        }


def _report_terminal_restore(case: unittest.TestCase, output: bytes, *, readback=None) -> dict:
    report = json.loads(_line_of(output, b"TTY_RESTORE="))
    if readback is not None:
        report["readback"] = readback
    print("TTY_RESTORE " + json.dumps(dict(case=case.id(), **report), sort_keys=True), file=sys.stderr)
    case.assertEqual(report["snapshot_count"], 2)
    case.assertEqual(report["attempt_count"], 2)
    case.assertEqual(report["snapshots"], ["exact", "exact"])
    case.assertEqual(report["requests"], ["exact", "exact"])
    case.assertNotIn("mismatch", report["readback"])
    return report


def _set_size(fd: int, rows: int, columns: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))


LINE_CONFIRM = "Enter launch · q quit: ".encode()


class PTYProcess:
    # Subclasses may own initialization without enabling optional diagnostics.
    diagnostics: InputDiagnostics | None = None
    timeout_diagnostics: dict | None = None

    def __init__(
        self,
        code: str,
        *,
        rows: int = 24,
        columns: int = 80,
        extra_env=None,
        extra_pythonpath=(),
        diagnostics: bool = False,
        restoration: bool = False,
    ):
        self.diagnostics = InputDiagnostics() if diagnostics else None
        self.timeout_diagnostics = None
        self.master, self.slave = pty.openpty()
        _set_size(self.slave, rows, columns)
        self.before = termios.tcgetattr(self.slave)
        env = dict(os.environ)
        env.update(
            {
                "PYTHONDONTWRITEBYTECODE": "1",
                # Screen children add the tests dir so they
                # can build their Runtime with _tui_fixture.
                "PYTHONPATH": os.pathsep.join([str(SRC_ROOT), *map(str, extra_pythonpath)]),
                "TERM": "xterm-256color",
            }
        )
        if extra_env:
            env.update(extra_env)
        if restoration:
            # This ordinary-return probe shares one slave on fd0/fd1. Reuse
            # the parent's existing baseline/readback, without child getters.
            code = (
                "import json\n"
                "from charmer import tui as _restore_tui\n"
                "from test_tui_pty import _TerminalRestoration\n"
                f"_restoration = _TerminalRestoration([{self.before!r}, {self.before!r}])\n"
                "_restoration.install(_restore_tui)\n"
            ) + code + (
                "\n_restoration.uninstall()\n"
                "print('TTY_RESTORE=' + json.dumps(_restoration.metadata()))\n"
                "_restoration.check_requests()\n"
            )
        if self.diagnostics is not None:
            code = self.diagnostics.child_setup() + code
        try:
            self.process = subprocess.Popen(
                [sys.executable, "-c", code],
                stdin=self.slave,
                stdout=self.slave,
                stderr=self.slave,
                close_fds=True,
                pass_fds=self.diagnostics.pass_fds() if self.diagnostics is not None else (),
                env=env,
            )
        except BaseException:
            os.close(self.master)
            os.close(self.slave)
            if self.diagnostics is not None:
                self.diagnostics.close()
            raise
        self.output = bytearray()

    def send(self, data: bytes) -> None:
        os.write(self.master, data)

    def resize(self, rows: int, columns: int) -> None:
        _set_size(self.slave, rows, columns)
        self.process.send_signal(signal.SIGWINCH)

    def read_until(self, needle: bytes, timeout: float = TIMEOUT) -> bytes:
        deadline = time.monotonic() + timeout
        while needle not in self.output and time.monotonic() < deadline:
            ready, _, _ = select.select([self.master], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                break
            try:
                chunk = os.read(self.master, 65536)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            self.output.extend(chunk)
        if needle not in self.output:
            raise AssertionError(f"did not observe {needle!r}; output={bytes(self.output)!r}")
        return bytes(self.output)

    def finish(self, timeout: float = FINISH_TIMEOUT) -> tuple[int, bytes, list]:
        deadline = time.monotonic() + timeout
        while self.process.poll() is None and time.monotonic() < deadline:
            ready, _, _ = select.select([self.master], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise
                if chunk:
                    self.output.extend(chunk)
        if self.process.poll() is None:
            try:
                if self.diagnostics is not None:
                    try:
                        self.timeout_diagnostics = self.diagnostics.on_timeout(self)
                    except Exception as exc:
                        self.timeout_diagnostics = {"diagnostic_error": type(exc).__name__}
            finally:
                # Crossing the original deadline is irrevocably a failure,
                # even if a diagnostic signal/wake lets the child exit 130.
                self.process.kill()
                self.process.wait(timeout=2)
            detail = f"; diagnostics={json.dumps(self.timeout_diagnostics)}" if self.diagnostics is not None else ""
            raise AssertionError(f"PTY child timed out; output={bytes(self.output)!r}{detail}")
        # Drain bytes available after exit.
        while True:
            ready, _, _ = select.select([self.master], [], [], 0)
            if not ready:
                break
            try:
                chunk = os.read(self.master, 65536)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            self.output.extend(chunk)
        after = termios.tcgetattr(self.slave)
        return self.process.returncode, bytes(self.output), after

    def close(self) -> None:
        try:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait(timeout=2)
        finally:
            os.close(self.master)
            os.close(self.slave)
            if self.diagnostics is not None:
                self.diagnostics.close()


class PTYProcessSubclassTests(unittest.TestCase):
    def child(self) -> PTYProcess:
        class WithoutBaseInit(PTYProcess):
            def __init__(self):
                self.master, self.slave = pty.openpty()
                self.output = bytearray()
                self.process = subprocess.Popen(
                    [sys.executable, "-c", "import time; print('SUBCLASS-WAIT', flush=True); time.sleep(60)"],
                    stdin=self.slave, stdout=self.slave, stderr=self.slave, close_fds=True,
                )

        child = WithoutBaseInit()

        def cleanup() -> None:
            try:
                if child.process.poll() is None:
                    child.process.kill()
                    child.process.wait(timeout=2)
            finally:
                for fd in (child.master, child.slave):
                    try:
                        os.close(fd)
                    except OSError as exc:
                        if exc.errno != errno.EBADF:
                            raise

        self.addCleanup(cleanup)
        child.read_until(b"SUBCLASS-WAIT")
        return child

    def test_close_without_base_init_reaps_the_child_with_a_bounded_wait(self) -> None:
        from unittest import mock

        child = self.child()
        with mock.patch.object(child.process, "wait", wraps=child.process.wait) as wait:
            try:
                child.close()
            except AttributeError as exc:
                self.fail(f"close requires optional base initialization: {exc}")
        wait.assert_called_once_with(timeout=2)
        self.assertEqual(child.process.returncode, -signal.SIGKILL)
        self.assertIsNone(child.diagnostics)
        self.assertIsNone(child.timeout_diagnostics)

    def test_timeout_without_base_init_still_fails_and_reaps_with_a_bounded_wait(self) -> None:
        from unittest import mock

        child = self.child()
        with mock.patch.object(child.process, "wait", wraps=child.process.wait) as wait:
            try:
                with self.assertRaisesRegex(AssertionError, "PTY child timed out"):
                    child.finish(timeout=0)
            except AttributeError as exc:
                self.fail(f"timeout requires optional base initialization: {exc}")
        wait.assert_called_once_with(timeout=2)
        self.assertEqual(child.process.returncode, -signal.SIGKILL)
        self.assertIsNone(child.diagnostics)
        self.assertIsNone(child.timeout_diagnostics)


class DumbTerminalPTYTests(unittest.TestCase):
    """TERM=dumb: no interactive editor; the $VISUAL/$EDITOR guidance
    (``compose edit`` is the ``profile edit`` alias, which needs an editor
    without the TUI editor)."""

    def _edit_code(self, temp: str, secret_file: Path) -> str:
        return f"""
import sys
from pathlib import Path
from charmer.cli.runtime import Runtime
from charmer.cli.entry import main
root = Path({str(CATALOG_ROOT)!r})
base = Path({temp!r})
env = {{'HOME': str(base/'home'), 'XDG_CONFIG_HOME': str(base/'config'), 'XDG_STATE_HOME': str(base/'state'), 'TERM': 'dumb', 'CHARMER_SECRET_ENV': {str(secret_file)!r}}}
runtime = Runtime(asset_root=root, environ=env, cwd=base/'project', launch_callback=lambda prepared: 0, served_models_callback=lambda gateway, token: (None, None), health_get=lambda base, path: 200)
raise SystemExit(main(['compose', 'edit', 'balanced'], runtime=runtime, input_stream=sys.stdin, output_stream=sys.stdout, interactive=True))
"""

    def test_dumb_terminal_prints_plan_and_editor_command(self) -> None:
        temp = tempfile.mkdtemp(prefix="charmer-pty-dumb-")
        self.addCleanup(__import__("shutil").rmtree, temp, True)
        child = PTYProcess(
            self._edit_code(temp, _secret_file(temp)),
            extra_env={"TERM": "dumb", "NO_COLOR": "1"},
        )
        self.addCleanup(child.close)
        child.read_until(b"$EDITOR")
        code, output, _ = child.finish()
        self.assertEqual(code, 1, output)
        self.assertIn(b"profile edit needs an editor: set $VISUAL or $EDITOR", output)
        self.assertNotIn(b"\x1b", output)


class QuickConfirmPTYTests(unittest.TestCase):
    """A bare launch on a real PTY: the card on a capable TERM, the line
    confirm on a dumb TERM and under ``--line``.
    The card children build their Runtime with ``_tui_fixture``."""

    def _runtime_code(self, temp: str, secret_file: Path) -> str:
        return f"""
import sys
from pathlib import Path
from charmer.cli.runtime import Runtime
from charmer.cli.entry import main
root = Path({str(CATALOG_ROOT)!r})
base = Path({temp!r})
env = {{'HOME': str(base/'home'), 'XDG_CONFIG_HOME': str(base/'config'), 'XDG_STATE_HOME': str(base/'state'), 'CHARMER_SECRET_ENV': {str(secret_file)!r}}}
def fake(prepared):
    print('FAKE_LAUNCH=' + prepared.result.session_action.kind, flush=True)
    return 0
runtime = Runtime(asset_root=root, environ=env, cwd=base/'project', launch_callback=fake, doctor_binary_callback=lambda contract: ([], ['fixture binary verified.']), doctor_callback=lambda _runtime: [], served_models_callback=lambda gateway, token: (None, None), health_get=lambda base, path: 200)
raise SystemExit(main([], runtime=runtime, input_stream=sys.stdin, output_stream=sys.stdout, interactive=True))
"""

    def _temp_runtime(self, prefix: str) -> tuple[str, Path]:
        temp = tempfile.mkdtemp(prefix=prefix)
        self.addCleanup(__import__("shutil").rmtree, temp, True)
        return temp, _secret_file(temp)

    def _card(self, prefix: str, argv=()) -> PTYProcess:
        temp = tempfile.mkdtemp(prefix=prefix)
        self.addCleanup(__import__("shutil").rmtree, temp, True)
        child = PTYProcess(_fixture_card_code(temp, list(argv)), extra_pythonpath=(TESTS_ROOT,))
        self.addCleanup(child.close)
        return child

    def test_line_confirm_one_enter_uses_fake_launch(self) -> None:
        temp, secret_file = self._temp_runtime("charmer-pty-runtime-")
        child = PTYProcess(self._runtime_code(temp, secret_file), extra_env={"TERM": "dumb"})
        self.addCleanup(child.close)
        child.read_until(LINE_CONFIRM)
        child.send(b"\n")
        returncode, output, _ = child.finish()
        self.assertEqual(returncode, 0, output)
        # The line body is the card's line form.
        self.assertIn("profile: balanced · follows profile".encode(), output)
        self.assertIn(b"FAKE_LAUNCH=fresh", output)

    def test_card_on_a_capable_terminal_launches_and_restores(self) -> None:
        # A capable TERM gets the card.
        child = self._card("charmer-pty-card-")
        child.read_until(CARD_READY, FIXTURE_TIMEOUT)
        child.send(b"\n")
        returncode, output, after = child.finish()
        self.assertEqual(returncode, 0, output)
        self.assertIn(b"FAKE_LAUNCH=fresh", output)
        self.assertNotIn(LINE_CONFIRM, output)
        _restored(self, child, after)

    def test_card_esc_never_launches(self) -> None:
        child = self._card("charmer-pty-card-esc-")
        child.read_until(CARD_READY, FIXTURE_TIMEOUT)
        child.send(b"\x1b")
        returncode, output, after = child.finish()
        self.assertEqual(returncode, 0, output)
        self.assertIn(b"Nothing was launched.", output)
        self.assertNotIn(b"FAKE_LAUNCH", output)
        _restored(self, child, after)

    def test_line_flag_keeps_the_line_confirm(self) -> None:
        child = self._card("charmer-pty-card-line-", ["--line"])
        child.read_until(LINE_CONFIRM, FIXTURE_TIMEOUT)
        child.send(b"q\n")
        returncode, output, after = child.finish()
        self.assertEqual(returncode, 0, output)
        self.assertIn(b"Nothing was launched.", output)
        self.assertNotIn(b"FAKE_LAUNCH", output)
        self.assertNotIn(CARD_READY, output)
        _restored(self, child, after)

    def test_card_help_modal_shows_the_guarantee_panel(self) -> None:
        # The 3.0 form of the deleted
        # quick-confirm details-and-guarantee-modal PTY test.
        from charmer import cli

        child = self._card("charmer-pty-card-help-")
        child.read_until(CARD_READY, FIXTURE_TIMEOUT)
        child.send(b"?")
        child.read_until("launch card — help".encode())
        # The first help line wraps at 80 columns: its opening words.
        child.read_until(" ".join(cli.QUICK_HELP.splitlines()[0].split()[:6]).encode())
        child.read_until(cli.QUICK_HELP.splitlines()[1].encode())
        # The help wraps and scrolls; at 80x24 Space pages down to the panel.
        child.send(b"    ")
        child.read_until(b"Native workflows (ultracode):")
        mark = len(child.output)
        child.send(b"\x1b")  # closes the Modal: the card is back
        _read_after(child, mark, b"Status  Ready")
        child.send(b"\x1b")
        returncode, output, after = child.finish()
        self.assertEqual(returncode, 0, output)
        self.assertIn(b"Nothing was launched.", output)
        self.assertNotIn(b"FAKE_LAUNCH", output)
        _restored(self, child, after)


if __name__ == "__main__":
    unittest.main()


def _bare_launch_code(temp: str, secret_file: Path) -> str:
    return f"""
import sys
from pathlib import Path
from charmer.cli.runtime import Runtime
from charmer.cli.entry import main
root = Path({str(CATALOG_ROOT)!r})
base = Path({temp!r})
env = {{'HOME': str(base/'home'), 'XDG_CONFIG_HOME': str(base/'config'), 'XDG_STATE_HOME': str(base/'state'), 'TERM': 'dumb', 'CHARMER_SECRET_ENV': {str(secret_file)!r}}}
def fake(prepared):
    print('FAKE_LAUNCH=' + prepared.result.session_action.kind, flush=True)
    return 0
runtime = Runtime(asset_root=root, environ=env, cwd=base/'project', launch_callback=fake, doctor_binary_callback=lambda contract: ([], ['fixture binary verified.']), doctor_callback=lambda _runtime: [], served_models_callback=lambda gateway, token: (None, None), health_get=lambda base, path: 200)
raise SystemExit(main([], runtime=runtime))
"""


def _secret_file(temp: str) -> Path:
    secret_dir = Path(temp) / "secrets"
    secret_dir.mkdir(mode=0o700, exist_ok=True)
    secret_file = secret_dir / "claude.env"
    secret_file.write_text("KIMI_CLAUDE_API_KEY=pty-test-dummy\n")
    secret_file.chmod(0o600)
    return secret_file


class _NoCttyChild:
    """Child in a new session without a controlling terminal, TTY stdio."""

    def __init__(self, code: str, *, extra_env=None):
        self.master, slave = pty.openpty()
        _set_size(slave, 24, 80)
        env = dict(os.environ)
        env.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(SRC_ROOT), "TERM": "dumb"})
        if extra_env:
            env.update(extra_env)
        self.process = subprocess.Popen(
            [sys.executable, "-c", code],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            preexec_fn=os.setsid,  # new session: no controlling terminal
            close_fds=True,
            env=env,
        )
        os.close(slave)
        self.output = bytearray()

    def read_until(self, needle: bytes, timeout: float = TIMEOUT) -> bytes:
        deadline = time.monotonic() + timeout
        while needle not in self.output and time.monotonic() < deadline:
            ready, _, _ = select.select([self.master], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                break
            try:
                chunk = os.read(self.master, 65536)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            self.output.extend(chunk)
        if needle not in self.output:
            raise AssertionError(f"did not observe {needle!r}; output={bytes(self.output)!r}")
        return bytes(self.output)

    def send(self, data: bytes) -> None:
        os.write(self.master, data)

    def finish(self, timeout: float = FINISH_TIMEOUT) -> tuple[int, bytes]:
        deadline = time.monotonic() + timeout
        while self.process.poll() is None and time.monotonic() < deadline:
            ready, _, _ = select.select([self.master], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        # Every slave fd is closed, so the child is inside exit, but it
                        # may not be waitable yet (its fds are released before it becomes
                        # a zombie): reap it within the deadline instead of polling once
                        # (a once-flaky reap).
                        try:
                            self.process.wait(timeout=max(0.0, deadline - time.monotonic()))
                        except subprocess.TimeoutExpired:
                            pass
                        break
                    raise
                if chunk:
                    self.output.extend(chunk)
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=2)
            raise AssertionError(f"child timed out; output={bytes(self.output)!r}")
        try:
            while True:
                chunk = os.read(self.master, 65536)
                if not chunk:
                    break
                self.output.extend(chunk)
        except OSError:
            pass
        os.close(self.master)
        return self.process.returncode, bytes(self.output)

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=2)


class _CttyPipesChild:
    """Child with the slave as controlling terminal but piped stdin/stdout.

    Proves /dev/tty is genuinely preferred: interaction must flow over the
    PTY master (the child's controlling terminal), never over the pipes.
    """

    def __init__(self, code: str, *, extra_env=None):
        self.master, slave = pty.openpty()
        _set_size(slave, 24, 80)
        env = dict(os.environ)
        env.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(SRC_ROOT), "TERM": "dumb"})
        if extra_env:
            env.update(extra_env)

        def _attach_ctty() -> None:
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

        self.process = subprocess.Popen(
            [sys.executable, "-c", code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            preexec_fn=_attach_ctty,
            # Preserve a slave through exec: on Darwin closing the last one
            # can hang up the terminal before the child opens /dev/tty.
            pass_fds=(slave,),
            close_fds=True,
            env=env,
        )
        os.close(slave)
        self.output = bytearray()

    def read_until(self, needle: bytes, timeout: float = TIMEOUT) -> bytes:
        deadline = time.monotonic() + timeout
        while needle not in self.output and time.monotonic() < deadline:
            ready, _, _ = select.select([self.master], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                continue
            try:
                chunk = os.read(self.master, 65536)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    # Transient: slave side fully closed between spawn and the
                    # child opening /dev/tty; data queues on the master later.
                    time.sleep(0.05)
                    continue
                raise
            if not chunk:
                break
            self.output.extend(chunk)
        if needle not in self.output:
            raise AssertionError(f"did not observe {needle!r}; output={bytes(self.output)!r}")
        return bytes(self.output)

    def send(self, data: bytes) -> None:
        os.write(self.master, data)

    def finish(self, timeout: float = TIMEOUT) -> tuple[int, bytes, bytes]:
        deadline = time.monotonic() + timeout
        while self.process.poll() is None and time.monotonic() < deadline:
            ready, _, _ = select.select([self.master], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        time.sleep(0.05)
                        continue
                    raise
                if chunk:
                    self.output.extend(chunk)
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=2)
            raise AssertionError(f"child timed out; output={bytes(self.output)!r}")
        while True:
            ready, _, _ = select.select([self.master], [], [], 0.2)
            if not ready:
                break
            try:
                chunk = os.read(self.master, 65536)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            self.output.extend(chunk)
        os.close(self.master)
        pipe_out = self.process.stdout.read() if self.process.stdout else b""
        if self.process.stdin is not None:
            self.process.stdin.close()
        if self.process.stdout is not None:
            self.process.stdout.close()
        return self.process.returncode, bytes(self.output), pipe_out

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=2)
        try:
            os.close(self.master)
        except OSError:
            pass
        if self.process.stdin is not None:
            self.process.stdin.close()
        if self.process.stdout is not None:
            self.process.stdout.close()


class SessionsAndTransitionPTYTests(unittest.TestCase):
    """The `sessions transition` alias (the relaunch executor) on a real PTY.

    The tty `sessions list` / bare `-r` tests are in
    :class:`SessionsScreenPTYTests` (the curses sessions screen on a
    capable terminal, with `--line` twins that keep the line flows).
    """

    def _temp(self, prefix: str) -> tuple[str, Path]:
        temp = tempfile.mkdtemp(prefix=prefix)
        self.addCleanup(__import__("shutil").rmtree, temp, True)
        return temp, _secret_file(temp)

    def _sessions_code(self, temp: str, secret_file: Path, argv: list[str]) -> str:
        return f"""
import copy, sys
from pathlib import Path
from charmer import sessions, scope as scope_mod
from charmer.cli.types import LaunchTarget
from charmer.cli.runtime import Runtime
from charmer.cli.entry import main
from charmer.cli.session_facts import _native_project_slug
root = Path({str(CATALOG_ROOT)!r})
base = Path({temp!r})
env = {{'HOME': str(base/'home'), 'XDG_CONFIG_HOME': str(base/'config'), 'XDG_STATE_HOME': str(base/'state'), 'CHARMER_SECRET_ENV': {str(secret_file)!r}}}
def fake(prepared):
    print('FAKE_LAUNCH=' + prepared.result.session_action.kind, flush=True)
    return 0
runtime = Runtime(asset_root=root, environ=env, cwd=base/'project', launch_callback=fake, doctor_binary_callback=lambda contract: ([], ['fixture binary verified.']), doctor_callback=lambda _runtime: [], served_models_callback=lambda gateway, token: (None, None), health_get=lambda base, path: 200)
(base/'project').mkdir(parents=True, exist_ok=True)  # records always have an existing cwd
MID = '11111111-1111-4111-8111-111111111111'
sessions.SessionStore.new_id = lambda _store: MID
runtime.profiles.install_seeds()
balanced = runtime.profiles.load('balanced')
prepared = runtime.prepare(LaunchTarget('profile', balanced, 'balanced', True, 'Profile balanced'), action='fresh', passthrough=[])
record = dict(prepared.record, mutation_token=sessions.new_mutation_token())
sessions.ensure_state_v4(runtime.session_store.root)
scope_mod.swap_scope(runtime.session_store.root, MID, prepared.result.scope_plan)
runtime.session_store.save(record)
transcript = base/'home'/'.claude'/'projects'/_native_project_slug(runtime.cwd)/(MID + '.jsonl')
transcript.parent.mkdir(parents=True, exist_ok=True)
transcript.touch()
shifted = copy.deepcopy(balanced)
shifted['name'] = 'shifted'
shifted['lead'] = {{'model': 'sol', 'effort': 'xhigh'}}
runtime.profiles.save(shifted)
raise SystemExit(main({argv!r}, runtime=runtime, input_stream=sys.stdin, output_stream=sys.stdout, interactive=True))
"""

    def test_transition_confirm_relaunches(self) -> None:
        temp, secret_file = self._temp("charmer-pty-transition-")
        child = PTYProcess(
            self._sessions_code(
                temp,
                secret_file,
                [
                    "sessions",
                    "transition",
                    "11111111-1111-4111-8111-111111111111",
                    "--composition",
                    "shifted",
                ],
            ),
            extra_env={"TERM": "dumb"},
        )
        self.addCleanup(child.close)
        child.read_until(b"Has the target process exited? [y/N] ")
        child.send(b"y\n")
        code, output, _ = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(b"FAKE_LAUNCH=resume", output)

    def test_transition_curses_modal_shows_the_diff_and_relaunches(self) -> None:
        # _transition_confirm keeps its curses modal (TransitionTuiScreenTests
        # stays); the preview is the lineup diff.
        temp, secret_file = self._temp("charmer-pty-transition-curses-")
        child = PTYProcess(
            self._sessions_code(
                temp,
                secret_file,
                [
                    "sessions",
                    "transition",
                    "11111111-1111-4111-8111-111111111111",
                    "--composition",
                    "shifted",
                ],
            )
        )
        self.addCleanup(child.close)
        child.read_until(b"semantic diff")
        child.read_until(b"relaunch session 11111111 with profile shifted")
        child.send(b"\n")
        child.read_until(b"EXITED (not merely idle)")
        child.send(b"\n")
        code, output, _ = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(b"FAKE_LAUNCH=resume", output)

    def test_transition_no_never_launches(self) -> None:
        temp, secret_file = self._temp("charmer-pty-transition-n-")
        child = PTYProcess(
            self._sessions_code(
                temp,
                secret_file,
                [
                    "sessions",
                    "transition",
                    "11111111-1111-4111-8111-111111111111",
                    "--composition",
                    "shifted",
                ],
            ),
            extra_env={"TERM": "dumb"},
        )
        self.addCleanup(child.close)
        child.read_until(b"Has the target process exited? [y/N] ")
        child.send(b"\n")
        code, output, _ = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(b"Nothing was launched.", output)
        self.assertNotIn(b"FAKE_LAUNCH", output)


class BareLaunchAutoDetectionPTYTests(unittest.TestCase):
    """Bare launch auto-detection with no injected interactive flag."""

    def test_no_controlling_terminal_still_reaches_the_line_confirm(self) -> None:
        temp = tempfile.mkdtemp(prefix="charmer-pty-bare-")
        self.addCleanup(__import__("shutil").rmtree, temp, True)
        child = _NoCttyChild(_bare_launch_code(temp, _secret_file(temp)))
        self.addCleanup(child.close)
        # /dev/tty cannot be opened here (no controlling terminal), but both
        # standard streams are TTYs, so the fallback must engage.
        child.read_until(LINE_CONFIRM)
        child.send(b"q\n")  # cancel at the line confirm; never launches
        returncode, output = child.finish()
        self.assertEqual(returncode, 0, output)
        self.assertNotIn(b"FAKE_LAUNCH", output)

    def test_controlling_terminal_slave_stays_open_across_exec(self) -> None:
        # Darwin can hang up the controlling terminal when the last slave fd
        # closes. Keep a slave open before product code first opens /dev/tty.
        child = _CttyPipesChild(
            "import os\n"
            "fds = [int(name) for name in os.listdir('/dev/fd') if name.isdigit()]\n"
            "print(any(fd > 2 and os.isatty(fd) for fd in fds))\n")
        self.addCleanup(child.close)
        output, _ = child.process.communicate(timeout=TIMEOUT)
        self.assertEqual(child.process.returncode, 0, output)
        self.assertEqual(output.strip(), b"True", "the harness closed every slave descriptor before exec")

    def test_controlling_terminal_pipes_stdio_still_uses_dev_tty(self) -> None:
        temp = tempfile.mkdtemp(prefix="charmer-pty-ctty-")
        self.addCleanup(__import__("shutil").rmtree, temp, True)
        child = _CttyPipesChild(_bare_launch_code(temp, _secret_file(temp)))
        self.addCleanup(child.close)
        # stdin/stdout are pipes, but /dev/tty exists: the line confirm must
        # render on the controlling terminal (PTY master), not the pipes.
        child.read_until(LINE_CONFIRM)
        child.send(b"q\n")  # cancel at the line confirm; never launches
        returncode, tty_output, pipe_output = child.finish()
        self.assertEqual(returncode, 0, tty_output)
        self.assertIn(LINE_CONFIRM, tty_output)
        self.assertNotIn(LINE_CONFIRM, pipe_output)
        self.assertNotIn(b"FAKE_LAUNCH", tty_output + pipe_output)

    def test_capable_terminal_card_routes_to_controlling_terminal(self) -> None:
        # A capable TERM gets the card, on
        # /dev/tty (the PTY master), never on the pipes.
        from charmer import catalog

        temp = tempfile.mkdtemp(prefix="charmer-pty-ctty-curses-")
        self.addCleanup(__import__("shutil").rmtree, temp, True)
        child = _CttyPipesChild(
            _fixture_card_code(temp, [], streams=False),
            extra_env={
                "TERM": "xterm-256color",
                "PYTHONPATH": os.pathsep.join([str(SRC_ROOT), str(TESTS_ROOT)]),
            },
        )
        self.addCleanup(child.close)
        profile_line = f"profile: {_expected_default(self)}".encode()
        child.read_until(CARD_READY, FIXTURE_TIMEOUT)
        child.send(b"\x1b")
        returncode, tty_output, pipe_output = child.finish(FIXTURE_TIMEOUT)
        self.assertEqual(returncode, 0, tty_output)
        self.assertIn(profile_line, tty_output)
        self.assertNotIn(profile_line, pipe_output)
        self.assertNotIn(b"FAKE_LAUNCH", tty_output + pipe_output)

    def test_genuine_pipe_fails_with_explicit_profile_error(self) -> None:
        temp = tempfile.mkdtemp(prefix="charmer-pty-pipe-")
        self.addCleanup(__import__("shutil").rmtree, temp, True)
        env = dict(os.environ)
        env.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(SRC_ROOT), "TERM": "dumb"})
        process = subprocess.Popen(
            [sys.executable, "-c", _bare_launch_code(temp, _secret_file(temp))],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            preexec_fn=os.setsid,  # hermetic: no controlling terminal at all
            env=env,
        )
        self.addCleanup(lambda: process.poll() is None and (process.kill(), process.wait()))
        output, _ = process.communicate(timeout=TIMEOUT * 2)
        self.assertEqual(process.returncode, 2, output)
        self.assertIn(b"noninteractive launch requires --profile NAME", output)
        self.assertNotIn(LINE_CONFIRM, output)
        self.assertNotIn(b"FAKE_LAUNCH", output)


# These children really open the /dev/tty alias, with piped stdio. Compare
# descriptor identities dynamically, never by an inherited fixture fd number
# or the platform's device ids.
_ALIAS_TTY_SNAPSHOT = """
import json, os, termios
from charmer import tui
from test_tui_pty import _TerminalRestoration

def fd_identity(fd):
    info = os.fstat(fd)
    return info.st_dev, info.st_ino, info.st_rdev, info.st_mode

assert not os.isatty(0) and not os.isatty(1), 'fixture requires piped stdio'
alias_streams = (open('/dev/tty', 'r', encoding='utf-8', buffering=1),
                 open('/dev/tty', 'w', encoding='utf-8', buffering=1))
alias_before = [termios.tcgetattr(stream.fileno()) for stream in alias_streams]
stdio_before = [fd_identity(fd) for fd in (0, 1)]
restoration = _TerminalRestoration(alias_before)
restoration.install(tui)

def assert_alias_restored(*, expected=('ok', 'ok')):
    restoration.uninstall()
    assert [fd_identity(fd) for fd in (0, 1)] == stdio_before, 'stdio descriptor restoration'
    alias_after = [termios.tcgetattr(stream.fileno()) for stream in alias_streams]
    print('TTY_RESTORE=' + json.dumps(restoration.metadata(alias_after)))
    restoration.check(alias_after, expected=expected)
    assert tui._INPUT_STATE is None, 'input state cleanup'
    print('ALIAS_RESTORED=True', flush=True)
"""


def _alias_reader_code(*, count: int = 1, close_input: bool = False, ignore_hup: bool = False) -> str:
    """A native alias reader; the original stdout pipe keeps primary failures observable."""

    return f"""
import codecs, json, locale, signal, sys
{_ALIAS_TTY_SNAPSHOT}
for name in ('C.UTF-8', 'en_US.UTF-8', 'UTF-8'):
    try:
        locale.setlocale(locale.LC_CTYPE, name)
        break
    except locale.Error:
        continue
assert codecs.lookup(locale.getencoding()).name == 'utf-8'
if {ignore_hup!r}:
    # Only this owned hangup fixture ignores SIGHUP: death by that signal
    # would prove nothing about the reader's handling of a hung-up alias.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
report_fd = os.dup(1)
keys = []
def app(win):
    state = tui._input_state(win)
    assert state.fd == 0
    assert [fd_identity(fd) for fd in (0, 1)] == [fd_identity(stream.fileno()) for stream in alias_streams]
    tui.safe_add(win, 0, 2, 'ALIAS-INPUT-READY')
    win.refresh()
    if {close_input!r}:
        os.close(state.fd)
    for _ in range({count}):
        try:
            key = tui.read_key(win)
        except tui.CursesError as error:
            # Teardown can itself fail on a closed or hung-up terminal.
            os.write(report_fd, ('PRIMARY_INPUT_ERROR=' + str(error) + '\\n').encode())
            raise
        keys.append([key.kind, key.ch])
        os.write(1, ('STEP=' + str(len(keys)) + '\\n').encode())
code = 0
try:
    try:
        tui.run_curses_on_streams(app, *alias_streams, palette=tui.MONO_PALETTE)
    except tui.CursesError as error:
        print('INPUT_ERROR=' + str(error), flush=True)
        code = 1
    assert [fd_identity(fd) for fd in (0, 1)] == stdio_before, 'stdio descriptor restoration'
    assert tui._INPUT_STATE is None, 'input state cleanup'
    print('STDIO_RESTORED=True', flush=True)
    if not {ignore_hup!r}:
        assert_alias_restored(expected={('closed-fd', 'ok') if close_input else ('ok', 'ok')!r})
    else:
        restoration.uninstall()
        print('TTY_RESTORE=' + json.dumps(restoration.metadata()))
        restoration.check_requests(expected=('hangup', 'hangup'))
    print('KEYS=' + json.dumps(keys), flush=True)
    print('EXIT=' + str(code), flush=True)
finally:
    os.close(report_fd)
    for stream in alias_streams:
        stream.close()
raise SystemExit(code)
"""


class AliasInputHealthPTYTests(unittest.TestCase):
    """Real alias idle, loss and cancellation controls; no synthetic poll events."""

    def child(self, code: str) -> _CttyPipesChild:
        child = _CttyPipesChild(code, extra_env={
            "TERM": "xterm-256color", "ESCDELAY": "25", "NO_COLOR": "1",
            "PYTHONPATH": os.pathsep.join([str(SRC_ROOT), str(TESTS_ROOT)]),
        })
        self.addCleanup(child.close)
        return child

    def reader(self, **kwargs) -> _CttyPipesChild:
        child = self.child(_alias_reader_code(**kwargs))
        child.read_until(b"ALIAS-INPUT-READY", FIXTURE_TIMEOUT)
        return child

    def finish_keys(self, child: _CttyPipesChild, expected: list) -> None:
        code, tty_output, pipe_output = child.finish(TIMEOUT)
        output = tty_output + pipe_output
        _report_terminal_restore(self, pipe_output)
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(_line_of(pipe_output, b"KEYS=")), expected)
        self.assertIn(b"ALIAS_RESTORED=True", pipe_output)
        self.assertNotIn(b"PRIMARY_INPUT_ERROR=", output)
        self.assertNotIn(b"FAKE_LAUNCH", output)
        self.assertNotIn(b"Traceback", output)

    def test_hung_up_alias_fails_in_the_reader_not_by_sighup_or_timeout(self) -> None:
        from charmer import tui

        child = self.reader(ignore_hup=True)
        os.close(child.master)
        child.master = -1  # cleanup must not close a subsequently reused fd
        try:
            child.process.wait(timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            self.fail("hung-up alias reader did not fail within the bound; a timeout is not input-loss evidence")
        pipe_output = child.process.stdout.read()
        _report_terminal_restore(self, pipe_output)
        self.assertEqual(child.process.returncode, 1, pipe_output)
        self.assertIn(b"PRIMARY_INPUT_ERROR=", pipe_output)
        self.assertIn(_line_of(pipe_output, b"PRIMARY_INPUT_ERROR="),
                      (tui.INPUT_UNAVAILABLE, tui.INPUT_EARLY_ERROR))
        self.assertIn(b"STDIO_RESTORED=True", pipe_output)
        self.assertEqual(json.loads(_line_of(pipe_output, b"KEYS=")), [])
        self.assertNotIn(b"FAKE_LAUNCH", pipe_output)
        self.assertNotIn(b"Traceback", pipe_output)

    def test_closed_active_alias_fd_reports_primary_input_failure(self) -> None:
        from charmer import tui

        child = self.reader(close_input=True)
        code, tty_output, pipe_output = child.finish(TIMEOUT)
        output = tty_output + pipe_output
        self.assertEqual(code, 1, output)
        # A teardown failure alone is not evidence that the reader rejected
        # the closed active fd: require its original error on the pipe.
        report = _report_terminal_restore(self, pipe_output)
        self.assertEqual(report["setters"], ["failed-ebadf", "ok"])
        self.assertIn(b"PRIMARY_INPUT_ERROR=", pipe_output)
        self.assertEqual(_line_of(pipe_output, b"PRIMARY_INPUT_ERROR="), tui.INPUT_UNAVAILABLE)
        self.assertEqual(_line_of(pipe_output, b"\nINPUT_ERROR="), tui.INPUT_UNAVAILABLE)
        self.assertIn(b"ALIAS_RESTORED=True", pipe_output)
        self.assertEqual(json.loads(_line_of(pipe_output, b"KEYS=")), [])
        self.assertNotIn(b"FAKE_LAUNCH", output)
        self.assertNotIn(b"Traceback", output)

    def test_interrupt_waiting_on_alias_exits_130_without_wake_and_restores_exactly(self) -> None:
        # Exact requests/configuration; Darwin's qualified PENDIN readback is
        # reported separately, not asserted to be full-vector equality.
        temp = tempfile.mkdtemp(prefix="charmer-pty-alias-interrupt-")
        self.addCleanup(__import__("shutil").rmtree, temp, True)
        child = self.child(_fixture_card_code(
            temp, [], streams=False, setup=_ALIAS_TTY_SNAPSHOT,
            after="assert_alias_restored()\nfor stream in alias_streams:\n    stream.close()"))
        child.read_until(CARD_READY, FIXTURE_TIMEOUT)
        time.sleep(0.25)  # ordinary idle slices on the alias, not another key
        self.assertIsNone(child.process.poll(), bytes(child.output))
        child.process.send_signal(signal.SIGINT)  # the only wake; no input bytes
        code, tty_output, pipe_output = child.finish(TIMEOUT)
        output = tty_output + pipe_output
        _report_terminal_restore(self, pipe_output)
        self.assertEqual(code, 130, output)
        self.assertIn(b"EXIT=130", pipe_output)
        self.assertIn(b"nothing was launched", output)
        self.assertIn(b"ALIAS_RESTORED=True", pipe_output)
        self.assertNotIn(b"FAKE_LAUNCH", output)
        self.assertNotIn(b"Traceback", output)

    def test_split_utf8_across_alias_idle_slices_preserves_the_character(self) -> None:
        child = self.reader()
        child.send(b"\xe7")
        time.sleep(0.25)
        self.assertIsNone(child.process.poll(), bytes(child.output))
        child.send(b"\x95\x8c")
        self.finish_keys(child, [["char", "界"]])

    def test_alt_unicode_across_alias_peek_expiry_keeps_order(self) -> None:
        child = self.reader(count=2)
        child.send(b"\x1b\xc3")
        child.read_until(b"STEP=1", FIXTURE_TIMEOUT)  # Esc's peek has expired
        child.send(b"\xb6")
        self.finish_keys(child, [["esc", ""], ["char", "ö"]])


class TerminalRestorationContractTests(unittest.TestCase):
    """Pure vectors and mocked existing calls; no PTY or native input needed."""

    def observation(self) -> _TerminalRestoration:
        attrs = [1 << 70, 3, termios.CS8, termios.ICANON | termios.ECHO | (1 << 70), 13, 13,
                 [bytes((i,)) if i % 2 else i for i in range(termios.NCCS)]]
        other = copy.deepcopy(attrs)
        other[1] ^= 0x80  # Distinct terminals must not exchange their snapshots.
        observed = _TerminalRestoration([attrs, other])
        observed.snapshots = [(fd, copy.deepcopy(before)) for fd, before in enumerate(observed.before)]
        observed.requests = [[fd, termios.TCSANOW, copy.deepcopy(before), None]
                             for fd, before in enumerate(observed.before)]
        return observed

    def test_exact_vectors_remain_valid_on_linux_and_darwin(self) -> None:
        for platform in ("linux", "darwin"):
            for canonical in (False, True):
                for pending in (False, True):
                    with self.subTest(platform=platform, canonical=canonical, pending=pending):
                        before = self.observation().before[0]
                        before[3] = (before[3] | termios.ICANON) if canonical else (before[3] & ~termios.ICANON)
                        if pending:
                            before[3] |= _DARWIN_PENDIN
                        self.assertEqual(_terminal_readback(before, copy.deepcopy(before), platform=platform), "exact")

    def test_only_new_darwin_canonical_pendin_is_qualified(self) -> None:
        observed = self.observation()
        after = copy.deepcopy(observed.before)
        for attrs in after:
            attrs[3] |= _DARWIN_PENDIN
        observed.check(after, platform="darwin")
        self.assertEqual(observed.metadata(after, platform="darwin")["readback"], ["darwin-pendin-added"] * 2)
        for platform in ("linux", "freebsd", "win32"):
            with self.subTest(platform=platform), self.assertRaisesRegex(AssertionError, "readback mismatch"):
                observed.check(after, platform=platform)

    def test_pendin_removal_and_noncanonical_addition_are_rejected(self) -> None:
        for platform in ("linux", "darwin"):
            before = self.observation().before[0]
            after = copy.deepcopy(before)
            before[3] |= _DARWIN_PENDIN
            self.assertEqual(_terminal_readback(before, after, platform=platform), "mismatch")
            before[3] &= ~(_DARWIN_PENDIN | termios.ICANON)
            after = copy.deepcopy(before)
            after[3] |= _DARWIN_PENDIN
            self.assertEqual(_terminal_readback(before, after, platform=platform), "mismatch")

    def test_every_other_flag_bit_including_unknown_bits_is_rejected(self) -> None:
        before = self.observation().before[0]
        for pending in (0, _DARWIN_PENDIN):
            for slot in range(4):
                for bit in (*range(64), 70, 127):
                    if slot == 3 and 1 << bit == _DARWIN_PENDIN:
                        continue
                    with self.subTest(pending=bool(pending), slot=slot, bit=bit):
                        after = copy.deepcopy(before)
                        after[3] |= pending
                        after[slot] ^= 1 << bit
                        self.assertEqual(_terminal_readback(before, after, platform="darwin"), "mismatch")

    def test_both_speeds_and_all_scalar_types_are_exact(self) -> None:
        before = self.observation().before[0]
        for slot in range(6):
            for value in (float(before[slot]), before[slot] + 1):
                with self.subTest(slot=slot, kind=type(value).__name__):
                    after = copy.deepcopy(before)
                    after[3] |= _DARWIN_PENDIN
                    after[slot] = value
                    self.assertEqual(_terminal_readback(before, after, platform="darwin"), "mismatch")

    def test_each_control_character_value_and_type_are_exact(self) -> None:
        before = self.observation().before[0]
        for slot, value in enumerate(before[6]):
            changed = bytes((value[0] ^ 1,)) if type(value) is bytes else value ^ 1
            equal_other_type = bytearray(value) if type(value) is bytes else float(value)
            for replacement in (changed, equal_other_type, [value]):
                with self.subTest(slot=slot, kind=type(replacement).__name__):
                    after = copy.deepcopy(before)
                    after[3] |= _DARWIN_PENDIN
                    after[6][slot] = replacement
                    self.assertEqual(_terminal_readback(before, after, platform="darwin"), "mismatch")

    def test_full_vector_and_control_character_structure_are_exact(self) -> None:
        class ListSubclass(list):
            pass

        before = self.observation().before[0]
        pending = copy.deepcopy(before)
        pending[3] |= _DARWIN_PENDIN
        variants = [tuple(pending), ListSubclass(pending), pending[:-1], pending + [0]]
        for cc in (tuple(before[6]), ListSubclass(before[6]), before[6][:-1], before[6] + [b"x"], None):
            after = copy.deepcopy(pending)
            after[6] = cc
            variants.append(after)
        for after in variants:
            with self.subTest(kind=type(after).__name__):
                self.assertEqual(_terminal_readback(before, after, platform="darwin"), "mismatch")

    def test_python_equal_but_differently_typed_readback_is_not_exact(self) -> None:
        class IntSubclass(int):
            pass

        class BytesSubclass(bytes):
            pass

        class ListSubclass(list):
            pass

        before = self.observation().before[0]
        variants = [ListSubclass(before)]
        for slot in range(6):
            after = copy.deepcopy(before)
            after[slot] = IntSubclass(after[slot])
            variants.append(after)
        for slot, value in enumerate(before[6]):
            after = copy.deepcopy(before)
            after[6][slot] = BytesSubclass(value) if type(value) is bytes else float(value)
            variants.append(after)
        after = copy.deepcopy(before)
        after[6] = ListSubclass(after[6])
        variants.append(after)
        for after in variants:
            self.assertEqual(before, after)  # Plain list equality would wrongly accept these.
            for platform in ("linux", "darwin"):
                self.assertEqual(_terminal_readback(before, after, platform=platform), "mismatch")

    def test_fixture_snapshot_and_request_must_each_match_exactly(self) -> None:
        for slot in (0, 1):
            for field in ("baseline", "snapshot", "request"):
                observed = self.observation()
                attrs = (observed.before[slot] if field == "baseline" else
                         observed.snapshots[slot][1] if field == "snapshot" else observed.requests[slot][2])
                attrs[3] |= _DARWIN_PENDIN  # The readback exception never applies to these.
                with self.subTest(slot=slot, field=field), self.assertRaisesRegex(AssertionError, "mismatch"):
                    observed.check_requests()
        observed = self.observation()
        for slot in (0, 1):
            observed.snapshots[slot][1][0] ^= 1 << 127
            observed.requests[slot][2][0] ^= 1 << 127
        with self.assertRaisesRegex(AssertionError, "baseline/product snapshot mismatch"):
            observed.check_requests()  # S == R alone cannot establish the fixture baseline.

    def test_wrong_baseline_snapshot_and_request_types_are_rejected(self) -> None:
        for field in ("baseline", "snapshot", "request"):
            for slot in (0, 1):
                for part in ("scalar", "cc-value", "cc-type", "cc-structure", "vector"):
                    observed = self.observation()
                    attrs = (observed.before[slot] if field == "baseline" else
                             observed.snapshots[slot][1] if field == "snapshot" else observed.requests[slot][2])
                    if part == "scalar":
                        attrs[4] = float(attrs[4])
                    elif part == "cc-value":
                        attrs[6][-1] = b"different"
                    elif part == "cc-type":
                        attrs[6][0] = False  # Numerically equal to the saved integer zero.
                    elif part == "cc-structure":
                        attrs[6] = tuple(attrs[6])
                    elif field == "baseline":
                        observed.before[slot] = tuple(attrs)
                    elif field == "snapshot":
                        observed.snapshots[slot] = (slot, tuple(attrs))
                    else:
                        observed.requests[slot][2] = tuple(attrs)
                    with self.subTest(field=field, slot=slot, part=part), self.assertRaisesRegex(AssertionError, "mismatch"):
                        observed.check_requests()

    def test_missing_extra_reordered_attempts_snapshots_and_wrong_timing_fail(self) -> None:
        for field in ("snapshots", "requests"):
            for change in ("missing", "extra", "reordered"):
                observed = self.observation()
                records = getattr(observed, field)
                if change == "missing":
                    records.pop()
                elif change == "extra":
                    records.append(copy.deepcopy(records[-1]))
                else:
                    records.reverse()
                with self.subTest(field=field, change=change), self.assertRaises(AssertionError):
                    observed.check_requests()
        for when in (termios.TCSADRAIN, float(termios.TCSANOW)):
            observed = self.observation()
            observed.requests[0][1] = when
            with self.assertRaises(AssertionError):
                observed.check_requests()

    def test_unexpected_setter_failures_never_pass_even_with_exact_readback(self) -> None:
        for slot in (0, 1):
            for error in (termios.error(errno.EBADF, "fixture"), termios.error(errno.EIO, "fixture"),
                          OSError(errno.EIO, "fixture"), KeyboardInterrupt()):
                observed = self.observation()
                observed.requests[slot][3] = error
                with self.subTest(slot=slot, kind=type(error).__name__), self.assertRaisesRegex(AssertionError, "setter failure"):
                    observed.check(copy.deepcopy(observed.before), platform="darwin")
                self.assertNotEqual(observed.metadata()["setters"][slot], "ok")

    def test_closed_fd_requires_real_ebadf_and_successful_remaining_restore(self) -> None:
        observed = self.observation()
        observed.requests[0][3] = termios.error(errno.EBADF, "fixture")
        observed.check(copy.deepcopy(observed.before), expected=("closed-fd", "ok"))
        self.assertEqual(observed.metadata()["setters"], ["failed-ebadf", "ok"])
        for error in (None, termios.error(errno.EIO, "fixture"), OSError(errno.EBADF, "fixture")):
            observed.requests[0][3] = error
            with self.assertRaisesRegex(AssertionError, "must fail with EBADF"):
                observed.check_requests(expected=("closed-fd", "ok"))
        observed.requests[0][3] = termios.error(errno.EBADF, "fixture")
        observed.requests[1][3] = termios.error(errno.EBADF, "fixture")
        with self.assertRaisesRegex(AssertionError, "setter failure"):
            observed.check_requests(expected=("closed-fd", "ok"))
        observed.requests[0][2][3] |= _DARWIN_PENDIN
        with self.assertRaisesRegex(AssertionError, "request mismatch"):
            observed.check_requests(expected=("closed-fd", "ok"))

    def test_hangup_outcomes_are_explicit_and_readback_is_not_claimed(self) -> None:
        observed = self.observation()
        observed.requests[0][3] = termios.error(errno.EIO, "fixture")
        observed.requests[1][3] = termios.error(errno.ENXIO, "fixture")
        observed.check_requests(expected=("hangup", "hangup"))
        self.assertEqual(observed.metadata()["setters"], ["failed-eio", "failed-enxio"])
        self.assertEqual(observed.metadata()["readback"], ["not-observed"] * 2)
        observed.requests[0][3] = termios.error(errno.EBADF, "fixture")
        with self.assertRaisesRegex(AssertionError, "hung-up setter outcome"):
            observed.check_requests(expected=("hangup", "hangup"))

    def observed_product_run(self, *, primary=None, setter_error=None):
        from unittest import mock
        from charmer import tui

        before = self.observation().before
        observed = _TerminalRestoration(before)
        streams = [mock.Mock(), mock.Mock(), mock.Mock()]
        streams[0].fileno.return_value, streams[1].fileno.return_value = 0, 1
        getter = mock.Mock(side_effect=lambda fd: before[fd])
        result, state = object(), mock.Mock()

        def set_attrs(fd, when, attrs):
            self.assertIs(attrs, before[fd])
            if fd == 0 and setter_error is not None:
                raise setter_error

        setter = mock.Mock(side_effect=set_attrs)

        def wrapper(app):
            if primary is not None:
                raise primary
            return result

        with mock.patch.object(tui, "sys", types.SimpleNamespace(stdin=streams[0], stdout=streams[1], stderr=streams[2])), \
                mock.patch.object(tui, "curses", types.SimpleNamespace(wrapper=wrapper)), \
                mock.patch.object(tui, "_terminal_attrs", getter), \
                mock.patch.object(tui.termios, "tcsetattr", setter), \
                mock.patch.object(tui, "_input_mode_supported", return_value=True), \
                mock.patch.object(tui, "_screen_admitted", return_value=True), \
                mock.patch.object(tui, "InputState", return_value=state), \
                mock.patch.object(tui, "_INPUT_STATE", None), \
                mock.patch.object(tui, "_SCREEN_PROVENANCE", "new"), \
                mock.patch.object(tui, "_REPLY_TAIL", []), mock.patch.object(tui, "_REPLY_SINCE", []):
            observed.install(tui)
            returned = caught = None
            try:
                returned = tui.run_curses_on_streams(object(), streams[0], streams[1])
            except BaseException as error:
                caught = error
            finally:
                observed.uninstall()
            self.assertIsNone(tui._INPUT_STATE)
        self.assertEqual(getter.call_args_list, [mock.call(0), mock.call(1)])
        self.assertEqual([call.args[:2] for call in setter.call_args_list], [(0, termios.TCSANOW), (1, termios.TCSANOW)])
        state.clear.assert_called_once_with()
        return observed, result, returned, caught

    def test_observer_records_actual_consumed_snapshots_and_original_requests(self) -> None:
        observed, result, returned, caught = self.observed_product_run()
        self.assertIsNone(caught)
        self.assertIs(returned, result)
        observed.check_requests()
        observed.requests[0][2][6][0] = 99
        self.assertEqual(observed.snapshots[0][1][6][0], 0)  # Frozen, not aliased to the request.
        self.assertEqual(observed.before[0][6][0], 0)

    def test_primary_input_failure_and_interrupt_keep_identity_despite_failed_restore(self) -> None:
        from charmer import tui

        for primary in (tui.CursesError(tui.INPUT_UNAVAILABLE), KeyboardInterrupt()):
            failure = termios.error(errno.EBADF, "fixture")
            observed, _, _, caught = self.observed_product_run(primary=primary, setter_error=failure)
            self.assertIs(caught, primary)
            self.assertIs(observed.requests[0][3], failure)
            observed.check_requests(expected=("closed-fd", "ok"))

    def test_restore_failure_without_primary_is_still_observable(self) -> None:
        from charmer import tui

        failure = termios.error(errno.EBADF, "fixture")
        observed, _, _, caught = self.observed_product_run(setter_error=failure)
        self.assertIsInstance(caught, tui.CursesError)
        self.assertEqual(str(caught), "terminal attributes could not be restored")
        self.assertIs(observed.requests[0][3], failure)
        with self.assertRaisesRegex(AssertionError, "setter failure"):
            observed.check_requests()

    def test_observer_forwards_objects_results_and_exceptions_without_extra_calls(self) -> None:
        from unittest import mock

        attrs, result, when = self.observation().before[0], object(), object()
        getter, setter = mock.Mock(return_value=attrs), mock.Mock(return_value=result)
        owner = types.SimpleNamespace(_terminal_attrs=getter, termios=types.SimpleNamespace(tcsetattr=setter))

        def run_curses_on_streams():
            def restore_attrs():
                return owner.termios.tcsetattr(0, when, saved)
            saved = owner._terminal_attrs(0)
            return saved, restore_attrs()

        owner.run_curses_on_streams = run_curses_on_streams
        observed = _TerminalRestoration([attrs, attrs])
        observed.install(owner)
        try:
            self.assertIs(owner._terminal_attrs(1), attrs)  # A non-save caller is not counted.
            self.assertIs(owner.termios.tcsetattr(1, when, attrs), result)
            self.assertEqual(observed.snapshots, [])
            self.assertEqual(observed.requests, [])
            saved, returned = owner.run_curses_on_streams()
            self.assertIs(saved, attrs)
            self.assertIs(returned, result)
            self.assertIs(setter.call_args.args[1], when)
            self.assertIs(setter.call_args.args[2], attrs)
            attrs[6][0] = 99
            self.assertEqual(observed.snapshots[0][1][6][0], 0)
            self.assertEqual(observed.requests[0][2][6][0], 0)
            error = KeyboardInterrupt()
            setter.side_effect = error
            with self.assertRaises(KeyboardInterrupt) as raised:
                owner.run_curses_on_streams()
            self.assertIs(raised.exception, error)
            self.assertIs(observed.requests[-1][3], error)
            self.assertEqual(getter.call_count, 3)
            self.assertEqual(setter.call_count, 3)
            error = OSError(errno.EIO, "fixture getter failure")
            getter.side_effect = error
            with self.assertRaises(OSError) as raised:
                owner.run_curses_on_streams()
            self.assertIs(raised.exception, error)
            self.assertEqual(getter.call_count, 4)
            self.assertEqual(setter.call_count, 3)
            self.assertEqual(len(observed.snapshots), 2)
            self.assertEqual(len(observed.requests), 2)
        finally:
            observed.uninstall()
        self.assertIs(owner._terminal_attrs, getter)
        self.assertIs(owner.termios.tcsetattr, setter)

    def test_metadata_is_bounded_classification_not_a_tty_dump(self) -> None:
        observed = self.observation()
        observed.requests[0][3] = termios.error(errno.EBADF, "exception text must not be emitted" * 100)
        report = observed.metadata(copy.deepcopy(observed.before), platform="darwin")
        self.assertEqual(set(report), {"snapshot_count", "attempt_count", "snapshots", "requests", "setters", "readback"})
        self.assertLess(len(json.dumps(report)), 512)
        self.assertNotIn("exception text", json.dumps(report))
        self.assertEqual(report["readback"], ["exact", "exact"])
        for kwargs in ({}, {"close_input": True}, {"ignore_hup": True}):
            compile(_alias_reader_code(**kwargs), "<alias-restore-fixture>", "exec")


# ---------------------------------------------------------------------------
# Escdelay, ^S flow control and the
# curses-less line fallback on a real PTY.  The probe apps need no Runtime;
# CursesAbsentPTYTests builds its Runtime inline as QuickConfirmPTYTests does
# (these children do not use tests/_tui_fixture.py).


def _probe_code(*, exit_on: str) -> str:
    """A curses probe app: draw READY, read one key, report after teardown."""

    return f"""
import curses
import sys
from charmer import tui
seen = {{}}
def app(win):
    seen['escdelay'] = curses.get_escdelay()
    tui.safe_add(win, 1, 2, 'PROBE-READY')
    win.refresh()
    while True:
        key = tui.read_key(win)
        if {exit_on!r} == 'any' or (key.kind == 'ctrl' and key.ch == {exit_on!r}):
            return key
result = tui.run_curses_on_streams(app, sys.stdin, sys.stdout)
print('ESCDELAY=' + str(seen['escdelay']), flush=True)
print('KEY=' + result.kind + ':' + result.ch, flush=True)
if result.kind == 'ctrl' and result.ch == 's':
    print('GOT-CTRL-S', flush=True)
"""


class PTYInputDiagnosticTests(unittest.TestCase):
    def blocked_child(self) -> PTYProcess:
        # A synthetic read, not a reproduction of ncurses' SIGINT behaviour.
        code = """
import os, termios, tty
before = termios.tcgetattr(0)
try:
    tty.setcbreak(0)
    print('DIAGNOSTIC-WAIT', flush=True)
    os.read(0, 1)
finally:
    termios.tcsetattr(0, termios.TCSANOW, before)
raise SystemExit(130)
"""
        child = PTYProcess(code, extra_pythonpath=(TESTS_ROOT,), diagnostics=True)
        self.addCleanup(child.close)
        child.read_until(b"DIAGNOSTIC-WAIT")
        return child

    def test_first_native_entry_and_return_do_not_change_success_or_terminal_restore(self) -> None:
        dispositions = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGUSR1)}
        child = PTYProcess(_probe_code(exit_on="s"), extra_pythonpath=(TESTS_ROOT,), diagnostics=True)
        self.addCleanup(child.close)
        child.read_until(b"PROBE-READY")
        child.send(b"x\x13")  # two reads; only the first C-call boundary is recorded
        code, output, after = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(b"GOT-CTRL-S", output)
        _restored(self, child, after)
        events = child.diagnostics.phases()
        self.assertEqual([event["phase"] for event in events],
                         ["installed", "read-key-enter", "getch-enter", "getch-return"])
        for event in events:
            self.assertEqual(event["sigint"], events[0]["sigint"])
            self.assertEqual(event["blocked_signals"], events[0]["blocked_signals"])
            self.assertNotIn("value", event)
        self.assertIsNone(child.timeout_diagnostics)
        self.assertEqual(dispositions, {sig: signal.getsignal(sig) for sig in dispositions})

    def test_timeout_still_fails_when_diagnostic_wake_allows_exit_130(self) -> None:
        # Only this dump control requires a default disposition; the helper
        # must leave an inherited ignored signal alone in every other child.
        previous = signal.signal(signal.SIGUSR1, signal.SIG_DFL)
        try:
            child = self.blocked_child()
        finally:
            signal.signal(signal.SIGUSR1, previous)
        self.assertEqual(signal.getsignal(signal.SIGUSR1), previous)
        with self.assertRaisesRegex(AssertionError, "PTY child timed out;.*diagnostics="):
            child.finish(timeout=0)
        report = child.timeout_diagnostics
        self.assertIsNone(report["before_diagnostics"]["returncode"])
        self.assertEqual(report["dump_signal"], signal.SIGUSR1)
        self.assertIn('File "<string>"', report["stack"])
        self.assertEqual(report["wake"], "one-nul-byte")
        self.assertEqual(report["after_wake"]["returncode"], 130)
        self.assertEqual(child.process.returncode, 130)
        _restored(self, child, termios.tcgetattr(child.slave))

    def test_inherited_ignored_dump_signal_still_fails_when_wake_allows_exit_130(self) -> None:
        from unittest import mock

        previous = signal.signal(signal.SIGUSR1, signal.SIG_IGN)
        try:
            child = self.blocked_child()
        finally:
            signal.signal(signal.SIGUSR1, previous)
        self.assertEqual(signal.getsignal(signal.SIGUSR1), previous)
        events = child.diagnostics.phases()
        self.assertEqual([event["phase"] for event in events], ["installed"])
        self.assertEqual(events[0]["pid"], child.process.pid)
        self.assertIsNone(events[0]["dump_signal"])
        with mock.patch.object(child.process, "send_signal", wraps=child.process.send_signal) as send_signal:
            with self.assertRaisesRegex(AssertionError, "PTY child timed out;.*diagnostics="):
                child.finish(timeout=0)
        self.assertNotIn(mock.call(signal.SIGUSR1), send_signal.call_args_list)
        report = child.timeout_diagnostics
        self.assertIsNone(report["before_diagnostics"]["returncode"])
        self.assertEqual(report["dump_signal"], "not-registered")
        self.assertIsNone(report["after_dump_signal"]["returncode"])
        self.assertEqual(report["stack"], "")
        self.assertEqual(report["wake"], "one-nul-byte")
        self.assertEqual(report["after_wake"]["returncode"], 130)
        self.assertEqual(child.process.returncode, 130)
        _restored(self, child, termios.tcgetattr(child.slave))

    def test_no_diagnostic_signal_without_the_child_registration_record(self) -> None:
        from unittest import mock

        child = self.blocked_child()
        with mock.patch.object(child.diagnostics, "phases", return_value=[]), \
                mock.patch.object(child.process, "send_signal", wraps=child.process.send_signal) as send_signal:
            with self.assertRaisesRegex(AssertionError, "PTY child timed out"):
                child.finish(timeout=0)
        self.assertNotIn(mock.call(signal.SIGUSR1), send_signal.call_args_list)
        self.assertEqual(child.timeout_diagnostics["dump_signal"], "not-registered")
        self.assertIsNotNone(child.process.poll())

    def test_diagnostic_error_still_fails_and_reaps_the_child(self) -> None:
        from unittest import mock

        child = self.blocked_child()
        with mock.patch.object(child.diagnostics, "on_timeout", side_effect=OSError("fixture diagnostic I/O")):
            with self.assertRaisesRegex(AssertionError, "PTY child timed out"):
                child.finish(timeout=0)
        self.assertEqual(child.timeout_diagnostics, {"diagnostic_error": "OSError"})
        self.assertIsNotNone(child.process.poll())


class EscDelayPTYTests(unittest.TestCase):
    """A lone Esc is delivered promptly."""

    def test_lone_escape_is_prompt(self) -> None:
        from unittest import mock

        with mock.patch.dict(os.environ):
            os.environ.pop("ESCDELAY", None)  # the child inherits no ESCDELAY
            child = PTYProcess(_probe_code(exit_on="any"))
        self.addCleanup(child.close)
        child.read_until(b"PROBE-READY")
        started = time.monotonic()
        child.send(b"\x1b")
        child.read_until(b"KEY=esc:")
        elapsed = time.monotonic() - started
        code, output, after = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(b"ESCDELAY=25", output)
        # A loose sanity bound; ncurses' default delay is 1000 ms.
        self.assertLess(elapsed, 0.9, output)
        mask = termios.ECHO | termios.ICANON
        self.assertEqual(child.before[3] & mask, after[3] & mask)

    def test_escdelay_env_wins(self) -> None:
        child = PTYProcess(_probe_code(exit_on="any"), extra_env={"ESCDELAY": "600"})
        self.addCleanup(child.close)
        child.read_until(b"PROBE-READY")
        child.send(b"\x1b")
        code, output, _after = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(b"ESCDELAY=600", output)
        self.assertIn(b"KEY=esc:", output)


class IxonRestorePTYTests(unittest.TestCase):
    """^S reaches the screen; IXON restored after."""

    def test_ctrl_s_reaches_the_screen_and_flow_control_is_restored(self) -> None:
        child = PTYProcess(_probe_code(exit_on="s"), extra_pythonpath=(TESTS_ROOT,), restoration=True)
        self.addCleanup(child.close)
        self.assertTrue(
            child.before[0] & termios.IXON,
            "precondition: a fresh PTY has flow control (IXON) on",
        )
        child.read_until(b"PROBE-READY")
        child.send(b"\x13")  # ^S: frozen output if IXON were still on
        child.read_until(b"GOT-CTRL-S")
        code, output, after = child.finish()
        self.assertEqual(code, 0, output)
        self.assertEqual(child.before[0] & termios.IXON, after[0] & termios.IXON)
        mask = termios.ECHO | termios.ICANON
        self.assertEqual(child.before[3] & mask, after[3] & mask)
        _report_terminal_restore(self, output, readback=[_terminal_readback(child.before, after)] * 2)


class CursesAbsentPTYTests(unittest.TestCase):
    """A Python without curses on a capable terminal still
    reaches the line confirm (the line card)."""

    def _code(self, temp: str, secret_file: Path) -> str:
        return f"""
import sys
sys.modules.update(curses=None, _curses=None)
from pathlib import Path
from charmer import tui
assert tui.curses_available() is False
from charmer.cli.runtime import Runtime
from charmer.cli.entry import main
root = Path({str(CATALOG_ROOT)!r})
base = Path({temp!r})
env = {{'HOME': str(base/'home'), 'XDG_CONFIG_HOME': str(base/'config'), 'XDG_STATE_HOME': str(base/'state'), 'CHARMER_SECRET_ENV': {str(secret_file)!r}}}
def fake(prepared):
    print('FAKE_LAUNCH=' + prepared.result.session_action.kind, flush=True)
    return 0
runtime = Runtime(asset_root=root, environ=env, cwd=base/'project', launch_callback=fake, doctor_binary_callback=lambda contract: ([], ['fixture binary verified.']), doctor_callback=lambda _runtime: [], served_models_callback=lambda gateway, token: (None, None), health_get=lambda base, path: 200)
raise SystemExit(main([], runtime=runtime, input_stream=sys.stdin, output_stream=sys.stdout, interactive=True))
"""

    def test_capable_terminal_without_curses_uses_the_line_card(self) -> None:
        temp = tempfile.mkdtemp(prefix="charmer-pty-nocurses-")
        self.addCleanup(__import__("shutil").rmtree, temp, True)
        child = PTYProcess(self._code(temp, _secret_file(temp)))
        self.addCleanup(child.close)
        child.read_until(LINE_CONFIRM)
        child.send(b"q\n")
        code, output, after = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(b"Nothing was launched.", output)
        self.assertNotIn(b"FAKE_LAUNCH", output)
        self.assertNotIn(b"Traceback", output)
        # The line card, under the unchanged line footer.
        self.assertIn("charmer — profile: balanced · follows profile · Fresh".encode(), output)
        mask = termios.ECHO | termios.ICANON
        self.assertEqual(child.before[3] & mask, after[3] & mask)
        self.assertEqual(child.before[0] & termios.IXON, after[0] & termios.IXON)


# ---------------------------------------------------------------------------
# The 3.0 profile editor on a real PTY.  Children
# build their Runtime with ``_tui_fixture.fixture_runtime`` and run ``main``
# inside ``hermetic(runtime)``; nothing reads the
# journal, ``/proc`` or the daemon socket directory.

TESTS_ROOT = REPO_ROOT / "tests"
# curses turns keypad transmit mode on (smkx), so arrows arrive as SS3 sequences.
DOWN_KEY = b"\x1bOB"
RIGHT_KEY = b"\x1bOC"
EDITOR_READY = "edit profile — ".encode()
FIXTURE_TIMEOUT = 30.0


def _fixture_editor_code(temp: str, name: str, *, setup: str = "", after: str = "", extra: dict | None = None) -> str:
    return f"""
import sys
from pathlib import Path
import _tui_fixture as fx
from charmer import catalog, lineup as lineup_mod, profile, scope, sessions
from charmer.cli import entry
runtime = fx.fixture_runtime(Path({temp!r}), environ_extra={dict(extra or {})!r})
{setup}
with fx.hermetic(runtime):
    code = entry.main(['profile', 'edit', {name!r}], runtime=runtime, input_stream=sys.stdin, output_stream=sys.stdout, interactive=True)
print('EXIT=' + str(code), flush=True)
{after}
raise SystemExit(code)
"""


def fx_ids() -> tuple[str, str]:
    import _tui_fixture as fx

    return fx.FIXED_ID, fx.OTHER_ID


def _restored(case: unittest.TestCase, child: PTYProcess, after: list) -> None:
    mask = termios.ECHO | termios.ICANON
    case.assertEqual(child.before[3] & mask, after[3] & mask)
    case.assertEqual(child.before[0] & termios.IXON, after[0] & termios.IXON)


class ProfileEditorPTYTests(unittest.TestCase):
    """The 3.0 twin of the deleted 2.x ``CursesPTYTests``: ``profile edit <DEFAULT_SEED>``."""

    def child(self, *, setup: str = "", name: str | None = None, extra: dict | None = None, **size) -> PTYProcess:
        from charmer import catalog

        temp = tempfile.mkdtemp(prefix="charmer-pty-editor-")
        self.addCleanup(__import__("shutil").rmtree, temp, True)
        child = PTYProcess(
            _fixture_editor_code(temp, name or catalog.DEFAULT_SEED, setup=setup, extra=extra),
            extra_pythonpath=(TESTS_ROOT,),
            **size,
        )
        self.addCleanup(child.close)
        return child

    def assertCancelled(self, child: PTYProcess) -> bytes:
        code, output, after = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(b"Nothing was saved.", output)
        self.assertNotIn(b"Traceback", output)
        _restored(self, child, after)
        return output

    def test_cancel_restores_the_terminal(self) -> None:
        child = self.child()
        child.read_until(EDITOR_READY, FIXTURE_TIMEOUT)
        child.send(b"\x1b")
        self.assertCancelled(child)

    def test_resize_to_small_is_safe(self) -> None:
        child = self.child()
        child.read_until(EDITOR_READY, FIXTURE_TIMEOUT)
        child.resize(8, 35)
        child.send(b"j")  # wake getch; the next draw sees the resized PTY
        child.read_until(b"Terminal too small")
        child.send(b"\x1b")
        self.assertCancelled(child)

    def test_too_small_start_cancels(self) -> None:
        child = self.child(rows=8, columns=35)
        child.read_until(b"Terminal too small", FIXTURE_TIMEOUT)
        child.send(b"\x1b")
        self.assertCancelled(child)

    def test_sigint_cancels_and_restores(self) -> None:
        child = self.child()
        child.read_until(EDITOR_READY, FIXTURE_TIMEOUT)
        child.process.send_signal(signal.SIGINT)
        self.assertCancelled(child)

    def test_blocked_check_row(self) -> None:
        setup = (
            "doc = runtime.profiles.load(catalog.DEFAULT_SEED)\n"
            "doc.pop('seed')\n"
            "doc['name'] = 'blocked'\n"
            "doc['lead'] = {'model': fx.needs_choice_key(runtime), 'effort': profile.ULTRACODE}\n"
            "runtime.profiles.save(doc)\n"
        )
        child = self.child(setup=setup, name="blocked")
        child.read_until(b"error(s)", FIXTURE_TIMEOUT)
        child.send(b"\x1b")
        self.assertCancelled(child)

    def test_ctrl_g_json_not_applied_then_discard(self) -> None:
        editor = f"{sys.executable} -c \"import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('{{ not json')\""
        child = self.child(extra={"VISUAL": editor})
        child.read_until(EDITOR_READY, FIXTURE_TIMEOUT)
        child.send(DOWN_KEY + DOWN_KEY + b"U")  # unbind the first agent: the editor is dirty
        child.read_until("unsaved changes ●".encode())
        child.send(b"\x07")  # ^G: the raw JSON in $VISUAL
        child.read_until(b"JSON not applied")
        child.send(b"\x1b")
        child.read_until(b"Discard")
        child.send(RIGHT_KEY + b"\n")
        self.assertCancelled(child)


class EditSavePropagatePTYTests(unittest.TestCase):
    """Edit a writer's effort, ^S, then A applies it live to the unknown follower."""

    def test_save_propagates_live_to_the_running_follower(self) -> None:
        from charmer import catalog, profile

        temp = tempfile.mkdtemp(prefix="charmer-pty-propagate-")
        self.addCleanup(__import__("shutil").rmtree, temp, True)
        writer = profile.WRITER_IDS[0]
        setup = (
            f"writer = {writer!r}\n"
            "fx.v4_record(runtime, catalog.DEFAULT_SEED, managed_id=fx.FIXED_ID, title='running one')\n"
            "fx.v4_record(runtime, catalog.DEFAULT_SEED, managed_id=fx.OTHER_ID, ended=True)\n"
            "store = runtime.session_store\n"
            "agent = scope.scope_dir(store.root, fx.FIXED_ID) / '.claude' / 'agents' / (writer + '.md')\n"
            "before_bytes = agent.read_bytes()\n"
            "before_gen = store.load(fx.FIXED_ID)['lineup_generation']\n"
            "before_effort = runtime.profiles.load(catalog.DEFAULT_SEED)['agents'][writer]['effort']\n"
            "efforts = profile.declared_efforts(runtime.lineup_catalog().lines[runtime.profiles.load(catalog.DEFAULT_SEED)['agents'][writer]['model']])\n"
            "assert efforts.index(before_effort) < len(efforts) - 1, 'precondition: -> reaches another effort'\n"
            "import io\n"
            "real_on_saved = lineup_mod.on_saved\n"
            "reports = []\n"
            "def tee(runtime_, names, *, apply_live, out, now=None):\n"
            "    buffer = io.StringIO()\n"
            "    written = real_on_saved(runtime_, names, apply_live=apply_live, out=buffer)\n"
            "    out.write(buffer.getvalue())\n"
            "    reports.append(buffer.getvalue())\n"
            "    return written\n"
            "lineup_mod.on_saved = tee\n"
        )
        after = (
            "print('REPORT=' + repr(reports), flush=True)\n"
            "print('GEN=%d->%d' % (before_gen, store.load(fx.FIXED_ID)['lineup_generation']), flush=True)\n"
            "print('AGENT_CHANGED=' + str(agent.read_bytes() != before_bytes), flush=True)\n"
            "print('EFFORT=' + before_effort + '->' + runtime.profiles.load(catalog.DEFAULT_SEED)['agents'][writer]['effort'], flush=True)\n"
        )
        child = PTYProcess(
            _fixture_editor_code(temp, catalog.DEFAULT_SEED, setup=setup, after=after),
            extra_pythonpath=(TESTS_ROOT,),
        )
        self.addCleanup(child.close)
        child.read_until(EDITOR_READY, FIXTURE_TIMEOUT)
        downs = 2 + catalog.AGENT_ROLE_IDS.index(writer)  # General → Lead → the agents
        child.send(DOWN_KEY * downs + b"\n")
        child.read_until(b"choose model")
        child.send(RIGHT_KEY + b"\n")
        child.read_until("unsaved changes ●".encode())
        child.send(b"\x13")  # ^S (flow control is off inside the TUI)
        child.read_until(b"2 sessions follow this profile")
        child.send(b"A")
        child.read_until(b"applied live (lineup gen")  # the report screen shows on_saved's lines
        child.send(b"\x1b")  # the report
        child.read_until(f"saved {catalog.DEFAULT_SEED}".encode())
        child.send(b"\x1b")  # the (clean) editor
        code, output, after_attrs = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(f"Saved profile {catalog.DEFAULT_SEED!r}.".encode(), output)
        self.assertNotIn(b"Traceback", output)
        report = output.split(b"REPORT=", 1)[1].split(b"\r", 1)[0].decode()
        self.assertIn(f"{fx_ids()[0][:8]} project: applied live (lineup gen", report)
        self.assertIn(f"{fx_ids()[1][:8]} project: applies at the next resume", report)
        gen = output.split(b"GEN=", 1)[1].split(b"\r", 1)[0].split(b"\n", 1)[0].decode()
        before, after_gen = (int(v) for v in gen.split("->"))
        self.assertEqual(after_gen, before + 1, output)
        self.assertIn(b"AGENT_CHANGED=True", output)
        effort = output.split(b"EFFORT=", 1)[1].split(b"\r", 1)[0].split(b"\n", 1)[0].decode()
        self.assertNotEqual(*effort.split("->"))
        _restored(self, child, after_attrs)


# ---------------------------------------------------------------------------
# The launch card and the screens it opens on a real PTY.  Children build their Runtime with ``_tui_fixture.fixture_runtime``
# and run ``main`` inside ``hermetic(runtime)``; the launch callback prints
# ``FAKE_LAUNCH=<kind>`` (and the prepared target) after curses teardown.

CARD_READY = b"Status  Ready"
DIRECT_READY = "direct session — lead only, no profile".encode()
END_KEY = b"\x1bOF"


def _fixture_card_code(
    temp: str,
    argv: list,
    *,
    setup: str = "",
    after: str = "",
    asset_root: str | None = None,
    streams: bool = True,
) -> str:
    """A bare-launch child over the fixture runtime (``streams=False``: main opens /dev/tty)."""

    extra = f", asset_root=Path({asset_root!r})" if asset_root else ""
    io_args = "input_stream=sys.stdin, output_stream=sys.stdout, interactive=True" if streams else ""
    return f"""
import json
import sys
from pathlib import Path
import _tui_fixture as fx
from charmer import catalog, profile, settings
from charmer.cli import entry, selection, session_facts
def fake(prepared):
    print('FAKE_LAUNCH=' + prepared.result.session_action.kind, flush=True)
    print('TARGET=' + str(prepared.target.profile), flush=True)
    print('RECORD=' + json.dumps({{'profile': prepared.record['profile'], 'agents': prepared.record['applied']['agents']}}), flush=True)
    return 0
runtime = fx.fixture_runtime(Path({temp!r}), launch_callback=fake{extra})
{setup}
with fx.hermetic(runtime):
    code = entry.main({argv!r}, runtime=runtime{', ' + io_args if io_args else ''})
print('EXIT=' + str(code), flush=True)
{after}
raise SystemExit(code)
"""


def _expected_default(case: unittest.TestCase) -> str:
    """The default profile a bare launch in a fresh fixture child resolves
    (derived on a parent-side fixture runtime of the same shape)."""

    import _tui_fixture as fx
    from charmer.setup import defaults

    root = tempfile.mkdtemp(prefix="charmer-pty-default-")
    case.addCleanup(__import__("shutil").rmtree, root, True)
    runtime = fx.fixture_runtime(Path(root))
    with fx.hermetic(runtime):
        return defaults.resolve_default(runtime).name


def _read_after(child: PTYProcess, offset: int, needle: bytes, timeout: float = FIXTURE_TIMEOUT) -> None:
    """Read until ``needle`` appears in the output past ``offset`` (a redraw after a key)."""

    deadline = time.monotonic() + timeout
    while needle not in child.output[offset:] and time.monotonic() < deadline:
        ready, _, _ = select.select([child.master], [], [], max(0, deadline - time.monotonic()))
        if not ready:
            break
        try:
            chunk = os.read(child.master, 65536)
        except OSError:
            break
        if not chunk:
            break
        child.output.extend(chunk)
    if needle not in child.output[offset:]:
        raise AssertionError(f"did not observe {needle!r} after the key; output={bytes(child.output)!r}")


def _line_of(output: bytes, prefix: bytes) -> str:
    return output.split(prefix, 1)[1].split(b"\r", 1)[0].split(b"\n", 1)[0].decode()


class _CardChildCase(unittest.TestCase):
    def child(self, prefix: str, argv=(), **kwargs) -> PTYProcess:
        temp = tempfile.mkdtemp(prefix=prefix)
        self.addCleanup(__import__("shutil").rmtree, temp, True)
        self.temp = temp
        child = PTYProcess(_fixture_card_code(temp, list(argv), **kwargs), extra_pythonpath=(TESTS_ROOT,))
        self.addCleanup(child.close)
        return child

    def fixture_runtime(self, **kwargs):
        """A parent-side fixture runtime, only to derive keys and positions."""

        import _tui_fixture as fx

        root = tempfile.mkdtemp(prefix="charmer-pty-derive-")
        self.addCleanup(__import__("shutil").rmtree, root, True)
        return fx.fixture_runtime(Path(root), **kwargs)


class CardLaunchPTYTests(_CardChildCase):
    def test_enter_launches_the_default_seed(self) -> None:
        from charmer import catalog

        child = self.child("charmer-pty-card-launch-")
        child.read_until(CARD_READY, FIXTURE_TIMEOUT)
        self.assertIn(b"charmer", child.output)
        expected = _expected_default(self)
        self.assertIn(f"profile: {expected}".encode(), child.output)
        child.send(b"\n")
        code, output, after = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(b"FAKE_LAUNCH=fresh", output)
        self.assertEqual(_line_of(output, b"TARGET="), expected)
        self.assertNotIn(b"Traceback", output)
        _restored(self, child, after)


class DirectLaunchPTYTests(_CardChildCase):
    def test_d_then_enter_launches_an_ad_hoc_direct_session(self) -> None:
        # Every OAuth pool counts as signed in, so the preselected row is unmarked.
        setup = (
            "pools = {p['transport']['pool'] for p in runtime.lineup_catalog().providers.values() "
            "if p['transport']['kind'] == 'oauth-pool'}\n"
            "import charmer.cli.gateway_facts\n"
            "charmer.cli.gateway_facts._oauth_credential_records = lambda _rt: {p: 1 for p in pools}\n"
        )
        child = self.child("charmer-pty-direct-", setup=setup)
        child.read_until(CARD_READY, FIXTURE_TIMEOUT)
        child.send(b"d")
        child.read_until(DIRECT_READY)
        child.send(b"\n")
        code, output, after = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(b"FAKE_LAUNCH=fresh", output)
        self.assertEqual(json.loads(_line_of(output, b"RECORD=")), {"profile": None, "agents": {}})
        _restored(self, child, after)


class ModelsAdmitPTYTests(_CardChildCase):
    def test_m_then_enter_admits_the_new_line(self) -> None:
        from charmer import catalog as catalog_mod
        from test_screens_catalog import _fixture_copy, new_line_key

        key = new_line_key(catalog_mod.load_catalog(CATALOG_ROOT))
        asset = _fixture_copy(self, new=(key,))
        after = "print('ADMITTED=' + json.dumps(runtime.settings_store.load().get('admitted_lines')), flush=True)\n"
        child = self.child("charmer-pty-models-", asset_root=str(asset), after=after)
        child.read_until(CARD_READY, FIXTURE_TIMEOUT)
        child.send(b"M")
        child.send(DOWN_KEY * 40)  # the New rows are last; the cursor stops there
        child.read_until(b"admit badge")
        child.send(b"\n")
        child.read_until(f"Admit {key}?".encode())
        # Metadata admission defaults to Cancel; choose Admit explicitly.
        child.send(b"\x1bOD\n")
        child.read_until(f"admitted {key}".encode())
        child.send(b"\x1b")  # Models → the card
        mark = len(child.output)
        _read_after(child, mark, CARD_READY)
        child.send(b"\x1b")
        code, output, after_attrs = child.finish()
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(_line_of(output, b"ADMITTED=")), [key])
        self.assertNotIn(b"FAKE_LAUNCH", output)
        _restored(self, child, after_attrs)


class ProvidersTogglePTYTests(_CardChildCase):
    def test_g_then_space_space_toggles_and_never_runs_journalctl(self) -> None:
        from charmer import cli, tui

        runtime = self.fixture_runtime()
        screen = cli._ProvidersScreen(runtime, palette=tui.MONO_PALETTE, journal=lambda: None)
        index = next(
            (i for i, row in enumerate(screen.rows) if row.id in {e["provider"] for e in runtime.lineup_catalog().lines.values()}),
            None,
        )
        self.assertIsNotNone(index, "a provider with at least one line")
        provider = screen.rows[index].id
        steps = index - screen.selected
        stub_dir = Path(tempfile.mkdtemp(prefix="charmer-pty-journal-stub-"))
        self.addCleanup(__import__("shutil").rmtree, stub_dir, True)
        marker = stub_dir / "marker"
        stub = stub_dir / "journalctl"
        stub.write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n")
        stub.chmod(0o755)
        after = (
            "doc = json.loads(settings.settings_path(runtime.environ).read_text())\n"
            f"print('ENABLED=' + json.dumps(doc['providers'][{provider!r}]['enabled']), flush=True)\n"
        )
        temp = tempfile.mkdtemp(prefix="charmer-pty-providers-")
        self.addCleanup(__import__("shutil").rmtree, temp, True)
        child = PTYProcess(
            _fixture_card_code(temp, [], after=after),
            extra_pythonpath=(TESTS_ROOT,),
            extra_env={"PATH": f"{stub_dir}{os.pathsep}{os.environ.get('PATH', '')}"},
        )
        self.addCleanup(child.close)
        child.read_until(CARD_READY, FIXTURE_TIMEOUT)
        child.send(b"G")
        child.read_until(b"local status")
        child.send((DOWN_KEY if steps >= 0 else b"\x1bOA") * abs(steps))
        child.send(b" ")
        child.read_until("disabled — applies at the next launch".encode())
        child.send(b" ")
        child.read_until("enabled — applies at the next launch".encode())
        child.send(b"\x1b")
        mark = len(child.output)
        _read_after(child, mark, CARD_READY)
        child.send(b"\x1b")
        code, output, after_attrs = child.finish()
        self.assertEqual(code, 0, output)
        self.assertEqual(_line_of(output, b"ENABLED="), "true")
        self.assertFalse(marker.exists(), "cf[4]: the journal is never read in a fixture child")
        _restored(self, child, after_attrs)


class SettingsNextLaunchPTYTests(_CardChildCase):
    def test_o_edits_compaction_and_the_card_shows_it(self) -> None:
        import io as io_mod

        from charmer import cli, settings, tui

        runtime = self.fixture_runtime()
        screen = cli._SettingsScreen(runtime, tui.MONO_PALETTE, tty_in=io_mod.StringIO(), tty_out=io_mod.StringIO())
        selectable = [entry[1].key for entry in screen.entries if screen._selectable(entry)]
        steps = selectable.index(settings.COMPACTION_PERCENT_KEY)
        value = settings.COMPACTION_PERCENT_DEFAULT - 5
        after = (
            "doc = json.loads(settings.settings_path(runtime.environ).read_text())\n"
            "print('PERCENT=' + str(doc[settings.COMPACTION_PERCENT_KEY]), flush=True)\n"
        )
        child = self.child("charmer-pty-settings-", after=after)
        child.read_until(CARD_READY, FIXTURE_TIMEOUT)
        child.send(b"O")
        child.read_until(b"auto-compact at")
        child.send(b"j" * steps + b"\n")
        child.read_until(b"current ")
        child.send(str(value).encode() + b"\n")
        child.send(b"\n")
        child.read_until("applies at the next launch or resume".encode())
        mark = len(child.output)
        child.send(b"\x1b")  # Settings → the card
        _read_after(child, mark, f"({value} %)".encode())
        child.send(b"\x1b")
        code, output, after_attrs = child.finish()
        self.assertEqual(code, 0, output)
        self.assertEqual(_line_of(output, b"PERCENT="), str(value))
        _restored(self, child, after_attrs)


# The sessions screen and the lineup dialog on a real PTY.  Children build their Runtime with
# ``_tui_fixture.fixture_runtime``, seed one v4 record following the default
# seed (with its metadata-only transcript) and run ``main`` inside
# ``hermetic(runtime)``.

_SESSIONS_SETUP = """
record = fx.v4_record(runtime, catalog.DEFAULT_SEED, managed_id=fx.FIXED_ID)
transcript = (Path(runtime.environ['HOME']) / '.claude' / 'projects'
              / session_facts._native_project_slug(record['cwd']) / (record['runtime_session_id'] + '.jsonl'))
transcript.parent.mkdir(parents=True, exist_ok=True)
transcript.touch()
before = fx.canonical(runtime.session_store.load(fx.FIXED_ID))
print('GEN0=' + str(record['lineup_generation']), flush=True)
"""
_SESSIONS_AFTER = """
after = runtime.session_store.load(fx.FIXED_ID)
print('GEN=' + str(after['lineup_generation']), flush=True)
print('PENDING=' + str('pending' in after), flush=True)
print('UNCHANGED=' + str(fx.canonical(after) == before), flush=True)
"""
SESSIONS_READY = "sessions — project (cwd) · 1 total".encode()
RIGHT_KEY = b"\x1bOC"  # keypad (application) mode, as END_KEY


class _SessionsChildCase(_CardChildCase):
    def sessions_child(self, prefix: str, argv, *, setup: str = "") -> PTYProcess:
        return self.child(prefix, argv, setup=_SESSIONS_SETUP + setup, after=_SESSIONS_AFTER)


class SessionsScreenPTYTests(_SessionsChildCase):
    """A capable TERM gets the curses sessions screen;
    ``--line`` keeps the line flows."""

    def test_sessions_screen_esc_cancels_and_restores_terminal(self) -> None:
        child = self.sessions_child("charmer-pty-sessions-", ["-r"])
        child.read_until(SESSIONS_READY, FIXTURE_TIMEOUT)
        child.send(b"\x1b")
        code, output, after = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(b"EXIT=0", output)
        self.assertNotIn(b"FAKE_LAUNCH", output)
        self.assertNotIn(b"Traceback", output)
        _restored(self, child, after)

    def test_sessions_list_on_a_terminal_prints_the_listing(self) -> None:
        child = self.sessions_child("charmer-pty-sessions-list-", ["sessions", "list"])
        code, output, after = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(b"11111111", output)
        self.assertNotIn(SESSIONS_READY, output)
        self.assertNotIn(b"FAKE_LAUNCH", output)
        self.assertIn(b"UNCHANGED=True", output)
        _restored(self, child, after)

    def test_bare_resume_screen_launches(self) -> None:
        child = self.sessions_child("charmer-pty-bare-r-", ["-r"])
        child.read_until(SESSIONS_READY, FIXTURE_TIMEOUT)
        child.send(b"\r")
        code, output, after = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(b"FAKE_LAUNCH=resume", output)
        _restored(self, child, after)

    def test_line_sessions_list_prints_the_listing_and_restores_terminal(self) -> None:
        child = self.sessions_child("charmer-pty-line-sessions-", ["--line", "sessions", "list"])
        code, output, after = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(b"11111111", output)
        self.assertNotIn(b"resume which session", output)
        self.assertNotIn(b"FAKE_LAUNCH", output)
        _restored(self, child, after)

    def test_line_sessions_list_never_prompts_or_launches(self) -> None:
        child = self.sessions_child("charmer-pty-line-sessions-r-", ["--line", "sessions", "list"])
        child.send(b"1\n")
        code, output, after = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(b"EXIT=0", output)
        self.assertNotIn(b"resume which session", output)
        self.assertNotIn(b"FAKE_LAUNCH", output)
        _restored(self, child, after)

    def test_line_bare_resume_keeps_the_line_chooser_and_launches(self) -> None:
        child = self.sessions_child("charmer-pty-line-bare-r-", ["--line", "-r"])
        child.read_until(b"resume which session (number, Enter cancels): ", FIXTURE_TIMEOUT)
        child.send(b"1\n")
        code, output, after = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(b"FAKE_LAUNCH=resume", output)
        _restored(self, child, after)


def _steps_to(target_expr: str) -> str:
    """Child setup: print how many → reach ``target_expr`` from the dialog's initial profile
    (the session's own).

    Connected profiles lead the dialog's order; these journeys step by
    recency, so nothing counts as connected in the child."""

    return f"""
type(runtime).profile_readiness = lambda self, names, observations=None: {{}}
order = selection.profile_pick_order(runtime)
initial = record['profile'] if record['profile'] in order else order[0]
target = {target_expr}
print('STEPS=' + str((order.index(target) - order.index(initial)) % len(order)), flush=True)
"""


_LIVE_SEED = """(lambda default: next(
    n for n in sorted(runtime.catalog.seed_profiles)
    if n != catalog.DEFAULT_SEED
    and (lambda other: other is not None and other.relaunch_fields() == default.relaunch_fields()
         and other.lead.binding.key == default.lead.binding.key)(
        profile.evaluate(runtime.profiles.load(n), runtime.lineup_catalog(), bindings=runtime.bindings.bindings(),
                         effective=runtime.current_effective()).lineup)))(
    profile.evaluate(runtime.profiles.load(catalog.DEFAULT_SEED), runtime.lineup_catalog(),
                     bindings=runtime.bindings.bindings(), effective=runtime.current_effective()).lineup)"""
_RELAUNCH_SEED = """next(n for n in sorted(runtime.catalog.seed_profiles)
    if runtime.profiles.load(n).get('native_agents') != runtime.profiles.load(catalog.DEFAULT_SEED)['native_agents'])"""


class SessionsLineupPTYTests(_SessionsChildCase):
    """Bare ``-r``; ``T``; the derived LIVE / RELAUNCH seed; Enter."""

    def _to_target(self, child: PTYProcess) -> None:
        child.read_until(SESSIONS_READY, FIXTURE_TIMEOUT)
        steps = int(_line_of(bytes(child.output), b"STEPS="))
        offset = len(child.output)
        child.send(b"T")
        _read_after(child, offset, b"change lineup")
        _read_after(child, offset, b"mode")
        for _ in range(steps):
            offset = len(child.output)
            child.send(RIGHT_KEY)
            # Wait for the redraw of the next profile; curses updates only
            # what changed (it may scroll the mode row rather than redraw it).
            _read_after(child, offset, b"\x1b[")

    def test_live(self) -> None:
        child = self.sessions_child("charmer-pty-lineup-live-", ["-r"], setup=_steps_to(_LIVE_SEED))
        self._to_target(child)
        offset = len(child.output)
        child.send(b"\r")
        _read_after(child, offset, b"/reload-plugins")
        offset = len(child.output)
        child.send(b"\r")
        _read_after(child, offset, SESSIONS_READY)
        child.send(b"\x1b")
        code, output, after = child.finish()
        self.assertEqual(code, 0, output)
        # The "mode" label and its text are drawn with different attributes.
        self.assertIn("LIVE — running agents keep their model".encode(), output)
        self.assertEqual(int(_line_of(output, b"GEN=")), int(_line_of(output, b"GEN0=")) + 1)
        self.assertNotIn(b"FAKE_LAUNCH", output)
        _restored(self, child, after)

    def test_relaunch(self) -> None:
        child = self.sessions_child("charmer-pty-lineup-relaunch-", ["-r"], setup=_steps_to(_RELAUNCH_SEED))
        self._to_target(child)
        offset = len(child.output)
        child.send(b"\r")
        _read_after(child, offset, b"Recorded as a pending change")
        offset = len(child.output)
        child.send(b"\r")
        _read_after(child, offset, SESSIONS_READY)
        child.send(b"\x1b")
        code, output, after = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn("RELAUNCH — exit the session; resume applies it".encode(), output)
        self.assertEqual(_line_of(output, b"PENDING="), "True")
        self.assertEqual(int(_line_of(output, b"GEN=")), int(_line_of(output, b"GEN0=")))
        self.assertNotIn(b"FAKE_LAUNCH", output)
        _restored(self, child, after)

    def test_esc_in_the_lineup_dialog_changes_nothing(self) -> None:
        # Restores tp SessionsAndTransitionPTYTests.test_transition_esc_prints_command_without_mutating.
        child = self.sessions_child("charmer-pty-lineup-esc-", ["-r"])
        child.read_until(SESSIONS_READY, FIXTURE_TIMEOUT)
        offset = len(child.output)
        child.send(b"T")
        _read_after(child, offset, b"change lineup")
        offset = len(child.output)
        child.send(b"\x1b")
        _read_after(child, offset, SESSIONS_READY)
        child.send(b"\x1b")
        code, output, after = child.finish()
        self.assertEqual(code, 0, output)
        self.assertEqual(_line_of(output, b"UNCHANGED="), "True")
        self.assertNotIn(b"FAKE_LAUNCH", output)
        _restored(self, child, after)


class OnboardingJourneyPTYTests(unittest.TestCase):
    """Real curses, fixture transports, both exact-client outcomes."""

    def child(self, *, pool=False, unavailable=False, keyed=False):
        code = f'''
import sys, json, copy
import tests  # live-port tripwire, including this child
from unittest import mock
from test_onboarding_isolation import JourneyFixture, KeyedJourneyFixture
import test_openai_compat_keyed as keyed_fixture
from charmer import catalog, operator, state
from charmer import tui, views, profile, qualify
import charmer.cli.screens.providers as providers
import charmer.cli.screens.models as models
import charmer.cli.screens.profile_editor as editor
import charmer.cli.types as types
case = KeyedJourneyFixture() if {keyed!r} else JourneyFixture()
case.setUp()
runtime = case.runtime
pool = {pool!r}
key = 'custom-pool-reviewer' if pool else 'custom-vendor-fixture-reviewer'
pid = 'anthropic' if pool else 'openrouter'
if {keyed!r}:
    key, pid = keyed_fixture.KEYED_KEY, keyed_fixture.KEYED_ID
    state.atomic_write(case.secret_file, case.secret_file.read_bytes() +
                       f'{{keyed_fixture.KEYED_SECRET}}={{keyed_fixture.KEYED_VALUE}}\\n'.encode())
if {unavailable!r}:
    runtime.exact_client_runner = lambda request: qualify.ExactClientOutcome('inconclusive', 'unavailable')
try:
    if {keyed!r}:
        def new_provider_app(win):
            providers._ProvidersScreen(runtime, palette=tui.MONO_PALETTE, journal=lambda:None).run(win)
        tui.run_curses_on_streams(new_provider_app, sys.stdin, sys.stdout)
        layer = runtime.operator_snapshot().layer
        assert layer.route_status[pid] == 'approved'
        assert not case.http_calls
        # The TUI kind field, not a default, must have declared the keyed kind.
        declared = layer.providers[pid]
        assert declared.kind == operator.KEYED_KIND, declared.kind
        assert catalog.is_keyed_compat(declared.entry), declared.entry
        print('KEYED_KIND=' + declared.kind, flush=True)
        print('KEYED_ROUTE_APPROVED', flush=True)
    def provider_app(win):
        screen = providers._ProvidersScreen(runtime, palette=tui.MONO_PALETTE, journal=lambda:None)
        screen.selected = next(i for i,r in enumerate(screen.rows) if r.id == pid)
        screen.run(win)
    tui.run_curses_on_streams(provider_app, sys.stdin, sys.stdout)
    case.serve_current()
    if {keyed!r}:
        assert key not in runtime.current_effective().admitted_lines
        assert case.keyed_picker(key).selectable
        assert not case.http_calls
        print('KEYED_NOT_ADMITTED_SELECTABLE', flush=True)
    print('MODELS_STAGE', flush=True)
    real_admit = providers.ConnectActions.admit
    admission_calls = []
    def metadata_admit(actions, selected):
        with mock.patch.object(runtime, 'smoke', side_effect=AssertionError('admission inference')), \\
                mock.patch.object(runtime, 'qualify_post', side_effect=AssertionError('admission inference')):
            code = real_admit(actions, selected)
        admission_calls.append(code)
        return code
    providers.ConnectActions.admit = metadata_admit
    def model_app(win):
        screen = models._ModelsScreen(runtime, palette=tui.MONO_PALETTE)
        screen.index = screen.keys.index(key)
        screen.run(win)
    tui.run_curses_on_streams(model_app, sys.stdin, sys.stdout)
    assert admission_calls == [0], admission_calls
    print('ADMISSION_REQUESTS=0', flush=True)
    doc, ev = case.evaluation(key)
    if {unavailable!r}:
        assert not ev.errors, ev.errors
        assert any(w.code == 'exact-client' and 'unavailable' in w.message for w in ev.lineup.warnings), ev.lineup.warnings
        print('ATTENTION_EXACT_CLIENT', flush=True)
    assert not ev.errors, ev.errors
    doc['agents'] = {{}}
    model = views.picker_rows(views.line_rows(runtime.lineup_catalog(), runtime.current_effective(), custom_ids=frozenset()),
                              slot='cm-reviewer', bindings={{}}, lcat=runtime.lineup_catalog(),
                              eff=runtime.current_effective(), current=None)
    print('BIND_INDEX=' + str(next(i for i,row in enumerate(model.items) if row.key == key)), flush=True)
    def edit_app(win):
        st = tui.ProfileEditorState(doc, cat=runtime.lineup_catalog(), bindings={{}},
                                    effective=runtime.current_effective(), origin=None, is_seed=False)
        callbacks = editor._profile_editor_callbacks(runtime, tui.MONO_PALETTE, editor_state=st)
        tui.ProfileEditorScreen(st, palette=tui.MONO_PALETTE, callbacks=callbacks,
                                environ=runtime.environ, initial_focus='cm-reviewer').run(win)
    tui.run_curses_on_streams(edit_app, sys.stdin, sys.stdout)
    saved = runtime.profiles.load(doc['name'])
    assert saved['agents']['cm-reviewer']['model'] == key, saved
    target = types.LaunchTarget('profile', saved, saved['name'], True, 'Profile fixture-onboarding')
    prepared = runtime.prepare(target, action='fresh', passthrough=[])
    assert runtime.perform(prepared) == 0
    print('JOURNEY_LAUNCHED=' + str(len(case.launches)), flush=True)
    print('LISTINGS=' + str(len(case.listing_calls)), flush=True)
finally:
    case.doCleanups()
'''
        child = PTYProcess(code, extra_pythonpath=(REPO_ROOT, TESTS_ROOT))
        self.addCleanup(child.close)
        return child

    def journey(self, *, pool=False, unavailable=False, keyed=False):
        child = self.child(pool=pool, unavailable=unavailable, keyed=keyed)
        if keyed:
            child.read_until(b"providers", FIXTURE_TIMEOUT)
            child.send(b"N")
            child.read_until(b"add your own provider")
            child.send(b"\x1bOF\n")  # End: every field (the manual form)
            child.read_until(b"Provider id")
            child.send(b"chatco\n" + b"\x1bOC" + b"\nhttps://api.chatco.example/v1\n\nCHATCO_API_KEY\n\n\n\n\n\n")
            child.read_until(b"Declare this provider?", FIXTURE_TIMEOUT)
            child.send(b"\n")
            child.read_until(b"approve the credential route of provider chatco:", FIXTURE_TIMEOUT)
            child.send(b"y")
            child.read_until(b"declared provider chatco", FIXTURE_TIMEOUT)
            mark = len(child.output)
            child.send(b"\x1b")  # the result, then the picker and Providers
            _read_after(child, mark, b"Enter connect", FIXTURE_TIMEOUT)
            child.send(b"\x1b\x1b")
            child.read_until(b"KEYED_ROUTE_APPROVED", FIXTURE_TIMEOUT)
            self.assertEqual(_line_of(bytes(child.output), b"KEYED_KIND="), "openai-compatible")
        child.read_until(b"providers", FIXTURE_TIMEOUT)
        child.send(b"A")
        child.read_until(b"add models")
        if pool or keyed:
            child.send(DOWN_KEY + b"\n")
        else:
            child.send(b"\n")
            child.read_until(b"Confirm explicit action")
            child.send(b"y")
            # The account-filtered listing has its own consent after the public one.
            child.read_until(b"/user")
            child.send(b"y")
            child.read_until(b"advertised models")
            child.send(b" \n")
        child.read_until(b"declare model")
        # Keep the listing prefill, or enter sourced manual pool metadata.
        if keyed:
            child.send(b"chatco-chat-1\n\ncustom-chatco-chat\n200000\n\nfixture docs, date\n\n")
        elif pool:
            child.send(b"claude-fixture-reviewer-9\n\ncustom-pool-reviewer\n1000000\n\nfixture docs, date\n\n")
        else:
            # Keep the display name and other prefills. The family of an
            # aggregator line is "unknown": replace it (one more Backspace
            # would step back to the previous field).
            child.send(b"\n\n\n\n\n\n" + b"\x7f" * len("unknown") + b"openai\n")
        # Efforts and default; output bound; request agents, then the role.
        child.send(b"\n\n64000\n" + b"\x1bOC" + b"\ncm-reviewer\n")
        child.read_until(b"Declaration preview", FIXTURE_TIMEOUT)
        child.send(b"\n")
        child.read_until(b"declared custom-", FIXTURE_TIMEOUT)
        child.send(b"\x1b")  # result -> Providers
        child.send(b"\x1b")  # Providers -> next fixture stage
        child.read_until(b"MODELS_STAGE", FIXTURE_TIMEOUT)
        child.read_until(b"admit badge", FIXTURE_TIMEOUT)
        mark = len(child.output)
        child.send(b"\n")
        _read_after(child, mark, b"Confirm explicit action", FIXTURE_TIMEOUT)
        child.send(b"y")
        child.read_until(b"Action result", FIXTURE_TIMEOUT)
        child.send(b"\x1bq")
        child.read_until("qualify — choose checks".encode(), FIXTURE_TIMEOUT)
        child.send(b"\n\n\n")
        child.read_until(b"Qualification plan", FIXTURE_TIMEOUT)
        if pool:
            child.read_until(b"exact-client", FIXTURE_TIMEOUT)
        child.send(b"y")
        child.read_until(b"evidence recorded for", FIXTURE_TIMEOUT)
        child.send(b"\x1b\x1b")
        if unavailable:
            child.read_until(b"ATTENTION_EXACT_CLIENT", FIXTURE_TIMEOUT)
        child.read_until(b"BIND_INDEX=", FIXTURE_TIMEOUT)
        # The index comes from the fixture's actual picker inventory.
        index = int(_line_of(bytes(child.output), b"BIND_INDEX="))
        child.send(b"\n")
        child.read_until(b"choose model", FIXTURE_TIMEOUT)
        child.send(b"\x1bOH" + DOWN_KEY * index + b"\n\x13")
        child.read_until(b"saved fixture-onboarding", FIXTURE_TIMEOUT)
        child.send(b"\x1b")
        child.read_until(b"JOURNEY_LAUNCHED=1", FIXTURE_TIMEOUT)
        code, output, attrs = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(b"LISTINGS=" + (b"0" if pool or keyed else b"2"), output)
        if keyed:
            self.assertIn(b"KEYED_NOT_ADMITTED_SELECTABLE", output)
        self.assertIn(b"ADMISSION_REQUESTS=0", output)
        self.assertNotIn(b"Traceback", output)
        _restored(self, child, attrs)

    def test_openrouter_non_xai_journey(self):
        self.journey()

    def test_authenticated_pool_journey(self):
        self.journey(pool=True)

    def test_pool_missing_exact_client_warns_and_launches(self):
        self.journey(pool=True, unavailable=True)

    def test_provider_wizard_recommended_kind_and_cancel(self):
        code = '''
import sys
from charmer import tui, views
form = tui.OnboardingForm('new provider', views.provider_form_fields())
result = tui.run_curses_on_streams(form.run, sys.stdin, sys.stdout)
assert result is None
print('CANCELLED_WITHOUT_DECLARATION', flush=True)
'''
        child = PTYProcess(code, extra_pythonpath=(REPO_ROOT, TESTS_ROOT))
        self.addCleanup(child.close)
        child.read_until(b"Provider id")
        child.send(b"fixture-new\n")
        child.read_until(b"anthropic-compatible")
        child.read_until(b"recommended")
        child.send(b"\x1b")
        code, output, attrs = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(b"CANCELLED_WITHOUT_DECLARATION", output)
        _restored(self, child, attrs)


class KeyedJourneyPTYTests(unittest.TestCase):
    child = OnboardingJourneyPTYTests.child

    def test_keyed_tui_optional_admission_qualification_and_binding(self):
        OnboardingJourneyPTYTests.journey(self, keyed=True)
