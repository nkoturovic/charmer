"""Clipped lists name the unseen rows without covering the selection."""

from __future__ import annotations

import io
import unittest

from charmer import tui
from charmer.cli.screens import models, settings
from test_screens_catalog import _ScreenCase
from test_tui import DOWN, END, HOME, FakeWindow


class TextMoreTests(unittest.TestCase):
    def test_text_marker_counts_wrapped_rows_and_keeps_the_footer_clear(self) -> None:
        view = tui.TextView("Details", [f"row {i}: " + "word " * 12 for i in range(30)])
        for width in (40, 80, 120):
            with self.subTest(width=width):
                view.offset = 0
                win = FakeWindow(height=24, width=width)
                view._draw(win)
                marker = next((y for y in range(24) if win.line(y).strip().startswith("↓ ")), None)
                self.assertIsNotNone(marker)
                visible = marker - 3
                self.assertEqual(win.line(marker).strip(), f"↓ {len(view._wrapped(width)) - visible} more")
                self.assertEqual(win.attr_at(marker, 2), view.palette.attr("dim"))
                self.assertNotIn("Home/End", win.text())
                self.assertIn("↑↓ scroll", win.text())
                self.assertIn("PgUp/PgDn page", win.text())
                self.assertIn("Esc back", win.line(23))

    def test_text_fitting_exactly_has_no_marker(self) -> None:
        for size in (0, 19, 20):
            with self.subTest(size=size):
                view = tui.TextView("Details", [f"row {i}" for i in range(size)])
                win = FakeWindow(height=24, width=80)
                view._draw(win)
                self.assertNotIn("\n  ↓ ", win.text())
                if size:
                    self.assertIn(f"row {size - 1}", win.text())

    def test_narrow_text_paging_does_not_skip_a_row(self) -> None:
        lines = [f"fixture row {i:02d}" for i in range(45)]
        view = tui.TextView("Details", lines)
        win = FakeWindow([tui.curses.KEY_NPAGE, tui.curses.KEY_NPAGE, "\x1b"], height=24, width=40)
        view.run(win)
        for line in lines:
            self.assertTrue(any(line in frame for frame in win.frames), line)
        self.assertNotIn("\n  ↓ ", win.frames[-1])

    def test_text_scroll_reaches_every_row_and_home_end_still_work(self) -> None:
        lines = [f"fixture row {i:02d}" for i in range(35)]
        view = tui.TextView("Details", lines)
        win = FakeWindow([*[DOWN] * len(lines), HOME, END, "\x1b"], height=24, width=80)
        view.run(win)
        for line in lines:
            self.assertTrue(any(line in frame for frame in win.frames), line)
        self.assertNotIn("\n  ↓ ", win.frames[-1])
        self.assertIn(lines[-1], win.frames[-1])
        self.assertIn("\n  ↓ ", win.frames[-2])


class ListMoreTests(_ScreenCase):
    def assert_more(self, screen, *, top, remaining, selected):
        win = FakeWindow(height=24, width=80)
        screen._draw(win)
        marker = next((y for y in range(24) if win.line(y).strip().startswith("↓ ")), None)
        self.assertIsNotNone(marker)
        visible = marker - top
        self.assertEqual(win.line(marker).strip(), f"↓ {remaining(screen.offset + visible)} more")
        self.assertEqual(win.attr_at(marker, 2), screen.palette.attr("dim"))
        self.assertLess(top + screen.index - screen.offset, marker)
        self.assertTrue(selected(win, top + screen.index - screen.offset))

    def test_models_marker_counts_hidden_rows_and_never_covers_selection(self) -> None:
        screen = models._ModelsScreen(self.runtime, palette=tui.MONO_PALETTE)
        screen.index = screen._first_index()
        self.assert_more(screen, top=4, remaining=lambda start: sum(k is not None for k in screen.keys[start:]),
                         selected=lambda win, y: win.line(y).startswith("  ›"))
        for index, key in enumerate(screen.keys):
            if key is None:
                continue
            screen.index = index
            win = FakeWindow(height=24, width=80)
            screen._draw(win)
            row = win.line(4 + index - screen.offset)
            if key == "__candidates__":
                self.assertIn("candidates", row)
            else:
                self.assertTrue(row.startswith("  ›"), key)
        self.assertNotIn("\n  ↓ ", win.text())
        screen.index, screen.offset = screen._first_index(), 0
        wide = FakeWindow(height=60, width=120)
        screen._draw(wide)
        self.assertNotIn("\n  ↓ ", wide.text())

    def test_settings_marker_counts_hidden_rows_and_never_covers_selection(self) -> None:
        screen = settings._SettingsScreen(self.runtime, palette=tui.MONO_PALETTE,
                                          tty_in=io.StringIO(), tty_out=io.StringIO())
        focused = lambda win, y: bool(win.attr_at(y, 2) & tui.curses.A_REVERSE)
        self.assert_more(screen, top=3, remaining=lambda start: sum(
            kind != "section" for kind, _value in screen.entries[start:]), selected=focused)
        for index, (kind, _value) in enumerate(screen.entries):
            if kind == "section":
                continue
            screen.index = index
            win = FakeWindow(height=24, width=80)
            screen._draw(win)
            self.assertTrue(focused(win, 3 + index - screen.offset), index)
        self.assertNotIn("\n  ↓ ", win.text())
        wide = FakeWindow(height=80, width=120)
        screen._draw(wide)
        self.assertNotIn("\n  ↓ ", wide.text())
