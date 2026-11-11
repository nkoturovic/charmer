"""A charmer fork: a new managed session from a copy of another session's conversation.

The fork is a fresh record of its own (``forked_from``) whose launch runs
``--resume <parent runtime> --fork-session --session-id <new>``; the parent
is read, never written. These tests cover the preparation (the lineup the
parent would get at its next resume, the plan, the keyed fence), the commit
and its backstop (the parent's fingerprint under its
lifecycle lock), failure cleanup with the continue pointer put back, the
fork gate's refusals, the CLI flow's argument refusals and its lines, the
SessionStart notice and store reconcile of the fork's own start, and the
forget path that removes a session's generated lead prompts.

The fork plans here come from the real preparation (``Runtime.prepare``
with ``action="fork"``); the commit tests call ``launch.perform_launch``
with the plan's parent and fingerprint, the flow tests run the whole
``_fork_flow`` through ``Runtime.perform``.
"""

from __future__ import annotations

import hashlib
import io
import os
import unittest
import uuid
from pathlib import Path
from typing import Any
from unittest import mock

from charmer import cli, hooks, launch, scope, sessions, state, strict_json
from charmer import settings as settings_mod
from charmer.cli import doctor, launch_flow, resume_checks, session_facts
from charmer.cli import selection
from charmer.launch import LaunchError
from _layout import RESOURCES_ROOT
from _v4 import V4Case

# The session schema 2.4.0 shipped (src/charmer/data/schemas/session.schema.json
# at the v2.4.0 tag): a fork record must load in a 2.4.0 launcher unchanged.
SESSION_SCHEMA_240_SHA256 = "0c866bda06d78b71869b25730814e84d696c5976cf397edaa3af988bbb178bf8"


class ForkCase(V4Case):
    """A launched parent and fork plans built from it."""

    def setUp(self) -> None:
        super().setUp()
        self.parent = self.launch_fresh()
        self.pid = self.parent["managed_id"]

    def fork_target(self, parent_id: str | None = None) -> cli.LaunchTarget:
        record = self.store.load(parent_id or self.pid)
        return cli.LaunchTarget("record", None, record.get("profile"), bool(record.get("follow")),
                                f"Fork of {sessions.managed_id(record)[:8]} — new session")

    def fork_plan(self, parent_id: str | None = None, **kwargs: Any) -> tuple[Any, str]:
        """The real fork preparation of ``parent_id`` and its fingerprint."""

        parent_id = parent_id or self.pid
        prepared = self.runtime.prepare(self.fork_target(parent_id), action="fork", passthrough=[],
                                        session_id=parent_id, **kwargs)
        return prepared, prepared.fork_fingerprint

    def perform_fork(self, prepared: Any, fingerprint: str, *, execve: Any = None,
                     parent_id: str | None = None) -> Any:
        runtime = self.runtime
        return launch.perform_launch(
            prepared.result, record=prepared.record, store=runtime.session_store,
            native_contract=runtime.catalog.docs["native-contract"], gateway=runtime.catalog.docs["gateway"],
            execve=execve or self._execve, environ=runtime.environ, home=runtime.home,
            health_get=runtime.health_get, readiness=runtime._launch_readiness,
            fork_parent=parent_id or self.pid, fork_fingerprint=fingerprint,
        )

    def parent_state(self) -> tuple[bytes | None, dict[str, bytes]]:
        return self.record_bytes(self.pid), self.tree(self.live(self.pid))

    def pointer(self) -> bytes | None:
        return self.store.read_pointer_bytes(str(self.project))

    def fork_id(self, prepared: Any) -> str:
        return sessions.managed_id(prepared.record)


NOW = "2026-11-11T12:00:00Z"


