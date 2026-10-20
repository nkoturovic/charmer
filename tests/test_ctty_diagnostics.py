"""Pure observer checks: fake InputState/poll and mocked descriptor/mode I/O."""

from __future__ import annotations

import errno
import json
import os
import select
import termios
import unittest
from types import SimpleNamespace
from unittest import mock

import _ctty_diagnostics as diagnostics


PLATFORMS = ("linux", "darwin")


class CttyDiagnosticsTests(unittest.TestCase):
    def patch(self, owner, name, **kwargs):
        patcher = mock.patch.object(owner, name, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def setUp(self):
        self.poll = mock.Mock()
        self.poll.poll.return_value = []
        self.initialize = mock.Mock()
        self.read_delegate = mock.Mock(return_value=None)
        case = self

        class InputState:
            def __init__(state, fd, *args, **kwargs):
                case.initialize(state, fd, *args, **kwargs)
                state.fd, state.poll = fd, case.poll
                state.poll.register(fd, select.POLLIN)

            def read(state, *args, **kwargs):
                return case.read_delegate(state, *args, **kwargs)

        self.input_state = InputState
        self.register_exit = self.patch(diagnostics.atexit, "register")
        self.setter = self.patch(diagnostics.termios, "tcsetattr", return_value=None)
        self.flags = self.patch(diagnostics.fcntl, "fcntl", side_effect=lambda fd, request: {
            0: os.O_RDONLY | os.O_NONBLOCK, 1: os.O_WRONLY, 9: os.O_RDWR,
        }[fd])
        self.stat = self.patch(diagnostics.os, "fstat", side_effect=lambda fd: SimpleNamespace(
            st_dev=11, st_ino=100 + fd, st_rdev=17, st_mode=0o20620))
        self.isatty = self.patch(diagnostics.os, "isatty", return_value=True)
        self.writer = self.patch(diagnostics.os, "write", side_effect=lambda fd, data: len(data))
        self.observer = diagnostics._Observer(9)
        self.observer.install(InputState)

    def report(self):
        self.register_exit.call_args.args[0]()
        self.writer.assert_called_once()
        fd, payload = self.writer.call_args.args
        self.assertEqual(fd, 2)
        self.assertLessEqual(len(payload), diagnostics._LIMIT)
        self.assertTrue(payload.startswith(diagnostics._PREFIX))
        self.assertTrue(payload.endswith(b"\n"))
        return json.loads(payload[len(diagnostics._PREFIX):])

    def attributes(self):
        cc = [b"\0"] * 32
        cc[termios.VMIN], cc[termios.VTIME] = b"\1", 0
        return [termios.IXON, 0, termios.CS8,
                termios.ECHO | termios.ICANON | termios.ISIG, 0, 0, cc]

    def test_install_only_registers_the_exit_callback(self):
        self.register_exit.assert_called_once_with(self.observer.emit)
        for operation in (self.poll.poll, self.poll.register, self.setter,
                          self.flags, self.stat, self.isatty, self.writer):
            operation.assert_not_called()

    def test_snapshot_follows_original_init_and_reads_only_the_three_owned_fds(self):
        def flags(fd, request):
            initialized = self.initialize.call_args.args[0]
            self.assertEqual(initialized.fd, 0)
            self.assertIs(initialized.poll, self.poll)
            self.assertEqual(request, diagnostics.fcntl.F_GETFL)
            return {0: os.O_RDONLY | os.O_NONBLOCK, 1: os.O_WRONLY, 9: os.O_RDWR}[fd]

        self.flags.side_effect = flags
        state = self.input_state(0, mock.sentinel.argument, token=mock.sentinel.keyword)
        self.initialize.assert_called_once_with(state, 0, mock.sentinel.argument, token=mock.sentinel.keyword)
        self.poll.register.assert_called_once_with(0, select.POLLIN)
        event = self.report()["events"][0]
        self.assertEqual(event["event"], "input-init")
        self.assertEqual(event["fd"], 0)
        self.assertEqual([item["fd"] for item in event["fds"]], [0, 1, 9])
        self.assertEqual([item["access"] for item in event["fds"]], ["rdonly", "wronly", "rdwr"])
        self.assertEqual([item["nonblock"] for item in event["fds"]], [True, False, False])
        for fd, facts in zip((0, 1, 9), event["fds"]):
            self.assertEqual((facts["dev"], facts["ino"], facts["rdev"], facts["mode"], facts["isatty"]),
                             (11, 100 + fd, 17, 0o20620, True))
        self.assertEqual(self.flags.call_args_list,
                         [mock.call(fd, diagnostics.fcntl.F_GETFL) for fd in (0, 1, 9)])
        self.assertEqual(self.stat.call_args_list, [mock.call(fd) for fd in (0, 1, 9)])
        self.assertEqual(self.isatty.call_args_list, [mock.call(fd) for fd in (0, 1, 9)])
        self.poll.poll.assert_not_called()
        self.setter.assert_not_called()

    def test_read_and_existing_polls_forward_unchanged_with_ordinary_and_peek_context(self):
        state = self.input_state(0)
        window = SimpleNamespace(getch=mock.Mock())
        native_getch = window.getch
        values = iter(("\x1b", None, mock.sentinel.other))

        def read(state, win, **kwargs):
            self.assertIs(win, window)
            self.assertIs(state.poll.poll(0), self.poll.poll.return_value)
            return next(values)

        self.read_delegate.side_effect = read
        self.assertEqual(state.read(window), "\x1b")
        self.assertIsNone(state.read(window, deadline=12.5))
        self.assertIs(state.read(window), mock.sentinel.other)
        self.assertIs(window.getch, native_getch)
        native_getch.assert_not_called()
        self.assertEqual(self.read_delegate.call_args_list,
                         [mock.call(state, window), mock.call(state, window, deadline=12.5), mock.call(state, window)])
        report = self.report()
        reads = [event for event in report["events"] if event["event"] == "read"]
        polls = [event for event in report["events"] if event["event"] == "poll"]
        self.assertEqual([(event["read"], event["context"], event["result"]) for event in reads],
                         [(1, "ordinary", "esc"), (2, "peek", "none"), (3, "ordinary", "other")])
        self.assertEqual([(event["read"], event["context"]) for event in polls],
                         [(1, "ordinary"), (2, "peek"), (3, "ordinary")])
        self.assertEqual([event["seq"] for event in report["events"]], list(range(1, 8)))
        self.assertEqual(self.observer.context, {})
        self.assertFalse(report["truncated"])
        self.assertEqual(self.poll.poll.call_args_list, [mock.call(0)] * 3)
        self.setter.assert_not_called()
        self.assertEqual(self.flags.call_count, 3)  # No validity probes on ordinary results.

    def test_read_exception_preserves_identity_errno_and_excludes_exception_text(self):
        state = self.input_state(0)
        error = OSError(errno.EIO, "fixture-private-error-text")
        self.read_delegate.side_effect = error
        with self.assertRaises(OSError) as caught:
            state.read(mock.sentinel.window, deadline=12.5)
        self.assertIs(caught.exception, error)
        event = self.report()["events"][-1]
        self.assertEqual((event["event"], event["exception"], event["errno"], event["context"]),
                         ("read-raise", "OSError", errno.EIO, "peek"))
        self.assertNotIn(b"fixture-private-error-text", self.writer.call_args.args[1])
        self.assertEqual(self.observer.context, {})
        self.poll.poll.assert_not_called()
        self.setter.assert_not_called()

    def test_read_interrupt_is_not_replaced(self):
        state = self.input_state(0)
        error = KeyboardInterrupt()
        self.read_delegate.side_effect = error
        with self.assertRaises(KeyboardInterrupt) as caught:
            state.read(mock.sentinel.window)
        self.assertIs(caught.exception, error)
        event = self.report()["events"][-1]
        self.assertEqual((event["exception"], event["errno"]), ("KeyboardInterrupt", None))

    def test_fault_snapshots_precede_simulated_fd_restoration_for_each_bad_poll_bit(self):
        state = self.input_state(0)
        for flag in (select.POLLHUP, select.POLLERR, select.POLLNVAL):
            with self.subTest(flag=flag):
                restored = False
                events = [(0, flag)]
                self.poll.poll.return_value = events
                error = ValueError("original-read-error")

                def flags(fd, request):
                    self.assertFalse(restored, "fault snapshot ran after descriptor restoration")
                    return os.O_RDWR

                def read(state, win, **kwargs):
                    nonlocal restored
                    self.assertIs(state.poll.poll(0), events)
                    restored = True
                    raise error

                self.flags.side_effect = flags
                self.read_delegate.side_effect = read
                with self.assertRaises(ValueError) as caught:
                    state.read(mock.sentinel.window, deadline=12.5)
                self.assertIs(caught.exception, error)
        report = self.report()
        faults = [event for event in report["events"] if event["event"] == "poll"]
        self.assertEqual([event["events"] for event in faults],
                         [[[0, flag]] for flag in (select.POLLHUP, select.POLLERR, select.POLLNVAL)])
        for event in faults:
            self.assertEqual(event["context"], "peek")
            self.assertEqual([fd["access"] for fd in event["fds"]], ["rdwr"] * 3)
        self.assertEqual(self.flags.call_count, 12)  # Init plus one snapshot per existing bad result.
        self.assertEqual(self.poll.poll.call_args_list, [mock.call(0)] * 3)
        self.setter.assert_not_called()

    def test_poll_exception_preserves_identity_and_takes_a_validity_snapshot(self):
        state = self.input_state(0)
        error = OSError(errno.EINVAL, "fixture-private-error-text")
        self.poll.poll.side_effect = error
        with self.assertRaises(OSError) as caught:
            state.poll.poll(0)
        self.assertIs(caught.exception, error)
        event = self.report()["events"][-1]
        self.assertEqual((event["event"], event["exception"], event["errno"]),
                         ("poll-raise", "OSError", errno.EINVAL))
        self.assertEqual([fd["fd"] for fd in event["fds"]], [0, 1, 9])
        self.assertEqual(self.flags.call_count, 6)
        self.poll.poll.assert_called_once_with(0)
        self.setter.assert_not_called()

    def test_poll_result_cap_keeps_the_offending_entry_and_returns_the_original_list(self):
        state = self.input_state(0)
        events = [(fd, select.POLLIN) for fd in range(10, 16)] + [(0, select.POLLNVAL)]
        self.poll.poll.return_value = events
        self.assertIs(state.poll.poll(0), events)
        state.poll.modify(0, select.POLLIN)
        self.poll.modify.assert_called_once_with(0, select.POLLIN)
        report = self.report()
        event = report["events"][-1]
        self.assertEqual(len(event["events"]), diagnostics._MAX_POLL_EVENTS)
        self.assertIn([0, select.POLLNVAL], event["events"])
        self.assertEqual(event["events_omitted"], len(events) - diagnostics._MAX_POLL_EVENTS)
        self.assertEqual(report["poll_events_omitted"], event["events_omitted"])
        self.assertTrue(report["truncated"])
        self.poll.poll.assert_called_once_with(0)

    def test_tcsetattr_forwards_the_original_arguments_result_and_requested_bits(self):
        attributes = self.attributes()
        self.setter.return_value = mock.sentinel.result
        self.assertIs(diagnostics.termios.tcsetattr(0, termios.TCSANOW, attributes), mock.sentinel.result)
        self.setter.assert_called_once_with(0, termios.TCSANOW, attributes)
        self.assertIs(self.setter.call_args.args[2], attributes)
        event = self.report()["events"][0]
        self.assertEqual((event["event"], event["fd"], event["action"], event["ok"]),
                         ("tcsetattr", 0, termios.TCSANOW, True))
        self.assertEqual(event["mode"], {"ixon": True, "istrip": False, "cs8": True,
                                         "echo": True, "icanon": True, "isig": True, "vmin": 1, "vtime": 0})
        self.flags.assert_not_called()
        self.poll.poll.assert_not_called()

    def test_tcsetattr_preserves_the_original_termios_error_and_numeric_errno(self):
        attributes = self.attributes()
        error = termios.error(errno.EBADF, "fixture-private-error-text")
        self.setter.side_effect = error
        with self.assertRaises(termios.error) as caught:
            diagnostics.termios.tcsetattr(0, termios.TCSANOW, attributes)
        self.assertIs(caught.exception, error)
        self.setter.assert_called_once_with(0, termios.TCSANOW, attributes)
        event = self.report()["events"][0]
        self.assertFalse(event["ok"])
        self.assertEqual(event["errno"], errno.EBADF)
        self.assertEqual([fd["fd"] for fd in event["fds"]], [0, 1, 9])
        self.assertNotIn(b"fixture-private-error-text", self.writer.call_args.args[1])
        self.poll.poll.assert_not_called()

    def test_init_exception_is_not_replaced_and_adds_no_poll(self):
        error = OSError(errno.ENOTTY, "fixture-private-error-text")
        self.initialize.side_effect = error
        with self.assertRaises(OSError) as caught:
            self.input_state(0)
        self.assertIs(caught.exception, error)
        event = self.report()["events"][0]
        self.assertEqual((event["event"], event["errno"]), ("input-init-raise", errno.ENOTTY))
        self.poll.register.assert_not_called()
        self.poll.poll.assert_not_called()
        self.setter.assert_not_called()

    def test_metadata_read_errors_are_structured_and_do_not_hide_the_original_call(self):
        error = OSError(errno.EBADF, "fixture-private-error-text")
        self.flags.side_effect = self.stat.side_effect = error
        self.isatty.side_effect = ValueError("fixture-private-error-text")
        self.input_state(0)
        event = self.report()["events"][0]
        for fd in event["fds"]:
            self.assertEqual(fd["getfl_error"]["errno"], errno.EBADF)
            self.assertEqual(fd["fstat_error"]["errno"], errno.EBADF)
            self.assertEqual(fd["isatty_error"]["exception"], "ValueError")
        self.assertNotIn(b"fixture-private-error-text", self.writer.call_args.args[1])
        self.poll.register.assert_called_once_with(0, select.POLLIN)

    def test_memory_and_exit_byte_bounds_prioritize_failure_and_count_all_record_loss(self):
        state = self.input_state(0)
        for _ in range(300):
            state.read(mock.sentinel.window)
        self.poll.poll.return_value = [(0, select.POLLNVAL)]
        state.poll.poll(0)  # The first failure must survive the later noise.
        for _ in range(300):
            state.read(mock.sentinel.window)
        self.setter.side_effect = termios.error(errno.EBADF, "fixture-private-error-text")
        attributes = self.attributes()
        for _ in range(100):
            with self.assertRaises(termios.error):
                diagnostics.termios.tcsetattr(0, termios.TCSANOW, attributes)
        self.assertLessEqual(len(self.observer.records), diagnostics._MAX_RECORDS)
        report = self.report()
        self.assertTrue(report["truncated"])
        self.assertEqual(report["records_seen"], 702)
        self.assertEqual(report["records_dropped"], 702 - len(report["events"]))
        self.assertEqual(report["events"][0]["event"], "input-init")
        first_failure = next(event for event in report["events"] if event["event"] == "poll")
        self.assertEqual(first_failure["events"], [[0, select.POLLNVAL]])
        self.assertEqual([fd["fd"] for fd in first_failure["fds"]], [0, 1, 9])
        self.assertEqual(self.read_delegate.call_count, 600)
        self.poll.poll.assert_called_once_with(0)
        self.assertEqual(self.setter.call_count, 100)

    def test_exit_write_is_once_only_without_retry_even_if_the_pipe_write_fails(self):
        self.writer.side_effect = OSError(errno.EPIPE, "fixture-pipe-closed")
        callback = self.register_exit.call_args.args[0]
        callback()
        callback()
        self.writer.assert_called_once()
        self.assertEqual(self.writer.call_args.args[0], 2)
        self.assertLessEqual(len(self.writer.call_args.args[1]), diagnostics._LIMIT)
        self.poll.poll.assert_not_called()
        self.setter.assert_not_called()

    def test_pipe_summary_is_escaped_bounded_and_retains_error_head_and_observer_tail(self):
        tail = diagnostics._PREFIX + b'{"fixture":true}\n'
        output = b"\x1b[31mfixture-error\n" + b"x" * 20000 + tail + b"y" * 20000
        detail = diagnostics.pipe_summary(output, tty_bytes=999999)
        self.assertIn("tty_bytes=999999", detail)
        self.assertIn("pipe_truncated=True", detail)
        self.assertIn("fixture-error", detail)
        self.assertIn(tail[:-1].decode("ascii"), detail)
        self.assertNotIn("\x1b", detail)
        self.assertNotIn("\n", detail)
        self.assertLess(len(detail), 4 * (diagnostics._PIPE_LIMIT + 32) + 256)

    def test_pipe_summary_preserves_the_complete_observer_for_a_successful_child(self):
        self.report()
        output = b"EXIT=0\n" + self.writer.call_args.args[1]
        detail = diagnostics.pipe_summary(output, tty_bytes=123)
        self.assertIn(repr(output), detail)
        self.assertIn("pipe_truncated=False", detail)
        self.assertIn("tty_bytes=123", detail)

    def card_child(self, returncode=1):
        from tests.test_tui_pty import _CttyPipesChild

        # No constructor, PTY, subprocess or real descriptor operations.
        child = object.__new__(_CttyPipesChild)
        child.master, child.output = 20, bytearray(b"DO-NOT-DUMP-TTY" * 2000)
        child.process = mock.Mock(returncode=returncode)
        child.process.poll.return_value = returncode
        child.process.stdout.fileno.return_value = 21

        def wait(*, timeout):
            child.process.returncode = 1
            return 1

        child.process.wait.side_effect = wait
        child.read_until, child.send, child.finish = mock.Mock(), mock.Mock(), mock.Mock()
        child.finish.return_value = (0, b"profile: fixture-profile", b"EXIT=0\n")
        return child

    def run_card(self, child, *, output=b'error\nCTTY_DIAGNOSTICS={"event":"input-init-raise"}\n',
                 ready=True, read_error=None):
        from tests import test_tui_pty as pty_tests

        class Result(unittest.TestResult):
            def addFailure(result, test, error):
                result.original_failure = error[1]
                super().addFailure(test, error)

        def pipe_ready(*args):
            self.assertIsNotNone(child.process.returncode, "pipe inspected before existing reap")
            child.process.stdout.close.assert_not_called()
            return ([21] if ready else [], [], [])

        with mock.patch.object(pty_tests, "_CttyPipesChild", return_value=child), \
                mock.patch.object(pty_tests.tempfile, "mkdtemp", return_value="/fixture/ctty"), \
                mock.patch("shutil.rmtree"), \
                mock.patch.object(pty_tests, "_expected_default", return_value="fixture-profile"), \
                mock.patch.object(pty_tests.os, "close") as close, \
                mock.patch.object(diagnostics.select, "select", side_effect=pipe_ready) as select_pipe, \
                mock.patch.object(diagnostics.os, "read", return_value=output, side_effect=read_error) as read_pipe, \
                mock.patch("builtins.print") as printed:
            timeline = mock.Mock()
            for name, operation in (("poll", child.process.poll), ("kill", child.process.kill),
                                    ("wait", child.process.wait), ("master_close", close),
                                    ("stdin_close", child.process.stdin.close),
                                    ("pipe_fd", child.process.stdout.fileno), ("pipe_ready", select_pipe),
                                    ("pipe_read", read_pipe), ("report", printed),
                                    ("stdout_close", child.process.stdout.close)):
                timeline.attach_mock(operation, name)
            case = pty_tests.BareLaunchAutoDetectionPTYTests(
                "test_capable_terminal_card_routes_to_controlling_terminal")
            result = Result()
            case.run(result)  # Includes the target's real registered close cleanup.
        self.assertEqual(result.errors, [])
        return SimpleNamespace(result=result, printed=printed, select_pipe=select_pipe,
                               read_pipe=read_pipe, timeline=timeline)

    def test_before_ready_failure_reports_after_existing_cleanup_and_preserves_exception_identity(self):
        from tests.test_tui_pty import CARD_READY, FIXTURE_TIMEOUT

        for returncode in (1, None):
            with self.subTest(returncode=returncode):
                child = self.card_child(returncode)
                error = AssertionError("original-before-ready-failure")
                child.read_until.side_effect = error
                run = self.run_card(child)
                self.assertIs(run.result.original_failure, error)
                self.assertEqual(len(run.result.failures), 1)
                child.read_until.assert_called_once_with(CARD_READY, FIXTURE_TIMEOUT)
                child.send.assert_not_called()
                child.finish.assert_not_called()
                child.process.poll.assert_called_once_with()
                if returncode is None:
                    child.process.kill.assert_called_once_with()
                    child.process.wait.assert_called_once_with(timeout=2)
                else:
                    child.process.kill.assert_not_called()
                    child.process.wait.assert_not_called()
                run.select_pipe.assert_called_once_with([21], [], [], 0)
                run.read_pipe.assert_called_once_with(21, diagnostics._PIPE_LIMIT + 1)
                run.printed.assert_called_once()
                text = run.printed.call_args.args[0]
                self.assertIn("input-init-raise", text)
                self.assertIn(f"tty_bytes={len(child.output)}", text)
                self.assertNotIn("DO-NOT-DUMP-TTY", text)
                self.assertNotIn("\n", text)
                self.assertEqual([call[0] for call in run.timeline.mock_calls],
                                 ["poll"] + (["kill", "wait"] if returncode is None else [])
                                 + ["master_close", "stdin_close", "pipe_fd", "pipe_ready",
                                    "pipe_read", "report", "stdout_close"])
                self.assertIsNone(child.pipe_failure_report)
                child.process.stdout.read.assert_not_called()
                child.process.communicate.assert_not_called()

    def test_before_ready_capture_is_bounded_nonblocking_and_reports_unavailable_reads_without_error_text(self):
        cases = ((True, None, "at-limit"), (False, None, "not-ready"),
                 (True, OSError(errno.EIO, "fixture-private-error-text"), "unavailable"))
        for ready, read_error, status in cases:
            with self.subTest(status=status):
                child = self.card_child()
                error = AssertionError("original-before-ready-failure")
                child.read_until.side_effect = error
                run = self.run_card(child, output=b"\x1b" * (diagnostics._PIPE_LIMIT + 1),
                                    ready=ready, read_error=read_error)
                self.assertIs(run.result.original_failure, error)
                run.select_pipe.assert_called_once_with([21], [], [], 0)
                if ready:
                    run.read_pipe.assert_called_once_with(21, diagnostics._PIPE_LIMIT + 1)
                else:
                    run.read_pipe.assert_not_called()
                run.printed.assert_called_once()
                text = run.printed.call_args.args[0]
                self.assertIn(f"pipe_capture={status}", text)
                self.assertNotIn("\x1b", text)
                self.assertNotIn("\n", text)
                self.assertNotIn("fixture-private-error-text", text)
                self.assertLess(len(text), 4 * (diagnostics._PIPE_LIMIT + 32) + 512)
                child.process.poll.assert_called_once_with()
                child.process.kill.assert_not_called()
                child.process.wait.assert_not_called()
                child.process.stdout.close.assert_called_once_with()
                child.process.stdout.read.assert_not_called()
                child.process.communicate.assert_not_called()

    def test_before_ready_timeout_message_uses_tty_byte_count_without_diagnostic_io_or_extra_waits(self):
        from tests.test_tui_pty import CARD_READY, _CttyPipesChild

        child = self.card_child()
        child.pipe_failure_report = diagnostics.report_unready_pipe
        with mock.patch("tests.test_tui_pty.time.monotonic", return_value=0) as clock, \
                mock.patch.object(diagnostics.select, "select") as select_pipe, \
                mock.patch.object(diagnostics.os, "read") as read_pipe:
            with self.assertRaises(AssertionError) as caught:
                _CttyPipesChild.read_until(child, CARD_READY, timeout=0)
        self.assertEqual(str(caught.exception), f"did not observe {CARD_READY!r}; tty_bytes={len(child.output)}")
        self.assertEqual(clock.call_count, 2)
        select_pipe.assert_not_called()
        read_pipe.assert_not_called()
        child.process.poll.assert_not_called()
        child.process.kill.assert_not_called()
        child.process.wait.assert_not_called()

    def test_completed_target_reports_once_without_cleanup_pipe_reads_on_success_or_failure(self):
        from tests.test_tui_pty import CARD_READY, FIXTURE_TIMEOUT

        for returncode in (0, 1):
            with self.subTest(returncode=returncode):
                child = self.card_child(returncode)
                child.finish.return_value = (returncode, b"profile: fixture-profile", b"EXIT=fixture\n")
                run = self.run_card(child)
                self.assertEqual(run.result.wasSuccessful(), returncode == 0)
                self.assertEqual(len(run.result.failures), int(returncode != 0))
                child.read_until.assert_called_once_with(CARD_READY, FIXTURE_TIMEOUT)
                child.send.assert_called_once_with(b"\x1b")
                child.finish.assert_called_once_with(FIXTURE_TIMEOUT)
                self.assertIsNone(child.pipe_failure_report)
                run.printed.assert_called_once()
                self.assertNotIn("before_ready=True", run.printed.call_args.args[0])
                run.select_pipe.assert_not_called()
                run.read_pipe.assert_not_called()
                child.process.poll.assert_called_once_with()
                child.process.kill.assert_not_called()
                child.process.wait.assert_not_called()

    def test_early_reporting_error_does_not_replace_original_failure_or_skip_stdout_close(self):
        child = self.card_child(None)
        error = AssertionError("original-before-ready-failure")
        child.read_until.side_effect = error
        with mock.patch.object(diagnostics, "report_unready_pipe", side_effect=RuntimeError("fixture-report-error")):
            run = self.run_card(child)
        self.assertIs(run.result.original_failure, error)
        self.assertEqual(len(run.result.failures), 1)
        child.process.poll.assert_called_once_with()
        child.process.kill.assert_called_once_with()
        child.process.wait.assert_called_once_with(timeout=2)
        child.process.stdout.close.assert_called_once_with()
        self.assertIsNone(child.pipe_failure_report)
        run.select_pipe.assert_not_called()
        run.read_pipe.assert_not_called()
        run.printed.assert_not_called()
