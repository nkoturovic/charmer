"""Visible keys, whole messages and compact-screen instructions."""
from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from charmer import tui, views
from charmer.cli import text
from charmer.cli.screens import actions, providers, get_started, profiles, settings
import _tui_render as render
from test_tui import FakeWindow, ESC


class ContentTests(unittest.TestCase):
    def test_result_modal_respects_the_parent_screen_floor(self):
        from types import SimpleNamespace

        screen = SimpleNamespace(message="a complete refusal " * 30, palette=tui.MONO_PALETTE,
                                 min_size=lambda width: (20, 60))
        with mock.patch.object(tui.Modal, "run") as modal:
            self.assertFalse(tui.message_modal(FakeWindow([], width=35, height=8), screen))
        modal.assert_not_called()

    def test_close_only_message_modal_can_show_the_last_line(self):
        from test_tui import END

        win = FakeWindow([END, ESC], width=80, height=24)
        tui.Modal("Action result", [f"detail {i}" for i in range(60)], buttons=(("Close", True),)).run(win, tui.MONO_PALETTE)
        self.assertIn("detail 59", "\n".join(win.frames))

    def test_every_non_silent_action_has_a_bar_label_or_help_only_reason(self):
        for screen, rows in actions.ACTIONS.items():
            for row in rows:
                with self.subTest(screen=screen, key=row.key):
                    self.assertTrue(row.silent or row.label or row.help_only, f"{screen} {row.key}: no bar or reason")

    def test_profiles_and_get_started_help_include_all_keys(self):
        for module, screen in ((profiles, "profiles"), (get_started, "get-started"), (settings, "settings")):
            with self.subTest(screen=screen):
                self.assertTrue(module.HELP.startswith(actions.help_text(screen)))
        self.assertIn("W —", profiles.HELP)
        self.assertIn("F —", get_started.HELP)

    def test_editor_and_forms_match_table_help(self):
        for screen, help_text in (("editor", tui.PROFILE_EDITOR_HELP), ("form", tui.PROFILE_FORM_GENERAL_HELP),
                                  ("native-form", tui.PROFILE_FORM_NATIVE_HELP), ("binding-picker", tui.BINDING_PICKER_HELP),
                                  ("named-bindings", tui.NAMED_BINDINGS_HELP), ("routing", tui.ROUTING_HELP)):
            with self.subTest(screen=screen):
                self.assertTrue(help_text.startswith(actions.help_text(screen)))
        self.assertTrue(actions.action("editor.raw-json").help_only)
        self.assertIn("F1", tui.PROFILE_FORM_GENERAL_HELP)

    def test_empty_states_name_their_next_key(self):
        self.assertIn("N creates one", text.PROFILES_NONE)
        self.assertIn("L links a session", text.SESSIONS_EMPTY)

    def test_lineup_last_target_is_visible_at_eighty_columns(self):
        frame = render.build("lineup-live")
        target = next(line for line in frame.splitlines() if line.strip().startswith("to "))
        self.assertIn("fallback", target)

    def test_card_help_names_add_model_new_provider_and_launch_anyway(self):
        from charmer.cli.screens import launch_sessions

        help_text = launch_sessions.CARD_HELP
        self.assertTrue(help_text.startswith(actions.help_text("card")))
        self.assertIn("A add models", actions.action("card.providers").help)
        self.assertIn("N new", actions.action("card.providers").help)
        self.assertIn("L —", help_text)


class FixtureContentTests(unittest.TestCase):
    def setUp(self):
        root = self.enterContext(tempfile.TemporaryDirectory(prefix="key-content-"))
        self.runtime = self.enterContext(render.golden_runtime(Path(root)))

    def test_provider_credential_refusal_keeps_its_remedy_at_eighty_columns(self):
        screen = providers._ProvidersScreen(self.runtime, palette=tui.MONO_PALETTE, journal=lambda: None)
        screen.selected = next(i for i, row in enumerate(screen.rows) if row.id == "anthropic")
        win = FakeWindow(["K", ESC, ESC], height=24, width=80)
        screen.run(win)
        self.assertIn("run it in a separate shell", "\n".join(win.frames))

    def test_provider_picker_shows_rows_below_the_fold(self):
        self.assertRegex(render.build("get-started-picker"), r"↓ \d+ more")
        # provider-kind is the form's protocol step, not the provider list.
        self.assertIn("↑↓ choose", render.build("provider-kind"))

    def test_settings_navigation_and_review_arrows_are_visible(self):
        screen = settings._SettingsScreen(self.runtime, tui.MONO_PALETTE,
                                           card_lineup=render.default_lineup(self.runtime), tty_in=io.StringIO(), tty_out=io.StringIO())
        for i, (kind, row) in enumerate(screen.entries):
            if kind != "row":
                continue
            screen.index = i
            bar = dict(screen.keybar_bindings())
            self.assertIn("G", bar)
            self.assertIn("M", bar)
            if row.key == "review_round_cap":
                self.assertIn("← →", bar)

    def test_empty_sessions_offer_only_working_navigation(self):
        from charmer.cli.screens import launch_sessions

        screen = launch_sessions._SessionsScreen(self.runtime, palette=tui.MONO_PALETTE)
        self.assertEqual({key for key, _label in screen._keybar().bindings}, {"L", "/", "B", "C", "?", "Esc"})
        frame = render.render(lambda: screen, 80, 24)
        self.assertIn("B filters", frame)
        self.assertIn("C all dirs", frame)
        self.assertNotIn("more · Esc", frame)

    def test_root_empty_sessions_explain_that_escape_quits(self):
        from charmer.cli.screens import launch_sessions

        screen = launch_sessions._SessionsScreen(self.runtime, palette=tui.MONO_PALETTE, root=True)
        frame = " ".join(render.render(lambda: screen, 80, 24).split())
        self.assertIn("Esc quits; run charmer to launch one", frame)
        self.assertNotIn("Esc, then Enter launches one", frame)

    def test_not_connected_card_keeps_its_working_primary_keys(self):
        from charmer import catalog

        card = render.card_screen(self.runtime, render.profile_target(self.runtime, catalog.DEFAULT_SEED))
        card.not_connected = True
        card._replan()
        for width in (80, 100, 120):
            with self.subTest(width=width):
                win = FakeWindow([], width=width, height=24)
                card._keybar().draw(win, 23, tui.MONO_PALETTE)
                self.assertIn("E edit", win.text())
                self.assertIn("Tab next", win.text())

    def test_models_add_chooses_a_provider_then_uses_existing_flow(self):
        from charmer.cli.screens import models, common

        screen = models._ModelsScreen(self.runtime, palette=tui.MONO_PALETTE)
        with mock.patch.object(common, "_run_choice_list", return_value=0) as chooser, \
                mock.patch.object(common.OnboardingActions, "add_models") as add:
            screen.run(FakeWindow(["A", ESC], height=40, width=120))
        chooser.assert_called_once()
        add.assert_called_once()
        self.assertIn(add.call_args.args[0], self.runtime.lineup_catalog().providers)
