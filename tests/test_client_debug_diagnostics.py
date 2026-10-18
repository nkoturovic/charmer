"""Exact-client fixture: Claude Code's ``--debug`` diagnostics on a compiled scope.

Network-hermetic like every real-binary probe (``bwrap --unshare-net``,
loopback fake provider, fixture homes). One headless run of the production
compile of a full lineup (every role's generated agent file, one with its
own window) and its six compiled hooks, answered by a fixture stand-in for
``bin/charmer-hook-3``, with ``--debug-file`` inside the fixture. The debug
log is read in memory only: it keeps counts, hook event names, outcomes and
durations, never a line's text.

It checks that the client names no generated agent file as having an
invalid ``autoCompactWindow`` or a frontmatter key it does not recognize or
apply, and records each hook's outcome and duration. Debug output is a
fixture-only check: it is never enabled in a managed session.

The output line: ``client check debug-diagnostics: <STATUS> <detail>``.
"""

from __future__ import annotations

import copy
import json
import re
import sys
import unittest
import uuid

import test_scope_probe_client as client_probe
from _catalog import FIXTURE_ROOT
from _tier import real_binary_gate
from charmer import catalog, probe, profile, scope, settings, state
from test_agent_context import bounded, codex_line, narrowed_balanced

LINE_PREFIX = "client check debug-diagnostics:"
_HINTS = {"CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1"}
_AGENT = "cm-analyst"
_SMALL_BOUND = 128_000
# What the client says about an agent file it reads (2.1.302 wording).
AGENT_COMPLAINTS = (
    re.compile(r"has invalid autoCompactWindow"),
    re.compile(r"frontmatter keys? Claude Code does not recognize or apply"),
    re.compile(r"has invalid (?:effort|permissionMode|maxTurns)"),
    re.compile(r"Error parsing agent from"),
)


def line(status: str, detail: str) -> str:
    return f"{LINE_PREFIX} {status} {detail}".rstrip()


def compile_lineup(shim) -> tuple[profile.ResolvedLineup, scope.ScopePlan, dict]:
    """The balanced fixture seed with ``cm-analyst`` on a line bound below
    the 200K class (its agent file carries its own window): the production
    scope (fake-provider settings) and the compiled hooks naming ``shim``."""

    bundle = catalog.load_catalog(FIXTURE_ROOT)
    key = codex_line(bundle.docs)
    docs = bounded(copy.deepcopy(bundle.docs), key, _SMALL_BOUND)
    lcat = profile.LineupCatalog.from_docs(docs)
    eff = settings.effective({"version": 1}, provider_ids=lcat.providers, line_keys=lcat.lines)
    document = narrowed_balanced(bundle)
    document["agents"][_AGENT] = {"model": key, "effort": docs["models"]["models"][key]["default_effort"]}
    lineup = profile.resolve(document, lcat, effective=eff)
    meta = scope.catalog_meta_v2(docs)
    plan = scope.compile_lineup_scope(lineup, lcat, eff, bundle.prompt_bodies, meta, lineup_generation=1)
    # Only the hooks come from the lifecycle compile: its env names the
    # gateway, never the fake provider.
    hooked = scope.compile_lineup_scope(lineup, lcat, eff, bundle.prompt_bodies, meta, lineup_generation=1,
                                        managed_id=str(uuid.uuid4()), hook_command=str(shim))
    return lineup, plan, hooked.settings["hooks"]


def stand_in(log) -> bytes:
    """A shim that records the event it was given and answers as a healthy
    launcher does (an allow for the model fence, nothing for the others)."""

    return f'''#!{sys.executable}
import json, os, sys
event = sys.argv[2] if len(sys.argv) > 2 else "unknown"
fd = os.open({str(log)!r}, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
with os.fdopen(fd, "w") as out: out.write(json.dumps({{"event": event}}) + "\\n")
if event == "premodel":
    print(json.dumps({{"hookSpecificOutput": {{"hookEventName": "PreModelSwitch",
                      "permissionDecision": "allow", "permissionDecisionReason": "fixture"}}}}))
'''.encode()


def agent_complaints(text: str, agent_dir: str) -> int:
    """How many debug lines name a generated agent file with a complaint."""

    return sum(1 for row in text.splitlines()
               if agent_dir in row and any(pattern.search(row) for pattern in AGENT_COMPLAINTS))


# A compiled hook's run in the 2.1.302 debug log: ``Hooks: <Event>[:<source>]
# [<command>] finished with status <n> (<ms>ms)`` (SessionEnd: ``completed
# with status <n>``, without a duration).
_HOOK_RUN = re.compile(r"(?P<event>[A-Za-z]+)(?::[\w.-]+)? \[[^\]]*charmer-hook-3[^\]]*\] "
                       r"(?:finished|completed) with status (?P<status>-?\d+)(?: \((?P<ms>\d+)ms\))?")


def hook_runs(text: str) -> list[dict]:
    """Each compiled hook run the debug log reports, as ``{event, status,
    ms}`` (``ms`` None when the client states none)."""

    runs = []
    for row in text.splitlines():
        match = _HOOK_RUN.search(row)
        if match is not None:
            runs.append({"event": match.group("event"), "status": int(match.group("status")),
                         "ms": int(match.group("ms")) if match.group("ms") else None})
    return runs


class _SpawnOnce(client_probe._Recorder):
    """The lead spawns ``cm-analyst`` once; every other request answers."""

    def __init__(self):
        super().__init__()
        self.spawned = False
        self.child_models: set[str] = set()

    def message(self, document, record, hints):
        if hints.get("x-claude-code-agent-id"):
            self.child_models.add(record.model)
            return client_probe._reply(document)
        if not self.spawned and "Agent" in record.tool_names:
            self.spawned = True
            return client_probe._reply(document, [client_probe._spawn(1, _AGENT)], stop="tool_use")
        return client_probe._reply(document)


