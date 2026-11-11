"""Fable operator-line upgrades and rollback against packaged 2.4.0 data.

The declaration and records are synthetic, never copied from an operator's
installation. Renders and reloads use fixture seams; no provider or gateway
is contacted. The held-catalog control keeps the local 5.2 line usable.
"""

from __future__ import annotations

import copy
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import test_cli  # module import: no test classes re-exported
import test_rollback_preflight as rollback_tests
from _catalog import SHIPPED_ROOT, served_selectors, uses_shipped_catalog
from _layout import FIXTURES_ROOT
from charmer import (catalog, cli, continuity, endpoint, launch, operator as operator_mod, profile, proxy,
                     render, rollback_preflight as rp, sessions, state, strict_json)
from charmer.cli import doctor as doctor_mod

OLD = FIXTURES_ROOT / "rollback-preflight" / "2.4.0"
KEY = "custom-fable-5-2"
WIRE = "claude-fable-5-2"
SELECTOR = WIRE + "[1m]"
OTHER = "custom-spare"
DECLARATION = {
    "wire_model": WIRE, "display": "Fable 5.2", "generation": "5.2",
    "efforts": ["low", "medium", "high", "xhigh", "max"], "default_effort": "high",
    "capabilities": ["lead", "agents"], "roles": "all",
    "context": {"declared_tokens": 1000000, "source": "docs",
                "source_ref": "https://platform.claude.com/docs/en/models/fable-5-2/overview"},
}
PROFILE = {
    "version": 2, "name": "mine", "description": "Fable line of my own",
    "lead": {"model": KEY, "effort": "xhigh"},
    "agents": {"cm-analyst-strong": {"model": KEY, "effort": "max"},
               "cm-reviewer": {"use": "fable-review"}},
    "native_agents": {"explore": "replace", "general_purpose": "off", "plan": "native"},
    "workflows": "native",
}
BINDINGS = {"version": 1, "bindings": {
    "fable-review": {"model": KEY, "effort": "medium"},
    "fable-spare": {"model": KEY, "effort": "low"},
}}


