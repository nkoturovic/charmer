"""Catalog generation moves against genuine 2.5.0 data; no provider calls."""
from __future__ import annotations

import copy
import unittest

import test_cli
import test_fable_catalog_upgrade as fable
import test_rollback_preflight as rollback_tests
from _catalog import SHIPPED_ROOT, uses_shipped_catalog
from _layout import FIXTURES_ROOT
from charmer import (catalog, endpoint, operator as operator_mod, profile, proxy,
                     render, rollback_preflight as rp, state)
from charmer.cli import doctor as doctor_mod

OLD = FIXTURES_ROOT / "rollback-preflight" / "2.5.0"
KEY = "custom-gpt-6-2-sol"
WIRE = "gpt-6.2-sol"
LEVELS = ("low", "medium", "high", "xhigh", "max")
ALIASES = {f"{KEY}-{level}" for level in LEVELS}
OTHER = "custom-spare"
DECLARATION = {
    "wire_model": WIRE, "display": "GPT-6.2 Sol", "generation": "6.2",
    "efforts": {level: f"reasoning-effort-{level}" for level in LEVELS}, "default_effort": "high",
    "capabilities": ["lead", "agents"], "roles": "all",
    "context": {"declared_tokens": 872000, "source": "docs",
                "source_ref": "https://developers.openai.com/api/docs/models/gpt-6.2-sol"},
}
SPARE = {**DECLARATION, "wire_model": "gpt-spare", "display": "Spare"}
PROFILE = {
    "version": 2, "name": "mine", "description": "A temporary GPT line",
    "lead": {"model": KEY, "effort": "max"},
    "agents": {"cm-reviewer": {"use": "gpt-review"}},
    "native_agents": {"explore": "replace", "general_purpose": "off", "plan": "native"},
    "workflows": "native",
}
BINDINGS = {"version": 1, "bindings": {"gpt-review": {"model": KEY, "effort": "low"}}}


class GptUpgradeCase(test_cli.OperatorCommandCase):
    # Reuse the release-upgrade harness, not a second implementation of
    # Runtime's seams, preparation or the real resume commit.
    use_catalog = fable.FableUpgradeCase.use_catalog
    resolved = fable.FableUpgradeCase.resolved
    resume = fable.FableUpgradeCase.resume
    commit_resume = fable.FableUpgradeCase.commit_resume

    def setUp(self) -> None:
        super().setUp()
        endpoint.write_config(self.runtime.home, endpoint.EndpointConfig(port=19327, backend="on-demand"))
        self.runtime.proc_root.mkdir()
        self.old_root = rp.compose_root(SHIPPED_ROOT, rp.read_target(OLD), self.root / "old-assets")
        self.use_catalog(self.old_root)
        self.assertEqual(self.runtime.catalog.docs["version"]["launcher_version"], "2.5.0")
        self.assertEqual(self.runtime.catalog.docs["version"]["catalog_version"], 44)
        self.declare(True)
        self.apply_and_serve()
        self.runtime.bindings.update(lambda doc: doc.update(copy.deepcopy(BINDINGS)), cat=self.runtime.lineup_catalog())
        self.runtime.profiles.save(copy.deepcopy(PROFILE))
        self.record = self.save_v4_session("mine", write_scope=True, last_event_source="end")
        self.before_state = self.state_bytes()

    def declare(self, present):
        lines = {OTHER: copy.deepcopy(SPARE)}
        if present:
            lines[KEY] = copy.deepcopy(DECLARATION)
        operator_mod.write_provider_file(self.runtime.environ, "openai", {"version": 1, "lines": lines})

    def rollback(self):
        before, scratch = rollback_tests.tree(self.root), rollback_tests.scratch_roots()
        report = rp.check(environ=self.runtime.gateway_environ(), home=self.runtime.home,
                          state_root=self.runtime.session_store.root, current_root=self.runtime.asset_root,
                          target_root=OLD, current="2.6.0", target="2.5.0")
        self.assertEqual(rollback_tests.tree(self.root), before)
        self.assertEqual(rollback_tests.scratch_roots(), scratch)
        self.assertIsNone(report.unreadable)
        return report


