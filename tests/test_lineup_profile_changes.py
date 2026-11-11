"""Profile/session changes on real compiled fixture scopes, without providers."""

from __future__ import annotations

import copy
import unittest
from unittest import mock

from charmer import lineup, scope, sessions, state, strict_json
from test_lineup import LineupCase


class NoSubagentsChangesTests(LineupCase):
    launch_profile = None

    def setUp(self):
        super().setUp()
        record = self.launch_fresh(self.direct_target("opus55"), no_subagents=True)
        self.mid, self.rid = record["managed_id"], record["runtime_session_id"]

    def test_agent_targets_are_refused_without_changing_record_or_scope(self):
        for request in ("profile balanced", "set implementer=opus55:xhigh"):
            with self.subTest(request=request):
                before = self.record_bytes(self.mid), self.scope_files()
                code, text, err = self.request(request)
                self.assertEqual((code, err), (0, ""))
                self.assertIn("was started with --no-subagents", text)
                self.assertIn("delegation stays denied for its whole life", text)
                self.assertIn("For agents, start a new session: charmer --profile", text)
                self.assertEqual((self.record_bytes(self.mid), self.scope_files()), before)

    def test_lead_only_change_is_allowed(self):
        code, text, err = self.request("direct fable")
        self.assertEqual((code, err), (0, ""))
        self.assertNotIn("cannot take agents", text)
        self.assertEqual(self.store.load(self.mid)["applied"]["agents"], {})
        self.assertIn("lead_target", self.store.load(self.mid))

    def test_show_warns_about_preexisting_unusable_agents(self):
        populated = self.launch_fresh(self.profile_target())
        applied = copy.deepcopy(populated["applied"])
        applied["no_subagents"] = True
        record = self.mutate(populated["managed_id"], applied=applied)
        self.assertIn("! delegation is denied in this session (--no-subagents): its agents cannot run",
                      lineup.show_text(self.runtime, record))


class ForkGuidanceTests(LineupCase):
    def test_fork_prints_the_full_managed_id_and_writes_nothing(self):
        before = self.record_bytes(self.mid), self.scope_files()
        code, text, err = self.request("fork")
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"charmer -r {self.mid} --fork", text)
        self.assertIn("This session is not changed and keeps running.", text)
        self.assertIn("select this session, then N", text)
        self.assertIn("Claude Code's own /fork does not work", text)
        self.assertEqual((self.record_bytes(self.mid), self.scope_files()), before)
        self.assertFalse(lineup.parse_cli(["--session", self.rid, "fork"], {}).writes)

    def test_fork_names_the_pending_profile_that_resume_will_use(self):
        self.mutate(self.mid, pending={"requested_at": "2026-09-25T00:00:00Z", "kind": "profile",
                    "profile": "quality", "follow": True, "document": None, "reasons": ["lead"]})
        _code, text, _err = self.request("fork")
        self.assertIn("next resume (quality)", text)

    def test_previous_release_skill_bytes_are_lazy_drift(self):
        from charmer.cli import doctor

        previous = getattr(scope, "_SKILL_MD_BYTES_2_4_0", scope.SKILL_MD_BYTES)
        state.atomic_write(self.live(self.mid) / scope.SKILL_RELPATH, previous)
        problems, attention, _info = doctor._check_scope_integrity(self.runtime, self.store.load(self.mid))
        self.assertEqual(problems, [])
        self.assertTrue(any("/cm skill differs" in str(row) and "applies at the next resume" in str(row)
                            for row in attention), attention)
        self.assertIn(previous, scope.PREVIOUS_SKILL_MD_BYTES)


