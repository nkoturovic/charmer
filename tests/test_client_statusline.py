"""Client check S17 against the pinned Claude Code: the subagent status line.

Managed scopes compile ``subagentStatusLine`` into the flag settings
(``--settings``) unless a user, project or local settings layer defines the
key (``scope.compile_lineup_scope``, ``Runtime.subagent_status_line``). S17
pins the client behaviour that rule rests on, in a terminal (the client
runs the command only for the agent panel of an interactive session):

- (a) a ``--settings`` document's ``subagentStatusLine`` is run when the
  user layer has none, and the rows it prints replace the native rows;
  end to end, the compiled value (the stable shim, a release-shaped
  launcher over this checkout and ``charmer statusline subagents``) draws
  the shipped row, ``requested: <selector>`` included;
- (b) with none in the flag settings, the user layer's own key is run;
- control: when both define it, the flag settings' command runs and the
  user's never does: the reason charmer compiles none over a user-owned key;
- (c) the document on the command's stdin has ``columns`` and ``tasks[]``
  with the fields the shipped renderer reads; the observed field set is
  printed (names only).

Real-binary runs only, through the probe harness: disposable fixture HOME,
the loopback fake provider, the live-daemon-domain tripwire and the
loopback-only network namespace; off the operator host every test skips with
a BOUNDARY message, and a touched live daemon domain is INCONCLUSIVE, never a
pass. The recording command keeps metadata only (key names, value types and
the synthetic agent type, type, model and status); no prompt, response or
transcript text is persisted or printed. One ``client check S17: <verdict>
<detail>`` line is printed.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path
from typing import Any

from charmer import probe, scope, state, strict_json
from charmer.probe import ProbeError

from _layout import RESOURCES_ROOT
from _tier import real_binary_gate
from test_statusline import release_shaped_launcher

_CONTRACT_PATH = RESOURCES_ROOT / "catalog" / "native-contract.json"
_LEAD = "spike-lead"
_AGENT_MODEL = "charmer-s17-model-high"  # a gateway selector: the shipped row says "requested:"
_AGENT_TYPE = "cm-spike-s17"
_AGENT_NAME = "s17-named"
_BODY_MARKER = "CLIENTCHECK-S17-BODY"
_AGENT_FILE = f"""---
name: {_AGENT_TYPE}
description: Synthetic probe agent for the subagent status-line check.
model: {_AGENT_MODEL}
effort: high
---
You are the {_AGENT_TYPE} fixture agent. {_BODY_MARKER}.
"""
_DIRECTIVE = "CLIENTCHECK-SPAWN-1"
_TOOL_ID = "toolu_check_s17"
_PROMPT_GLYPH = "❯".encode("utf-8")
_LIVE_DOMAIN_TOUCHED = "live daemon domain touched"
# The task fields the shipped renderer reads (charmer.statusline).
RENDERER_TASK_FIELDS = frozenset({"id", "agentType", "type", "name", "model", "effort", "status", "label",
                                  "description", "tokenCount", "contextWindowSize"})
# The shipped row stays visible past the client's first tick (300 ms) and
# its periodic one (5 s).
SHIPPED_HOLD_SECONDS = 6.5
_ESCAPE = re.compile(r"\x1b\[([0-9;?<>=]*)([A-Za-z@`~])|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][0-9A-Za-z]|\x1b.")


def seen_lines(raw: str) -> set[str]:
    """Every terminal line the capture drew, after cursor moves: the client
    redraws only changed cells (a ``CSI n G`` skips an unchanged one), so a
    flattened capture loses characters a real terminal shows."""

    grid: dict[int, dict[int, str]] = {}
    row = col = 0
    saved = (0, 0)
    seen: set[str] = set()

    def line(number: int) -> str:
        cells = grid.get(number, {})
        return "".join(cells.get(index, " ") for index in range(max(cells, default=-1) + 1)).rstrip()

    position = 0
    while position < len(raw):
        match = _ESCAPE.match(raw, position)
        if match is None:
            char = raw[position]
            position += 1
            if char == "\r":
                col = 0
            elif char == "\n":
                seen.add(line(row))
                row += 1
            elif char == "\b":
                col = max(0, col - 1)
            elif char >= " ":
                grid.setdefault(row, {})[col] = char
                col += 1
            continue
        position = match.end()
        final, params = match.group(2), match.group(1) or ""
        if final is None:
            if match.group(0) == "\x1b7":
                saved = (row, col)
            elif match.group(0) == "\x1b8":
                row, col = saved
            continue
        numbers = [int(part) if part.isdigit() else 0 for part in params.split(";")]
        count = max(numbers[0], 1)
        seen.add(line(row))
        if final == "G":
            col = count - 1
        elif final == "C":
            col += count
        elif final == "D":
            col = max(0, col - count)
        elif final == "A":
            row -= count
        elif final == "B":
            row += count
        elif final in "Hf":
            row, col = max(numbers[0], 1) - 1, (max(numbers[1], 1) - 1 if len(numbers) > 1 else 0)
        elif final == "K":
            cells = grid.setdefault(row, {})
            for index in [index for index in cells if numbers[0] == 2 or index >= col]:
                del cells[index]
        elif final == "J" and numbers[0] in (2, 3):
            grid.clear()
    seen.add(line(row))
    return seen

_RECORDER = """\
import json, os, sys
label, out = sys.argv[1], sys.argv[2]
try:
    document = json.loads(sys.stdin.buffer.read())