class ForkPrepareTests(ForkCase):
    """The preparation: the lineup the parent would get at its next resume,
    the refusals in the preparation and again before the commit."""

    def test_a_recorded_change_applies_to_the_fork_and_stays_on_the_parent(self) -> None:
        self.mutate(self.pid, pending={
            "requested_at": NOW, "kind": "profile", "profile": "quality", "follow": True,
            "document": None, "reasons": ["lead class large → large"],
        }, mutation_token=sessions.new_mutation_token())
        before = self.parent_state()
        prepared, _fingerprint = self.fork_plan()
        self.assertEqual((prepared.record["profile"], prepared.record["follow"]), ("quality", True))
        self.assertNotIn("pending", prepared.record)
        self.assertTrue(prepared.diff)
        self.runtime.perform(prepared)
        self.assertEqual(self.parent_state(), before)
        self.assertEqual(self.store.load(self.pid)["pending"]["profile"], "quality")

    def test_a_deleted_followed_profile_pins_the_fork_not_the_parent(self) -> None:
        self.runtime.profiles.duplicate("balanced", "mine")
        parent = self.launch_fresh(self.profile_target("mine"))
        self.runtime.profiles.delete("mine")
        prepared, _fingerprint = self.fork_plan(parent["managed_id"])
        self.assertIn(f"profile 'mine' no longer exists; the fork is pinned to session "
                      f"{parent['managed_id'][:8]}'s applied lineup", prepared.notices)
        self.assertEqual((prepared.record["profile"], prepared.record["follow"]), ("mine", False))
        self.assertEqual(prepared.record["applied"]["lead"], parent["applied"]["lead"])
        self.assertTrue(self.store.load(parent["managed_id"])["follow"])

    def test_a_pinned_parent_forks_with_its_applied_lineup(self) -> None:
        self.mutate(self.pid, follow=False, mutation_token=sessions.new_mutation_token())
        prepared, _fingerprint = self.fork_plan()
        self.assertFalse(prepared.record["follow"])
        for key in ("lead", "agents", "no_subagents"):
            self.assertEqual(prepared.record["applied"][key], self.parent["applied"][key], key)
        self.assertEqual(prepared.diff, ())

    def test_the_fork_takes_the_parents_directory_and_title(self) -> None:
        self.store.set_title(self.pid, "review")
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        runtime = self.make_runtime(cwd=elsewhere)
        prepared = runtime.prepare(self.fork_target(), action="fork", passthrough=[], session_id=self.pid)
        self.assertEqual(prepared.record["cwd"], self.parent["cwd"])
        self.assertEqual(prepared.record["title"], "review (fork)")
        self.assertEqual(prepared.record["forked_from"], self.pid)
        self.assertEqual(prepared.record["launch_epoch"], 1)
        self.assertEqual(prepared.record["lineup_generation"], 1)

    def test_every_resume_of_the_fork_keeps_the_record_off(self) -> None:
        prepared, _fingerprint = self.fork_plan()
        self.runtime.perform(prepared)
        child = self.fork_id(prepared)
        self.write_transcript(child)
        argv = list(self.prepare_resume(child).result.argv)
        self.assertEqual(argv[argv.index("--system-prompt-snapshot") + 1], "off")
        self.assertNotIn("--system-prompt-snapshot", self.prepare_resume(self.pid).result.argv)

    def test_the_preparation_refuses_and_writes_nothing(self) -> None:
        self.write_transcript(self.parent["runtime_session_id"]).unlink()
        state_before = self.tree(self.store.root)
        with self.assertRaises(cli.CLIError) as caught:
            self.fork_plan()
        self.assertTrue(str(caught.exception).startswith(f"cannot fork session {self.pid[:8]}: no conversation"))
        self.assertEqual(self.tree(self.store.root), state_before)
        with self.assertRaises(cli.CLIError):
            self.runtime.prepare(self.fork_target(), action="fork", passthrough=[])

    def test_the_gate_is_checked_again_before_the_commit(self) -> None:
        prepared, _fingerprint = self.fork_plan()
        self.write_transcript(self.parent["runtime_session_id"]).unlink()
        launches, pointer = len(self.execs), self.pointer()
        with self.assertRaises(cli.CLIError) as caught:
            self.runtime.perform(prepared)
        self.assertTrue(str(caught.exception).startswith(f"cannot fork session {self.pid[:8]}: no conversation"))
        self.assertIsNone(self.record_bytes(self.fork_id(prepared)))
        self.assertFalse(self.live(self.fork_id(prepared)).exists())
        self.assertEqual((len(self.execs), self.pointer()), (launches, pointer))

    def test_a_running_parent_may_be_forked(self) -> None:
        self.runtime.live_prefixes = frozenset({self.parent["runtime_session_id"][:8]})
        prepared, _fingerprint = self.fork_plan()
        self.runtime.perform(prepared)
        self.assertEqual(self.store.load(self.fork_id(prepared))["forked_from"], self.pid)

    def test_the_subagent_policy_is_the_parents(self) -> None:
        with self.assertRaisesRegex(launch.LaunchError, "a fork keeps its parent's"):
            self.fork_plan(no_subagents=not self.parent["applied"]["no_subagents"])


