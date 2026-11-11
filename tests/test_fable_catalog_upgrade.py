"""Fable operator-line upgrades and rollback against packaged 2.4.0 data.

The declaration and records are synthetic, never copied from an operator's
installation. Renders and reloads use fixture seams; no provider or gateway
is contacted. The held-catalog control keeps the local 5.2 line usable.
"""

from __future__ import annotations

import copy
import unittest
from unittest import mock

import test_cli  # module import: no test classes re-exported
import test_rollback_preflight as rollback_tests
from _catalog import SHIPPED_ROOT, served_selectors, uses_shipped_catalog
from _layout import FIXTURES_ROOT
from charmer import (catalog, cli, endpoint, operator as operator_mod, profile, proxy,
                     rollback_preflight as rp, sessions, state, strict_json)
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
        self.use_catalog(SHIPPED_ROOT)

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


if __name__ == "__main__":
    unittest.main()
