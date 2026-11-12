"""The profile setter's command body, independent of argument registration."""
from __future__ import annotations

import argparse
import io
from unittest import mock

from charmer import errors, profile, tui
from charmer.cli.commands import profile as commands
from test_propagation import PropagationCase


class ProfileSetTests(PropagationCase):
    def command(self, *assignments, name="mine", preview=False, apply_live=None):
        output = io.StringIO()
        args = argparse.Namespace(name=name, assignments=list(assignments), preview=preview,
                                  profile_apply=apply_live)
        try:
            code = commands._profile_command(self.runtime, args, "set", input_stream=io.StringIO(),
                                             output_stream=output, interactive=False)
        except errors.CharmerError as exc:
            return 1, str(exc)
        return code, output.getvalue()

    def test_multi_slot_edit_preserves_untouched_fields_and_named_bindings(self):
        self.runtime.bindings.set("reader", "sol", "high", cat=self.runtime.lineup_catalog())
        self.edit("mine", lambda d: d["agents"].update({"cm-explorer": {"use": "reader"}}))
        original = self.runtime.profiles.load("mine")
        code, text = self.command("lead=fable", "cm-implementer=opus55:xhigh")
        self.assertEqual(code, 0, text)
        expected = original
        expected["lead"] = {"model": "fable", "effort": self.runtime.lineup_catalog().lines["fable"]["default_effort"]}
        expected["agents"]["cm-implementer"] = {"model": "opus55", "effort": "xhigh"}
        self.assertEqual(self.runtime.profiles.load("mine"), expected)
        self.assertIn("Saved profile 'mine'", text)

    def test_invalid_second_assignment_leaves_file_unchanged(self):
        before = self.runtime.profiles.raw_bytes("mine")
        for bad in ("cm-implementer=unknown", "cm-implementer=opus55:bogus", "not-a-role=opus55",
                    "lead=opus55:xhigh", "cm-implementer=opus55:"):
            with self.subTest(bad=bad):
                code, text = self.command("lead=fable:max", bad)
                self.assertEqual(code, 1, text)
                self.assertNotIn("unsupported profile command", text)
                self.assertEqual(self.runtime.profiles.raw_bytes("mine"), before)

    def test_concurrent_save_refuses_without_overwriting_other_editor(self):
        original_save = profile.ProfileStore.save

        def changed(store, document, **kwargs):
            self.edit("mine", lambda d: d.update(description="another editor"))
            return original_save(store, document, **kwargs)

        with mock.patch.object(profile.ProfileStore, "save", changed):
            code, text = self.command("lead=fable:max")
        self.assertEqual(code, 1)
        self.assertIn("changed", text)
        self.assertEqual(self.runtime.profiles.load("mine")["description"], "another editor")

    def test_preview_is_read_only_and_prints_diff(self):
        before = self.tree(self.root / "config"), self.tree(self.store.root)
        with mock.patch.object(profile.ProfileStore, "install_seeds", side_effect=AssertionError("seed write")):
            code, text = self.command("lead=fable:max", preview=True)
        self.assertEqual(code, 0, text)
        self.assertIn("lead", text)
        self.assertIn("→", text)
        self.assertEqual((self.tree(self.root / "config"), self.tree(self.store.root)), before)

    def test_seed_copy_and_named_replacement_match_editor(self):
        self.assertFalse(self.runtime.profiles.has_user("balanced"))
        before = self.tree(self.root / "config"), self.tree(self.store.root)
        code, text = self.command("lead=fable:max", name="balanced", preview=True)
        self.assertEqual(code, 0, text)
        self.assertIn("user copy", text)
        self.assertIn("would be created", text)
        self.assertEqual((self.tree(self.root / "config"), self.tree(self.store.root)), before)
        editor = tui.ProfileEditorState(self.runtime.profiles.load("balanced"),
                                        cat=self.runtime.lineup_catalog(), bindings={},
                                        effective=self.runtime.current_effective(), origin="balanced", is_seed=True)
        editor.set_binding(profile.LEAD_ROLE, "fable", "max")
        code, text = self.command("lead=fable:max", name="balanced")
        self.assertEqual(code, 0, text)
        self.assertEqual(self.runtime.profiles.load("balanced"), editor.document)
        self.runtime.bindings.set("worker", "sol", "high", cat=self.runtime.lineup_catalog())
        self.edit("mine", lambda d: d["agents"].update({"cm-implementer": {"use": "worker"}}))
        named_before = self.runtime.bindings.load()
        code, text = self.command("cm-implementer=opus55:xhigh")
        self.assertEqual(code, 0, text)
        self.assertEqual(self.runtime.profiles.load("mine")["agents"]["cm-implementer"],
                         {"model": "opus55", "effort": "xhigh"})
        self.assertEqual(self.runtime.bindings.load(), named_before)

    def test_save_propagates_with_the_explicit_apply_choice(self):
        record = self.follower()
        code, text = self.command("cm-implementer=opus55:xhigh", apply_live=True)
        self.assertEqual(code, 0, text)
        self.assertIn("applied live", text)
        self.assertEqual(self.store.load(record["managed_id"])["lineup_generation"], 2)

    def test_later_agent_edit_reaches_a_session_already_holding_split_pending(self):
        record = self.follower()
        code, text = self.command("lead=grok46:high", "cm-implementer=opus55:xhigh", apply_live=True)
        self.assertEqual(code, 0, text)
        self.assertIn("agents applied live", text)
        code, text = self.command("cm-implementer=sol:high", apply_live=True)
        self.assertEqual(code, 0, text)
        self.assertIn("agents applied live (lineup gen 3)", text)
        after = self.store.load(record["managed_id"])
        self.assertEqual(after["pending"]["profile"], "mine")
        self.assertEqual(after["applied"]["agents"]["cm-implementer"]["key"], "sol")
