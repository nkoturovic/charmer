"""Ranked key bars and the selected provider's available actions."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from charmer import tui
from charmer.cli.screens import actions, providers
from charmer.setup import check, model

import _tui_render as render
from _catalog import FIXTURE_ROOT, served_selectors
from test_tui import FakeWindow


class RankedBarTests(unittest.TestCase):
    def draw(self, bindings, width=80):
        win = FakeWindow([], width=width, height=24)
        bar = tui.KeyBar(bindings)
        bar.draw(win, 23, tui.MONO_PALETTE)
        return bar, [win.line(y).strip() for y in range(24) if win.line(y).strip()]

    def test_overflow_names_the_hidden_count(self):
        _, lines = self.draw([(f"K{i}", f"action-{i}") for i in range(12)] + [("?", "help"), ("Esc", "back")])
        self.assertEqual(lines, [" · ".join(f"K{i} action-{i}" for i in range(5)),
                                 "K5 action-5 · K6 action-6 · K7 action-7 · K8 action-8 · ? 3 more · Esc back"])

    def test_rank_beats_position_and_ties_drop_last_first(self):
        bindings = [(f"K{i}", f"action-{i}", rank) for i, rank in enumerate((3, 2, 3, 2, 3, 2, 1, 1, 1, 1, 1, 1))]
        _, lines = self.draw(bindings + [("?", "help", 3), ("Esc", "back", 3)])
        self.assertEqual(lines, ["K1 action-1 · K3 action-3 · K6 action-6 · K7 action-7 · K8 action-8",
                                 "K9 action-9 · K10 action-10 · K11 action-11 · ? 4 more · Esc back"])

    def test_fitting_pairs_and_triples_draw_identically(self):
        pairs = [("Enter", "choose"), ("A", "add"), ("?", "help"), ("Esc", "back")]
        plain, lines = self.draw(pairs)
        ranked, other = self.draw([(*pair, 1) for pair in pairs])
        self.assertEqual(lines, ["Enter choose · A add · ? help · Esc back"])
        self.assertEqual(other, lines)
        self.assertEqual(ranked.text(), plain.text())
        self.assertEqual(ranked.rows(80), 1)

    def test_no_help_keeps_an_ellipsis_and_respects_rank(self):
        _, lines = self.draw([(f"K{i}", f"action-{i}", 3 if i < 6 else 1) for i in range(12)] + [("Esc", "back", 3)])
        text = "\n".join(lines)
        self.assertIn("…", text)
        self.assertIn("Esc back", text)
        for i in range(6, 12):
            self.assertIn(f"K{i} action-{i}", text)
        self.assertLessEqual(len(lines), 2)

    def test_help_and_exit_never_drop_even_when_not_last(self):
        _, lines = self.draw([("?", "help", 3), ("Esc", "back", 3)]
                             + [(f"K{i}", f"action-{i}", 1) for i in range(12)])
        text = "\n".join(lines)
        self.assertRegex(text, r"\? \d+ more")
        self.assertIn("Esc back", text)
        self.assertNotIn("…", text)
        self.assertLessEqual(len(lines), 2)


class ActionBuilderTests(unittest.TestCase):
    def test_table_builders_keep_order_overrides_and_non_bar_reasons(self):
        rows = (actions.Action("sample.add", "A", "add a row", label="add", rank=1),
                actions.Action("sample.raw", "^G", "edit raw", help_only="advanced editor shortcut"),
                actions.Action("sample.alias", "Q", "add a row", silent=True),
                actions.Action("sample.help", "?", "help", label="help", help="? opens this help"))
        with mock.patch.dict(actions.ACTIONS, sample=rows):
            self.assertEqual(actions.bar("sample", {"help", "add", "raw", "alias"},
                                         labels={"add": "new row"}, ranks={"add": 2}),
                             (("A", "new row", 2), ("?", "help", 2)))
            self.assertEqual(actions.help_text("sample", "Notes follow."),
                             "A — add a row\n^G — edit raw\n? opens this help\n\nNotes follow.")
            with self.assertRaises(KeyError):
                actions.bar("sample", {"typo"})


class ProviderKeyTests(unittest.TestCase):
    def setUp(self):
        root = self.enterContext(tempfile.TemporaryDirectory(prefix="provider-keys-"))
        self.runtime = self.enterContext(render.golden_runtime(Path(root)))
        self.screen = providers._ProvidersScreen(self.runtime, palette=tui.MONO_PALETTE, journal=lambda: None)

    def test_add_models_on_every_fixture_row_at_every_width(self):
        for index, row in enumerate(self.screen.rows):
            self.screen.selected = index
            for width in (80, 100, 120):
                with self.subTest(row=row.id, width=width):
                    frame = render.render(lambda: self.screen, width, 40)
                    self.assertIn("A add models", "\n".join(frame.splitlines()[-2:]))

    def test_test_key_uses_the_same_plan_as_the_handler(self):
        for identity in (("fixture", frozenset(served_selectors(FIXTURE_ROOT)), ""),
                         (None, frozenset(), "gateway down"),
                         (None, frozenset(served_selectors(FIXTURE_ROOT)), "render changed")):
            with self.subTest(identity=identity), mock.patch.object(check, "render_identity", return_value=identity):
                try:
                    expected = {target.provider_id for target in check.plan_test(self.runtime, [r.id for r in self.screen.rows]).targets}
                except model.Refused:
                    expected = set()
                self.screen._load_facts()
                offered = set()
                for index, row in enumerate(self.screen.rows):
                    self.screen.selected = index
                    if "T" in dict(self.screen._keybar_bindings()):
                        offered.add(row.id)
                self.assertEqual(offered, expected)

    def test_provider_help_opens_with_every_non_silent_key(self):
        from charmer.cli.screens import common
        captured = []
        with mock.patch.object(common, "_help_modal", side_effect=lambda _w, _p, _t, text, _d: captured.append(text)):
            self.screen.run(FakeWindow(["?", "\x1b"], height=40, width=120))
        expected = actions.help_text("providers", "").splitlines()
        self.assertEqual(captured[0].splitlines()[:len(expected)], expected)
        self.assertTrue(any(line.startswith("A") for line in expected))
        self.assertFalse(any(line.startswith("Q") for line in expected))

    def test_row_variants_reach_all_bar_keys_and_keep_primary_keys(self):
        original = self.screen.facts
        account = next(f for f in original if f["id"] == "anthropic")
        keyed = next(f for f in original if f["id"] == "deepseek")
        variants = [account, dict(account, kind="direct-anthropic", auth="bearer", credential="EXAMPLE_KEY present"),
                    keyed, dict(keyed, credential="EXAMPLE_KEY missing"),
                    dict(keyed, source="operator", route="unapproved"),
                    dict(keyed, source="operator", route="changed", credential="EXAMPLE_KEY missing"),
                    dict(keyed, source="operator", kind="openai-compat", auth="none", route="unapproved"),
                    dict(keyed, kind="openai-compat", auth="none"), None]
        reached = set()
        for fact in variants:
            for down in (False, True):
                for apply in (False, True):
                    self.screen.facts = [fact] if fact is not None else []
                    self.screen.selected = 0
                    self.screen.gateway_down, self.screen.apply_offer = down, apply
                    self.screen.testable = {f["id"] for f in original}
                    bar = self.screen._keybar()
                    reached.update(key for key, _ in bar.bindings)
                    for width in (80, 100, 120):
                        with self.subTest(fact=fact, down=down, apply=apply, width=width):
                            win = FakeWindow([], width=width, height=24)
                            bar.draw(win, 23, tui.MONO_PALETTE)
                            text = win.text()
                            for (key, label), rank in zip(bar.bindings, bar.ranks):
                                if rank == 1:
                                    self.assertIn(f"{key} {label}", text)
                            hidden = sum(f"{key} {label}" not in text for key, label in bar.bindings if key != "?")
                            self.assertIn(f"? {hidden} more" if hidden else "? help", text)
                            self.assertNotIn("…", text)
        self.assertEqual(reached, {row.key for row in actions.ACTIONS["providers"] if not row.silent and not row.help_only})
