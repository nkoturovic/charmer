"""Bounded notice messages, generation provenance and locked acknowledgement."""
from __future__ import annotations

import argparse
import io
import stat
from unittest import mock

from charmer import hooks, lineup, profile, scope, state, strict_json
from test_hooks_v4 import _SeamCase


class ModelSwitchMessagesTests(_SeamCase):
    def test_pin_is_both_a_system_message_and_model_context_without_marker(self):
        code, output, errors = self.postmodel()
        self.assertEqual(code, 0, errors)
        document = strict_json.loads(output)
        self.assertEqual(document.get("systemMessage"), self.context(output))
        self.assertIn("no longer follows profile balanced", document["systemMessage"])
        self.assertIn("later edits of the profile will not reach it", document["systemMessage"])
        self.assertFalse(hooks.seen_path(self.store.root, self.rid).exists())

    def test_an_ordinary_pending_change_survives_and_the_message_says_so(self):
        key = self.record["applied"]["lead"]["key"]
        pending = {"requested_at": "2026-09-25T00:00:00Z", "kind": "lineup", "profile": None,
                   "follow": False, "document": profile.ad_hoc_direct(key), "reasons": ["native agents"]}
        self.mutate(self.mid, pending=pending)
        _code, output, _errors = self.postmodel()
        message = strict_json.loads(output).get("systemMessage", "")
        self.assertIn("for this run", message)
        self.assertIn("still applies then", message)
        self.assertNotIn("later edits of the profile will not reach it", message)
        self.assertEqual(self.store.load(self.mid)["pending"], pending)
        self.assertEqual(self.prepare_resume(self.mid).record["applied"]["lead"]["key"], key)

    def test_a_split_pending_change_survives_and_resume_takes_the_profile(self):
        document = self.runtime.profiles.load("balanced")
        document.pop("seed", None)
        document["name"] = "split"
        document["lead"] = {"model": "grok46", "effort": "high"}
        document["agents"]["cm-implementer"] = {"model": "opus55", "effort": "xhigh"}
        self.runtime.profiles.save(document)
        lineup.locked_decision(self.runtime, self.mid, lambda record: lineup.decide_locked(
            self.runtime, record, lineup.Request("profile", name="split"), forced=False, exec_ok=False))
        before = self.store.load(self.mid)
        self.assertEqual(before["lineup_generation"], 2)
        _code, output, _errors = self.postmodel()
        message = strict_json.loads(output).get("systemMessage", "")
        self.assertIn("profile split", message)
        self.assertIn("still applies then", message)
        self.assertEqual(self.store.load(self.mid)["pending"], before["pending"])
        resumed = self.prepare_resume(self.mid).record
        self.assertEqual(resumed["applied"]["lead"]["key"], "grok46")
        self.assertTrue(resumed["follow"])


