"""Client check mcp-policy: MCP permission matching and policy before start.

charmer compiles no MCP configuration: a managed session inherits the user's
MCP servers and permission rules and any managed MCP policy. Claude Code
2.1.299-2.1.302 changed both halves of that inheritance:

* an MCP allow rule (and an agent's tools list) matches only the named
  normalised server, so ``mcp__fx`` no longer grants ``fx__x`` or ``fx_``, and
  deny matching for names with ``__`` is exact;
* MCP policy is checked before a server is started, for launch, subagent
  servers and resumed sessions.

Fake stdio servers (a fixture script) log each start and each tool call;
their names overlap after normalisation and carry ``__``. Every run carries
the production compile's permissions with a fixture fence as flag settings.

* ``match-control``: user allow rules ``mcp__<server>__*`` for ``fx``, ``fx_``
  and ``fx__x``: each server's ``probe`` tool runs once (the stimulus is
  real);
* ``match-wild``: user allow ``mcp__fx__*`` alone: only ``fx``'s tool runs;
* ``match``: user allow ``mcp__fx`` and deny ``mcp__fx__x``: only ``fx``'s
  tool runs; ``fx_``'s waits for a permission ``-p`` cannot give, and
  ``fx__x``'s is not offered (the deny names that whole server);
* ``counter-control``: no MCP policy: the user server ``fxdenied`` starts at
  launch, on ``--resume`` and at a terminal session's start, an agent's own
  server ``fxdeniedagent`` starts when the agent is spawned, and a plugin
  server added to a ``--plugin-dir`` fixture once the terminal session read
  the plugin's MCP config (its never-denied ``fxready`` started)
  (``plugin:mcpfix:fxreload``) starts on ``/reload-plugins`` and not before
  it;
* ``counter``: a managed ``deniedMcpServers`` naming the three: none ever
  starts (launch, subagent spawn, resume, terminal start, reload).

Each matching case must give every tool a classified result (ran, not
granted, not offered) and ``fx``'s must run once; each start the policy
case refuses must happen in the control; a missing stimulus is
INCONCLUSIVE, never a pass.

Hermetic like every real-client check; the managed file is mounted inside
the namespace only (``tests/_client_policy.py``). Only tool-result classes,
server names and counts leave the run. The output line: ``client check
mcp-policy: <STATUS> class=<class> <detail>``.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from charmer import probe, scope, state, strict_json
from charmer.probe import ProbeError

from _client_policy import etc_view, managed_settings
from _layout import RESOURCES_ROOT
from _tier import real_binary_gate

PASS_CLASS = "MCP-named-deny-first"
_CONTRACT = RESOURCES_ROOT / "catalog" / "native-contract.json"
_LEAD = "mcp-policy-lead"
_AGENT = "mcp-policy-agent"
_AGENT_TYPE = "cm-mcp-policy"
_BODY = "MCP-POLICY-AGENT-BODY"
_MATCH_SERVERS = ("fx", "fx_", "fx__x")
_DENIED, _DENIED_AGENT = "fxdenied", "fxdeniedagent"
# The fixture plugin's servers: fxready (never denied) from the start, and
# fxreload, written once fxready has started in the terminal session (the
# plugin's MCP config was read: a server written before that would start
# with the session, not on the reload). A plugin server's policy name is
# plugin:<plugin>:<server> (a managed deny of the bare "fxreload" does not
# cover it: observed while building the check).
_PLUGIN, _READY, _RELOADED = "mcpfix", "fxready", "fxreload"
_RELOADED_POLICY_NAME = f"plugin:{_PLUGIN}:{_RELOADED}"
_CLASSIFIED = frozenset({"ran", "not-granted", "unavailable"})
_SERVER = r'''import json, sys
name, log = sys.argv[1], sys.argv[2]
def note(line):
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
note("start " + name)
for raw in sys.stdin:
    try:
        message = json.loads(raw)
    except ValueError:
        continue
    method, ident = message.get("method"), message.get("id")
    if ident is None:
        continue
    if method == "initialize":
        result = {"protocolVersion": message.get("params", {}).get("protocolVersion", "2025-06-18"),
                  "capabilities": {"tools": {}}, "serverInfo": {"name": name, "version": "1.0.0"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "probe", "description": "client check tool",
                             "inputSchema": {"type": "object", "properties": {}}}]}
    elif method == "tools/call":
        note("call " + name)
        result = {"content": [{"type": "text", "text": "MCP-POLICY-RAN"}]}
    else:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": ident,
                                     "error": {"code": -32601, "message": "unknown"}}) + "\n")
        sys.stdout.flush()
        continue
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": ident, "result": result}) + "\n")
    sys.stdout.flush()
'''


def tool_name(server: str) -> str:
    return f"mcp__{server}__probe"


def result_class(content: Any) -> str:
    """One MCP tool result's class (its text never leaves memory)."""

    text = json.dumps(content)
    if "MCP-POLICY-RAN" in text:
        return "ran"
    if "haven't granted it yet" in text:
        return "not-granted"
    if "No such tool available" in text:
        return "unavailable"
    return "other"


