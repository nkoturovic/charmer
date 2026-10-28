"""A 2.2.1 operator registry whose lines the 2.3.0 catalog now ships.

A user who added Haiku 5.5, Muse Wonder 1.0, DeepSeek V4.1 Flash 1017 or
Kimi K3.1 Pro on OpenRouter as a line of their own on 2.2.1 updates into a
catalog that serves the same (provider, wire) (``haiku``, ``muse``,
``or-deepseek``, ``or-kimi``). Phase one writes
that registry with the operator code of 2.2.1 (unchanged since) against a
catalog without those lines (the frozen fixture), so the ledger captures its
aliases exactly as a 2.2.1 render does; phase two loads the same state with
the shipped catalog. The shadowed lines yield and are reported as now
provided by the catalog with their remedy, their keys resolve to the
catalog lines (profiles, bindings, records), their captured aliases stay
served for running sessions, and every other line loads.

No provider request and no gateway: the fixture served set and an instant
reload; never 8317/8316.
"""

from __future__ import annotations

import copy
import unittest
from unittest import mock

import charmer.proxy
import test_cli  # module import: no test classes re-exported
from _catalog import SHIPPED_ROOT, served_selectors
from charmer import cli, custom, operator as operator_mod, profile, scope, state, strict_json
from charmer.cli import doctor as doctor_mod

SECRETS = (b"KIMI_CLAUDE_API_KEY=cli-test-dummy\nACME_API_KEY=acme-dummy-value\n"
           b"META_CLAUDE_API_KEY=meta-dummy\nOPENROUTER_CLAUDE_API_KEY=or-dummy\n")
# What `charmer models add` on 2.2.1 writes for each (its default effort
# shape: the provider's reviewed high contract, else ["high"]).
DECLARED = {
    "anthropic": {"custom-claude-haiku-5-5": {
        "wire_model": "claude-haiku-5-5", "display": "claude-haiku-5-5", "efforts": ["high"], "default_effort": "high",
        "context": {"declared_tokens": 1000000, "source": "docs",
                    "source_ref": "https://platform.claude.com/docs/en/models/haiku-5-5/overview, 2026-10-20"}}},
    "meta": {"custom-muse-wonder-1-0": {
        "wire_model": "muse-wonder-1.0", "display": "muse-wonder-1.0", "efforts": {"high": "output-config-high"},
        "default_effort": "high",
        "context": {"declared_tokens": 1048576, "source": "docs", "source_ref": "https://dev.meta.ai/docs/models, 2026-10-20"}}},
    "openrouter": {
        "custom-deepseek-deepseek-v4-1-flash-1017": {
            "wire_model": "deepseek/deepseek-v4.1-flash-1017", "display": "deepseek/deepseek-v4.1-flash-1017",
            "family": "deepseek", "efforts": {"high": "output-config-high"}, "default_effort": "high",
            "context": {"declared_tokens": 1048576, "source": "docs", "source_ref": "https://openrouter.ai, 2026-10-20"}},
        "custom-moonshotai-kimi-k3-1-pro": {
            "wire_model": "moonshotai/kimi-k3.1-pro", "display": "moonshotai/kimi-k3.1-pro", "family": "moonshot",
            "efforts": {"high": "output-config-high"}, "default_effort": "high",
            "context": {"declared_tokens": 1048576, "source": "docs", "source_ref": "https://openrouter.ai, 2026-10-20"}},
        "custom-minimax-minimax-m3-1-flash": {
            "wire_model": "minimax/minimax-m3.1-flash", "display": "minimax/minimax-m3.1-flash", "family": "minimax",
            "efforts": {"high": "output-config-high"}, "default_effort": "high",
            "context": {"declared_tokens": 1048576, "source": "docs", "source_ref": "https://openrouter.ai, 2026-10-20"}},
    },
}
SHADOWED = {"custom-claude-haiku-5-5": ("anthropic", "claude-haiku-5-5", "haiku"),
            "custom-muse-wonder-1-0": ("meta", "muse-wonder-1.0", "muse"),
            "custom-deepseek-deepseek-v4-1-flash-1017": ("openrouter", "deepseek/deepseek-v4.1-flash-1017",
                                                          "or-deepseek"),
            "custom-moonshotai-kimi-k3-1-pro": ("openrouter", "moonshotai/kimi-k3.1-pro", "or-kimi")}
