"""H: doctor's findings as a list and its fixed set of actions.

The list is ``doctor.present_report``'s rows, the ones plain doctor prints: the
same order, totals and action targets. Each action is driven through the
screen on ``test_tui.FakeWindow`` (``MONO_PALETTE``) over a hermetic fixture
runtime; the doctor services an action reuses are recorded, or run on
fixture records where what they skip matters. A finding's text is data:
drawn, never parsed or run. Nothing here reaches a gateway or a provider.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import curses as _curses

from charmer import catalog, gateway_hold, sessions, tui
from charmer.cli import doctor, gateway_facts
import charmer.cli.doctor_actions as doctor_actions
import charmer.cli.screens.findings as findings
import charmer.cli.screens.gateway_actions as gateway_actions
import charmer.cli.screens.launch_sessions as launch_sessions
import charmer.cli.text as cli_text

import _tui_fixture as fx
from _tui_render import golden_runtime, render
from test_tui import DOWN, HOME, FakeWindow

ESC = "\x1b"
ENTER = "\n"
RIGHT = _curses.KEY_RIGHT
KEY_PROVIDER = "fixture-keyed"


def _flat(frames: list[str]) -> str:
    return "\n".join(" ".join(line.strip(" |") for line in frame.splitlines()) for frame in frames)


def hold_line(*, coverage: bool = True) -> gateway_facts.Line:
    if coverage:
        return gateway_facts.tagged(gateway_facts.HOLD_COVERAGE_TEXT.format(reasons="journald retention is shorter"),
                                    area="Gateway", action="hold-coverage")
    return gateway_facts.tagged("gateway persistence hold: HELD — stop, restart, update and uninstall are refused: "
                                "a credential save failed (claude) — " + gateway_hold.REMEDY,
                                area="Gateway", action="hold")


def advisory(profile_name: str, slot: str, key: str = "fixture-op-model") -> gateway_facts.Line:
    message = f"{key}: qualification predates the current pins (stale evidence)"
    return gateway_facts.tagged(f"profile {profile_name}: {message}", area="Models", level=doctor.ADVISORY,
                                group=("model", "qualification", message), short=message, subject=key,
                                owner=profile_name, slot=slot, action="advisory-model")


def mixed_report() -> tuple[list[str], list[str], list[str]]:
    """A report like the operator's: profile lines collected before the hold,
    a scope repair, sessions that need a lead, an unused alias and 3 × 4
    advisories on one model."""

    attention: list[str] = []
    attention += [advisory(name, slot) for name in ("alpha", "beta", "gamma")
                  for slot in ("cm-lead", "cm-explorer", "cm-reviewer", "cm-analyst")]
    attention.append(gateway_facts.tagged("profile alpha: next launch: lead: no OAuth credential record "
                                          "(readiness; never a block)", area="Profiles"))
    attention.append(gateway_facts.tagged("continuity alias charmer-old is not served (its upstream route may be "
                                          "gone); once no live session uses it: charmer doctor --prune-aliases "
                                          "charmer-old", area="Gateway", action="unused-alias",
                                          subject="charmer-old"))
    attention.append(gateway_facts.tagged("2 session(s) need a profile or model choice (lead removed): resume "
                                          "interactively with charmer -r <id> — 1111aaaa, 2222bbbb",
                                          area="Sessions", action="needs-choice"))
    attention.append(hold_line())
    problems = [gateway_facts.tagged("session 3333cccc: scope differs from the record-authoritative compile "
                                     "(settings.json content differs); run charmer doctor --repair "
                                     "3333cccc-0000-4000-8000-000000000000", area="Sessions",
                                     action="scope-repair")]
    return problems, ["Sessions: 3 recorded.", "Session 3333cccc: detail"], attention


class _FindingsCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cm-findings-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.runtime = fx.fixture_runtime(self.tmp / "root")
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(fx.hermetic(self.runtime))
        stack.enter_context(mock.patch("charmer.cli.gateway_facts._doctor_now", lambda: fx.FIXED_NOW))
        self.runtime.profiles.install_seeds()

    def screen(self, report=None, **kwargs) -> findings.FindingsScreen:
        lines = report if report is not None else mixed_report()
        reports = lines if isinstance(lines, list) else [lines]
        calls = iter(reports)
        last = [reports[-1]]

        def collect():
            try:
                last[0] = next(calls)
            except StopIteration:
                pass
            return last[0]

        kwargs.setdefault("tty_in", io.StringIO())
        kwargs.setdefault("tty_out", io.StringIO())
        return findings.FindingsScreen(self.runtime, palette=tui.MONO_PALETTE, report=collect, **kwargs)

    @staticmethod
    def drive(screen, keys, *, height=24, width=80):
        win = FakeWindow(list(keys), height=height, width=width)
        return screen.run(win), win

    @staticmethod
    def select(screen, action: str) -> list:
        """The DOWN presses that select the first row with ``action``."""

        rows = [screen.lines[index].row for index in screen.selectable]
        index = next(i for i, row in enumerate(rows) if row is not None and row.action == action)
        return [DOWN] * index


# ================================================================ the list


class FindingsListTests(_FindingsCase):
    def test_blocked_first_then_attention_by_area_with_the_hold_first(self) -> None:
        screen = self.screen()
        kinds = [(item.kind, item.text.strip()) for item in screen.lines]
        texts = [text for _kind, text in kinds]
        self.assertTrue(texts[0].startswith("BLOCKED (1)"))
        self.assertTrue(texts[2].startswith("Attention (4)"))
        areas = [text.split(" (")[0] for kind, text in kinds if kind == "area"]
        self.assertEqual(areas, ["Gateway", "Sessions", "Profiles"])
        gateway_rows = texts[texts.index("Gateway (2)") + 1:texts.index("Gateway (2)") + 3]
        # Collected last, listed first: the persistence hold leads the Gateway area.
        self.assertTrue(gateway_rows[0].startswith("! gateway persistence hold: HELD"), gateway_rows)
        self.assertEqual(kinds[-2][0], "toggle")
        self.assertIn(doctor.ADVISORIES_TITLE, kinds[-2][1])
        self.assertEqual(kinds[-1][0], "verify")

    def test_cli_and_h_share_ordering_totals_and_targets(self) -> None:
        problems, info, attention = mixed_report()
        screen = self.screen((problems, info, attention))
        screen.expanded = True
        screen._rebuild()
        listed = [item.row for item in screen.lines if item.row is not None]
        presented = doctor.present_report(problems, attention).rows
        self.assertEqual(listed, list(presented))
        printed = [line.strip().removeprefix("- ") for _role, line in
                   doctor.report_lines(problems, info, attention, verbose=False)]
        for row in listed:
            self.assertIn(row.text, printed)
        self.assertEqual([printed.index(row.text) for row in listed],
                         sorted(printed.index(row.text) for row in listed))
        self.assertEqual({row.action for row in listed},
                         {"scope-repair", "hold-coverage", "unused-alias", "needs-choice", "advisory-model", None})

    def test_advisories_are_one_row_closed_until_enter_opens_them(self) -> None:
        screen = self.screen()
        self.assertFalse(any(item.row is not None and item.row.level == doctor.ADVISORY for item in screen.lines))
        keys = _steps_to(screen, lambda item: item.kind == "toggle") + [ENTER, ESC]
        _result, win = self.drive(screen, keys)
        opened = [item for item in screen.lines if item.row is not None and item.row.level == doctor.ADVISORY]
        self.assertEqual(len(opened), 1)
        self.assertIn("(3 profiles, 12 slots)", opened[0].text)
        self.assertEqual(len(opened[0].row.originals), 12)
        self.assertIn("▾ " + doctor.ADVISORIES_TITLE, win.frames[-1])

    def test_plain_doctor_and_opening_h_never_fetch_release_metadata(self) -> None:
        from charmer import install_verify, release_update
        from charmer.cli import entry
        from test_install_verification_cli import snapshot

        before = snapshot(self.tmp)
        with mock.patch.object(install_verify, "verify_current", side_effect=AssertionError("passive verify")), \
                mock.patch.object(release_update, "HttpsTransport", side_effect=AssertionError("passive fetch")):
            output = io.StringIO()
            entry.main(["doctor"], runtime=self.runtime, output_stream=output, interactive=False)
            screen = findings.FindingsScreen(self.runtime, palette=tui.MONO_PALETTE,
                                             tty_in=io.StringIO(), tty_out=io.StringIO())
            _result, win = self.drive(screen, [ESC])
        self.assertEqual(snapshot(self.tmp), before)
        self.assertIn("charmer doctor", win.frames[-1])
        self.assertTrue(any(item.kind == "verify" for item in screen.lines))
        self.assertNotIn("Verified against the signed release", output.getvalue())

    def test_v_shows_every_original_line(self) -> None:
        screen = self.screen()
        shown: list = []
        real = tui.TextView

        class Recording(real):
            def run(self, win):
                shown.append((self.title, list(self.lines)))
                return super().run(win)

        with mock.patch.object(tui, "TextView", Recording):
            _result, _win = self.drive(screen, ["v", ESC, ESC])
        title, lines = shown[0]
        self.assertEqual(title, f"{cli_text.FINDINGS_TITLE} -v")
        originals = [line for line in lines if "qualification predates" in line]
        self.assertEqual(len(originals), 12)
        self.assertIn("Session 3333cccc: detail", lines)  # the detail lines too

    def test_the_selected_row_shows_its_text_and_what_enter_does(self) -> None:
        screen = self.screen()
        keys = self.select(screen, "unused-alias") + [ESC]
        _result, win = self.drive(screen, keys)
        frame = win.frames[-1]
        self.assertIn("continuity alias charmer-old is not served", frame)
        self.assertIn(cli_text.FINDINGS_ACTIONS["unused-alias"], frame)

    def test_a_narrow_terminal_says_too_small_and_only_esc_acts(self) -> None:
        screen = self.screen()
        rows, cols = screen.min_size(40)
        _result, win = self.drive(screen, [ENTER, ESC], width=cols - 1, height=rows)
        self.assertIn(tui.TOO_SMALL.format(what=screen.what), win.frames[-1])

    def test_nothing_to_report(self) -> None:
        screen = self.screen(([], ["Sessions: 0 recorded."], []))
        _result, win = self.drive(screen, [ENTER, ESC, ESC])
        self.assertIn(f"{cli_text.FINDINGS_TITLE} — Ready", win.frames[-1])
        self.assertIn(cli_text.FINDINGS_NONE, win.frames[-1])


# ================================================================ the actions


class FindingsActionTests(_FindingsCase):
    def test_a_finding_with_shell_syntax_is_drawn_as_data_never_run(self) -> None:
        text = 'profile x: model `$(touch /tmp/pwned)`; rm -rf ~ && echo "$HOME" | sh'
        screen = self.screen(([], [], [text]))
        with mock.patch("subprocess.run", side_effect=AssertionError("ran")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("ran")), \
                mock.patch("os.system", side_effect=AssertionError("ran")):
            _result, win = self.drive(screen, [ENTER, ESC, ESC])
        shown = _flat(win.frames)
        self.assertIn(cli_text.FINDINGS_COMMAND_TITLE, shown)
        self.assertIn(text, shown)

    def test_a_row_without_an_action_shows_its_exact_command(self) -> None:
        finding = gateway_facts.Finding("CLAUDE_CONFIG_DIR is set (~/x), but managed sessions do not use it",
                                        code="claude-config-dir-ignored", public="CLAUDE_CONFIG_DIR is set",
                                        remedy="unset CLAUDE_CONFIG_DIR")
        screen = self.screen(([], [], [gateway_facts.tagged(finding, area="Installation")]))
        _result, win = self.drive(screen, [ENTER, ESC, ESC])
        shown = _flat(win.frames)
        self.assertIn(cli_text.FINDINGS_COMMAND.format(command="unset CLAUDE_CONFIG_DIR"), shown)

    def test_enter_on_a_hold_row_shows_the_clear_hold_route_and_clears_nothing(self) -> None:
        shown: list = []
        real = tui.TextView

        class Recording(real):
            def run(self, win):
                shown.append((self.title, list(self.lines)))
                return super().run(win)

        for coverage, route in ((True, gateway_actions.HOLD_COVERAGE_ROUTE),
                                (False, gateway_actions.HOLD_FAILURE_ROUTE)):
            with self.subTest(coverage=coverage):
                shown.clear()
                screen = self.screen(([], [], [hold_line(coverage=coverage)]))
                with mock.patch("charmer.gateway_lifecycle.Gateway.clear_hold",
                                side_effect=AssertionError("cleared")), \
                        mock.patch.object(tui, "TextView", Recording):
                    _result, _win = self.drive(screen, [ENTER, ESC, ESC])
                title, lines = shown[0]
                self.assertEqual(title, cli_text.FINDINGS_HOLD_TITLE)
                self.assertIn(route, lines)
                self.assertEqual(lines[-1], cli_text.FINDINGS_HOLD_CLEAR)

    def test_enter_on_a_missing_key_opens_the_key_entry(self) -> None:
        line = gateway_facts.tagged(f"profile p: provider {KEY_PROVIDER} has no credential (set its key)",
                                    area="Providers", action="missing-key", subject=KEY_PROVIDER)
        screen = self.screen(([], [], [line]))
        import charmer.cli.screens.providers as screens_providers

        with mock.patch.object(screens_providers.ConnectActions, "set_key",
                               return_value=screens_providers.Outcome("key saved")) as set_key:
            _result, win = self.drive(screen, [ENTER, ESC])
        set_key.assert_called_once_with(KEY_PROVIDER)
        self.assertIn("key saved", win.frames[-1])

    def test_enter_on_a_scope_row_repairs_after_a_confirmation(self) -> None:
        screen = self.screen()
        keys = self.select(screen, "scope-repair")
        with mock.patch.object(doctor_actions, "_doctor_repair_all", return_value=0) as repair:
            _result, win = self.drive(screen, [*keys, ENTER, ENTER, ESC])  # Cancel is focused first
            repair.assert_not_called()
            self.assertIn(cli_text.FINDINGS_CANCELLED, win.frames[-1])
            _result, win = self.drive(screen, [HOME, *keys, ENTER, RIGHT, ENTER, ESC, ESC])
        repair.assert_called_once()
        self.assertIs(repair.call_args.args[0], self.runtime)
        self.assertNotIn("include_live", repair.call_args.kwargs)  # stopped sessions only
        self.assertIn(cli_text.FINDINGS_REPAIR_TITLE, _flat(win.frames))

    def test_repair_from_the_list_skips_live_and_unknown_sessions(self) -> None:
        ended = fx.v4_record(self.runtime, catalog.DEFAULT_SEED, managed_id=fx.FIXED_ID, ended=True)
        running = fx.v4_record(self.runtime, catalog.DEFAULT_SEED, managed_id=fx.OTHER_ID)
        line = gateway_facts.tagged("2 scope(s) diverged from record authority; run `charmer doctor "
                                    "--repair-all` to converge them in one pass", area="Sessions",
                                    action="scope-repair")
        screen = self.screen(([line], [], []))
        _result, win = self.drive(screen, [ENTER, RIGHT, ENTER, ESC, ESC])
        shown = _flat(win.frames)
        self.assertIn("Repair-all: 1 session(s) converged to record authority.", shown)
        self.assertIn(f"skipped {sessions.managed_id(running)[:8]}: last event", shown)
        self.assertNotIn(f"skipped {sessions.managed_id(ended)[:8]}", shown)

    def test_enter_on_an_unused_alias_prunes_it_after_a_confirmation(self) -> None:
        problems, info, attention = mixed_report()
        after = (problems, info, [line for line in attention if getattr(line, "action", None) != "unused-alias"])
        screen = self.screen([(problems, info, attention), after])  # a cancel reads nothing again
        keys = self.select(screen, "unused-alias")
        with mock.patch.object(doctor_actions, "_doctor_prune_aliases", return_value=0) as prune:
            _result, win = self.drive(screen, [*keys, ENTER, ENTER, ESC])
            prune.assert_not_called()
            _result, win = self.drive(screen, [HOME, *keys, ENTER, RIGHT, ENTER, ESC, ESC])
        prune.assert_called_once()
        self.assertEqual(prune.call_args.args[1], ["charmer-old"])
        # The report is read again after the action: the row is gone.
        self.assertNotIn("charmer-old", "\n".join(item.text for item in screen.lines))

    def test_enter_on_leftovers_previews_then_prunes(self) -> None:
        line = gateway_facts.tagged("3 protocol-3 hook failure(s) logged since the last launch (start 3) in "
                                    "hook-errors.log (metadata only); charmer doctor --prune rotates the log "
                                    "once read", area="Sessions", action="prune")
        screen = self.screen(([], [], [line]))

        def preview(runtime, out):
            out.write("Candidate: lead-prompt-x.md — obsolete generated lead prompt\n")
            return 0

        with mock.patch.object(doctor_actions, "_doctor_prune_preview", side_effect=preview), \
                mock.patch.object(doctor_actions, "_doctor_prune", return_value=0) as prune:
            _result, win = self.drive(screen, [ENTER, "n", ESC])
            prune.assert_not_called()
            self.assertIn("Candidate: lead-prompt-x.md", _flat(win.frames))
            _result, win = self.drive(screen, [ENTER, "y", ESC, ESC])
        prune.assert_called_once()

    def test_enter_on_dead_sessions_marks_them_after_a_confirmation(self) -> None:
        dead = fx.v4_record(self.runtime, catalog.DEFAULT_SEED, managed_id=fx.FIXED_ID)
        self.runtime.proc_root = self.tmp / "proc"
        self.runtime.proc_root.mkdir()
        line = gateway_facts.tagged("1 session(s) have no recorded end and do not run", area="Sessions",
                                    action="dead-sessions")
        screen = self.screen(([], [], [line]))
        import charmer.cli.session_actions as session_actions

        with mock.patch.object(session_actions, "_mark_ended_ids",
                               return_value=[(fx.FIXED_ID, "marked ended")]) as mark:
            _result, win = self.drive(screen, [ENTER, ENTER, ESC])
            mark.assert_not_called()
            _result, win = self.drive(screen, [ENTER, RIGHT, ENTER, ESC, ESC])
        mark.assert_called_once_with(self.runtime, [sessions.managed_id(dead)], proc_root=self.runtime.proc_root)
        self.assertIn("marked ended", _flat(win.frames))

    def test_enter_on_a_newer_shipped_profile_reseeds_it(self) -> None:
        line = gateway_facts.tagged(f"profile {catalog.DEFAULT_SEED}: a newer shipped version of this seed exists "
                                    f"(v1 → v2): charmer profile reseed {catalog.DEFAULT_SEED}",
                                    area="Profiles", action="seed-update", subject=catalog.DEFAULT_SEED)
        screen = self.screen(([], [], [line]))
        from charmer.setup import profiles as setup_profiles

        plan = mock.Mock(nothing=False, lines=["the shipped changes"], version=2)
        with mock.patch.object(setup_profiles, "plan_reseed", return_value=plan) as planned, \
                mock.patch.object(setup_profiles, "apply_reseed",
                                  return_value=mock.Mock(lines=["reseeded"])) as applied, \
                mock.patch("charmer.cli.screens.propagation.run_propagation_screen") as propagate:
            _result, win = self.drive(screen, [ENTER, "n", ESC])
            applied.assert_not_called()
            _result, win = self.drive(screen, [ENTER, "y", ESC])
        planned.assert_called_with(self.runtime, catalog.DEFAULT_SEED)
        applied.assert_called_once()
        propagate.assert_called_once()
        self.assertIn("reseeded", win.frames[-1])

    def test_enter_on_an_advisory_model_opens_its_optional_diagnostics(self) -> None:
        screen = self.screen(([], [], [advisory("alpha", "cm-lead")]))
        import charmer.cli.screens.common as screens_common

        with mock.patch.object(screens_common.OnboardingActions, "qualify") as qualify:
            _result, _win = self.drive(screen, [ENTER, DOWN, ENTER, ESC])  # open the advisories, Enter on the row
        qualify.assert_called_once_with("fixture-op-model")

    def test_enter_on_an_admission_advisory_shows_its_next_step_and_command(self) -> None:
        from charmer import profile

        finding = next(item for item in profile.qualification_warnings("operator-model", None, slot="cm-reviewer")
                       if item.code == "admission")
        line = doctor._tag_model(f"profile p: {finding.message}", finding, key="operator-model", owner="p",
                                 slot="cm-reviewer")
        screen = self.screen(([], [], [line]))
        shown: list = []
        real = tui.TextView

        class Recording(real):
            def run(self, win):
                shown.append((self.title, list(self.lines)))
                return super().run(win)

        with mock.patch.object(tui, "TextView", Recording):
            _result, _win = self.drive(screen, [ENTER, DOWN, ENTER, ESC, ESC])  # open the advisories, Enter on it
        title, lines = shown[0]
        self.assertEqual(title, cli_text.FINDINGS_COMMAND_TITLE)
        self.assertTrue(lines[0].startswith("operator-model: not admitted"), lines)
        self.assertIn("optional: charmer models admit operator-model, or M → Enter (sends no request)", lines[0])
        self.assertIn(f"profile p: {finding.message}", lines)  # every line the row stands for
        self.assertEqual(lines[-1], cli_text.FINDINGS_COMMAND.format(command="charmer models admit operator-model"))

    def test_enter_on_a_needs_choice_row_opens_sessions(self) -> None:
        fx.v4_record(self.runtime, catalog.DEFAULT_SEED, managed_id=fx.FIXED_ID,
                     needs_choice=True, scope=False, generation=0)
        fx.v4_record(self.runtime, catalog.DEFAULT_SEED, managed_id=fx.OTHER_ID,
                     scope=False, generation=0)
        screen = self.screen()
        keys = self.select(screen, "needs-choice")
        with mock.patch.object(launch_sessions._SessionsScreen, "_needs_menu", return_value=None) as menu:
            result, _win = self.drive(screen, [*keys, ENTER, ESC])
        self.assertIsNone(result)
        self.assertEqual([record["managed_id"] for record in menu.call_args.args[1]], [fx.FIXED_ID])
        intent = ("perform", object())  # the explicit per-session-resume option can still return an intent
        screen = self.screen()
        with mock.patch.object(launch_sessions._SessionsScreen, "_needs_menu", return_value=intent):
            result, _win = self.drive(screen, [*self.select(screen, "needs-choice"), ENTER])
        self.assertIs(result, intent)

    def test_a_read_only_run_does_nothing(self) -> None:
        screen = self.screen()
        keys = self.select(screen, "scope-repair")
        with mock.patch.object(self.runtime, "allow_state_writes", False), \
                mock.patch.object(doctor_actions, "_doctor_repair_all", side_effect=AssertionError("wrote")):
            _result, win = self.drive(screen, [*keys, ENTER, ESC])
        self.assertIn(cli_text.FINDINGS_READ_ONLY, win.frames[-1])


# ================================================================ from doctor's own collection


FORGOTTEN_ID = "66666666-6666-4666-8666-666666666666"


class FindingsCollectedTests(_FindingsCase):
    """The housekeeping rows come from doctor's own checks on the fixture
    home, and H acts on them with the same services and guards: a session
    that exited without an end, and generated files of a forgotten session
    while the hook-error log is empty."""

    def setUp(self) -> None:
        super().setUp()
        # A process table this test owns: listable (known), with only the
        # processes a test writes.
        self.runtime.proc_root = self.tmp / "proc"
        self.runtime.proc_root.mkdir()
        self.root = self.runtime.session_store.root

    def running(self, managed: str) -> None:
        process = self.runtime.proc_root / "4242"
        process.mkdir()
        (process / "cmdline").write_bytes(b"claude\0--session-id\0" + managed.encode() + b"\0")

    def collected_rows(self, action: str) -> list:
        problems, _info, attention = doctor._collect_doctor_reports(self.runtime)
        return [row for row in doctor.present_report(problems, attention).rows if row.action == action]

    def real_screen(self) -> findings.FindingsScreen:
        return findings.FindingsScreen(self.runtime, palette=tui.MONO_PALETTE, tty_in=io.StringIO(),
                                       tty_out=io.StringIO())

    def test_an_exited_session_without_an_end_is_a_row_that_marks_it_ended(self) -> None:
        dead = fx.v4_record(self.runtime, catalog.DEFAULT_SEED, managed_id=fx.FIXED_ID)
        fx.v4_record(self.runtime, catalog.DEFAULT_SEED, managed_id=fx.OTHER_ID)
        fx.v4_record(self.runtime, catalog.DEFAULT_SEED, managed_id=fx.THIRD_ID, ended=True)
        self.running(fx.OTHER_ID)
        rows = self.collected_rows("dead-sessions")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].area, "Sessions")
        self.assertIn(f"charmer sessions mark-ended {fx.FIXED_ID}", rows[0].text)
        self.assertNotIn(fx.OTHER_ID[:8], " ".join(rows[0].originals))  # running
        self.assertNotIn(fx.THIRD_ID[:8], " ".join(rows[0].originals))  # ended
        screen = self.real_screen()
        keys = self.select(screen, "dead-sessions")
        _result, win = self.drive(screen, [*keys, ENTER, ENTER, ESC])  # Cancel is focused first
        self.assertNotEqual(self.store_record(fx.FIXED_ID).get("last_event_source"), "end")
        self.assertIn(cli_text.FINDINGS_CANCELLED, win.frames[-1])
        _result, win = self.drive(screen, [HOME, *keys, ENTER, RIGHT, ENTER, ESC, ESC])
        self.assertIn(f"{fx.FIXED_ID}  marked ended", _flat(win.frames))
        self.assertEqual(self.store_record(fx.FIXED_ID)["last_event_source"], "end")
        self.assertNotEqual(self.store_record(fx.OTHER_ID).get("last_event_source"), "end")
        self.assertEqual(sessions.managed_id(dead), fx.FIXED_ID)
        # The report is read again after the action: the row is gone.
        self.assertFalse([item for item in screen.lines if item.row is not None
                          and item.row.action == "dead-sessions"])

    def test_several_dead_sessions_are_one_row_with_the_first_three(self) -> None:
        for managed in (fx.FIXED_ID, fx.OTHER_ID, fx.THIRD_ID, fx.FOURTH_ID):
            fx.v4_record(self.runtime, catalog.DEFAULT_SEED, managed_id=managed)
        rows = self.collected_rows("dead-sessions")
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0].originals), 4)
        self.assertTrue(rows[0].text.startswith("4 sessions do not run and have no recorded end"), rows[0].text)
        self.assertIn("charmer sessions mark-ended --all-dead", rows[0].text)
        self.assertTrue(rows[0].text.endswith(f"11111111, 22222222, 33333333… {doctor.VERBOSE_HINT}"), rows[0].text)

    def test_no_dead_session_row_while_whether_sessions_run_cannot_be_told(self) -> None:
        fx.v4_record(self.runtime, catalog.DEFAULT_SEED, managed_id=fx.FIXED_ID)
        self.runtime.proc_root = self.tmp / "no-proc"  # unlistable: unknown
        self.assertEqual(self.collected_rows("dead-sessions"), [])
        self.runtime.proc_root = self.tmp / "proc"
        with mock.patch.object(self.runtime, "background_liveness",
                               lambda: sessions.BackgroundLiveness(False, frozenset(), "fixture: unknown")):
            self.assertEqual(self.collected_rows("dead-sessions"), [])

    def leftovers(self) -> list:
        from charmer import state

        scope = state.ensure_private_dir(self.root / "scopes" / FORGOTTEN_ID)
        state.atomic_write(scope / "settings.json", b"{}\n")
        prompt = self.root / f"lead-prompt-{'a' * 16}-{FORGOTTEN_ID}.md"
        state.atomic_write(prompt, b"generated\n")
        notice = state.ensure_private_dir(self.root / "notice") / f"{FORGOTTEN_ID}.seen"
        state.atomic_write(notice, b"")
        log = state.ensure_private_dir(self.root / "lineup-log") / f"{FORGOTTEN_ID}.log"
        state.atomic_write(log, b"{}\n")
        return [scope, prompt, notice, log]

    def test_generated_leftovers_are_a_row_that_previews_then_prunes(self) -> None:
        from charmer import hooks

        files = self.leftovers()
        self.assertFalse((self.root / hooks.HOOK_ERRORS_LOG).exists())  # no hook failure names the prune
        rows = self.collected_rows("prune")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].area, "Sessions")
        self.assertTrue(rows[0].text.startswith("4 generated files left by forgotten or ended sessions (1 scope, 1 "
                                                "lead prompt, 1 notice marker, 1 lineup log)"), rows[0].text)
        self.assertEqual(rows[0].remedy, doctor.LEFTOVERS_REMEDY)
        screen = self.real_screen()
        keys = self.select(screen, "prune")
        preview = io.StringIO()
        self.assertEqual(doctor_actions._doctor_prune_preview(self.runtime, preview), 0)
        # Keep all four candidate paths on one line each in the preview frame.
        width = max(80, max(map(len, preview.getvalue().splitlines())) + 4)
        _result, win = self.drive(screen, [*keys, ENTER, "n", ESC], width=width)
        self.assertTrue(all(os.path.lexists(path) for path in files))  # a cancel removes nothing
        shown = _flat(win.frames)
        self.assertEqual(shown.count("Candidate: "), 4)  # the preview names what the row counts
        self.assertIn("scope/staging for forgotten record", shown)
        _result, win = self.drive(screen, [HOME, *keys, ENTER, "y", ESC, ESC], width=width)
        self.assertIn(f"scope for forgotten session {FORGOTTEN_ID}", _flat(win.frames))
        self.assertFalse([path for path in files if os.path.lexists(path)])
        self.assertFalse([item for item in screen.lines if item.row is not None and item.row.action == "prune"])

    def test_one_leftover_is_worded_as_one(self) -> None:
        from charmer import state

        state.atomic_write(state.ensure_private_dir(self.root / "lineup-log") / f"{FORGOTTEN_ID}.log", b"{}\n")
        rows = self.collected_rows("prune")
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].text.startswith("1 generated file left by a forgotten or ended session (1 lineup "
                                                "log): charmer doctor --prune --preview lists it,"), rows[0].text)

    def test_no_leftover_row_while_a_record_cannot_be_read(self) -> None:
        self.leftovers()
        (self.root / "sessions" / f"{FIXED_ID_BROKEN}.json").write_text("{not json")
        self.assertEqual([row for row in self.collected_rows("prune") if "generated file" in row.text], [])

    def store_record(self, managed: str) -> dict:
        return self.runtime.session_store.load(managed)


FIXED_ID_BROKEN = "77777777-7777-4777-8777-777777777777"


# ================================================================ the gateway's actions


class FindingsGatewayTests(_FindingsCase):
    def gateway(self, keys, view, *, answer="\n"):
        out = io.StringIO()
        screen = self.screen(([], [], []), tty_in=io.StringIO(answer), tty_out=out)
        with fx.gateway_actions_seam(view) as acted:
            result, win = self.drive(screen, keys)
        self.assertIsNone(result)
        return acted, out.getvalue(), win

    def test_w_restarts_a_proven_gateway(self) -> None:
        view = fx.GatewayViewStub(restart=True)
        acted, printed, win = self.gateway(["w", ENTER, ESC], view, answer="y\n\n")
        self.assertEqual(acted, [(view, "restart")])
        self.assertIn("[ Restart now ]", _flat(win.frames))
        self.assertIn("No managed session runs now.\nRestart the gateway now? [y/N] ", printed)
        self.assertIn("gateway restart: fixture done", printed)
        self.assertIn("gateway restart: fixture done", win.frames[-1])

    def test_w_installs_the_gateway_service(self) -> None:
        view = fx.GatewayViewStub(service=True)
        acted, _printed, win = self.gateway(["w", ENTER, ESC], view)
        self.assertEqual(acted, [(view, "service")])
        self.assertIn("[ Install service ]", _flat(win.frames))

    def test_w_opens_the_gateway_log(self) -> None:
        view = fx.GatewayViewStub()
        with mock.patch.object(gateway_actions, "log_view",
                               return_value=("gateway log — fixture", ["fixture log line"])) as log:
            acted, _printed, win = self.gateway(["w", ENTER, ESC, ESC], view)
        log.assert_called_once_with(view)
        self.assertEqual(acted, [])
        self.assertTrue(any("fixture log line" in frame for frame in win.frames))

    def test_g_remains_a_silent_gateway_alias(self) -> None:
        screen = self.screen(([], [], []))
        with mock.patch.object(gateway_actions, "dialog", return_value="fixture gateway result") as dialog, \
                mock.patch.object(screen, "_collect", wraps=screen._collect) as collect:
            result, win = self.drive(screen, ["g", ESC])
        self.assertIsNone(result)
        dialog.assert_called_once_with(self.runtime, win, screen.palette, background=screen._draw,
                                       tty_in=screen.tty_in, tty_out=screen.tty_out)
        collect.assert_called_once()
        self.assertIn("fixture gateway result", win.frames[-1])
        self.assertIn("W gateway", win.frames[-1])
        self.assertNotIn("G gateway", win.frames[-1])

    def test_a_stopped_gateway_after_an_update_names_the_way_back(self) -> None:
        line = "if this began after charmer update: charmer update --rollback, then charmer gateway start"
        view = fx.GatewayViewStub(start=True, rollback=line)
        acted, _printed, win = self.gateway(["w", ESC, ESC], view)
        self.assertIn(line, _flat(win.frames))
        self.assertEqual(acted, [])


# ================================================================ goldens


def _golden_screen(runtime, report, **kwargs) -> findings.FindingsScreen:
    return findings.FindingsScreen(runtime, palette=tui.MONO_PALETTE, tty_in=io.StringIO(), tty_out=io.StringIO(),
                                   report=report, **kwargs)


def findings_golden(root: Path) -> str:
    """The list over the mixed report (BLOCKED, Attention by area, advisories closed)."""

    with golden_runtime(root) as runtime:
        report = mixed_report()
        return render(lambda: _golden_screen(runtime, lambda: report))


def _driven(root: Path, keys: "callable", reports: list) -> str:
    """The frame after ``keys(screen)`` and Esc (each report is what doctor
    says at the next read)."""

    from _tui_render import current_size

    with golden_runtime(root) as runtime:
        sequence = iter(reports)
        last = [reports[0]]

        def report():
            last[0] = next(sequence, last[0])
            return last[0]

        screen = _golden_screen(runtime, report)
        width, height = current_size()
        win = FakeWindow([*keys(screen), ESC], height=height, width=width)
        with mock.patch.object(doctor_actions, "_doctor_prune_aliases",
                               side_effect=lambda _runtime, _aliases, out: out.write("pruned charmer-old\n") or 0):
            screen.run(win)
        return "".join(win.line(y) + "\n" for y in range(height))


def _steps_to(screen: findings.FindingsScreen, found) -> list:
    rows = [screen.lines[index] for index in screen.selectable]
    return [DOWN] * next(i for i, item in enumerate(rows) if found(item))


def findings_advisories_golden(root: Path) -> str:
    """The keyboard: Down to the advisories' header, Enter opens them."""

    return _driven(root, lambda screen: [*_steps_to(screen, lambda item: item.kind == "toggle"), ENTER],
                   [mixed_report()])


