"""Models add entry point and setup picker affordances, using private fixtures."""

from __future__ import annotations

import dataclasses
from unittest import mock

import test_cli
from test_screens_get_started import GetStartedCase
from test_tui import FakeWindow
from charmer import tui
from charmer.cli.screens import actions, common, get_started, models

ESC = "\x1b"


class ModelAddTests(test_cli.OperatorCommandCase):
    def screen(self):
        return models._ModelsScreen(self.runtime, palette=tui.MONO_PALETTE)

    def test_a_chooses_a_merged_provider_and_runs_the_existing_flow(self):
        self.declare_small()
        screen = self.screen()
        with mock.patch.object(common, "_run_choice_list", return_value=0) as chooser, \
                mock.patch.object(common.OnboardingActions, "add_models") as add, \
                mock.patch.object(screen, "_load", wraps=screen._load) as refresh:
            screen.run(FakeWindow(["A", ESC], height=40, width=120))
        chooser.assert_called_once()
        add.assert_called_once()
        providers = self.runtime.lineup_catalog().providers
        self.assertIn(add.call_args.args[0], providers)
        self.assertIn("acme", providers)
        labels = chooser.call_args.args[3]
        self.assertEqual(len(labels), len(providers))
        for pid in providers:
            self.assertTrue(any(f"({pid})" in label for label in labels), pid)
        refresh.assert_called_once()
        self.assertEqual(self.http_calls, [])

    def test_cancelling_the_provider_chooser_does_not_add_or_write(self):
        screen = self.screen()
        before = self.state_bytes()
        with mock.patch.object(common, "_run_choice_list", return_value=None) as chooser, \
                mock.patch.object(common.OnboardingActions, "add_models") as add:
            screen.run(FakeWindow(["a", ESC], height=40, width=80))
        chooser.assert_called_once()
        add.assert_not_called()
        self.assertEqual(self.state_bytes(), before)
        self.assertEqual(self.http_calls, [])

    def test_add_is_visible_at_80_columns_on_every_row(self):
        self.declare_small()
        screen = self.screen()
        for index, key in enumerate(screen.keys):
            if key is None:
                continue
            with self.subTest(key=key):
                screen.index = index
                win = FakeWindow(height=24, width=80)
                screen._draw(win)
                self.assertIn("A add model", "\n".join(win.frames[-1].splitlines()[-2:]))

    def test_add_still_works_with_no_model_rows(self):
        empty = dataclasses.replace(self.runtime.lineup_catalog(), lines={}, retired={})
        with mock.patch.object(self.runtime, "lineup_catalog", return_value=empty):
            screen = self.screen()
            self.assertEqual(screen.line_rows, ())
            with mock.patch.object(screen, "_load", wraps=screen._load) as refresh, \
                    mock.patch.object(common, "_run_choice_list", return_value=0) as chooser, \
                    mock.patch.object(common.OnboardingActions, "add_models") as add:
                win = FakeWindow(["A", ESC], height=40, width=80)
                screen.run(win)
        self.assertIn("A add model", "\n".join(win.frames[0].splitlines()[-2:]))
        chooser.assert_called_once()
        add.assert_called_once()
        refresh.assert_called_once()

    def test_help_opens_with_the_generated_model_key_list(self):
        screen = self.screen()
        with mock.patch.object(common, "_help_modal") as help_modal:
            screen.run(FakeWindow(["?", ESC], height=40, width=80))
        self.assertTrue(help_modal.call_args.args[3].startswith(actions.help_text("models")))