OTHER = "custom-minimax-minimax-m3-1-flash"
PROFILE = {"version": 2, "name": "mine", "description": "uses lines of my own",
           "lead": {"model": "custom-deepseek-deepseek-v4-1-flash-1017", "effort": "high"},
           "agents": {"cm-explorer": {"model": "custom-claude-haiku-5-5", "effort": "high"},
                      "cm-implementer-light": {"model": OTHER, "effort": "high"},
                      "cm-reviewer": {"model": "custom-moonshotai-kimi-k3-1-pro", "effort": "high"}},
           "native_agents": {"explore": "replace", "general_purpose": "off", "plan": "native"},
           "workflows": "native"}


class ShadowedOperatorLineTests(test_cli.OperatorCommandCase):
    # A legacy custom.json registry present at the update (no migration
    # marker), or None.
    LEGACY: dict | None = None

    def setUp(self) -> None:
        super().setUp()
        # Phase one: 2.2.1's registry, written and rendered on a catalog
        # that does not ship these lines (the ledger captures each alias).
        state.atomic_write(self.secret_file, SECRETS)
        state.ensure_private_dir(self.pdir)
        for provider, lines in DECLARED.items():
            state.atomic_write(self.pdir / f"{provider}.json", strict_json.pretty_file_bytes(
                {"version": 1, "lines": copy.deepcopy(lines)}))
        code, _out, err = self.op(["providers", "apply"])
        self.assertEqual(code, 0, err)
        self.before = self.runtime.operator_snapshot().layer
        self.runtime.profiles.save(copy.deepcopy(PROFILE))
        self.captured = set(self.ledger().aliases)
        if self.LEGACY is not None:
            path = custom.registry_path(self.runtime.environ)
            state.ensure_private_dir(path.parent)
            state.atomic_write(path, strict_json.pretty_file_bytes(self.LEGACY))
        # Phase two: the same state, the 2.3.0 catalog.
        self.served = set(served_selectors(SHIPPED_ROOT))
        self.runtime = cli.Runtime(
            **self._runtime_seams(), asset_root=SHIPPED_ROOT, environ=self.runtime.environ, cwd=self.root / "project",
            launch_callback=lambda prepared: self.launches.append(prepared) or 0, doctor_callback=lambda _r: [],
            doctor_binary_callback=lambda _contract: ([], ["fixture"]),
            doctor_daemon_callback=self.runtime.doctor_daemon_callback)
        patcher = mock.patch.object(self.runtime, "verify_reload", side_effect=lambda sentinel: charmer.proxy.ReloadResult(
            "reloaded", f"gateway: reloaded (sentinel {sentinel})"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_221_registry_was_whole(self) -> None:
        self.assertEqual(set(self.before.lines), {*SHADOWED, OTHER})
        self.assertLessEqual({"claude-haiku-5-5", "custom-muse-wonder-1-0-high",
                              "custom-deepseek-deepseek-v4-1-flash-1017-high",
                              "custom-moonshotai-kimi-k3-1-pro-high"}, self.captured)

    def test_shadowed_lines_yield_and_every_other_line_loads(self) -> None:
        layer = self.runtime.operator_snapshot().layer
        self.assertEqual(set(layer.lines), {OTHER})
        self.assertEqual(set(layer.displaced), set(SHADOWED))
        self.assertEqual(operator_mod.blocking_problems(layer), ())
        for key, (provider, wire, successor) in SHADOWED.items():
            (problem,) = [p for found in layer.problems_by_file.values() for p in found if key in p.json_path]
            self.assertEqual(problem.code, "displaced")
            self.assertIn(f"{provider}/{wire} is now provided by the catalog line \"{successor}\"", problem.text())
            self.assertIn(f"charmer models rm {key} --successor {successor}", problem.text())
        # An explicit render goes through; the shadowed lines' captured
        # aliases stay served (running sessions keep them until resume),
        # and the Haiku capture never shadows the catalog's own route.
        code, _out, err = self.op(["providers", "apply"])
        self.assertEqual(code, 0, err)
        plan = self.runtime.operator_render_plan()
        self.assertLessEqual({"custom-muse-wonder-1-0-high", "custom-deepseek-deepseek-v4-1-flash-1017-high",
                              "custom-moonshotai-kimi-k3-1-pro-high"}, set(plan.captures))
        self.assertEqual(plan.captures["custom-moonshotai-kimi-k3-1-pro-high"]["wire"], "moonshotai/kimi-k3.1-pro")
        self.assertEqual(plan.captures["custom-muse-wonder-1-0-high"]["wire"], "muse-wonder-1.0")

    def test_reported_with_the_remedy(self) -> None:
        _blocks, attention, _info = doctor_mod._doctor_operator_report(self.runtime, None)
        for key, (provider, wire, successor) in SHADOWED.items():
            expected = (f"operator line {key} ({provider}/{wire}) is now provided by the catalog as {successor}: "
                        f"yours yields to it — remove it: charmer models rm {key} --successor {successor}")
            self.assertTrue([line for line in attention if line.startswith(expected)], attention)
        code, out, err = self.op(["models"])
        self.assertEqual(code, 0, err)
        for key, (_provider, _wire, successor) in SHADOWED.items():
            self.assertIn(f"{key}\t", out)
            self.assertIn(f"now provided by the catalog as {successor}\tremove yours: charmer models rm {key} "
                          f"--successor {successor}", out)
        self.assertIn(f"{OTHER}\t", out)
        code, out, err = self.op(["models", "show", "custom-claude-haiku-5-5"])
        self.assertEqual(code, 0, err)
        self.assertIn("is now provided by the catalog as haiku", out)

    def test_the_surfaces_say_it_verbatim(self) -> None:
        # The exact texts an update rehearsal asserts.
        self.maxDiff = None
        layer = self.runtime.operator_snapshot().layer
        files = {"anthropic": "providers.d/anthropic.json", "meta": "providers.d/meta.json",
                 "openrouter": "providers.d/openrouter.json"}
        users = {"custom-claude-haiku-5-5": "profile mine cm-explorer",
                 "custom-muse-wonder-1-0": None,
                 "custom-deepseek-deepseek-v4-1-flash-1017": "profile mine lead",
                 "custom-moonshotai-kimi-k3-1-pro": "profile mine cm-reviewer"}
        validate, doctor, listing, show = [], [], [], {}
        for key, (provider, wire, successor) in SHADOWED.items():
            remedy = f"charmer models rm {key} --successor {successor}"
            validate.append(f"{files[provider]}: lines.{key}: {provider}/{wire} is now provided by the catalog line "
                            f"\"{successor}\" — yours yields to it; remove it: {remedy}")
            said = (f"operator line {key} ({provider}/{wire}) is now provided by the catalog as {successor}: "
                    f"yours yields to it — remove it: {remedy}")
            doctor.append(said + (f"; used by {users[key]}: they move to {successor} at their next launch or resume"
                                  if users[key] else ""))
            listing.append(f"{key}\t{wire}\tnow provided by the catalog as {successor}\tremove yours: {remedy}")
            show[key] = (0, said + "\n", "")
        _blocks, attention, _info = doctor_mod._doctor_operator_report(self.runtime, None)
        self.assertEqual(sorted(p.text() for found in layer.problems_by_file.values() for p in found), sorted(validate))
        self.assertEqual(sorted(line for line in attention if line.startswith("operator line ")), sorted(doctor))
        code, out, err = self.op(["providers", "validate"])
        self.assertEqual(code, 0, err)
        self.assertLessEqual(set(validate), set((out + err).splitlines()))
        code, out, err = self.op(["models"])
        self.assertEqual(code, 0, err)
        self.assertLessEqual(set(listing), set(out.splitlines()))
        for key in SHADOWED:
            self.assertEqual(self.op(["models", "show", key]), show[key])
        # Adding a shipped model: under a key of its own, and under the key
        # 2.2.1 derived (the yielded line is still in the file).
        self.assertEqual(self.op(["models", "add", "anthropic", "claude-haiku-5-5", "--as", "custom-haiku-again",
                                  "--context", "1000000", "--source", "docs", "--source-ref",
                                  "https://platform.claude.com/docs/en/models/haiku-5-5/overview, 2026-10-27"]),
                         (1, "", "charmer: models add: anthropic/claude-haiku-5-5 ships with charmer as the line haiku — "
                                 "nothing written; bind it in a profile (charmer profile edit NAME) or see charmer models\n"))
        self.assertEqual(self.op(["models", "add", "openrouter", "moonshotai/kimi-k3.1-pro", "--context", "1048576",
                                  "--source", "docs", "--source-ref", "https://openrouter.ai, 2026-10-27"]),
                         (1, "", "charmer: models add: openrouter/moonshotai/kimi-k3.1-pro ships with charmer as the "
                                 "line or-kimi — nothing written; yours (custom-moonshotai-kimi-k3-1-pro) yields to it — "
                                 "remove it: charmer models rm custom-moonshotai-kimi-k3-1-pro --successor or-kimi\n"))

    def test_their_keys_resolve_to_the_catalog_lines(self) -> None:
        lcat = self.runtime.lineup_catalog()
        for key, (_provider, _wire, successor) in SHADOWED.items():
            self.assertEqual(lcat.resolve_key(key).key, successor)
        lineup = profile.resolve(self.runtime.profiles.load("mine"), lcat, effective=self.runtime.current_effective())
        self.assertEqual(lineup.lead.binding.key, "or-deepseek")
        self.assertEqual(lineup.lead.binding.selector, "charmer-or-deepseek-4-1-1017-high[1m]")
        self.assertEqual(lineup.agents["cm-explorer"].binding.key, "haiku")
        self.assertEqual(lineup.agents["cm-explorer"].binding.selector, "claude-haiku-5-5[1m]")
        self.assertEqual(lineup.agents["cm-reviewer"].binding.key, "or-kimi")
        self.assertEqual(lineup.agents["cm-reviewer"].binding.selector, "charmer-or-kimi-3-1-high[1m]")
        self.assertEqual(lineup.agents["cm-implementer-light"].binding.key, OTHER)
        self.assertTrue([notice for notice in lineup.notices if notice.code == "retired-successor"])

    def test_adding_a_shipped_model_again_names_its_line(self) -> None:
        before = self.state_bytes()
        code, out, err = self.op(["models", "add", "anthropic", "claude-haiku-5-5", "--as", "custom-haiku-again",
                                  "--context", "1000000", "--source", "docs", "--source-ref",
                                  "https://platform.claude.com/docs/en/models/haiku-5-5/overview, 2026-10-27"])
        self.assertEqual(code, 1, out + err)
        self.assertIn("anthropic/claude-haiku-5-5 ships with charmer as the line haiku — nothing written", err)
        self.assertEqual(self.state_bytes(), before)

    def test_the_remedy_moves_the_profile_and_clears_the_finding(self) -> None:
        for key, (_provider, _wire, successor) in SHADOWED.items():
            code, out, err = self.op(["models", "rm", key, "--successor", successor, "--yes"])
            self.assertEqual(code, 0, out + err)
        document = self.runtime.profiles.load("mine")
        self.assertEqual(document["lead"]["model"], "or-deepseek")
        self.assertEqual(document["agents"]["cm-explorer"]["model"], "haiku")
        self.assertEqual(document["agents"]["cm-reviewer"]["model"], "or-kimi")
        layer = self.runtime.operator_snapshot().layer
        self.assertEqual((set(layer.lines), set(layer.displaced)), ({OTHER}, set()))
        _blocks, attention, _info = doctor_mod._doctor_operator_report(self.runtime, None)
        self.assertFalse([line for line in attention if "now provided by the catalog" in line])


class LegacyCollisionTests(ShadowedOperatorLineTests):
    """The same update with a legacy custom.json (no migration marker) that
    names each shadowed key too, on an unrelated route (DeepSeek's own API,
    another wire and credential). Before the update the declaration wins
    the key; after it the key follows its shipped successor, never the
    legacy model: every test above holds unchanged."""

    LEGACY = {"version": 1, "providers": {}, "models": {
        **{key: {"provider": "deepseek", "wire_model": "deepseek-flash", "context_tokens": 1000000,
                 "created_via": "manual"}
           for key in SHADOWED},
        "legacy-other": {"provider": "deepseek", "wire_model": "deepseek-other", "context_tokens": 1000000,
                         "created_via": "manual"}}}

    def test_custom_list_marks_displaced_legacy_models(self) -> None:
        before = self.state_bytes()
        code, out, err = self.op(["custom", "list"])
        self.assertEqual(code, 0, err)
        expected = [
            f"model {key}\twire=deepseek-flash · provider=deepseek · context=1000000 · manual · "
            f"ignored — {key} now resolves to the shipped line {successor}; remove: charmer custom remove-model {key}"
            for key, (_provider, _wire, successor) in SHADOWED.items()
        ]
        expected.append("model legacy-other\twire=deepseek-other · provider=deepseek · context=1000000 · manual")
        self.assertEqual(out.splitlines(), sorted(expected))
        _blocks, _info, attention = doctor_mod._collect_doctor_reports(self.runtime)
        for key, (_provider, _wire, successor) in SHADOWED.items():
            marker = (f"ignored — {key} now resolves to the shipped line {successor}; "
                      f"remove: charmer custom remove-model {key}")
            self.assertTrue(any(marker in line for line in attention), marker)
        self.assertEqual(self.state_bytes(), before)

    def test_custom_conflicts_includes_displaced_reservations(self) -> None:
        before = self.state_bytes()
        self.assertEqual(self.runtime.custom_conflicts(),
                         sorted(f"model {key} (retired catalog key)" for key in SHADOWED))
        self.assertEqual(self.state_bytes(), before)
        for key, (_provider, _wire, successor) in SHADOWED.items():
            with self.subTest(key=key):
                self.assertNotIn(key, self.runtime.catalog.docs["retired"]["retired"])
                code, out, err = self.op(["custom", "remove-model", key])
                self.assertEqual(code, 0, out + err)
                self.assertEqual(self.runtime.lineup_catalog().resolve_key(key).key, successor)
        self.assertEqual(self.runtime.custom_conflicts(), [])
        self.assertEqual(self.op(["custom", "list"]),
                         (0, "model legacy-other\twire=deepseek-other · provider=deepseek · context=1000000 · manual\n", ""))

    def test_a_colliding_legacy_model_never_takes_the_key(self) -> None:
        self.assertFalse(self.runtime.operator_snapshot().layer.marker)
        docs = self.runtime.ordinary_docs
        lcat = self.runtime.lineup_catalog()
        for key, (_provider, _wire, successor) in SHADOWED.items():
            with self.subTest(key=key):
                self.assertNotIn(key, docs["models"]["models"])
                self.assertNotIn(key, lcat.lines)
                self.assertEqual(lcat.resolve_key(key).key, successor)
        eff = self.runtime.current_effective()
        lineup = profile.resolve(self.runtime.profiles.load("mine"), lcat, effective=eff)
        bound = {lineup.lead.binding.key, *(agent.binding.key for agent in lineup.agents.values())}
        self.assertLessEqual({"or-deepseek", "haiku", "or-kimi"}, bound)
        self.assertFalse(bound & set(SHADOWED))
        self.assertTrue([notice for notice in lineup.notices if notice.code == "retired-successor"])
        plan = scope.compile_lineup_scope(lineup, lcat, eff, self.runtime.catalog.prompt_bodies,
                                          scope.catalog_meta_v2(docs), lineup_generation=1)
        legacy_selectors = {f"custom-{key}" for key in SHADOWED}
        self.assertFalse(legacy_selectors & set(plan.settings["availableModels"]))
        self.assertEqual(plan.settings["model"], "charmer-or-deepseek-4-1-1017-high[1m]")
        code, out, err = self.op(["providers", "apply"])
        self.assertEqual(code, 0, out + err)
        rendered = (charmer.proxy.config_dir(self.runtime.home) / "config.yaml").read_text()
        for selector in legacy_selectors:
            self.assertNotIn(f'"{selector}"', rendered)


if __name__ == "__main__":
    unittest.main()