def findings_refresh_golden(root: Path) -> str:
    """After Enter on the unused alias and Prune: the report read again."""

    problems, info, attention = mixed_report()
    after = (problems, info, [line for line in attention if getattr(line, "action", None) != "unused-alias"])
    return _driven(root, lambda screen: [*_steps_to(screen, lambda item: item.row is not None
                                                     and item.row.action == "unused-alias"),
                                         ENTER, RIGHT, ENTER, ESC],
                   [(problems, info, attention), after])


def gateway_rollback_golden(root: Path) -> str:
    """The gateway dialog of a stopped gateway after a gateway-changing
    update, over Providers (where G opens it): the rollback command under
    the state (rollback stays a command-line action)."""

    from types import SimpleNamespace

    from charmer import cli, endpoint, gateway_lifecycle
    from _tui_render import first_oauth_pool

    with golden_runtime(root) as runtime:
        journal = fx.journal_window(dead_pool=first_oauth_pool(runtime))
        providers = cli._ProvidersScreen(runtime, palette=tui.MONO_PALETTE, journal=lambda: journal)
        base_url = endpoint.gateway_endpoint(runtime.catalog.docs["gateway"]).base_url
        view = gateway_actions.GatewayView(
            SimpleNamespace(base_url=base_url, backend=endpoint.ON_DEMAND),
            SimpleNamespace(state=gateway_lifecycle.STOPPED, detail="the gateway is not running"),
            runtime=runtime, rollback=gateway_lifecycle.ROLLBACK_REMEDY.format(version="2.0.1"))
        from _tui_render import current_size

        width, height = current_size()
        win = FakeWindow([ESC], height=height, width=width)
        providers._draw(win)  # the screen G opens it over
        with mock.patch.object(gateway_actions, "observe", return_value=view):
            gateway_actions.dialog(runtime, win, tui.MONO_PALETTE, background=providers._draw)
        return "".join(win.line(y) + "\n" for y in range(height))


if __name__ == "__main__":
    unittest.main()
