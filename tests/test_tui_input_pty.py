"""Terminal input journeys on a real pseudo-terminal.

A late answer to the background-colour query never closes a screen; an
unknown terminal type gets the line interface with one notice and no
query; Ctrl-C exits 130 with the terminal restored and nothing launched.
Direct without a model, upper- and lower-case keys, ``?`` typed into a
text field, scrolling, the pending-change previews, cancelled recovery
actions and W on Providers and Direct are driven key by key.
"""

from __future__ import annotations

import json
import shutil
import signal
import tempfile
import termios
import time
import unittest
from pathlib import Path
from unittest import mock

from test_tui_pty import (
    END_KEY,
    FIXTURE_TIMEOUT,
    TESTS_ROOT,
    PTYProcess,
    _line_of,
    _read_after,
    _restored,
)

CARD_STATUS = b"Status  "
SESSIONS_READY = "sessions — project (cwd) · 1 total".encode()
OSC_QUERY = b"\x1b]11;?"
OSC_REPLY = b"\x1b]11;rgb:1e1e/1e1e/1e1e\x1b\\"
TAB = b"\t"
# A local terminal: no remote markers, no colour hint (so the card asks).
LOCAL = {"COLORFGBG": "", "SSH_CONNECTION": "", "SSH_CLIENT": "", "SSH_TTY": ""}
# The fixture runtime's own environment says TERM=dumb; these children name a
# capable terminal there too, so the card asks for the background colour.
CAPABLE = ", environ_extra={'TERM': 'xterm-256color'}"


def _child_code(temp: str, argv: list[str], *, runtime_kwargs: str = "", setup: str = "", after: str = "") -> str:
    return f"""
import sys
from pathlib import Path
import _tui_fixture as fx
from charmer import catalog, sessions
from charmer.cli import entry, session_facts
def fake(prepared):
    print('FAKE_LAUNCH=' + prepared.result.session_action.kind, flush=True)
    return 0
runtime = fx.fixture_runtime(Path({temp!r}), launch_callback=fake{runtime_kwargs})
{setup}
with fx.hermetic(runtime):
    code = entry.main({argv!r}, runtime=runtime, input_stream=sys.stdin, output_stream=sys.stdout,
                      interactive=True)
print('EXIT=' + str(code), flush=True)
{after}
raise SystemExit(code)
"""


_RECORD = """
record = fx.v4_record(runtime, catalog.DEFAULT_SEED, managed_id=fx.FIXED_ID, follow={follow})
transcript = (Path(runtime.environ['HOME']) / '.claude' / 'projects'
              / session_facts._native_project_slug(record['cwd']) / (record['runtime_session_id'] + '.jsonl'))
transcript.parent.mkdir(parents=True, exist_ok=True)
transcript.touch()
{pending}
before = fx.canonical(runtime.session_store.load(fx.FIXED_ID))
"""
_PENDING = """
record = runtime.session_store.load(fx.FIXED_ID)
record['pending'] = {'requested_at': record['created_at'], 'kind': 'profile',
                     'profile': next(n for n in sorted(runtime.catalog.seed_profiles) if n != record['profile']),
                     'follow': True, 'document': None, 'reasons': ['native agents: explore replace → native']}
runtime.session_store.save(record)
"""
_UNCHANGED = """
after = runtime.session_store.load(fx.FIXED_ID) if runtime.session_store.exists(fx.FIXED_ID) else None
print('UNCHANGED=' + str(after is not None and fx.canonical(after) == before), flush=True)
"""