class ForkKeyedFenceTests(ForkCase):
    """A ``--no-subagents`` direct session on a keyed line forks with the
    same fence, credentials that reach the client only through the token
    helper and a scrubbed environment; a provider switched off between the
    preparation and the commit refuses."""

    def setUp(self) -> None:
        super().setUp()
        self.keyed = self.launch_fresh(self.direct_target("kimi-k3"), no_subagents=True)
        self.kid = self.keyed["managed_id"]

    def test_the_fork_keeps_the_fence_and_helper_only_credentials(self) -> None:
        self.runtime.environ["KIMI_CLAUDE_API_KEY"] = "fixture-inherited-key"
        prepared, _fingerprint = self.fork_plan(self.kid)
        record = prepared.record
        self.assertTrue(record["applied"]["no_subagents"])
        self.assertEqual(record["applied"]["lead"], self.keyed["applied"]["lead"])
        self.assertEqual((record["profile"], record["follow"]), (None, False))
        parent_scope = self.live(self.kid)
        self.assertEqual(prepared.result.scope_plan.other_files[scope.LEAD_SET_JSON],
                         (parent_scope / scope.LEAD_SET_JSON).read_bytes())
        self.assertIn("KIMI_CLAUDE_API_KEY", prepared.result.env_unset)
        self.runtime.perform(prepared)
        _path, argv, env = self.execs[-1]
        child = env["CHARMER_MANAGED_ID"]
        self.assertEqual(child, self.fork_id(prepared))
        self.assertNotIn("KIMI_CLAUDE_API_KEY", env)
        parent_settings = strict_json.loads((parent_scope / "settings.json").read_bytes())
        settings = strict_json.loads((self.live(child) / "settings.json").read_bytes())
        self.assertEqual(settings["apiKeyHelper"], parent_settings["apiKeyHelper"])
        for key in ("availableModels", "model", "modelPicker"):
            self.assertEqual(settings.get(key), parent_settings.get(key), key)
        files = self.tree(self.live(child))
        for value in ("v4-test-dummy", "fixture-inherited-key"):
            with self.subTest(value=value):
                self.assertFalse(any(value in item for item in argv))
                self.assertFalse(any(value in item for item in env.values()))
                self.assertFalse(any(value.encode() in data for data in files.values()))

    def test_a_provider_switched_off_after_the_preparation_refuses(self) -> None:
        prepared, _fingerprint = self.fork_plan(self.kid)
        launches, pointer, parent = len(self.execs), self.pointer(), self.record_bytes(self.kid)
        path = settings_mod.settings_path(self.runtime.environ)
        state.ensure_private_dir(path.parent)
        state.atomic_write(path, strict_json.pretty_file_bytes({"version": 1, "providers": {"kimi": {"enabled": False}}}))
        with self.assertRaisesRegex(LaunchError, "changed or its route/provider became unavailable"):
            self.runtime.perform(prepared)
        self.assertIsNone(self.record_bytes(self.fork_id(prepared)))
        self.assertFalse(self.live(self.fork_id(prepared)).exists())
        self.assertEqual((len(self.execs), self.pointer(), self.record_bytes(self.kid)),
                         (launches, pointer, parent))


class ForkCommitTests(ForkCase):
    def test_a_fork_commits_its_own_record_and_scope_and_never_writes_the_parent(self) -> None:
        before = self.parent_state()
        prepared, fingerprint = self.fork_plan()
        self.assertEqual(prepared.fork_parent, self.pid)
        self.assertEqual(fingerprint, sessions.fork_fingerprint(self.store.load(self.pid)))
        self.assertEqual(self.parent_state(), before)  # the preparation writes nothing
        child = self.fork_id(prepared)
        self.assertNotEqual(child, self.pid)
        self.runtime.perform(prepared)
        _path, argv, env = self.execs[-1]
        self.assertEqual(argv[1:6], ["--resume", self.parent["runtime_session_id"], "--fork-session",
                                     "--session-id", child])
        # Every launch of a fork switches Claude Code's system-prompt record off.
        self.assertEqual(argv[-2:], ["--system-prompt-snapshot", "off"])
        self.assertIn(str(self.live(child) / "settings.json"), argv)
        self.assertNotIn(str(self.live(self.pid) / "settings.json"), argv)
        self.assertEqual(env["CHARMER_MANAGED_ID"], child)
        self.assertEqual(env["CHARMER_LAUNCH_EPOCH"], "1")
        record = self.store.load(child)
        self.assertEqual(record["forked_from"], self.pid)
        self.assertEqual(record["runtime_session_id"], child)
        self.assertEqual(record["launch_epoch"], 1)
        self.assertEqual(record["lineup_generation"], 1)
        self.assertEqual(record["cwd"], self.parent["cwd"])
        self.assertEqual(record["pending_forks"], [])
        self.assertTrue(self.live(child).is_dir())
        self.assertEqual(self.parent_state(), before)
        self.assertEqual(self.store.last(str(self.project)), child)

    def test_two_forks_of_one_parent_give_two_records(self) -> None:
        first, first_fp = self.fork_plan()
        second, second_fp = self.fork_plan()
        self.perform_fork(first, first_fp)
        self.perform_fork(second, second_fp)
        a, b = self.fork_id(first), self.fork_id(second)
        self.assertNotEqual(a, b)
        self.assertEqual({self.store.load(a)["forked_from"], self.store.load(b)["forked_from"]}, {self.pid})

    def test_the_fork_record_loads_under_the_schema_2_4_0_shipped(self) -> None:
        schema = (RESOURCES_ROOT / "schemas" / "session.schema.json").read_bytes()
        self.assertEqual(hashlib.sha256(schema).hexdigest(), SESSION_SCHEMA_240_SHA256)
        prepared, fingerprint = self.fork_plan()
        self.perform_fork(prepared, fingerprint)
        # store.save validated it; a fresh store with the shipped schema reads it.
        again = sessions.SessionStore(self.store.root, sessions.default_schema(), read_only=True)
        self.assertEqual(again.load(self.fork_id(prepared))["forked_from"], self.pid)

    def test_a_fork_plan_must_name_the_parent_it_was_prepared_from(self) -> None:
        prepared, fingerprint = self.fork_plan()
        other = str(uuid.uuid4())
        with self.assertRaises(LaunchError):
            self.perform_fork(prepared, fingerprint, parent_id=other)
        with self.assertRaises(LaunchError):
            self.perform_fork(prepared, "")
        self.assertIsNone(self.record_bytes(self.fork_id(prepared)))