class SetupAffordanceTests(GetStartedCase):
    def test_get_started_help_includes_the_generated_offline_client_key(self):
        with mock.patch.object(common, "_help_modal") as help_modal:
            self.run_screen(self.screen(), ["?", ESC])
        self.assertTrue(help_modal.call_args.args[3].startswith(actions.help_text("get-started")))
        self.assertIn("F —", help_modal.call_args.args[3])

    def test_offline_client_bar_key_and_dispatch_only_apply_on_client_step(self):
        screen = self.screen(focus="claude")
        with mock.patch.object(screen, "_offline_claude") as offline:
            _result, win = self.run_screen(screen, ["F", ESC])
        offline.assert_called_once()
        self.assertIn("F offline file", "\n".join(win.frames[0].splitlines()[-2:]))
        screen.selected = next(i for i, step in enumerate(screen.state.steps) if step.id == "providers")
        with mock.patch.object(screen, "_offline_claude") as offline:
            _result, win = self.run_screen(screen, ["F", ESC])
        offline.assert_not_called()
        self.assertNotIn("F offline file", "\n".join(win.frames[0].splitlines()[-2:]))

    def test_picker_help_opens_with_the_generated_key_list(self):
        picker = get_started._ProviderPicker(self.runtime, tui.MONO_PALETTE)
        with mock.patch.object(common, "_help_modal") as help_modal:
            picker.run(FakeWindow(["?", ESC], height=24, width=80))
        self.assertTrue(help_modal.call_args.args[3].startswith(actions.help_text("provider-picker")))

    def test_step_shortcuts_are_visible_with_canonical_arrow_spelling(self):
        view = get_started._StepView("Choose a profile", [], palette=tui.MONO_PALETTE,
                                     buttons=(("Keep", None), ("Open Profiles", "profiles")),
                                     shortcuts={"P": "profiles"})
        win = FakeWindow(["P"], height=24, width=80)
        self.assertEqual(view.run(win), "profiles")
        self.assertIn("P open profiles", win.frames[0])
        self.assertIn("← → choose", win.frames[0])

    def test_both_provider_pickers_count_hidden_entries_not_headings(self):
        from charmer.setup import providers

        for only in (None, "other"):
            source = list(providers.picker_entries(self.runtime, only=only))
            source.extend(dataclasses.replace(source[0], id=f"fixture-{i}", group=f"own:fixture-{i}",
                                              group_title=f"Fixture {i}", label=f"Fixture connection {i}")
                          for i in range(12))
            with mock.patch.object(providers, "picker_entries", return_value=source):
                picker = get_started._ProviderPicker(self.runtime, tui.MONO_PALETTE, only=only)
            entries = [i for i, row in enumerate(picker.rows) if row.kind == "entry"]
            for selected in entries:
                with self.subTest(only=only, selected=selected):
                    picker.selected = selected
                    win = FakeWindow(height=24, width=80)
                    with mock.patch.object(tui, "safe_add", wraps=tui.safe_add) as draw:
                        picker._draw(win)
                    calls = [call.args for call in draw.call_args_list]
                    marker = [call for call in calls if call[3].startswith("↓ ")]
                    rule_y = next(call[1] for call in calls if call[3] == "─")
                    if marker:
                        self.assertEqual(len(marker), 1)
                        marker_y = marker[0][1]
                        self.assertEqual(marker_y, rule_y - 1)
                        last = picker.offset + marker_y - 3
                        hidden = sum(row.kind == "entry" for row in picker.rows[last:])
                        self.assertGreater(hidden, 0)
                        self.assertEqual(marker[0][3], tui.MORE_ROWS.format(n=hidden))
                    else:
                        last = picker.offset + rule_y - 3
                        self.assertFalse(any(row.kind == "entry" for row in picker.rows[last:]))
                    if selected == entries[0]:
                        self.assertTrue(marker, "the first frame must advertise entries below the fold")
                    selection_y = next(call[1] for call in calls if call[3] == "›")
                    self.assertEqual(selection_y, 3 + selected - picker.offset)
                    self.assertLess(selection_y, marker[0][1] if marker else rule_y)
                    self.assertIn("Enter connect", win.frames[-1].splitlines()[-1])
            win = FakeWindow(height=len(picker.rows) + 20, width=80)
            picker._edge(last=False)
            picker._draw(win)
            self.assertNotIn("↓ ", win.frames[-1])