class _InputCase(unittest.TestCase):
    def child(self, prefix: str, argv=(), *, env=None, diagnostics: bool = False, **kwargs) -> PTYProcess:
        temp = tempfile.mkdtemp(prefix=prefix)
        self.addCleanup(shutil.rmtree, temp, True)
        child = PTYProcess(_child_code(temp, list(argv), **kwargs), extra_pythonpath=(TESTS_ROOT,),
                           extra_env={**LOCAL, **(env or {})}, diagnostics=diagnostics)
        self.addCleanup(child.close)
        return child

    def press(self, child: PTYProcess, key: bytes, needle: bytes) -> None:
        offset = len(child.output)
        child.send(key)
        _read_after(child, offset, needle)

    def quit_card(self, child: PTYProcess) -> bytes:
        child.send(b"\x1b")
        code, output, after = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(b"Nothing was launched.", output)
        self.assertNotIn(b"FAKE_LAUNCH", output)
        _restored(self, child, after)
        return output


class LateColourAnswerTests(_InputCase):
    """The colour query times out quickly; an answer that arrives later is
    read as one sequence, never as Esc followed by text."""

    def _late(self, delay: float) -> None:
        child = self.child(f"charmer-pty-osc-{int(delay * 1000)}-", runtime_kwargs=CAPABLE)
        child.read_until(OSC_QUERY, FIXTURE_TIMEOUT)
        time.sleep(delay)
        # Under load, the query can finish before curses enters no-echo
        # mode. Wait for the card as well as the minimum reply delay, so
        # this tests a late reply at the key reader, not kernel input echo.
        child.read_until(CARD_STATUS, FIXTURE_TIMEOUT)
        child.send(OSC_REPLY)
        # The card is still open and keys still work.
        self.press(child, b"?", "launch card — help".encode())
        self.press(child, b"\x1b", CARD_STATUS)
        output = self.quit_card(child)
        self.assertNotIn(b"rgb:1e1e", output.split(OSC_QUERY, 1)[1])

    def test_late_reply_waits_for_the_card_before_injection(self) -> None:
        child = mock.Mock()
        with mock.patch.object(self, "child", return_value=child), \
                mock.patch.object(self, "press"), mock.patch.object(self, "quit_card", return_value=OSC_QUERY), \
                mock.patch.object(time, "sleep") as sleep:
            self._late(0.2)
        sleep.assert_called_once_with(0.2)
        self.assertEqual(child.method_calls, [mock.call.read_until(OSC_QUERY, FIXTURE_TIMEOUT),
                                             mock.call.read_until(CARD_STATUS, FIXTURE_TIMEOUT),
                                             mock.call.send(OSC_REPLY)])

    def test_an_answer_after_200_ms(self) -> None:
        self._late(0.2)

    def test_an_answer_after_one_second(self) -> None:
        self._late(1.0)

    def test_a_terminator_that_arrives_after_the_card_is_drawn(self) -> None:
        # The reply starts inside the query's wait; its ESC backslash comes
        # only once the card is up, at the key reader.
        child = self.child("charmer-pty-osc-split-", runtime_kwargs=CAPABLE)
        child.read_until(OSC_QUERY, FIXTURE_TIMEOUT)
        child.send(OSC_REPLY[:-2])
        child.read_until(CARD_STATUS, FIXTURE_TIMEOUT)
        time.sleep(0.3)
        child.send(OSC_REPLY[-2:])
        # The card is still open and keys still work.
        self.press(child, b"?", "launch card — help".encode())
        self.press(child, b"\x1b", CARD_STATUS)
        self.quit_card(child)

    def test_a_reply_in_three_fragments_with_a_pause_before_its_terminator(self) -> None:
        # The reply starts inside the query's wait, more of its body comes
        # once the card is up, then a pause, then its ESC backslash: the card
        # stays open and no byte of the reply arrives as a key.
        child = self.child("charmer-pty-osc-three-", runtime_kwargs=CAPABLE)
        child.read_until(OSC_QUERY, FIXTURE_TIMEOUT)
        child.send(b"\x1b]11;rgb:0000/0000/")
        child.read_until(CARD_STATUS, FIXTURE_TIMEOUT)
        child.send(b"0000")
        time.sleep(0.3)
        child.send(b"\x1b\\")
        time.sleep(0.3)
        self.press(child, b"?", "launch card — help".encode())
        self.press(child, b"\x1b", CARD_STATUS)
        self.quit_card(child)

    def test_esc_then_enter_after_a_cut_reply_cancels(self) -> None:
        # The reply starts inside the query's wait and its rest never comes;
        # once the card is up, a real Esc and then Enter right after it: the
        # Esc cancels the card, and nothing is launched.
        child = self.child("charmer-pty-osc-esc-enter-", runtime_kwargs=CAPABLE)
        child.read_until(OSC_QUERY, FIXTURE_TIMEOUT)
        child.send(b"\x1b]11;rgb:0000/0000/")
        child.read_until(CARD_STATUS, FIXTURE_TIMEOUT)
        time.sleep(0.2)
        child.send(b"\x1b")
        child.send(b"\r")
        code, output, after = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(b"Nothing was launched.", output)
        self.assertNotIn(b"FAKE_LAUNCH", output)
        _restored(self, child, after)


