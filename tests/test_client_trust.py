"""Client check directory-trust: what Claude Code asks about the session's
scope folder, which every managed launch passes with ``--add-dir``.

A terminal session of a newer client asks before it adds a folder nobody
trusted ("Adding a new directory: ... Your answer holds for this session",
choices "No, exit" and "Yes, add it"). A stored trust entry for the folder
or, outside a git repository, for any folder above it is honoured. Every run
here is the production compile of the balanced seed in a terminal, with the
production argv (session id, resume or fork; name; flag settings; lead;
effort; ``--add-dir <scope>``; the lead prompt file) and the fixture project
trusted. What a case changes is the scope's own trust:

* ``fresh``, ``resume`` and ``fork`` with the scope not trusted: does the
  question come, and after "Yes" does the session work (a turn reads
  ``lineup.md``, spawns ``cm-explorer`` on its bound model and ``/cm`` runs
  the launcher), and does the client store anything for the scope;
* ``no``: what "No, exit" does;
* ``scopes-root``: the folder holding every scope trusted, the scope not;
* ``exact-scope``: the scope itself trusted;
* ``print``: ``-p`` with the scope not trusted (turn and ``/cm``);
* ``reload``: after "Yes", an edited agent file is used after
  ``/reload-plugins`` and by a new process, as with a trusted scope.

The line: ``client check directory-trust: <STATUS> class=<class> <detail>``;
each case prints its observations. Metadata only: booleans, models, agent
types, exit codes and the client's own short messages.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from typing import Any

from charmer import probe, state, strict_json
from charmer.probe import ProbeError

import test_client_agent_reload as reload_lane
import test_client_agents as agents
import test_client_models as models
from _catalog import uses_shipped_catalog
from _layout import RESOURCES_ROOT
from _tier import real_binary_gate

_CONTRACT = RESOURCES_ROOT / "catalog" / "native-contract.json"
_PROMPT = "❯".encode()
_TURN = "client check trust"
_AGENT = "cm-explorer"
_DIALOG = "Adding a new directory"
_NO_EXIT = re.compile(r"To go on without it[^.]{0,200}\.")
_STUB = "#!/bin/sh\necho {marker}\n"


def _pause(seconds: float = 1.0):
    return lambda: threading.Event().wait(seconds)


# "No, exit" is listed first and has the focus; the question takes input
# only after a short arming delay, so each key waits a moment first. The
# question draws its focus glyph (the prompt glyph) once it is armed, on "No,
# exit", and again on "Yes, add it" after the arrow key: one step consumes
# each, so a caller's next prompt-glyph step matches only what the session
# draws once the question has closed. Text typed while it closes is lost.
ANSWER_YES = (probe.PTYInteraction(b"Adding", b"", preserve_after_wait=True),
              probe.PTYInteraction(_PROMPT, b"\x1b[B", before_send=_pause()),
              probe.PTYInteraction(_PROMPT, b"\r", before_send=_pause(0.5)))
ANSWER_NO = (probe.PTYInteraction(b"Adding", b"\r", before_send=_pause()),)
ONBOARDING = (probe.PTYInteraction(b"Choose", b"2\r"), probe.PTYInteraction(b"Press", b"\r"))


def trusted_paths(fixture: probe.ProbeFixture) -> set[str]:
    """The folders the fixture's client config marks as trusted."""

    config = fixture.claude_config_dir / ".claude.json"
    if not config.exists():
        return set()
    projects = json.loads(config.read_text(encoding="utf-8")).get("projects") or {}
    return {key for key, value in projects.items() if isinstance(value, dict) and value.get("hasTrustDialogAccepted")}


def untrust(fixture: probe.ProbeFixture, path: Path) -> None:
    """Drop one folder's entry from the fixture's client config."""

    config = fixture.claude_config_dir / ".claude.json"
    document = strict_json.loads(state.read_private(config))
    projects = dict(document.get("projects") or {})
    projects.pop(str(path), None)
    state.atomic_write(config, strict_json.canonical_file_bytes({**document, "projects": projects}))


