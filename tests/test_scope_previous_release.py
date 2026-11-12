"""Doctor over scopes compiled by the genuine previous release, without a client or provider."""
from __future__ import annotations

import difflib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from charmer import catalog, compiler, operator, scope, sessions, state, strict_json, transition
from charmer.cli import doctor, doctor_actions
from test_statusline import isolated_child_environ, write_fixture_endpoint


PREVIOUS_RELEASE_ENV = "CHARMER_TEST_PREVIOUS_RELEASE"
CURRENT_ASSETS = Path(__file__).resolve().parents[1] / "src" / "charmer" / "data"
MID = "11111111-1111-4111-8111-111111111111"
HOOK = "/fixture/bin/charmer-hook-3"
TOKEN_HELPER = "/fixture/bin/charmer-gateway-token"
OPERATOR_KEY = "custom-fable-5-2"
DECLARATION = {"version": 1, "lines": {OPERATOR_KEY: {
    "wire_model": "claude-fable-5-2", "display": "Fable 5.2",
    "efforts": ["high"], "default_effort": "high",
    "context": {"declared_tokens": 1000000, "source": "docs",
                "source_ref": "https://models.example/fable-5-2"},
}}}

_PREVIOUS_COMPILE = r"""
import json
import os
from pathlib import Path
import sys

previous, manifest = map(Path, sys.argv[1:])
sys.path[:0] = [str(previous / "src")]
from charmer import catalog, compiler, operator, profile, scope, sessions, settings, state, strict_json
assert Path(compiler.__file__).resolve().is_relative_to((previous / "src").resolve())
assets = previous / "src/charmer/data"
bundle = catalog.load_catalog(assets)
assert bundle.docs["version"]["launcher_version"] == "2.4.0"
assert bundle.docs["native-contract"]["verified"][0]["version"] == "2.1.312"
data = json.loads(manifest.read_text())
root = Path(data["state_root"])
project = Path(data["project"])
docs = bundle.docs
if data["declaration"]:
    layer = operator.validate_layer(docs, {"anthropic": strict_json.pretty_file_bytes(data["declaration"])},
                                    schemas=operator.load_schemas(assets))
    assert not operator.blocking_problems(layer), [problem.text() for problem in operator.blocking_problems(layer)]
    docs = operator.merge_docs(docs, layer)
lcat = profile.LineupCatalog.from_docs(docs)
effective = settings.effective({"version": 1, "admitted_lines": [data["lead"], *data["agents"].values()]},
                               provider_ids=lcat.providers, line_keys=lcat.lines)
document = profile.ad_hoc_direct(data["lead"], "high")
document["name"] = "previous-scope"
document["agents"] = {slot: {"model": key, "effort": "high"} for slot, key in data["agents"].items()}
lineup = profile.resolve(document, lcat, effective=effective)
mid = data["managed_id"]
result = compiler.compile_lineup_launch(
    docs=docs, prompt_bodies=bundle.prompt_bodies, lineup=lineup, effective=effective,
    session_action=compiler.build_fresh(mid), lineup_generation=1, launch_epoch=1,
    state_root=root, scope_dir=scope.scope_dir(root, mid), hook_command=data["hook"],
    token_helper_command=data["token_helper"], session_cwd=project, worktree_available=True,
    launch_environ=os.environ, default_mode=scope.PERMISSION_DEFAULT_MODE,
    subagent_status_line=scope.status_line_setting(scope.status_line_shim_path(root)))
plan = result.scope_plan
context = compiler.lead_set_context(result.fence, effective.compaction_percent,
                                   ceiling=profile.window_ceiling(effective))
applied = sessions.applied_from_lineup(lineup, context=context,
    settings_snapshot=settings.snapshot(effective), no_subagents=False)
record = sessions.make_v4_record(
    managed_id=mid, cwd=str(project), profile=document["name"], follow=True, applied=applied,
    lineup_generation=1, lead_class=lineup.lead_class, catalog_version=lcat.catalog_version,
    catalog_hash=strict_json.bundle_digest(docs), launcher_version="2.4.0", launch_epoch=1,
    launch_fence=sessions.launch_digest(plan.settings, plan.other_files[scope.LEAD_SET_JSON]),
    scope_lead=sessions.lead_ref(applied["lead"]), now="2026-10-28T00:00:00Z")
store = sessions.SessionStore(root, sessions.default_schema())
state.atomic_write(root / sessions.STATE_MARKER, b"4\n")
scope.write_scope(root, mid, plan)
store.save(record)
store.record_session_end(mid, observed_runtime_id=mid, launch_epoch=1, reason="fixture ended")
print("genuine 2.4.0 compiler; client contract 2.1.312; no client or provider executed")
"""