class TerminalTypeTests(_InputCase):
    def test_an_unknown_terminal_type_gets_the_line_interface_and_one_notice(self) -> None:
        child = self.child("charmer-pty-term-", env={"TERM": "xterm-nonexistent-type"})
        child.read_until("Enter launch · q quit: ".encode(), FIXTURE_TIMEOUT)
        child.send(b"q\r")
        code, output, after = child.finish()
        self.assertEqual(code, 0, output)
        self.assertEqual(output.count(b"TERM=xterm-nonexistent-type is not known here"), 1)
        self.assertIn(b"TERM=xterm-256color", output)
        self.assertNotIn(OSC_QUERY, output)
        self.assertNotIn(b"FAKE_LAUNCH", output)
        _restored(self, child, after)


class InterruptTests(_InputCase):
    def test_ctrl_c_on_the_card_exits_130_and_restores_the_terminal(self) -> None:
        child = self.child("charmer-pty-ctrl-c-", diagnostics=True)
        child.read_until(CARD_STATUS, FIXTURE_TIMEOUT)
        # The test terminal is not the child's controlling terminal, so the
        # interrupt Ctrl-C raises is delivered as the signal itself. The
        # sideband records phases but adds no input-readiness wait.
        child.diagnostics.mark_sigint()
        child.process.send_signal(signal.SIGINT)
        code, output, after = child.finish()
        self.assertEqual(code, 130, output)
        self.assertIn(b"nothing was launched", output)
        self.assertNotIn(b"FAKE_LAUNCH", output)
        self.assertNotIn(b"Traceback", output)
        _restored(self, child, after)


class InterruptDiagnosticContractTests(unittest.TestCase):
    def test_diagnostics_add_no_readiness_wait_and_keep_the_exit_and_restore_assertions(self) -> None:
        case = InterruptTests("test_ctrl_c_on_the_card_exits_130_and_restores_the_terminal")
        child = mock.Mock()
        child.before = [termios.IXON, 0, 0, termios.ECHO | termios.ICANON]
        output = b"nothing was launched"
        child.finish.return_value = (130, output, child.before[:])
        with mock.patch.object(case, "child", return_value=child) as make_child:
            case.test_ctrl_c_on_the_card_exits_130_and_restores_the_terminal()
        make_child.assert_called_once_with("charmer-pty-ctrl-c-", diagnostics=True)
        self.assertEqual(child.method_calls, [mock.call.read_until(CARD_STATUS, FIXTURE_TIMEOUT),
                                             mock.call.diagnostics.mark_sigint(),
                                             mock.call.process.send_signal(signal.SIGINT), mock.call.finish()])
        # Diagnostics must not mask a bad normal exit, a launch, a traceback,
        # a missing cancellation message, or either terminal-restore failure.
        for code, text, after in ((0, output, child.before), (130, b"", child.before),
                                  (130, output + b" FAKE_LAUNCH", child.before),
                                  (130, output + b" Traceback", child.before),
                                  (130, output, [0, 0, 0, child.before[3]]),
                                  (130, output, [child.before[0], 0, 0, 0])):
            with self.subTest(code=code, text=text, after=after):
                child.finish.return_value = (code, text, after)
                with mock.patch.object(case, "child", return_value=child), self.assertRaises(AssertionError):
                    case.test_ctrl_c_on_the_card_exits_130_and_restores_the_terminal()


