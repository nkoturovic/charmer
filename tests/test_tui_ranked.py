"""Bounded presentation regressions, using pure rows and offline screens."""

from __future__ import annotations

import dataclasses
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _tui_fixture as fx
from charmer import catalog, profile, settings, tui, views
from charmer.cli.screens.common import OnboardingActions
from charmer.cli.screens.settings import _SettingsScreen
from test_screens_findings import _FindingsCase, mixed_report
from test_tui import FakeWindow


class RankedPresentationTests(unittest.TestCase):
    def test_text_wrap_hangs_below_each_original_indent(self):
        for width in (40, 80, 120):
            for indent in (0, 2, 6):
                with self.subTest(width=width, indent=indent):
                    text = " " * indent + "word " * 40
                    lines = tui.TextView("Details", [text])._wrapped(width)
                    self.assertGreater(len(lines), 1)
                    self.assertTrue(all(line.startswith(" " * (indent + 2)) for line in lines[1:]))
                    self.assertTrue(all(len(line) <= width - 4 for line in lines))
                    self.assertEqual(" ".join(" ".join(lines).split()), text.strip())

    def test_confirmation_shows_one_cancel_and_enter_declines(self):
        view = tui.TextView("Apply?", ["A reviewed change"], confirm=True)
        win = FakeWindow(["\n", "y"])
        self.assertFalse(view.run(win))
        self.assertEqual(win.frames[0].count("cancel"), 1)
        self.assertIn("Esc cancel", win.frames[0])
        self.assertFalse(view.run(FakeWindow(["n"])))
        self.assertTrue(view.run(FakeWindow(["y"])))

    def test_onboarding_confirmation_uses_the_question_as_title(self):
        win = FakeWindow(["\x1b"])
        action = OnboardingActions(None, win, tui.MONO_PALETTE)
        self.assertFalse(action.confirm("Ready change\n\nApprove this route? [y/N] "))
        frame = win.frames[0]
        self.assertIn("Approve this route?", frame)
        self.assertEqual(frame.count("Approve this route?"), 1)
        self.assertNotIn("[y/N]", frame)
        self.assertNotIn("Confirm explicit action", frame)

    def test_confirmation_keeps_the_explanation_after_its_question(self):
        win = FakeWindow(["\x1b"])
        action = OnboardingActions(None, win, tui.MONO_PALETTE)
        action.confirm("Checklist passed\nAdmit fixture? No requests are sent. [y/N] ")
        frame = win.frames[0]
        self.assertEqual(frame.count("Admit fixture?"), 1)
        self.assertIn("No requests are sent.", frame)
        self.assertNotIn("[y/N]", frame)

    def test_long_confirmation_question_stays_scrollable(self):
        title = "Apply " + "reviewed configuration " * 6 + "now?"
        win = FakeWindow(["\x1b"])
        OnboardingActions(None, win, tui.MONO_PALETTE).confirm(title + " [y/N] ")
        body = " ".join(" ".join(win.frames[0].splitlines()[3:]).split())
        self.assertIn(title, body)
        self.assertNotIn("[y/N]", body)

    def test_focused_picker_warning_is_beside_effort_and_wraps(self):
        note = "Attention: provider bound below the 200K client class; no per-agent window"
        item = views.PickerItem("line", "fixture", "fixture " + "wide " * 20, True,
                                efforts=("low", "high"), initial_effort="low", note=note)
        picker = tui.BindingPicker(views.PickerModel("Choose model", (item,), 0))
        for width in (80, 120):
            win = FakeWindow(["\x1b"], width=width, height=24)
            picker.run(win)
            frame = win.frames[0]
            detail = " ".join(frame[frame.index("← → effort:"):].split())
            self.assertIn(note, detail)
            self.assertIn("[low]", detail)
            self.assertNotIn("V details", picker._effort_line())

    def test_model_tables_use_short_names_and_details_keep_full_names(self):
        lcat = profile.LineupCatalog.from_catalog(catalog.load_catalog(fx.FIXTURE_ROOT))
        row = views.line_rows(lcat, settings.effective({}, provider_ids=lcat.providers,
                                                     line_keys=lcat.lines), custom_ids=frozenset())[0]
        rows = [dataclasses.replace(row, key=f"fixture-{n}", source=source, status=status, admitted=False,
                                    display="Short · provider and class details", short="Short")
                for n, (source, status) in enumerate((("catalog", "active"), ("custom", "active"),
                                                     ("catalog", "new")))]
        model = views.models_model(rows, used={}, retired={}, continuity_count=0, catalog_version=1, radar={})
        self.assertEqual([cells[1] for cells in (*model.rows, *model.new_rows)], ["Short"] * 3)
        for details in model.details.values():
            self.assertIn("Short · provider and class details", details[0])

    def test_picker_warning_does_not_repeat_the_details_key(self):
        lcat = profile.LineupCatalog.from_catalog(catalog.load_catalog(fx.FIXTURE_ROOT))
        eff = settings.effective({}, provider_ids=lcat.providers, line_keys=lcat.lines)
        rows = views.line_rows(lcat, eff, custom_ids=frozenset())
        rows = [dataclasses.replace(row, provider_tokens=100000) for row in rows]
        model = views.picker_rows(rows, slot="binding", bindings={}, lcat=lcat, eff=eff, current=None)
        warnings = [item for item in model.items if item.selectable and item.note.startswith("Attention:")]
        self.assertTrue(warnings)
        self.assertTrue(all(" — V details" not in item.note for item in warnings))

    def test_settings_buttons_put_cancel_before_the_action(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = fx.fixture_runtime(Path(directory) / "root")
            with fx.hermetic(runtime):
                screen = _SettingsScreen(runtime, tui.MONO_PALETTE, tty_in=io.StringIO(), tty_out=io.StringIO())
                seen = []
                with mock.patch.object(tui.Modal, "run", autospec=True,
                                       side_effect=lambda modal, *a, **kw: seen.append(modal.buttons) or False):
                    screen._edit_int(FakeWindow([]), screen._row(settings.COMPACTION_PERCENT_KEY))
                    screen._reset(FakeWindow([]), screen._row(settings.COMPACTION_PERCENT_KEY))
                self.assertEqual([buttons[0] for buttons in seen], [("Cancel", False)] * 2)

    def test_profiles_separator_spans_the_content_width(self):
        import _tui_render
        frame = _tui_render.build("profiles")
        rules = [line for line in frame.splitlines() if "─" in line]
        self.assertEqual(len(rules), 2)
        self.assertTrue(all(line.startswith("  " + views.rule(80)) for line in rules))

    def test_input_modal_keeps_its_explicit_nondestructive_default(self):
        modal = tui.Modal("Edit number", buttons=(("Cancel", False), ("Save", True)),
                          input=tui.TextInput("71"), default=True)
        self.assertTrue(modal.run(FakeWindow(["\n", "\n"]), tui.MONO_PALETTE))


class FindingsMoreTests(_FindingsCase):
    def test_more_marker_counts_hidden_wrapped_rows(self):
        report = mixed_report()
        screen = self.screen(report)
        win = FakeWindow(["\x1b"], height=24, width=80)
        screen.run(win)
        markers = [line.strip() for line in win.frames[0].splitlines() if "↓" in line]
        self.assertTrue(markers)
        self.assertRegex(markers[0], r"^↓ [1-9][0-9]* more")
