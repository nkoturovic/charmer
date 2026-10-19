"""Recorded 1.0.0 scopes survive presentation-only compiler changes.

The executable changes since are the fork-subagent deny (``Agent(fork)``,
compiled from the 2.1.302 pin on) and the model fence's failure policy
(``onFailure: block`` and the fence's own timeout on the PreModelSwitch
hook): a baseline scope differs
from the current compile by exactly those, which doctor reports as lazy state
(Attention, applied at the next resume or by repair) and repair writes,
nothing else.

The two frozen outputs were built with the actual source compiler at
BASELINE_REF (git archive of src/, private HOME, no Runtime or execution).
That source differs from release 1.0.0 only in version metadata; the record
therefore names 1.0.0. build_baseline is the pure, shared reproduction input,
not a mock old renderer. The checked-in bytes keep tests independent of git
history (including source archives and shallow release checkouts).

The baselines were compiled under the product's earlier name; charmer
compiles the same scope under its own. The comparisons therefore read the
frozen bytes through the rename's name map (:func:`charmer_names`: the
earlier name and environment prefix, from charmer.legacy) and recompute
what the record derives from the renamed files (the launch fence, the
applied hash and the diagnostics' generation line). What the retention judges, the
presentation of the diagnostics, is untouched by the rename. A frozen
scope read without the map translates its recorded selectors through the
catalog's renamed map, so its record compiles the same bindings under their
new names, from which the live scope differs until the next resume
(``test_the_frozen_scope_under_its_earlier_names_applies_at_the_next_resume``).
"""

from __future__ import annotations

import copy
import dataclasses
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from charmer import (catalog, compiler, legacy, operator, profile, scope, sessions, settings, state, strict_json,
                     transition)
from charmer.cli import doctor, text as cli_text
from _catalog import FIXTURE_ROOT
from _layout import FIXTURES_ROOT


BASELINE_REF = "ec821bab8ec3fb7a2d31554c1abd337ba82e6da9"
FIXTURES = FIXTURES_ROOT / "permissive"
MID = "11111111-1111-4111-8111-111111111111"
HOOK = "/fixture/bin/charmer-hook-3"
HELPER = "/fixture/bin/charmer-gateway-token"
OPERATOR_KEY = "custom-acme-large"


def inputs(scenario):
    bundle = catalog.load_catalog(FIXTURE_ROOT)
    docs = bundle.docs
    document = profile.ad_hoc_direct("opus")
    document["name"] = "compatibility"
    document["agents"] = {
        "cm-reviewer": {"model": "gpt55", "effort": "high"},
        "cm-reviewer-strong": {"model": "grok46", "effort": "xhigh"},
        "cm-implementer": {"model": "sol", "effort": "high"},
    }
    admitted = []
    if scenario == "unknown-family":
        declaration = strict_json.load(FIXTURE_ROOT.parent / "operator" / "providers.d" / "acme.json")
        declaration["provider"]["independence_family"] = "unknown"
        declaration["lines"] = {OPERATOR_KEY: declaration["lines"][OPERATOR_KEY]}
        declaration["lines"][OPERATOR_KEY]["context"]["declared_tokens"] = 1_000_000
        layer = operator.validate_layer(
            docs, {"acme": strict_json.pretty_file_bytes(declaration)}, schemas=operator.load_schemas(FIXTURE_ROOT))
        if layer.problems:
            raise AssertionError([problem.text() for problem in layer.problems])
        docs = operator.merge_docs(docs, layer)
        document["lead"] = {"model": OPERATOR_KEY, "effort": "high"}
        admitted = [OPERATOR_KEY]
    elif scenario != "catalog":
        raise ValueError(scenario)
    lcat = profile.LineupCatalog.from_docs(docs)
    eff = settings.effective({"version": 1, "admitted_lines": admitted},
                             provider_ids=lcat.providers, line_keys=lcat.lines)
    return docs, bundle.prompt_bodies, document, lcat, eff