class ForkBackstopTests(ForkCase):
    """The commit re-reads the parent's fingerprint under its lifecycle lock."""

    def refused(self, change) -> None:
        prepared, fingerprint = self.fork_plan()
        change()
        parent_bytes, pointer = self.record_bytes(self.pid), self.pointer()
        with self.assertRaises(LaunchError) as caught:
            self.perform_fork(prepared, fingerprint)
        self.assertEqual(str(caught.exception), launch.FORK_CHANGED.format(m8=self.pid[:8]))
        child = self.fork_id(prepared)
        self.assertIsNone(self.record_bytes(child))
        self.assertFalse(self.live(child).exists())
        self.assertEqual(self.record_bytes(self.pid), parent_bytes)
        self.assertEqual(self.pointer(), pointer)
        self.assertEqual(self.execs[-1][1][1:3], ["--session-id", self.pid])  # only the parent's launch

    def test_a_pin_from_another_terminal_discards_the_prepared_intent(self) -> None:
        self.refused(lambda: self.mutate(self.pid, follow=False, mutation_token=sessions.new_mutation_token()))

    def test_a_live_lineup_change_refuses(self) -> None:
        record = self.store.load(self.pid)
        self.refused(lambda: self.mutate(self.pid, lineup_generation=record["lineup_generation"] + 1))

    def test_a_same_id_relink_to_another_directory_refuses(self) -> None:
        other = self.root / "elsewhere"
        other.mkdir()
        self.refused(lambda: self.mutate(self.pid, cwd=str(other)))

    def test_new_repair_evidence_refuses(self) -> None:
        self.refused(lambda: self.mutate(self.pid, identity_state=sessions.IDENTITY_REPAIR_NEEDED,
                                         observed_cwd="/elsewhere"))

    def test_a_new_native_fork_of_the_parent_refuses(self) -> None:
        self.refused(lambda: self.mutate(
            self.pid, identity_state=sessions.IDENTITY_PENDING_FORK,
            pending_forks=[{"session_id": str(uuid.uuid4()), "observed_at": "2026-11-11T12:00:00Z"}]))

    def test_a_resume_of_the_parent_refuses(self) -> None:
        self.refused(lambda: self.mutate(self.pid, launch_epoch=self.parent["launch_epoch"] + 1))

    def test_a_title_change_or_hook_activity_does_not_invalidate_the_fork(self) -> None:
        prepared, fingerprint = self.fork_plan()
        self.store.set_title(self.pid, "renamed meanwhile")
        self.mutate(self.pid, last_event_source="end", last_seen_at="2026-11-11T12:00:00Z",
                    identity_state=sessions.IDENTITY_AUTHORITATIVE)
        self.perform_fork(prepared, fingerprint)
        self.assertEqual(self.store.load(self.fork_id(prepared))["forked_from"], self.pid)

    def test_the_fingerprint_ignores_title_timestamps_and_verification(self) -> None:
        record = self.store.load(self.pid)
        base = sessions.fork_fingerprint(record)
        for change in ({"title": "x"}, {"last_seen_at": "2030-01-01T00:00:00Z"}, {"last_event_source": "end"},
                       {"identity_state": sessions.IDENTITY_AUTHORITATIVE}, {"last_end_reason": "other"}):
            with self.subTest(change=change):
                self.assertEqual(sessions.fork_fingerprint({**record, **change}), base)
        for change in ({"runtime_session_id": str(uuid.uuid4())}, {"launch_epoch": 9}, {"follow": False},
                       {"mutation_token": "t"}, {"applied_hash": "sha256:" + "0" * 64}, {"lineup_generation": 7},
                       {"cwd": "/x"}, {"identity_state": sessions.IDENTITY_REPAIR_NEEDED},
                       {"observed_model": "m"}, {"pending": {"reasons": []}}, {"lead_target": {"key": "k"}},
                       {"pending_forks": [{"session_id": "a"}]}):
            with self.subTest(change=sorted(change)):
                self.assertNotEqual(sessions.fork_fingerprint({**record, **change}), base)