class _Inconclusive(AssertionError):
    """A control did not hold: no class can be chosen."""


# The starts the control must show, per phase; the policy case refuses each.
_EXPECTED_STARTS = {"launch": (_DENIED, _DENIED_AGENT), "resume": (_DENIED,), "terminal": (_DENIED,),
                    "reload": (_RELOADED,)}


def verdict(observed: dict[str, Any]) -> str:
    """The class from the matching results and the start counts."""

    for case in ("match-control", "match-wild", "match"):
        rows = observed[case]
        unclassified = {server: rows["results"].get(server, "no-result") for server in _MATCH_SERVERS
                        if rows["results"].get(server) not in _CLASSIFIED}
        if unclassified:
            raise _Inconclusive(f"{case}: {unclassified} (a tool call got no classified result)")
        ran = [server for server in _MATCH_SERVERS if rows["results"][server] == "ran"]
        if any(rows["calls"].get(server) != 1 for server in ran):
            raise _Inconclusive(f"{case}: results {rows['results']} calls {rows['calls']} "
                                "(a tool that ran was not called once)")
    control = observed["match-control"]
    if any(control["results"][server] != "ran" for server in _MATCH_SERVERS):
        raise _Inconclusive(f"match-control: {control['results']} (a named server's tool did not run)")
    for case in ("counter-control", "counter"):
        if set(observed[case]["starts"]) != set(_EXPECTED_STARTS):
            raise _Inconclusive(f"{case}: phases {sorted(observed[case]['starts'])} (a phase did not run)")
        if not observed[case]["starts"]["terminal"].get(_READY):
            raise _Inconclusive(f"{case}: the fixture plugin's {_READY} never started in the terminal session "
                                "(its MCP config was not read before the change)")
    starts = observed["counter-control"]["starts"]
    if any(not starts[phase].get(name) for phase, names in _EXPECTED_STARTS.items() for name in names) \
            or starts["terminal"].get(_RELOADED):
        raise _Inconclusive(f"counter-control: {starts} (an unmanaged server did not start in its phase)")
    if observed["match"]["results"]["fx"] != "ran":
        return "MCP-deny-overlaps"
    if observed["match-wild"]["results"]["fx"] != "ran":
        return "MCP-allow-lost"
    for case in ("match", "match-wild"):
        rows = observed[case]
        if rows["calls"].get("fx_") or rows["calls"].get("fx__x") or \
                any(rows["results"][server] == "ran" for server in ("fx_", "fx__x")):
            return "MCP-allow-broad"
    denied = {name for names in _EXPECTED_STARTS.values() for name in names}
    if any(counts.get(name) for counts in observed["counter"]["starts"].values() for name in denied):
        return "MCP-denied-started"
    return PASS_CLASS