class _TrustResponder:
    """A turn typed as ``_TURN <nonce>`` reads ``lineup.md``, then spawns
    ``cm-explorer``, then answers TRUST-DONE once the agent's reply is back;
    a turn carrying this run's ``/cm`` output gets CM-ACK; the agent gets
    AGENT-OK. Every marker carries the run's nonce, so a resumed or forked
    conversation's earlier turns never count."""

    def __init__(self, lineup: Path, line: str) -> None:
        self.lineup, self.line = str(lineup), line
        self.nonce = uuid.uuid4().hex[:8]
        self.read: bool | None = None
        self.spawns: list[tuple[str, str]] = []
        self.cm_ran = False
        self._lock = threading.Lock()

    def marker(self, name: str) -> str:
        return f"{name}_{self.nonce}"

    def _read_result(self, messages: list[Any]) -> bool:
        for message in messages:
            for block in message.get("content") or [] if isinstance(message, dict) else []:
                if isinstance(block, dict) and block.get("tool_use_id") == self.marker("toolu_trust_read"):
                    return not block.get("is_error") and self.line in json.dumps(block.get("content"))
        return False

    def respond(self, document: dict[str, Any], path: str, context: Any) -> Any:
        if path.split("?", 1)[0].rstrip("/").endswith("/count_tokens"):
            return 200, {"input_tokens": 1}
        record = context.record
        hints = dict(record.hint_header_values)
        messages = document.get("messages") or []
        conversation = json.dumps(messages)
        agent_type = hints.get("x-claude-code-agent-type")
        content: list[dict[str, Any]]
        stop = "end_turn"
        with self._lock:
            if agent_type or hints.get("x-claude-code-request-class") == "subagent":
                self.spawns.append((str(agent_type), str(record.model)))
                content = [{"type": "text", "text": self.marker("AGENT-OK")}]
            elif not record.tool_count:
                content = [{"type": "text", "text": "PROBE-OK"}]
            elif self.marker("CM-SKILL-RAN") in conversation:
                self.cm_ran = True
                content = [{"type": "text", "text": self.marker("CM-ACK")}]
            elif self.marker("toolu_trust_agent") in conversation:
                done = self.marker("AGENT-OK") in conversation
                content = [{"type": "text", "text": self.marker("TRUST-DONE" if done else "TRUST-SPAWNED")}]
            elif self.marker("toolu_trust_read") in conversation:
                self.read = self._read_result(messages)
                content = [{"type": "tool_use", "id": self.marker("toolu_trust_agent"), "name": "Agent",
                            "input": {"description": "trust check", "subagent_type": _AGENT,
                                      "prompt": "Reply with exactly: AGENT-OK"}}]
                stop = "tool_use"
            elif self.marker(_TURN) in conversation:
                content = [{"type": "tool_use", "id": self.marker("toolu_trust_read"), "name": "Read",
                            "input": {"file_path": self.lineup}}]
                stop = "tool_use"
            else:
                content = [{"type": "text", "text": "PROBE-OK"}]
        payload = probe._message_payload(content, model=document.get("model"), stop_reason=stop,
                                         message_id=f"msg_trust_{uuid.uuid4().hex[:8]}")
        return 200, probe._sse_message(payload) if document.get("stream") else payload