class FableUpgradeCase(test_cli.OperatorCommandCase):
    def setUp(self) -> None:
        super().setUp()
        # Pin the disposable endpoint before commands can inspect service state.
        endpoint.write_config(self.runtime.home, endpoint.EndpointConfig(port=19327, backend="on-demand"))
        self.runtime.proc_root.mkdir()
        self.old_root = rp.compose_root(SHIPPED_ROOT, rp.read_target(OLD), self.root / "old-assets")
        self.use_catalog(self.old_root)
        state.ensure_private_dir(self.pdir)
        spare = {**copy.deepcopy(DECLARATION), "wire_model": "claude-spare-1", "display": "Spare"}
        state.atomic_write(self.pdir / "anthropic.json", strict_json.pretty_file_bytes({
            "version": 1, "lines": {KEY: copy.deepcopy(DECLARATION), OTHER: spare},
        }))
        self.apply_and_serve()
        self.runtime.bindings.update(lambda doc: doc.update(copy.deepcopy(BINDINGS)), cat=self.runtime.lineup_catalog())
        self.runtime.profiles.save(copy.deepcopy(PROFILE))
        self.runtime.profiles.save({**copy.deepcopy(PROFILE), "name": "second",
                                    "lead": {"use": "fable-spare"}})
        self.record = self.save_v4_session("mine", write_scope=True, last_event_source="end")
        self.before_layer = self.runtime.operator_snapshot().layer
        self.before_state = self.state_bytes()

    def use_catalog(self, root) -> None:
        self.served = set(served_selectors(root)) | {WIRE, "claude-spare-1"}
        self.runtime = cli.Runtime(
            **self._runtime_seams(), asset_root=root, environ=self.runtime.environ, cwd=self.root / "project",
            launch_callback=lambda prepared: self.launches.append(prepared) or 0,
            doctor_callback=lambda _runtime: [], doctor_binary_callback=lambda _contract: ([], ["fixture"]),
            doctor_daemon_callback=self.runtime.doctor_daemon_callback)
        self.runtime.qualify_http = self._qualify_http
        patcher = mock.patch.object(self.runtime, "verify_reload", side_effect=lambda sentinel: proxy.ReloadResult(
            "reloaded", f"gateway: reloaded (sentinel {sentinel})"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def resume(self, record=None):
        record = record or self.runtime.session_store.load(test_cli.FIXED_ID)
        return self.runtime.prepare(
            cli.LaunchTarget("record", None, record["profile"], record["follow"], "Session"),
            action="resume", session_id=record["managed_id"], passthrough=[])

    def commit_resume(self):
        prepared = self.resume()
        self.runtime.launch_callback = None
        self.runtime.execve = mock.Mock(return_value=0)
        # Only the binary is fake. Runtime.perform and perform_launch commit
        # the real resume plan, under their ordinary barriers and CAS checks.
        with mock.patch.object(launch, "PinnedCopy") as pinned:
            pinned.return_value.verify.return_value = SimpleNamespace(inspected_path=Path("/fixture/client"))
            self.assertEqual(self.runtime.perform(prepared), 0)
            pinned.return_value.release.assert_called_once()
        self.runtime.execve.assert_called_once()
        return self.runtime.session_store.load(test_cli.FIXED_ID)

    def resolved(self, name="mine"):
        return profile.resolve(self.runtime.profiles.load(name), self.runtime.lineup_catalog(),
                               bindings=self.runtime.bindings.load()["bindings"], effective=self.runtime.current_effective())

    def rollback(self):
        before = rollback_tests.tree(self.root / "home", self.root / "config", self.root / "state",
                                     self.root / "secrets")
        scratch = rollback_tests.scratch_roots()
        report = rp.check(environ=self.runtime.gateway_environ(), home=self.runtime.home,
                          state_root=self.runtime.session_store.root, current_root=self.runtime.asset_root,
                          target_root=OLD, current="2.5.0", target="2.4.0")
        self.assertEqual(rollback_tests.tree(self.root / "home", self.root / "config", self.root / "state",
                                            self.root / "secrets"), before)
        self.assertEqual(rollback_tests.scratch_roots(), scratch)
        self.assertIsNone(report.unreadable)
        return report


@uses_shipped_catalog
class HeldFableOperatorLineTests(FableUpgradeCase):
    def setUp(self) -> None:
        super().setUp()
        # An explicit held-catalog control: current release documents and
        # client pin, but the genuine 2.4.0 Fable generation and routes.
        documents = rp.read_target(SHIPPED_ROOT)
        for relative, group, key in (("catalog/models.json", "models", "fable"),
                                     ("catalog/retired.json", "retired", "fable51")):
            current = strict_json.loads(documents[relative])
            current[group][key] = strict_json.loads((OLD / relative).read_bytes())[group][key]
            documents[relative] = strict_json.pretty_file_bytes(current)
        providers = strict_json.loads(documents["catalog/providers.json"])
        routes = providers["providers"]["anthropic"]["passthrough_routes"]
        providers["providers"]["anthropic"]["passthrough_routes"] = [row for row in routes if row["name"] != WIRE]
        documents["catalog/providers.json"] = strict_json.pretty_file_bytes(providers)
        self.use_catalog(rp.compose_root(SHIPPED_ROOT, documents, self.root / "held-assets"))

    def test_local_line_is_not_displaced_or_a_collision(self) -> None:
        layer = self.runtime.operator_snapshot().layer
        self.assertEqual(set(layer.lines), {KEY, OTHER})
        self.assertEqual(layer.displaced, {})
        self.assertEqual(operator_mod.blocking_problems(layer), ())
        _blocks, attention, _info = doctor_mod._doctor_operator_report(self.runtime, None)
        self.assertFalse([line for line in attention if "now provided by the catalog" in line], attention)
        self.assertEqual(self.runtime.catalog.lines["fable"]["wire_model"], "claude-fable-5-1")
        self.assertEqual(self.state_bytes(), self.before_state)

    def test_profile_binding_and_record_keep_the_operator_key_and_selector(self) -> None:
        lineup = self.resolved()
        self.assertEqual(lineup.lead.binding.key, KEY)
        self.assertEqual(lineup.lead.binding.selector, SELECTOR)
        self.assertEqual(lineup.agents["cm-reviewer"].binding.key, KEY)
        prepared = self.resume()
        self.assertEqual(prepared.record["applied"]["lead"]["key"], KEY)
        self.assertEqual(prepared.record["applied"]["lead"]["selector"], SELECTOR)
        self.assertEqual(self.runtime.profiles.load("mine"), PROFILE)
        self.assertEqual(self.runtime.bindings.load(), BINDINGS)
        self.assertEqual(self.runtime.session_store.load(test_cli.FIXED_ID), self.record)
        self.assertEqual(self.state_bytes(), self.before_state)

    def test_rollback_keeps_serving_the_declared_or_captured_selector(self) -> None:
        for declaration in (True, False):
            with self.subTest(declaration=declaration):
                if not declaration:
                    operator_mod.write_provider_bytes(self.env, "anthropic", operator_mod.document_bytes({
                        "version": 1, "lines": {OTHER: {**copy.deepcopy(DECLARATION),
                                                         "wire_model": "claude-spare-1", "display": "Spare"}},
                    }))
                rollback_tests.session(self.runtime.session_store.root, test_cli.OTHER_ID, SELECTOR, {})
                self.assertIn(WIRE, self.ledger().aliases)
                self.assertEqual(self.rollback().findings, ())


@uses_shipped_catalog
class MovedFableOperatorLineTests(FableUpgradeCase):
    def setUp(self) -> None:
        super().setUp()
        self.use_catalog(SHIPPED_ROOT)

    def remove(self) -> None:
        self.apply_and_serve()
        code, out, err = self.op(["models", "rm", KEY, "--successor", "fable", "--yes"])
        self.assertEqual(code, 0, out + err)

    def test_displacement_reports_one_attention_with_remedy_and_no_collision(self) -> None:
        layer = self.runtime.operator_snapshot().layer
        self.assertEqual(set(layer.lines), {OTHER})
        self.assertEqual(set(layer.displaced), {KEY})
        self.assertEqual(operator_mod.blocking_problems(layer), ())
        self.assertEqual([problem.code for problem in layer.problems], ["displaced"])
        blocks, attention, _info = doctor_mod._doctor_operator_report(self.runtime, None)
        self.assertFalse(blocks)
        rows = [line for line in attention if "now provided by the catalog" in line]
        self.assertEqual(len(rows), 1, attention)
        self.assertIn(f"operator line {KEY} (anthropic/{WIRE}) is now provided by the catalog as fable", rows[0])
        self.assertIn(f"charmer models rm {KEY} --successor fable", rows[0])
        self.assertIn("they move to fable at their next launch or resume", rows[0])
        self.assertEqual(self.state_bytes(), self.before_state)
        code, out, err = self.op(["models", "show", KEY])
        self.assertEqual(code, 0, err)
        self.assertIn("is now provided by the catalog as fable", out)
        self.assertIn(f"charmer models rm {KEY} --successor fable", out)

    def test_profile_and_named_bindings_resolve_without_rewriting_files(self) -> None:
        for name in ("mine", "second"):
            lineup = self.resolved(name)
            self.assertEqual(lineup.lead.binding.key, "fable")
            self.assertEqual(lineup.lead.binding.selector, SELECTOR)
            self.assertEqual(lineup.agents["cm-analyst-strong"].binding.key, "fable")
            self.assertEqual(lineup.agents["cm-reviewer"].binding.key, "fable")
            self.assertTrue([notice for notice in lineup.notices if notice.code == "retired-successor"])
        self.assertEqual(self.runtime.profiles.load("mine"), PROFILE)
        self.assertEqual(self.runtime.bindings.load(), BINDINGS)
        self.assertEqual(self.state_bytes(), self.before_state)

    def test_next_resume_records_shipped_key_without_a_manual_record_edit(self) -> None:
        self.assertEqual(self.record["applied"]["lead"]["key"], KEY)
        self.assertEqual(self.record["applied"]["lead"]["selector"], SELECTOR)
        self.assertEqual(self.runtime.session_store.load(test_cli.FIXED_ID), self.record)
        record = self.commit_resume()
        self.assertEqual(record["applied"]["lead"]["key"], "fable")
        self.assertEqual(record["applied"]["lead"]["selector"], SELECTOR)
        self.assertEqual(record["applied"]["lead"]["effort"], "xhigh")
        for field in ("managed_id", "runtime_session_id", "runtime_aliases", "created_at", "profile", "follow"):
            self.assertEqual(record.get(field), self.record.get(field), field)
        self.assertEqual(record["launch_epoch"], self.record["launch_epoch"] + 1)
        self.assertIn(KEY, self.runtime.operator_snapshot().layer.displaced)

    def test_removal_rewrites_every_profile_and_binding_preserving_effort(self) -> None:
        profiles = {name: self.runtime.profiles.load(name) for name in ("mine", "second")}
        self.remove()
        for name, old in profiles.items():
            expected = copy.deepcopy(old)
            for slot in (expected["lead"], *expected["agents"].values()):
                if slot.get("model") == KEY:
                    slot["model"] = "fable"
            self.assertEqual(self.runtime.profiles.load(name), expected)
        self.assertEqual(self.runtime.bindings.load(), {"version": 1, "bindings": {
            key: {**spec, "model": "fable"} for key, spec in BINDINGS["bindings"].items()}})
        layer = self.runtime.operator_snapshot().layer
        self.assertEqual((set(layer.lines), set(layer.displaced)), ({OTHER}, set()))
        self.assertEqual(self.ledger().removed[KEY]["successor"], "fable")
        self.assertEqual(self.runtime.session_store.load(test_cli.FIXED_ID), self.record)

    def test_prune_with_fixture_static_registry_removes_only_the_retained_copy(self) -> None:
        """Fixture-only static 5.2 facts, not evidence from a gateway build.

        The shipped-registry test below remains the integration check; this
        fixture isolates the cleanup semantics from the gateway rebase.
        """
        registry = self.root / "fixture-registry"
        shutil.copytree(SHIPPED_ROOT / "registry", registry)
        document = strict_json.loads((registry / "models.json").read_bytes())
        document["claude"] = [row for row in document["claude"] if row["id"] != WIRE] + [{
            "id": WIRE, "display_name": "Claude Fable 5.2", "context_length": 1000000,
            "max_completion_tokens": 128000,
            "thinking": {"zero_allowed": False, "dynamic_allowed": True,
                         "levels": ["low", "medium", "high", "xhigh", "max"]},
        }]
        (registry / "models.json").write_bytes(strict_json.pretty_file_bytes(document))
        self.runtime.environ["CHARMER_REGISTRY_DIR"] = str(registry)
        self.check_prune_cleanup()

    def test_prune_preview_names_shipped_line_and_removes_only_the_retained_copy(self) -> None:
        self.check_prune_cleanup()

    def check_prune_cleanup(self) -> None:
        self.remove()
        self.serve_current()
        before = self.state_bytes()
        code, out, err = self.op(["doctor", "--prune-aliases", WIRE, "--preview"])
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"{WIRE} — the shipped line fable keeps serving it", out)
        self.assertEqual(self.state_bytes(), before)
        code, out, err = self.op(["doctor", "--prune-aliases", WIRE])
        self.assertEqual(code, 0, out + err)
        self.assertNotIn(WIRE, self.ledger().aliases)
        self.assertIn("claude-spare-1", self.ledger().aliases)
        self.assertEqual(self.runtime.session_store.load(test_cli.FIXED_ID), self.record)
        document = proxy.candidate_document(self.runtime.home, environ=self.runtime.gateway_environ(),
                                             asset_root=SHIPPED_ROOT, state_root=self.runtime.session_store.root)
        self.assertIn(WIRE, render.rendered_selectors(document))

    def test_removal_before_resume_keeps_the_recorded_successor(self) -> None:
        self.remove()
        self.assertEqual(self.ledger().removed[KEY]["successor"], "fable")
        self.assertEqual(self.runtime.profiles.load("mine")["lead"]["model"], "fable")
        self.assertEqual(self.resume().record["applied"]["lead"]["key"], "fable")
        self.assertEqual(self.runtime.session_store.load(test_cli.FIXED_ID), self.record)

    def test_live_selector_refuses_removal_even_after_adopting_the_shipped_key(self) -> None:
        for adopted in (False, True):
            with self.subTest(adopted=adopted):
                if adopted:
                    record = self.commit_resume()
                    self.assertEqual(record["applied"]["lead"]["key"], "fable")
                else:
                    self.runtime.session_store.reconcile_runtime(test_cli.FIXED_ID,
                                                                 observed_runtime_id=test_cli.FIXED_ID,
                                                                 source="startup",
                                                                 launch_epoch=self.record["launch_epoch"])
                self.assertNotEqual(self.runtime.session_store.load(test_cli.FIXED_ID)["last_event_source"], "end")
                self.apply_and_serve()
                before = self.state_bytes()
                code, out, err = self.op(["models", "rm", KEY, "--successor", "fable", "--yes"])
                self.assertEqual(code, 1, out + err)
                self.assertIn("live sessions use it (11111111)", err)
                self.assertEqual(self.state_bytes(), before)

    def test_process_only_liveness_blocks_prune_but_not_declaration_removal(self) -> None:
        # Removal currently checks record liveness only. A stale end marker
        # does not block it, but the retained capture cannot be pruned while
        # the daemon still reports the holder live.
        self.runtime.background_liveness = lambda: sessions.BackgroundLiveness(True, frozenset({test_cli.FIXED_ID[:8]}))
        self.remove()
        self.serve_current()
        before = self.state_bytes()
        document = proxy.candidate_document(self.runtime.home, environ=self.runtime.gateway_environ(),
                                             asset_root=SHIPPED_ROOT, state_root=self.runtime.session_store.root)
        self.assertIn(WIRE, render.rendered_selectors(document))
        self.assertIn(WIRE, self.ledger().aliases)
        for flags in (["--preview"], []):
            with self.subTest(flags=flags):
                code, out, err = self.op(["doctor", "--prune-aliases", WIRE, *flags])
                self.assertEqual(code, 1, out + err)
                self.assertIn("11111111", out + err)
                self.assertIn(WIRE, self.ledger().aliases)
                self.assertEqual(self.state_bytes(), before)

    def test_admission_qualification_and_secrets_do_not_transfer_to_shipped_line(self) -> None:
        self.use_catalog(self.old_root)
        self.serve_current()
        for argv in (["models", "admit", KEY], ["models", "qualify", KEY, "--smoke"]):
            code, out, err = self.op(argv, "y\n")
            self.assertEqual(code, 0, out + err)
        schemas = operator_mod.load_schemas(self.old_root)
        evidence = operator_mod.load_evidence(self.runtime.gateway_environ(), schemas)
        self.assertIn(KEY, evidence.lines)
        self.assertIn(KEY, self.ledger().admissions)
        self.assertTrue(self.http_calls)
        before = self.state_bytes()
        self.use_catalog(SHIPPED_ROOT)
        self.assertEqual(self.runtime.lineup_catalog().lines["fable"], self.runtime.catalog.lines["fable"])
        self.assertEqual(self.runtime.catalog.lines["fable"]["context"]["validated_tokens"], 200000)
        self.assertNotIn("fable", self.ledger().admissions)
        self.assertNotIn("fable", self.runtime.current_effective().admitted_lines)
        self.assertEqual(operator_mod.load_evidence(self.runtime.gateway_environ(), schemas), evidence)
        self.assertEqual(self.state_bytes(), before)

    def test_old_fable_scope_keeps_51_route_until_resume(self) -> None:
        self.use_catalog(self.old_root)
        record = self.save_v4_session(
            session_id=test_cli.OTHER_ID, write_scope=True, last_event_source="end",
            target=cli.LaunchTarget("ad-hoc", profile.ad_hoc_direct("fable", "xhigh"), None, False, "Fable"))
        scope_dir = self.runtime.session_store.root / "scopes" / test_cli.OTHER_ID
        old_scope = rollback_tests.tree(scope_dir)
        self.assertEqual(strict_json.loads((scope_dir / "settings.json").read_bytes())["model"],
                         "claude-fable-5-1[1m]")
        self.use_catalog(SHIPPED_ROOT)
        self.apply_and_serve()
        document = proxy.candidate_document(self.runtime.home, environ=self.runtime.gateway_environ(),
                                             asset_root=SHIPPED_ROOT, state_root=self.runtime.session_store.root)
        aliases = document["oauth-model-alias"]["claude"]
        for wire in ("claude-fable-5", "claude-fable-5-1", WIRE):
            self.assertIn({"name": wire, "alias": wire, "force-mapping": True, "fork": True}, aliases)
        self.assertEqual(rollback_tests.tree(scope_dir), old_scope)
        self.assertEqual(self.runtime.session_store.load(test_cli.OTHER_ID), record)
        prepared = self.resume(record)
        self.assertEqual(prepared.record["applied"]["lead"]["selector"], SELECTOR)
        self.assertEqual(prepared.result.scope_plan.settings["env"]["ANTHROPIC_DEFAULT_FABLE_MODEL"], SELECTOR)
        fresh = self.prepare_direct_v4("fable")
        self.assertEqual(fresh.result.scope_plan.settings["model"], SELECTOR)
        self.assertEqual(fresh.result.scope_plan.settings["env"]["ANTHROPIC_DEFAULT_FABLE_MODEL"], SELECTOR)

    def test_claude_and_max_seeds_follow_without_reseed_or_effort_changes(self) -> None:
        old = catalog.load_catalog(self.old_root)
        for name in ("claude", "max"):
            with self.subTest(seed=name):
                self.assertEqual(self.runtime.catalog.seed_profiles[name], old.seed_profiles[name])
                self.runtime.profiles.save(copy.deepcopy(old.seed_profiles[name]))
                lineup = self.resolved(name)
                specs = {"lead": old.seed_profiles[name]["lead"], **old.seed_profiles[name]["agents"]}
                actual = {"lead": lineup.lead, **lineup.agents}
                self.assertEqual(set(actual), set(specs))
                for role, spec in specs.items():
                    self.assertEqual(actual[role].binding.key, spec["model"])
                    self.assertEqual(actual[role].binding.effort, spec["effort"])
                    if spec["model"] == "fable":
                        self.assertEqual(actual[role].binding.selector, SELECTOR)
                self.assertEqual(self.runtime.profiles.seed_updates(), [])
                self.assertEqual(self.runtime.profiles.load(name), old.seed_profiles[name])


@uses_shipped_catalog
class FableRollbackTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="charmer-fable-rollback-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = state.ensure_private_dir(self.root / "home")
        self.state_root = self.root / "state"
        self.env = {"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.root / "config"),
                    "XDG_STATE_HOME": str(self.root / "xdg-state"), "CHARMER_SECRET_ENV": str(self.root / "absent.env")}
        endpoint.write_config(self.home, endpoint.EndpointConfig(port=19327, backend="on-demand"))
        self.old_root = rp.compose_root(SHIPPED_ROOT, rp.read_target(OLD), self.root / "old-assets")

    def check(self):
        before, scratch = rollback_tests.tree(self.root), rollback_tests.scratch_roots()
        report = rp.check(environ=self.env, home=self.home, state_root=self.state_root,
                          current_root=SHIPPED_ROOT, target_root=OLD, current="2.5.0", target="2.4.0")
        self.assertEqual(rollback_tests.tree(self.root), before)
        self.assertEqual(rollback_tests.scratch_roots(), scratch)
        self.assertIsNone(report.unreadable)
        return report

    def live(self, *, agent=False, ended=False, fence_only=False):
        lead = "claude-opus-5-5[1m]" if agent or fence_only else SELECTOR
        rollback_tests.session(self.state_root, test_cli.FIXED_ID, lead,
                               {"cm-analyst": SELECTOR} if agent else {}, live=not ended,
                               fence=[lead, SELECTOR])
        # Include the stable key: ended records resume to that generation
        # in the target, not to the recorded selector.
        path = self.state_root / "sessions" / f"{test_cli.FIXED_ID}.json"
        document = strict_json.loads(path.read_bytes())
        document["applied"]["lead"]["key"] = "fable" if lead == SELECTOR else "opus"
        rollback_tests.write_json(path, document)

    def declaration(self, *, present=True):
        operator_mod.write_provider_file(self.env, "anthropic", {
            "version": 1, "lines": {KEY: copy.deepcopy(DECLARATION)} if present else {}})

    def test_live_52_without_a_declaration_or_capture_is_a_finding(self) -> None:
        for agent in (False, True):
            with self.subTest(agent=agent):
                self.live(agent=agent)
                report = self.check()
                self.assertEqual(report.findings, (rp.Finding(
                    rp.SESSIONS, f"session 11111111 uses {WIRE}, which 2.4.0 does not serve",
                    f"exit it first (end the session; after the rollback, charmer -r {test_cli.FIXED_ID} "
                    "resumes it on 2.4.0's models)"),))
                self.assertEqual(report.unchanged, 0)

    def test_declaration_or_retained_capture_keeps_52_served_under_240(self) -> None:
        self.declaration()
        self.live()
        self.assertEqual(self.check().findings, ())
        proxy.render_runtime_config(self.home, environ={**self.env, "CHARMER_ASSETS": str(self.old_root)},
                                    state_root=self.state_root)
        self.declaration(present=False)
        ledger = operator_mod.load_ledger(self.env, operator_mod.load_schemas(SHIPPED_ROOT))
        self.assertIn(WIRE, ledger.aliases)
        document = proxy.candidate_document(self.home, environ=self.env, asset_root=self.old_root,
                                             state_root=self.state_root)
        self.assertIn(WIRE, render.rendered_selectors(document))
        self.assertEqual(self.check().findings, ())

    def test_after_removal_and_prune_the_session_finding_returns(self) -> None:
        self.declaration()
        proxy.render_runtime_config(self.home, environ={**self.env, "CHARMER_ASSETS": str(self.old_root)},
                                    state_root=self.state_root)
        self.declaration(present=False)
        schemas = operator_mod.load_schemas(SHIPPED_ROOT)
        ledger = operator_mod.load_ledger(self.env, schemas)
        operator_mod.write_ledger(self.env, operator_mod.apply_capture_prune(
            operator_mod.ledger_document(ledger), [WIRE], 44), schemas)
        persisted = continuity.read(self.home)
        continuity.write(self.home, continuity.apply_prune(persisted, continuity.PrunePlan((WIRE,), ()), 44))
        self.live()
        self.assertEqual([finding.group for finding in self.check().findings], [rp.SESSIONS])

    def test_ended_stable_key_resumes_on_51_without_a_live_session_finding(self) -> None:
        self.live(ended=True)
        self.assertEqual(self.check().findings, ())
        target = catalog.load_catalog(self.old_root)
        self.assertEqual(target.resolve_key("fable").key, "fable")
        self.assertEqual(target.lines["fable"]["selector"], "claude-fable-5-1[1m]")

    def test_picker_fence_only_is_not_a_session_finding(self) -> None:
        self.live(fence_only=True)
        self.assertEqual(self.check().findings, ())


if __name__ == "__main__":
    unittest.main()
