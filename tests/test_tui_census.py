"""Source key loops, action rows and the small fixed-widget tier."""
from __future__ import annotations

import ast
import unittest
from types import SimpleNamespace

import _key_census as census
from _layout import REPO_ROOT
from charmer import tui
from charmer.cli import text as cli_text
from charmer.cli.screens import actions, get_started


def _source(loop):
    path, _owner, _method = loop
    return (REPO_ROOT / path).read_text()


def _bar_keys(*bars):
    # Compound labels such as y/N name both actual keys, not one key "Y/N".
    return {part.upper() if len(part) == 1 else part
            for bar in bars for key, *_rest in bar for part in key.split("/")}


class CensusSelfTests(unittest.TestCase):
    def assert_missing(self, source, keys):
        handled = census.handled_keys(source, "Screen")
        self.assertEqual(handled, keys)
        self.assertEqual(census.unmapped("synthetic", handled, ()), [
            f"synthetic handles {key}: not in its bar candidates nor its help" for key in sorted(keys)
        ])
        # Adding real rows, including a silent alias, is the green control.
        rows = [SimpleNamespace(key=key, silent=True) for key in keys]
        self.assertEqual(census.unmapped("synthetic", handled, rows), [])

    def test_unmapped_letter_goes_red(self):
        self.assert_missing('''class Screen:
    def run(self, win):
        key = read_key(win)
        if key.ch.upper() == "Z":
            unknown()
''', {"Z"})

    def test_named_tuple_goes_red(self):
        self.assert_missing('''KEYS = ("z", "Z")
class Screen:
    def run(self, win):
        aliases = KEYS
        key = read_key(win)
        if key.kind == "char" and key.ch in aliases:
            unknown()
''', {"Z"})

    def test_dispatch_dict_goes_red(self):
        self.assert_missing('''class Screen:
    def run(self, win):
        dispatch: dict = {"Z": unknown, "Y": another}
        key = read_key(win)
        letter = key.ch.upper() if key.kind == "char" else ""
        if letter in dispatch:
            dispatch[letter]()
''', {"Y", "Z"})

    def test_dispatch_subscript_without_membership_goes_red(self):
        self.assert_missing('''class Screen:
    def run(self, win):
        dispatch = {"Z": unknown}
        key = read_key(win)
        dispatch[key.ch.upper()]()
''', {"Z"})

    def test_dispatch_get_without_membership_goes_red(self):
        self.assert_missing('''class Screen:
    def run(self, win):
        dispatch = {"Z": unknown}
        key = read_key(win)
        dispatch.get(key.ch.upper(), ignore)()
''', {"Z"})

    def test_modal_shortcut_literal_goes_red(self):
        self.assert_missing('''class Screen:
    def run(self, win):
        shortcuts = {"Z": "unknown"}
        return tui.Modal("choose", shortcuts=shortcuts).run(win)
''', {"Z"})

    def test_control_tuple_does_not_consume_esc_or_enter(self):
        self.assert_missing('''class Screen:
    def run(self, win):
        key = read_key(win)
        if key.kind == "esc" or (key.kind == "ctrl" and key.ch == "c"):
            return
        if key.kind == "enter" or (key.kind == "ctrl" and key.ch in ("s", "o")):
            save()
''', {"Esc", "Enter", "^S", "^O"})

    def test_string_membership_is_individual_characters(self):
        self.assert_missing('''class Screen:
    def run(self, win):
        key = read_key(win)
        if key.kind == "char" and key.ch in "yYnN":
            return key.ch in "yY"
''', {"Y", "N"})

    def test_navigation_does_not_hide_folded_action_letters(self):
        self.assert_missing('''class Screen:
    def run(self, win):
        key = read_key(win)
        if key.kind == "up" or key.ch == "k":
            move()
        elif key.kind == "down" or key.ch == "j":
            move()
        elif key.ch.upper() in ("J", "K"):
            action()
''', {"J", "K"})

    def test_unresolved_key_collection_fails_closed(self):
        source = '''class Screen:
    def run(self, win):
        key = read_key(win)
        if key.ch in calculate_keys():
            unknown()
'''
        with self.assertRaisesRegex(AssertionError, "resolve.*calculate_keys"):
            census.handled_keys(source, "Screen")

    def test_new_unmapped_read_key_loop_goes_red(self):
        source = '''class Screen:
    def run(self, win):
        while True:
            key = read_key(win)
    def surprise(self, win):
        while True:
            event = tui.read_key(win)
'''
        known = ("fixture.py", "Screen", "run")
        added = ("fixture.py", "Screen", "surprise")
        readers = census.read_key_loops(source, "fixture.py")
        self.assertEqual(set(readers), {known, added})
        errors = census.loop_errors(readers, {known})
        self.assertEqual(errors, ["fixture.py:Screen.surprise reads a key: no screen or fixed-bar mapping"])
        self.assertEqual(census.loop_errors(readers, {known, added}), [])

    def test_module_level_loop_is_not_lost(self):
        source = "while True:\n    event = tui.read_key(win)\n"
        loop = ("fixture.py", "", "<module>")
        self.assertEqual(census.read_key_loops(source, "fixture.py"), {loop: 1})
        self.assertEqual(census.loop_errors({loop: 1}, set()),
                         ["fixture.py:<module> reads a key: no screen or fixed-bar mapping"])

    def test_decoder_recursion_exception_cannot_hide_a_new_loop(self):
        path = "src/charmer/tui.py"
        self.assertEqual(census.read_key_loops("def read_key(win):\n    return read_key(win)\n", path), {})
        with self.assertRaisesRegex(AssertionError, "decoder gained a key loop"):
            census.read_key_loops("def read_key(win):\n    while True:\n        return read_key(win)\n", path)

    def test_dynamic_widget_still_resolves_local_shortcut_literals(self):
        source = '''class Screen:
    def run(self, win):
        shortcuts = {"Z": "unknown"}
        key = read_key(win)
        if key.ch in shortcuts:
            return shortcuts[key.ch]
'''
        self.assertEqual(census.handled_keys(source, "Screen", dynamic_shortcuts=True), {"Z"})

    def test_second_read_loop_in_mapped_method_goes_red(self):
        source = '''class Screen:
    def run(self, win):
        while True:
            key = read_key(win)
        while True:
            key = read_key(win)
'''
        loop = ("fixture.py", "Screen", "run")
        errors = census.loop_errors(census.read_key_loops(source, "fixture.py"), {loop})
        self.assertEqual(errors, ["fixture.py:Screen.run has 2 key reads: split or explicitly map its loops"])

    def test_new_shortcut_owner_is_not_exempt(self):
        source = '''def surprise(win):
    return Modal("new", shortcuts={"Z": True}).run(win)
'''
        self.assertEqual(census.shortcut_owners(source, "fixture.py"), {("fixture.py", "", "surprise")})

    def test_fixed_tier_does_not_exempt_action_letters(self):
        self.assertEqual(census.fixed_unmapped("widget", {"Enter", "Z"}, {"Enter"}),
                         ["widget handles Z: not in its fixed bar or navigation"])
        self.assertEqual(census.fixed_unmapped("widget", {"Enter", "Z"}, {"Enter", "Z"}), [])

    def test_resume_w_requires_a_fresh_only_guard(self):
        source = '''class Screen:
    def run(self, win):
        key = read_key(win)
        ch = key.ch.lower()
        if ch == "w":
            setup()
        elif ch == "l" and not self.resume:
            launch()
        elif self.resume and ch in ("e", "p"):
            continue
'''
        self.assertEqual(census.card_fresh_keys(source, "Screen"), {"E", "P", "L"})
        guarded = source.replace('if ch == "w":', 'if ch == "w" and not self.resume:')
        self.assertEqual(census.card_fresh_keys(guarded, "Screen"), {"E", "P", "L", "W"})


