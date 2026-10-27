"""Exact-client checks of the lineup defaults the compile states.

Real pinned client through the probe harness (network-hermetic, loopback fake
provider, fixture homes; zero provider calls), like
``tests/test_client_models.py`` and ``tests/test_compaction_window_probe.py``
whose helpers these reuse. Only the two keys this compile adds are passed as
flag settings env (never the compiled gateway URL): the probe's provider is
the only endpoint.

- Background requests: the client sends its own background requests (request
  class ``auxiliary``) and the native ``claude-code-guide`` to
  ``ANTHROPIC_DEFAULT_HAIKU_MODEL`` alone, and reads it from the user's
  settings env, which a launch does not clear. With a model outside the fence
  named there (and, measured, in the project's settings env), the compiled
  value keeps both inside the fence: an Opus lead, a GPT lead, and a lineup
  that binds ``haiku``. With Haiku in the fence but not bound,
  ``claude-code-guide`` runs on the lead (the variable names it).
- Compaction: C's case ``env-lead-user-disable-auto-compact`` (the user's
  settings env ``DISABLE_AUTO_COMPACT=1``) under the production compile, which
  carries ``"0"``: the managed lead compacts at its trigger.

Evidence is metadata only (model ids, request classes, counts).
"""

from __future__ import annotations

import copy
import unittest
from typing import Any

import test_client_models as client_models
import test_compaction_window_probe as compaction
import test_scope_probe_client as client_probe
from _catalog import SHIPPED_ROOT
from charmer import catalog, compiler, probe, profile, scope, settings, state, strict_json

_MARKER = client_models._HAIKU_MARKER
_HAIKU = client_models._HAIKU
_wire = client_models._wire


def compiled_lineup(document: dict[str, Any]) -> tuple[profile.ResolvedLineup, dict[str, Any]]:
    """The production scope settings of ``document`` on the shipped catalog."""

    bundle = catalog.load_catalog(SHIPPED_ROOT)
    lcat = profile.LineupCatalog.from_docs(bundle.docs)
    eff = settings.effective({"version": 1}, provider_ids=lcat.providers, line_keys=lcat.lines)
    lineup = profile.resolve(document, lcat, effective=eff)
    plan = scope.compile_lineup_scope(lineup, lcat, eff, bundle.prompt_bodies, scope.catalog_meta_v2(bundle.docs),
                                      lineup_generation=1)
    return lineup, plan.settings


def seed(name: str, **agents: dict[str, str]) -> dict[str, Any]:
    document = copy.deepcopy(catalog.load_catalog(SHIPPED_ROOT).seed_profiles[name])
    document["agents"].update(agents)
    return document


