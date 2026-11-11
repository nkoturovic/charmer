"""Client check fork-launch: a launcher-owned fork on the pinned client.

A charmer fork is a new managed session started with
``--resume <parent runtime id> --fork-session --session-id <new id>`` and
the new session's own record, scope, hooks and lead prompt. These probes
measure what the pinned Claude Code does with that argv, end to end:

- the launch runs in a launcher subprocess that really ``execve``\\ s the
  client (the session's lifecycle lock is held across the exec and dropped
  by it, as in production, so the client's SessionStart hook can take it);
  this module waits outside. The launch is prepared by a minimal prototype
  (a fresh launch of the fixture ``balanced`` profile whose argv gains the
  fork flags and whose record gains ``forked_from``);
- every run is hermetic: fixture-owned HOME, XDG roots and charmer state,
  the pinned copy hard-linked into the fixture's owned location and
  verified by the launch, the fixture provider bridged into a
  loopback-only network namespace (``bwrap --unshare-net``, its own PID
  namespace) whose ``/tmp`` is a fixture directory, so a daemon directory
  the client might create is private and observable; the compiled hooks run
  the source launcher through a fixture wrapper that records metadata only
  (event, source, ids) and stands in for a ready gateway in the token
  helper's readiness check;
- G1 (headless): the fork's conversation file, the parent's bytes, the
  fork's first lead request (history marker, the managed-session sentinel in
  the system prompt), SessionStart on the fork's hooks, no daemon or jobs
  directory, the fork's own-fork notice and its authoritative record; with
  a parent whose prompt snapshot is established (and the fork's own resume
  after it), with a compacted parent, with ``--system-prompt-snapshot off``
  on every launch of the fork, and five fork starts in a row (each start
  counted on the fork's hooks);
- G2 (PTY): an idle parent stays live while the fork runs; both take more
  turns; both conversation files stay independent (also with the fork's
  prompt snapshot switched off: which prompt each turn sends);
- G2b: the parent is streaming, or running a tool, when it is forked; the
  fork runs headless (then resumes) or in a PTY;
- G3 (record only): ``/fork`` typed in a charmer session.

Evidence is metadata only: marker names, ids of fixture sessions, counts,
digests, booleans, role sequences. No request text, transcript text or
screen text beyond the client's one-line refusal is printed.

Each row prints ``fork-launch row <name>: <json>`` and fails its test on any
problem; the class prints one ``client check fork-launch: ...`` line from
``tearDownClass`` (``PASS`` only when every row passed).
"""

from __future__ import annotations

import dataclasses
import fcntl
import hashlib
import json
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from charmer import endpoint, pin, probe, state, strict_json

from _catalog import FIXTURE_GATEWAY_TOKEN, FIXTURE_ROOT
from _layout import REPO_ROOT, RESOURCES_ROOT
from _tier import real_binary_gate

PLATFORMS = ("linux",)

_CONTRACT_PATH = RESOURCES_ROOT / "catalog" / "native-contract.json"
_RUN_TIMEOUT = 180.0
_HINT_ENV = {"CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1"}

# Turn markers typed by the probes and the replies the fixture provider gives.
T1 = "FORKPROBE-PARENT-TURN-ONE"
T2 = "FORKPROBE-FORK-TURN-TWO"
T3 = "FORKPROBE-PARENT-TURN-THREE"
T4 = "FORKPROBE-FORK-TURN-FOUR"
T5 = "FORKPROBE-FORK-TURN-FIVE"
HOLD_STREAM = "FORKPROBE-HOLD-STREAM"
HOLD_TOOL = "FORKPROBE-HOLD-TOOL"
SUMMARY = "FORKPROBE-COMPACT-SUMMARY"
_TURNS = (HOLD_STREAM, HOLD_TOOL, T1, T2, T3, T4, T5)
_SNAPSHOT_OFF = ("--system-prompt-snapshot", "off")
_TOOL_ID = "toolu_forkprobe_hold"
_TOOL_OUTPUT = "FORKTOOL-OUT42"  # printed by the tool, never part of its command
_LEAD_BODY = "You are the lead of a managed session."
_EXTRA_MARKERS = {
    "partial-stream": "FORKSTREAM-PARTIAL",
    "tool-output": "FORKTOOL-OUT42",
    "interrupted": "Request interrupted",
    "no-response-placeholder": "No response requested.",
}
_PREFACES = {
    "startup": "Session started. The lineup below is current.",
    "resume": "Session resumed. Your recorded lead is",
    "fork-shared": "This is a native fork that shares the parent session",
    "fork-own": "This session was forked from another charmer session and has its own record and scope",
}
_SENTINEL_RE = re.compile(r"Managed session: ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?<>=]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[()][A-Z0-9]|\x1b[=>78]")


def reply_text(tag: str) -> str:
    return f"FORKREPLY-{tag.rsplit('-', 1)[-1]}"


# --------------------------------------------------------------- the provider


@dataclasses.dataclass(frozen=True)
class _HeldSse(probe.SseResponse):
    """An SSE reply sent in two parts: ``hold_after`` events, then a wait."""

    hold_after: int = 0
    gate: Any = None
    started: Any = None


class _StreamingHandler(probe._ProviderHandler):
    """The fake provider's handler, able to stop in the middle of a stream."""

    def _send_sse(self, status: int, payload: probe.SseResponse, headers=()) -> None:
        if not isinstance(payload, _HeldSse):
            super()._send_sse(status, payload, headers)
            return
        chunks = [f"event: {event}\n".encode() + b"data: " + strict_json.canonical_bytes(data) + b"\n\n"
                  for event, data in payload.events]
        self.send_response(status)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(sum(len(chunk) for chunk in chunks)))
        self.end_headers()
        for chunk in chunks[: payload.hold_after]:
            self.wfile.write(chunk)
        self.wfile.flush()
        payload.started.set()
        payload.gate.wait(timeout=_RUN_TIMEOUT)
        for chunk in chunks[payload.hold_after:]:
            self.wfile.write(chunk)
        self.wfile.flush()