class CardKeyTests(_InputCase):
    def test_keys_work_in_both_cases(self) -> None:
        child = self.child("charmer-pty-case-")
        child.read_until(CARD_STATUS, FIXTURE_TIMEOUT)
        for key in (b"s", b"S"):
            self.press(child, key, b"sessions \xe2\x80\x94")
            self.press(child, b"\x1b", CARD_STATUS)
        for key in (b"g", b"G"):
            self.press(child, key, "providers — local status".encode())
            self.press(child, b"\x1b", CARD_STATUS)
        self.quit_card(child)

    def test_the_help_scrolls_to_its_end(self) -> None:
        child = self.child("charmer-pty-scroll-")
        child.read_until(CARD_STATUS, FIXTURE_TIMEOUT)
        self.press(child, b"?", "launch card — help".encode())
        self.press(child, END_KEY, b"-- workflow guarantees")
        self.press(child, b"\x1b", CARD_STATUS)
        self.quit_card(child)

    def test_a_question_mark_typed_in_a_text_field_is_text(self) -> None:
        child = self.child("charmer-pty-field-")
        child.read_until(CARD_STATUS, FIXTURE_TIMEOUT)
        self.press(child, b"o", "settings — global".encode())
        # The kept environment row: End reaches Import apply, then up past
        # the three Portability rows, Gateway and the two Providers rows.
        child.send(END_KEY)
        for _ in range(6):
            child.send(b"k")
        self.press(child, b"\r", "kept environment".encode())
        child.send(b"MCP?")
        self.press(child, b"\r\r", b"0-9")
        self.assertIn(b"not saved, nothing changed", bytes(child.output))
        self.assertNotIn("settings — help".encode(), bytes(child.output))
        self.press(child, b"\x1b", b"\x1b[")
        self.press(child, b"\x1b", CARD_STATUS)
        self.quit_card(child)


class RemedyLayoutTests(_InputCase):
    def test_narrow_picker_warning_is_focused_and_v_opens_details(self) -> None:
        setup = """
from charmer import tui, views

def picker_main(argv, *, runtime, input_stream, output_stream, interactive):
    lcat, effective = runtime.lineup_catalog(), runtime.current_effective()
    rows = views.line_rows(lcat, effective, custom_ids=frozenset())
    picker = views.picker_rows(rows, slot="cm-reviewer", bindings={}, lcat=lcat,
                               eff=effective, current=None)
    selected = tui.run_curses_on_streams(
        lambda win: tui.BindingPicker(picker, palette=tui.MONO_PALETTE).run(win),
        input_stream, output_stream, palette=tui.MONO_PALETTE)
    print("PICKER_RESULT=" + repr(selected), flush=True)
    return 0
entry.main = picker_main
"""
        child = self.child("charmer-pty-picker-remedy-", setup=setup)
        child.read_until(b"Attention: effort unverified", FIXTURE_TIMEOUT)
        self.press(child, b"v", "model — details".encode())
        child.read_until(b"effort unverified for this line", FIXTURE_TIMEOUT)
        self.press(child, b"\x1b", "reviewer — choose model".encode())
        child.send(b"\x1b")
        code, output, after = child.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(b"PICKER_RESULT=None", output)
        self.assertNotIn(b"FAKE_LAUNCH", output)
        _restored(self, child, after)

    def test_narrow_models_keeps_full_approval_remedy_and_separate_badge(self) -> None:
        setup = """
import dataclasses
from charmer import tui
from charmer.cli.screens.models import _ModelsScreen

def models_main(argv, *, runtime, input_stream, output_stream, interactive):
    key = next(iter(runtime.lineup_catalog().lines))
    original = runtime.current_effective
    runtime.current_effective = lambda: dataclasses.replace(original(), unavailable_lines={
        key: "provider fixture: route unapproved; route approval required — charmer providers approve fixture"})
    screen = _ModelsScreen(runtime, palette=tui.MONO_PALETTE)
    screen.index = screen.keys.index(key)
    tui.run_curses_on_streams(screen.run, input_stream, output_stream, palette=tui.MONO_PALETTE)
    return 0
entry.main = models_main
"""
        child = self.child("charmer-pty-model-remedy-", setup=setup)
        child.read_until(b"charmer providers approve fixture", FIXTURE_TIMEOUT)
        child.read_until(b"admission: not admitted (optional)", FIXTURE_TIMEOUT)
        child.send(b"\x1b")
        code, output, after = child.finish()
        self.assertEqual(code, 0, output)
        self.assertNotIn(b"FAKE_LAUNCH", output)
        _restored(self, child, after)


