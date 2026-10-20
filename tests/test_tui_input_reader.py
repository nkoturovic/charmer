"""Finite input waits, decoding, admission and context lifetime; no live terminal."""
from __future__ import annotations

import copy
import curses
import select
import termios
import unittest
from types import SimpleNamespace
from unittest import mock

from charmer import errors as cli_errors
from charmer import tui
from test_tui import FakeWindow, IDLE, _FdStream, _SessionHarness


class Clock:
    def __init__(self):
        self.now = 10.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class ByteWindow:
    """Every event is a byte/special/error and its elapsed native time."""

    def __init__(self, clock, events=()):
        self.clock = clock
        self.events = list(events)
        self.waits = []
        self.calls = 0

    def timeout(self, ms):
        if not 0 < ms <= tui.INPUT_SLICE_MS:
            raise AssertionError("expected a positive finite input slice")
        self.waits.append(ms)

    def getch(self):
        self.calls += 1
        if not self.events:
            raise AssertionError("ByteWindow script exhausted")
        value, elapsed = self.events.pop(0)
        self.clock.now += self.waits[-1] / 1000 if value is IDLE else elapsed
        if isinstance(value, BaseException):
            raise value
        return -1 if value is IDLE else value


def bytes_of(data):
    return [(byte, 0) for byte in data]


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.poll = mock.Mock()
        self.poll.poll.return_value = []
        for patcher in (mock.patch.object(tui, "_clock", self.clock),
                        mock.patch.object(tui.time, "sleep", self.clock.sleep),
                        mock.patch.object(tui.select, "poll", return_value=self.poll),
                        mock.patch.object(tui, "_INPUT_STATE", None)):
            patcher.start()
            self.addCleanup(patcher.stop)
        tui._carry_reply(None)
        self.addCleanup(tui._carry_reply, None)

    def window(self, events=(), encoding="utf-8"):
        with mock.patch.object(tui.locale, "getencoding", return_value=encoding):
            state = tui.InputState(0)
        win = ByteWindow(self.clock, events)
        state.window = win
        tui._INPUT_STATE = state
        self.addCleanup(state.clear)
        return win, state

    def darwin_nval(self):
        self.enterContext(mock.patch.object(tui.sys, "platform", "darwin"))
        attrs = self.enterContext(mock.patch.object(
            tui, "_terminal_attrs", return_value=[0, 0, termios.CS8, 0, 0, 0, []]))
        selector = self.enterContext(mock.patch.object(
            tui.select, "select", return_value=([], [], [])))
        self.poll.poll.return_value = [(0, select.POLLNVAL)]
        return attrs, selector

    def test_split_utf8_across_multiple_slices_has_no_idle_key(self):
        win, state = self.window([(0xe7, 0), (IDLE, 0), (IDLE, 0), (0x95, 0), (0x8c, 0)])
        self.assertEqual(tui.read_key(win), tui.Key("char", "界"))
        self.assertEqual(win.waits, [100] * 5)
        self.assertEqual(state.decoder.getstate()[0], b"")
        self.poll.register.assert_called_once_with(0, select.POLLIN)
        self.assertEqual(self.poll.poll.call_args_list, [mock.call(0)] * 2)

    def test_one_peek_budget_preserves_incomplete_character(self):
        win, state = self.window([(0xc3, 0.03), (IDLE, 0)])
        self.assertIsNone(tui._peek(win))
        self.assertAlmostEqual(self.clock.now, 10.08)
        self.assertLessEqual(win.waits[1], 50)
        self.assertEqual(state.decoder.getstate()[0], b"\xc3")
        win.events += [(0xb6, 0)]
        self.assertEqual(tui._next_value(win), "ö")

    def test_special_and_resize_bypass_and_preserve_partial_bytes(self):
        win, state = self.window([(0xc3, 0), (curses.KEY_RESIZE, 0), (curses.KEY_F1, 0), (0xb6, 0)])
        self.assertEqual(tui.read_key(win), tui.Key("resize"))
        self.assertEqual(state.decoder.getstate()[0], b"\xc3")
        self.assertEqual(tui.read_key(win), tui.Key("f1"))
        self.assertEqual(tui.read_key(win), tui.Key("char", "ö"))

    def test_locale_codec_is_not_assumed_to_be_utf8(self):
        win, _ = self.window([(0xa4, 0)], encoding="iso8859-15")
        self.assertEqual(tui.read_key(win), tui.Key("char", "€"))

    def test_decoded_pending_characters_are_separate_from_logical_pushback(self):
        win, state = self.window(bytes_of(b"+AKMApQ-"), encoding="utf-7")
        self.assertEqual(tui._next_value(win), "£")
        self.assertEqual(state.pending, ["¥"])
        tui._push_back(win, ["z"])
        self.assertEqual(tui._peek(win), "z")
        self.assertEqual(tui._next_value(win), "¥")
        self.assertEqual(win.calls, len(b"+AKMApQ-"))

    def test_malformed_input_has_only_fixed_error_text(self):
        for data in (b"\xff", b"\xc3x", b"\xed\xa0\x80"):
            with self.subTest(data=data):
                win, _ = self.window(bytes_of(data))
                with self.assertRaises(tui.CursesError) as raised:
                    tui.read_key(win)
                self.assertEqual(str(raised.exception), tui.INPUT_ENCODING_ERROR)
                self.assertIsNone(raised.exception.__cause__)

    def test_incomplete_character_is_not_finalized_at_a_timeout(self):
        win, state = self.window([(0xe2, 0), (IDLE, 0), (IDLE, 0)])
        self.assertIsNone(tui._peek(win))
        self.assertIsNone(tui._peek(win))
        self.assertEqual(state.decoder.getstate()[0], b"\xe2")
        win.events += bytes_of(b"\x82\xac")
        self.assertEqual(tui._next_value(win), "€")

    def test_key_returned_after_native_budget_overrun_is_not_lost(self):
        for value, expected in ((ord("x"), "x"), (curses.KEY_UP, curses.KEY_UP)):
            with self.subTest(value=value):
                win, _ = self.window([(value, 0.6)])
                self.assertEqual(tui._peek(win), expected)
                self.assertLessEqual(win.waits[0], 80)
                self.assertEqual(win.calls, 1)

    def test_alt_unicode_and_function_keys_keep_order(self):
        win, _ = self.window(bytes_of(b"\x1b\xc3\xb6") + [(curses.KEY_DOWN, 0)])
        self.assertEqual([tui.read_key(win) for _ in range(3)],
                         [tui.Key("esc"), tui.Key("char", "ö"), tui.Key("down")])

    def test_partial_alt_tail_can_finish_after_peek_expires(self):
        win, _ = self.window([(27, 0), (0xc3, 0), (IDLE, 0), (0xb6, 0)])
        self.assertEqual(tui.read_key(win), tui.Key("esc"))
        self.assertEqual(tui.read_key(win), tui.Key("char", "ö"))

    def test_five_early_errors_are_paced_and_not_described_as_eof(self):
        win, state = self.window([(-1, 0)] * 5)
        with self.assertRaises(tui.CursesError) as raised:
            tui.read_key(win)
        self.assertEqual(str(raised.exception), tui.INPUT_EARLY_ERROR)
        self.assertEqual(state.early_errors, 5)
        self.assertEqual(win.calls, 5)
        self.assertEqual(len(self.clock.sleeps), 5)
        self.assertAlmostEqual(sum(self.clock.sleeps), 0.5)

    def test_full_wait_resets_early_streak_with_five_ms_tolerance(self):
        win, state = self.window([(-1, 0)] * 4 + [(-1, 0.096), (ord("x"), 0)])
        self.assertEqual(tui._next_value(win), "x")
        self.assertEqual(state.early_errors, 0)
        self.assertAlmostEqual(sum(self.clock.sleeps), 0.404)

    def test_successful_byte_resize_and_special_reset_streak(self):
        for value in (0xc3, curses.KEY_RESIZE, curses.KEY_UP):
            with self.subTest(value=value):
                events = [(-1, 0)] * 4 + [(value, 0)] + [(-1, 0)] * 4 + [(0xb6, 0)]
                win, state = self.window(events)
                if value != 0xc3:
                    self.assertEqual(tui._next_value(win), value)
                    self.assertEqual(state.early_errors, 0)
                    win.events[-1] = (ord("x"), 0)
                self.assertEqual(tui._next_value(win), "ö" if value == 0xc3 else "x")
                self.assertEqual(state.early_errors, 0)

    def test_streak_survives_peeks_and_sub_ten_ms_tails(self):
        win, state = self.window([(-1, 0)] * 6)
        for expected in range(1, 5):
            self.assertIsNone(tui._peek(win))
            self.assertEqual(state.early_errors, expected)
        self.assertIsNone(state.read(win, deadline=self.clock() + 0.009))
        self.assertEqual(state.early_errors, 4)
        with self.assertRaisesRegex(tui.CursesError, "repeatedly returned early errors"):
            tui._peek(win)
        self.assertTrue(all(seconds <= 0.080001 for seconds in self.clock.sleeps))

    def test_poll_failure_flags_and_invalid_registration_fail_closed(self):
        # NVAL stays fatal outside the qualified Darwin tty case, even when
        # independent tty/select checks would succeed.
        with mock.patch.object(tui.sys, "platform", "linux"), \
                mock.patch.object(tui, "_terminal_attrs", return_value=[0]) as attrs, \
                mock.patch.object(tui.select, "select", return_value=([], [], [])) as selector:
            for flags in (select.POLLHUP, select.POLLERR, select.POLLNVAL,
                          select.POLLIN | select.POLLHUP, select.POLLIN | select.POLLNVAL):
                with self.subTest(flags=flags):
                    win, _ = self.window([(IDLE, 0)])
                    self.poll.poll.return_value = [(0, flags)]
                    with self.assertRaisesRegex(tui.CursesError, "terminal input is unavailable"):
                        tui._next_value(win)
            attrs.assert_not_called()
            selector.assert_not_called()
        self.poll.register.side_effect = ValueError("invalid descriptor")
        with self.assertRaisesRegex(tui.CursesError, "terminal input is unavailable"):
            tui.InputState(-1)

    def test_pollin_alone_does_not_mean_decoded_readiness_or_eof(self):
        win, _ = self.window([(0xc3, 0), (IDLE, 0), (0xb6, 0)])
        self.poll.poll.return_value = [(0, select.POLLIN)]
        self.assertEqual(tui._next_value(win), "ö")
        win.events += [(-1, 0)] * 5
        with self.assertRaisesRegex(tui.CursesError, "repeatedly returned early errors"):
            tui._next_value(win)

    def test_poll_oserror_is_fixed_but_interrupt_propagates(self):
        for error in (OSError("fixture descriptor error"), ValueError("fixture descriptor error"),
                      KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                win, _ = self.window([(-1, 0)])
                self.poll.poll.side_effect = error
                fixed = isinstance(error, (OSError, ValueError))
                with self.assertRaises(tui.CursesError if fixed else KeyboardInterrupt) as raised:
                    tui._next_value(win)
                if fixed:
                    self.assertEqual(str(raised.exception), tui.INPUT_UNAVAILABLE)
                    self.assertIsNone(raised.exception.__cause__)
                else:
                    self.assertIs(raised.exception, error)

    def test_darwin_nval_valid_tty_keeps_ordinary_idle_slices(self):
        attrs, selector = self.darwin_nval()
        win, state = self.window([(IDLE, 0)] * 3 + bytes_of(b"x"))
        self.assertEqual(tui._next_value(win), "x")
        self.assertEqual(win.waits, [100] * 4)
        self.assertEqual(state.early_errors, 0)
        self.assertAlmostEqual(self.clock.now, 10.3)
        self.assertEqual(attrs.call_args_list, [mock.call(0)] * 3)
        self.assertEqual(selector.call_args_list, [mock.call([0], [], [], 0)] * 3)
        self.assertEqual(self.poll.poll.call_args_list, [mock.call(0)] * 3)
        self.poll.register.assert_called_once_with(0, select.POLLIN)

    def test_darwin_nval_after_escape_keeps_one_peek_budget(self):
        attrs, selector = self.darwin_nval()
        win, _ = self.window([(27, 0), (IDLE, 0)])
        self.assertEqual(tui.read_key(win), tui.Key("esc"))
        self.assertEqual(win.calls, 2)
        self.assertEqual(win.waits[0], 100)
        self.assertLessEqual(win.waits[1], tui.OSC_GAP_MS)
        self.assertAlmostEqual(self.clock.now, 10.08)
        attrs.assert_called_once_with(0)
        selector.assert_called_once_with([0], [], [], 0)

    def test_darwin_nval_peek_deadline_is_shared_across_partial_utf8(self):
        attrs, selector = self.darwin_nval()
        win, state = self.window([(0xc3, 0.03), (IDLE, 0)])
        self.assertIsNone(tui._peek(win))
        self.assertAlmostEqual(self.clock.now, 10.08)
        self.assertEqual(win.calls, 2)
        self.assertLessEqual(win.waits[1], 50)
        self.assertEqual(state.decoder.getstate()[0], b"\xc3")
        win.events += bytes_of(b"\xb6")
        self.assertEqual(tui._next_value(win), "ö")
        attrs.assert_called_once_with(0)
        selector.assert_called_once_with([0], [], [], 0)

    def test_darwin_nval_revalidates_instead_of_disabling_poll(self):
        attrs, selector = self.darwin_nval()
        attrs.side_effect = [attrs.return_value, None]
        win, _ = self.window([(IDLE, 0)] * 2 + bytes_of(b"x"))
        with self.assertRaises(tui.CursesError) as raised:
            tui._next_value(win)
        self.assertEqual(str(raised.exception), tui.INPUT_UNAVAILABLE)
        self.assertEqual(win.calls, 2)
        self.assertEqual(attrs.call_args_list, [mock.call(0)] * 2)
        self.assertEqual(self.poll.poll.call_args_list, [mock.call(0)] * 2)
        selector.assert_called_once_with([0], [], [], 0)

    def test_darwin_nval_closed_nonterminal_and_unavailable_attrs_fail_closed(self):
        self.poll.poll.return_value = [(0, select.POLLNVAL)]
        with mock.patch.object(tui.sys, "platform", "darwin"), \
                mock.patch.object(tui.select, "select") as selector:
            for mode, tty_result, attr_error in (
                    ("closed", OSError(9, "fixture closed descriptor"), None),
                    ("nonterminal", False, None),
                    ("unavailable", True, termios.error(5, "fixture attributes unavailable")),
                    ("invalid", True, ValueError("fixture attributes unavailable"))):
                with self.subTest(mode=mode), \
                        mock.patch.object(tui.os, "isatty") as isatty, \
                        mock.patch.object(tui.termios, "tcgetattr", side_effect=attr_error) as getattrs:
                    if isinstance(tty_result, OSError):
                        isatty.side_effect = tty_result
                    else:
                        isatty.return_value = tty_result
                    win, _ = self.window([(IDLE, 0)])
                    with self.assertRaises(tui.CursesError) as raised:
                        tui._peek(win)
                    self.assertEqual(str(raised.exception), tui.INPUT_UNAVAILABLE)
                    self.assertIsNone(raised.exception.__cause__)
                    self.assertEqual(win.calls, 1)
                    isatty.assert_called_once_with(0)
                    if tty_result is True:
                        getattrs.assert_called_once_with(0)
                    else:
                        getattrs.assert_not_called()
            with mock.patch.object(tui, "termios", None):
                win, _ = self.window([(IDLE, 0)])
                with self.assertRaisesRegex(tui.CursesError, "terminal input is unavailable"):
                    tui._peek(win)
            selector.assert_not_called()
        self.assertEqual(self.clock.sleeps, [])

    def test_darwin_nval_select_errors_have_only_fixed_text(self):
        self.darwin_nval()
        for error in (OSError(9, "fixture selector failure"), ValueError("fixture selector failure")):
            with self.subTest(error=type(error).__name__), \
                    mock.patch.object(tui.select, "select", side_effect=error) as selector:
                win, _ = self.window([(IDLE, 0)])
                with self.assertRaises(tui.CursesError) as raised:
                    tui._next_value(win)
                self.assertEqual(str(raised.exception), tui.INPUT_UNAVAILABLE)
                self.assertIsNone(raised.exception.__cause__)
                self.assertEqual(win.calls, 1)
                selector.assert_called_once_with([0], [], [], 0)
        self.assertEqual(self.clock.sleeps, [])

    def test_darwin_hup_and_err_stay_fatal_even_combined_with_nval(self):
        attrs, selector = self.darwin_nval()
        masks = [bad | extra for bad in (select.POLLHUP, select.POLLERR)
                 for extra in (0, select.POLLIN, select.POLLNVAL, select.POLLIN | select.POLLNVAL)]
        masks.append(select.POLLIN | select.POLLHUP | select.POLLERR | select.POLLNVAL)
        events = [[(0, flags)] for flags in masks]
        events += [[(0, select.POLLNVAL), (0, bad)] for bad in (select.POLLHUP, select.POLLERR)]
        for observed in events:
            with self.subTest(events=observed):
                self.poll.poll.return_value = observed
                win, _ = self.window([(IDLE, 0)])
                with self.assertRaises(tui.CursesError) as raised:
                    tui._peek(win)
                self.assertEqual(str(raised.exception), tui.INPUT_UNAVAILABLE)
                self.assertEqual(win.calls, 1)
        attrs.assert_not_called()
        selector.assert_not_called()
        self.assertEqual(self.clock.sleeps, [])

    def test_darwin_nval_select_readiness_fabricates_neither_key_nor_eof(self):
        _, selector = self.darwin_nval()
        self.poll.poll.return_value = [(0, select.POLLIN | select.POLLNVAL)]
        selector.return_value = ([0], [], [])
        win, state = self.window([(IDLE, 0)])
        self.assertIsNone(tui._peek(win))
        self.assertEqual((state.pending, state.pushback, state.early_errors), ([], [], 0))
        win.events += [(-1, 0)] * 5
        with self.assertRaises(tui.CursesError) as raised:
            tui._next_value(win)
        self.assertEqual(str(raised.exception), tui.INPUT_EARLY_ERROR)
        self.assertEqual(win.calls, 6)
        self.assertEqual(selector.call_args_list, [mock.call([0], [], [], 0)] * 6)
        self.assertAlmostEqual(sum(self.clock.sleeps), 0.5)

    def test_darwin_nval_fifth_early_error_survives_peeks_and_short_tails(self):
        attrs, selector = self.darwin_nval()
        win, state = self.window([(-1, 0)] * 6)
        for expected in range(1, 5):
            self.assertIsNone(tui._peek(win))
            self.assertEqual(state.early_errors, expected)
        self.assertIsNone(state.read(win, deadline=self.clock() + 0.009))
        self.assertEqual(state.early_errors, 4)
        with self.assertRaises(tui.CursesError) as raised:
            tui._peek(win)
        self.assertEqual(str(raised.exception), tui.INPUT_EARLY_ERROR)
        self.assertEqual(state.early_errors, 5)
        self.assertEqual(win.calls, 6)
        self.assertEqual(attrs.call_count, 6)
        self.assertEqual(selector.call_args_list, [mock.call([0], [], [], 0)] * 6)
        self.assertTrue(all(seconds <= 0.080001 for seconds in self.clock.sleeps))

    def test_darwin_nval_full_native_wait_still_resets_the_streak(self):
        self.darwin_nval()
        win, state = self.window([(-1, 0)] * 4 + [(-1, 0.096)] + [(-1, 0)] * 4 + bytes_of(b"x"))
        self.assertEqual(tui._next_value(win), "x")
        self.assertEqual(state.early_errors, 0)
        self.assertAlmostEqual(sum(self.clock.sleeps), 0.804)

    def test_darwin_nval_fallback_time_cannot_mask_native_early_errors(self):
        _, selector = self.darwin_nval()
        def checked(*_args):
            self.clock.now += 0.1
            return [], [], []
        selector.side_effect = checked
        win, state = self.window([(-1, 0)] * 5)
        with self.assertRaises(tui.CursesError) as raised:
            tui._next_value(win)
        self.assertEqual(str(raised.exception), tui.INPUT_EARLY_ERROR)
        self.assertEqual(state.early_errors, 5)
        self.assertEqual(win.calls, 5)

    def test_darwin_nval_validation_interrupts_propagate_unchanged(self):
        attrs, selector = self.darwin_nval()
        for stage in (attrs, selector):
            with self.subTest(stage="attrs" if stage is attrs else "select"):
                error = KeyboardInterrupt()
                attrs.side_effect = error if stage is attrs else None
                selector.side_effect = error if stage is selector else None
                win, _ = self.window([(IDLE, 0)])
                with self.assertRaises(KeyboardInterrupt) as raised:
                    tui._peek(win)
                self.assertIs(raised.exception, error)
                self.assertEqual(win.calls, 1)
        self.assertEqual(self.clock.sleeps, [])

    def test_darwin_nval_peek_preserves_partial_utf8_and_pushback_order(self):
        attrs, selector = self.darwin_nval()
        win, state = self.window(bytes_of(b"\x1b\xc3") + [(IDLE, 0)])
        self.assertEqual(tui.read_key(win), tui.Key("esc"))
        self.assertEqual(state.decoder.getstate()[0], b"\xc3")
        tui._push_back(win, ["a", "b"])
        tui._push_back(win, ["z"])
        self.assertEqual([tui._peek(win), tui._next_value(win), tui._next_value(win)], ["z", "a", "b"])
        self.assertEqual(win.calls, 3)
        win.events += bytes_of(b"\xb6")
        self.assertEqual(tui.read_key(win), tui.Key("char", "ö"))
        self.assertEqual(state.decoder.getstate()[0], b"")
        attrs.assert_called_once_with(0)
        selector.assert_called_once_with([0], [], [], 0)

    def test_native_read_and_timeout_errors_are_not_idle(self):
        for error in (tui.CursesError("fixture read failure"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                win, _ = self.window([(error, 0)])
                with self.assertRaises(type(error)) as raised:
                    tui._peek(win)
                self.assertIs(raised.exception, error)
                self.poll.poll.assert_not_called()
        win, _ = self.window()
        error = tui.CursesError("fixture timeout failure")
        with mock.patch.object(win, "timeout", side_effect=error), self.assertRaises(tui.CursesError) as raised:
            tui._next_value(win)
        self.assertIs(raised.exception, error)
        self.assertEqual(win.calls, 0)

    def test_interrupt_during_backoff_propagates_unchanged(self):
        win, _ = self.window([(-1, 0)])
        error = KeyboardInterrupt()
        with mock.patch.object(tui.time, "sleep", side_effect=error), self.assertRaises(KeyboardInterrupt) as raised:
            tui._next_value(win)
        self.assertIs(raised.exception, error)
        self.assertEqual(win.calls, 1)

    def test_idle_does_not_extend_osc_carry_age(self):
        win, _ = self.window([(IDLE, 0)] * 21 + bytes_of(b"a"))
        tui._carry_reply("]11;rgb:0/0/", since=self.clock())
        self.assertEqual(tui.read_key(win), tui.Key("char", "a"))
        self.assertEqual(tui._REPLY_TAIL, [])

    def test_osc_peek_idle_does_not_become_reply_progress(self):
        win, _ = self.window(bytes_of(b"\x1b]11;rgb:0") + [(IDLE, 0)])
        self.assertEqual(tui._next_value(win), "\x1b")
        self.assertTrue(tui._swallow_osc_reply(win))
        self.assertEqual(tui._REPLY_SINCE, [10.0])
        win.events += bytes_of(b"/0") + [(IDLE, 0)]
        progressed = self.clock()
        tui._finish_carried_reply(win)
        self.assertEqual(tui._REPLY_SINCE, [progressed])
        self.assertGreater(self.clock(), progressed)

    def test_context_and_window_association_is_required(self):
        win, state = self.window(bytes_of(b"x"))
        with self.assertRaises(tui.CursesError):
            tui.read_key(ByteWindow(self.clock))
        state.suspended = True
        with self.assertRaises(tui.CursesError):
            tui.read_key(win)
        tui._INPUT_STATE = None
        with self.assertRaises(tui.CursesError):
            tui.read_key(win)
        self.assertEqual(win.calls, 0)

    def test_script_exhaustion_fails_even_in_peek(self):
        win, _ = self.window()
        with self.assertRaisesRegex(AssertionError, "script exhausted"):
            tui._peek(win)
        tui._INPUT_STATE = None
        with self.assertRaisesRegex(AssertionError, "script exhausted"):
            tui._peek(FakeWindow(raw=True))

    def test_decoder_reset_interrupt_still_clears_owned_fields(self):
        win, state = self.window()
        state.pending.append("x")
        state.pushback.append("y")
        state.early_errors = 4
        state.suspended = True
        error = KeyboardInterrupt()
        with mock.patch.object(state.decoder, "reset", side_effect=error), \
                self.assertRaises(KeyboardInterrupt) as raised:
            state.clear()
        self.assertIs(raised.exception, error)
        self.assertEqual((state.pending, state.pushback, state.early_errors), ([], [], 0))
        self.assertIsNone(state.window)
        self.assertFalse(state.suspended)

    def test_idle_never_redraws_a_widget(self):
        tui._INPUT_STATE = None
        quiet = FakeWindow([10], raw=True)
        idle = FakeWindow([IDLE] * 20 + [10], raw=True)
        for win in (quiet, idle):
            self.assertTrue(tui.Modal("Pick", [], buttons=(("OK", True),)).run(win, tui.MONO_PALETTE))
        self.assertEqual(idle.frames, quiet.frames)


class HarnessInputRegistrationTests(unittest.TestCase):
    def test_ended_context_window_cannot_use_standalone_registration(self):
        standalone_lookup = tui._input_state
        harness = _SessionHarness(self, iflag=0, environ={})
        win = harness.run(lambda win: win)
        self.assertIsNone(tui._INPUT_STATE)
        for lookup in (tui._input_state, standalone_lookup):
            with self.subTest(lookup=lookup), mock.patch.object(tui, "_input_state", lookup):
                win.keys[:] = ["x"]
                with self.assertRaises(tui.CursesError):
                    tui._next_value(win)
                self.assertEqual(win.keys, ["x"])

    def test_active_context_registration_loss_cannot_use_fake_state(self):
        standalone_lookup = tui._input_state
        harness = _SessionHarness(self, iflag=0, environ={})
        def app(win):
            for lookup in (tui._input_state, standalone_lookup):
                with self.subTest(lookup=lookup), mock.patch.object(tui, "_input_state", lookup):
                    win.keys[:] = ["x"]
                    with mock.patch.object(tui, "_INPUT_STATE", None):
                        with self.assertRaises(tui.CursesError):
                            tui._next_value(win)
                        self.assertEqual(win.keys, ["x"])
        harness.run(app)

    def test_both_import_names_share_the_strict_harness_lookup(self):
        import importlib

        saved_lookup = tui._input_state
        self.addCleanup(setattr, tui, "_input_state", saved_lookup)
        modules = [importlib.import_module(name) for name in ("test_tui", "tests.test_tui")]
        self.assertIs(modules[0]._real_input_state, modules[1]._real_input_state)
        with mock.patch.object(tui, "_INPUT_STATE", None):
            for module in modules:
                self.assertEqual(tui._next_value(module.FakeWindow(["x"])), "x")
        for module in modules:
            harness = module._SessionHarness(self, iflag=0, environ={})
            self.assertIs(tui._input_state, modules[0]._real_input_state)
            win = harness.run(lambda win: win)
            win.keys.append("x")
            with self.assertRaises(tui.CursesError):
                tui._next_value(win)
            self.assertEqual(win.keys, ["x"])


class AdmissionTests(unittest.TestCase):
    def harness(self):
        return _SessionHarness(self, iflag=0, environ={})

    def test_bad_cs7_or_istrip_refuses_before_wrapper_and_app(self):
        harness = self.harness()
        for cflag, iflag in ((harness.termios.CS7, 0), (harness.termios.CS8, harness.termios.ISTRIP)):
            harness.termios.cflag, harness.termios.iflag = cflag, iflag
            output = _FdStream(1)
            with self.assertRaisesRegex(tui.CursesError, "verified CS8"):
                tui.run_curses_on_streams(lambda win: self.fail("app entered"), _FdStream(0), output)
            self.assertEqual(output.getvalue(), tui.INPUT_MODE_NOTICE + "\n")
            self.assertEqual((harness.termios.cflag, harness.termios.iflag), (cflag, iflag))
            self.assertNotIn(("wrapper",), harness.events)
            self.assertEqual(tui._SCREEN_PROVENANCE, "new")

    def test_unreadable_attributes_and_unowned_screen_refuse(self):
        harness = self.harness()
        with mock.patch.object(harness.termios, "tcgetattr", side_effect=harness.termios.error):
            with self.assertRaises(tui.CursesError):
                harness.run(lambda win: self.fail("app entered"))
        harness.initialized = True
        with self.assertRaises(tui.CursesError):
            harness.run(lambda win: self.fail("unowned SCREEN admitted"))
        self.assertNotIn(("wrapper",), harness.events)

    def test_first_entry_and_ended_owned_screen_reentry(self):
        harness = self.harness()
        states = []
        def app(win):
            states.append(tui._input_state(win))
            self.assertEqual(tui._SCREEN_PROVENANCE, "owned")
            return "ok"
        self.assertEqual(harness.run(app), "ok")
        self.assertEqual(harness.run(app), "ok")
        self.assertIsNot(states[0], states[1])
        self.assertTrue(all(state.window is None for state in states))
        self.assertIsNone(tui._INPUT_STATE)

    def test_failed_or_interrupted_initialization_never_blesses_later_screen(self):
        harness = self.harness()
        for error in (tui.CursesError("fixture init failure"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__), mock.patch.object(tui, "_SCREEN_PROVENANCE", "new"):
                harness.initialized = False
                tui._carry_reply("]11;rgb:0")
                with mock.patch.object(tui.curses, "wrapper", side_effect=error), self.assertRaises(type(error)):
                    harness.run(lambda win: self.fail("app entered"))
                self.assertEqual(tui._SCREEN_PROVENANCE, "uncertain")
                self.assertIsNone(tui._INPUT_STATE)
                self.assertEqual(tui._REPLY_TAIL, [])
                # Both no public initialization and an externally initialized
                # SCREEN are refused after the incomplete product attempt.
                for initialized in (False, True):
                    harness.initialized = initialized
                    with self.assertRaises(tui.CursesError):
                        harness.run(lambda win: self.fail("uncertain SCREEN admitted"))

    def test_all_known_admission_refusals_skip_automatic_palette_query(self):
        harness = self.harness()
        with mock.patch.object(tui, "query_background_osc11", return_value="light") as query:
            for mode in ("cs7", "strip", "unowned", "uncertain"):
                with self.subTest(mode=mode):
                    harness.termios.cflag = harness.termios.CS7 if mode == "cs7" else harness.termios.CS8
                    harness.termios.iflag = harness.termios.ISTRIP if mode == "strip" else 0
                    harness.initialized = mode == "unowned"
                    tui._SCREEN_PROVENANCE = "uncertain" if mode == "uncertain" else "new"
                    tui._carry_reply("]11;rgb:0")
                    palette = tui.detect_palette({"TERM": "xterm"}, tty_in=_FdStream(7), tty_out=_FdStream(8))
                    self.assertEqual(palette, tui.DARK_PALETTE)
                    query.assert_not_called()
                    self.assertEqual(tui._REPLY_TAIL, ["]11;rgb:0"])
            harness.initialized = False
            tui._SCREEN_PROVENANCE = "new"
            self.assertEqual(tui.detect_palette({"TERM": "xterm"}, tty_in=_FdStream(7), tty_out=_FdStream(8)),
                             tui.LIGHT_PALETTE)
            query.assert_called_once_with(7, 8)
        tui._carry_reply(None)

    def test_entry_keeps_carry_exit_clears_only_python_owned_input(self):
        harness = self.harness()
        states = []
        def app(win):
            self.assertEqual(tui._REPLY_TAIL, ["]11;rgb:0"])
            state = tui._input_state(win)
            states.append(state)
            state.decoder.decode(b"\xc3")
            state.pending.append("x")
            state.pushback.append("y")
            state.early_errors = 4
            raise KeyboardInterrupt
        tui._carry_reply("]11;rgb:0")
        with mock.patch.object(tui.locale, "getencoding", return_value="utf-8"), \
                mock.patch.object(tui.curses, "flushinp", side_effect=AssertionError("flush")), \
                mock.patch.object(tui.curses, "ungetch", side_effect=AssertionError("ungetch")), \
                self.assertRaises(KeyboardInterrupt):
            harness.run(app)
        state, = states
        self.assertEqual((state.decoder.getstate()[0], state.pending, state.pushback, state.early_errors),
                         (b"", [], [], 0))
        self.assertEqual((tui._REPLY_TAIL, tui._REPLY_SINCE), ([], []))
        self.assertIsNone(tui._INPUT_STATE)

    def _cleanup_fault(self, stage, error):
        harness = self.harness()
        attrs = {0: [harness.termios.IXON, 11, harness.termios.CS8, 13, 14, 15, [b"a", b"b"]],
                 1: [0, 21, harness.termios.CS8, 23, 24, 25, [b"c", b"d"]]}
        before = copy.deepcopy(attrs)
        states = []
        original_carry = tui._carry_reply
        self.addCleanup(original_carry, None)

        def get(fd):
            harness.events.append(("snapshot", fd))
            return copy.deepcopy(attrs[fd])

        def put(fd, when, value):
            harness.events.append(("restore-attrs", fd))
            attrs[fd][:] = copy.deepcopy(value)

        def carry(text, *, since=None):
            harness.events.append(("carry", text))
            if text is None and stage == "carry":
                raise error
            original_carry(text, since=since)

        def app(win):
            state = tui._input_state(win)
            states.append(state)
            state.decoder.decode(b"\xc3")
            decoder = state.decoder
            state.decoder = mock.Mock(wraps=decoder)
            def reset():
                harness.events.append(("decoder-reset",))
                if stage == "decoder":
                    raise error
                decoder.reset()
            state.decoder.reset.side_effect = reset
            state.pending.append("x")
            state.pushback.append("y")
            state.early_errors = 4
            state.suspended = True
            original_carry("]11;rgb:0", since=10.0)
            attrs[0][1] = 99
            attrs[1][6][0] = b"z"

        with mock.patch.object(tui.locale, "getencoding", return_value="utf-8"), \
                mock.patch.object(harness.termios, "tcgetattr", get), \
                mock.patch.object(harness.termios, "tcsetattr", put):
            with mock.patch.object(tui, "_carry_reply", carry), self.assertRaises(type(error)) as raised:
                harness.run(app, in_fd=7, out_fd=8)
            self.assertIs(raised.exception, error)
            self.assertEqual(attrs, before)
            state, = states
            self.assertEqual((state.pending, state.pushback, state.early_errors), ([], [], 0))
            self.assertIsNone(state.window)
            self.assertFalse(state.suspended)
            self.assertIsNone(tui._INPUT_STATE)
            self.assertEqual((tui._REPLY_TAIL, tui._REPLY_SINCE), ([], []))
            events = harness.events
            restores = [max(i for i, event in enumerate(events) if event == ("restore-attrs", fd))
                        for fd in (0, 1)]
            self.assertLess(events.index(("teardown",)), events.index(("decoder-reset",)))
            self.assertLess(events.index(("decoder-reset",)), events.index(("carry", None)))
            self.assertLess(events.index(("carry", None)), restores[0])
            self.assertLess(*restores)
            previous = restores[-1]
            for fd in (0, 1):
                duplicate = next(event[2] for event in events if event[:2] == ("dup", fd))
                restored = events.index(("dup2", duplicate, fd))
                closed = events.index(("close", duplicate))
                self.assertLess(previous, restored)
                self.assertLess(restored, closed)
                previous = closed

            def next_app(win):
                fresh = tui._input_state(win)
                self.assertIsNot(fresh, state)
                self.assertEqual((fresh.pending, fresh.pushback, fresh.early_errors), ([], [], 0))
                self.assertEqual(fresh.decoder.getstate()[0], b"")
                self.assertEqual((tui._REPLY_TAIL, tui._REPLY_SINCE), ([], []))
                win.keys.append("x")
                return tui._next_value(win)
            self.assertEqual(harness.run(next_app, in_fd=7, out_fd=8), "x")
            self.assertEqual(attrs, before)
            self.assertIsNone(tui._INPUT_STATE)

    def test_decoder_cleanup_interrupt_restores_tty_fds_and_allows_reentry(self):
        self._cleanup_fault("decoder", KeyboardInterrupt())

    def test_carry_cleanup_error_restores_tty_fds_and_allows_reentry(self):
        self._cleanup_fault("carry", RuntimeError("fixture carry cleanup failure"))

    def test_carry_cleanup_interrupt_restores_tty_fds_and_allows_reentry(self):
        self._cleanup_fault("carry", KeyboardInterrupt())

    def test_program_mode_is_checked_before_app(self):
        harness = self.harness()
        original = tui.curses.wrapper
        def wrapper(func):
            harness.termios.iflag |= harness.termios.ISTRIP
            return original(func)
        with mock.patch.object(tui.curses, "wrapper", wrapper), self.assertRaises(tui.CursesError):
            harness.run(lambda win: self.fail("bad program mode reached app"))
        self.assertEqual(harness.termios.iflag, 0)
        self.assertIn(("teardown",), harness.events)

    def test_suspension_retains_decoder_and_rechecks_restoration(self):
        harness = self.harness()
        def app(win):
            state = tui._input_state(win)
            state.decoder.decode(b"\xc3")
            state.pushback.append("x")
            state.early_errors = 3
            with tui.suspended_curses(win):
                self.assertTrue(state.suspended)
                with self.assertRaises(tui.CursesError):
                    tui._next_value(win)
            self.assertFalse(state.suspended)
            self.assertEqual(state.decoder.decode(b"\xb6"), "ö")
            self.assertEqual(tui._next_value(win), "x")
            self.assertEqual(state.early_errors, 3)
            with self.assertRaises(tui.CursesError):
                with tui.suspended_curses(win):
                    harness.termios.iflag |= harness.termios.ISTRIP
            self.assertTrue(state.suspended)
            with self.assertRaises(tui.CursesError):
                tui._next_value(win)
        with mock.patch.object(tui.locale, "getencoding", return_value="utf-8"), \
                mock.patch.object(tui.curses, "endwin"), mock.patch.object(tui.curses, "reset_prog_mode"):
            harness.run(app)
        self.assertEqual(harness.termios.iflag, 0)

    def test_teardown_then_exact_attrs_then_descriptor_restore_on_interrupt(self):
        harness = self.harness()
        attrs = {0: [harness.termios.IXON, 11, harness.termios.CS8, 13, 14, 15, [b"a", b"b"]],
                 1: [0, 21, harness.termios.CS8, 23, 24, 25, [b"c", b"d"]]}
        before = copy.deepcopy(attrs)
        def get(fd):
            harness.events.append(("snapshot", fd))
            return copy.deepcopy(attrs[fd])
        def put(fd, when, value):
            harness.events.append(("restore-attrs", fd))
            attrs[fd][:] = copy.deepcopy(value)
        def app(win):
            attrs[0][1] = 99
            attrs[1][6][0] = b"z"
            raise KeyboardInterrupt
        with mock.patch.object(harness.termios, "tcgetattr", get), \
                mock.patch.object(harness.termios, "tcsetattr", put), self.assertRaises(KeyboardInterrupt):
            harness.run(app, in_fd=7, out_fd=8)
        self.assertEqual(attrs, before)
        events = harness.events
        self.assertLess(events.index(("dup2", 7, 0)), events.index(("snapshot", 0)))
        self.assertLess(events.index(("dup2", 8, 1)), events.index(("snapshot", 1)))
        restores = [max(i for i, event in enumerate(events) if event == ("restore-attrs", fd))
                    for fd in (0, 1)]
        self.assertLess(events.index(("teardown",)), restores[0])
        self.assertLess(*restores)
        for fd in (0, 1):
            duplicate = next(event[2] for event in events if event[:2] == ("dup", fd))
            self.assertLess(restores[-1], events.index(("dup2", duplicate, fd)))


class DualTerminalTests(unittest.TestCase):
    """Independent attributes for actual fds 0/1 and supplied streams 7/8."""

    def setUp(self):
        self.harness = _SessionHarness(self, iflag=0, environ={})
        term = self.harness.termios
        self.attrs = {0: [term.IXON, 11, term.CS8, 13, 14, 15, [b"a", b"b"]],
                      1: [0, 21, term.CS8, 23, 24, 25, [b"c", b"d"]]}
        self.before = copy.deepcopy(self.attrs)
        self.unavailable = set()
        self.non_ttys = set()
        self.restore_errors = {}
        self.fd_owner = {0: 0, 1: 1, 7: 0, 8: 1}
        self.events = self.harness.events

        def get(fd):
            self.events.append(("snapshot", fd))
            owner = self.fd_owner[fd]
            if owner in self.unavailable:
                raise term.error("fixture attributes unavailable")
            return copy.deepcopy(self.attrs[owner])

        def put(fd, when, value):
            self.events.append(("restore-attrs", fd))
            if ("teardown",) in self.events and fd in self.restore_errors:
                raise self.restore_errors[fd]
            self.attrs[self.fd_owner[fd]][:] = copy.deepcopy(value)

        def is_tty(fd):
            self.events.append(("isatty", fd))
            return self.fd_owner[fd] not in self.non_ttys

        for patcher in (mock.patch.object(term, "tcgetattr", get),
                        mock.patch.object(term, "tcsetattr", put),
                        mock.patch.object(tui.os, "isatty", is_tty)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(tui._carry_reply, None)

    def bad_output(self, mode):
        self.attrs = copy.deepcopy(self.before)
        self.unavailable.clear()
        self.non_ttys.clear()
        if mode == "strip":
            self.attrs[1][0] |= self.harness.termios.ISTRIP
        elif mode == "cs7":
            self.attrs[1][2] = self.harness.termios.CS7
        elif mode == "unavailable":
            self.unavailable.add(1)
        elif mode == "non-tty":
            self.non_ttys.add(1)

    def assert_restoration_order(self):
        teardown = self.events.index(("teardown",))
        restores = [(i, event[1]) for i, event in enumerate(self.events)
                    if i > teardown and event[0] == "restore-attrs"]
        self.assertEqual([fd for _, fd in restores], [0, 1])
        previous = restores[-1][0]
        for fd in (0, 1):
            duplicate = next(event[2] for event in self.events if event[:2] == ("dup", fd))
            restored = self.events.index(("dup2", duplicate, fd))
            closed = self.events.index(("close", duplicate))
            self.assertLess(previous, restored)
            self.assertLess(restored, closed)
            previous = closed

    def test_good_input_bad_or_unavailable_output_refuses_before_wrapper(self):
        for mode in ("strip", "cs7", "unavailable", "non-tty"):
            with self.subTest(mode=mode):
                self.bad_output(mode)
                before = copy.deepcopy(self.attrs)
                self.events.clear()
                output = _FdStream(8)
                with self.assertRaises(tui.CursesError):
                    tui.run_curses_on_streams(lambda win: self.fail("app entered"), _FdStream(7), output)
                self.assertEqual(output.getvalue(), tui.INPUT_MODE_NOTICE + "\n")
                self.assertEqual(self.attrs, before)
                self.assertNotIn(("wrapper",), self.events)
                self.assertEqual(tui._SCREEN_PROVENANCE, "new")
                self.assertIn(("isatty", 1), self.events)
                self.assertNotIn(("isatty", 2), self.events)
                for fd in (0, 1):
                    duplicate = next(event[2] for event in self.events if event[:2] == ("dup", fd))
                    self.assertIn(("dup2", duplicate, fd), self.events)
                    self.assertIn(("close", duplicate), self.events)

    def test_palette_checks_both_supplied_terminals_and_keeps_carry(self):
        with mock.patch.object(tui, "query_background_osc11", return_value="light") as query:
            for mode in ("strip", "cs7", "unavailable", "non-tty"):
                with self.subTest(mode=mode):
                    self.bad_output(mode)
                    self.events.clear()
                    tui._carry_reply("]11;rgb:0")
                    self.assertEqual(tui.detect_palette({"TERM": "xterm"}, tty_in=_FdStream(7),
                                                        tty_out=_FdStream(8)), tui.DARK_PALETTE)
                    query.assert_not_called()
                    self.assertEqual(tui._REPLY_TAIL, ["]11;rgb:0"])
                    self.assertIn(("isatty", 7), self.events)
                    self.assertIn(("isatty", 8), self.events)
                    self.assertNotIn(("snapshot", 0), self.events)
                    self.assertNotIn(("snapshot", 1), self.events)
            self.bad_output("good")
            self.assertEqual(tui.detect_palette({"TERM": "xterm"}, tty_in=_FdStream(7),
                                                tty_out=_FdStream(8)), tui.LIGHT_PALETTE)
            query.assert_called_once_with(7, 8)

    def test_owned_screen_reentry_still_checks_output_terminal(self):
        self.assertEqual(self.harness.run(lambda win: "ok", in_fd=7, out_fd=8), "ok")
        for mode in ("strip", "cs7", "unavailable", "non-tty"):
            with self.subTest(mode=mode):
                self.bad_output(mode)
                self.events.clear()
                with self.assertRaises(tui.CursesError):
                    self.harness.run(lambda win: self.fail("app entered"), in_fd=7, out_fd=8)
                self.assertNotIn(("wrapper",), self.events)
                self.assertEqual(tui._SCREEN_PROVENANCE, "owned")

    def test_different_good_terminals_are_snapshotted_and_restored(self):
        def app(win):
            self.assertFalse(self.attrs[0][0] & self.harness.termios.IXON)
            self.assertEqual(self.attrs[1], self.before[1])
            self.attrs[0][1] = 99
            self.attrs[1][6][0] = b"z"
            return "ok"
        self.assertEqual(self.harness.run(app, in_fd=7, out_fd=8), "ok")
        self.assertEqual(self.attrs, self.before)
        self.assertLess(self.events.index(("dup2", 7, 0)), self.events.index(("snapshot", 0)))
        self.assertLess(self.events.index(("dup2", 8, 1)), self.events.index(("snapshot", 1)))
        self.assert_restoration_order()

    def test_initialization_failure_restores_both_tty_snapshots(self):
        error = tui.CursesError("fixture initialization failure")
        def wrapper(func):
            self.attrs[0][1] = 99
            self.attrs[1][6][0] = b"z"
            raise error
        with mock.patch.object(tui.curses, "wrapper", wrapper), self.assertRaises(tui.CursesError) as raised:
            self.harness.run(lambda win: self.fail("app entered"), in_fd=7, out_fd=8)
        self.assertIs(raised.exception, error)
        self.assertEqual(self.attrs, self.before)
        self.assertEqual([event for event in self.events if event[0] == "restore-attrs"],
                         [("restore-attrs", 0), ("restore-attrs", 1)])
        self.assertIsNone(tui._INPUT_STATE)

    def test_program_mode_checks_output_before_app(self):
        original = tui.curses.wrapper
        def wrapper(func):
            self.attrs[1][0] |= self.harness.termios.ISTRIP
            return original(func)
        with mock.patch.object(tui.curses, "wrapper", wrapper), self.assertRaises(tui.CursesError):
            self.harness.run(lambda win: self.fail("app entered"), in_fd=7, out_fd=8)
        self.assertEqual(self.attrs, self.before)
        self.assert_restoration_order()

    def test_suspension_checks_output_before_any_further_read(self):
        def app(win):
            state = tui._input_state(win)
            with self.assertRaises(tui.CursesError):
                with tui.suspended_curses(win):
                    self.attrs[1][0] |= self.harness.termios.ISTRIP
            self.assertTrue(state.suspended)
            with self.assertRaises(tui.CursesError):
                tui._next_value(win)
        with mock.patch.object(tui.curses, "endwin"), mock.patch.object(tui.curses, "reset_prog_mode"):
            self.harness.run(app, in_fd=7, out_fd=8)
        self.assertEqual(self.attrs, self.before)
        self.assert_restoration_order()

    def test_both_tty_restores_are_attempted_when_either_raises(self):
        for fd in (0, 1):
            for error in (KeyboardInterrupt(), self.harness.termios.error("fixture restore failure")):
                with self.subTest(fd=fd, error=type(error).__name__):
                    self.bad_output("good")
                    self.events.clear()
                    self.restore_errors = {fd: error}
                    def app(win):
                        self.attrs[0][1] = 99
                        self.attrs[1][1] = 98
                    expected = KeyboardInterrupt if isinstance(error, KeyboardInterrupt) else tui.CursesError
                    with self.assertRaises(expected) as raised:
                        self.harness.run(app, in_fd=7, out_fd=8)
                    if isinstance(error, KeyboardInterrupt):
                        self.assertIs(raised.exception, error)
                    else:
                        self.assertEqual(str(raised.exception), "terminal attributes could not be restored")
                        self.assertIsNone(raised.exception.__cause__)
                    self.assertEqual(self.attrs[1 - fd], self.before[1 - fd])
                    self.assertNotEqual(self.attrs[fd], self.before[fd])
                    self.assert_restoration_order()
                    self.assertIsNone(tui._INPUT_STATE)

    def _launch_card_restore_failure(self, app_error=None):
        from charmer.cli.screens import launch_sessions

        runtime = SimpleNamespace(environ={})
        target = launch_sessions.cli_types.LaunchTarget(
            kind="profile", document={}, profile="fixture", follow=True, source="Profile fixture")
        self.restore_errors = {fd: termios.error(5, "fixture restore failure") for fd in (0, 1)}

        def run(win):
            self.attrs[0][1] = 99
            self.attrs[1][1] = 98
            if app_error is not None:
                raise app_error
            return None

        with mock.patch.object(self.harness.termios, "error", termios.error), \
                mock.patch.object(launch_sessions, "_LaunchCardScreen") as screen_type, \
                mock.patch.object(tui, "detect_palette", return_value=tui.MONO_PALETTE):
            self.assertIs(self.harness.termios.error, termios.error)
            screen_type.return_value.run.side_effect = run
            caught = None
            try:
                launch_sessions.launch_card(
                    runtime, target, action="fresh", passthrough=(),
                    input_stream=_FdStream(7), output_stream=_FdStream(8), no_color=True)
            except BaseException as actual:
                caught = actual
            screen_type.return_value.run.assert_called_once()
        self.assert_restoration_order()
        self.assertIsNone(tui._INPUT_STATE)
        return caught

    def test_launch_card_reports_genuine_termios_restore_error_as_cli_error(self):
        caught = self._launch_card_restore_failure()
        self.assertIs(type(caught), cli_errors.CLIError)
        self.assertEqual(str(caught), "the launch card stopped: terminal attributes could not be restored")
        self.assertIs(type(caught.__cause__), tui.CursesError)
        self.assertEqual(str(caught.__cause__), "terminal attributes could not be restored")
        self.assertIsNone(caught.__cause__.__cause__)
        self.assertTrue(caught.__cause__.__suppress_context__)

    def test_launch_card_preserves_app_interrupt_over_genuine_termios_restore_error(self):
        error = KeyboardInterrupt()
        self.assertIs(self._launch_card_restore_failure(error), error)

    def test_original_app_failure_survives_both_restore_failures(self):
        for error in (KeyboardInterrupt(), RuntimeError("fixture app failure")):
            with self.subTest(error=type(error).__name__):
                self.bad_output("good")
                self.events.clear()
                self.restore_errors = {fd: RuntimeError("fixture restore failure") for fd in (0, 1)}
                def app(win):
                    raise error
                caught = None
                try:
                    self.harness.run(app, in_fd=7, out_fd=8)
                except BaseException as actual:
                    caught = actual
                self.assertIs(caught, error)
                self.assert_restoration_order()
                self.assertIsNone(tui._INPUT_STATE)


if __name__ == "__main__":
    unittest.main()
