"""Every external propagation outcome posts through the existing notice marker."""
from __future__ import annotations

import argparse
import io
from unittest import mock

from charmer import hooks, lineup, profile, state, strict_json, tui
from charmer.cli.screens import propagation
from test_propagation import PropagationCase
from test_tui import FakeWindow


class ExternalChangeNoticeTests(PropagationCase):
    def setUp(self):
        super().setUp()
        self.record = self.follower()
        self.mid, self.rid = self.record["managed_id"], self.record["runtime_session_id"]
        gen = hooks.read_gen(self.live(self.mid))
        path = hooks.seen_path(self.store.root, self.rid)
        state.ensure_private_dir(path.parent)
        state.atomic_write(path, f"{gen}\nstart {gen}\n".encode())
        check = mock.patch.object(hooks.client_check, "check", return_value=None)
        check.start()
        self.addCleanup(check.stop)
        original_post = getattr(hooks, "post_message", None)
        self.posts = []

        def post(root, rid, text):
            lock = self.store.lifecycle_lock(self.mid)
            acquired = lock.acquire(blocking=False)
            if acquired:
                lock.release()
            self.assertFalse(acquired, "the notification producer must hold the record lock")
            self.posts.append(text)
            return original_post(root, rid, text)

        if original_post is not None:
            patch = mock.patch.object(hooks, "post_message", side_effect=post)
            patch.start()
            self.addCleanup(patch.stop)

    def change(self):
        self.edit("mine", lambda d: d["agents"].update(
            {"cm-implementer": {"model": "opus55", "effort": "xhigh"}}))

    def prompt(self):
        output, error = io.StringIO(), io.StringIO()
        hooks.dispatch(argparse.Namespace(event="prompt", managed_id=self.mid,
                       launch_epoch=self.record["launch_epoch"]), state_root=self.store.root,
                       environ=self.runtime.environ, input_stream=io.StringIO('{"session_id":"' + self.rid + '"}'),
                       output_stream=output, error_stream=error)
        return strict_json.loads(output.getvalue()) if output.getvalue() else {}

    def once(self, text):
        self.assertEqual(len(self.posts), 1)
        document = self.prompt()
        self.assertIn(text, document.get("systemMessage", ""))
        self.assertEqual(self.prompt(), {})
        return document

    def test_external_live_change_posts_the_reload_instruction(self):
        self.request("set implementer=opus55:xhigh")
        document = self.once("this session's agents changed (lineup gen 2:")
        self.assertIn("type /reload-plugins before new agents start", document["systemMessage"])
        self.assertIn("lineup_generation 2", document["hookSpecificOutput"]["additionalContext"])

    def test_applied_profile_save_posts_once(self):
        self.change()
        self.saved(["mine"])
        self.once("this session's agents changed")

    def test_save_left_for_later_posts_without_changing_record_or_scope(self):
        before = self.record_bytes(self.mid), self.scope_files()
        self.change()
        self.saved(["mine"], apply_live=False)
        self.assertEqual((self.record_bytes(self.mid), self.scope_files()), before)
        document = self.once("profile mine changed; this session keeps its lineup")
        self.assertNotIn("hookSpecificOutput", document)

    def test_save_requiring_relaunch_posts_the_resume_command(self):
        self.edit("mine", lambda d: d["native_agents"].update({"plan": "off"}))
        self.saved(["mine"])
        document = self.once("needs a relaunch here")
        self.assertIn(f"charmer -r {self.mid}", document["systemMessage"])

    def test_external_pending_profile_change_posts_even_without_file_changes(self):
        self.save_profile("policy", lambda d: d["native_agents"].update({"plan": "off"}))
        before = self.scope_files()
        self.request("profile policy")
        self.assertEqual(self.scope_files(), before)
        self.once("profile policy needs a relaunch here")

    def test_external_pin_and_record_only_follow_both_post(self):
        before = self.scope_files()
        self.request("pin")
        self.once("this session is now pinned")
        self.posts.clear()
        self.request("follow")
        self.once("this session now follows profile mine")
        self.assertEqual(self.scope_files(), before)

    def test_save_blocked_by_pending_pin_posts_its_way_out(self):
        self.mutate(self.mid, pending={
            "requested_at": "2026-09-25T00:00:00Z", "kind": "lineup", "profile": "mine",
            "follow": False, "document": profile.ad_hoc_direct("opus55"), "reasons": ["native agents"]})
        self.change()
        self.saved(["mine"])
        self.once("a pending /cm change pins this session at its next resume")

    def test_in_session_changes_do_not_post_a_message(self):
        self.runtime.environ["CHARMER_MANAGED_ID"] = self.mid
        self.request("set implementer=opus55:xhigh")
        self.assertEqual(self.posts, [])
        self.assertNotIn("systemMessage", self.prompt())

    def test_dismissed_tui_save_posts_the_later_message(self):
        self.change()
        screen = propagation._PropagationScreen(self.runtime, tui.MONO_PALETTE, ["mine"])
        self.assertEqual(screen.run(FakeWindow(["\x1b"], height=40, width=120)), "later")
        self.once("profile mine changed; this session keeps its lineup")

    def test_profile_removal_notifies_the_pin_with_a_valid_way_out(self):
        self.runtime.profiles.delete("mine")
        self.removed("mine")
        document = self.once("was removed")
        self.assertIn("now pinned", document["systemMessage"])
        self.assertIn("/cm profile <name>", document["systemMessage"])
        text = lineup.show_text(self.runtime, self.store.load(self.mid))
        self.assertIn("pinned: profile mine no longer exists", text)

    def test_profile_rename_notifies_the_pin_and_new_profile_name(self):
        self.mutate(self.mid, pending={
            "requested_at": "2026-09-25T00:00:00Z", "kind": "profile", "profile": "mine",
            "follow": True, "document": None, "reasons": ["native agents"]})
        self.runtime.profiles.rename("mine", "renamed")
        self.removed("mine", renamed_to="renamed")
        document = self.once("was renamed to 'renamed'")
        self.assertIn("now pinned", document["systemMessage"])
        self.assertIn("/cm profile renamed", document["systemMessage"])
        text = lineup.show_text(self.runtime, self.store.load(self.mid))
        self.assertIn("pending change: take profile renamed and pin this session", text)

    def test_stopped_follower_has_no_prompt_message(self):
        self.mutate(self.mid, last_event_source="end")
        self.change()
        self.saved(["mine"])
        self.assertEqual(self.posts, [])
        self.assertEqual(self.prompt(), {})

    def test_forget_and_orphan_prune_still_remove_the_marker(self):
        from charmer.cli import doctor, doctor_actions

        self.change()
        self.saved(["mine"], apply_live=False)
        path = hooks.seen_path(self.store.root, self.rid)
        self.assertTrue(path.exists())
        self.store.forget_session(self.mid)
        self.assertFalse(path.exists())
        # A stale marker left by a lost record is still in the existing
        # generated-file inventory, regardless of the marker's contents.
        state.atomic_write(path, b"!message -\n\ncharmer: orphan\n")
        candidates = doctor.prune_candidates(self.runtime).candidates
        self.assertIn(path, [item[0] for item in candidates])
        self.assertEqual(doctor_actions._doctor_prune(self.runtime, io.StringIO()), 0)
        self.assertFalse(path.exists())