class HaikuFenceProbe(client_models._SpikeTestCase):
    """The compiled ANTHROPIC_DEFAULT_HAIKU_MODEL against a user's value
    outside the fence. The line is ``client check haiku-fence: PASS <detail>``."""

    def _layer(self, fixture: probe.ProbeFixture, layer: str | None) -> None:
        if layer is None:
            return
        target = (fixture.claude_config_dir if layer == "user" else
                  state.ensure_private_dir(fixture.project_dir / ".claude")) / "settings.json"
        state.atomic_write(target, strict_json.canonical_file_bytes(
            {"env": {compiler.HAIKU_DEFAULT_ENV: _MARKER}}))

    def _models(self, lead: str, fence: tuple[str, ...], flag_env: dict[str, str] | None, *,
                layer: str | None, guide: bool) -> list[str]:
        """The models of the background requests (or of claude-code-guide)."""

        child_env = dict(client_models._HINT_ENV)
        if guide:
            child_env[client_models._EXPLORE_CAP_OFF] = "1"

        def run(*, timeout=client_models._RUN_TIMEOUT):
            fixture = self._fixture()
            self._layer(fixture, layer)
            responder = client_models._SpikeResponder(agents=("claude-code-guide",) if guide else ())
            return self._run("haiku-fence", lead, responder=responder, fence=fence, child_env=child_env, pty=True,
                             timeout=timeout, fixture=fixture, settings_env=flag_env)

        (_result, rows), _retried = client_models._retry_s9_pty(run)
        if guide:
            picked = {row["model"] for row in rows if row["agent_type"] == "claude-code-guide"}
            self.assertTrue(picked, f"claude-code-guide was never spawned (lead {lead})")
        else:
            picked = {row["model"] for row in rows if row["request_class"] == "auxiliary"}
            self.assertTrue(picked, f"no background request (lead {lead})")
        return sorted(picked)

    def test_background_requests_stay_in_the_fence(self) -> None:
        lineups = {
            "opus": seed("claude"),
            "gpt": seed("openai"),
            "haiku": seed("claude", **{"cm-explorer": {"model": "haiku", "effort": "medium"}}),
        }
        observed: dict[str, Any] = {}
        for label, document in lineups.items():
            lineup, compiled = compiled_lineup(document)
            lead = lineup.lead.binding.selector
            fence = tuple(compiled["availableModels"])
            flag_env = {key: compiled["env"][key] for key in (compiler.HAIKU_DEFAULT_ENV, compiler.AUTO_COMPACT_ENV)}
            target = flag_env[compiler.HAIKU_DEFAULT_ENV]
            self.assertIn(f"{_HAIKU}[1m]", fence)  # Haiku is fenced in every case
            self.assertEqual(target, f"{_HAIKU}[1m]" if label == "haiku" else lead)
            observed[label] = {
                "user": self._models(lead, fence, flag_env, layer="user", guide=False),
                "guide": self._models(lead, fence, flag_env, layer="user", guide=True),
            }
            if label == "opus":
                # The project layer, measured with and without the compiled value.
                observed[label]["project"] = self._models(lead, fence, flag_env, layer="project", guide=False)
                observed[label]["project-uncompiled"] = self._models(lead, fence, None, layer="project", guide=False)
                observed[label]["user-uncompiled"] = self._models(lead, fence, None, layer="user", guide=False)
            wires = {_wire(selector) for selector in fence}
            for case in ("user", "guide", "project"):
                if case in observed[label]:
                    self.assertEqual(observed[label][case], [_wire(target)], (label, case))
                    self.assertLessEqual(set(observed[label][case]), wires, (label, case))
        # Without the compiled value the user's model outside the fence gets
        # them (what the compile closes); the project layer, measured:
        self.assertEqual(observed["opus"]["user-uncompiled"], [_MARKER])
        self.assertEqual(observed["opus"]["project-uncompiled"], [_MARKER])
        detail = " ".join(f"{label}:{case}->{'/'.join(models)}" for label, cases in observed.items()
                          for case, models in cases.items())
        print(f"client check haiku-fence: PASS {detail} {self._identity()}", flush=True)


class CompactionCounterProbe(client_probe._ProbeCase, unittest.TestCase):
    """C's compaction probe case with a user's DISABLE_AUTO_COMPACT=1 under
    the production compile: the lead compacts at its trigger."""

    probe_id = "compaction-counter"
    test_observation = None  # run under the name below
    _case = compaction.CompactionWindowProbe._case

    @classmethod
    def tearDownClass(cls) -> None:
        status, observed, detail = cls.verdict
        print(f"compaction counter probe: {status} class={observed} {detail}", flush=True)

    def test_a_user_disable_auto_compact_still_compacts_the_managed_lead(self) -> None:
        compiled = compaction.production_compile()
        self.assertEqual(compiled["plan"].settings["env"][compiler.AUTO_COMPACT_ENV], "0")
        (case,) = [case for case in compaction.cases(compiled["context"].window)
                   if case.name == "env-lead-user-disable-auto-compact"]
        self.assertEqual((dict(case.user_env), case.flag_env), ({compiler.AUTO_COMPACT_ENV: "1"}, ()))
        expected = {case.name: {"target": "lead", "wire": compaction.wire(compiled["lead"].selector),
                                "trigger": case.expected, "representation": case.representation}}
        try:
            row = self._case(case, compiled)
        except probe.ProbeError as exc:
            if "live daemon domain touched" in str(exc):
                self.__class__.verdict = ("BOUNDARY", "unavailable", "live daemon-domain tripwire hit")
                self.skipTest("BOUNDARY: live daemon-domain tripwire hit")
            raise
        outcome = compaction.outcomes({case.name: row}, expected)[case.name]
        detail = f"{case.name}={outcome}@{row['first_summary_after']}/{case.expected}"
        self.__class__.verdict = ("PASS" if outcome == "ok" else "FAIL", outcome, detail)
        self.assertEqual(outcome, "ok", detail)


if __name__ == "__main__":
    unittest.main()