class _Caller:
    """Calls each named MCP tool once (in order), then, when ``spawn``,
    spawns the fixture cm agent; keeps each tool result's class."""

    def __init__(self, tools: tuple[str, ...] = (), spawn: bool = False) -> None:
        self.lock = threading.Lock()
        self.tools = tools
        self.spawn = spawn
        self.results: dict[str, str] = {}
        self.agent_ran = False

    def respond(self, document: dict[str, Any], path: str, context: probe.RequestContext) -> tuple[int, Any]:
        if path.split("?", 1)[0].endswith("/count_tokens"):
            return 200, {"input_tokens": 1}
        names = {tool.get("name") for tool in document.get("tools") or () if isinstance(tool, dict)}
        content: list[dict[str, Any]] = [{"type": "text", "text": "PROBE-OK"}]
        with self.lock:
            if _BODY in json.dumps(document.get("system")):
                self.agent_ran = True
            else:
                answered = {block.get("tool_use_id"): block.get("content")
                            for message in document.get("messages") or () if isinstance(message, dict)
                            and isinstance(message.get("content"), list)
                            for block in message["content"] if isinstance(block, dict)
                            and block.get("type") == "tool_result"}
                for index, tool in enumerate(self.tools):
                    key = f"toolu_mcp_policy_{index}"
                    if key in answered:
                        self.results.setdefault(tool, result_class(answered[key]))
                pending = [index for index in range(len(self.tools)) if f"toolu_mcp_policy_{index}" not in answered]
                if pending:
                    index = pending[0]
                    content = [{"type": "tool_use", "id": f"toolu_mcp_policy_{index}", "name": self.tools[index],
                                "input": {}}]
                elif self.spawn and "Agent" in names and "toolu_mcp_policy_spawn" not in answered:
                    content = [{"type": "tool_use", "id": "toolu_mcp_policy_spawn", "name": "Agent",
                                "input": {"description": "client check mcp-policy", "prompt": "Reply ok",
                                          "subagent_type": _AGENT_TYPE, "run_in_background": False}}]
        stop = "tool_use" if content[0]["type"] == "tool_use" else "end_turn"
        payload = probe._message_payload(content, model=document.get("model"), stop_reason=stop,
                                         message_id=f"msg_mcp_policy_{context.ordinal:04d}")
        return 200, probe._sse_message(payload) if document.get("stream") else payload