@uses_shipped_catalog
class MovedGptOperatorLineTests(GptUpgradeCase):
    def setUp(self) -> None:
        super().setUp()
        self.use_catalog(SHIPPED_ROOT)

    def test_displacement_and_each_capture_report_the_exact_attention(self) -> None:
        layer = self.runtime.operator_snapshot().layer
        self.assertEqual(set(layer.lines), {OTHER})
        self.assertEqual(set(layer.displaced), {KEY})
        self.assertEqual(operator_mod.blocking_problems(layer), ())
        blocks, attention, _info = doctor_mod._doctor_operator_report(self.runtime, "fixture-token")
        self.assertFalse(blocks)
        rows = [line for line in attention if "now provided by the catalog" in line]
        self.assertEqual(len(rows), 1, attention)
        self.assertIn(f"operator line {KEY} (openai/{WIRE}) is now provided by the catalog as sol", rows[0])
        self.assertIn(f"charmer models rm {KEY} --successor sol", rows[0])
        self.assertEqual(sorted(line for line in attention if line.startswith("oauth-extra-models:")), sorted(
            f"oauth-extra-models: overlay codex/{WIRE}: captured alias {alias} differs from the winning catalog "
            "line sol; the winning definition is served" for alias in ALIASES))
        for alias in ALIASES:
            self.assertNotIn("output", self.ledger().aliases[alias])
        self.assertEqual(self.runtime.catalog.lines["sol"]["output"]["declared_tokens"], 128000)
        self.assertEqual(self.state_bytes(), self.before_state)
        self.apply_and_serve()
        document = self.runtime.operator_render_plan()
        for alias in ALIASES:
            self.assertEqual(document.captures[alias]["wire"], WIRE)

    def test_profile_and_resume_move_five_effort_bindings_without_rewriting_intent(self) -> None:
        lineup = self.resolved()
        self.assertEqual((lineup.lead.binding.key, lineup.lead.binding.selector),
                         ("sol", "gpt-multi-sol-6-2-max[1m]"))
        self.assertEqual((lineup.agents["cm-reviewer"].binding.key, lineup.agents["cm-reviewer"].binding.selector),
                         ("sol", "gpt-multi-sol-6-2-low[1m]"))
        self.assertEqual(self.runtime.profiles.load("mine"), PROFILE)
        self.assertEqual(self.runtime.bindings.load(), BINDINGS)
        self.assertEqual(self.state_bytes(), self.before_state)
        self.apply_and_serve()
        record = self.commit_resume()
        self.assertEqual(record["applied"]["lead"]["selector"], "gpt-multi-sol-6-2-max[1m]")
        self.assertEqual(record["applied"]["agents"]["cm-reviewer"]["selector"], "gpt-multi-sol-6-2-low[1m]")
        for field in ("managed_id", "runtime_session_id", "profile", "follow", "created_at"):
            self.assertEqual(record[field], self.record[field])
        self.assertEqual(record["launch_epoch"], self.record["launch_epoch"] + 1)

    def test_removal_and_prune_keep_the_successor_and_other_lines(self) -> None:
        self.apply_and_serve()
        code, out, err = self.op(["models", "rm", KEY, "--successor", "sol", "--yes"])
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.runtime.profiles.load("mine")["lead"], {"model": "sol", "effort": "max"})
        self.assertEqual(self.runtime.bindings.load()["bindings"]["gpt-review"], {"model": "sol", "effort": "low"})
        self.assertEqual(self.ledger().removed[KEY]["successor"], "sol")
        self.assertEqual(self.resume().record["applied"]["lead"]["selector"], "gpt-multi-sol-6-2-max[1m]")
        self.assertLessEqual(ALIASES, set(self.ledger().aliases))
        self.serve_current()
        before = self.state_bytes()
        code, out, err = self.op(["doctor", "--prune-aliases", *sorted(ALIASES), "--preview"])
        self.assertEqual(code, 0, out + err)
        for alias in ALIASES:
            self.assertIn(alias, out)
        self.assertEqual(self.state_bytes(), before)
        code, out, err = self.op(["doctor", "--prune-aliases", *sorted(ALIASES)])
        self.assertEqual(code, 0, out + err)
        self.assertFalse(ALIASES & set(self.ledger().aliases))
        self.assertIn(f"{OTHER}-high", self.ledger().aliases)
        document = proxy.candidate_document(self.runtime.home, environ=self.runtime.gateway_environ(),
                                             asset_root=SHIPPED_ROOT, state_root=self.runtime.session_store.root)
        served = render.rendered_selectors(document)
        self.assertFalse(ALIASES & served)
        self.assertIn("gpt-multi-sol-6-2-high", served)
        self.assertEqual(self.runtime.session_store.load(test_cli.FIXED_ID), self.record)

    def test_rollback_shipped_selectors_are_findings_even_with_temporary_aliases(self) -> None:
        # A temporary selector is served by both catalogs, unlike a shipped
        # generation selector. Neither a declaration nor its capture masks it.
        # Keyed routes must be configured in the fixture to be served in either view.
        state.atomic_write(self.secret_file, rollback_tests.SECRETS)
        self.runtime.profiles.save({**copy.deepcopy(PROFILE), "lead": {"model": "sol", "effort": "max"}})
        self.runtime.bindings.update(lambda doc: doc["bindings"]["gpt-review"].update(model="sol"),
                                     cat=self.runtime.lineup_catalog())
        for temporary in ("declared", "captured", "absent"):
            with self.subTest(temporary=temporary):
                if temporary != "declared":
                    self.declare(False)
                if temporary == "absent":
                    schemas = operator_mod.load_schemas(SHIPPED_ROOT)
                    operator_mod.write_ledger(self.runtime.environ, operator_mod.apply_capture_prune(
                        operator_mod.ledger_document(self.ledger()), sorted(ALIASES), 45), schemas)
                for selector, agent in (("gpt-multi-sol-6-2-high", False), ("gpt-multi-astra-6-2-max", True),
                                        ("charmer-or-gpt-6-2-high", True), ("charmer-or-gemini-4-high", True),
                                        ("charmer-or-gemini-carbon-medium", False)):
                    with self.subTest(selector=selector):
                        rollback_tests.session(self.runtime.session_store.root, test_cli.OTHER_ID,
                                               "claude-opus-5-5[1m]" if agent else selector + "[1m]",
                                               {"cm-reviewer": selector + "[1m]"} if agent else {})
                        report = self.rollback()
                        self.assertEqual(report.findings, (rp.Finding(
                            rp.SESSIONS, f"session 22222222 uses {selector}, which 2.5.0 does not serve",
                            f"exit it first (end the session; after the rollback, charmer -r {test_cli.OTHER_ID} "
                            "resumes it on 2.5.0's models)"),))
                        self.assertEqual(report.unchanged, 0)
                if temporary != "absent":
                    rollback_tests.session(self.runtime.session_store.root, test_cli.OTHER_ID,
                                           f"{KEY}-max[1m]", {})
                    self.assertEqual(self.rollback().findings, ())

    def test_rollback_ignores_ended_and_fence_only_selectors(self) -> None:
        for ended in (True, False):
            with self.subTest(ended=ended):
                rollback_tests.session(self.runtime.session_store.root, test_cli.OTHER_ID,
                                       "gpt-multi-sol-6-2-high[1m]" if ended else "claude-opus-5-5[1m]", {},
                                       live=not ended, fence=["gpt-multi-sol-6-2-high[1m]"])
                self.assertEqual(self.rollback().findings, ())

    def test_previous_entries_and_seeds_are_preserved(self) -> None:
        old = catalog.load_catalog(self.old_root)
        current = self.runtime.catalog
        self.assertEqual(current.lines["luna"], old.lines["luna"])
        for key in ("sol@6", "gpt55"):
            self.assertEqual(current.retired[key], old.retired[key])
        for key in ("astra", "sol", "or-gpt", "or-gemini"):
            line = old.lines[key]
            retired = current.retired[f"{key}@{line['generation']}"]
            self.assertEqual(retired["selectors"], {spec["selector"]: spec["proxy_contract"]
                                                  for spec in line["efforts"].values()})
            for name in ("provider", "display", "generation", "capabilities", "roles"):
                self.assertEqual(retired[name], line[name])
            self.assertNotIn("output", retired)
            self.assertNotIn("registry_overlay", retired)
        for name, document in old.seed_profiles.items():
            self.assertEqual(current.seed_profiles[name], document)