class ForkFailureTests(ForkCase):
    """A failed fork launch: no record, no scope, the previous continue pointer."""

    def failing(self, *, before_raise=None):
        def execve(path: str, argv: list[str], env: dict[str, str]) -> int:
            if before_raise is not None:
                before_raise()
            raise OSError("exec failed in the fixture")

        return execve

    def test_a_failed_exec_restores_the_parents_pointer(self) -> None:
        pointer = self.pointer()
        self.assertEqual(self.store.last(str(self.project)), self.pid)
        prepared, fingerprint = self.fork_plan()
        with self.assertRaises(OSError):
            self.perform_fork(prepared, fingerprint, execve=self.failing())
        child = self.fork_id(prepared)
        self.assertIsNone(self.record_bytes(child))
        self.assertFalse(self.live(child).exists())
        self.assertEqual(self.pointer(), pointer)
        self.assertEqual(self.store.last(str(self.project)), self.pid)

    def test_a_third_launchs_pointer_is_kept(self) -> None:
        third = str(uuid.uuid4())
        prepared, fingerprint = self.fork_plan()
        with self.assertRaises(OSError):
            self.perform_fork(prepared, fingerprint, execve=self.failing(
                before_raise=lambda: self.store.update_last(str(self.project), third, blocking=True)))
        self.assertIsNone(self.record_bytes(self.fork_id(prepared)))
        self.assertEqual(self.store.last(str(self.project)), third)

    def test_no_earlier_pointer_leaves_none(self) -> None:
        self.store.clear_last(str(self.project), self.pid, blocking=True)
        prepared, fingerprint = self.fork_plan()
        with self.assertRaises(OSError):
            self.perform_fork(prepared, fingerprint, execve=self.failing())
        self.assertIsNone(self.pointer())

    def test_a_failure_before_exec_restores_the_pointer_too(self) -> None:
        pointer = self.pointer()
        prepared, fingerprint = self.fork_plan()
        with mock.patch.object(self.store, "update_last", side_effect=OSError("disk full in the fixture")):
            with self.assertRaises(OSError):
                self.perform_fork(prepared, fingerprint)
        self.assertIsNone(self.record_bytes(self.fork_id(prepared)))
        self.assertEqual(self.pointer(), pointer)


class ForkTitleTests(unittest.TestCase):
    def test_titles(self) -> None:
        self.assertIsNone(sessions.fork_title(None))
        self.assertIsNone(sessions.fork_title("  "))
        self.assertEqual(sessions.fork_title("review"), "review (fork)")
        long = "x" * 80
        title = sessions.fork_title(long)
        self.assertEqual(len(title), sessions.TITLE_MAX)
        self.assertTrue(title.endswith(" (fork)"))
        self.assertEqual(sessions.fork_title("a" * 72 + " b"), "a" * 72 + " (fork)")

    def test_a_forked_title_is_a_valid_title(self) -> None:
        self.assertTrue(sessions.fork_title("é" * 80).isprintable())


class ForkGateTests(ForkCase):
    """The resume gate without the liveness check; nothing is written."""

    def gate(self, record: dict) -> str | None:
        return resume_checks.fork_gate_refusal(self.runtime, record)

    def test_an_ended_or_running_parent_with_a_conversation_may_be_forked(self) -> None:
        self.assertIsNone(self.gate(self.parent))
        self.runtime.live_prefixes = frozenset({self.parent["runtime_session_id"][:8]})
        self.assertIsNone(self.gate(self.parent))

    def test_refusal_texts(self) -> None:
        m8 = self.pid[:8]
        rid = self.parent["runtime_session_id"]
        transcript = self.write_transcript(rid)
        pending = {**self.parent, "identity_state": sessions.IDENTITY_PENDING_FORK,
                   "pending_forks": [{"session_id": str(uuid.uuid4()), "observed_at": "2026-11-11T12:00:00Z"}]}
        repair = {**self.parent, "identity_state": sessions.IDENTITY_REPAIR_NEEDED, "observed_cwd": "/elsewhere"}
        self.assertEqual(
            self.gate({**self.parent, "version": 3}),
            f"cannot fork session {m8}: it is a legacy record — resume it once (charmer -r {m8}) to migrate it, "
            "then fork it")
        self.assertEqual(self.gate(pending), f"cannot fork session {m8}: " + sessions.pending_fork_message(pending))
        self.assertEqual(self.gate(repair), f"cannot fork session {m8}: " + sessions.relink_message(repair))
        transcript.unlink()
        self.assertEqual(
            self.gate(self.parent),
            f"cannot fork session {m8}: no conversation file exists for runtime {rid} (expected at {transcript}); "
            "a session that has not had a message yet has nothing to fork")

    def test_a_conversation_elsewhere_gets_the_resume_gates_text(self) -> None:
        rid = self.parent["runtime_session_id"]
        self.write_transcript(rid).unlink()
        other = self.root / "moved"
        other.mkdir()
        self.write_transcript(rid, cwd=str(other))
        refusal = self.gate(self.parent)
        gate = resume_checks._evaluate_resume_gate(self.runtime, self.parent, live_prefixes=frozenset())
        self.assertEqual(gate.kind, "transcript-elsewhere")
        self.assertEqual(refusal, f"cannot fork session {self.pid[:8]}: " + " ".join(gate.lines))

    def test_a_parent_that_needs_a_lead_choice(self) -> None:
        from charmer.cli.runtime import ResumeChoice

        choice = ResumeChoice(needs_choice=True)
        with mock.patch.object(self.runtime, "resume_choice", return_value=choice), \
                mock.patch.object(sessions, "lead_choice_notice", return_value="fixture notice"):
            refusal = self.gate(self.parent)
        self.assertTrue(refusal.startswith(f"session {self.pid} needs a profile or model choice"))
        self.assertTrue(refusal.endswith(" — then fork it (a saved choice applies to the fork)"))


