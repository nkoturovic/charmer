"""Settings portability and the gateway plan, driven with offline fixtures."""

from __future__ import annotations

import io
from pathlib import Path
from unittest import mock

from charmer import portability, settings, state, tui
from _layout import REPO_ROOT
from charmer.cli import consent
from charmer.cli.commands import plan as plan_cmd
from charmer.cli.commands import portability as portability_cmd
from charmer.cli.screens import gateway_actions
from charmer.cli.screens.settings import _SettingsScreen
from test_portability import PortabilityCase, inventory
from test_tui import DOWN, END, FakeWindow, RIGHT

ESC = "\x1b"
ENTER = "\n"


class PortabilityScreenTests(PortabilityCase):
    def screen(self):
        return _SettingsScreen(self.runtime, tui.MONO_PALETTE, tty_in=io.StringIO(), tty_out=io.StringIO())

    def drive(self, row, keys, *, width=80, height=24):
        screen = self.screen()
        target = next((i for i, entry in enumerate(screen.entries)
                       if entry[0] == "row" and entry[1].key == row), None)
        self.assertIsNotNone(target, f"Settings has no {row} path")
        steps = sum(screen._selectable(entry) for entry in screen.entries[screen.index:target])
        win = FakeWindow([*[DOWN] * steps, ENTER, *keys, ESC], width=width, height=height)
        screen.run(win)
        return screen, win

    @staticmethod
    def text(win):
        return " ".join(" ".join(win.frames).split())

    def ready_file(self):
        document = portability_cmd.collect_export(self.runtime)
        document["settings"][settings.COMPACTION_PERCENT_KEY] = 71
        return self.bundle_file(document)

    def test_export_path_writes_the_cli_file_and_receipt(self):
        target = self.outside / "portable.json"
        screen, win = self.drive("export", [*str(target), ENTER, RIGHT, ENTER, ESC])
        self.assertTrue(target.is_file(), screen.message)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(portability.load_export(target.read_bytes(), asset_root=self.runtime.asset_root).document,
                         portability_cmd.collect_export(self.runtime))
        self.assertTrue((self.runtime.session_store.root / portability.RECEIPT_NAME).is_file())
        self.assertIn("confirmed export recorded", self.text(win))

    def test_blank_export_keeps_the_cli_stdout_default_without_a_receipt(self):
        before = inventory(self.root)
        _screen, win = self.drive("export", [ENTER, RIGHT, ENTER, ESC])
        self.assertIn(portability.FORMAT, self.text(win))
        self.assertIn("blank: show JSON", self.text(win))
        self.assertEqual(before, inventory(self.root))

    def test_import_preview_displays_the_cli_plan_without_writing(self):
        path = self.ready_file()
        before = inventory(self.root)
        _screen, win = self.drive("import-preview", [*str(path), ENTER, RIGHT, ENTER, END, ESC])
        self.assertIn(portability.PREVIEW_HEADER, self.text(win))
        self.assertIn("Import plan digest:", self.text(win))
        self.assertEqual(before, inventory(self.root))

    def test_import_apply_confirms_then_uses_the_cli_service(self):
        path = self.ready_file()
        with mock.patch.object(consent, "stdio_ttys", return_value=True):
            _screen, win = self.drive("import", [*str(path), ENTER, RIGHT, ENTER,
                                                ESC, RIGHT, ENTER, ESC])
        self.assertEqual(self.runtime.settings_store.load()[settings.COMPACTION_PERCENT_KEY], 71)
        self.assertIn("Apply imported configuration?", self.text(win))
        self.assertIn(portability.APPLIED_HEADER, self.text(win))
        self.assertIn(portability.APPLIED_TRUST, self.text(win))

    def test_import_apply_enter_cancels_by_default(self):
        path = self.ready_file()
        before = inventory(self.root)
        with mock.patch.object(consent, "stdio_ttys", return_value=True):
            _screen, win = self.drive("import", [*str(path), ENTER, RIGHT, ENTER, ESC, ENTER, ESC])
        self.assertEqual(before, inventory(self.root))
        self.assertIn("import: declined", self.text(win))

    def test_import_human_guard_refuses_before_reading_the_file(self):
        with mock.patch.dict(self.runtime.environ, {"CLAUDECODE": ""}), \
                mock.patch.object(portability, "read_bundle", side_effect=AssertionError("read before guard")):
            _screen, win = self.drive("import", [*"missing.json", ENTER, RIGHT, ENTER, ESC])
        self.assertIn("needs a terminal outside Claude Code", self.text(win))

    def test_readonly_export_and_import_apply_do_not_write(self):
        self.runtime.allow_state_writes = False
        target = self.outside / "portable.json"
        _screen, win = self.drive("export", [*str(target), ENTER, RIGHT, ENTER, ESC])
        self.assertFalse(target.exists())
        self.assertIn("read-only", self.text(win))
        with mock.patch.object(portability, "read_bundle", side_effect=AssertionError("read before guard")):
            _screen, win = self.drive("import", [*"missing.json", ENTER, RIGHT, ENTER, ESC])
        self.assertIn("read-only", self.text(win))

    def test_path_cancel_and_invalid_input_change_nothing(self):
        before = inventory(self.root)
        for row in ("export", "import-preview", "import"):
            with self.subTest(row=row):
                self.drive(row, [ESC])
                self.assertEqual(before, inventory(self.root))
        _screen, win = self.drive("import-preview", [*"missing.json", ENTER, RIGHT, ENTER, ESC])
        self.assertIn("nothing written", self.text(win))
        self.assertEqual(before, inventory(self.root))

    def test_import_stale_plan_is_refused_after_confirmation(self):
        path = self.ready_file()
        original = tui.Modal.run

        def change_at_confirmation(modal, *args, **kwargs):
            if modal.title == "Apply imported configuration?":
                self.runtime.settings_store.update(
                    lambda doc: doc.__setitem__(settings.COMPACTION_PERCENT_KEY, 72),
                    catalog=self.runtime.lineup_catalog())
            return original(modal, *args, **kwargs)

        with mock.patch.object(consent, "stdio_ttys", return_value=True), \
                mock.patch.object(tui.Modal, "run", change_at_confirmation):
            _screen, win = self.drive("import", [*str(path), ENTER, RIGHT, ENTER, ESC, RIGHT, ENTER, ESC])
        self.assertEqual(self.runtime.settings_store.load()[settings.COMPACTION_PERCENT_KEY], 72)
        self.assertIn("changed since the preview", self.text(win))
        self.assertNotIn(portability.APPLIED_HEADER, self.text(win))

    def test_keyless_origin_has_its_own_cancel_focused_confirmation(self):
        state.ensure_private_dir(self.pdir)
        state.atomic_write(self.pdir / "lanbox.json",
                           (REPO_ROOT / "tests/fixtures/operator/providers.d/lanbox.json").read_bytes())
        document = portability_cmd.collect_export(self.runtime)
        document["settings"][settings.COMPACTION_PERCENT_KEY] = 71
        self.fresh_target()
        path = self.bundle_file(document)
        with mock.patch.object(consent, "stdio_ttys", return_value=True):
            _screen, win = self.drive("import", [*str(path), ENTER, RIGHT, ENTER,
                                                END, ESC, ENTER, END, ESC, RIGHT, ENTER, ESC])
        self.assertFalse((self.pdir / "lanbox.json").exists())
        self.assertEqual(self.runtime.settings_store.load()[settings.COMPACTION_PERCENT_KEY], 71)
        self.assertIn("Declare keyless provider?", self.text(win))
        self.assertIn("http://box.lan:8010", self.text(win))
        self.assertIn("excluded (declined): lanbox", self.text(win))

    def test_paths_fit_both_terminal_sizes(self):
        for width, height in ((80, 24), (120, 40)):
            with self.subTest(width=width):
                _screen, win = self.drive("import-preview", [ESC], width=width, height=height)
                self.assertIn("Import preview", self.text(win))
                self.assertNotIn("Terminal too small", self.text(win))


class GatewayPlanTests(PortabilityCase):
    def test_p_shows_the_same_readonly_plan_as_cli(self):
        output = io.StringIO()
        from argparse import Namespace
        plan_cmd.command(self.runtime, Namespace(), output)
        before = inventory(self.root)
        win = FakeWindow(["P", ESC])
        shown = []
        with mock.patch.object(tui.TextView, "run", autospec=True,
                               side_effect=lambda view, _win: shown.append(list(view.lines))):
            gateway_actions.dialog(self.runtime, win, tui.MONO_PALETTE)
        self.assertEqual(shown, [output.getvalue().splitlines()], "P did not show the CLI plan")
        self.assertEqual(before, inventory(self.root))
        self.assertIn("P plan", "\n".join(win.frames))

    def test_plan_failure_is_visible_without_a_gateway_action(self):
        win = FakeWindow(["P", ESC])
        with mock.patch.object(plan_cmd, "build", side_effect=plan_cmd.PlanError("fixture plan refused")), \
                mock.patch.object(gateway_actions, "act", side_effect=AssertionError("plan acted")):
            gateway_actions.dialog(self.runtime, win, tui.MONO_PALETTE)
        self.assertIn("fixture plan refused", "\n".join(win.frames))
