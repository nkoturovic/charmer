"""Client checks spawn-mod and spawn-mod-binding: an ``agent.spawn`` mod
against a compiled lineup.

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
  it (a hook can only refuse a resumed agent's run);
* ``outside``: a mod rewriting every spawn to a model outside the fence;
* ``managed``: the in-fence mod under a managed ``allowManagedHooksOnly``;
* ``inside``: the same mod rewriting every spawn to another lineup model;
* ``inside-model-deny``: the in-fence mod with a deny ``Agent(model:*)``
  added to the permissions (a candidate mitigation, measured only).

Two distinct verdicts come from the same runs:

* ``spawn-mod`` (fence membership, a mandatory re-pin check): no model
  outside the fence ever reaches the wire, a resumed agent's rewrite is
  ignored and a managed ``allowManagedHooksOnly`` keeps the mod out
  (class ``SM-fenced``);
* ``spawn-mod-binding`` (the compiled binding): every agent stays on its
  compiled model under the outside and inside mods (class ``SB-compiled``).
  On 2.1.302 it does not hold (``SB-rebound``): the outside mod lands every
  agent on the lead's model and the inside mod moves fresh and workflow
  agents onto the mod's model (the resumed agent keeps its spawn's). Its
  test is a known failure (``unittest.expectedFailure``): the record says
  FAIL, and a client or a compile that keeps the binding turns it into an
  unexpected success that fails the run. The measured mitigation: the deny
  ``Agent(model:*)`` refuses the rewritten Agent-tool spawn ("a permission
  rule denies") but not the workflow agent's (observed while building the
  check: it also refuses the lead's own per-call model override, and a
  Workflow script's own ``model`` option moves a workflow agent with or
  without it). Nothing in the compiled ``--settings`` scope
  keeps a user's mod out without stopping charmer's own hooks (only managed
  ``allowManagedHooksOnly``, which doctor reports as a managed lock).

Every case and phase must show agent requests (a missing stimulus is
INCONCLUSIVE, never a pass), and every wire model is checked against the
fixture fence. Hermetic like every real-client check (loopback-only network
namespace, fixture homes, the fake provider); the managed file is mounted
inside the namespace only (``tests/_client_policy.py``). Only models, phase
labels and result classes leave the responder. The output lines:
``client check spawn-mod: <STATUS> class=<class> <detail>`` and ``client
check spawn-mod-binding: <STATUS> class=<class> <detail>``.
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

from charmer import probe, scope, state
from charmer.probe import ProbeError

from _client_policy import managed_settings
from _layout import RESOURCES_ROOT
from _tier import real_binary_gate

PASS_CLASS = "SM-fenced"
BINDING_CLASS = "SB-compiled"
_CONTRACT = RESOURCES_ROOT / "catalog" / "native-contract.json"
_LEAD = "spawn-mod-lead"
_AGENT = "spawn-mod-agent"
_OTHER = "spawn-mod-other"  # another lineup model, inside the fence
_OUTSIDE = "spawn-mod-outside"  # outside the fence
_FENCE = frozenset({_LEAD, _AGENT, _OTHER})
_AGENT_TYPE = "cm-spawn-mod"
_BODY = "SPAWN-MOD-AGENT-BODY"
_RESUME = "SPAWN-MOD-RESUME"
_WORKFLOW = "SPAWN-MOD-WF"
_AGENT_ID_RE = re.compile(r"agentId: ([A-Za-z0-9_-]{4,64})")
_PHASES = ("fresh", "resume", "workflow")
_CASES = ("control", "resume-rewrite", "outside", "managed", "inside")
_MODEL_DENY = "Agent(model:*)"
_RULE_REFUSAL = "rewrote this spawn into one a permission rule denies"
_REWRITE_EVERY = """export const register = (on) => {
  on('agent.spawn', ($, e, next) => next({ ...e, model: '%s' }))
}
"""
_REWRITE_RESUMED = """export const register = (on) => {
  on('agent.spawn', ($, e, next) => (e.resume ? next({ ...e, model: '%s' }) : next(e)))
}
"""


class _Inconclusive(AssertionError):
    """A control did not hold: no class can be chosen."""


def _complete(observed: dict[str, dict[str, Any]]) -> None:
    """Raise :class:`_Inconclusive` unless every case and phase has agent requests."""

    for case in _CASES:
        rows = observed.get(case)
        if rows is None:
            raise _Inconclusive(f"{case}: not run")
        for phase in _PHASES:
            if not rows.get(phase):
                raise _Inconclusive(f"{case} {phase}: no agent request (the stimulus did not run)")


def _controls(observed: dict[str, dict[str, Any]]) -> None:
    """Raise :class:`_Inconclusive` unless every case and phase ran and the
    controls held: the control on the compiled model, the resume-only mod
    loaded (its resumed rewrite ignored, the client says) and leaving fresh
    and workflow spawns alone, and the outside and inside mods loaded (the
    client logs each fresh and workflow spawn's rewrite, whatever it then
    does with it)."""

    _complete(observed)
    for phase in _PHASES:
        if observed["control"][phase] != [_AGENT]:
            raise _Inconclusive(f"control {phase}: {observed['control'][phase]} (the compiled model did not run)")
    if observed["control"].get("rewrites", 0):
        raise _Inconclusive("control: a rewrite was logged without a mod")
    if not observed["resume-rewrite"].get("resume_ignored"):
        raise _Inconclusive("resume-rewrite: the client never ignored a resumed rewrite (the mod did not load?)")
    for phase in ("fresh", "workflow"):
        if observed["resume-rewrite"][phase] != [_AGENT]:
            raise _Inconclusive(f"resume-rewrite {phase}: {observed['resume-rewrite'][phase]} "
                                "(the resume-only mod changed a spawn that was not resumed)")
    for case in ("outside", "inside"):
        if observed[case].get("rewrites", 0) < 2:
            raise _Inconclusive(f"{case}: {observed[case].get('rewrites', 0)} rewrites logged "
                                "(the mod did not rewrite the fresh and the workflow spawn: not loaded?)")


def verdict(observed: dict[str, dict[str, Any]]) -> str:
    """The fence-membership class from each case's wire models per phase (a
    model outside the fence fails whichever case carries it)."""

    _complete(observed)
    if any(model not in _FENCE for case in _CASES for phase in _PHASES for model in observed[case][phase]):
        return "SM-outside-on-wire"
    _controls(observed)
    if observed["resume-rewrite"]["resume"] != [_AGENT]:
        return "SM-resume-rewritten"
    if observed["managed"].get("rewrites", 0) or any(observed["managed"][phase] != [_AGENT] for phase in _PHASES):
        return "SM-managed-loaded"
    return PASS_CLASS


def binding_verdict(observed: dict[str, dict[str, Any]]) -> str:
    """The compiled-binding class: every agent of the outside and inside
    cases on its compiled model (the controls as for :func:`verdict`)."""

    _controls(observed)
    if any(observed[case][phase] != [_AGENT] for case in ("outside", "inside") for phase in _PHASES):
        return "SB-rebound"
    return BINDING_CLASS


def moved(observed: dict[str, dict[str, Any]]) -> list[str]:
    """``case.phase=models`` for each agent off its compiled model."""

    return [f"{case}.{phase}={'/'.join(observed[case][phase])}" for case in ("outside", "inside")
            for phase in _PHASES if observed[case][phase] != [_AGENT]]


def _phases(rows: dict[str, Any]) -> str:
    return ",".join(f"{phase}:{'/'.join(rows.get(phase) or ()) or '-'}" for phase in _PHASES)


class _Responder:
    """Spawn the fixture cm agent, continue it with SendMessage (retried
    while its transcript is not yet resumable), then run one Workflow
    ``agent()`` of the same type; keep each agent request's wire model under
    its phase and the class of the spawn's tool result."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.step = 0
        self.agent_id: str | None = None
        self.sends = 0
        self.spawn_result = "none"
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
                    spawned = results["toolu_spawn_mod_spawn"]
                    match = _AGENT_ID_RE.search(spawned)
                    self.agent_id = match.group(1) if match else None
                    self.spawn_result = ("refused-by-rule" if _RULE_REFUSAL in spawned
                                         else "ran" if match else "other")
                    self.step = 2
                if self.step == 2:
                    last = f"toolu_spawn_mod_send_{self.sends}"
                    resumed = last in results and '\\"success\\":true' in results[last]
                    retry = last in results and not resumed and self.sends < 3 and self.agent_id is not None
                    if self.sends == 0 or retry:
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