class ForkFlowTests(ForkCase):
    """``launch_flow._fork_flow`` driven directly, on the real runtime."""

    def flow(self, **kwargs: Any) -> tuple[int, str, str]:
        out, report, line = io.StringIO(), io.StringIO(), io.StringIO()
        prepare = mock.Mock(wraps=self.runtime.prepare)
        try:
            with mock.patch.object(self.runtime, "prepare", prepare), mock.patch("sys.stderr", io.StringIO()):
                code = launch_flow._fork_flow(
                    self.runtime, self.pid, passthrough=[], interactive=False, confirm=False,
                    input_stream=io.StringIO(), output_stream=out, report_stream=report,
                    exec_stream=line, **{"print_launch": False, **kwargs})
        finally:
            self.prepared = [(call.kwargs["action"], call.kwargs.get("read_only", False))
                             for call in prepare.call_args_list]
        return code, report.getvalue(), line.getvalue()

    def test_argument_refusals(self) -> None:
        cases = (
            (dict(session=None, continue_session=False, profile=False, environ={}),
             "--fork needs the session to fork: charmer -r SESSION --fork (or N on its row in the sessions screen)"),
            (dict(session=None, continue_session=True, profile=False, environ={}),
             "--fork works with -r SESSION only"),
            (dict(session=self.pid, continue_session=False, profile=True, environ={}),
             "--fork starts with the lineup the session would get at its next resume; after the fork starts, "
             "change it with /cm profile NAME (or T on its row)"),
            (dict(session=self.pid, continue_session=False, profile=False, environ={"CLAUDECODE": "1"}),
             "charmer -r SESSION --fork starts a new interactive session: run it in another terminal "
             "(in a session, /cm fork prints the command)"),
        )
        for kwargs, text in cases:
            with self.subTest(text=text[:30]):
                with self.assertRaises(cli.CLIError) as caught:
                    launch_flow._fork_preflight(**kwargs)
                self.assertEqual(str(caught.exception), text)
        self.assertIsNone(launch_flow._fork_preflight(session=self.pid, continue_session=False, profile=False,
                                                      environ={}))

    def test_the_gate_refuses_before_anything_is_prepared(self) -> None:
        self.write_transcript(self.parent["runtime_session_id"]).unlink()
        state_before = self.tree(self.store.root)
        with self.assertRaises(cli.CLIError) as caught:
            self.flow()
        self.assertTrue(str(caught.exception).startswith(f"cannot fork session {self.pid[:8]}: no conversation"))
        self.assertEqual(self.prepared, [])
        self.assertEqual(self.tree(self.store.root), state_before)

    def test_print_launch_shows_the_fork_argv_and_writes_nothing(self) -> None:
        state_before = self.tree(self.store.root)
        launches = len(self.execs)
        code, report, line = self.flow(print_launch=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.prepared, [("fork", True)])
        self.assertEqual(len(self.execs), launches)
        self.assertIn(f"  --resume\n  {self.parent['runtime_session_id']}\n  --fork-session\n  --session-id\n",
                      report)
        self.assertIn("  --system-prompt-snapshot\n  off\n", report)
        self.assertEqual(line, "")
        self.assertEqual(self.tree(self.store.root), state_before)

    def test_the_line_before_exec_names_both_sessions(self) -> None:
        before = self.parent_state()
        code, _report, line = self.flow()
        self.assertEqual(code, 0)
        self.assertEqual(self.prepared, [("fork", False)])
        _path, argv, env = self.execs[-1]
        child = env["CHARMER_MANAGED_ID"]
        self.assertEqual(argv[1:6], ["--resume", self.parent["runtime_session_id"], "--fork-session",
                                     "--session-id", child])
        self.assertEqual(self.store.load(child)["forked_from"], self.pid)
        self.assertEqual(self.parent_state(), before)
        self.assertEqual(line, f"charmer: forking session {self.pid[:8]} into new session {child[:8]} "
                               "(profile:balanced); the original is not changed.\n")

    def test_the_card_action_word_is_fork(self) -> None:
        prepared, _fingerprint = self.fork_plan()
        self.assertEqual(selection._card_action(prepared), "fork")


