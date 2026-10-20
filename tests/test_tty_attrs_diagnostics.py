"""Pure mocks for the attribute observer; no PTYs, native UI or process runs."""

from __future__ import annotations

import ast
import errno
import json
import termios
import unittest
from types import SimpleNamespace
from unittest import mock

import _tty_attrs_diagnostics as diagnostics


PLATFORMS = ("linux", "darwin")


def attributes():
    cc = [b"\0"] * termios.NCCS
    cc[:6] = [b"\3", 0, b"\1", 1, b"\xff", 255]
    return [termios.IXON | termios.ICRNL, termios.OPOST, termios.CS8 | termios.CREAD,
            termios.ICANON | termios.ECHO, 9600, 9600, cc]


class TtyAttrsDiagnosticsTests(unittest.TestCase):
    def patch(self, owner, name, **kwargs):
        patcher = mock.patch.object(owner, name, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def setUp(self):
        self.before = [attributes(), attributes()]
        self.saved = {0: attributes(), 1: attributes()}
        self.actual = {fd: attributes() for fd in (0, 1, 10, 11)}
        self.getter = self.patch(diagnostics.termios, "tcgetattr", side_effect=lambda fd: self.actual[fd])
        self.setter = self.patch(diagnostics.termios, "tcsetattr", return_value=mock.sentinel.setter_result)
        self.fstat = self.patch(diagnostics.os, "fstat", side_effect=lambda fd: SimpleNamespace(
            st_dev=7, st_ino=100 + fd, st_rdev=9, st_mode=0o20620))
        self.writer = self.patch(diagnostics.os, "write", side_effect=lambda fd, data: len(data))
        self.terminal = mock.Mock(side_effect=lambda fd: self.saved[fd])
        self.tui = SimpleNamespace(_terminal_attrs=self.terminal, _INPUT_STATE=None,
                                   InputState=mock.sentinel.input_state, read_key=mock.sentinel.read_key)
        tui = self.tui

        def run_curses_on_streams(fds=(0, 1)):
            saved_attrs = []
            for fd in fds:
                saved_attrs.append((fd, tui._terminal_attrs(fd)))

            def restore_attrs(fd, attrs):
                return diagnostics.termios.tcsetattr(fd, termios.TCSANOW, attrs)

            return [restore_attrs(fd, attrs) for fd, attrs in saved_attrs]

        self.tui.run_curses_on_streams = run_curses_on_streams
        self.observer = diagnostics.AttrsDiagnostics(self.tui, (10, 11), self.before)
        self.observer.attach(self.tui)

    def finish_observations(self):
        first = self.observer.final_getattr(10)
        last = self.observer.final_getattr(11)
        self.assertIs(first, self.actual[10])
        self.assertIs(last, self.actual[11])
        return self.report()

    def report(self, observer=None):
        (observer or self.observer).emit()
        self.writer.assert_called_once()
        fd, payload = self.writer.call_args.args
        self.assertEqual(fd, 2)
        self.assertLessEqual(len(payload), diagnostics._LIMIT)
        self.assertTrue(payload.startswith(diagnostics._PREFIX))
        self.assertTrue(payload.endswith(b"\n"))
        return json.loads(payload[len(diagnostics._PREFIX):])

    def fixture_assert(self):
        from tests.test_tui_pty import _ALIAS_TTY_SNAPSHOT

        tree = ast.parse(_ALIAS_TTY_SNAPSHOT)
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == "assert_alias_restored")
        predicate = next(node for node in function.body if isinstance(node, ast.Assert)
                         and isinstance(node.msg, ast.Constant) and node.msg.value == "exact alias attributes")
        self.assertEqual(ast.dump(predicate.test), ast.dump(ast.parse("alias_after == alias_before", mode="eval").body))
        # Execute ONLY the existing assertion function, never the fixture's
        # top-level open/getattr/setup code. All getters/identities are mocked.
        namespace = {"_alias_attrs_observer": self.observer, "alias_before": self.before,
                     "alias_streams": [SimpleNamespace(fileno=mock.Mock(return_value=fd)) for fd in (10, 11)],
                     "fd_identity": mock.Mock(side_effect=lambda fd: (7, fd, 9, 0o100600)),
                     "stdio_before": [(7, fd, 9, 0o100600) for fd in (0, 1)],
                     "termios": diagnostics.termios, "tui": self.tui, "print": mock.Mock()}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "fixture-assert", "exec"), namespace)
        return namespace

    def test_baselines_are_immutable_full_typed_snapshots_with_read_only_identities(self):
        self.assertEqual(self.fstat.call_args_list, [mock.call(10), mock.call(11)])
        self.getter.assert_not_called()
        self.setter.assert_not_called()
        self.writer.assert_not_called()
        row = self.observer.stages["B"][0]
        frozen = self.observer.snapshots[row["a"]]
        self.assertIsInstance(frozen, tuple)
        self.assertIsInstance(frozen[6], tuple)
        self.assertEqual(frozen[:6], tuple(self.before[0][:6]))
        self.assertEqual(frozen[6][:6], ("b03", "i0", "b01", "i1", "bff", "i255"))
        self.assertEqual(len(frozen[6]), termios.NCCS)
        self.assertEqual(row["id"], [7, 110, 9, 0o20620])
        self.assertEqual(len(self.observer.snapshots), 1)  # Equal complete vectors deduplicate.
        self.before[0][3] ^= termios.ECHO
        self.before[0][6][0] = b"\xff"
        self.assertNotEqual(frozen[3], self.before[0][3])
        self.assertEqual(frozen[6][0], "b03")

    def test_actual_save_and_restore_calls_forward_objects_once_and_add_only_two_readbacks(self):
        results = self.tui.run_curses_on_streams()
        self.assertEqual(results, [mock.sentinel.setter_result] * 2)
        self.assertEqual(self.terminal.call_args_list, [mock.call(0), mock.call(1)])
        self.assertEqual(self.setter.call_args_list,
                         [mock.call(fd, termios.TCSANOW, self.saved[fd]) for fd in (0, 1)])
        for call, fd in zip(self.setter.call_args_list, (0, 1)):
            self.assertIs(call.args[2], self.saved[fd])
        self.assertEqual(self.getter.call_args_list, [mock.call(0), mock.call(1)])
        report = self.finish_observations()
        self.assertEqual(self.getter.call_args_list, [mock.call(fd) for fd in (0, 1, 10, 11)])
        self.assertEqual(self.fstat.call_count, 2)
        self.assertEqual(report["status"], "COMPLETE")
        self.assertEqual(report["counts"], dict.fromkeys(("B", "S", "R", "A", "F"), 2))
        self.assertEqual(report["records_omitted"], 0)
        self.assertEqual(report["D"], [])
        self.assertEqual(report["attrs"][0][:6], self.before[0][:6])
        self.assertEqual(report["cc"][0][:6], ["b03", "i0", "b01", "i1", "bff", "i255"])
        self.assertIs(self.tui.InputState, mock.sentinel.input_state)
        self.assertIs(self.tui.read_key, mock.sentinel.read_key)

    def test_save_and_setter_argument_snapshots_precede_delegate_mutation_without_changing_original_lists(self):
        def setter(fd, action, attrs):
            self.assertIs(attrs, self.saved[fd])
            attrs[3] ^= termios.ECHO
            attrs[6][0] = 3
            return mock.sentinel.setter_result

        self.setter.side_effect = setter
        self.tui.run_curses_on_streams()
        for phase in ("S", "R"):
            for row in self.observer.stages[phase]:
                frozen = self.observer.snapshots[row["a"]]
                self.assertEqual(frozen[3], self.before[0][3])
                self.assertEqual(frozen[6][0], "b03")
        self.assertEqual(self.saved[0][6][0], 3)  # Only the mock delegate changed it.
        self.assertEqual(self.setter.call_count, 2)

    def test_same_named_other_callers_are_not_mistaken_for_product_save_or_restore(self):
        result = self.tui._terminal_attrs(fd=0)
        self.assertIs(result, self.saved[0])

        def restore_attrs(fd, attrs):
            return diagnostics.termios.tcsetattr(fd, termios.TCSADRAIN, attrs)

        self.assertIs(restore_attrs(0, self.saved[0]), mock.sentinel.setter_result)
        self.terminal.assert_called_once_with(fd=0)
        self.setter.assert_called_once_with(0, termios.TCSADRAIN, self.saved[0])
        self.getter.assert_not_called()
        self.assertEqual(self.observer.counts["S"], 0)
        self.assertEqual(self.observer.counts["R"], 0)

    def test_save_exception_keeps_original_identity_and_does_not_add_any_getter_or_setter(self):
        error = OSError(errno.EIO, "fixture-private-error-text")
        self.terminal.side_effect = error
        with self.assertRaises(OSError) as caught:
            self.tui.run_curses_on_streams()
        self.assertIs(caught.exception, error)
        self.terminal.assert_called_once_with(0)
        self.getter.assert_not_called()
        self.setter.assert_not_called()
        report = self.report()
        self.assertEqual(report["S"][0]["e"], ["OSError", errno.EIO])
        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertNotIn(b"fixture-private-error-text", self.writer.call_args.args[1])

    def test_restore_exception_keeps_original_identity_and_does_not_trigger_a_readback(self):
        error = termios.error(errno.EBADF, "fixture-private-error-text")
        self.setter.side_effect = error
        with self.assertRaises(termios.error) as caught:
            self.tui.run_curses_on_streams()
        self.assertIs(caught.exception, error)
        self.setter.assert_called_once_with(0, termios.TCSANOW, self.saved[0])
        self.getter.assert_not_called()
        row = self.observer.stages["R"][0]
        self.assertFalse(row["ok"])
        self.assertEqual(row["e"], [type(error).__name__, errno.EBADF])

    def test_expected_readback_errors_are_metadata_and_preserve_the_original_setter_results(self):
        for error in (OSError(errno.EIO, "fixture-private-error-text"),
                      termios.error(errno.ENOTTY, "fixture-private-error-text")):
            with self.subTest(error_type=type(error).__name__):
                self.getter.side_effect = error
                self.assertEqual(self.tui.run_curses_on_streams(), [mock.sentinel.setter_result] * 2)
                self.assertEqual(self.getter.call_count, 2)
                self.assertEqual([row["e"] for row in self.observer.stages["A"]],
                                 [[type(error).__name__, error.args[0]]] * 2)
                self.assertIn("A-readback-error", self.observer.issues)
                # Fresh bounded records for the next exception class; no native calls.
                self.observer.stages["S"].clear()
                self.observer.stages["R"].clear()
                self.observer.stages["A"].clear()
                for phase in ("S", "R", "A"):
                    self.observer.counts[phase] = 0
                self.getter.reset_mock()

    def test_readback_keyboard_interrupt_is_not_swallowed_or_replaced(self):
        error = KeyboardInterrupt()
        self.getter.side_effect = error
        with self.assertRaises(KeyboardInterrupt) as caught:
            self.tui.run_curses_on_streams()
        self.assertIs(caught.exception, error)
        self.setter.assert_called_once_with(0, termios.TCSANOW, self.saved[0])
        self.getter.assert_called_once_with(0)

    def test_final_getter_forwards_arguments_and_returns_the_same_object_once(self):
        result = self.observer.final_getattr(fd=10)
        self.getter.assert_called_once_with(fd=10)
        self.assertIs(result, self.actual[10])
        reference = self.observer.stages["F"][0]["a"]
        result[6][0] = 3
        self.assertEqual(self.observer.snapshots[reference][6][0], "b03")

    def test_final_getter_exception_is_unchanged_and_emits_one_incomplete_record(self):
        error = OSError(errno.EIO, "fixture-private-error-text")
        self.getter.side_effect = error
        with self.assertRaises(OSError) as caught:
            self.observer.final_getattr(10)
        self.assertIs(caught.exception, error)
        self.getter.assert_called_once_with(10)
        report = self.report()  # Already emitted; never writes twice.
        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertEqual(report["F"][0]["e"], ["OSError", errno.EIO])
        self.assertNotIn(b"fixture-private-error-text", self.writer.call_args.args[1])

    def test_diffs_preserve_unknown_flag_bits_speed_changes_and_cc_type_differences(self):
        unknown = 1 << 60
        self.actual[10][3] ^= unknown | termios.ECHO
        self.actual[10][4] += 1
        self.actual[10][6][0] = 3  # Same byte value, different Python type.
        self.tui.run_curses_on_streams()
        report = self.finish_observations()
        diff = next(row for row in report["D"] if row["p"] == ["B0", "F0"])
        self.assertEqual(diff["i"], [3, 4, [6, 0]])
        self.assertEqual(diff["x"][0][0:2], [3, unknown | termios.ECHO])
        self.assertIn("ECHO", diff["x"][0][2])
        attrs = report["attrs"][report["F"][0]["a"]]
        self.assertEqual(attrs[3], self.actual[10][3])
        self.assertEqual(report["cc"][attrs[6]][0], "i3")

    def test_unexpected_shapes_forward_unchanged_but_are_explicitly_incomplete(self):
        self.saved[0] = mock.sentinel.unexpected_attributes
        self.assertEqual(self.tui.run_curses_on_streams(), [mock.sentinel.setter_result] * 2)
        self.assertIs(self.setter.call_args_list[0].args[2], mock.sentinel.unexpected_attributes)
        self.actual[10][6].pop()
        report = self.finish_observations()
        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertIn("S-shape", report["issues"])
        self.assertIn("R-shape", report["issues"])
        self.assertIn("F-cc-count", report["issues"])
        self.assertIsNone(report["S"][0]["a"])

    def test_extra_calls_are_forwarded_once_but_cannot_add_more_than_two_readbacks(self):
        self.tui.run_curses_on_streams(fds=(0, 1, 0))
        self.assertEqual(self.terminal.call_args_list, [mock.call(0), mock.call(1), mock.call(0)])
        self.assertEqual(self.setter.call_count, 3)
        self.assertEqual(self.getter.call_args_list, [mock.call(0), mock.call(1)])
        report = self.finish_observations()
        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertEqual(report["records_omitted"], 2)
        self.assertEqual(report["counts"]["S"], 3)
        self.assertEqual(report["counts"]["R"], 3)
        self.assertEqual(report["counts"]["A"], 2)
        self.assertIn("S-count", report["issues"])
        self.assertIn("R-count", report["issues"])

    def test_overflow_is_bounded_valid_json_and_explicitly_omits_data_without_claiming_complete(self):
        def big(index):
            return [(1 << 120) + index + field for field in range(6)] + [[bytes([index])] * termios.NCCS]

        observer = diagnostics.AttrsDiagnostics(self.tui, (10, 11), [big(0), big(1)])
        index = 2
        for phase in ("S", "R", "A", "F"):
            for slot in (0, 1):
                fd = slot + 10 if phase == "F" else slot
                facts = {"ok": True, "when": termios.TCSANOW} if phase == "R" else {}
                observer.record(phase, fd, big(index), **facts)
                index += 1
        report = self.report(observer)
        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertIn("record-overflow", report["issues"])
        self.assertGreater(report["full_bytes"], diagnostics._LIMIT)
        self.assertIn("D", report["omitted"])
        self.assertEqual(report["counts"], dict.fromkeys(("B", "S", "R", "A", "F"), 2))
        self.assertLessEqual(len(observer.snapshots), 10)

    def test_existing_exact_equality_consumes_final_getter_results_once_and_emits_before_failure(self):
        self.tui.run_curses_on_streams()
        self.actual[10][6][0] = 3  # Equal numeric value is still not equal to bytes.
        namespace = self.fixture_assert()

        def write(fd, payload):
            self.assertEqual(self.getter.call_count, 4)  # Two A plus the two existing F, never another getter.
            namespace["print"].assert_not_called()
            return len(payload)

        self.writer.side_effect = write
        with self.assertRaisesRegex(AssertionError, "exact alias attributes"):
            namespace["assert_alias_restored"]()
        report = self.report()
        self.assertEqual(report["status"], "COMPLETE")
        self.assertEqual(self.getter.call_args_list, [mock.call(fd) for fd in (0, 1, 10, 11)])
        self.assertNotIn(b"PTY", self.writer.call_args.args[1])
        namespace["print"].assert_not_called()

    def test_exit_write_error_does_not_skip_or_replace_the_original_exact_assertion(self):
        self.tui.run_curses_on_streams()
        self.actual[10][3] ^= termios.ECHO
        namespace = self.fixture_assert()
        self.writer.side_effect = OSError(errno.EPIPE, "fixture-private-error-text")
        with self.assertRaisesRegex(AssertionError, "exact alias attributes"):
            namespace["assert_alias_restored"]()
        self.observer.emit()
        self.writer.assert_called_once()
        self.assertEqual(self.getter.call_count, 4)

    def test_parent_failure_message_uses_bounded_escaped_pipe_only_without_weakening_exit_code(self):
        from tests.test_tui_pty import AliasInputHealthPTYTests, TIMEOUT

        case = AliasInputHealthPTYTests()
        pipe = b"\x1bfixture-error\n" + b"x" * 20000 + diagnostics._PREFIX + b'{"status":"INCOMPLETE"}\n'
        child = SimpleNamespace(finish=mock.Mock(return_value=(1, b"DO-NOT-DUMP-TTY" * 2000, pipe)))
        with mock.patch("builtins.print") as printed:
            with self.assertRaises(AssertionError) as caught:
                case.finish_keys(child, [["char", "界"]], attrs_diagnostics=True)
        child.finish.assert_called_once_with(TIMEOUT)
        printed.assert_called_once()
        message = str(caught.exception)
        self.assertIn("1 != 0", message)
        self.assertIn("INCOMPLETE", message)
        self.assertIn("pipe_truncated=True", message)
        self.assertNotIn("DO-NOT-DUMP-TTY", message)
        self.assertNotIn("\x1b", message)
        self.assertLess(len(message), 4 * 8192 + 1024)

    def test_only_selected_reader_opts_in_and_original_input_sleep_and_poll_sequence_stays(self):
        from tests import test_tui_pty as pty_tests

        self.assertNotIn("from _tty_attrs_diagnostics import install", pty_tests._alias_reader_code())
        self.assertIn("from _tty_attrs_diagnostics import install", pty_tests._alias_reader_code(attrs_diagnostics=True))
        case = pty_tests.AliasInputHealthPTYTests()
        child = mock.Mock(output=bytearray())
        child.process.poll.return_value = None
        case.reader, case.finish_keys = mock.Mock(return_value=child), mock.Mock()
        with mock.patch.object(pty_tests.time, "sleep") as sleep:
            case.test_split_utf8_across_alias_idle_slices_preserves_the_character()
        case.reader.assert_called_once_with(attrs_diagnostics=True)
        self.assertEqual(child.send.call_args_list, [mock.call(b"\xe7"), mock.call(b"\x95\x8c")])
        sleep.assert_called_once_with(0.25)
        child.process.poll.assert_called_once_with()
        case.finish_keys.assert_called_once_with(child, [["char", "界"]], attrs_diagnostics=True)

    def test_reader_failure_source_omits_pty_bytes_without_new_io_or_exception_chaining(self):
        from tests import test_tui_pty as pty_tests

        child = object.__new__(pty_tests._CttyPipesChild)
        child.master, child.output = 20, bytearray(b"DO-NOT-DUMP-TTY" * 2000)
        child.process = mock.Mock()
        case = pty_tests.AliasInputHealthPTYTests()
        case.child = mock.Mock(return_value=child)
        with mock.patch.object(pty_tests.time, "monotonic", side_effect=[0, pty_tests.FIXTURE_TIMEOUT]) as clock, \
                mock.patch.object(pty_tests.select, "select") as select_pipe, \
                mock.patch.object(pty_tests.os, "read") as read, \
                mock.patch.object(pty_tests.time, "sleep") as sleep:
            with self.assertRaises(AssertionError) as caught:
                case.reader(attrs_diagnostics=True)
        self.assertTrue(child.metadata_only_failures)
        self.assertEqual(str(caught.exception),
                         f"did not observe {b'ALIAS-INPUT-READY'!r}; tty_bytes={len(child.output)}")
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual(clock.call_count, 2)
        select_pipe.assert_not_called()
        read.assert_not_called()
        sleep.assert_not_called()
        child.process.poll.assert_not_called()
        child.process.kill.assert_not_called()
        child.process.wait.assert_not_called()
        child.process.stdout.read.assert_not_called()

    def test_finish_timeout_source_omits_pty_bytes_and_preserves_existing_poll_kill_wait_sequence(self):
        from tests import test_tui_pty as pty_tests

        child = object.__new__(pty_tests._CttyPipesChild)
        child.master, child.output = 20, bytearray(b"DO-NOT-DUMP-TTY" * 2000)
        child.metadata_only_failures = True
        child.process = mock.Mock()
        child.process.poll.return_value = None
        timeline = mock.Mock()
        for name, operation in (("poll", child.process.poll), ("kill", child.process.kill),
                                ("wait", child.process.wait)):
            timeline.attach_mock(operation, name)
        case = pty_tests.AliasInputHealthPTYTests()
        with mock.patch.object(pty_tests.time, "monotonic", side_effect=[0, pty_tests.TIMEOUT]) as clock, \
                mock.patch.object(pty_tests.select, "select") as select_pipe, \
                mock.patch.object(pty_tests.os, "read") as read, \
                mock.patch.object(pty_tests.os, "close") as close, \
                mock.patch.object(pty_tests.time, "sleep") as sleep, \
                mock.patch("builtins.print") as printed:
            with self.assertRaises(AssertionError) as caught:
                case.finish_keys(child, [["char", "界"]], attrs_diagnostics=True)
        self.assertEqual(str(caught.exception), f"child timed out; tty_bytes={len(child.output)}")
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual(clock.call_count, 2)
        self.assertEqual(timeline.mock_calls, [mock.call.poll(), mock.call.poll(), mock.call.kill(), mock.call.wait(timeout=2)])
        select_pipe.assert_not_called()
        read.assert_not_called()
        close.assert_not_called()
        sleep.assert_not_called()
        printed.assert_not_called()
        child.process.stdout.read.assert_not_called()
        child.process.stdout.close.assert_not_called()
        child.process.stdin.close.assert_not_called()

    def test_metadata_safe_helper_failures_are_enabled_before_readiness_only_for_diagnostic_readers(self):
        from tests import test_tui_pty as pty_tests

        for options in ({}, {"attrs_diagnostics": True}):
            with self.subTest(options=options):
                case = pty_tests.AliasInputHealthPTYTests()
                child = SimpleNamespace(read_until=mock.Mock())
                case.child = mock.Mock(return_value=child)

                def ready(*args):
                    self.assertEqual(child.metadata_only_failures, bool(options))

                child.read_until.side_effect = ready
                self.assertIs(case.reader(**options), child)
                child.read_until.assert_called_once_with(b"ALIAS-INPUT-READY", pty_tests.FIXTURE_TIMEOUT)
        self.assertFalse(pty_tests._CttyPipesChild.metadata_only_failures)

    def test_each_postreturn_assertion_keeps_its_full_predicate_but_reports_only_bounded_pipe_detail(self):
        from tests.test_tui_pty import AliasInputHealthPTYTests, TIMEOUT

        expected = [["char", "界"]]
        padding = b"p" * 10000 + b"DO-NOT-APPEND-RAW-PIPE-MIDDLE" + b"p" * 10000
        observer = diagnostics._PREFIX + b'{"status":"COMPLETE"}\n'
        cases = [("keys", None), ("alias-marker", None)]
        cases += [(marker, channel) for marker in (b"PRIMARY_INPUT_ERROR=", b"FAKE_LAUNCH", b"Traceback")
                  for channel in ("tty", "pipe")]
        for failure, channel in cases:
            with self.subTest(failure=failure, channel=channel):
                case = AliasInputHealthPTYTests()
                actual = [["char", "wrong-" + "k" * 20000]] if failure == "keys" else expected
                pipe = b"KEYS=" + json.dumps(actual).encode("ascii") + b"\n"
                if failure != "alias-marker":
                    pipe += b"ALIAS_RESTORED=True\n"
                tty = b"DO-NOT-DUMP-TTY" * 2000
                if channel == "tty":
                    tty += failure
                if channel == "pipe":
                    pipe += failure + b"\n"
                pipe += padding + observer
                child = SimpleNamespace(finish=mock.Mock(return_value=(0, tty, pipe)))
                with mock.patch("builtins.print") as printed:
                    with self.assertRaises(AssertionError) as caught:
                        case.finish_keys(child, expected, attrs_diagnostics=True)
                # Exact message equality catches both raw TTY dumps and any
                # unittest-appended, unbounded rendering of pipe/key values.
                self.assertEqual(str(caught.exception), diagnostics.pipe_summary(pipe))
                self.assertNotIn("DO-NOT-DUMP-TTY", str(caught.exception))
                self.assertNotIn("DO-NOT-APPEND-RAW-PIPE-MIDDLE", str(caught.exception))
                self.assertLess(len(str(caught.exception)), 4 * 8192 + 1024)
                self.assertTrue(case.longMessage)
                child.finish.assert_called_once_with(TIMEOUT)
                printed.assert_called_once()

    def test_postreturn_key_parse_errors_keep_original_exception_types_and_restore_safe_message_scope(self):
        from tests.test_tui_pty import AliasInputHealthPTYTests, TIMEOUT

        cases = ((b"ALIAS_RESTORED=True\n", IndexError),
                 (b"KEYS=not-json\nALIAS_RESTORED=True\n", json.JSONDecodeError),
                 (b"KEYS=\xff\nALIAS_RESTORED=True\n", UnicodeDecodeError))
        for pipe, error_type in cases:
            with self.subTest(error_type=error_type.__name__):
                case = AliasInputHealthPTYTests()
                child = SimpleNamespace(finish=mock.Mock(return_value=(0, b"DO-NOT-DUMP-TTY" * 2000, pipe)))
                with mock.patch("builtins.print") as printed:
                    with self.assertRaises(error_type) as caught:
                        case.finish_keys(child, [["char", "界"]], attrs_diagnostics=True)
                self.assertNotIn("DO-NOT-DUMP-TTY", str(caught.exception))
                self.assertLess(len(str(caught.exception)), 256)
                self.assertTrue(case.longMessage)
                child.finish.assert_called_once_with(TIMEOUT)
                printed.assert_called_once()