def _case(fresh: Any, resume: Any, workflow: Any, **counts: int) -> dict[str, Any]:
    return {"fresh": list(fresh), "resume": list(resume), "workflow": list(workflow), **counts}


class VerdictTests(unittest.TestCase):
    """The rows as measured on 2.1.302 (``probe-baselines.txt``)."""

    def setUp(self) -> None:
        agent = (_AGENT,)
        self.rows = {"control": _case(agent, agent, agent),
                     "resume-rewrite": _case(agent, agent, agent, resume_ignored=1),
                     "outside": _case((_LEAD,), (_LEAD,), (_LEAD,), rewrites=2),
                     "managed": _case(agent, agent, agent),
                     "inside": _case((_OTHER,), (_OTHER,), (_OTHER,), rewrites=2)}

    def _with(self, case: str, **changes: Any) -> dict[str, Any]:
        rows = {name: dict(phases) for name, phases in self.rows.items()}
        rows[case].update(changes)
        return rows

    def test_the_class_needs_the_fence_resume_and_managed_cases(self) -> None:
        self.assertEqual(verdict(self.rows), PASS_CLASS)
        self.assertEqual(verdict(self._with("outside", fresh=[_OUTSIDE])), "SM-outside-on-wire")
        self.assertEqual(verdict(self._with("resume-rewrite", resume=[_OTHER])), "SM-resume-rewritten")
        self.assertEqual(verdict(self._with("managed", fresh=[_OTHER], resume=[_OTHER], workflow=[_OTHER])),
                         "SM-managed-loaded")
        self.assertEqual(verdict(self._with("managed", rewrites=1)), "SM-managed-loaded")
        for case, changes in (("control", {"workflow": [_LEAD]}), ("control", {"rewrites": 1}),
                              ("resume-rewrite", {"fresh": [_OTHER]}), ("resume-rewrite", {"resume_ignored": 0}),
                              ("outside", {"rewrites": 1}), ("inside", {"rewrites": 0})):
            with self.subTest(case=case, changes=changes), self.assertRaises(_Inconclusive):
                verdict(self._with(case, **changes))

    def test_every_model_outside_the_fence_fails_whatever_its_name(self) -> None:
        stray = "another-outside-fence"
        for case in _CASES:
            for phase in _PHASES:
                rows = self._with(case, **{phase: [*self.rows[case][phase], stray]})
                with self.subTest(case=case, phase=phase):
                    self.assertEqual(verdict(rows), "SM-outside-on-wire")
        self.assertEqual(verdict(self._with("outside", fresh=[stray], resume=[stray], workflow=[stray])),
                         "SM-outside-on-wire")

    def test_a_missing_case_or_phase_is_inconclusive(self) -> None:
        for case in _CASES:
            with self.subTest(case=case, missing="case"), self.assertRaises(_Inconclusive):
                verdict({name: phases for name, phases in self.rows.items() if name != case})
            for phase in _PHASES:
                for shape in ("empty", "absent"):
                    rows = self._with(case)
                    if shape == "empty":
                        rows[case][phase] = []
                    else:
                        del rows[case][phase]
                    with self.subTest(case=case, phase=phase, shape=shape):
                        with self.assertRaises(_Inconclusive):
                            verdict(rows)
                        with self.assertRaises(_Inconclusive):
                            binding_verdict(rows)
        rows = self._with("outside", fresh=[], resume=[], workflow=[])
        rows["inside"] = {"fresh": [_OTHER], "rewrites": 2}
        with self.assertRaises(_Inconclusive):
            verdict(rows)

    def test_the_binding_needs_every_rewritten_agent_on_its_compiled_model(self) -> None:
        self.assertEqual(binding_verdict(self.rows), "SB-rebound")
        self.assertEqual(moved(self.rows), [f"{case}.{phase}={model}" for case, model in (("outside", _LEAD),
                                                                                          ("inside", _OTHER))
                                            for phase in _PHASES])
        compiled = {phase: [_AGENT] for phase in _PHASES}
        held = self._with("outside", **compiled)
        held["inside"].update(compiled)
        self.assertEqual(binding_verdict(held), BINDING_CLASS)
        self.assertEqual(moved(held), [])
        for case in ("outside", "inside"):
            for phase in _PHASES:
                rows = {name: dict(phases) for name, phases in held.items()}
                rows[case][phase] = [_OTHER]
                with self.subTest(case=case, phase=phase):
                    self.assertEqual(binding_verdict(rows), "SB-rebound")
        with self.assertRaises(_Inconclusive):  # a mod that did not load proves nothing
            binding_verdict({**held, "inside": {**held["inside"], "rewrites": 0}})