class ComplaintTests(unittest.TestCase):
    def test_complaints_count_only_generated_agent_files(self) -> None:
        text = "\n".join((
            "Agent file /s/.claude/agents/cm-x.md has invalid autoCompactWindow '5'. Must be an integer",
            "Agent file /s/.claude/agents/cm-y.md has a frontmatter key Claude Code does not recognize or apply: z",
            "Agent file /other/agents/a.md has invalid autoCompactWindow '5'.",
            "Agent file /s/.claude/agents/cm-z.md loaded",
        ))
        self.assertEqual(agent_complaints(text, "/s/.claude/agents"), 2)
        self.assertEqual(agent_complaints("", "/s/.claude/agents"), 0)

    def test_hook_runs_keep_the_event_status_and_duration(self) -> None:
        text = "\n".join((
            "t [DEBUG] Hooks: SessionStart:startup [/f/state/bin/charmer-hook-3 session-event start --managed-id x "
            "--launch-epoch 0 --hook-protocol 3] finished with status 0 (33ms)",
            "t [DEBUG] Hook output does not start with {, treating as plain text",
            "t [DEBUG] Hooks: SubagentStart:cm-analyst [/f/state/bin/charmer-hook-3 session-event subagent] "
            "finished with status 1 (25ms)",
            "t [DEBUG] SessionEnd:other [/f/state/bin/charmer-hook-3 session-event end] completed with status 0",
            "t [DEBUG] Hooks: PreToolUse [/usr/bin/other] finished with status 0 (3ms)",
        ))
        self.assertEqual(hook_runs(text), [{"event": "SessionStart", "status": 0, "ms": 33},
                                           {"event": "SubagentStart", "status": 1, "ms": 25},
                                           {"event": "SessionEnd", "status": 0, "ms": None}])


class DebugDiagnosticsProbe(client_probe._ProbeCase, unittest.TestCase):
    probe_id = "debug-diagnostics"
    test_observation = None

    @classmethod
    def setUpClass(cls):
        cls.verdict = ("INCONCLUSIVE", "probe did not complete")
        cls.gate = real_binary_gate(client_probe._CONTRACT, what="the debug diagnostics check")
        if cls.gate.boundary:
            cls.verdict = ("BOUNDARY", cls.gate.boundary)

    @classmethod
    def tearDownClass(cls):
        print(line(*cls.verdict), flush=True)

    def test_generated_agents_and_hooks_under_debug(self):
        try:
            fixture = self._fixture()
            log = fixture.root / "events.jsonl"
            debug = fixture.root / "debug.log"
            shim = state.ensure_private_dir(fixture.root / "state/bin") / "charmer-hook-3"
            state.atomic_write(shim, stand_in(log))
            shim.chmod(0o700)
            lineup, plan, hooks = compile_lineup(shim)
            document = copy.deepcopy(plan.settings)
            document["env"] = dict(document["env"], **_HINTS)
            document["hooks"] = hooks
            live = scope.write_scope(fixture.root / "state", str(uuid.uuid4()), plan)
            self.assertIn(b"autoCompactWindow: ", plan.agent_files[f".claude/agents/{_AGENT}.md"])
            responder = _SpawnOnce()
            result, responder = self._run(fixture, responder=responder, model=lineup.lead.binding.selector,
                                          extra=("--add-dir", str(live), "--debug-file", str(debug)),
                                          document=document, timeout=180)
            self._complete(result)
            text = debug.read_text(errors="replace") if debug.exists() else ""
            if not text:
                raise client_probe._Inconclusive("the client wrote no debug log")
            complaints = agent_complaints(text, str(live / ".claude/agents"))
            runs = hook_runs(text)
            events = sorted({json.loads(row)["event"] for row in log.read_text().splitlines()}) \
                if log.exists() else []
            self.evidence.update(complaints=complaints, hook_runs=runs, shim_events=events,
                                 spawned=responder.spawned, agent_files=len(plan.agent_files))
            if not responder.spawned:
                raise client_probe._Inconclusive("the lead never spawned the agent")
            detail = (f"agents={len(plan.agent_files)} complaints={complaints} shim_events={','.join(events)} "
                      "hooks=" + ",".join(f"{run['event']}:status-{run['status']}"
                                          + (f":{run['ms']}ms" if run["ms"] is not None else "") for run in runs))
            failed = [run for run in runs if run["status"] != 0]
            if not runs:
                raise client_probe._Inconclusive("the debug log names no compiled hook run")
            if complaints or failed:
                self.__class__.verdict = ("FAIL", detail)
                self.fail(detail)
            self.__class__.verdict = ("PASS", detail)
        except unittest.SkipTest as exc:
            self.__class__.verdict = ("BOUNDARY", str(exc))
            raise
        except client_probe._Inconclusive as exc:
            self.__class__.verdict = ("INCONCLUSIVE", str(exc))
            self.fail(str(exc))
        except probe.ProbeError as exc:
            if "live daemon domain touched" in str(exc):
                self.__class__.verdict = ("BOUNDARY", "live daemon-domain tripwire hit")
                self.skipTest("BOUNDARY: live daemon-domain tripwire hit")
            self.__class__.verdict = ("INCONCLUSIVE", "probe execution unavailable")
            self.fail("probe execution unavailable (stdio deliberately not retained)")
        finally:
            probe.write_evidence(self._fixture(), self.probe_id, self.evidence)


if __name__ == "__main__":
    unittest.main()