class DirectTests(_InputCase):
    _NO_LEADS = """
lcat = runtime.lineup_catalog()
for provider in sorted({e['provider'] for e in lcat.lines.values() if 'lead' in e['capabilities']}):
    runtime.settings_store.set_provider_enabled(provider, False, catalog=runtime.catalog)
"""

    def test_direct_disabled_models_stay_visible_with_remedy_and_never_launch(self) -> None:
        child = self.child("charmer-pty-direct-none-", ["direct"], setup=self._NO_LEADS)
        child.read_until("provider off — G → Space enables it".encode(), FIXTURE_TIMEOUT)
        self.assertIn(b"(unavailable)", bytes(child.output))
        self.press(child, b"\r", b"model unavailable")
        self.press(child, b"\x1b", "direct session — lead only".encode())
        offset = len(child.output)
        child.send(b"\x1b")
        code, output, after = child.finish()
        self.assertNotIn(b"FAKE_LAUNCH", output)
        self.assertNotIn(b"Traceback", output[offset:])
        self.assertIn(b"EXIT=", output)
        _restored(self, child, after)

    def test_w_is_offered_on_direct_and_providers_while_the_gateway_is_down(self) -> None:
        child = self.child("charmer-pty-w-", runtime_kwargs=", gateway_down=True")
        child.read_until(CARD_STATUS, FIXTURE_TIMEOUT)
        self.press(child, b"d", "direct session — lead only".encode())
        self.press(child, b"W", "Local gateway".encode())
        self.press(child, b"\x1b", b"\x1b[")
        self.press(child, b"\x1b", b"\x1b[")
        self.press(child, b"g", "providers — local status".encode())
        self.press(child, b"w", "Local gateway".encode())
        self.press(child, b"\x1b", b"\x1b[")
        self.press(child, b"\x1b", b"\x1b[")
        output = self.quit_card(child)
        # Both bars named W while the gateway was down.
        self.assertGreaterEqual(output.count(b"W\x1b[39m\x1b(B\x1b[m gateway"), 2)


class SessionPreviewTests(_InputCase):
    def sessions(self, prefix: str, *, follow: bool, pending: bool) -> PTYProcess:
        setup = _RECORD.format(follow=follow, pending=_PENDING if pending else "")
        child = self.child(prefix, ["-r"], setup=setup, after=_UNCHANGED)
        child.read_until(SESSIONS_READY, FIXTURE_TIMEOUT)
        return child

    def finish_unchanged(self, child: PTYProcess) -> None:
        child.send(b"\x1b")
        code, output, after = child.finish()
        self.assertEqual(code, 0, output)
        self.assertEqual(_line_of(output, b"UNCHANGED="), "True")
        self.assertNotIn(b"FAKE_LAUNCH", output)
        _restored(self, child, after)

    def test_follow_previews_the_dropped_change_and_esc_keeps_it(self) -> None:
        child = self.sessions("charmer-pty-follow-", follow=False, pending=True)
        self.press(child, b"f", b"follow \xe2\x80\x94 preview for")
        child.read_until(b"dropped", FIXTURE_TIMEOUT)
        self.press(child, b"\x1b", b"nothing changed")
        self.finish_unchanged(child)

    def test_keep_previews_discarding_the_pending_change_and_esc_writes_nothing(self) -> None:
        child = self.sessions("charmer-pty-keep-", follow=True, pending=True)
        self.press(child, b"T", b"change lineup")
        for _ in range(3):
            offset = len(child.output)
            child.send(TAB)
            _read_after(child, offset, b"\x1b[")
        child.read_until(b"discard the pending change", FIXTURE_TIMEOUT)
        self.press(child, b"\x1b", b"\x1b[")
        self.finish_unchanged(child)

    def test_recovery_actions_cancel_by_default(self) -> None:
        child = self.sessions("charmer-pty-recovery-", follow=True, pending=False)
        self.press(child, b"m", "ended?".encode())
        self.press(child, b"\r", b"nothing changed")
        self.press(child, b"x", b"Forget session")
        self.press(child, b"\r", b"Forget cancelled.")
        self.finish_unchanged(child)


