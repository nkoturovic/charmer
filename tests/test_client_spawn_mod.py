"""Client check spawn-mod: an ``agent.spawn`` mod against a compiled lineup.

Claude Code 2.1.300 lets a mod's ``agent.spawn`` hook rewrite a workflow
agent's ``prompt`` and ``model`` (before, it could only refuse one), and
2.1.299 raises the hook for a resumed agent too, with ``resume`` set. A mod
is a plugin the user installs (here a ``--plugin-dir`` fixture whose hooks
module rewrites the spawn's model); it runs with the user's authority inside
the client.

Every run carries the production compile's permissions with a fixture fence
(the lead, the cm agent's model and one more lineup model) and
``disableAllHooks: false`` as every scope compiles it. The lead spawns the
fixture cm agent (frontmatter model), continues it with ``SendMessage`` (a
resumed agent) and runs a Workflow ``agent()`` of the same type:

* ``control``: no mod, every agent on its frontmatter model;
* ``resume-rewrite``: a mod rewriting only resumed spawns: the client ignores
  it (a hook can only refuse a resumed agent's run), every agent keeps the
  compiled model;
* ``outside``: a mod rewriting every spawn to a model outside the fence: that
  model never reaches the wire (the fence lands the agent on another fenced
  model, recorded);
* ``managed``: the in-fence mod under a managed ``allowManagedHooksOnly``:
  the client never loads the mod, every agent keeps the compiled model;
* ``inside``: the same mod rewriting every spawn to another lineup model:
  fresh and workflow agents run on the mod's model, and the resumed agent on
  its spawn's.

The trust boundary this records: the compiled ``--settings`` scope keeps
hooks enabled (charmer's lifecycle hooks and the model fence depend on
them), so nothing in it stops a mod the user installed from choosing
another model the fence admits; only managed settings
(``allowManagedHooksOnly``, which stops charmer's own hooks too and which
doctor reports as a managed lock) keep mods out. The fence bounds every
rewrite to the lineup's models.

Hermetic like every real-client check (loopback-only network namespace,
fixture homes, the fake provider); the managed file is mounted inside the
namespace only (``tests/_client_policy.py``). Only models and phase labels
leave the responder. The output line: ``client check spawn-mod: <STATUS>
class=<class> <detail>``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from charmer import probe, scope, state, strict_json
from charmer.probe import ProbeError

from _client_policy import managed_settings
from _layout import RESOURCES_ROOT
from _tier import real_binary_gate

LINE_PREFIX = "client check spawn-mod:"
PASS_CLASS = "SM-fenced"
_CONTRACT = RESOURCES_ROOT / "catalog" / "native-contract.json"
_LEAD = "spawn-mod-lead"
_AGENT = "spawn-mod-agent"
_OTHER = "spawn-mod-other"  # another lineup model, inside the fence
_OUTSIDE = "spawn-mod-outside"  # outside the fence
_AGENT_TYPE = "cm-spawn-mod"
_BODY = "SPAWN-MOD-AGENT-BODY"
_RESUME = "SPAWN-MOD-RESUME"
_WORKFLOW = "SPAWN-MOD-WF"
_AGENT_ID_RE = re.compile(r"agentId: ([A-Za-z0-9_-]{4,64})")
_PHASES = ("fresh", "resume", "workflow")
_REWRITE_EVERY = """export const register = (on) => {
  on('agent.spawn', ($, e, next) => next({ ...e, model: '%s' }))
}
"""
_REWRITE_RESUMED = """export const register = (on) => {
  on('agent.spawn', ($, e, next) => (e.resume ? next({ ...e, model: '%s' }) : next(e)))
}
"""


def line(status: str, observed: str, detail: str) -> str:
    return f"{LINE_PREFIX} {status} class={observed} {detail}".rstrip()


class _Inconclusive(AssertionError):
    """A control did not hold: no class can be chosen."""


def verdict(observed: dict[str, dict[str, list[str]]]) -> str:
    """The class from each case's wire models per phase."""

    def models(case: str) -> set[str]:
        return {model for phase in observed[case].values() for model in phase}

    for phase in _PHASES:
        if observed["control"].get(phase) != [_AGENT]:
            raise _Inconclusive(f"control {phase}: {observed['control'].get(phase)} (the compiled model did not run)")
    if observed["inside"].get("fresh") != [_OTHER]:
        raise _Inconclusive(f"inside fresh: {observed['inside'].get('fresh')} (the mod did not rewrite: not loaded?)")
    if any(_OUTSIDE in models(case) for case in observed):
        return "SM-outside-on-wire"
    if observed["resume-rewrite"].get("resume") != [_AGENT]:
        return "SM-resume-rewritten"
    if models("managed") != {_AGENT}:
        return "SM-managed-loaded"
    return PASS_CLASS