class NoticeMessageTests(_SeamCase):
    def setUp(self):
        super().setUp()
        check = mock.patch.object(hooks.client_check, "check", return_value=None)
        check.start()
        self.addCleanup(check.stop)

    def gen(self, number=None):
        path = self.live(self.mid) / scope.LINEUP_GEN
        current = path.read_text().strip()
        if number is not None:
            current = f"{number} {current.split()[1]}"
            state.atomic_write(path, (current + "\n").encode())
            self.mutate(self.mid, lineup_generation=number)
        return current

    def marker(self, seen=None, *, start=None, message=None):
        path = hooks.seen_path(self.store.root, self.rid)
        state.ensure_private_dir(path.parent)
        first = seen or "-"
        if message is not None:
            first = "!message " + first
        lines = [first]
        if start is not None or message is not None:
            lines.append("start " + start if start is not None else "")
        if message is not None:
            lines.append(message)
        state.atomic_write(path, ("\n".join(lines) + "\n").encode())
        return path

    def start(self, source):
        code, text, err = self.run_main(self.argv("start"), {
            "hook_event_name": "SessionStart", "source": source, "session_id": self.rid,
            "cwd": str(self.project)}, runtime=self.runtime)
        self.assertEqual(code, 0, err)
        return strict_json.loads(text)

    def prompt(self, output=None):
        output = output if output is not None else io.StringIO()
        error = io.StringIO()
        code = hooks.dispatch(argparse.Namespace(event="prompt", managed_id=self.mid,
                              launch_epoch=self.record["launch_epoch"]), state_root=self.store.root,
                              environ=self.runtime.environ,
                              input_stream=io.StringIO('{"session_id":"' + self.rid + '"}'),
                              output_stream=output, error_stream=error)
        self.assertEqual(code, 0, error.getvalue())
        return strict_json.loads(output.getvalue()) if output.getvalue() else {}

    def post(self, text):
        self.assertTrue(callable(getattr(hooks, "post_message", None)), "message producer is available")
        with self.store.lifecycle_lock(self.mid):
            hooks.post_message(self.store.root, self.rid, text)

    def test_pending_message_is_shown_once_and_start_line_survives_ack(self):
        gen = self.gen()
        path = self.marker(gen, start=gen, message="charmer: fixture change")
        document = self.prompt()
        self.assertEqual(document.get("systemMessage"), "charmer: fixture change")
        self.assertNotIn("hookSpecificOutput", document, "unchanged generation needs no repeated lineup")
        self.assertEqual(path.read_text().splitlines(), [gen, "start " + gen])
        self.assertEqual(self.prompt(), {})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_message_posted_between_read_and_ack_is_not_lost(self):
        gen = self.gen()
        path = self.marker(gen, start=gen, message="charmer: first change")
        test = self

        class Interleaved(io.StringIO):
            def write(self, text):
                test.post("charmer: second change")
                return super().write(text)

        first = self.prompt(Interleaved())
        self.assertIn("first change", first.get("systemMessage", ""))
        self.assertTrue(path.read_text().startswith("!message "))
        second = self.prompt()
        self.assertIn("second change", second.get("systemMessage", ""))
        self.assertIn("first change", second.get("systemMessage", ""))
        self.assertEqual(path.read_text().splitlines()[1], "start " + gen)

    def test_busy_lock_does_not_acknowledge(self):
        gen = self.gen()
        path = self.marker(gen, start=gen, message="charmer: retry this notice")
        raw = path.read_bytes()
        with self.store.lifecycle_lock(self.mid):
            document = self.prompt()
        self.assertIn("retry this notice", document.get("systemMessage", ""))
        self.assertEqual(path.read_bytes(), raw)
        self.assertIn("retry this notice", self.prompt().get("systemMessage", ""))
        self.assertEqual(self.prompt(), {})

    def test_compaction_keeps_undelivered_message_and_start_generation(self):
        gen = self.gen()
        path = self.marker(gen, start=gen, message="charmer: still waiting")
        self.start("compact")
        self.assertTrue(path.read_text().startswith("!message "))
        self.assertIn("start " + gen, path.read_text())
        self.assertIn("still waiting", self.prompt().get("systemMessage", ""))

    def test_unchanged_compaction_has_no_reload_claim(self):
        self.gen(4)
        self.start("startup")
        context = self.start("compact")["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("staged — active after /reload-plugins", context)
        self.assertNotIn("was applied while this session was running", context)

    def test_unchanged_process_never_claims_a_staged_reload_after_a_message(self):
        gen = self.gen(4)
        self.start("startup")
        self.post("charmer: profile saved for later")
        self.prompt()
        document = self.start("compact")
        context = document["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("staged — active after", context)
        self.assertNotIn("was applied while this session was running", context)
        self.assertIn("start " + gen, hooks.seen_path(self.store.root, self.rid).read_text())

    def test_live_change_keeps_conditional_reload_wording_after_compaction(self):
        original = self.gen(3)
        self.start("startup")
        current = self.gen(4)
        self.post("charmer: agents changed")
        self.prompt()
        context = self.start("compact")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Lineup gen 4 was applied while this session was running: if /reload-plugins "
                      "has not been run since, run it before starting agents.", context)
        self.assertEqual(hooks.seen_path(self.store.root, self.rid).read_text().splitlines(),
                         [current, "start " + original])

    def test_in_process_runtime_id_change_carries_start_and_undelivered_message(self):
        original = self.gen(3)
        self.start("startup")
        self.post("charmer: carry this notice")
        self.gen(4)
        observed = "45454545-4545-4545-8545-454545454545"
        code, text, err = self.run_main(self.argv("start"), {
            "hook_event_name": "SessionStart", "source": "clear", "session_id": observed,
            "cwd": str(self.project)}, runtime=self.runtime)
        self.assertEqual(code, 0, err)
        self.assertIn("was applied while this session was running", self.context(text))
        self.rid = observed
        self.assertIn("start " + original, hooks.seen_path(self.store.root, self.rid).read_text())
        self.assertIn("carry this notice", self.prompt().get("systemMessage", ""))

    def test_native_fork_does_not_inherit_the_parent_process_marker(self):
        self.start("startup")
        self.post("charmer: parent-only notice")
        observed = "46464646-4646-4646-8646-464646464646"
        code, _text, err = self.run_main(self.argv("start"), {
            "hook_event_name": "SessionStart", "source": "fork", "session_id": observed,
            "cwd": str(self.project)}, runtime=self.runtime)
        self.assertEqual(code, 0, err)
        child, _raw = hooks.read_marker(self.store.root, observed)
        parent, _raw = hooks.read_marker(self.store.root, self.rid)
        self.assertIsNone(child.start)
        self.assertFalse(child.message)
        self.assertIn("parent-only notice", parent.message)

    def test_resume_replaces_start_but_preserves_a_pending_message(self):
        old = self.gen(3)
        path = self.marker(old, start=old, message="charmer: resume pending notice")
        current = self.gen(4)
        self.start("resume")
        self.assertIn("start " + current, path.read_text())
        self.assertIn("resume pending notice", self.prompt().get("systemMessage", ""))

    def test_old_marker_means_unknown_start_and_old_reader_rejects_new_forms(self):
        gen = self.gen()
        for seen, start, message in ((gen, gen, None), (gen, gen, "charmer: hello"),
                                      (None, gen, None), (None, None, "charmer: hello")):
            raw = self.marker(seen, start=start, message=message).read_bytes()
            self.assertIsNone(hooks._gen_from_bytes(raw))
        path = self.marker(gen)
        state.atomic_write(path, b"-\n")
        context = self.prompt()["hookSpecificOutput"]["additionalContext"]
        self.assertIn("if /reload-plugins has not been run since", context)

    def test_post_merges_newest_first_and_bounds_utf8_bytes(self):
        gen = self.gen()
        path = self.marker(gen, start=gen)
        for number in range(20):
            self.post(f"charmer: change {number} " + "é" * 80)
        raw = path.read_bytes()
        self.assertLessEqual(len(raw), 700)
        lines = raw.decode().splitlines()
        self.assertEqual(lines[1], "start " + gen)
        self.assertTrue(lines[2].startswith("charmer: change 19"))
        self.assertRegex(lines[-1], r"^\+[1-9][0-9]* more$")
        self.assertLessEqual(len("\n".join(lines[2:]).encode()), 512)