class ProfileStatusTests(LineupCase):
    def test_show_describes_policy_only_profile_drift(self):
        self.save_profile("edited", lambda d: (
            d["native_agents"].update({"plan": "off"}),
            d.update({"workflows": "off", "settings_overrides": {"compaction_percent": 80}})))
        record = self.mutate(self.mid, profile="edited")
        before = self.record_bytes(self.mid), self.scope_files()
        text = lineup.show_text(self.runtime, record)
        self.assertIn("profile edited changed since this session applied it:", text)
        for row in ("native agents: plan", "workflows", "compaction override"):
            self.assertIn(row, text)
        self.assertIn("/cm follow applies it (nothing now;", text)
        self.assertIn("next resume", text)
        self.assertEqual((self.record_bytes(self.mid), self.scope_files()), before)

    def test_show_names_pin_and_pending_remedies(self):
        record = self.mutate(self.mid, follow=False, pending={
            "requested_at": "2026-09-25T00:00:00Z", "kind": "profile", "profile": "balanced",
            "follow": True, "document": None, "reasons": ["workflows"]})
        text = lineup.show_text(self.runtime, record)
        self.assertIn("pinned: edits of profile balanced do not reach this session", text)
        self.assertIn("/cm follow follows it again", text)
        self.assertIn("drop it with /cm pin", text)
        self.assertIn("replace it with /cm follow", text)

    def test_drift_remedy_does_not_promise_agents_outside_the_fence(self):
        self.save_profile("edited", lambda d: d["agents"].update(
            {"cm-implementer": {"model": "opus55", "effort": "xhigh"}}))
        record = self.mutate(self.mid, profile="edited", launch_fence="sha256:" + "0" * 64)
        text = lineup.show_text(self.runtime, record)
        self.assertIn("profile edited changed since", text)
        self.assertIn("/cm follow applies it (nothing now;", text)
        self.assertNotIn("/cm follow applies it (agents now", text)