@uses_shipped_catalog
class DirectoryTrustProbe(unittest.TestCase):
    probe_id = "directory-trust"
    cases: dict[str, dict[str, Any]] = {}

    @classmethod
    def setUpClass(cls) -> None:
        cls.gate = real_binary_gate(_CONTRACT, what="the directory-trust check")
        cls.cases = {}

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.gate.boundary:
            print(f"client check {cls.probe_id}: BOUNDARY class=unavailable {cls.gate.boundary}", flush=True)
            return
        print(f"client check {cls.probe_id}: MEASURED class={trust_class(cls.cases)} "
              f"cases={sorted(cls.cases)} client={cls.gate.version}", flush=True)

    def setUp(self) -> None:
        if self.gate.boundary:
            self.skipTest(self.gate.boundary)
        self.trusted = self.gate.trusted
        self.root = Path(tempfile.mkdtemp(prefix="charmer-trust-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.environ = {"HOME": str(self.root / "live" / "home"), "PATH": "/usr/bin:/bin"}
        self.fixture = probe.build_fixture(self.root / "fixture", environ=self.environ)
        probe.seed_fixture_project_trust(self.fixture, self.fixture.project_dir)
        self.bin = state.ensure_private_dir(self.fixture.root / "bin")
        self.lead = state.ensure_private_dir(self.fixture.root / "lead") / "lead.md"
        state.atomic_write(self.lead, b"You are the lead of a client check session.\n")

    def _compile(self) -> dict[str, Any]:
        managed = models.compiled_managed_scope(self.fixture, "balanced", trust_scope=False)
        self.assertNotIn(str(managed["live"]), trusted_paths(self.fixture))
        return managed

    def _argv(self, managed: dict[str, Any], action: tuple[str, ...], fork: bool = False) -> tuple[str, ...]:
        tail = ("--system-prompt-snapshot", "off") if fork else ()
        return (*action, "--name", "cm:balanced", "--settings", str(managed["settings"]), "--model",
                managed["lead"], "--effort", managed["effort"], "--add-dir", str(managed["live"]),
                "--append-system-prompt-file", str(self.lead), *tail)

    def _responder(self, managed: dict[str, Any]) -> _TrustResponder:
        """This run's responder; the fixture's ``charmer`` (what ``/cm``
        runs) prints this run's marker."""

        lineup = Path(managed["live"]) / "lineup.md"
        line = max(lineup.read_text(encoding="utf-8").splitlines(), key=len)
        responder = _TrustResponder(lineup, line)
        probe.write_fixture_executable(self.fixture, self.bin, "charmer",
                                       _STUB.format(marker=responder.marker("CM-SKILL-RAN")))
        return responder

    def _terminal(self, name: str, managed: dict[str, Any], argv: tuple[str, ...],
                  before: tuple[probe.PTYInteraction, ...], *, turn: bool = True) -> dict[str, Any]:
        responder = self._responder(managed)
        steps = [*before]
        if turn:
            steps += [probe.PTYInteraction(_PROMPT, f"{responder.marker(_TURN)}\r".encode(), before_send=_pause()),
                      probe.PTYInteraction(responder.marker("TRUST-DONE").encode(), b"/cm profiles\r",
                                           before_send=_pause()),
                      probe.PTYInteraction(responder.marker("CM-ACK").encode(), b"/exit\r", before_send=_pause())]
        # The question is the step that waits for its title; without one, its
        # text on the (bounded) captured screen.
        asked = next((index for index, step in enumerate(steps) if step.wait_for == b"Adding"), None)
        trusted_before = trusted_paths(self.fixture)
        observed: dict[str, Any] = {}
        with probe.FakeAnthropicProvider(responder=responder) as provider:
            try:
                result = probe.run_native_pty(argv, steps, trusted=self.trusted, fixture=self.fixture,
                                              provider=provider, timeout=150, allow_real=True,
                                              environ=self.environ, child_env=dict(models._HINT_ENV),
                                              path_dirs=(self.bin,))
                screen = agents._screen_text(result.stdout)
                observed["exit"] = result.returncode
                dialog = asked is not None or _DIALOG in screen
            except probe.PTYTimeoutError as exc:
                screen = ""
                observed["exit"] = f"timeout {exc.phase} {exc.completed_interactions}/{len(steps)}"
                dialog = asked is not None and exc.completed_interactions > asked
            except ProbeError as exc:
                if models._TRIPWIRE in str(exc):
                    self.skipTest("BOUNDARY: live daemon-domain tripwire hit")
                screen, observed["exit"] = agents._screen_text(str(exc)), "closed"
                dialog = _DIALOG in screen
        said = _NO_EXIT.search(screen)
        trusted_after = trusted_paths(self.fixture)
        observed.update({
            "dialog": dialog, "read": responder.read,
            "spawns": sorted(set(responder.spawns)), "cm": responder.cm_ran,
            "scope_stored": str(managed["live"]) in trusted_after,
            "trust_added": sorted(trusted_after - trusted_before),
            "said": said.group(0) if said else None})
        type(self).cases[name] = observed
        print(f"client check {self.probe_id} case {name}: {observed}", flush=True)
        return observed

    def _works(self, observed: dict[str, Any], managed: dict[str, Any]) -> None:
        # The client's hint names every agent from a folder "custom"; the
        # explorer's own bound model on the wire is what identifies it.
        explorer = managed["lineup"].agents[_AGENT]
        self.assertTrue(observed["read"], observed)
        self.assertIn(("custom", models._wire(explorer.binding.selector)), observed["spawns"], observed)
        self.assertTrue(observed["cm"], observed)

    def test_fresh_resume_and_fork_ask_and_yes_gives_a_working_session(self) -> None:
        managed = self._compile()
        session = str(uuid.uuid4())
        fresh = self._terminal("fresh", managed, self._argv(managed, ("--session-id", session)),
                               (*ONBOARDING, *ANSWER_YES))
        resume = self._terminal("resume", managed, self._argv(managed, ("--resume", session)), ANSWER_YES)
        forked = self._compile()
        fork = self._terminal("fork", forked, self._argv(
            forked, ("--resume", session, "--fork-session", "--session-id", str(uuid.uuid4())), fork=True),
            ANSWER_YES)
        for observed, compiled in ((fresh, managed), (resume, managed), (fork, forked)):
            self.assertTrue(observed["dialog"], observed)
            self.assertEqual(observed["exit"], 0, observed)
            self.assertFalse(observed["scope_stored"], observed)
            self._works(observed, compiled)

    def test_no_exits_the_client(self) -> None:
        managed = self._compile()
        observed = self._terminal("no", managed, self._argv(managed, ("--session-id", str(uuid.uuid4()))),
                                  (*ONBOARDING, *ANSWER_NO), turn=False)
        self.assertTrue(observed["dialog"], observed)
        self.assertEqual(observed["exit"], 1, observed)
        self.assertFalse(observed["scope_stored"], observed)

    def test_a_trusted_scopes_folder_covers_the_scope(self) -> None:
        managed = self._compile()
        probe.seed_fixture_project_trust(self.fixture, Path(managed["live"]).parent)
        observed = self._terminal("scopes-root", managed, self._argv(managed, ("--session-id", str(uuid.uuid4()))),
                                  ONBOARDING)
        self.assertFalse(observed["dialog"], observed)
        self.assertEqual(observed["exit"], 0, observed)
        self._works(observed, managed)

    def test_a_trusted_scope_is_not_asked_about(self) -> None:
        managed = self._compile()
        probe.seed_fixture_project_trust(self.fixture, Path(managed["live"]))
        observed = self._terminal("exact-scope", managed, self._argv(managed, ("--session-id", str(uuid.uuid4()))),
                                  ONBOARDING)
        self.assertFalse(observed["dialog"], observed)
        self.assertEqual(observed["exit"], 0, observed)
        self._works(observed, managed)

    def test_the_launch_question_stores_the_folder(self) -> None:
        """The one-time step a user takes for the scopes folder: start the
        client there and answer its launch question "Yes, I trust this
        folder". The client stores the folder's entry (the form the
        ``scopes-root`` case seeds)."""

        fixture = probe.build_fixture(self.root / "launch", environ=self.environ)
        self.assertNotIn(str(fixture.project_dir), trusted_paths(fixture))
        steps = (probe.PTYInteraction(b"Choose", b"2\r"), probe.PTYInteraction(b"Press", b"\r"),
                 probe.PTYInteraction(b"confirm", b"\x1b[B", before_send=_pause(0.5)),
                 probe.PTYInteraction(_PROMPT, b"\r", before_send=_pause(0.5)),
                 probe.PTYInteraction(_PROMPT, b"/exit\r", before_send=_pause()))
        with probe.FakeAnthropicProvider() as provider:
            result = probe.run_native_pty((), steps, trusted=self.trusted, fixture=fixture, provider=provider,
                                          timeout=90, allow_real=True, environ=self.environ)
        observed = {"exit": result.returncode, "stored": str(fixture.project_dir) in trusted_paths(fixture),
                    "requests": len(result.requests)}
        type(self).cases["launch-question"] = observed
        print(f"client check {self.probe_id} case launch-question: {observed}", flush=True)
        self.assertEqual(observed["exit"], 0, observed)
        self.assertTrue(observed["stored"], observed)

    def test_print_mode_never_asks(self) -> None:
        managed = self._compile()
        observed: dict[str, Any] = {}
        for label in ("turn", "cm"):
            responder = self._responder(managed)
            prompt = responder.marker(_TURN) if label == "turn" else "/cm profiles"
            with probe.FakeAnthropicProvider(responder=responder) as provider:
                result = probe.run_native(("-p", prompt, *self._argv(managed, ("--session-id", str(uuid.uuid4())))),
                                          trusted=self.trusted, fixture=self.fixture, provider=provider, timeout=150,
                                          allow_real=True, environ=self.environ, child_env=dict(models._HINT_ENV),
                                          path_dirs=(self.bin,))
            observed[label] = {"exit": result.returncode, "read": responder.read,
                               "spawns": sorted(set(responder.spawns)), "cm": responder.cm_ran,
                               "stderr": result.stderr.strip()[-300:]}
        observed["scope_stored"] = str(managed["live"]) in trusted_paths(self.fixture)
        type(self).cases["print"] = observed
        print(f"client check {self.probe_id} case print: {observed}", flush=True)
        self.assertEqual(observed["turn"]["exit"], 0, observed)


def trust_class(cases: dict[str, dict[str, Any]]) -> str:
    """T1: no question on the production shape; T2: a trusted scopes folder
    stops it; T3: the question comes back and nothing stored stops it."""

    fresh = cases.get("fresh")
    if fresh is None:
        return "incomplete"
    if not fresh["dialog"]:
        return "T1"
    root = cases.get("scopes-root")
    if root is None:
        return "incomplete"
    return "T2" if not root["dialog"] else "T3"


class ReloadAfterTrustTests(agents._SpikeRealBinaryTestCase):
    """The agent-reload check's atomic replace, with the scope not trusted and
    "Yes" answered: ``/reload-plugins`` and a new process use the edit."""

    _edit = reload_lane.AgentFileReloadTests._edit
    _run = reload_lane.AgentFileReloadTests._run
    _way = reload_lane.AgentFileReloadTests._way

    def _prepare_fixture(self, name: str) -> None:
        super()._prepare_fixture(name)
        untrust(self.fixture, self.scope)

    def _onboarding(self) -> list[probe.PTYInteraction]:
        return [*super()._onboarding(), *ANSWER_YES]

    def test_reload_after_yes_uses_the_edit(self) -> None:
        row = self._way("atomic", 9)
        old, new = f"{agents._MODEL_A}/A", f"{agents._MODEL_B}/B"
        print(f"client check {DirectoryTrustProbe.probe_id} case reload: {row} {self.evidence_label}", flush=True)
        self.assertEqual(row["spawn"], old)
        self.assertEqual(row["reload"], new)
        self.assertEqual(row["process-resume"], new)


class TrustClassTests(unittest.TestCase):
    def test_the_class_follows_the_fresh_and_scopes_folder_cases(self) -> None:
        self.assertEqual(trust_class({"fresh": {"dialog": False}}), "T1")
        self.assertEqual(trust_class({"fresh": {"dialog": True}, "scopes-root": {"dialog": False}}), "T2")
        self.assertEqual(trust_class({"fresh": {"dialog": True}, "scopes-root": {"dialog": True}}), "T3")
        self.assertEqual(trust_class({"fresh": {"dialog": True}}), "incomplete")
        self.assertEqual(trust_class({}), "incomplete")


# A stand-in for the client's question in a terminal: the focus glyph comes
# once the question is armed, and keys that arrive while it closes are
# dropped before the session's input prompt is drawn.
_QUESTION_CLIENT = """import os, sys, termios, time, tty
tty.setraw(0)
os.write(1, b"Adding a new directory:\\r\\n  No, exit\\r\\n  Yes, add it\\r\\n")
time.sleep(0.2)
os.write(1, "\\u276f No, exit\\r\\n".encode())
focus = "no"
while True:
    key = os.read(0, 1)
    if key == b"\\x1b":
        key += os.read(0, 2)
    if key == b"\\x1b[B":
        focus = "yes"
        os.write(1, "  No, exit\\r\\n\\u276f Yes, add it\\r\\n".encode())
    elif key == b"\\r":
        break
if focus != "yes":
    sys.exit(1)
time.sleep(0.3)
termios.tcflush(0, termios.TCIFLUSH)
os.write(1, "\\u276f\\u00a0".encode())
line = b""
while not line.endswith(b"\\r"):
    line += os.read(0, 1)
os.write(1, b"\\r\\nTYPED=" + line[:-1] + b"\\r\\n")
"""


class AnswerYesTests(unittest.TestCase):
    def test_the_next_prompt_waits_for_the_session_prompt(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="charmer-trust-answer-"))
        os.chmod(root, 0o700)
        self.addCleanup(shutil.rmtree, root, True)
        environ = {"HOME": str(root / "live" / "home"), "PATH": "/usr/bin:/bin"}
        fixture = probe.build_fixture(root / "fixture", environ=environ)
        script = state.ensure_private_dir(root / "bin") / "question"
        script.write_text(f"#!{sys.executable}\n{_QUESTION_CLIENT}", encoding="utf-8")
        script.chmod(0o700)
        trusted = probe.TrustedExecutable(script, hashlib.sha256(script.read_bytes()).hexdigest(), fake=True)
        result = probe.run_native_pty((), (*ANSWER_YES, probe.PTYInteraction(_PROMPT, b"first prompt\r")),
                                      trusted=trusted, fixture=fixture, timeout=15, environ=environ)
        self.assertEqual(result.returncode, 0)
        self.assertIn("TYPED=first prompt", result.stdout)


if __name__ == "__main__":
    unittest.main()