class SpawnModProbe(unittest.TestCase):
    probe_id = "spawn-mod"
    binding_id = "spawn-mod-binding"
    verdict: tuple[str, str, str] = ("INCONCLUSIVE", "unavailable", "check did not complete")
    binding: tuple[str, str, str] = ("INCONCLUSIVE", "unavailable", "check did not complete")
    _observed: dict[str, Any] | None = None
    _inconclusive: str | None = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.gate = real_binary_gate(_CONTRACT, what="the spawn-mod check")
        if cls.gate.boundary:
            cls.verdict = cls.binding = ("BOUNDARY", "unavailable", cls.gate.boundary)

    @classmethod
    def tearDownClass(cls) -> None:
        status, observed, detail = cls.verdict
        print(f"client check {cls.probe_id}: {status} class={observed} {detail}".rstrip(), flush=True)
        status, observed, detail = cls.binding
        print(f"client check {cls.binding_id}: {status} class={observed} {detail}".rstrip(), flush=True)

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

    def _run(self, permissions: dict[str, Any], mod_body: str | None,
             managed: dict[str, Any] | None = None) -> dict[str, Any]:
        """One case: its wire models per phase, the spawn result's class and
        the counts of the client's own lines for a hook's rewrite of this
        agent type and for an ignored resumed rewrite (only counts leave the
        fixture's debug log)."""

        fixture = self._fixture()
        settings = probe.write_fixture_settings(fixture, "scope/settings.json", {
            "availableModels": sorted(_FENCE), "model": _LEAD, "permissions": permissions,
            "disableAllHooks": False})
        extra: tuple[str, ...] = ()
        if mod_body is not None:
            extra = ("--plugin-dir", str(self._mod(fixture, mod_body)))
        debug = fixture.root / "debug.log"
        responder = _Responder()

        def run() -> probe.NativeRunResult:
            with probe.FakeAnthropicProvider(responder=responder) as provider:
                return probe.run_native(
                    ("-p", "client check spawn-mod", "--settings", str(settings), "--model", _LEAD, *extra,
                     "--dangerously-skip-permissions", "--debug-file", str(debug)),
                    trusted=self.trusted, fixture=fixture, provider=provider, timeout=150, allow_real=True,
                    environ=self.environ)

        if managed is not None:
            with managed_settings(fixture, managed):
                result = run()
        else:
            result = run()
        if result.timed_out or result.returncode != 0:
            raise _Inconclusive("a run did not complete")
        if responder.step != 3:
            raise _Inconclusive(f"a run stopped at step {responder.step} (the workflow agent was never started)")
        lines = debug.read_text(encoding="utf-8", errors="replace").splitlines() if debug.is_file() else []
        rewrite = re.compile(rf"agent\.spawn {re.escape(_AGENT_TYPE)}: model \S+ -> \S+ by a hook")
        return {**responder.models, "spawn": responder.spawn_result,
                "rewrites": sum(1 for line in lines if rewrite.search(line)),
                "resume_ignored": sum(1 for line in lines if "a hook can only refuse a resumed agent's run" in line)}

    def _observations(self) -> dict[str, Any]:
        """Every case's observations, measured once for both verdicts."""

        cls = type(self)
        if cls._inconclusive is not None:
            raise _Inconclusive(cls._inconclusive)
        if cls._observed is not None:
            return cls._observed
        import test_compiler_v2  # the production compile (module import: no tests re-exported)

        _lineup, compiled = test_compiler_v2._seed_launch("balanced", default_mode=scope.PERMISSION_DEFAULT_MODE)
        permissions = json.loads(json.dumps(compiled.scope_plan.settings["permissions"]))
        self.assertIs(compiled.scope_plan.settings["disableAllHooks"], False)
        self.assertNotIn(_MODEL_DENY, permissions.get("deny", []))
        mitigated = {**permissions, "deny": [*permissions.get("deny", []), _MODEL_DENY]}
        observed: dict[str, Any] = {}
        try:
            observed["control"] = self._run(permissions, None)
            observed["resume-rewrite"] = self._run(permissions, _REWRITE_RESUMED % _OTHER)
            observed["outside"] = self._run(permissions, _REWRITE_EVERY % _OUTSIDE)
            observed["managed"] = self._run(permissions, _REWRITE_EVERY % _OTHER, {"allowManagedHooksOnly": True})
            observed["inside"] = self._run(permissions, _REWRITE_EVERY % _OTHER)
            observed["inside-model-deny"] = self._run(mitigated, _REWRITE_EVERY % _OTHER)
        except _Inconclusive as exc:
            cls._inconclusive = str(exc)
            cls.verdict = cls.binding = ("INCONCLUSIVE", "unavailable", str(exc))
            raise
        except ProbeError as exc:
            if "live daemon domain touched" in str(exc):
                cls.verdict = cls.binding = ("BOUNDARY", "unavailable", "live daemon-domain tripwire hit")
                cls._inconclusive = "BOUNDARY: live daemon-domain tripwire hit"
                self.skipTest("BOUNDARY: live daemon-domain tripwire hit")
            cls.verdict = cls.binding = ("INCONCLUSIVE", "unavailable", "probe execution unavailable")
            cls._inconclusive = "probe execution unavailable"
            raise
        cls._observed = observed
        return observed

    def _settle(self, attribute: str, check: Any) -> str:
        observed = self._observations()
        try:
            return check(observed)
        except _Inconclusive as exc:
            setattr(type(self), attribute, ("INCONCLUSIVE", "unavailable", str(exc)))
            raise

    def test_mod_rewrites_stay_inside_the_fence(self) -> None:
        observed_class = self._settle("verdict", verdict)
        observed = self._observations()
        detail = (" ".join(f"{case}={_phases(observed[case])}" for case in _CASES)
                  + f" rewrites-logged=outside:{observed['outside']['rewrites']},"
                  f"inside:{observed['inside']['rewrites']},managed:{observed['managed']['rewrites']} "
                  f"resumed-rewrite-ignored={observed['resume-rewrite']['resume_ignored']}"
                  " (fence membership only: the compiled binding is client check spawn-mod-binding) "
                  f"client={self.gate.version} sha256={self.trusted.sha256[:12]}")
        type(self).verdict = ("PASS" if observed_class == PASS_CLASS else "FAIL", observed_class, detail)
        self.assertEqual(observed_class, PASS_CLASS, detail)

    @unittest.expectedFailure
    def test_compiled_binding_survives_mod_rewrites(self) -> None:
        """Known failure on 2.1.302 (module docstring): an in-fence rewrite
        moves fresh and workflow agents, an outside one lands them on the
        lead; no compiled setting that keeps charmer's hooks prevents it."""

        observed_class = self._settle("binding", binding_verdict)
        observed = self._observations()
        deny = observed["inside-model-deny"]
        detail = (f"moved=[{' '.join(moved(observed))}] "
                  f"measured mitigation deny {_MODEL_DENY} with the inside mod: Agent-tool spawn={deny['spawn']} "
                  f"workflow={'/'.join(deny['workflow']) or '-'}; "
                  "trust boundary: the compiled --settings scope keeps hooks on for charmer's own "
                  "hooks, only managed allowManagedHooksOnly keeps mods out (and stops charmer's hooks); "
                  "the binding requirement is open "
                  f"client={self.gate.version} sha256={self.trusted.sha256[:12]}")
        type(self).binding = ("PASS" if observed_class == BINDING_CLASS else "FAIL", observed_class, detail)
        self.assertEqual(observed_class, BINDING_CLASS, detail)


if __name__ == "__main__":
    unittest.main()