def build_baseline(scenario):
    """Run this function with the archived baseline's src first on PYTHONPATH."""

    docs, prompts, document, lcat, eff = inputs(scenario)
    lineup = profile.resolve(document, lcat, effective=eff)
    result = compiler.compile_lineup_launch(
        docs=docs, prompt_bodies=prompts, lineup=lineup, effective=eff,
        session_action=compiler.build_fresh(MID), lineup_generation=1,
        state_root=Path("/fixture/state"), scope_dir=Path("/fixture/state/scopes") / MID,
        hook_command=HOOK, token_helper_command=HELPER,
        worktree_available=True, launch_environ={},
    )
    plan = result.scope_plan
    context = compiler.lead_set_context(result.fence, eff.compaction_percent,
                                        ceiling=profile.window_ceiling(eff))
    applied = sessions.applied_from_lineup(
        lineup, context=context, settings_snapshot=settings.snapshot(eff), no_subagents=False)
    record = sessions.make_v4_record(
        managed_id=MID, cwd="/fixture/project", profile=document["name"], follow=False,
        applied=applied, lineup_generation=1, lead_class=lineup.lead_class,
        catalog_version=lcat.catalog_version, catalog_hash=strict_json.bundle_digest(docs),
        launcher_version="1.0.0", launch_epoch=0,
        launch_fence=sessions.launch_digest(plan.settings, plan.other_files[scope.LEAD_SET_JSON]),
        scope_lead=sessions.lead_ref(applied["lead"]), now="2026-10-05T00:00:00Z",
    )
    return {
        "source_ref": BASELINE_REF, "source_release": "1.0.0", "scenario": scenario,
        "record": record,
        "agent_classes": {rid: agent.binding.client_context_tokens for rid, agent in lineup.agents.items()},
        "settings": plan.settings,
        "agent_files": {name: data.decode("utf-8") for name, data in plan.agent_files.items()},
        "other_files": {name: data.decode("utf-8") for name, data in plan.other_files.items()},
    }


def charmer_names(value):
    """A frozen 1.0.0 artifact under the rename's name map (strings only)."""

    if isinstance(value, str):
        return value.replace(legacy.NAME, "charmer").replace(legacy.ENVIRONMENT_PREFIX, "CHARMER_")
    if isinstance(value, dict):
        return {charmer_names(key): charmer_names(item) for key, item in value.items()}
    if isinstance(value, list):
        return [charmer_names(item) for item in value]
    return value


def renamed_baseline(scenario):
    """The frozen baseline of ``scenario`` under charmer's names (see the module docstring)."""

    frozen = strict_json.load(FIXTURES / f"{scenario}-1.0.0.json")
    fixture = charmer_names(frozen)
    record = fixture["record"]
    lineup_md = fixture["other_files"][scope.LINEUP_MD].encode("utf-8")
    fixture["other_files"][scope.LINEUP_GEN] = scope.lineup_gen_line(record["lineup_generation"], lineup_md)
    record["applied_hash"] = strict_json.bundle_digest(record["applied"])
    record["launch_fence"] = sessions.launch_digest(
        fixture["settings"], fixture["other_files"][scope.LEAD_SET_JSON].encode("utf-8"))
    return frozen, fixture


def without_fork_deny(settings):
    """``settings`` without the fork-subagent deny and the model fence's
    failure policy (its executable changes since 1.0.0)."""

    permissions = settings["permissions"]
    deny = [rule for rule in permissions["deny"] if rule != compiler.FORK_AGENT_DENY]
    hooks = copy.deepcopy(settings["hooks"])
    for group in hooks["PreModelSwitch"]:
        for hook in group["hooks"]:
            hook.pop("onFailure", None)
            hook["timeout"] = scope.HOOK_TIMEOUT_SECONDS
    return {**settings, "permissions": {**permissions, "deny": deny}, "hooks": hooks}


class ScopeCompatibilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="scope-compatibility-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def load(self, scenario="unknown-family", *, renamed=True):
        frozen, fixture = renamed_baseline(scenario)
        if not renamed:
            fixture = frozen
        self.assertEqual(fixture["source_ref"], BASELINE_REF)
        self.record = fixture["record"]
        self.docs, self.prompts, _, _, _ = inputs(scenario)
        self.old_plan = scope.ScopePlan(
            settings=fixture["settings"],
            agent_files={name: data.encode("utf-8") for name, data in fixture["agent_files"].items()},
            other_files={name: data.encode("utf-8") for name, data in fixture["other_files"].items()},
        )
        self.live = scope.write_scope(self.root, MID, self.old_plan)
        return fixture

    def expected(self):
        return transition.expected_plan(
            self.record, docs=self.docs, prompt_bodies=self.prompts, state_root=self.root,
            hook_command=HOOK, token_helper_command=HELPER, live=self.live,
            worktree_available=True, environ={}, managed_root=self.root / "managed",
        )

    def tree(self):
        return {str(path.relative_to(self.live)): path.read_bytes()
                for path in self.live.rglob("*") if path.is_file()}

    def test_actual_baseline_scopes_keep_all_files_and_record_intent(self):
        for scenario in ("catalog", "unknown-family"):
            with self.subTest(scenario=scenario):
                fixture = self.load(scenario)
                before, record = self.tree(), copy.deepcopy(self.record)
                result = self.expected()
                self.assertIsNotNone(result.plan, result.reason)
                self.assertTrue(result.launch_files_kept)
                self.assertEqual(result.diagnostics_kept, scenario == "unknown-family")
                # Only the fork deny moves: everything else is the baseline's.
                drift = scope.live_drift(self.live, result.plan)
                self.assertEqual(drift, [f"{scope.SETTINGS_RELPATH} content differs"])
                self.assertTrue(scope.fork_policy_drift(self.live, result.plan, drift))
                self.assertIn(compiler.FORK_AGENT_DENY, result.plan.settings["permissions"]["deny"])
                self.assertNotIn(compiler.FORK_AGENT_DENY, self.old_plan.settings["permissions"]["deny"])
                self.assertEqual(without_fork_deny(result.plan.settings), self.old_plan.settings)
                self.assertEqual(scope.plan_hash(dataclasses.replace(result.plan, settings=self.old_plan.settings)),
                                 scope.plan_hash(self.old_plan))
                self.assertEqual(self.tree(), before)
                self.assertEqual(self.record, record)
                # Actual old class reporting, not fixtures emitted by the new compiler.
                self.assertEqual(fixture["agent_classes"]["cm-reviewer"], 258400)
                self.assertEqual(fixture["agent_classes"]["cm-reviewer-strong"], 500000)
                if scenario == "unknown-family":
                    old = self.old_plan.other_files[scope.LINEUP_MD]
                    self.assertIn(b"same-family", old)
                    self.assertNotIn(b"independence unknown", old)
                    self.assertIn(b"independence unknown", result.catalog_plan.other_files[scope.LINEUP_MD])

    def test_doctor_keeps_baseline_rendering_as_attention_but_blocks_edited_text(self):
        self.load()
        parts = transition.RuntimeParts(
            docs=self.docs, prompt_bodies=self.prompts, hook_command=HOOK,
            token_helper_command=HELPER, environ={}, managed_root=self.root / "managed")
        reader = SimpleNamespace(
            session_store=SimpleNamespace(root=self.root), converge_parts=lambda: parts,
            catalog=SimpleNamespace(bundle_sha256=self.record["catalog_hash"]), environ={})
        before = self.tree()
        with mock.patch.object(compiler, "git_work_tree", return_value=True):
            problems, attention, info = doctor._check_scope_integrity(reader, self.record)
        self.assertEqual(problems, [])
        self.assertTrue(any("proven scope is kept until the next resume" in line for line in attention))
        # The fork deny it predates is lazy state, never BLOCK.
        self.assertIn(cli_text.FORK_POLICY_DRIFT.format(m8=MID[:8], mid=MID), attention)
        self.assertEqual(self.tree(), before)
        altered = self.old_plan.other_files[scope.LINEUP_MD] + b"Injected instructions.\n"
        state.atomic_write(self.live / scope.LINEUP_MD, altered)
        state.atomic_write(self.live / scope.LINEUP_GEN, scope.lineup_gen_line(1, altered).encode())
        with mock.patch.object(compiler, "git_work_tree", return_value=True):
            problems, _attention, _info = doctor._check_scope_integrity(reader, self.record)
        self.assertTrue(any("scope differs" in line for line in problems))

    def test_doctor_composes_the_fork_deny_with_a_default_mode_difference(self):
        """A baseline scope without the fork deny whose permission default
        mode also differs from the compile is lazy on both counts (both
        Attention lines, no problem); another settings or deny change
        alongside stays BLOCK."""

        self.load()
        parts = transition.RuntimeParts(
            docs=self.docs, prompt_bodies=self.prompts, hook_command=HOOK,
            token_helper_command=HELPER, environ={}, managed_root=self.root / "managed")
        reader = SimpleNamespace(
            session_store=SimpleNamespace(root=self.root), converge_parts=lambda: parts,
            catalog=SimpleNamespace(bundle_sha256=self.record["catalog_hash"]), environ={})
        path = self.live / scope.SETTINGS_RELPATH
        document = strict_json.loads(path.read_bytes())
        document["permissions"]["defaultMode"] = "acceptEdits"
        state.atomic_write(path, strict_json.canonical_file_bytes(document))

        def check(settings_document):
            state.atomic_write(path, strict_json.canonical_file_bytes(settings_document))
            with mock.patch.object(compiler, "git_work_tree", return_value=True):
                return doctor._check_scope_integrity(reader, self.record)[:2]

        problems, attention = check(document)
        self.assertEqual(problems, [])
        self.assertIn(cli_text.FORK_POLICY_DRIFT.format(m8=MID[:8], mid=MID), attention)
        self.assertIn(cli_text.DEFAULT_MODE_DRIFT.format(m8=MID[:8], mid=MID), attention)
        extra_deny = copy.deepcopy(document)
        extra_deny["permissions"]["deny"].append("Bash(rm:*)")
        edited_key = copy.deepcopy(document)
        edited_key["workflowSizeGuideline"] = "large"
        for label, edited in (("deny", extra_deny), ("settings", edited_key)):
            with self.subTest(alongside=label):
                problems, attention = check(edited)
                self.assertTrue(any("scope differs" in line for line in problems), problems)
                self.assertNotIn(cli_text.FORK_POLICY_DRIFT.format(m8=MID[:8], mid=MID), attention)

    def test_converge_leaves_proven_baseline_files_and_record_unchanged(self):
        self.load()
        store = sessions.SessionStore(self.root, strict_json.load(FIXTURE_ROOT / "schemas" / "session.schema.json"))
        state.atomic_write(self.root / sessions.STATE_MARKER, b"4\n")
        store.save(self.record)
        before, record = self.tree(), store.read_record_bytes(MID)
        report = transition.converge(
            self.root, store, MID,
            runtime_parts=transition.RuntimeParts(
                docs=self.docs, prompt_bodies=self.prompts, hook_command=HOOK,
                token_helper_command=HELPER, environ={}, managed_root=self.root / "managed",
                worktree_available=lambda _cwd: True,
            ),
        )
        # Repair writes the fork deny the baseline predates, and only that.
        self.assertTrue(any("recompiled from the record and the installed catalog" in line for line in report),
                        report)
        after = self.tree()
        self.assertEqual({name for name in after if after[name] != before.get(name)}, {scope.SETTINGS_RELPATH})
        repaired = strict_json.loads(after[scope.SETTINGS_RELPATH])
        self.assertIn(compiler.FORK_AGENT_DENY, repaired["permissions"]["deny"])
        self.assertEqual(without_fork_deny(repaired), self.old_plan.settings)
        self.assertEqual(store.read_record_bytes(MID), record)

    def test_normal_resume_compile_converges_to_current_diagnostics(self):
        self.load()
        docs, prompts, document, lcat, eff = inputs("unknown-family")
        lineup = profile.resolve(document, lcat, effective=eff)
        plan = scope.compile_lineup_scope(
            lineup, lcat, eff, prompts, scope.catalog_meta_v2(docs),
            lineup_generation=2, managed_id=MID, hook_command=HOOK,
            token_helper_command=HELPER, worktree_available=True,
        )
        self.assertIn(b"independence unknown", plan.other_files[scope.LINEUP_MD])
        self.assertNotEqual(plan.other_files[scope.LINEUP_MD], self.old_plan.other_files[scope.LINEUP_MD])
        self.assertEqual(lineup.agents["cm-reviewer"].binding.client_context_tokens, 200000)
        self.assertEqual(lineup.agents["cm-reviewer-strong"].binding.client_context_tokens, 200000)
        self.record["lineup_generation"] = 2
        self.record["launcher_version"] = "1.1.0"
        self.record["launch_fence"] = sessions.launch_digest(plan.settings, plan.other_files[scope.LEAD_SET_JSON])
        scope.swap_scope(self.root, MID, plan)
        result = self.expected()
        self.assertFalse(result.diagnostics_kept)
        self.assertEqual(scope.live_drift(self.live, result.plan), [])

    def test_edited_markdown_with_recomputed_hash_is_not_legacy_proof(self):
        self.load()
        altered = self.old_plan.other_files[scope.LINEUP_MD].replace(
            b"Review rounds: at most 2", b"Review rounds: at most 9")
        self.assertNotEqual(altered, self.old_plan.other_files[scope.LINEUP_MD])
        state.atomic_write(self.live / scope.LINEUP_MD, altered)
        state.atomic_write(self.live / scope.LINEUP_GEN,
                           scope.lineup_gen_line(self.record["lineup_generation"], altered).encode())
        result = self.expected()
        self.assertFalse(result.diagnostics_kept)
        self.assertTrue(any(scope.LINEUP_MD in drift for drift in scope.live_drift(self.live, result.plan)))

    def test_bad_generation_missing_and_unsafe_diagnostics_are_not_kept(self):
        for change in ("generation", "missing", "mode", "symlink"):
            with self.subTest(change=change):
                self.load()
                path = self.live / scope.LINEUP_GEN
                if change == "generation":
                    state.atomic_write(path, scope.lineup_gen_line(2, self.old_plan.other_files[scope.LINEUP_MD]).encode())
                elif change == "missing":
                    path.unlink()
                elif change == "mode":
                    path.chmod(0o644)
                else:
                    target = self.root / "diagnostics-target"
                    state.atomic_write(target, path.read_bytes())
                    path.unlink()
                    path.symlink_to(target)
                result = self.expected()
                self.assertFalse(result.diagnostics_kept)
                self.assertTrue(scope.live_drift(self.live, result.plan))

    def test_agent_model_tools_and_isolation_drift_still_block(self):
        for rid, old, new in (
            ("cm-reviewer", b"model: gpt-multi-gpt55-high", b"model: arbitrary-model"),
            ("cm-reviewer", b"disallowedTools: Edit, Write", b"disallowedTools: Edit"),
            ("cm-implementer", b"isolation: worktree", b"isolation: none"),
        ):
            with self.subTest(field=old):
                self.load()
                name = next(name for name in self.old_plan.agent_files if name.endswith(f"/{rid}.md"))
                data = self.old_plan.agent_files[name]
                altered = data.replace(old, new)
                self.assertNotEqual(data, altered)
                state.atomic_write(self.live / name, altered)
                result = self.expected()
                self.assertTrue(any(name in drift for drift in scope.live_drift(self.live, result.plan)))

    def test_launch_fence_corruption_never_enables_legacy_retention(self):
        for filename in (scope.SETTINGS_RELPATH, scope.LEAD_SET_JSON):
            with self.subTest(filename=filename):
                self.load()
                path = self.live / filename
                if filename == scope.SETTINGS_RELPATH:
                    changed = strict_json.loads(path.read_bytes())
                    changed["availableModels"].remove(self.record["applied"]["agents"]["cm-reviewer"]["selector"])
                    state.atomic_write(path, strict_json.pretty_file_bytes(changed))
                else:
                    state.atomic_write(path, path.read_bytes() + b" ")
                result = self.expected()
                self.assertFalse(result.launch_files_kept)
                self.assertFalse(result.diagnostics_kept)
                self.assertTrue(scope.live_drift(self.live, result.plan))

    def test_only_exact_source_release_qualifies(self):
        self.load()
        for release in ("1.1.0", "1.1.0-dev", "1.0.1", "1.0.0-dev", None):
            with self.subTest(release=release):
                self.record["launcher_version"] = release
                result = self.expected()
                self.assertFalse(result.diagnostics_kept)
                self.assertTrue(scope.live_drift(self.live, result.plan))

    def test_the_frozen_scope_under_its_earlier_names_applies_at_the_next_resume(self):
        # Read without the name map, the 1.0.0 record names the earlier
        # selectors: the record-authoritative compile translates them (the
        # same bindings, no "moved" refusal) and compiles them under their new
        # names; the live scope, under the earlier names, differs from it
        # until the next resume recompiles it.
        for scenario in ("catalog", "unknown-family"):
            with self.subTest(scenario=scenario):
                _frozen, renamed = renamed_baseline(scenario)
                self.load(scenario, renamed=False)
                recorded = {binding["selector"] for binding in self.record["applied"]["agents"].values()}
                self.assertTrue(any(selector.startswith(legacy.LEGACY_SELECTOR_PREFIX) for selector in recorded))
                result = self.expected()
                self.assertIsNotNone(result.plan, result.reason)
                # The bindings the record names, under their new selectors: the
                # agent the rename moved is compiled exactly as the renamed scope.
                agent = ".claude/agents/cm-reviewer-strong.md"
                self.assertEqual(result.plan.agent_files[agent], renamed["agent_files"][agent].encode("utf-8"))
                self.assertNotIn(legacy.LEGACY_SELECTOR_PREFIX.encode(), result.plan.agent_files[agent])
                self.assertIn(f"{agent} content differs", scope.live_drift(self.live, result.plan))

    def test_changed_binding_or_role_policy_is_not_presentation_compatibility(self):
        self.load()
        self.record["applied"]["agents"]["cm-reviewer"]["selector"] = "not-the-catalog-selector"
        result = self.expected()
        self.assertIsNone(result.plan)
        self.assertIn("recorded not-the-catalog-selector", result.reason)
        self.load()
        self.record["applied"]["native_agents"]["general_purpose"] = "off"
        result = self.expected()
        self.assertTrue(scope.live_drift(self.live, result.plan))