class ForkListingTests(ForkCase):
    def test_list_and_show_name_the_parent(self) -> None:
        prepared, fingerprint = self.fork_plan()
        self.perform_fork(prepared, fingerprint)
        child = self.fork_id(prepared)
        self.write_transcript(child)
        out = io.StringIO()
        self.assertEqual(cli.main(["sessions", "list"], runtime=self.runtime, output_stream=out,
                                  interactive=False), 0)
        rows = [row for row in out.getvalue().splitlines() if child[:8] in row]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].endswith(f"  (fork of {self.pid[:8]}…)"))
        parent_rows = [row for row in out.getvalue().splitlines() if f" {self.pid[:8]} " in row]
        self.assertTrue(parent_rows and "(fork of" not in parent_rows[0])
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stderr", err):
            self.assertEqual(cli.main(["sessions", "show", child], runtime=self.runtime, output_stream=out,
                                      interactive=False), 0)
        self.assertIn(f"(fork of {self.pid[:8]}…): started from a copy of session {self.pid}'s conversation",
                      err.getvalue())
        self.assertEqual(strict_json.loads(out.getvalue().encode())["forked_from"], self.pid)
        self.assertEqual(session_facts.fork_of_note(self.parent), "")


class ForkStartTests(ForkCase):
    """The fork's own SessionStart: the notice and the store reconcile."""

    def launched_fork(self) -> str:
        prepared, fingerprint = self.fork_plan()
        self.perform_fork(prepared, fingerprint)
        return self.fork_id(prepared)

    def start(self, mid: str, observed: str, source: str = "fork", epoch: int = 1) -> tuple[int, str]:
        payload = {"hook_event_name": "SessionStart", "session_id": observed, "source": source,
                   "cwd": str(self.project)}
        out = io.StringIO()
        with mock.patch("sys.stderr", io.StringIO()):
            code = cli.main(["session-event", "start", "--managed-id", mid, "--launch-epoch", str(epoch),
                             "--hook-protocol", "3"], runtime=self.runtime,
                            input_stream=io.StringIO(strict_json.canonical_bytes(payload).decode()),
                            output_stream=out, interactive=False)
        return code, out.getvalue()

    def test_the_first_fork_start_is_the_forks_startup(self) -> None:
        child = self.launched_fork()
        parent_bytes = self.record_bytes(self.pid)
        code, output = self.start(child, child)
        self.assertEqual(code, 0)
        record = self.store.load(child)
        self.assertEqual(record["identity_state"], sessions.IDENTITY_AUTHORITATIVE)
        self.assertEqual(record["last_event_source"], "startup")
        self.assertEqual(record["pending_forks"], [])
        self.assertEqual(self.record_bytes(self.pid), parent_bytes)
        context = strict_json.loads(output)["hookSpecificOutput"]["additionalContext"]
        self.assertIn(hooks._PREFACES["fork-own"], context)
        self.assertNotIn("shares the parent session's scope", context)
        self.assertNotIn("staged — active after /reload-plugins", context)
        # A repeated fork start records nothing more.
        again = self.record_bytes(child)
        self.assertEqual(self.start(child, child)[0], 0)
        self.assertEqual(self.store.load(child)["last_event_source"], "startup")
        self.assertEqual(strict_json.loads(self.record_bytes(child))["identity_state"],
                         strict_json.loads(again)["identity_state"])

    def test_a_native_fork_inside_the_fork_is_still_a_pending_fork_of_the_fork(self) -> None:
        child = self.launched_fork()
        self.start(child, child)
        native = str(uuid.uuid4())
        code, output = self.start(child, native)
        self.assertEqual(code, 0)
        record = self.store.load(child)
        self.assertEqual([item["session_id"] for item in record["pending_forks"]], [native])
        context = strict_json.loads(output)["hookSpecificOutput"]["additionalContext"]
        self.assertIn(hooks._PREFACES["fork"], context)
        self.assertNotIn(hooks._PREFACES["fork-own"], context)

    def test_a_fork_start_of_a_record_that_is_no_fork_records_nothing(self) -> None:
        before = self.store.load(self.pid)
        self.assertEqual(self.start(self.pid, self.pid)[0], 0)
        after = self.store.load(self.pid)
        self.assertEqual(after["last_event_source"], before["last_event_source"])
        self.assertEqual(after["identity_state"], before["identity_state"])

    def test_notice_keys(self) -> None:
        self.assertEqual(hooks.notice_key("SessionStart", "fork", own_fork=True), "fork-own")
        self.assertEqual(hooks.notice_key("SessionStart", "fork"), "fork")
        self.assertEqual(hooks.notice_key("SessionStart", "fork-own"), "other")
        self.assertEqual(hooks.notice_key("SessionStart", "startup", own_fork=True), "startup")
        text = hooks.notice_text("lineup", "4 abc", event="SessionStart", source="fork", lead=None, own_fork=True)
        self.assertNotIn("staged — active after /reload-plugins", text)
        self.assertIn("staged — active after /reload-plugins",
                      hooks.notice_text("lineup", "4 abc", event="SessionStart", source="fork", lead=None))


