"""Action rows against actual help and rendered, state-specific screen bars."""
from __future__ import annotations

import dataclasses
import re
import tempfile
import unittest
from pathlib import Path

from charmer import tui
from charmer.cli.screens import actions

import _key_fixtures as fixtures
import _tui_render as render


class ProviderCensusTests(unittest.TestCase):
    def test_providers_handlers_have_table_rows(self) -> None:
        import _key_census as census

        self.assertEqual(census.unmapped("providers", census.screen_keys("providers"), actions.ACTIONS["providers"]), [])

    def test_census_reports_a_synthetic_unmapped_key(self) -> None:
        import _key_census as census

        source = '''class Screen:
    def run(self, win):
        key = read_key(win)
        if key.ch.upper() == "Z":
            unknown()
'''
        self.assertEqual(census.unmapped("providers", census.handled_keys(source, "Screen"), actions.ACTIONS["providers"]),
                         ["providers handles Z: not in its bar candidates nor its help"])


class ActionTableTests(unittest.TestCase):
    def test_ids_are_stable_names(self) -> None:
        seen: set[str] = set()
        for screen, rows in actions.ACTIONS.items():
            keys = [row.key for row in rows]
            self.assertEqual(len(keys), len(set(keys)), screen)
            for row in rows:
                self.assertRegex(row.id, rf"^{re.escape(screen)}\.[a-z][a-z-]*$")
                self.assertNotIn(row.id, seen)
                seen.add(row.id)
                self.assertTrue(row.does)
                if row.command is not None:
                    self.assertTrue(row.command.startswith("charmer"), row.id)
                self.assertIn(row.rank, (1, 2, 3), row.id)
                if row.help_only:
                    self.assertTrue(row.help_only.strip(), row.id)
                    self.assertNotEqual(row.rank, 1, row.id)
                if not row.silent and not row.help_only:
                    self.assertTrue(row.label or row.key == "Esc", row.id)
        self.assertEqual(actions.action("card.doctor").key, "H")
        for missing in ("card.nothing", "nowhere.quit"):
            with self.assertRaises(KeyError):
                actions.action(missing)

    def test_arrow_keys_are_named_one_way(self) -> None:
        for screen, rows in actions.ACTIONS.items():
            with self.subTest(screen=screen):
                self.assertFalse({"Left/Right", "←→"} & {row.key for row in rows})

    def test_findings_gateway_description_includes_the_plan(self) -> None:
        self.assertEqual(actions.action("findings.gateway").does,
                         "the gateway's actions: start, restart, service install, log, plan")

    def test_each_non_silent_action_is_in_actual_help(self):
        helps = fixtures.screen_helps()
        self.assertEqual(set(helps), set(actions.ACTIONS))
        for screen, text in helps.items():
            with self.subTest(screen=screen):
                rows = actions.ACTIONS[screen]
                self.assertEqual(fixtures.help_errors(screen, rows, text), [])
                expected = [row.help for row in rows if not row.silent]
                lines = text.splitlines()
                if screen in ("card", "resume-card"):
                    # The card's established discovery hint sits immediately
                    # after ?, inside the opening key list.
                    self.assertEqual(lines.count(tui.CHEATSHEET_HINT), 1)
                    lines = [line for line in lines if line != tui.CHEATSHEET_HINT]
                self.assertEqual(lines[:len(expected)], expected,
                                 f"{screen}: actual help must begin with the complete non-silent key list")

    def test_editor_constants_match_the_table_for_actual_variants(self):
        variants = (
            ("editor", "profile", tui.PROFILE_EDITOR_KEYBAR, None),
            ("binding-picker", "model", tui.BINDING_PICKER_KEYBAR, None),
            ("native-form", "choices", tui.PROFILE_FORM_NATIVE_KEYBAR, None),
            ("named-bindings", "list", tui.NAMED_BINDINGS_KEYBAR, None),
            ("routing", "profile", tui.ROUTING_KEYBAR, None),
            ("form", "choice", tui.PROFILE_FORM_GENERAL_KEYBAR, {"change", "apply", "help", "cancel"}),
            ("form", "text", tui.PROFILE_FORM_GENERAL_TEXT_KEYBAR, {"change", "apply", "text-help", "cancel"}),
            ("onboarding-form", "choice", tuple(b for b in tui.ONBOARDING_KEYBAR if b[0] != "F1"),
             {"next", "previous", "choose", "help", "cancel"}),
            ("onboarding-form", "text", tuple(b for b in tui.ONBOARDING_KEYBAR if b[0] not in ("?", "↑↓")),
             {"next", "previous", "text-help", "cancel"}),
        )
        for screen, state, actual, ids in variants:
            with self.subTest(screen=screen, state=state):
                self.assertEqual(actual, actions.bar(screen, ids))


class RenderedCensusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The screen observations all use injected fixture services. Draws are
        # in-memory, and no key that writes or contacts a provider is pressed.
        with tempfile.TemporaryDirectory(prefix="key-census-") as root:
            with render.golden_runtime(Path(root)) as runtime:
                cls.frames = [fixtures.draw(case, width) for case in fixtures.cases(runtime) for width in fixtures.WIDTHS]

    def test_every_bar_key_has_an_action_and_every_action_is_drawn(self):
        screens = {frame.screen for frame in self.frames}
        self.assertEqual(screens, set(actions.ACTIONS), "each screen needs enumerated actual fixture states")
        for frame in self.frames:
            with self.subTest(screen=frame.screen, state=frame.state, width=frame.width):
                allowed = {row.key for row in actions.ACTIONS[frame.screen] if not row.silent and not row.help_only}
                offered = {key for key, _label, _rank in frame.bindings}
                self.assertEqual(offered - allowed - fixtures.BAR_NOTES.get(frame.screen, set()), set())
                self.assertFalse({"Left/Right", "←→"} & offered)
        for screen, rows in actions.ACTIONS.items():
            with self.subTest(screen=screen):
                frames = [frame for frame in self.frames if frame.screen == screen]
                self.assertEqual(fixtures.reachability_errors(screen, rows, frames), [])

    def test_each_applicable_action_is_drawn_in_its_own_state(self):
        for screen in actions.ACTIONS:
            frames = [frame for frame in self.frames if frame.screen == screen]
            self.assertTrue(all(frame.active is not None for frame in frames), screen)
            self.assertEqual(fixtures.availability_errors(screen, actions.ACTIONS[screen], frames), [])
        providers = [frame for frame in self.frames if frame.screen == "providers"]
        self.assertEqual(set().union(*(frame.enter_duplicates for frame in providers)), {"V", "K", "L"})
        self.assertTrue(any("signed-in" in frame.state and "X" in frame.drawn for frame in providers))

    def test_no_applicable_rank_one_is_hidden_at_supported_widths(self):
        for frame in self.frames:
            with self.subTest(screen=frame.screen, state=frame.state, width=frame.width):
                self.assertEqual(fixtures.frame_errors(frame, actions.ACTIONS[frame.screen]), [])