def settings_changes(before, after, prefix=""):
    changes = []
    for key in sorted(before.keys() | after.keys()):
        path = f"{prefix}.{key}" if prefix else key
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        if isinstance(old, dict) and isinstance(new, dict):
            changes.extend(settings_changes(old, new, path))
        elif isinstance(old, list) and isinstance(new, list) and all(isinstance(item, str) for item in old + new):
            changes.append({"path": path, "removed": [item for item in old if item not in new],
                            "added": [item for item in new if item not in old]})
        else:
            changes.append({"path": path, "before": old, "after": new})
    return changes


class PreviousReleaseDoctorTests(unittest.TestCase):
    def setUp(self):
        tree = os.environ.get(PREVIOUS_RELEASE_ENV)
        if not tree:
            self.skipTest(f"BOUNDARY: {PREVIOUS_RELEASE_ENV} names no 2.4.0 source tree")
        self.previous = Path(tree)
        version = strict_json.load(self.previous / "src/charmer/data/version.json")["launcher_version"]
        self.assertEqual(version, "2.4.0")
        temporary = tempfile.TemporaryDirectory(prefix="previous-scope-doctor-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = state.ensure_private_dir(self.root / "home")
        self.project = state.ensure_private_dir(self.root / "project")
        self.state_root = self.root / "state"
        write_fixture_endpoint(self.home)
        self.environ = isolated_child_environ({"HOME": str(self.home)}, self.root / "runtime")

    def merged(self, assets, declaration):
        bundle = catalog.load_catalog(assets)
        docs = bundle.docs
        if declaration:
            layer = operator.validate_layer(docs, {"anthropic": strict_json.pretty_file_bytes(declaration)},
                                            schemas=operator.load_schemas(assets))
            self.assertEqual(operator.blocking_problems(layer), ())
            docs = operator.merge_docs(docs, layer)
        return bundle, docs, layer if declaration else None

    def check_previous_scope(self, *, renamed=False, lead="opus", agent=None, assert_lazy=True, report=True):
        if renamed:
            lead = OPERATOR_KEY
        declaration = DECLARATION if OPERATOR_KEY in (lead, agent) else None
        scenario = f"lead={lead}, agent={agent or 'none'}"
        manifest = self.root / "compile-input.json"
        state.atomic_write(manifest, strict_json.canonical_file_bytes({
            "state_root": str(self.state_root), "project": str(self.project), "managed_id": MID,
            "hook": HOOK, "token_helper": TOKEN_HELPER, "lead": lead,
            "agents": {"cm-reviewer": agent} if agent else {}, "declaration": declaration,
        }))
        child = subprocess.run([sys.executable, "-c", _PREVIOUS_COMPILE, str(self.previous), str(manifest)],
                               cwd=self.project, env=self.environ, capture_output=True, text=True, timeout=120)
        self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
        if report:
            print(child.stdout.strip())
        store = sessions.SessionStore(self.state_root, sessions.default_schema())
        record = store.load(MID)
        self.assertEqual(record["launcher_version"], "2.4.0")
        self.assertEqual(record["last_event_source"], "end")
        live = scope.scope_dir(self.state_root, MID)
        bundle, docs, layer = self.merged(CURRENT_ASSETS, declaration)
        parts = transition.RuntimeParts(docs=docs, prompt_bodies=bundle.prompt_bodies,
            hook_command=HOOK, token_helper_command=TOKEN_HELPER, environ=self.environ,
            managed_root=self.root / "managed", worktree_available=lambda _cwd: True)
        reader = SimpleNamespace(session_store=store, converge_parts=lambda: parts,
            catalog=bundle, environ=self.environ, home=self.home,
            operator_snapshot=lambda: SimpleNamespace(layer=layer),
            background_liveness=lambda: sessions.BackgroundLiveness(True, frozenset()),
            report_value=lambda _key, collect: collect())
        self.fixture = SimpleNamespace(store=store, record=record, live=live, reader=reader, parts=parts)
        problems, attention, _info = self.assess()
        expected = transition.expected_plan(record, docs=docs, prompt_bodies=bundle.prompt_bodies,
            state_root=self.state_root, hook_command=HOOK, token_helper_command=TOKEN_HELPER,
            live=live, worktree_available=True, environ=self.environ, managed_root=self.root / "managed")
        self.fixture.expected = expected
        if expected.plan is None:
            if report:
                print(json.dumps({"scenario": scenario, "problems": list(map(str, problems)),
                                  "attention": list(map(str, attention)), "deferred": expected.reason,
                                  "recorded_lead": record["applied"]["lead"],
                                  "recorded_agents": record["applied"]["agents"]}, indent=2))
            if assert_lazy:
                self.assertEqual(problems, [])
            return
        drift = scope.live_drift(live, expected.plan)
        old_bundle, old_docs, _old_layer = self.merged(self.previous / "src/charmer/data", declaration)
        control = transition.expected_plan(record, docs=old_docs, prompt_bodies=old_bundle.prompt_bodies,
            state_root=self.state_root, hook_command=HOOK, token_helper_command=TOKEN_HELPER,
            live=live, worktree_available=True, environ=self.environ, managed_root=self.root / "managed")
        self.assertIsNotNone(control.plan, control.reason)
        settings_skill = [line for line in drift if line.startswith((scope.SETTINGS_RELPATH, scope.SKILL_RELPATH))]
        details = {
            "scenario": scenario,
            "problems": list(map(str, problems)), "attention": list(map(str, attention)), "drift": drift,
            "launch_files_proven": expected.launch_files_kept,
            "generation_before": (live / scope.LINEUP_GEN).read_text().strip(),
            "generation_expected": expected.plan.other_files[scope.LINEUP_GEN].decode().strip(),
            "lineup_format_changed_with_unchanged_catalog": (
                (live / scope.LINEUP_MD).read_bytes() != control.plan.other_files[scope.LINEUP_MD]),
            "lineup_diff": list(difflib.unified_diff((live / scope.LINEUP_MD).read_text().splitlines(),
                expected.plan.other_files[scope.LINEUP_MD].decode().splitlines(), lineterm="")),
            "settings_changes": settings_changes(strict_json.load(live / scope.SETTINGS_RELPATH), expected.plan.settings),
            "skill_diff": list(difflib.unified_diff((live / scope.SKILL_RELPATH).read_text().splitlines(),
                expected.plan.other_files[scope.SKILL_RELPATH].decode().splitlines(), lineterm="")),
            "skill_policy_full": scope.skill_policy_drift(live, expected.plan, drift),
            "fork_policy_full": scope.fork_policy_drift(live, expected.plan, drift),
            "skill_policy_settings_and_skill_only": scope.skill_policy_drift(live, expected.plan, settings_skill),
        }
        if report:
            print(json.dumps(details, indent=2))
        if assert_lazy:
            self.assertEqual(problems, [], f"genuine 2.4.0 scope must be lazy, not BLOCK: {drift}")

    def assess(self):
        with mock.patch.object(compiler, "git_work_tree", return_value=True):
            return doctor._check_scope_integrity(self.fixture.reader, self.fixture.store.load(MID))

    def scope_bytes(self):
        return {str(path.relative_to(self.fixture.live)): path.read_bytes()
                for path in self.fixture.live.rglob("*") if path.is_file()}

    def reject_edits(self, **case):
        self.check_previous_scope(**case, assert_lazy=False, report=False)
        before = self.scope_bytes()
        live = self.fixture.live
        for mutation in ("unknown-text", "self-consistent-text", "other-rendering", "generation",
                         "digest", "other-file", "generated-file", "settings", "deny"):
            with self.subTest(mutation=mutation):
                if mutation in ("unknown-text", "self-consistent-text", "other-rendering"):
                    text = before[scope.LINEUP_MD] + b"\nUnrecognized instructions.\n"
                    if mutation == "other-rendering":
                        text = text.replace(b"## Lead", b"## Changed lead")
                    state.atomic_write(live / scope.LINEUP_MD, text)
                    if mutation != "unknown-text":
                        state.atomic_write(live / scope.LINEUP_GEN, scope.lineup_gen_line(1, text).encode())
                elif mutation == "generation":
                    state.atomic_write(live / scope.LINEUP_GEN, scope.lineup_gen_line(2, before[scope.LINEUP_MD]).encode())
                elif mutation == "digest":
                    state.atomic_write(live / scope.LINEUP_GEN, b"1 000000000000\n")
                elif mutation == "other-file":
                    state.atomic_write(live / "unrecognized.txt", b"not generated\n")
                elif mutation == "generated-file":
                    name = next((name for name in before if name.startswith(".claude/agents/")), scope.LEAD_SET_JSON)
                    state.atomic_write(live / name, before[name] + b"\n")
                else:
                    document = strict_json.loads(before[scope.SETTINGS_RELPATH])
                    if mutation == "settings":
                        document["disableWorkflows"] = not document["disableWorkflows"]
                    else:
                        document["permissions"]["deny"].append("Bash(fixture:*)")
                    state.atomic_write(live / scope.SETTINGS_RELPATH, strict_json.canonical_file_bytes(document))
                try:
                    problems, _attention, _info = self.assess()
                    self.assertTrue(problems, f"unknown {mutation} must remain BLOCK")
                finally:
                    for name, data in before.items():
                        state.atomic_write(live / name, data)
                    (live / "unrecognized.txt").unlink(missing_ok=True)

    def check_repair(self, *, deferred, **case):
        self.check_previous_scope(**case, assert_lazy=False, report=False)
        before = self.scope_bytes()
        record = self.fixture.store.read_record_bytes(MID)
        for bulk in (False, True):
            with self.subTest(bulk=bulk):
                output = io.StringIO()
                code = (doctor_actions._doctor_repair_all(self.fixture.reader, output) if bulk else
                        doctor_actions._doctor_repair(self.fixture.reader, MID, output))
                self.assertEqual(code, 0 if bulk or not deferred else 3, output.getvalue())
                self.assertEqual(self.fixture.store.read_record_bytes(MID), record)
                if deferred:
                    self.assertEqual(self.scope_bytes(), before)
                    self.assertIn("deferred", output.getvalue().lower())
                else:
                    after = self.scope_bytes()
                    self.assertEqual({name for name in after if after[name] != before.get(name)},
                                     {scope.LINEUP_MD, scope.LINEUP_GEN, scope.SETTINGS_RELPATH, scope.SKILL_RELPATH})
                    self.assertEqual(after[scope.LEAD_SET_JSON], before[scope.LEAD_SET_JSON])
                    self.assertEqual(sessions.launch_digest(strict_json.loads(after[scope.SETTINGS_RELPATH]),
                                                           after[scope.LEAD_SET_JSON]),
                                     self.fixture.record["launch_fence"])
                    self.assertIn("record authority" if bulk else "recompiled", output.getvalue())
                self.assertEqual(self.assess()[0], [])
        if not deferred:
            after = self.scope_bytes()
            output = io.StringIO()
            self.assertEqual(doctor_actions._doctor_repair(self.fixture.reader, MID, output), 0)
            self.assertIn("matches the record-authoritative compile", output.getvalue())
            self.assertEqual(self.scope_bytes(), after)

    def test_shipped_lead_without_rename_is_lazy(self):
        self.check_previous_scope(renamed=False)

    def test_displaced_operator_lead_is_lazy(self):
        self.check_previous_scope(renamed=True)

    def test_shipped_fable_lead_generation_move_is_lazy(self):
        self.check_previous_scope(lead="fable")

    def test_shipped_fable_agent_generation_move_is_lazy(self):
        self.check_previous_scope(agent="fable")

    def test_displaced_operator_agent_is_lazy(self):
        self.check_previous_scope(agent=OPERATOR_KEY)

    def test_displaced_lead_unknown_edits_stay_blocked(self):
        self.reject_edits(lead=OPERATOR_KEY)

    def test_displaced_agent_unknown_edits_stay_blocked(self):
        self.reject_edits(agent=OPERATOR_KEY)

    def test_moved_lead_unknown_edits_stay_blocked(self):
        self.reject_edits(lead="fable")

    def test_moved_agent_unknown_edits_stay_blocked(self):
        self.reject_edits(agent="fable")

    def test_displaced_lead_repair_converges_without_changing_intent(self):
        self.check_repair(lead=OPERATOR_KEY, deferred=False)

    def test_displaced_agent_repair_converges_without_changing_intent(self):
        self.check_repair(agent=OPERATOR_KEY, deferred=False)

    def test_moved_lead_repair_defers_without_writes(self):
        self.check_repair(lead="fable", deferred=True)

    def test_moved_agent_repair_defers_without_writes(self):
        self.check_repair(agent="fable", deferred=True)