class ByteReaderPTYTests(unittest.TestCase):
    def child(self, count, *, env=None, cycles=1):
        code = f"""
import codecs, curses, json, locale, os, sys
from charmer import tui
for name in ('C.UTF-8', 'en_US.UTF-8', 'UTF-8'):
    try:
        locale.setlocale(locale.LC_CTYPE, name)
        break
    except locale.Error:
        continue
assert codecs.lookup(locale.getencoding()).name == 'utf-8'
keys = []
def app(win):
    tui.safe_add(win, 0, 2, 'BYTE-INPUT-READY')
    win.refresh()
    os.write(1, ('DELAY=' + str(curses.get_escdelay()) + '\\n').encode())
    for _ in range({count}):
        key = tui.read_key(win)
        keys.append([key.kind, key.ch])
        os.write(1, ('STEP=' + str(len(keys)) + '\\n').encode())
try:
    for cycle in range({cycles}):
        tui.run_curses_on_streams(app, sys.stdin, sys.stdout, palette=tui.MONO_PALETTE)
        print('CYCLE=' + str(cycle + 1), flush=True)
except tui.CursesError as error:
    print('INPUT_ERROR=' + str(error), flush=True)
    raise SystemExit(2)
print('KEYS=' + json.dumps(keys), flush=True)
"""
        child = PTYProcess(code, extra_env=env)
        self.addCleanup(child.close)
        child.read_until(b"BYTE-INPUT-READY", FIXTURE_TIMEOUT)
        return child

    def finish_keys(self, child, expected):
        code, output, after = child.finish()
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(_line_of(output, b"KEYS=")), expected)
        self.assertNotIn(b"Traceback", output)
        self.assertEqual(after, child.before)

    def test_split_utf8_past_slice_and_across_resize(self):
        child = self.child(2)
        child.send(b"\xe7")
        time.sleep(0.16)
        child.resize(25, 81)
        child.read_until(b"STEP=1", FIXTURE_TIMEOUT)
        child.send(b"\x95\x8c")
        self.finish_keys(child, [["resize", ""], ["char", "界"]])

    def test_alt_unicode_tail_survives_peek_expiration(self):
        child = self.child(2)
        child.send(b"\x1b\xc3")
        child.read_until(b"STEP=1", FIXTURE_TIMEOUT)
        child.send(b"\xb6")
        self.finish_keys(child, [["esc", ""], ["char", "ö"]])

    def test_explicit_long_escdelay_keeps_native_keypad_continuation(self):
        child = self.child(3, env={"ESCDELAY": "600"})
        child.read_until(b"DELAY=600", FIXTURE_TIMEOUT)
        child.send(b"\x1bO")
        # Keypad's continuation wait is independent of the 100 ms slice.
        time.sleep(0.16)
        child.send(b"A")
        child.read_until(b"STEP=1", FIXTURE_TIMEOUT)
        child.send(b"\x1bx")
        self.finish_keys(child, [["up", ""], ["esc", ""], ["char", "x"]])

    def test_reentry_reuses_owned_screen_without_losing_eight_bit_input(self):
        child = self.child(1, cycles=2)
        child.send(b"\xc3\xb6")
        child.read_until(b"CYCLE=1", FIXTURE_TIMEOUT)
        child.send(b"\xc3\xb6")
        self.finish_keys(child, [["char", "ö"], ["char", "ö"]])

    def test_independent_output_tty_with_istrip_is_refused_and_restored(self):
        code = """
import copy, os, pty, select, sys, termios
from charmer import tui

def identity(fd):
    info = os.fstat(fd)
    return info.st_dev, info.st_ino, info.st_rdev

master, slave = pty.openpty()
try:
    original_fds = [identity(fd) for fd in (0, 1)]
    original_attrs = [termios.tcgetattr(fd) for fd in (0, 1)]
    assert all(os.isatty(fd) for fd in (0, 1, slave)), 'fixture requires owned ttys'
    assert all(attrs[2] & termios.CSIZE == termios.CS8 and not attrs[0] & termios.ISTRIP
               for attrs in original_attrs), 'fixture input/output A must be eight-bit'
    output_attrs = copy.deepcopy(termios.tcgetattr(slave))
    assert output_attrs[2] & termios.CSIZE == termios.CS8, 'fixture output B must be CS8'
    output_attrs[0] |= termios.ISTRIP
    termios.tcsetattr(slave, termios.TCSANOW, output_attrs)
    output_attrs = termios.tcgetattr(slave)
    assert output_attrs[0] & termios.ISTRIP, 'fixture output B did not retain ISTRIP'
    assert identity(slave) != original_fds[0], 'fixture requires independent ttys'
    entered = []
    wrappers = []
    original_wrapper = tui.curses.wrapper
    def wrapper(func):
        wrappers.append(True)
        return original_wrapper(func)
    def app(win):
        entered.append(True)
        raise AssertionError('incompatible output reached app')
    with os.fdopen(os.dup(slave), 'w', buffering=1) as output:
        tui.curses.wrapper = wrapper
        refused = False
        try:
            try:
                tui.run_curses_on_streams(app, sys.stdin, output, palette=tui.MONO_PALETTE)
            except tui.CursesError as error:
                assert str(error) == tui.INPUT_MODE_ERROR, 'unexpected refusal'
                refused = True
        finally:
            tui.curses.wrapper = original_wrapper
        assert refused and not wrappers and not entered, 'refusal must precede curses'
        assert [identity(fd) for fd in (0, 1)] == original_fds, 'descriptor restoration'
        assert [termios.tcgetattr(fd) for fd in (0, 1)] == original_attrs, 'tty A restoration'
        assert termios.tcgetattr(slave) == output_attrs, 'tty B restoration'
        assert tui._INPUT_STATE is None, 'input state cleanup'
        assert select.select([master], [], [], 1)[0], 'notice missing on tty B'
        notice = os.read(master, 4096).replace(b'\\r\\n', b'\\n')
        assert notice == (tui.INPUT_MODE_NOTICE + '\\n').encode(), 'notice destination'
finally:
    os.close(master)
    os.close(slave)
print('INDEPENDENT_TTYS_REFUSED_AND_RESTORED', flush=True)
"""
        child = PTYProcess(code)
        self.addCleanup(child.close)
        status, output, after = child.finish()
        self.assertEqual(status, 0, output)
        self.assertIn(b"INDEPENDENT_TTYS_REFUSED_AND_RESTORED", output)
        self.assertNotIn(b"full-screen input requires", output)
        self.assertNotIn(b"Traceback", output)
        self.assertEqual(after, child.before)

    def test_malformed_input_fails_and_restores_exact_attributes(self):
        child = self.child(1)
        child.send(b"\xff")
        code, output, after = child.finish()
        self.assertEqual(code, 2, output)
        self.assertIn(b"INPUT_ERROR=terminal input is invalid for the locale encoding", output)
        self.assertNotIn(b"Traceback", output)
        self.assertEqual(after, child.before)


if __name__ == "__main__":
    unittest.main()