class VerdictTests(unittest.TestCase):
    def setUp(self) -> None:
        ran = {server: "ran" for server in _MATCH_SERVERS}
        self.counts = {"launch": {_DENIED: 1, _DENIED_AGENT: 1}, "resume": {_DENIED: 1},
                       "terminal": {_DENIED: 1, _READY: 1}, "reload": {_RELOADED: 1, _READY: 1}}
        self.named = {"results": {"fx": "ran", "fx_": "not-granted", "fx__x": "not-granted"}, "calls": {"fx": 1}}
        self.rows = {"match-control": {"results": ran, "calls": dict.fromkeys(_MATCH_SERVERS, 1)},
                     "match-wild": self.named,
                     "match": {"results": {"fx": "ran", "fx_": "not-granted", "fx__x": "unavailable"},
                               "calls": {"fx": 1}},
                     "counter-control": {"starts": self.counts},
                     "counter": {"starts": {**dict.fromkeys(_EXPECTED_STARTS, {}), "terminal": {_READY: 1}}}}

    def test_the_class_needs_named_matching_and_no_denied_start(self) -> None:
        rows = self.rows
        self.assertEqual(verdict(rows), PASS_CLASS)
        broad = {"results": {"fx": "ran", "fx_": "ran", "fx__x": "unavailable"}, "calls": {"fx": 1, "fx_": 1}}
        self.assertEqual(verdict({**rows, "match": broad}), "MCP-allow-broad")
        self.assertEqual(verdict({**rows, "match-wild": {**self.named, "calls": {"fx": 1, "fx__x": 1}}}),
                         "MCP-allow-broad")
        overlap = {"results": {"fx": "unavailable", "fx_": "not-granted", "fx__x": "unavailable"}, "calls": {}}
        self.assertEqual(verdict({**rows, "match": overlap}), "MCP-deny-overlaps")
        lost = {"results": {"fx": "not-granted", "fx_": "not-granted", "fx__x": "not-granted"}, "calls": {}}
        self.assertEqual(verdict({**rows, "match-wild": lost}), "MCP-allow-lost")
        for phase, name in (("launch", _DENIED_AGENT), ("terminal", _DENIED), ("reload", _RELOADED)):
            started = {"starts": {**rows["counter"]["starts"]}}
            started["starts"][phase] = {**started["starts"][phase], name: 1}
            self.assertEqual(verdict({**rows, "counter": started}), "MCP-denied-started", phase)
        ready_again = {"starts": {**rows["counter"]["starts"], "reload": {_READY: 1}}}
        self.assertEqual(verdict({**rows, "counter": ready_again}), PASS_CLASS)  # the allowed server may restart

    def test_a_missing_stimulus_is_inconclusive(self) -> None:
        rows = self.rows
        silent = {"results": dict.fromkeys(_MATCH_SERVERS, "no-result"), "calls": {}}
        for case in ("match-control", "match-wild", "match"):
            with self.subTest(case=case, shape="no results"), self.assertRaises(_Inconclusive):
                verdict({**rows, case: silent})
            for server in _MATCH_SERVERS:
                results = {**rows[case]["results"], server: "other"}
                with self.subTest(case=case, server=server), self.assertRaises(_Inconclusive):
                    verdict({**rows, case: {**rows[case], "results": results}})
            with self.subTest(case=case, shape="ran, never called"), self.assertRaises(_Inconclusive):
                verdict({**rows, case: {**rows[case], "calls": {}}})
        with self.assertRaises(_Inconclusive):
            verdict({**rows, "match-control": {"results": {**rows["match-control"]["results"], "fx_": "not-granted"},
                                               "calls": {"fx": 1, "fx__x": 1}}})
        for phase, names in _EXPECTED_STARTS.items():
            for name in names:
                counts = {**self.counts, phase: {key: value for key, value in self.counts[phase].items()
                                                 if key != name}}
                with self.subTest(phase=phase, name=name), self.assertRaises(_Inconclusive):
                    verdict({**rows, "counter-control": {"starts": counts}})
        early = {**self.counts, "terminal": {_DENIED: 1, _READY: 1, _RELOADED: 1}}
        with self.assertRaises(_Inconclusive):  # started before the reload: not the reload's start
            verdict({**rows, "counter-control": {"starts": early}})
        for case in ("counter-control", "counter"):
            unread = {**rows[case]["starts"], "terminal": {key: value for key, value in
                                                           rows[case]["starts"]["terminal"].items() if key != _READY}}
            with self.subTest(case=case, missing=_READY), self.assertRaises(_Inconclusive):
                verdict({**rows, case: {"starts": unread}})
        for case in ("counter-control", "counter"):
            phases = {key: value for key, value in rows[case]["starts"].items() if key != "reload"}
            with self.subTest(case=case, missing="reload"), self.assertRaises(_Inconclusive):
                verdict({**rows, case: {"starts": phases}})