class FixtureCensusSelfTests(unittest.TestCase):
    def setUp(self):
        self.rows = (actions.Action("sample.add", "A", "add a row", label="add", rank=1),
                     actions.Action("sample.help", "?", "help", label="help"),
                     actions.Action("sample.alias", "Q", "add a row", silent=True),
                     actions.Action("sample.advanced", "^G", "advanced editor", help_only="Use the ordinary editor first."))
        self.case = fixtures.Case("sample", "available", tui.KeyBar((("A", "add", 1), ("?", "help", 2))))

    def test_removing_an_available_bar_key_fails_by_screen_and_key(self):
        frame = fixtures.draw(self.case, 80)
        self.assertEqual(fixtures.reachability_errors("sample", self.rows, [frame]), [])
        missing = fixtures.draw(dataclasses.replace(self.case, bar=tui.KeyBar((("?", "help", 2),))), 80)
        self.assertEqual(fixtures.reachability_errors("sample", self.rows, [missing]),
                         ["sample A: no fixture draws this bar candidate"])

    def test_another_state_cannot_hide_a_missing_applicable_action(self):
        available = fixtures.draw(self.case, 80)
        missing = fixtures.draw(dataclasses.replace(self.case, state="missing", bar=tui.KeyBar((("?", "help"),)),
                                                    active=frozenset({"A", "?"})), 80)
        self.assertEqual(fixtures.reachability_errors("sample", self.rows, [available, missing]), [])
        self.assertEqual(fixtures.availability_errors("sample", self.rows, [available, missing]),
                         ["sample A (missing): handled action is neither drawn nor an Enter duplicate"])

    def test_only_an_exact_enter_duplicate_is_exempt(self):
        rows = (actions.Action("sample.details", "Enter", "details", label="details", rank=1),
                actions.Action("sample.details-key", "V", "details", label="details"),
                actions.Action("sample.sign-out", "X", "sign out", label="sign out"))
        case = fixtures.Case("sample", "details", tui.KeyBar((("Enter", "details", 1),)),
                             frozenset({"Enter", "V"}), frozenset({"V"}))
        frame = fixtures.draw(case, 80)
        self.assertEqual(fixtures.availability_errors("sample", rows, [frame]), [])
        distinct = dataclasses.replace(frame, active=frame.active | {"X"})
        self.assertEqual(fixtures.availability_errors("sample", rows, [distinct]),
                         ["sample X (details): handled action is neither drawn nor an Enter duplicate"])
        no_primary = dataclasses.replace(frame, parts=())
        self.assertIn("sample Enter (details): duplicate bindings have no drawn primary action",
                      fixtures.availability_errors("sample", rows, [no_primary]))

    def test_candidate_membership_is_not_evidence_of_drawing(self):
        frame = fixtures.draw(self.case, 80)
        hidden = dataclasses.replace(frame, parts=("? help",))  # candidates still include A
        self.assertEqual(fixtures.reachability_errors("sample", self.rows, [hidden]),
                         ["sample A: no fixture draws this bar candidate"])
        self.assertEqual(fixtures.frame_errors(hidden, self.rows),
                         ["sample A (available, 80 columns): applicable rank-1 key is hidden"])
        self.assertEqual(fixtures.frame_errors(frame, self.rows), [])

    def test_real_overflow_is_not_counted_as_reachability(self):
        bindings = (("A", "a label longer than both bar rows " * 12, 1), ("?", "help", 2))
        frames = [fixtures.draw(dataclasses.replace(self.case, bar=tui.KeyBar(bindings)), width)
                  for width in fixtures.WIDTHS]
        self.assertEqual(fixtures.reachability_errors("sample", self.rows, frames),
                         ["sample A: no fixture draws this bar candidate"])
        for frame in frames:
            self.assertEqual(fixtures.frame_errors(frame, self.rows),
                             [f"sample A (available, {frame.width} columns): applicable rank-1 key is hidden"])

    def test_only_whole_bindings_count(self):
        frame = fixtures.draw(self.case, 80)
        for impostor in ("^A add", "A add another", "prefix A add"):
            with self.subTest(text=impostor):
                changed = dataclasses.replace(frame, parts=(impostor, "? help"))
                self.assertNotIn("A", changed.drawn)
        more = dataclasses.replace(frame, parts=("A add", "? 3 more"))
        self.assertEqual(more.drawn, {"A", "?"})

    def test_missing_help_fails_and_silent_alias_needs_no_line(self):
        text = "\n".join(row.help for row in self.rows if not row.silent)
        self.assertEqual(fixtures.help_errors("sample", self.rows, text), [])
        self.assertEqual(fixtures.help_errors("sample", self.rows, text.replace(self.rows[0].help, "")),
                         ["sample A: help line is missing"])


if __name__ == "__main__":
    unittest.main()