class ForgetLeadPromptTests(V4Case):
    """``sessions forget`` removes the lead prompts the launcher generated for the session."""

    def setUp(self) -> None:
        super().setUp()
        self.record = self.launch_fresh()
        self.mid = self.record["managed_id"]
        root = self.store.root
        # A superseded prompt of the same session, another session's prompt,
        # and names that only resemble the pattern.
        self.superseded = root / f"lead-prompt-{'a' * 16}-{self.mid}.md"
        state.atomic_write(self.superseded, b"older prompt\n")
        self.other = root / f"lead-prompt-{'b' * 16}-{uuid.uuid4()}.md"
        state.atomic_write(self.other, b"another session\n")
        self.near = [root / f"lead-prompt-{'c' * 15}-{self.mid}.md", root / f"lead-prompt-{'C' * 16}-{self.mid}.md",
                     root / f"lead-prompt-{'d' * 16}-{self.mid}.md.bak", root / f"x-lead-prompt-{'e' * 16}-{self.mid}.md"]
        for path in self.near:
            state.atomic_write(path, b"not a generated prompt\n")
        self.outside = self.root / "outside.md"
        self.outside.write_bytes(b"outside the state root\n")
        self.link = root / f"lead-prompt-{'f' * 16}-{self.mid}.md"
        os.symlink(self.outside, self.link)

    def prompts(self) -> list[Path]:
        return sorted(self.store.root.glob(f"lead-prompt-*-{self.mid}.md"))

    def test_lists_only_the_sessions_regular_prompts(self) -> None:
        launched = Path(self.execs[-1][1][self.execs[-1][1].index("--append-system-prompt-file") + 1])
        self.assertEqual(sessions.lead_prompt_files(self.store.root, self.mid),
                         sorted([launched, self.superseded]))

    def test_forget_removes_every_prompt_of_the_session_and_nothing_else(self) -> None:
        removed, _scope = self.store.forget_session(self.mid)
        self.assertTrue(removed)
        self.assertEqual(sessions.lead_prompt_files(self.store.root, self.mid), [])
        self.assertTrue(self.other.is_file())
        for path in self.near:
            self.assertTrue(path.is_file(), path.name)
        self.assertTrue(self.link.is_symlink())
        self.assertEqual(self.outside.read_bytes(), b"outside the state root\n")

    def test_a_corrupt_record_forget_removes_them_too(self) -> None:
        path = self.store.sessions_dir / f"{self.mid}.json"
        state.atomic_write(path, b"{not json")
        removed, _scope = self.store.forget_session(self.mid)
        self.assertTrue(removed)
        self.assertEqual(sessions.lead_prompt_files(self.store.root, self.mid), [])

    def test_a_failed_deletion_leaves_the_record(self) -> None:
        real = state.remove_private

        def remove(path):
            if Path(path).name.startswith("lead-prompt-"):
                raise OSError("fixture deletion failure")
            return real(path)

        with mock.patch.object(state, "remove_private", side_effect=remove):
            with self.assertRaises(OSError):
                self.store.forget_session(self.mid)
        self.assertTrue(self.store.exists(self.mid))

    def test_after_a_cli_forget_the_prune_preview_finds_no_prompt_of_the_session(self) -> None:
        out = io.StringIO()
        with mock.patch("sys.stderr", io.StringIO()):
            code = cli.main(["sessions", "forget", self.mid, "--yes"], runtime=self.runtime, output_stream=out,
                            interactive=False)
        self.assertEqual(code, 0)
        found = doctor.prune_candidates(self.runtime)
        self.assertEqual([path for path, _kind, _reason in found.candidates if self.mid in path.name
                          and path.name.startswith("lead-prompt-") and not path.is_symlink()], [])


if __name__ == "__main__":
    unittest.main()