def _text_blocks(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    texts = []
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"])
    return texts


def _system_text(document: dict[str, Any]) -> str:
    system = document.get("system")
    return "\n".join(_text_blocks(system)) if system is not None else ""


def _message_shape(messages: list[Any]) -> dict[str, Any]:
    """Roles in order and tool_use blocks without a tool_result (ids only)."""

    roles, uses, results = [], [], set()
    assistant: list[str] = []
    probe_result: dict[str, Any] | None = None
    for message in messages:
        if not isinstance(message, dict):
            continue
        roles.append(message.get("role"))
        content = message.get("content")
        if message.get("role") == "assistant":
            blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
            assistant.append("+".join(
                f"{block.get('type')}:{len(block.get('text') or '')}" if block.get("type") == "text"
                else str(block.get("type")) for block in blocks if isinstance(block, dict)) or "empty")
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and message.get("role") == "assistant":
                uses.append(str(block.get("id")))
            elif block.get("type") == "tool_result":
                results.add(str(block.get("tool_use_id")))
                if block.get("tool_use_id") == _TOOL_ID:
                    text = json.dumps(block.get("content"), ensure_ascii=False).lower()
                    probe_result = {
                        "is_error": block.get("is_error") is True,
                        "has_output": _TOOL_OUTPUT.lower() in text,
                        "words": sorted(word for word in ("interrupt", "cancel", "abort", "reject", "error",
                                                          "not completed") if word in text),
                    }
    repeated = sum(1 for left, right in zip(roles, roles[1:]) if left == right)
    return {"roles": "".join("u" if role == "user" else "a" if role == "assistant" else "?" for role in roles),
            "other_roles": sorted({str(role) for role in roles if role not in ("user", "assistant")}),
            "assistant_shapes": assistant, "probe_tool_result": probe_result,
            "repeated_roles": repeated, "unpaired_tool_uses": len([use for use in uses if use not in results])}


class _ForkResponder:
    """Answers every request; keeps metadata rows (marker names, ids, shapes)."""

    def __init__(self, tool_command: str) -> None:
        self._lock = threading.Lock()
        self.rows: list[dict[str, Any]] = []
        self._issued: set[str] = set()
        self._tool_command = tool_command
        self.stream_gate = threading.Event()
        self.stream_started = threading.Event()

    def respond(self, document: dict[str, Any], path: str, context: probe.RequestContext) -> Any:
        route = path.split("?", 1)[0].rstrip("/")
        if route.endswith("/count_tokens"):
            return 200, {"input_tokens": 1}
        if not route.endswith("/messages"):
            return 404, {"type": "error", "error": {"type": "not_found_error", "message": "probe"}}
        record = context.record
        hints = dict(record.hint_header_values)
        messages = document.get("messages") if isinstance(document.get("messages"), list) else []
        system = _system_text(document)
        body = json.dumps(messages, ensure_ascii=False)  # in memory only
        last_user = ""
        for message in reversed(messages):
            if isinstance(message, dict) and message.get("role") == "user":
                texts = _text_blocks(message.get("content"))
                if texts:
                    last_user = "\n".join(texts)
                    break
        # The newest marker of the last user text names the turn (a fork of
        # a busy parent may carry the parent's unanswered turn before it).
        found = [(last_user.rfind(marker), marker) for marker in _TURNS if marker in last_user]
        tag = max(found)[1] if found else None
        tool_names = set(record.tool_names)
        main = bool(tool_names & {"Agent", "Bash"}) and hints.get("x-claude-code-request-class") not in (
            "subagent", "workflow")
        tool_done = any(
            isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id") == _TOOL_ID
            for message in messages[-1:] if isinstance(message, dict)
            for block in (message.get("content") if isinstance(message.get("content"), list) else []))
        compact = tag is None and not tool_done and "summar" in last_user.lower()
        row = {
            "ordinal": context.ordinal,
            "main": main,
            "compact": compact,
            "tag": tag,
            "tool_done": tool_done,
            "model": record.model,
            "request_class": hints.get("x-claude-code-request-class"),
            "session_hint": hints.get("x-claude-code-session-id"),
            "hint_names": list(record.hint_header_names),
            "system_sentinels": sorted(set(_SENTINEL_RE.findall(system))),
            "system_lead_body": _LEAD_BODY in system,
            "system_sha256": hashlib.sha256(system.encode()).hexdigest()[:16],
            "messages_markers": sorted(name for name in (*_TURNS, SUMMARY) if name in body),
            "messages_prefaces": sorted(name for name, text in _PREFACES.items() if text in body),
            "messages_extra": sorted(name for name, text in _EXTRA_MARKERS.items() if text in body),
            "messages_sentinels": sorted(set(_SENTINEL_RE.findall(body))),
            "received": time.monotonic(),
            **_message_shape(messages),
        }
        with self._lock:
            self.rows.append(row)
            first_hold = tag is not None and tag not in self._issued
            if tag is not None:
                self._issued.add(tag)
        model = document.get("model")
        stream = bool(document.get("stream"))
        if compact:
            return 200, self._text(model, f"{SUMMARY} fixture summary of the conversation", context, stream)
        if main and tag == HOLD_STREAM and first_hold and stream:
            return 200, self._held(model, context)
        if main and tag == HOLD_TOOL and first_hold and not tool_done:
            block = {"type": "tool_use", "id": _TOOL_ID, "name": "Bash",
                     "input": {"command": self._tool_command, "description": "fork probe wait"}}
            payload = probe._message_payload([block], model=model, stop_reason="tool_use",
                                             message_id=f"msg_forkprobe_{context.ordinal:04d}")
            return 200, probe._sse_message(payload) if stream else payload
        if main and tool_done:
            return 200, self._text(model, "FORKREPLY-TOOL", context, stream)
        if main and tag is not None:
            return 200, self._text(model, reply_text(tag), context, stream)
        return 200, self._text(model, "FORKPROBE-AUX", context, stream)

    @staticmethod
    def _text(model: Any, text: str, context: probe.RequestContext, stream: bool) -> Any:
        payload = probe._message_payload([{"type": "text", "text": text}], model=model, stop_reason="end_turn",
                                         message_id=f"msg_forkprobe_{context.ordinal:04d}")
        return probe._sse_message(payload) if stream else payload

    def _held(self, model: Any, context: probe.RequestContext) -> _HeldSse:
        message = probe._message_payload([], model=model, stop_reason="end_turn",
                                         message_id=f"msg_forkprobe_{context.ordinal:04d}")
        events = [
            ("message_start", {"type": "message_start", "message": {**message, "usage": {
                **message["usage"], "output_tokens": 0}}}),
            ("content_block_start", {"type": "content_block_start", "index": 0,
                                     "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "text_delta", "text": "FORKSTREAM-PARTIAL "}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "text_delta", "text": "FORKREPLY-STREAM"}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                               "usage": {"output_tokens": 2}}),
            ("message_stop", {"type": "message_stop"}),
        ]
        return _HeldSse(tuple(events), hold_after=3, gate=self.stream_gate, started=self.stream_started)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self.rows]


# ------------------------------------------------------- the launch prototype

# Runs inside the namespace as ``python <this> <spec.json>``: prepares a
# fresh launch of the fixture ``balanced`` profile, a resume, or a fork (the
# fresh plan with the fork flags in front of its argv and ``forked_from`` in
# its record), writes the plan's ids, then performs it with the real
# ``os.execve``.
_LAUNCHER = r'''
import dataclasses, json, os, sys
spec = json.load(open(sys.argv[1], encoding="utf-8"))
from charmer import cli, sessions, strict_json
import charmer.service

env = dict(os.environ)
runtime = cli.Runtime(
    asset_root=spec["assets"], environ=env, cwd=spec["cwd"],
    health_get=lambda _base, _path: 200,
    listener_owner=lambda _base: charmer.service.OwnerVerdict("ours", "fixture"),
    background_liveness=lambda: sessions.BackgroundLiveness(True, frozenset(), None),
    managed_root=spec["managed_root"],
    served_models_callback=lambda _gateway, _token: (None, None),
    doctor_binary_callback=lambda _contract: ([], ["fixture binary verified."]),
    doctor_callback=lambda _runtime: [],
)
shipped = strict_json.loads(open(spec["contract"], "rb").read())
runtime.catalog.docs["native-contract"] = {**runtime.catalog.docs["native-contract"],
                                           "verified": shipped["verified"]}
runtime._live_prefixes = lambda: frozenset()
store = runtime.session_store
plan = {"mode": spec["mode"]}
try:
    if spec["mode"] == "resume":
        record = store.load(spec["session"])
        target = cli.LaunchTarget("record", None, record.get("profile"), bool(record.get("follow")), "resume")
        prepared = runtime.prepare(target, action="resume", passthrough=spec["passthrough"],
                                   session_id=spec["session"])
    else:
        document = runtime.profiles.load("balanced")
        target = cli.LaunchTarget("profile", document, "balanced", True, "Profile balanced")
        prepared = runtime.prepare(target, action="fresh", passthrough=spec["passthrough"])
    if spec["mode"] == "fork":
        parent = store.load(spec["parent"])
        argv = list(prepared.result.argv)
        if argv[:1] != ["--session-id"]:
            raise SystemExit("fork prototype: the fresh plan does not start with --session-id")
        argv = ["--resume", sessions.runtime_session_id(parent), "--fork-session", *argv]
        record = {**prepared.record, "forked_from": sessions.managed_id(parent)}
        prepared = dataclasses.replace(prepared, result=dataclasses.replace(prepared.result, argv=argv),
                                       record=record)
    if spec.get("extra_client_args"):
        argv = [*prepared.result.argv, *spec["extra_client_args"]]
        prepared = dataclasses.replace(prepared, result=dataclasses.replace(prepared.result, argv=argv))
    plan.update(managed_id=sessions.managed_id(prepared.record),
                runtime_id=sessions.runtime_session_id(prepared.record),
                argv=list(prepared.result.argv), lead_prompt=str(prepared.result.lead_prompt_path),
                env_unset=list(prepared.result.env_unset))
    with open(spec["plan"], "w", encoding="utf-8") as handle:
        json.dump(plan, handle)
    runtime.perform(prepared)
    plan["returned"] = True
except BaseException as exc:
    plan["error"] = f"{type(exc).__name__}: {exc}"
with open(spec["plan"], "w", encoding="utf-8") as handle:
    json.dump(plan, handle)
sys.exit(3)
'''

# The hook command (CHARMER_HOOK_COMMAND): records metadata of every
# session-event call, answers the token helper's readiness check (the
# fixture provider stands in for a ready gateway of ours), and runs the
# source launcher for everything else.
_WRAPPER = r'''#!{python} -I
import json, os, subprocess, sys, time
LOG = {log!r}
args = sys.argv[1:]
if args[:2] == ["gateway", "ensure"]:
    with open(LOG, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({{"call": "gateway-ensure"}}) + "\n")
    sys.exit(0)
command = [{python!r}, {launcher!r}, *args]
if args[:1] != ["session-event"]:
    os.execv(command[0], command)
data = sys.stdin.buffer.read()
try:
    payload = json.loads(data or b"{{}}")
except ValueError:
    payload = {{}}
def option(name):
    return args[args.index(name) + 1] if name in args[:-1] else None
row = {{"call": "session-event", "event": args[1] if len(args) > 1 else None,
       "managed_id": option("--managed-id"), "epoch": option("--launch-epoch"),
       "hook_event_name": payload.get("hook_event_name"), "source": payload.get("source"),
       "session_id": payload.get("session_id")}}
with open(LOG, "a", encoding="utf-8") as handle:
    handle.write(json.dumps({{**row, "call": "session-event-begin"}}) + "\n")
started = time.monotonic()
done = subprocess.run(command, input=data)
row.update(rc=done.returncode, ms=int((time.monotonic() - started) * 1000))
with open(LOG, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(row) + "\n")
sys.exit(done.returncode)
'''


# ------------------------------------------------------------------ terminals


class _Terminal:
    """One PTY session; a reader thread drains the master into a buffer."""

    def __init__(self, argv: list[str], *, env: dict[str, str], cwd: Path, rows: int = 40, columns: int = 150):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
        self.master = master
        self.output = bytearray()
        self._lock = threading.Lock()
        self.process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=slave, stdout=slave, stderr=slave,
                                        close_fds=True, process_group=0)
        os.close(slave)
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

    def _drain(self) -> None:
        while True:
            try:
                ready, _, _ = select.select([self.master], [], [], 0.2)
            except (OSError, ValueError):
                return
            if not ready:
                if self.process.poll() is not None:
                    try:
                        chunk = os.read(self.master, 65536)
                    except OSError:
                        return
                    if not chunk:
                        return
                else:
                    continue
            else:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError:
                    return
                if not chunk:
                    return
            with self._lock:
                self.output.extend(chunk)

    def mark(self) -> int:
        with self._lock:
            return len(self.output)

    def text(self, since: int = 0) -> str:
        with self._lock:
            raw = bytes(self.output[since:])
        return " ".join(_ANSI_RE.sub(" ", raw.decode("utf-8", errors="replace")).split())

    def wait_for(self, marker: str, *, since: int = 0, timeout: float = 60.0) -> bool:
        deadline = time.monotonic() + timeout
        needle = marker.encode()
        squeezed = " ".join(marker.split())
        while time.monotonic() < deadline:
            with self._lock:
                found = needle in self.output[since:]
            if found or squeezed in self.text(since):
                return True
            if self.process.poll() is not None:
                time.sleep(0.3)
                with self._lock:
                    return needle in self.output[since:]
            time.sleep(0.1)
        return False

    def send(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            written = os.write(self.master, view)
            view = view[written:]

    def type_line(self, text: str) -> None:
        self.send(text.encode())
        time.sleep(0.4)
        self.send(b"\r")

    def wait_exit(self, timeout: float = 60.0) -> int | None:
        try:
            return self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def close(self) -> None:
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait()
        self._reader.join(timeout=5)
        try:
            os.close(self.master)
        except OSError:
            pass


# -------------------------------------------------------------------- fixture


class _ForkFixture:
    """A fixture HOME with charmer state, the pinned copy and the provider."""

    def __init__(self, case: unittest.TestCase, trusted: probe.TrustedExecutable, version: str):
        base = Path(tempfile.mkdtemp(prefix="cmfork-"))
        os.chmod(base, 0o700)
        case.addCleanup(shutil.rmtree, base, True)
        # The namespace's /tmp is a fixture directory. A test tree under the
        # host's /tmp (the runner's own, holding the pinned copy and this
        # fixture) is bound back into it at the same path.
        real = Path(os.path.realpath(base))
        self.tmp_rebind = (Path("/tmp") / real.relative_to("/tmp").parts[0]
                           if real.is_relative_to("/tmp") and real != Path("/tmp") else None)
        self.case = case
        self.environ = {"HOME": str(base / "live" / "home"), "PATH": "/usr/bin:/bin"}
        self.fx = probe.build_fixture(base / "fx", environ=self.environ)
        root = self.fx.root
        self.nstmp = state.ensure_private_dir(root / "nstmp")
        self.run_dir = state.ensure_private_dir(root / "run")
        self.bin = state.ensure_private_dir(root / "bin")
        self.specs = state.ensure_private_dir(root / "specs")
        self.events = root / "events.log"
        self.release = root / "tool-release"
        self.tool_started = root / "tool-started"
        self.home = self.fx.home
        self.claude_dir = self.home / ".claude"
        command = (f"touch {self.tool_started}; n=0; while [ ! -e {self.release} ] && [ $n -lt 900 ]; "
                   "do sleep 0.2; n=$((n+1)); done; echo FORKTOOL-OUT$((6*7))")
        self.responder = _ForkResponder(command)
        self.provider = probe.FakeAnthropicProvider(responder=self.responder).start()
        case.addCleanup(self.provider.stop)
        socket_path = self.provider.unix_socket(self.run_dir)
        for server, _thread in self.provider._unix_servers.values():
            server.RequestHandlerClass = _StreamingHandler
        self.bridge = probe.PortBridge(self.provider.port, socket_path, "egress")
        self.case.addCleanup(self.stream_release)
        # charmer's gateway: the token file and the endpoint (the bridge port).
        token_dir = state.ensure_private_dir(self.home / ".config" / "charmer")
        state.atomic_write(token_dir / "api-key", (FIXTURE_GATEWAY_TOKEN + "\n").encode())
        endpoint.write_config(self.home, endpoint.EndpointConfig(port=self.provider.port))
        # The pinned copy in the fixture's owned location (a hard link of the
        # copy the runner verified and placed).
        env = self.fx.environ()
        owned = pin.owned_path(env, version, pin.host_platform())
        state.ensure_private_dir(owned.parent)
        try:
            os.link(trusted.resolved_path, owned)
        except OSError:
            shutil.copy2(trusted.resolved_path, owned)
        # Claude Code's own config: onboarding done, the project trusted.
        config = {"hasCompletedOnboarding": True, "theme": "dark",
                  "projects": {str(self.fx.project_dir): {"hasTrustDialogAccepted": True,
                                                          "hasCompletedProjectOnboarding": True}}}
        state.atomic_write(self.home / ".claude.json", strict_json.canonical_file_bytes(config))
        python = str(Path(os.path.realpath(sys.executable)))
        self.python = python
        self.wrapper = self.bin / "charmer-hook"
        self.wrapper.write_text(_WRAPPER.format(python=python, log=str(self.events),
                                                launcher=str(REPO_ROOT / "bin" / "charmer")), encoding="utf-8")
        self.wrapper.chmod(0o700)
        self.launcher = self.bin / "fork-launch.py"
        self.launcher.write_text(_LAUNCHER, encoding="utf-8")
        self.state_root = self.fx.xdg_state_home / "charmer"
        # A managed launch unsets CLAUDE_CONFIG_DIR (the client keeps its
        # conversations under ~/.claude); the launcher's shell does not set it.
        env.pop("CLAUDE_CONFIG_DIR", None)
        self.env = {
            **env,
            **_HINT_ENV,
            "CHARMER_HOOK_COMMAND": str(self.wrapper),
            "PYTHONPATH": os.environ.get("PYTHONPATH", f"{REPO_ROOT / 'src'}"),
        }
        guard_log = os.environ.get("CM_ISOLATION_GUARD_LOG")
        if guard_log:
            self.env["CM_ISOLATION_GUARD_LOG"] = guard_log
        self._count = 0

    # ---------------------------------------------------------- launching

    def _argv(self, spec: dict[str, Any]) -> tuple[list[str], Path]:
        self._count += 1
        spec_path = self.specs / f"launch-{self._count:02d}.json"
        plan = self.specs / f"plan-{self._count:02d}.json"
        spec = {
            **spec, "plan": str(plan), "assets": str(FIXTURE_ROOT), "cwd": str(self.fx.project_dir),
            "managed_root": str(self.fx.root / "managed-policy"), "contract": str(_CONTRACT_PATH),
        }
        state.atomic_write(spec_path, json.dumps(spec).encode())
        argv = probe.isolated_argv([self.python, str(self.launcher), str(spec_path)], env_keys=list(self.env),
                                   bridges=(self.bridge,), cwd=self.fx.project_dir, pid_namespace=True)
        at = argv.index("--dev-bind") + 3
        rebind = ["--bind", str(self.tmp_rebind), str(self.tmp_rebind)] if self.tmp_rebind else []
        argv[at:at] = ["--bind", str(self.nstmp), "/tmp", *rebind]
        return argv, plan

    @staticmethod
    def plan(path: Path) -> dict[str, Any]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def headless(self, mode: str, prompt: str, **spec: Any) -> dict[str, Any]:
        argv, plan_path = self._argv({"mode": mode, "passthrough": ["-p", prompt, *spec.pop("passthrough", ())],
                                      **spec})
        out, err = self.fx.root / f"out-{self._count:02d}.txt", self.fx.root / f"err-{self._count:02d}.txt"
        with open(out, "wb") as stdout, open(err, "wb") as stderr:
            process = subprocess.Popen(argv, cwd=self.fx.project_dir, env=self.env, stdin=subprocess.DEVNULL,
                                       stdout=stdout, stderr=stderr, close_fds=True, process_group=0)
            try:
                code = process.wait(timeout=_RUN_TIMEOUT)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                code = None
        return {"rc": code, "stdout": out.read_text(errors="replace"), "stderr": err.read_text(errors="replace"),
                **self.plan(plan_path)}

    def terminal(self, mode: str, *, passthrough: tuple[str, ...] = (), **spec: Any) -> tuple[_Terminal, Path]:
        argv, plan_path = self._argv({"mode": mode, "passthrough": list(passthrough), **spec})
        term = _Terminal(argv, env=self.env, cwd=self.fx.project_dir)
        self.case.addCleanup(term.close)
        return term, plan_path

    def ready(self, term: _Terminal, timeout: float = 90.0) -> bool:
        """Wait for the input prompt; answer a first-run dialog if one shows."""

        deadline = time.monotonic() + timeout
        since = 0
        while time.monotonic() < deadline:
            if term.process.poll() is not None:
                return False
            if not term.wait_for("❯", since=since, timeout=1.0):
                continue
            time.sleep(2.0)
            screen = term.text(since)[-3000:]
            if "Press Enter to continue" in screen or "Choose the text style" in screen:
                since = term.mark()
                term.send(b"\r")
                time.sleep(1.0)
                continue
            return True
        return False

    def stream_release(self) -> None:
        self.responder.stream_gate.set()
        try:
            self.release.touch()
        except OSError:
            pass

    # ---------------------------------------------------------- observing

    def transcript(self, runtime_id: str) -> Path:
        from charmer.cli import session_facts

        slug = session_facts._native_project_slug(str(self.fx.project_dir))
        return self.claude_dir / "projects" / slug / f"{runtime_id}.jsonl"

    def record_bytes(self, managed_id: str) -> bytes | None:
        path = self.state_root / "sessions" / f"{managed_id}.json"
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    def record(self, managed_id: str) -> dict[str, Any]:
        data = self.record_bytes(managed_id)
        return {} if data is None else json.loads(data)

    def scope_digest(self, managed_id: str) -> str:
        root = self.state_root / "scopes" / managed_id
        digest = hashlib.sha256()
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes() + b"\0")
        return digest.hexdigest()[:16]

    def hook_rows(self) -> list[dict[str, Any]]:
        try:
            return [json.loads(line) for line in self.events.read_text(encoding="utf-8").splitlines() if line]
        except FileNotFoundError:
            return []

    def daemon_state(self) -> dict[str, Any]:
        expected = hashlib.sha256(str(self.claude_dir).encode()).hexdigest()[:8]
        domains = sorted(entry.name for entry in self.nstmp.iterdir() if entry.name.startswith("cc-daemon"))
        return {"private_tmp_daemon_dirs": domains, "expected_subdomain": expected,
                "subdomain_present": any((self.nstmp / name / expected).exists() for name in domains),
                "jobs_dir": (self.claude_dir / "jobs").exists()}


def _entries(path: Path) -> list[dict[str, Any]]:
    rows = []
    try:
        lines = path.read_bytes().splitlines()
    except FileNotFoundError:
        return rows
    for line in lines:
        try:
            value = json.loads(line)
        except ValueError:
            rows.append({"_unparsable": True})
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _transcript_facts(path: Path, markers: tuple[str, ...] = (*_TURNS, SUMMARY, *_EXTRA_MARKERS.values())
                      ) -> dict[str, Any]:
    """Metadata of one fixture conversation file (never its text)."""

    if not path.is_file():
        return {"exists": False}
    data = path.read_bytes()
    entries = _entries(path)
    text = data.decode("utf-8", errors="replace")
    return {
        "exists": True,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest()[:16],
        "entries": len(entries),
        "session_ids": sorted({str(entry.get("sessionId")) for entry in entries if "sessionId" in entry}),
        "prompt_snapshots": sum(1 for entry in entries if isinstance(entry.get("attachment"), dict)
                                and entry["attachment"].get("type") == "prompt_snapshot"),
        "compact_boundaries": sum(1 for entry in entries if entry.get("type") == "system"
                                  and entry.get("subtype") == "compact_boundary"),
        "unparsable": sum(1 for entry in entries if entry.get("_unparsable")),
        "markers": sorted(marker for marker in markers if marker in text),
    }


# ----------------------------------------------------------------- the probes


class ForkLaunchProbe(unittest.TestCase):
    """client check fork-launch (see the module docstring)."""

    trusted: probe.TrustedExecutable
    version = "unknown"
    boundary: str | None = None
    rows: dict[str, dict[str, Any]] = {}
    verdicts: dict[str, str] = {}

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        gate = real_binary_gate(_CONTRACT_PATH, what="the fork-launch probes")
        cls.boundary = gate.boundary
        if gate.trusted is not None:
            cls.trusted = gate.trusted
            cls.version = gate.version
        cls.rows = {}
        cls.verdicts = {}

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.boundary is not None:
            print(f"client check fork-launch: BOUNDARY {cls.boundary}")
        elif cls.verdicts:
            failed = sorted(name for name, verdict in cls.verdicts.items() if verdict != "pass")
            overall = "PASS" if not failed else "FAIL"
            detail = " ".join(f"{name}={verdict}" for name, verdict in sorted(cls.verdicts.items()))
            print(f"client check fork-launch: {overall} client={cls.version} {detail}")
        super().tearDownClass()

    def setUp(self) -> None:
        super().setUp()
        if self.boundary is not None:
            self.skipTest(self.boundary)
        self.fixture = _ForkFixture(self, self.trusted, self.version)

    def _row(self, name: str, row: dict[str, Any], problems: list[str]) -> None:
        """Print the row's evidence, then fail the test on any problem."""

        row = {**row, "problems": problems}
        type(self).rows[name] = row
        type(self).verdicts[name] = "pass" if not problems else "fail"
        print(f"fork-launch row {name}: {json.dumps(row, sort_keys=True, default=str)}", flush=True)
        if problems:
            self.fail(f"{name}: " + "; ".join(problems))

    def _lead_rows(self, since: float = 0.0, tag: str | None = None) -> list[dict[str, Any]]:
        return [row for row in self.fixture.responder.snapshot()
                if row["main"] and row["received"] >= since and (tag is None or row["tag"] == tag)]

    # ------------------------------------------------------------- G1

    def _parent_headless(self, *, compact: bool) -> dict[str, Any]:
        fx = self.fixture
        first = fx.headless("fresh", T1)
        self.assertEqual(first.get("rc"), 0, f"parent launch: {first.get('error')} {first['stderr'][-800:]}")
        parent = first["managed_id"]
        if compact:
            compacted = fx.headless("resume", "/compact", session=parent)
            self.assertEqual(compacted.get("rc"), 0,
                             f"parent compaction: {compacted.get('error')} {compacted['stderr'][-800:]}")
        return first

    def _g1(self, name: str, *, compact: bool, extra: tuple[str, ...] = (), resume_fork: bool = False,
            resume_extra: tuple[str, ...] = ()) -> None:
        fx = self.fixture
        first = self._parent_headless(compact=compact)
        parent, parent_rt = first["managed_id"], first["runtime_id"]
        parent_file = fx.transcript(parent_rt)
        before = {"transcript": _transcript_facts(parent_file), "record": fx.record_bytes(parent),
                  "scope": fx.scope_digest(parent)}
        started = time.monotonic()
        fork = fx.headless("fork", T2, parent=parent, extra_client_args=list(extra))
        child = fork.get("managed_id")
        problems: list[str] = []
        if fork.get("rc") != 0:
            problems.append(f"fork exit {fork.get('rc')}: {fork.get('error')}")
        if reply_text(T2) not in fork.get("stdout", ""):
            problems.append("the fork printed no reply")
        argv = fork.get("argv") or []
        if argv[:5] != ["--resume", parent_rt, "--fork-session", "--session-id", child]:
            problems.append("fork argv shape")
        after = {"transcript": _transcript_facts(parent_file), "record": fx.record_bytes(parent),
                 "scope": fx.scope_digest(parent)}
        if after != before:
            problems.append("the parent changed: " + ", ".join(key for key in before if before[key] != after[key]))
        child_file = fx.transcript(child) if child else None
        child_facts = _transcript_facts(child_file) if child_file else {"exists": False}
        if not child_facts.get("exists"):
            problems.append("no conversation file for the fork's planned id")
        leads = self._lead_rows(started, T2)
        lead = leads[0] if leads else {}
        history = SUMMARY if compact else T1
        if history not in lead.get("messages_markers", ()):
            problems.append(f"the fork's first lead request lacks the parent's {history}")
        if lead.get("system_sentinels") != [child]:
            owner = ("parent" if lead.get("system_sentinels") == [parent]
                     else str(lead.get("system_sentinels")))
            problems.append(f"the fork's system prompt names {owner}, not the fork")
        if not lead.get("system_lead_body"):
            problems.append("the fork's system prompt lacks the lead body")
        hooks = [row for row in fx.hook_rows() if row.get("call") == "session-event"
                 and row.get("event") == "start" and row.get("managed_id") == child]
        starts = [(row.get("source"), row.get("session_id") == child, row.get("rc"), row.get("ms"))
                  for row in hooks]
        begun = [(row.get("source"), row.get("session_id") == child) for row in fx.hook_rows()
                 if row.get("call") == "session-event-begin" and row.get("event") == "start"
                 and row.get("managed_id") == child]
        if not any(start[:3] == ("fork", True, 0) for start in starts):
            problems.append(f"no SessionStart source fork with the new id on the fork's hooks ({starts})")
        parent_hooks = [row for row in fx.hook_rows() if row.get("managed_id") == parent
                        and row.get("call") == "session-event"]
        daemon = fx.daemon_state()
        if daemon["private_tmp_daemon_dirs"] or daemon["jobs_dir"]:
            problems.append("a daemon or jobs directory appeared")
        if "fork-own" not in lead.get("messages_prefaces", ()) or "fork-shared" in lead.get(
                "messages_prefaces", ()):
            problems.append(f"the fork's first request lacks its own-fork notice ({lead.get('messages_prefaces')})")
        record = fx.record(child) if child else {}
        if record.get("identity_state") != "authoritative" or record.get("pending_forks"):
            problems.append(f"the fork's record is {record.get('identity_state')} with pending forks "
                            f"{record.get('pending_forks')}")
        resumed: dict[str, Any] = {}
        if resume_fork and child:
            again_started = time.monotonic()
            again = fx.headless("resume", T5, session=child, extra_client_args=list(resume_extra))
            again_lead = self._lead_rows(again_started, T5)
            resumed = {"rc": again.get("rc"), "error": again.get("error"), "extra_client_args": list(resume_extra),
                       "system_sentinels": again_lead[0]["system_sentinels"] if again_lead else None,
                       "child_transcript": _transcript_facts(child_file)}
            if again.get("rc") != 0 or not again_lead or again_lead[0]["system_sentinels"] != [child]:
                problems.append("the fork's own resume does not send the fork's prompt")
        row = {
            "client": self.version, "parent": parent[:8], "fork": (child or "")[:8],
            "extra_client_args": list(extra),
            "parent_transcript": after["transcript"], "parent_unchanged": after == before,
            "parent_hook_events_after_fork": len(parent_hooks),
            "fork_transcript": child_facts,
            "fork_first_lead": {key: lead.get(key) for key in (
                "messages_markers", "messages_prefaces", "roles", "other_roles", "repeated_roles", "unpaired_tool_uses",
                "system_lead_body", "system_sha256", "model")} | {
                "system_names": ("fork" if lead.get("system_sentinels") == [child] else
                                 "parent" if lead.get("system_sentinels") == [parent] else
                                 str(lead.get("system_sentinels")))},
            "fork_lead_requests": len(leads),
            "fork_session_starts": starts, "fork_session_start_begins": begun,
            "fork_record": {key: record.get(key) for key in (
                "forked_from", "runtime_session_id", "identity_state", "last_event_source", "pending_forks")},
            "parent_record_pending_forks": json.loads(after["record"] or b"{}").get("pending_forks"),
            "daemon": daemon,
            "resumed_fork": resumed,
        }
        if row["fork_record"]["forked_from"] != parent:
            problems.append("the fork record lacks forked_from")
        self._row(name, row, problems)

    def test_g1_fork_of_a_parent_with_an_established_prompt_snapshot(self) -> None:
        self._g1("G1-snapshot", compact=False, resume_fork=True)

    def test_g1_fork_of_a_compacted_parent(self) -> None:
        self._g1("G1-compacted", compact=True)

    def test_g1_fork_and_its_resume_with_the_prompt_snapshot_switched_off(self) -> None:
        self._g1("G1-snapshot-off-every-launch", compact=False, extra=_SNAPSHOT_OFF, resume_fork=True,
                 resume_extra=_SNAPSHOT_OFF)

    FORK_STARTS = 5

    def test_g1_every_fork_start_reaches_the_forks_hooks(self) -> None:
        """One parent forked ``FORK_STARTS`` times in a row: every fork's
        SessionStart hook begins and ends on the fork's own hooks with source
        ``fork``, the fork's id and exit 0 (counted, so a miss is a defect)."""

        fx = self.fixture
        parent = self._parent_headless(compact=False)["managed_id"]
        counts = {"forks": 0, "exit_0": 0, "begun": 0, "recorded": 0, "notice": 0}
        durations: list[int] = []
        for _ in range(self.FORK_STARTS):
            started = time.monotonic()
            fork = fx.headless("fork", T2, parent=parent)
            child = fork.get("managed_id")
            counts["forks"] += 1
            counts["exit_0"] += fork.get("rc") == 0
            rows = [row for row in fx.hook_rows() if row.get("managed_id") == child and row.get("event") == "start"]
            counts["begun"] += any(row.get("call") == "session-event-begin" and row.get("source") == "fork"
                                   and row.get("session_id") == child for row in rows)
            done = [row for row in rows if row.get("call") == "session-event" and row.get("source") == "fork"
                    and row.get("session_id") == child and row.get("rc") == 0]
            counts["recorded"] += bool(done)
            durations.extend(row.get("ms") for row in done)
            leads = self._lead_rows(started, T2)
            counts["notice"] += bool(leads and "fork-own" in leads[0]["messages_prefaces"])
        problems = [f"{key} {value}/{self.FORK_STARTS}" for key, value in counts.items()
                    if value != self.FORK_STARTS]
        self._row("G1-repeat", {"client": self.version, "parent": parent[:8], "counts": counts,
                                "start_hook_ms": durations}, problems)

    # ------------------------------------------------------------- G2

    def _start_parent(self, *, passthrough: tuple[str, ...] = ()) -> tuple[_Terminal, dict[str, Any]]:
        fx = self.fixture
        term, plan_path = fx.terminal("fresh", passthrough=passthrough)
        if not fx.ready(term):
            self.fail(f"parent never reached its prompt: {fx.plan(plan_path).get('error')} "
                      f"{term.text()[-600:]}")
        return term, fx.plan(plan_path)

    def _turn(self, term: _Terminal, text: str, reply: str, timeout: float = 90.0) -> bool:
        mark = term.mark()
        term.type_line(text)
        return term.wait_for(reply, since=mark, timeout=timeout)

    def _exit(self, term: _Terminal) -> int | None:
        term.type_line("/exit")
        code = term.wait_exit(60)
        if code is None:
            term.send(b"\x04")
            time.sleep(0.5)
            term.send(b"\x04")
            code = term.wait_exit(30)
        return code

    def test_g2_idle_parent_stays_live_while_the_fork_runs(self) -> None:
        self._g2("G2-idle")

    def test_g2_idle_parent_with_the_fork_prompt_snapshot_switched_off(self) -> None:
        self._g2("G2-idle-snapshot-off", extra=_SNAPSHOT_OFF)

    def _g2(self, name: str, *, extra: tuple[str, ...] = ()) -> None:
        fx = self.fixture
        parent_term, parent_plan = self._start_parent()
        parent, parent_rt = parent_plan["managed_id"], parent_plan["runtime_id"]
        problems: list[str] = []
        if not self._turn(parent_term, T1, reply_text(T1)):
            self.fail(f"parent turn one: {parent_term.text()[-600:]}")
        time.sleep(1.0)
        parent_file = fx.transcript(parent_rt)
        at_fork = parent_file.read_bytes()
        record_at_fork = fx.record_bytes(parent)
        fork_term, fork_plan_path = fx.terminal("fork", parent=parent, extra_client_args=list(extra))
        if not fx.ready(fork_term):
            self.fail(f"fork never reached its prompt: {fx.plan(fork_plan_path).get('error')} "
                      f"{fork_term.text()[-600:]}")
        fork_plan = fx.plan(fork_plan_path)
        child = fork_plan["managed_id"]
        started = time.monotonic()
        if not self._turn(fork_term, T2, reply_text(T2)):
            problems.append("the fork's first turn got no reply")
        if fx.record_bytes(parent) != record_at_fork:
            problems.append("the parent's record changed while the fork started")
        if not self._turn(parent_term, T3, reply_text(T3)):
            problems.append("the live parent's next turn got no reply")
        if not self._turn(fork_term, T4, reply_text(T4)):
            problems.append("the fork's second turn got no reply")
        fork_exit, parent_exit = self._exit(fork_term), self._exit(parent_term)
        parent_final = parent_file.read_bytes()
        child_file = fx.transcript(child)
        parent_facts, child_facts = _transcript_facts(parent_file), _transcript_facts(child_file)
        if not parent_final.startswith(at_fork):
            problems.append("the parent's conversation before the fork is not a prefix of its final file")
        if T2 in parent_facts["markers"] or T4 in parent_facts["markers"]:
            problems.append("the fork's turns reached the parent's conversation file")
        if T3 in child_facts.get("markers", ()):
            problems.append("the parent's later turn reached the fork's conversation file")
        if not {T1, T2, T4} <= set(child_facts.get("markers", ())):
            problems.append("the fork's conversation file lacks the history or its own turns")
        if parent_facts["session_ids"] != [parent_rt] or child_facts.get("session_ids") != [child]:
            problems.append("a conversation file carries entries of the other session")
        if fork_exit != 0 or parent_exit != 0:
            problems.append(f"exit codes fork={fork_exit} parent={parent_exit}")
        leads = self._lead_rows(started, T2)
        record = fx.record(child)

        def names(tag: str) -> list[Any]:
            return [("fork" if row["system_sentinels"] == [child] else "parent"
                     if row["system_sentinels"] == [parent] else str(row["system_sentinels"]), row["system_sha256"])
                    for row in self._lead_rows(0.0, tag)]

        self._row(name, {
            "client": self.version, "parent": parent[:8], "fork": child[:8], "extra_client_args": list(extra),
            "parent_transcript": parent_facts, "fork_transcript": child_facts,
            "fork_first_lead": {key: leads[0].get(key) for key in ("messages_markers", "roles", "system_sentinels")}
            if leads else None,
            "system_prompt_by_turn": {"T1-parent": names(T1), "T2-fork": names(T2), "T3-parent": names(T3),
                                      "T4-fork": names(T4)},
            "parent_record_pending_forks": fx.record(parent).get("pending_forks"),
            "fork_record": {key: record.get(key) for key in ("forked_from", "identity_state", "pending_forks")},
            "exits": [fork_exit, parent_exit], "daemon": fx.daemon_state(),
        }, problems)

    # ------------------------------------------------------------- G2b

    def _busy(self, name: str, hold: str, *, fork_pty: bool = False) -> None:
        fx = self.fixture
        passthrough = ("--allowedTools", "Bash") if hold == HOLD_TOOL else ()
        parent_term, parent_plan = self._start_parent(passthrough=passthrough)
        parent, parent_rt = parent_plan["managed_id"], parent_plan["runtime_id"]
        problems: list[str] = []
        if not self._turn(parent_term, T1, reply_text(T1)):
            self.fail(f"parent turn one: {parent_term.text()[-600:]}")
        mark = parent_term.mark()
        parent_term.type_line(hold)
        if hold == HOLD_STREAM:
            busy = fx.responder.stream_started.wait(60)
        else:
            deadline = time.monotonic() + 60
            while not fx.tool_started.exists() and time.monotonic() < deadline:
                time.sleep(0.2)
            busy = fx.tool_started.exists()
        if not busy:
            self.fail(f"the parent never became busy: {parent_term.text(mark)[-600:]}")
        time.sleep(1.5)
        parent_file = fx.transcript(parent_rt)
        at_fork = parent_file.read_bytes()
        at_fork_facts = _transcript_facts(parent_file)
        record_at_fork = fx.record_bytes(parent)
        started = time.monotonic()
        fork_exit: int | None = None
        if fork_pty:
            fork_term, fork_plan_path = fx.terminal("fork", parent=parent)
            if not fx.ready(fork_term):
                self.fail(f"fork never reached its prompt: {fx.plan(fork_plan_path).get('error')} "
                          f"{fork_term.text()[-600:]}")
            child = fx.plan(fork_plan_path).get("managed_id") or ""
            if not self._turn(fork_term, T2, reply_text(T2)):
                problems.append("the fork's first turn got no reply")
            if not self._turn(fork_term, T4, reply_text(T4)):
                problems.append("the fork's second turn got no reply")
            fork_exit = self._exit(fork_term)
            if fork_exit != 0:
                problems.append(f"fork exit {fork_exit}")
        else:
            fork = fx.headless("fork", T2, parent=parent)
            child = fork.get("managed_id") or ""
            if fork.get("rc") != 0 or reply_text(T2) not in fork.get("stdout", ""):
                problems.append(f"the fork's turn failed: rc={fork.get('rc')} {fork.get('error')}")
            second = fx.headless("resume", T4, session=child) if child else {}
            if second.get("rc") != 0 or reply_text(T4) not in second.get("stdout", ""):
                problems.append(f"the fork's second turn failed: rc={second.get('rc')} {second.get('error')}")
        leads = self._lead_rows(started, T2)
        lead = leads[0] if leads else {}
        if lead.get("unpaired_tool_uses"):
            problems.append("the fork's first request carries a tool_use without its tool_result")
        if T1 not in lead.get("messages_markers", ()):
            problems.append("the fork's first request lacks the parent's history")
        second_leads = self._lead_rows(started, T4)
        if fx.record_bytes(parent) != record_at_fork:
            problems.append("the parent's record changed while it was forked")
        parent_still_busy = parent_term.process.poll() is None
        fx.stream_release()
        expected = "FORKREPLY-STREAM" if hold == HOLD_STREAM else "FORKREPLY-TOOL"
        if not parent_term.wait_for(expected, since=mark, timeout=90):
            problems.append("the parent did not finish its busy turn after the fork")
        if not self._turn(parent_term, T3, reply_text(T3)):
            problems.append("the parent's next turn got no reply")
        parent_exit = self._exit(parent_term)
        parent_final = parent_file.read_bytes()
        parent_facts = _transcript_facts(parent_file)
        child_facts = _transcript_facts(fx.transcript(child)) if child else {"exists": False}
        if not parent_final.startswith(at_fork):
            problems.append("the parent's conversation at the fork is not a prefix of its final file")
        if {T2, T4} & set(parent_facts["markers"]):
            problems.append("the fork's turns reached the parent's conversation file")
        if T3 in child_facts.get("markers", ()):
            problems.append("the parent's later turn reached the fork's conversation file")
        if parent_facts["session_ids"] != [parent_rt] or child_facts.get("session_ids") != [child]:
            problems.append("a conversation file carries entries of the other session")
        if parent_exit != 0:
            problems.append(f"parent exit {parent_exit}")
        retries = len(self._lead_rows(0.0, hold))
        self._row(name, {
            "client": self.version, "parent": parent[:8], "fork": child[:8],
            "fork_mode": "pty" if fork_pty else "headless", "fork_exit": fork_exit,
            "parent_transcript_at_fork": at_fork_facts, "parent_transcript": parent_facts,
            "fork_transcript": child_facts,
            "fork_first_lead": {key: lead.get(key) for key in (
                "messages_markers", "messages_extra", "roles", "repeated_roles", "unpaired_tool_uses",
                "assistant_shapes", "probe_tool_result", "system_sentinels")},
            "fork_second_lead": {key: second_leads[0].get(key) for key in (
                "messages_markers", "roles", "unpaired_tool_uses", "system_sentinels")} if second_leads else None,
            "parent_busy_requests": retries, "parent_alive_at_release": parent_still_busy,
            "parent_exit": parent_exit, "daemon": fx.daemon_state(),
        }, problems)

    def test_g2b_fork_while_the_parent_is_streaming(self) -> None:
        self._busy("G2b-streaming", HOLD_STREAM)

    def test_g2b_fork_while_the_parent_runs_a_tool(self) -> None:
        self._busy("G2b-tool", HOLD_TOOL)

    def test_g2b_interactive_fork_while_the_parent_is_streaming(self) -> None:
        self._busy("G2b-streaming-pty", HOLD_STREAM, fork_pty=True)

    def test_g2b_interactive_fork_while_the_parent_runs_a_tool(self) -> None:
        self._busy("G2b-tool-pty", HOLD_TOOL, fork_pty=True)

    # ------------------------------------------------------------- G3

    def test_g3_builtin_fork_in_a_charmer_session_is_recorded(self) -> None:
        fx = self.fixture
        term, plan = self._start_parent()
        if not self._turn(term, T1, reply_text(T1)):
            self.fail(f"session turn one: {term.text()[-600:]}")
        mark = term.mark()
        term.send(b"/fork")
        time.sleep(1.0)
        menu = term.text(mark)
        term.send(b"\r")
        refused = term.wait_for("Can't fork", since=mark, timeout=20)
        time.sleep(2.0)
        screen = term.text(mark)
        found = re.search(r"Can.t fork:[^⎿❯]{0,480}", screen)
        daemon = fx.daemon_state()
        exit_code = self._exit(term)
        problems = []
        if not refused:
            problems.append("the client did not refuse /fork")
        if daemon["private_tmp_daemon_dirs"] or daemon["jobs_dir"]:
            problems.append("a daemon or jobs directory appeared")
        self._row("G3-builtin-fork", {
            "client": self.version, "session": plan["managed_id"][:8], "refused": refused,
            "refusal": found.group(0)[:480] if found else None,
            "menu_lists_fork": "/fork" in menu, "daemon": daemon, "exit": exit_code,
        }, problems)


if __name__ == "__main__":
    unittest.main()