def _phases(rows: dict[str, list[str]]) -> str:
    return ",".join(f"{phase}:{'/'.join(models) or '-'}" for phase, models in rows.items())


class _Responder:
    """Spawn the fixture cm agent, continue it with SendMessage (retried
    while its transcript is not yet resumable), then run one Workflow
    ``agent()`` of the same type; keep each agent request's wire model under
    its phase."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.step = 0
        self.agent_id: str | None = None
        self.sends = 0
        self.models: dict[str, list[str]] = {phase: [] for phase in _PHASES}

    def _tool(self, document: dict[str, Any], ordinal: int, block: dict[str, Any]) -> tuple[int, Any]:
        payload = probe._message_payload([block], model=document.get("model"), stop_reason="tool_use",
                                         message_id=f"msg_spawn_mod_{ordinal:04d}")
        return 200, probe._sse_message(payload) if document.get("stream") else payload

    def respond(self, document: dict[str, Any], path: str, context: probe.RequestContext) -> tuple[int, Any]:
        if path.split("?", 1)[0].endswith("/count_tokens"):
            return 200, {"input_tokens": 1}
        names = {tool.get("name") for tool in document.get("tools") or () if isinstance(tool, dict)}
        messages = json.dumps(document.get("messages"))
        ordinal = context.ordinal
        with self.lock:
            if _BODY in json.dumps(document.get("system")):
                phase = "workflow" if _WORKFLOW in messages else "resume" if _RESUME in messages else "fresh"
                model = document.get("model")
                if isinstance(model, str) and model not in self.models[phase]:
                    self.models[phase].append(model)
            else:
                results = {block.get("tool_use_id"): json.dumps(block.get("content"))
                           for message in document.get("messages") or () if isinstance(message, dict)
                           and isinstance(message.get("content"), list)
                           for block in message["content"] if isinstance(block, dict)
                           and block.get("type") == "tool_result"}
                if self.step == 0 and "Agent" in names:
                    self.step = 1
                    return self._tool(document, ordinal, {
                        "type": "tool_use", "id": "toolu_spawn_mod_spawn", "name": "Agent",
                        "input": {"description": "client check spawn-mod", "prompt": "Reply ok",
                                  "subagent_type": _AGENT_TYPE, "run_in_background": False}})
                if self.step == 1 and "toolu_spawn_mod_spawn" in results:
                    match = _AGENT_ID_RE.search(results["toolu_spawn_mod_spawn"])
                    self.agent_id = match.group(1) if match else None
                    self.step = 2
                if self.step == 2:
                    last = f"toolu_spawn_mod_send_{self.sends}"
                    resumed = last in results and '\\"success\\":true' in results[last]
                    if self.sends == 0 or (last in results and not resumed and self.sends < 3):
                        # The finished agent's transcript lands a moment later.
                        time.sleep(2 if self.sends else 1)
                        self.sends += 1
                        return self._tool(document, ordinal, {
                            "type": "tool_use", "id": f"toolu_spawn_mod_send_{self.sends}", "name": "SendMessage",
                            "input": {"to": self.agent_id or "spawn-mod-missing", "summary": "client check",
                                      "message": f"{_RESUME} reply again"}})
                    if last in results:
                        self.step = 3
                        script = ("export const meta = { name: 'spawn-mod', description: 'client check' }\n"
                                  f"const r = await agent('Reply {_WORKFLOW}', {{label: 'w', agentType: '{_AGENT_TYPE}'}})\n"
                                  "return [r]\n")
                        return self._tool(document, ordinal, {"type": "tool_use", "id": "toolu_spawn_mod_wf",
                                                              "name": "Workflow", "input": {"script": script}})
        payload = probe._message_payload([{"type": "text", "text": "PROBE-OK"}], model=document.get("model"),
                                         stop_reason="end_turn", message_id=f"msg_spawn_mod_{ordinal:04d}")
        return 200, probe._sse_message(payload) if document.get("stream") else payload


class VerdictTests(unittest.TestCase):
    def test_the_class_needs_the_fence_resume_and_managed_cases(self) -> None:
        def case(fresh: str, resume: str, workflow: str) -> dict[str, list[str]]:
            return {"fresh": [fresh], "resume": [resume], "workflow": [workflow]}

        rows = {"control": case(_AGENT, _AGENT, _AGENT), "resume-rewrite": case(_AGENT, _AGENT, _AGENT),
                "outside": case(_LEAD, _LEAD, _LEAD), "managed": case(_AGENT, _AGENT, _AGENT),
                "inside": case(_OTHER, _OTHER, _OTHER)}
        self.assertEqual(verdict(rows), PASS_CLASS)
        self.assertEqual(verdict({**rows, "outside": case(_OUTSIDE, _LEAD, _LEAD)}), "SM-outside-on-wire")
        self.assertEqual(verdict({**rows, "resume-rewrite": case(_AGENT, _OTHER, _AGENT)}), "SM-resume-rewritten")
        self.assertEqual(verdict({**rows, "managed": case(_OTHER, _OTHER, _OTHER)}), "SM-managed-loaded")
        with self.assertRaises(_Inconclusive):
            verdict({**rows, "control": case(_AGENT, _AGENT, _LEAD)})
        with self.assertRaises(_Inconclusive):
            verdict({**rows, "inside": case(_AGENT, _AGENT, _AGENT)})


class SpawnModProbe(unittest.TestCase):
    verdict: tuple[str, str, str] = ("INCONCLUSIVE", "unavailable", "check did not complete")

    @classmethod
    def setUpClass(cls) -> None:
        cls.gate = real_binary_gate(_CONTRACT, what="the spawn-mod check")
        if cls.gate.boundary:
            cls.verdict = ("BOUNDARY", "unavailable", cls.gate.boundary)

    @classmethod
    def tearDownClass(cls) -> None:
        print(line(*cls.verdict), flush=True)

    def setUp(self) -> None:
        if self.gate.boundary:
            self.skipTest(self.gate.boundary)
        self.trusted = self.gate.trusted
        self.root = Path(tempfile.mkdtemp(prefix="charmer-spawn-mod-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.environ = {"HOME": str(self.root / "ambient"), "PATH": "/usr/bin:/bin"}
        self.count = 0

    def _fixture(self) -> probe.ProbeFixture:
        self.count += 1
        fixture = probe.build_fixture(self.root / f"run-{self.count}", environ=self.environ)
        probe.seed_fixture_project_trust(fixture, fixture.project_dir)
        agents = state.ensure_private_dir(fixture.project_dir / ".claude" / "agents")
        state.atomic_write(agents / f"{_AGENT_TYPE}.md", (
            f"---\nname: {_AGENT_TYPE}\ndescription: client check spawn-mod agent\nmodel: {_AGENT}\n---\n"
            f"{_BODY} reply ok.\n").encode("utf-8"))
        return fixture

    @staticmethod
    def _mod(fixture: probe.ProbeFixture, body: str) -> Path:
        mod = state.ensure_private_dir(fixture.root / "mods" / "spawn-mod")
        state.atomic_write(state.ensure_private_dir(mod / ".claude-plugin") / "plugin.json", json.dumps(
            {"name": "spawn-mod", "version": "0.1.0", "description": "client check fixture"}).encode("utf-8"))
        hooks = state.ensure_private_dir(mod / "hooks")
        state.atomic_write(hooks / "hooks.json", json.dumps({"modules": ["./register.ts"]}).encode("utf-8"))
        state.atomic_write(hooks / "register.ts", body.encode("utf-8"))
        return mod

    def _case(self, permissions: dict[str, Any], mod_body: str | None,
              managed: dict[str, Any] | None = None) -> dict[str, list[str]]:
        fixture = self._fixture()
        settings = probe.write_fixture_settings(fixture, "scope/settings.json", {
            "availableModels": [_LEAD, _AGENT, _OTHER], "model": _LEAD, "permissions": permissions,
            "disableAllHooks": False})
        extra: tuple[str, ...] = ()
        if mod_body is not None:
            extra = ("--plugin-dir", str(self._mod(fixture, mod_body)))
        responder = _Responder()

        def run() -> probe.NativeRunResult:
            with probe.FakeAnthropicProvider(responder=responder) as provider:
                return probe.run_native(
                    ("-p", "client check spawn-mod", "--settings", str(settings), "--model", _LEAD, *extra,
                     "--dangerously-skip-permissions"),
                    trusted=self.trusted, fixture=fixture, provider=provider, timeout=150, allow_real=True,
                    environ=self.environ)

        if managed is not None:
            with managed_settings(fixture, managed):
                result = run()
        else:
            result = run()
        if result.timed_out or result.returncode != 0:
            raise _Inconclusive("a run did not complete")
        return responder.models

    def test_mod_rewrites_stay_inside_the_fence(self) -> None:
        import test_compiler_v2  # the production compile (module import: no tests re-exported)

        _lineup, compiled = test_compiler_v2._seed_launch("balanced", default_mode=scope.PERMISSION_DEFAULT_MODE)
        permissions = json.loads(json.dumps(compiled.scope_plan.settings["permissions"]))
        self.assertIs(compiled.scope_plan.settings["disableAllHooks"], False)
        observed: dict[str, dict[str, list[str]]] = {}
        try:
            observed["control"] = self._case(permissions, None)
            observed["resume-rewrite"] = self._case(permissions, _REWRITE_RESUMED % _OTHER)
            observed["outside"] = self._case(permissions, _REWRITE_EVERY % _OUTSIDE)
            observed["managed"] = self._case(permissions, _REWRITE_EVERY % _OTHER, {"allowManagedHooksOnly": True})
            observed["inside"] = self._case(permissions, _REWRITE_EVERY % _OTHER)
            observed_class = verdict(observed)
        except _Inconclusive as exc:
            type(self).verdict = ("INCONCLUSIVE", "unavailable", str(exc))
            raise
        except ProbeError as exc:
            if "live daemon domain touched" in str(exc):
                type(self).verdict = ("BOUNDARY", "unavailable", "live daemon-domain tripwire hit")
                self.skipTest("BOUNDARY: live daemon-domain tripwire hit")
            type(self).verdict = ("INCONCLUSIVE", "unavailable", "probe execution unavailable")
            raise
        detail = (" ".join(f"{case}={_phases(rows)}" for case, rows in observed.items())
                  + " trust-boundary=mods-are-user-authority(managed allowManagedHooksOnly keeps them out) "
                  f"client={self.gate.version} sha256={self.trusted.sha256[:12]}")
        type(self).verdict = ("PASS" if observed_class == PASS_CLASS else "FAIL", observed_class, detail)
        self.assertEqual(observed_class, PASS_CLASS, detail)


if __name__ == "__main__":
    unittest.main()
