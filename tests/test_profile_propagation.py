"""Save reports and shared propagation eligibility on fixture sessions."""
from __future__ import annotations

import io
from unittest import mock

from charmer import lineup, profile
from charmer.cli.commands import profile as commands
from test_propagation import PropagationCase


class SaveReportTests(PropagationCase):
    def _change_agent(self):
        self.edit("mine", lambda d: d["agents"].update(
            {"cm-implementer": {"model": "opus55", "effort": "xhigh"}}))

    def _propagate(self, answer="", *, interactive=True, **kwargs):
        output = io.StringIO()
        commands._propagate_saved(self.runtime, ["mine"], input_stream=io.StringIO(answer),
                                  output_stream=output, interactive=interactive, **kwargs)
        return output.getvalue()

    def test_interactive_empty_answer_applies_by_default(self):
        record = self.follower()
        self._change_agent()
        text = self._propagate("\n")
        self.assertIn("[Y/n]", text)
        self.assertIn("applied live", text)
        self.assertEqual(self.store.load(record["managed_id"])["lineup_generation"], 2)

    def test_noninteractive_default_and_explicit_later_do_not_apply(self):
        record = self.follower()
        self._change_agent()
        before = self.record_bytes(record["managed_id"])
        text = self._propagate(interactive=False)
        self.assertIn(f"charmer lineup --session {record['runtime_session_id']} follow", text)
        self.assertEqual(self.record_bytes(record["managed_id"]), before)
        text = self._propagate(apply_live=False)
        self.assertNotIn("[Y/n]", text)
        self.assertEqual(self.record_bytes(record["managed_id"]), before)

    def test_explicit_apply_needs_no_terminal(self):
        record = self.follower()
        self._change_agent()
        text = self._propagate(interactive=False, apply_live=True)
        self.assertIn("applied live", text)
        self.assertEqual(self.store.load(record["managed_id"])["lineup_generation"], 2)

    def test_pinned_sessions_are_counted_and_listed_with_remedy(self):
        live = self.follower()
        stopped = self.follower(ended=True)
        for record in (live, stopped):
            self.mutate(record["managed_id"], follow=False)
        before = {r["managed_id"]: self.record_bytes(r["managed_id"]) for r in (live, stopped)}
        text = self._propagate()
        self.assertIn("saved mine · no session follows it · 2 pinned session(s)", text)
        for record in (live, stopped):
            self.assertIn(f"{self.where(record)}: pinned (follow off) — not changed", text)
            self.assertEqual(self.record_bytes(record["managed_id"]), before[record["managed_id"]])
        self.assertIn("/cm follow there, or Sessions → F", text)

    def test_pending_that_pins_has_different_running_and_stopped_remedies(self):
        for ended in (False, True):
            with self.subTest(ended=ended):
                record = self.follower(ended=ended)
                pending = {"requested_at": "2026-09-25T00:00:00Z", "kind": "lineup",
                           "profile": "mine", "follow": False,
                           "document": profile.ad_hoc_direct("opus55"), "reasons": ["native agents"]}
                self.mutate(record["managed_id"], pending=pending)
                text = self.saved(["mine"])
                self.assertIn("a pending change pins it at its next resume — not applied", text)
                remedy = (f"charmer lineup --session {record['runtime_session_id']} follow"
                          if ended else "/cm follow there")
                self.assertIn(remedy, text)
                self.assertEqual(self.store.load(record["managed_id"])["pending"], pending)

    def test_own_profile_pending_is_eligible_in_preview_and_locked_apply(self):
        record = self.follower()
        pending = {"requested_at": "2026-09-25T00:00:00Z", "kind": "profile", "profile": "mine",
                   "follow": True, "document": None, "reasons": ["native agents: plan native → off"]}
        self.mutate(record["managed_id"], pending=pending)
        self._change_agent()
        text = self._propagate("\n")
        self.assertIn("apply to 1 running session(s) now? [Y/n]", text)
        self.assertEqual(self.store.load(record["managed_id"])["lineup_generation"], 2)

    def test_forced_pending_stays_forced_and_is_named(self):
        record = self.follower()
        pending = {"requested_at": "2026-09-25T00:00:00Z", "kind": "profile", "profile": "mine",
                   "follow": True, "document": None, "reasons": [lineup.REASON_FORCED]}
        self.mutate(record["managed_id"], pending=pending)
        self._change_agent()
        before = self.record_bytes(record["managed_id"])
        text = self._propagate("\n")
        self.assertIn("forced relaunch", text)
        self.assertNotIn("apply to 1", text)
        self.assertEqual(self.record_bytes(record["managed_id"]), before)

    def test_locked_recheck_does_not_apply_a_new_pending_pin(self):
        record = self.follower()
        self._change_agent()
        original = lineup.on_saved

        def intervening(runtime, names, **kwargs):
            pending = {"requested_at": "2026-09-25T00:00:00Z", "kind": "lineup", "profile": "mine",
                       "follow": False, "document": profile.ad_hoc_direct("opus55"), "reasons": ["lead"]}
            self.mutate(record["managed_id"], pending=pending)
            return original(runtime, names, **kwargs)

        with mock.patch.object(lineup, "on_saved", side_effect=intervening):
            text = self._propagate("\n")
        self.assertIn("a pending change pins it", text)
        self.assertEqual(self.store.load(record["managed_id"])["lineup_generation"], 1)
