"""Propagation screen actions on real compiled fixture sessions."""
from __future__ import annotations

import contextlib
from unittest import mock

from charmer import catalog, lineup, tui
from charmer.cli.screens import profile_editor, propagation
from charmer.cli.screens import common, actions
from test_propagation import PropagationCase
from test_tui import FakeWindow


class PropagationScreenChangesTests(PropagationCase):
    def screen(self):
        return propagation._PropagationScreen(self.runtime, tui.MONO_PALETTE, ["mine"])

    def change(self):
        self.edit("mine", lambda d: d["agents"].update(
            {"cm-implementer": {"model": "opus55", "effort": "xhigh"}}))

    def test_enter_is_the_table_driven_primary_and_a_is_a_silent_alias(self):
        self.follower()
        screen = self.screen()
        self.assertIn(("Enter", "apply to running", 1), screen.keybar())
        table = actions.ACTIONS["propagation"]
        self.assertTrue(any(row.key == "A" and row.silent for row in table))
        self.assertIn("Enter", propagation.HELP)
        self.assertIn("A", propagation.HELP)

    def test_eligible_pending_follower_preview_does_not_say_not_applied(self):
        record = self.follower()
        self.mutate(record["managed_id"], pending={
            "requested_at": "2026-09-25T00:00:00Z", "kind": "profile", "profile": "mine",
            "follow": True, "document": None, "reasons": ["native agents: plan native → off"]})
        screen = self.screen()
        self.assertTrue(screen.offers_apply)
        win = FakeWindow([], height=40, width=120)
        screen._draw(win)
        self.assertNotIn("has a pending relaunch change; not applied", win.text())
        self.assertIn("apply now", win.text())

    def test_enter_applies_by_default(self):
        record = self.follower()
        self.change()
        with mock.patch.object(common._TextReportScreen, "run"):
            result = self.screen().run(FakeWindow(["\n", "\x1b"], height=40, width=120))
        self.assertEqual(result, "applied")
        self.assertEqual(self.store.load(record["managed_id"])["lineup_generation"], 2)

    def test_dismissals_run_the_later_producer_even_below_the_floor(self):
        self.follower()
        self.change()
        for key, height in (("\x1b", 40), ("\x03", 40), ("\x1b", 2)):
            with self.subTest(key=key, height=height):
                with mock.patch.object(lineup, "on_saved", return_value=0) as saved, \
                        mock.patch.object(common._TextReportScreen, "run") as report:
                    result = self.screen().run(FakeWindow([key], height=height, width=120))
                self.assertEqual(result, "later")
                saved.assert_called_once()
                self.assertFalse(saved.call_args.kwargs["apply_live"])
                self.assertEqual(saved.call_args.args[1], ["mine"])
                report.assert_not_called()

    def test_interrupt_while_asking_runs_later_before_propagating_interrupt(self):
        self.follower()
        with mock.patch.object(tui, "read_key", side_effect=KeyboardInterrupt), \
                mock.patch.object(lineup, "on_saved", return_value=0) as saved:
            with self.assertRaises(KeyboardInterrupt):
                self.screen().run(FakeWindow([], height=40, width=120))
        saved.assert_called_once()
        self.assertFalse(saved.call_args.kwargs["apply_live"])

    def test_only_pinned_sessions_still_get_a_report_and_count(self):
        record = self.follower()
        self.mutate(record["managed_id"], follow=False)
        reports = []
        with mock.patch.object(common._TextReportScreen, "run", lambda screen, _win: reports.extend(screen.lines)):
            result = propagation.run_propagation_screen(self.runtime, FakeWindow([]), tui.MONO_PALETTE, ["mine"])
        self.assertEqual(result, "later")
        text = "\n".join(reports)
        self.assertIn("no session follows it · 1 pinned session(s)", text)
        self.assertIn("pinned (follow off) — not changed", text)
        self.assertIn("Sessions → F", text)

    def test_later_report_contains_shared_profile_status(self):
        self.follower()
        self.change()
        reports = []
        with mock.patch.object(common._TextReportScreen, "run", lambda screen, _win: reports.extend(screen.lines)):
            result = self.screen().run(FakeWindow(["l"], height=40, width=120))
        self.assertEqual(result, "later")
        self.assertIn("profile mine changed since this session applied it:", "\n".join(reports))

    def test_editor_saves_again_to_its_only_already_split_follower(self):
        record = self.follower()

        def save(lead, agent):
            editor = profile_editor._profile_editor_state(self.runtime, "mine", "edit")
            if lead:
                editor.set_binding(catalog.LEAD_ROLE, "grok46", "high")
            editor.set_binding("cm-implementer", *agent)
            outcome = tui.ProfileEditorOutcome("save", "mine", editor.document, expected=editor.loaded_digest)
            callbacks = profile_editor._profile_editor_callbacks(self.runtime, tui.MONO_PALETTE, editor_state=editor)
            with mock.patch.object(common._TextReportScreen, "run"):
                ok, text = callbacks.save(FakeWindow(["\n", "\x1b"], height=40, width=120), outcome)
            self.assertTrue(ok, text)

        save(True, ("opus55", "xhigh"))
        first = self.store.load(record["managed_id"])
        self.assertEqual(first["lineup_generation"], 2)
        self.assertEqual(first["pending"]["profile"], "mine")
        self.assertTrue(self.screen().offers_apply)
        save(False, ("sol", "high"))
        second = self.store.load(record["managed_id"])
        self.assertEqual(second["lineup_generation"], 3)
        self.assertEqual(second["applied"]["agents"]["cm-implementer"]["key"], "sol")
        self.assertEqual(second["pending"]["profile"], "mine")

    def test_repaired_profile_file_gets_the_same_later_producer(self):
        from charmer.cli.commands import profile as commands
        from charmer.cli.screens import profiles

        self.follower()
        self.runtime.environ["EDITOR"] = "fixture-editor"

        def edit(_runtime, _document, **kwargs):
            self.change()
            kwargs["output_stream"].write("Saved profile 'mine'.\n")
            return True

        with mock.patch.object(commands, "_edit_profile", side_effect=edit), \
                mock.patch.object(tui, "suspended_curses", return_value=contextlib.nullcontext()), \
                mock.patch.object(lineup, "on_saved", wraps=lineup.on_saved) as saved:
            screen = profiles._ProfilesScreen(self.runtime, tui.MONO_PALETTE, selected="mine")
            screen._raw_edit(FakeWindow(["\x1b"], height=40, width=120), "mine")
        saved.assert_called_once()
        self.assertFalse(saved.call_args.kwargs["apply_live"])

    def test_editor_honors_explicit_apply_and_later_without_a_question(self):
        record = self.follower()
        for apply_live, binding in ((True, ("opus55", "xhigh")), (False, ("sol", "high"))):
            with self.subTest(apply_live=apply_live):
                editor = profile_editor._profile_editor_state(self.runtime, "mine", "edit")
                editor.set_binding("cm-implementer", *binding)
                outcome = tui.ProfileEditorOutcome("save", "mine", editor.document, expected=editor.loaded_digest)
                callbacks = profile_editor._profile_editor_callbacks(
                    self.runtime, tui.MONO_PALETTE, editor_state=editor, apply_live=apply_live)
                win = FakeWindow(["\x1b"], height=40, width=120)
                ok, text = callbacks.save(win, outcome)
                self.assertTrue(ok, text)
                self.assertTrue(any("propagation — mine" in frame for frame in win.frames))
                self.assertFalse(any("Enter apply to running" in frame for frame in win.frames))
                self.assertEqual(self.store.load(record["managed_id"])["lineup_generation"], 2)

    def test_editor_no_follower_message_keeps_the_pinned_count(self):
        editor = profile_editor._profile_editor_state(self.runtime, "mine", "edit")
        outcome = tui.ProfileEditorOutcome("save", "mine", editor.document, expected=editor.loaded_digest)
        callbacks = profile_editor._profile_editor_callbacks(self.runtime, tui.MONO_PALETTE, editor_state=editor)
        ok, text = callbacks.save(FakeWindow([]), outcome)
        self.assertTrue(ok)
        self.assertIn("no session follows it · 0 pinned session(s)", text)