@uses_shipped_catalog
class OpenRouterListingUpgradeTests(GptUpgradeCase):
    def test_listed_efforts_survive_displacement_resume_and_successor_removal(self) -> None:
        state.atomic_write(self.secret_file, rollback_tests.SECRETS)
        for key, wire, family, levels, checked, prefix in (
            ("or-gemini", "google/gemini-4-hydrogen", "google", LEVELS[:-1], ("low", "medium"),
             "charmer-or-gemini-4"),
            ("or-gpt", "openai/gpt-6.2-sol", "openai", LEVELS, ("low", "medium", "max"),
             "charmer-or-gpt-6-2"),
        ):
            for effort in checked:
                with self.subTest(line=key, effort=effort):
                    custom_key = f"custom-{key}-{effort}"
                    declaration = {
                        "wire_model": wire, "display": "Listed model", "family": family,
                        "efforts": {level: f"output-config-{level}" for level in levels},
                        "default_effort": "high",
                        "context": {"declared_tokens": 1050000 if key == "or-gpt" else 1048576,
                                    "source": "listing", "source_ref": "https://openrouter.ai/api/v1/models"},
                    }
                    # The declaration has every effort 2.5.0's discovery would add.
                    rollback_tests.declare(self.runtime.environ, {custom_key: declaration})
                    self.use_catalog(self.old_root)
                    self.apply_and_serve()
                    self.runtime.bindings.update(lambda doc: doc["bindings"].update({
                        "router-review": {"model": custom_key, "effort": effort}}), cat=self.runtime.lineup_catalog())
                    document = {**copy.deepcopy(PROFILE), "name": "router",
                                "lead": {"model": custom_key, "effort": effort},
                                "agents": {"cm-reviewer": {"use": "router-review"}}}
                    self.runtime.profiles.save(document)
                    self.assertEqual(self.resolved("router").lead.binding.key, custom_key)
                    record = self.save_v4_session("router", write_scope=True, last_event_source="end")
                    self.use_catalog(SHIPPED_ROOT)
                    layer = self.runtime.operator_snapshot().layer
                    self.assertIn(custom_key, layer.displaced)
                    self.assertEqual(operator_mod.blocking_problems(layer), ())
                    blocks, attention, _info = doctor_mod._doctor_operator_report(self.runtime, "fixture-token")
                    self.assertFalse(blocks)
                    remedy = f"charmer models rm {custom_key} --successor {key}"
                    self.assertIn(f"operator line {custom_key} (openrouter/{wire}) is now provided by the catalog as "
                                  f"{key}: yours yields to it — remove it: {remedy}; used by binding router-review, "
                                  f"profile router lead: they move to {key} at their next launch or resume", attention)
                    notices, _info = doctor_mod._doctor_record_report(self.runtime, [record])
                    self.assertTrue(any(f"lead {custom_key} renamed → {key}" in row and
                                        "applies at the next resume" in row for row in notices), notices)
                    with self.subTest(action="resolve-and-resume"):
                        evaluation = profile.evaluate(document, self.runtime.lineup_catalog(),
                                                      bindings=self.runtime.bindings.load()["bindings"],
                                                      effective=self.runtime.current_effective())
                        self.assertEqual(evaluation.errors, ())
                        for binding in (evaluation.lineup.lead.binding,
                                        evaluation.lineup.agents["cm-reviewer"].binding):
                            self.assertEqual((binding.key, binding.selector), (key, f"{prefix}-{effort}[1m]"))
                        self.apply_and_serve()
                        self.assertEqual(self.resume(record).record["applied"]["lead"]["selector"],
                                         f"{prefix}-{effort}[1m]")
                    with self.subTest(action="doctor-remedy"):
                        code, out, err = self.op(["models", "rm", custom_key, "--successor", key, "--yes"])
                        self.assertEqual(code, 0, out + err)
                        expected = {"model": key, "effort": effort}
                        self.assertEqual(self.runtime.profiles.load("router")["lead"], expected)
                        self.assertEqual(self.runtime.bindings.load()["bindings"]["router-review"], expected)
                        self.assertFalse(any(row.startswith(f"operator line {custom_key} ") for row in
                                             doctor_mod._doctor_operator_report(self.runtime, "fixture-token")[1]))

    def test_carbon_profile_and_named_binding_are_rollback_findings(self) -> None:
        state.atomic_write(self.secret_file, rollback_tests.SECRETS)
        self.use_catalog(SHIPPED_ROOT)
        self.runtime.profiles.save({**copy.deepcopy(PROFILE), "name": "carbon",
                                    "lead": {"model": "or-gemini-carbon", "effort": "low"}, "agents": {}})
        self.runtime.bindings.update(lambda doc: doc["bindings"].update({
            "carbon-review": {"model": "or-gemini-carbon", "effort": "medium"}}), cat=self.runtime.lineup_catalog())
        self.assertEqual(self.resolved("carbon").lead.binding.key, "or-gemini-carbon")
        report = self.rollback()
        self.assertEqual(report.findings, (
            rp.Finding(rp.PROFILES, "bindings.carbon-review.model: unknown model 'or-gemini-carbon'",
                       "rebind it in the profile editor (charmer, then P)"),
            rp.Finding(rp.PROFILES, "profile carbon: lead.model: unknown model 'or-gemini-carbon'",
                       "charmer profile edit carbon"),
        ))
        self.assertEqual(report.unchanged, 0)

    def test_shipped_carbon_advisory_does_not_require_an_openrouter_key(self) -> None:
        for connected in (False, True):
            with self.subTest(connected=connected):
                if connected:
                    state.atomic_write(self.secret_file, rollback_tests.SECRETS)
                self.use_catalog(SHIPPED_ROOT)
                rows = [row for row in doctor_mod._doctor_new_lines_report(self.runtime)
                        if row.subject == "or-gemini-carbon"]
                self.assertEqual(rows, [
                    "New model: Gemini 4 Carbon · OpenRouter (or-gemini-carbon) — not admitted (optional badge): "
                    "charmer models admit or-gemini-carbon (or Models, then Enter; use does not require it)"])
                self.assertEqual(rows[0].level, doctor_mod.ADVISORY)


if __name__ == "__main__":
    unittest.main()