class SplitProfileChangesTests(LineupCase):
    def _target(self, *, outside_agent=False):
        def edit(document):
            document["lead"] = {"model": "grok46", "effort": "high"}
            document["agents"]["cm-implementer"] = {
                "model": "grok46" if outside_agent else "opus55", "effort": "high" if outside_agent else "xhigh"}
        self.save_profile("mixed", edit)

    def test_agents_apply_now_and_lead_waits_in_one_record_commit(self):
        self._target()
        before = self.scope_files()
        original = self.store.load(self.mid)
        saves = []
        save = sessions.SessionStore.save

        def observe(store, record):
            if record["managed_id"] == self.mid:
                saves.append((record["lineup_generation"], "pending" in record))
            return save(store, record)

        with mock.patch.object(sessions.SessionStore, "save", observe):
            code, text, err = self.request("profile mixed")
        self.assertEqual(saves, [(2, True)])
        self.assertEqual((code, err), (0, ""))
        after = self.store.load(self.mid)
        self.assertEqual(after["lineup_generation"], 2)
        self.assertEqual(after["applied"]["lead"], original["applied"]["lead"])
        self.assertEqual(after["applied"]["agents"]["cm-implementer"]["key"], "opus55")
        self.assertEqual(after["pending"]["kind"], "profile")
        self.assertEqual(after["pending"]["profile"], "mixed")
        self.assertTrue(after["pending"]["follow"])
        self.assertIsNone(after["pending"]["document"])
        self.assertIn("requested_at", after["pending"])
        self.assertIn("waits for the next resume:", text)
        self.assertIn("next: type /reload-plugins for the agents now; the rest applies", text)
        for path in (scope.SETTINGS_RELPATH, "lead-set.json"):
            self.assertEqual(self.scope_files()[path], before[path])
        self.assertEqual(self.prepare_resume(self.mid).record["applied"]["lead"]["key"], "grok46")

    def test_agent_outside_fence_keeps_everything_pending(self):
        self._target(outside_agent=True)
        disk = strict_json.loads((self.live(self.mid) / scope.SETTINGS_RELPATH).read_bytes())
        disk["availableModels"] = [value for value in disk["availableModels"] if "grok" not in value]
        state.atomic_write(self.live(self.mid) / scope.SETTINGS_RELPATH, strict_json.canonical_file_bytes(disk))
        self.refence()
        before = self.scope_files()
        _code, text, _err = self.request("profile mixed")
        self.assertIn("relaunch needed", text)
        self.assertEqual(self.store.load(self.mid)["lineup_generation"], 1)
        self.assertEqual(self.scope_files(), before)

    def test_forced_exec_and_save_only_never_split(self):
        self._target()
        request = lineup.Request("profile", name="mixed", text="profile mixed")
        original = self.store.load(self.mid)
        for forced, exec_ok, pending_only in ((True, False, False), (False, True, False), (False, False, True)):
            with self.subTest(forced=forced, exec_ok=exec_ok, pending_only=pending_only):
                self.store.save(original)
                record = self.store.load(self.mid)
                before = self.scope_files()
                outcome = lineup.locked_decision(self.runtime, self.mid, lambda current: lineup.decide_locked(
                    self.runtime, current, request, forced=forced, exec_ok=exec_ok, pending_only=pending_only))
                self.assertEqual(outcome.kind, "exec" if exec_ok else "pending")
                self.assertEqual(self.store.load(self.mid)["applied"], record["applied"])
                self.assertEqual(self.scope_files(), before)

    def test_split_still_checks_credentials_and_new_role_collisions(self):
        self._target()
        before = self.record_bytes(self.mid), self.scope_files()
        with mock.patch.object(lineup, "_secret_problem", return_value="fixture credential unavailable"):
            _code, text, _err = self.request("profile mixed")
        self.assertIn("has no credential", text)
        self.assertEqual((self.record_bytes(self.mid), self.scope_files()), before)
        self.request("unset analyst")
        before = self.record_bytes(self.mid), self.scope_files()
        with mock.patch.object(scope, "find_cm_collisions", return_value=[("/fixture/project/agent.md", "cm-analyst")]):
            _code, text, _err = self.request("profile mixed")
        self.assertIn("would shadow newly bound ids", text)
        self.assertEqual((self.record_bytes(self.mid), self.scope_files()), before)

    def test_unreadable_launch_file_and_digest_mismatch_never_split(self):
        self._target()
        for rel in (scope.SETTINGS_RELPATH, "lead-set.json"):
            with self.subTest(rel=rel):
                path = self.live(self.mid) / rel
                original = path.read_bytes()
                state.atomic_write(path, b"unreadable fixture\n")
                before = self.scope_files()
                _code, text, _err = self.request("profile mixed")
                self.assertIn("relaunch needed", text)
                self.assertEqual(self.store.load(self.mid)["lineup_generation"], 1)
                self.assertEqual(self.scope_files(), before)
                state.atomic_write(path, original)
        self.mutate(self.mid, launch_fence="sha256:" + "0" * 64)
        _code, text, _err = self.request("profile mixed")
        self.assertIn("relaunch needed", text)
        self.assertEqual(self.store.load(self.mid)["lineup_generation"], 1)

    def test_existing_forced_profile_pending_does_not_split_on_follow(self):
        self._target()
        self.mutate(self.mid, profile="mixed", pending={
            "requested_at": "2026-09-25T00:00:00Z", "kind": "profile", "profile": "mixed",
            "follow": True, "document": None, "reasons": [lineup.REASON_FORCED]})
        _code, text, _err = self.request("follow")
        self.assertIn("relaunch needed", text)
        self.assertEqual(self.store.load(self.mid)["lineup_generation"], 1)
        self.assertIn(lineup.REASON_FORCED, self.store.load(self.mid)["pending"]["reasons"])

    def test_split_preview_names_both_effects_without_writes(self):
        self._target()
        before = self.record_bytes(self.mid), self.scope_files()
        shown = lineup.decide_locked(self.runtime, self.store.load(self.mid),
                                      lineup.Request("profile", name="mixed"), forced=False,
                                      exec_ok=False, preview_only=True)
        self.assertIn("Effect: LIVE for the agents now; the rest is recorded as pending for the next resume", shown.text)
        self.assertEqual((self.record_bytes(self.mid), self.scope_files()), before)