class SourceCensusTests(unittest.TestCase):
    def test_every_source_key_reader_has_an_explicit_mapping(self):
        readers = {}
        shortcuts = set()
        dormant_callbacks = {callback for aliases in census.FIXED_SILENT.values()
                             for callback, reason in aliases.values() if reason}
        for path in sorted((REPO_ROOT / "src/charmer").rglob("*.py")):
            source = path.read_text()
            relative = path.relative_to(REPO_ROOT).as_posix()
            readers.update(census.read_key_loops(source, relative))
            shortcuts.update(census.shortcut_owners(source, relative))
            supplied = {node.arg for node in ast.walk(ast.parse(source)) if isinstance(node, ast.keyword)}
            self.assertFalse(supplied & dormant_callbacks, f"{relative}: dormant callback became an active action")
        mapped = set(census.LOOPS.values()) - set(census.DELEGATED_LOOPS)
        fixed = set(census.FIXED_LOOPS.values())
        self.assertFalse(mapped & fixed, "a loop belongs to one tier")
        self.assertEqual(census.loop_errors(readers, mapped | fixed), [])
        shortcut_mapped = set(census.LOOPS.values()) | set(census.SHORTCUT_LOOPS.values())
        self.assertEqual(shortcuts - shortcut_mapped, set(), "shortcut call needs a screen/fixed-widget owner")
        self.assertTrue(set(census.SHORTCUT_LOOPS.values()) <= shortcuts, "stale shortcut mapping")

    def test_screen_map_and_action_tables_agree(self):
        self.assertEqual(set(census.LOOPS), set(actions.ACTIONS))

    def test_every_handled_action_has_a_table_row(self):
        for screen in census.LOOPS:
            with self.subTest(screen=screen):
                self.assertEqual(census.unmapped(screen, census.screen_keys(screen), actions.ACTIONS.get(screen, ())), [])

    def test_shared_card_handler_and_context_agree_with_both_tables(self):
        path, owner, method = census.LOOPS["card"]
        source = (REPO_ROOT / path).read_text()
        self.assertEqual(census.card_fresh_keys(source, owner), census.CARD_FRESH_KEYS,
                         "fresh-only keys must be guarded in the shared handler (including W)")
        handled = census.handled_keys(source, owner, method)
        card = {row.key for row in actions.ACTIONS["card"]}
        resume = {row.key for row in actions.ACTIONS["resume-card"]}
        self.assertEqual(handled, card | resume)
        self.assertEqual(card - resume, census.CARD_FRESH_KEYS)
        self.assertEqual(resume, handled - census.CARD_FRESH_KEYS)

    def test_fixed_widgets_handle_only_bar_or_navigation_keys(self):
        # Actual widget bar builders and shipped literal footers, not an action
        # allowlist. Modal/ScrollModal expose Enter through their focused button
        # and Esc through the common modal dismissal contract.
        select_loop = census.SHORTCUT_LOOPS["select-list"]
        select_footers = census.literal_keywords(_source(select_loop), *select_loop[1:], keyword="footer")
        step_loop = census.SHORTCUT_LOOPS["step-view"]
        step_shortcuts = census.handled_keys(_source(step_loop), *step_loop[1:])
        step = get_started._StepView("fixture", [], palette=tui.MONO_PALETTE,
                                    buttons=(("Continue", True), ("Cancel", False)),
                                    shortcuts={key: key for key in step_shortcuts})
        bars = {
            "select-list": _bar_keys(tui.SelectList("fixture", []).footer.bindings, *select_footers),
            "modal": {"Enter", "Esc"},
            "scroll-modal": {"Enter", "Esc"},
            "text-report": _bar_keys(cli_text.PROPAGATION_REPORT_KEYBAR),
            "text-view": _bar_keys(*(tui.TextView("fixture", [], confirm=confirm)._keybar().bindings
                                     for confirm in (False, True, "declare"))),
            "listing-consent": _bar_keys(tui.TextView("fixture", [], confirm=True)._keybar().bindings),
            "step-view": _bar_keys(step._keybar().bindings),
        }
        self.assertEqual(set(bars), set(census.FIXED_LOOPS))
        for widget, bar in bars.items():
            with self.subTest(widget=widget):
                handled = census.fixed_keys(widget)
                silent = census.FIXED_SILENT.get(widget, {})
                self.assertTrue(set(silent) <= handled, "stale dormant compatibility key")
                for callback, reason in silent.values():
                    self.assertTrue(callback and reason)
                self.assertEqual(census.fixed_unmapped(widget, handled - silent.keys(), bar), [])


if __name__ == "__main__":
    unittest.main()