class ManagedViewTests(unittest.TestCase):
    """The managed-settings view (``tests/_client_policy.py``): every host
    ``/etc`` entry back in place, the fixture folder at ``/etc/claude-code``
    and only bwrap options bubblewrap 0.9 (the release runner's) has."""

    def test_the_view_rebuilds_etc_with_old_bwrap_options(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            etc = Path(raw) / "etc"
            (etc / "ssl").mkdir(parents=True)
            (etc / "passwd").write_text("fixture\n")
            os.symlink("../usr/share/zoneinfo/UTC", etc / "localtime")
            (etc / "claude-code").mkdir()  # a host policy is replaced, never shown
            os.mkfifo(etc / "initctl")
            argv = etc_view("/fixture/managed-policy", etc)
        self.assertEqual(argv, ["--tmpfs", "/etc", "--symlink", "../usr/share/zoneinfo/UTC", "/etc/localtime",
                                "--ro-bind", str(etc / "passwd"), "/etc/passwd",
                                "--ro-bind", str(etc / "ssl"), "/etc/ssl",
                                "--ro-bind", "/fixture/managed-policy", "/etc/claude-code"])
        self.assertLessEqual({part for part in argv if part.startswith("--")}, {"--tmpfs", "--ro-bind", "--symlink"})

    def test_the_host_view_keeps_every_entry(self) -> None:
        argv = etc_view("/fixture/managed-policy")
        shown = {argv[index + 2] for index, part in enumerate(argv) if part in ("--ro-bind", "--symlink")}
        with os.scandir("/etc") as entries:
            expected = {f"/etc/{entry.name}" for entry in entries if entry.name != "claude-code"
                        and (entry.is_symlink() or entry.is_dir(follow_symlinks=False)
                             or entry.is_file(follow_symlinks=False))}
        self.assertEqual(shown - {"/etc/claude-code"}, expected)
        self.assertEqual(argv[-3:], ["--ro-bind", "/fixture/managed-policy", "/etc/claude-code"])


class McpPolicyProbe(unittest.TestCase):
    probe_id = "mcp-policy"
    verdict: tuple[str, str, str] = ("INCONCLUSIVE", "unavailable", "check did not complete")

    @classmethod
    def setUpClass(cls) -> None:
        cls.gate = real_binary_gate(_CONTRACT, what="the mcp-policy check")
        if cls.gate.boundary:
            cls.verdict = ("BOUNDARY", "unavailable", cls.gate.boundary)

    @classmethod
    def tearDownClass(cls) -> None:
        status, observed, detail = cls.verdict
        print(f"client check {cls.probe_id}: {status} class={observed} {detail}".rstrip(), flush=True)

    def setUp(self) -> None:
        if self.gate.boundary:
            self.skipTest(self.gate.boundary)
        self.trusted = self.gate.trusted
        self.root = Path(tempfile.mkdtemp(prefix="charmer-mcp-policy-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.environ = {"HOME": str(self.root / "ambient"), "PATH": "/usr/bin:/bin"}
        self.count = 0

    def _fixture(self, servers: tuple[str, ...], user: dict[str, Any]) -> tuple[probe.ProbeFixture, Path]:
        """A fixture with the user servers ``servers``, user settings ``user``
        and the fixture cm agent carrying its own server; the start log."""

        self.count += 1
        fixture = probe.build_fixture(self.root / f"run-{self.count}", environ=self.environ)
        probe.seed_fixture_project_trust(fixture, fixture.project_dir)
        state.atomic_write(state.ensure_private_dir(fixture.root / "bin") / "fake_mcp.py", _SERVER.encode("utf-8"))
        log = fixture.root / "mcp.log"

        def server(name: str) -> dict[str, Any]:
            return self._server(fixture, name)

        config = fixture.claude_config_dir / ".claude.json"
        document = strict_json.loads(state.read_private(config))
        document["mcpServers"] = {name: server(name) for name in servers}
        state.atomic_write(config, strict_json.canonical_file_bytes(document))
        state.atomic_write(fixture.claude_config_dir / "settings.json", strict_json.canonical_file_bytes(user))
        agents = state.ensure_private_dir(fixture.project_dir / ".claude" / "agents")
        own = json.dumps({_DENIED_AGENT: server(_DENIED_AGENT)})
        state.atomic_write(agents / f"{_AGENT_TYPE}.md", (
            f"---\nname: {_AGENT_TYPE}\ndescription: client check mcp-policy agent\nmodel: {_AGENT}\n"
            f"mcpServers:\n  - {own}\n---\n{_BODY} reply ok.\n").encode("utf-8"))
        return fixture, log

    @staticmethod
    def _server(fixture: probe.ProbeFixture, name: str) -> dict[str, Any]:
        """The stdio config of the fake server ``name`` (it logs to the fixture's ``mcp.log``)."""

        return {"type": "stdio", "command": "/usr/bin/python3",
                "args": [str(fixture.root / "bin" / "fake_mcp.py"), name, str(fixture.root / "mcp.log")]}

    @staticmethod
    def _log(log: Path) -> list[tuple[str, str]]:
        rows = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        return [tuple(row.split(" ", 1)) for row in rows if " " in row]  # type: ignore[misc]

    def _counts(self, log: Path, kind: str, since: int = 0, until: int | None = None) -> dict[str, int]:
        counts: dict[str, int] = {}
        for event, name in self._log(log)[since:until]:
            if event == kind:
                counts[name] = counts.get(name, 0) + 1
        return counts

    def _plugin(self, fixture: probe.ProbeFixture, *names: str) -> Path:
        """The ``--plugin-dir`` fixture plugin with the fake servers ``names``."""

        plugin = state.ensure_private_dir(fixture.root / "mods" / _PLUGIN)
        state.atomic_write(state.ensure_private_dir(plugin / ".claude-plugin") / "plugin.json", json.dumps(
            {"name": _PLUGIN, "version": "0.1.0", "description": "client check fixture"}).encode("utf-8"))
        state.atomic_write(plugin / ".mcp.json", json.dumps(
            {"mcpServers": {name: self._server(fixture, name) for name in names}}).encode("utf-8"))
        return plugin

    def _settings(self, fixture: probe.ProbeFixture, permissions: dict[str, Any]) -> Path:
        return probe.write_fixture_settings(fixture, "scope/settings.json", {
            "availableModels": [_LEAD, _AGENT], "model": _LEAD, "permissions": permissions})

    def _headless(self, fixture: probe.ProbeFixture, settings: Path, caller: _Caller, *extra: str) -> None:
        with probe.FakeAnthropicProvider(responder=caller) as provider:
            result = probe.run_native(("-p", "client check mcp-policy", "--settings", str(settings),
                                       "--model", _LEAD, *extra), trusted=self.trusted, fixture=fixture,
                                      provider=provider, timeout=150, allow_real=True, environ=self.environ)
        if result.timed_out or result.returncode != 0:
            raise _Inconclusive("a headless run did not complete")

    def _reload(self, fixture: probe.ProbeFixture, settings: Path, log: Path, expect_start: bool) -> int:
        """A terminal session with the fixture plugin (its server ``fxready``
        only): once ``fxready`` has started (the plugin's MCP config was
        read), the plugin gains ``fxreload``; a few seconds later (nothing
        may start it yet) ``/reload-plugins`` runs. The log position just
        before the reload command."""

        since = len(self._log(log))
        plugin = self._plugin(fixture, _READY)
        marks: dict[str, int] = {}

        def add_server() -> None:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not self._counts(log, "start", since).get(_READY):
                time.sleep(0.25)
            time.sleep(2)
            self._plugin(fixture, _READY, _RELOADED)
            time.sleep(3)
            marks["reload"] = len(self._log(log))

        def settle() -> None:
            deadline = time.monotonic() + (12 if expect_start else 5)
            while time.monotonic() < deadline:
                if self._counts(log, "start", marks["reload"]).get(_RELOADED) and expect_start:
                    return
                time.sleep(0.25)

        steps = [probe.PTYInteraction(b"Choose", b"2\r"), probe.PTYInteraction(b"Press", b"\r"),
                 probe.PTYInteraction("❯".encode(), b"/reload-plugins\r", before_send=add_server),
                 probe.PTYInteraction(b"Reloaded", b"/exit\r", before_send=settle)]
        with probe.FakeAnthropicProvider(responder=_Caller()) as provider:
            result = probe.run_native_pty(("--settings", str(settings), "--model", _LEAD, "--plugin-dir", str(plugin)),
                                          steps, trusted=self.trusted, fixture=fixture, provider=provider,
                                          timeout=150, allow_real=True, environ=self.environ)
        if result.timed_out or result.returncode != 0 or "reload" not in marks:
            raise _Inconclusive("the terminal reload run did not complete")
        return marks["reload"]

    def _match(self, permissions: dict[str, Any], allow: list[str], deny: list[str]) -> dict[str, Any]:
        user = {"permissions": {"defaultMode": scope.PERMISSION_DEFAULT_MODE, "allow": allow, "deny": deny}}
        fixture, log = self._fixture(_MATCH_SERVERS, user)
        caller = _Caller(tuple(tool_name(server) for server in _MATCH_SERVERS))
        self._headless(fixture, self._settings(fixture, permissions), caller)
        return {"results": {server: caller.results.get(tool_name(server), "no-result") for server in _MATCH_SERVERS},
                "calls": self._counts(log, "call")}

    def _counter(self, permissions: dict[str, Any], policy: dict[str, Any] | None) -> dict[str, Any]:
        fixture, log = self._fixture((_DENIED,), {"permissions": {"defaultMode": scope.PERMISSION_DEFAULT_MODE}})
        settings = self._settings(fixture, permissions)
        session = "3c9c0de0-3c9c-4c0d-8c9c-3c9c0de0a001"
        starts: dict[str, dict[str, int]] = {}
        caller = _Caller(spawn=True)

        def phases() -> None:
            self._headless(fixture, settings, caller, "--session-id", session, "--dangerously-skip-permissions")
            starts["launch"] = self._counts(log, "start")
            mark = len(self._log(log))
            self._headless(fixture, settings, _Caller(), "--resume", session)
            starts["resume"] = self._counts(log, "start", mark)
            mark = len(self._log(log))
            reload_mark = self._reload(fixture, settings, log, expect_start=policy is None)
            starts["terminal"] = self._counts(log, "start", mark, reload_mark)
            starts["reload"] = self._counts(log, "start", reload_mark)

        if policy is None:
            phases()
        else:
            with managed_settings(fixture, policy):
                phases()
        if not caller.agent_ran:
            raise _Inconclusive("the fixture cm agent was not spawned")
        return {"starts": starts}

    def test_mcp_rules_name_one_server_and_policy_precedes_start(self) -> None:
        import test_compiler_v2  # the production compile (module import: no tests re-exported)

        _lineup, compiled = test_compiler_v2._seed_launch("balanced", default_mode=scope.PERMISSION_DEFAULT_MODE)
        permissions = json.loads(json.dumps(compiled.scope_plan.settings["permissions"]))
        observed: dict[str, Any] = {}
        try:
            observed["match-control"] = self._match(permissions, [f"mcp__{s}__*" for s in _MATCH_SERVERS], [])
            observed["match-wild"] = self._match(permissions, ["mcp__fx__*"], [])
            observed["match"] = self._match(permissions, ["mcp__fx"], ["mcp__fx__x"])
            observed["counter-control"] = self._counter(permissions, None)
            observed["counter"] = self._counter(permissions, {"deniedMcpServers": [
                {"serverName": _DENIED}, {"serverName": _DENIED_AGENT}, {"serverName": _RELOADED_POLICY_NAME}]})
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
        detail = (f"match-control={observed['match-control']['results']} "
                  f"match-wild(allow mcp__fx__*)={observed['match-wild']['results']} "
                  f"match(allow mcp__fx, deny mcp__fx__x)={observed['match']['results']} "
                  f"calls={observed['match']['calls']} "
                  f"starts-without-policy={observed['counter-control']['starts']} "
                  f"starts-under-managed-deniedMcpServers={observed['counter']['starts']} "
                  f"(reload: {_RELOADED_POLICY_NAME} added once the session started the plugin's {_READY}, "
                  "never started before the command) "
                  f"client={self.gate.version} sha256={self.trusted.sha256[:12]}")
        type(self).verdict = ("PASS" if observed_class == PASS_CLASS else "FAIL", observed_class, detail)
        self.assertEqual(observed_class, PASS_CLASS, detail)


if __name__ == "__main__":
    unittest.main()