except ValueError:
    document = None
tasks = document.get("tasks") if isinstance(document, dict) else None
def shape(task):
    if not isinstance(task, dict):
        return {"kind": type(task).__name__}
    return {"keys": sorted(task), "types": {key: type(value).__name__ for key, value in task.items()},
            "agentType": task.get("agentType"), "type": task.get("type"), "model": task.get("model"),
            "status": task.get("status")}
meta = {"label": label, "top": sorted(document) if isinstance(document, dict) else None,
        "columns": type(document.get("columns")).__name__ if isinstance(document, dict) else None,
        "tasks": [shape(task) for task in tasks] if isinstance(tasks, list) else None}
name = os.path.join(out, "%04d-%s.json" % (len(os.listdir(out)), label))
with open(name, "w", encoding="utf-8") as handle:
    json.dump(meta, handle)
for task in tasks or []:
    if isinstance(task, dict) and isinstance(task.get("id"), str):
        print(json.dumps({"id": task["id"], "content": "S17-%s-ROW" % label.upper()}))
"""


class _Responder:
    """The lead spawns one background ``cm-spike-s17`` agent. The agent's own
    request is held until a status-line command recorded a document (or for
    ``hold`` seconds), so the row is visible when the client ticks."""

    def __init__(self, records: Path, hold: float | None) -> None:
        self.records = records
        self.hold = hold
        self._lock = threading.Lock()
        self.spawned = False
        self.named = False  # the Agent tool offers a name and the spawn gave one
        self.agent_requests = 0
        self.recorded_before_answer: bool | None = None

    def respond(self, document: dict[str, Any], path: str, context: Any) -> tuple[int, Any]:
        route = urllib.parse.urlsplit(path).path.rstrip("/")
        if route.endswith("/count_tokens"):
            return 200, {"input_tokens": 1}
        if not route.endswith("/messages"):
            return 404, {"type": "error", "error": {"type": "not_found_error", "message": "probe: unknown path"}}
        system = strict_json.canonical_bytes(document.get("system", "")).decode("utf-8")
        if _BODY_MARKER in system:
            if self.hold is not None:
                time.sleep(self.hold)
            else:
                deadline = time.monotonic() + 25
                while not any(self.records.iterdir()) and time.monotonic() < deadline:
                    time.sleep(0.1)
            with self._lock:
                self.agent_requests += 1
                self.recorded_before_answer = any(self.records.iterdir())
            return self._reply(document, "CLIENTCHECK-SUBAGENT-OK", context.ordinal)
        messages = document.get("messages") if isinstance(document.get("messages"), list) else []
        texts, results = [], []
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content")
            blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content or []
            for block in blocks if isinstance(blocks, list) else []:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(str(block.get("text")))
                elif isinstance(block, dict) and block.get("type") == "tool_result":
                    results.append(block.get("tool_use_id"))
        tools = {tool.get("name"): tool for tool in document.get("tools") or [] if isinstance(tool, dict)}
        with self._lock:
            if "Agent" in tools and any(_DIRECTIVE in text for text in texts) and not self.spawned:
                self.spawned = True
                spawn = {"description": "S17 delegation", "subagent_type": _AGENT_TYPE,
                         "prompt": "Reply with exactly one short line."}
                schema = tools["Agent"].get("input_schema") or {}
                if "name" in (schema.get("properties") or {}):
                    spawn["name"] = _AGENT_NAME
                    self.named = True
                block = {"type": "tool_use", "id": _TOOL_ID, "name": "Agent", "input": spawn}
                payload = probe._message_payload([block], model=document.get("model"), stop_reason="tool_use",
                                                 message_id=f"msg_client_check_{context.ordinal:04d}")
                return 200, probe._sse_message(payload) if document.get("stream") else payload
        if texts and "<task-notification>" in texts[-1]:
            return self._reply(document, "CLIENTCHECK-NOTIFIED-spawn1", context.ordinal)
        if _TOOL_ID in results:
            return self._reply(document, "CLIENTCHECK-DONE-spawn1", context.ordinal)
        return self._reply(document, "PROBE-OK", context.ordinal)

    @staticmethod
    def _reply(document: dict[str, Any], text: str, ordinal: int) -> tuple[int, Any]:
        payload = probe._message_payload([{"type": "text", "text": text}], model=document.get("model"),
                                         stop_reason="end_turn", message_id=f"msg_client_check_{ordinal:04d}")
        return 200, probe._sse_message(payload) if document.get("stream") else payload


@dataclasses.dataclass(frozen=True)
class _Run:
    documents: tuple[dict[str, Any], ...]  # recorded metadata, in order
    lines: frozenset[str]  # every terminal line the session drew
    responder: _Responder

    def drew(self, text: str) -> bool:
        return any(text in line for line in self.lines)

    def labels(self) -> set[str]:
        return {document["label"] for document in self.documents}


class SubagentStatusLineClientTests(unittest.TestCase):
    """S17 on the pinned client (see the module docstring)."""

    trusted: probe.TrustedExecutable
    boundary_skip: str | None = None
    evidence_label = ""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        gate = real_binary_gate(_CONTRACT_PATH, what="client check S17")
        cls.boundary_skip = gate.boundary
        if gate.trusted is not None:
            cls.trusted = gate.trusted
            cls.evidence_label = f"client={gate.version} sha256={gate.trusted.sha256[:12]}"

    def setUp(self) -> None:
        super().setUp()
        if self.boundary_skip is not None:
            self.skipTest(self.boundary_skip)
        self.root = Path(tempfile.mkdtemp(prefix="charmer-s17-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.environ = {"HOME": str(self.root / "live" / "home"), "PATH": "/usr/bin:/bin"}
        self.recorder = self.root / "recorder.py"
        self.recorder.write_text(f"#!{sys.executable}\n" + _RECORDER, encoding="utf-8")
        self.recorder.chmod(0o700)

    def run_case(self, name: str, session: str, *, flag: str | dict[str, str] | None,
                 user: str | None, hold: float | None = None) -> _Run:
        """One terminal session that spawns one background agent. ``flag`` is
        a recorder label or a literal ``subagentStatusLine`` for the flag
        settings; ``user`` a recorder label for the user layer."""

        fixture = probe.build_fixture(self.root / name, environ=self.environ)
        # The user layer IS $HOME/.claude/settings.json (a managed launch
        # unsets CLAUDE_CONFIG_DIR).
        fixture = dataclasses.replace(fixture, claude_config_dir=state.ensure_private_dir(fixture.home / ".claude"))
        records = state.ensure_private_dir(fixture.root / "records")
        scope_dir = state.ensure_private_dir(fixture.root / "scope")
        agents = state.ensure_private_dir(state.ensure_private_dir(scope_dir / ".claude") / "agents")
        state.atomic_write(agents / f"{_AGENT_TYPE}.md", _AGENT_FILE.encode("utf-8"))
        probe.seed_fixture_project_trust(fixture, fixture.project_dir)
        probe.seed_fixture_project_trust(fixture, scope_dir)

        def recording(label: str) -> dict[str, str]:
            return {"type": "command", "command": f"{self.recorder} {label} {records}"}

        if user is not None:
            state.atomic_write(fixture.claude_config_dir / "settings.json",
                               strict_json.canonical_file_bytes({"subagentStatusLine": recording(user)}))
        settings: dict[str, Any] = {"availableModels": [_LEAD, _AGENT_MODEL], "model": _LEAD,
                                    "permissions": {"defaultMode": scope.PERMISSION_DEFAULT_MODE}}
        if flag is not None:
            settings["subagentStatusLine"] = recording(flag) if isinstance(flag, str) else flag
        flag_path = fixture.root / "flag-settings.json"
        state.atomic_write(flag_path, strict_json.canonical_file_bytes(settings))
        responder = _Responder(records, hold)
        steps = [probe.PTYInteraction(b"Choose", b"2\r"), probe.PTYInteraction(b"Press", b"\r"),
                 probe.PTYInteraction(_PROMPT_GLYPH, f"{_DIRECTIVE} go\r".encode()),
                 probe.PTYInteraction(b"CLIENTCHECK-NOTIFIED-spawn1", b"/exit\r")]
        try:
            with probe.FakeAnthropicProvider(responder=responder) as provider:
                result = probe.run_native_pty(
                    ("--session-id", session, "--settings", str(flag_path), "--model", _LEAD,
                     "--add-dir", str(scope_dir)),
                    steps, trusted=self.trusted, fixture=fixture, provider=provider, timeout=150,
                    allow_real=True, environ=self.environ)
        except ProbeError as exc:
            if _LIVE_DOMAIN_TOUCHED in str(exc):
                print(f"client check S17: INCONCLUSIVE {_LIVE_DOMAIN_TOUCHED} ({self.evidence_label})")
                self.skipTest(f"BOUNDARY: {_LIVE_DOMAIN_TOUCHED}; inconclusive, never a pass")
            raise
        self.assertFalse(result.timed_out, name)
        self.assertEqual(result.returncode, 0, name)
        assert result.daemon is not None
        self.assertTrue(result.daemon.unchanged)
        self.assertTrue(responder.spawned and responder.agent_requests >= 1, name)
        documents = tuple(json.loads(path.read_text(encoding="utf-8")) for path in sorted(records.iterdir()))
        return _Run(documents, frozenset(seen_lines(result.stdout)), responder)

    def test_s17_subagent_status_line_precedence_and_document(self) -> None:
        # (a) the flag settings' key, with no user key.
        flag = self.run_case("flag", "17171717-0001-4001-8001-000000000001", flag="flag", user=None)
        self.assertEqual(flag.labels(), {"flag"}, "the --settings subagentStatusLine never ran")
        self.assertTrue(flag.responder.recorded_before_answer)
        self.assertTrue(flag.drew("S17-FLAG-ROW"), sorted(flag.lines))
        # (b) the user's own key, with none in the flag settings.
        user = self.run_case("user", "17171717-0002-4002-8002-000000000002", flag=None, user="user")
        self.assertEqual(user.labels(), {"user"}, "the user layer's subagentStatusLine never ran")
        self.assertTrue(user.drew("S17-USER-ROW"), sorted(user.lines))
        # Control: both define it; only the flag settings' command runs.
        both = self.run_case("both", "17171717-0003-4003-8003-000000000003", flag="flag", user="user")
        self.assertEqual(both.labels(), {"flag"}, "the user's command ran although --settings defines the key")
        self.assertFalse(both.drew("S17-USER-ROW"))
        self.assertTrue(both.drew("S17-FLAG-ROW"))
        # (a), end to end: the compiled value, the stable shim and the shipped renderer.
        launcher = release_shaped_launcher(self.root / "bundle", sys.executable)
        shim = scope.ensure_status_line_shim(state.ensure_private_dir(self.root / "shipped-state"), str(launcher))
        shipped = self.run_case("shipped", "17171717-0004-4004-8004-000000000004",
                                flag=scope.status_line_setting(shim), user=None, hold=SHIPPED_HOLD_SECONDS)
        self.assertTrue(shipped.drew(f"{_AGENT_TYPE} requested: {_AGENT_MODEL}"), sorted(shipped.lines))
        # (c) the stdin document's field set.
        document = flag.documents[0]
        self.assertTrue({"columns", "tasks"} <= set(document["top"]), document["top"])
        self.assertEqual(document["columns"], "int")
        task = next(task for task in document["tasks"] if task.get("agentType") == _AGENT_TYPE)
        self.assertEqual(task["types"]["id"], "str")
        self.assertEqual((task["model"], task["type"]), (_AGENT_MODEL, "local_agent"))
        self.assertTrue({"id", "agentType", "type", "status", "description", "label", "model", "tokenCount",
                         "contextWindowSize", "effort"} <= set(task["keys"]), sorted(task["keys"]))
        self.assertEqual("name" in task["keys"], flag.responder.named)  # a named agent's name is sent
        print("client check S17: PASS "
              f"flag-settings key runs ({len(flag.documents)} document(s)) and its rows replace the native ones; "
              f"user key runs without one ({len(user.documents)}); both -> flag only; "
              f"shipped shim row 'requested: {_AGENT_MODEL}' drawn; "
              f"document top={','.join(document['top'])} columns={document['columns']} "
              f"task fields={','.join(task['keys'])} (renderer reads "
              f"{','.join(sorted(RENDERER_TASK_FIELDS & set(task['keys'])))}) status={task['status']} "
              f"{self.evidence_label}")


if __name__ == "__main__":
    unittest.main()
