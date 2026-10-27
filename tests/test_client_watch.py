"""Client checks on the pin's watch list: upstream reports measured on the
pinned client, each with charmer's own policy where the report touches one.

A report is a hypothesis about the client, and a changelog line is no fix
claim: each check runs the reported behaviour hermetically (loopback fixture
provider, fixture homes and repositories, no network) and records whether it
reproduces on the pin.

* ``watch-worktree``: an isolated agent (``isolation: "worktree"``) runs
  ``pwd``, then a read-only ``echo`` naming the shell words ``hash``,
  ``source``, ``alias`` and ``complete``. It finishes without changes; the
  lead then continues it with ``SendMessage`` and it runs ``pwd`` again. Kept:
  where each run worked (project, worktree, other), whether the worktree
  still existed before the continuation, and whether the isolation check let
  the ``echo`` run. The lead prompt's rule (continue an isolated agent only
  while its worktree exists) stands on the first answer.
* ``watch-effort``: ``/effort`` in a terminal session writes the user's
  settings (``modelSettings.<model>.effortLevel``); a launch that states
  ``--effort`` (every charmer launch does) against such a value, a launch
  without it, and a running session whose user settings get such a value
  between two turns.
* ``watch-scratchpad``: whether a session's scratchpad directory remains
  after the session ends (headless and terminal).
* ``watch-mcp-large``: two parallel calls of one MCP tool whose results are
  each too large to stay inline: whether each call gets its own saved result.
* ``watch-bash-queue``: many workflow agents at once, each running one
  ``Bash`` command, with hooks on the events charmer compiles: how long each
  command waits for its result.
* ``watch-teams``: whether a session offers agent teams at all (the
  ``Agent`` tool's parameters and a ``ToolSearch`` for the team tools).

Only labels, counts, timings and fixed marker strings leave a run. The output
line: ``client check watch-<name>: <REPRODUCED|NOT-REPRODUCED|NOT-APPLICABLE|
INCONCLUSIVE> class=<class> <detail>``. Each test pins the class it measured,
so a client change shows as a failure to requalify.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import unittest
import uuid
from pathlib import Path
from typing import Any

from charmer import compiler, probe, scope, state, strict_json
from charmer.probe import ProbeError

import test_client_agents as agents
import test_client_gateway_fallbacks as fallbacks
from _layout import RESOURCES_ROOT

from charmer import catalog

_SHIPPED = catalog.load_catalog(RESOURCES_ROOT)
_FAMILY = compiler.family_default_env(
    {key: dict(entry) for key, entry in _SHIPPED.lines.items()}, dict(_SHIPPED.providers))
_OPUS_1M = _FAMILY["ANTHROPIC_DEFAULT_OPUS_MODEL"]
_PROMPT = "❯".encode("utf-8")
_WORDS = "echo watch hash source alias complete"
_WORDS_OUTPUT = "watch hash source alias complete"
# The control: the same allowed command without the shell words.
_PLAIN = "echo watch plain words"
_PLAIN_OUTPUT = "watch plain words"
# Measured on the pin (re-recorded on a re-pin): the unchanged worktree is
# removed when the agent finishes, its continuation then runs in the project
# checkout, and the isolation check refuses the shell words.
_WORKTREE_MEASURED = ("WC-removed-continued-in-project", "SW-refused-isolation")


def _command_class(block: dict[str, Any], output: str) -> str:
    """What became of one Bash call: ran, or refused (and by what, as a
    label read from the refusal; its text never leaves memory)."""

    text = agents._text_of(block.get("content")) if not isinstance(block.get("content"), str) else block["content"]
    if output in text and not block.get("is_error"):
        return "ran"
    lowered = text.lower()
    if "worktree" in lowered or "isolat" in lowered:
        return "refused-isolation"
    if "permission" in lowered or "denied" in lowered or "approval" in lowered:
        return "refused-permission"
    return "refused-other"


def _git(directory: Path, *argv: str) -> str:
    environ = {"HOME": str(directory), "PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_CONFIG_GLOBAL": "/dev/null", "LANG": "C.UTF-8"}
    return subprocess.run(["git", *argv], cwd=directory, env=environ, check=True, capture_output=True,
                          text=True, timeout=30).stdout


def _bash(tool_id: str, command: str) -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_id, "name": "Bash",
            "input": {"command": command, "description": "client check watch"}}


def _results(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for message in document.get("messages") or ():
        if isinstance(message, dict) and isinstance(message.get("content"), list):
            for block in message["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    found[str(block.get("tool_use_id"))] = block
    return found


def line(probe_id: str, status: str, observed: str, detail: str) -> str:
    return f"client check {probe_id}: {status} class={observed} {detail}".rstrip()


# --------------------------------------------------------- worktree agents


class _WorktreeResponder(agents._SpikeResponder):
    """The S1-S3 directive script with the spawn isolated in a worktree. The
    agent runs ``pwd`` on every run and, when spawned, the shell-word
    ``echo``; its working directory is kept as a label only."""

    def __init__(self, project: Path) -> None:
        super().__init__()
        self.project = str(project)
        self.cwd: dict[str, str] = {}
        self.paths: dict[str, str] = {}  # in memory only
        self.words: dict[str, str] = {}
        self._asked: set[str] = set()

    def _tool_use(self, kind: str, tag: str) -> dict[str, Any]:
        block = super()._tool_use(kind, tag)
        if kind == "SPAWN":
            block["input"]["isolation"] = "worktree"
        return block

    def _label(self, path: str) -> str:
        if path == self.project:
            return "project"
        if path.startswith(self.project + "/.claude/worktrees/"):
            return "worktree"
        return "other"

    def _respond_locked(self, document: dict[str, Any], ordinal: int):
        system = strict_json.canonical_bytes(document.get("system", "")).decode("utf-8")
        if agents._BODY_MARKER not in system:
            return super()._respond_locked(document, ordinal)
        tag = str(self.current_tag)
        results = _results(document)
        pwd_id, plain_id, words_id = (f"toolu_watch_pwd_{tag}", f"toolu_watch_plain_{tag}",
                                      f"toolu_watch_words_{tag}")
        if pwd_id not in results and pwd_id not in self._asked:
            self._asked.add(pwd_id)
            return self._reply(document, [_bash(pwd_id, "pwd")], "tool_use", ordinal)
        if pwd_id in results and tag not in self.cwd:
            block = results[pwd_id]
            text = agents._text_of(block.get("content")).strip()
            first = text.splitlines()[0].strip() if text else ""
            self.paths[tag] = first
            self.cwd[tag] = "error" if block.get("is_error") else self._label(first)
        if tag.startswith("spawn"):
            for tool_id, command, output, key in ((plain_id, _PLAIN, _PLAIN_OUTPUT, "plain"),
                                                  (words_id, _WORDS, _WORDS_OUTPUT, "words")):
                if tool_id in results:
                    self.words.setdefault(key, _command_class(results[tool_id], output))
                elif tool_id not in self._asked:
                    self._asked.add(tool_id)
                    return self._reply(document, [_bash(tool_id, command)], "tool_use", ordinal)
        return self._text(document, "CLIENTCHECK-SUBAGENT-OK", ordinal)


def worktree_classes(observed: dict[str, Any]) -> tuple[str, str]:
    """``(continuation class, shell-word class)`` from one run."""

    if observed["cwd"].get("spawn1") != "worktree":
        raise fallbacks._Inconclusive(f"the spawn did not run in a worktree ({observed})")
    if observed["words"].get("plain") != "ran":
        raise fallbacks._Inconclusive(f"the allowed control command did not run ({observed})")
    answer = observed["words"].get("words")
    if answer is None:
        raise fallbacks._Inconclusive(f"the shell-word command never answered ({observed})")
    words = "SW-ran" if answer == "ran" else f"SW-{answer}"
    resumed = observed["cwd"].get("send2")
    if observed["send"] != {"is_error": False, "success": True, "resumed_target": True} or resumed is None:
        raise fallbacks._Inconclusive(f"the continuation did not run ({observed})")
    if observed["worktree_after_spawn"]:
        continuation = "WC-kept-" + resumed
    else:
        continuation = "WC-removed-continued-in-" + resumed
    return continuation, words


class WorktreeWatchProbe(agents._SpikeRealBinaryTestCase):
    """Isolated agents: continuation after the worktree is gone, and the
    isolation check's answer to shell words."""

    probe_id = "watch-worktree"
    verdict = ("INCONCLUSIVE", "unavailable", "check did not complete")

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        if cls.boundary_skip is not None:
            cls.verdict = ("BOUNDARY", "unavailable", cls.boundary_skip)

    @classmethod
    def tearDownClass(cls) -> None:
        print(line(cls.probe_id, *cls.verdict), flush=True)
        super().tearDownClass()

    def setUp(self) -> None:
        super().setUp()
        settings = self.fixture.claude_config_dir / "settings.json"
        document = json.loads(settings.read_text(encoding="utf-8"))
        document["permissions"]["allow"] = ["Bash(pwd)", "Bash(echo:*)"]
        state.atomic_write(settings, strict_json.canonical_file_bytes(document))
        _git(self.fixture.project_dir, "init", "-q", "-b", "main")
        _git(self.fixture.project_dir, "-c", "user.name=client-check", "-c", "user.email=watch@invalid",
             "commit", "-q", "--allow-empty", "-m", "fixture")

    def test_an_isolated_agent_after_its_worktree(self) -> None:
        responder = _WorktreeResponder(self.fixture.project_dir.resolve())
        after: dict[str, Any] = {}

        def snapshot() -> None:
            listing = _git(self.fixture.project_dir, "worktree", "list", "--porcelain")
            path = responder.paths.get("spawn1", "")
            after["worktrees"] = sum(1 for row in listing.splitlines() if row.startswith("worktree "))
            after["exists"] = bool(path) and Path(path).is_dir()

        steps = [
            *self._onboarding(),
            probe.PTYInteraction(_PROMPT, b"CLIENTCHECK-SPAWN-1 go\r"),
            probe.PTYInteraction(b"CLIENTCHECK-NOTIFIED-spawn1", b"CLIENTCHECK-SEND-2 go\r", before_send=snapshot),
            probe.PTYInteraction(b"CLIENTCHECK-NOTIFIED-send2", b"/exit\r"),
        ]
        try:
            with probe.FakeAnthropicProvider(responder=responder) as provider:
                self._run_pty(self.probe_id, str(uuid.uuid4()), steps, provider)
        except (AssertionError, ProbeError) as exc:
            type(self).verdict = ("INCONCLUSIVE", "unavailable", f"the terminal run did not complete: "
                                  f"{str(exc)[:160]} cwd={responder.cwd} words={responder.words}")
            raise
        observed = {"cwd": dict(responder.cwd), "words": dict(responder.words),
                    "send": responder.send_outcomes.get("send2"), "worktree_after_spawn": after.get("exists"),
                    "worktrees_after_spawn": after.get("worktrees")}
        try:
            continuation, words = worktree_classes(observed)
        except fallbacks._Inconclusive as exc:
            type(self).verdict = ("INCONCLUSIVE", "unavailable", str(exc))
            raise
        status = ("REPRODUCED" if continuation.endswith("-project") or words != "SW-ran"
                  else "NOT-REPRODUCED")
        detail = (f"spawn cwd={observed['cwd'].get('spawn1')} control echo={observed['words'].get('plain')} "
                  f"shell-words echo={observed['words'].get('words')} worktree after the run="
                  f"{'kept' if observed['worktree_after_spawn'] else 'removed'} "
                  f"(git worktrees {observed['worktrees_after_spawn']}) continuation cwd="
                  f"{observed['cwd'].get('send2')} ({continuation}) {self.evidence_label}")
        type(self).verdict = (status, f"{continuation}+{words}", detail)
        self.assertEqual((continuation, words), _WORKTREE_MEASURED, detail)


class WorktreeVerdictTests(unittest.TestCase):
    def test_classes(self) -> None:
        base = {"cwd": {"spawn1": "worktree", "send2": "project"}, "words": {"plain": "ran", "words": "ran"},
                "send": {"is_error": False, "success": True, "resumed_target": True},
                "worktree_after_spawn": False}
        self.assertEqual(worktree_classes(base), ("WC-removed-continued-in-project", "SW-ran"))
        self.assertEqual(worktree_classes({**base, "worktree_after_spawn": True,
                                           "cwd": {"spawn1": "worktree", "send2": "worktree"}}),
                         ("WC-kept-worktree", "SW-ran"))
        self.assertEqual(worktree_classes({**base, "words": {"plain": "ran", "words": "refused-isolation"}})[1],
                         "SW-refused-isolation")
        for broken in ({"cwd": {"spawn1": "project", "send2": "project"}}, {"words": {"plain": "ran"}},
                       {"words": {"plain": "refused-permission", "words": "refused-permission"}},
                       {"send": {"is_error": True, "success": False, "resumed_target": False}}):
            with self.subTest(broken=broken), self.assertRaises(fallbacks._Inconclusive):
                worktree_classes({**base, **broken})


# ---------------------------------------------------------------- /effort


class EffortWatchProbe(fallbacks._GatedCase):
    """``/effort`` saves into the user's settings for the current model."""

    probe_id = "watch-effort"

    def _pty(self, fixture: probe.ProbeFixture, provider: probe.FakeAnthropicProvider,
             steps: list[probe.PTYInteraction], *extra: str) -> None:
        settings = self._settings(fixture, _OPUS_1M, [_OPUS_1M])
        result = probe.run_native_pty(("--settings", str(settings), "--model", _OPUS_1M, *extra), steps,
                                      trusted=self.trusted, fixture=fixture, provider=provider, timeout=150,
                                      allow_real=True, environ=self.environ)
        if result.timed_out or result.returncode != 0:
            raise fallbacks._Inconclusive(f"a terminal run did not complete (rc={result.returncode})")

    @staticmethod
    def _saved(fixture: probe.ProbeFixture) -> dict[str, Any]:
        path = fixture.claude_config_dir / "settings.json"
        document = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        model_settings = document.get("modelSettings") if isinstance(document.get("modelSettings"), dict) else {}
        return {key: value.get("effortLevel") for key, value in model_settings.items() if isinstance(value, dict)}

    def _headless(self, model_settings: dict[str, Any], *extra: str) -> str | None:
        fixture = self._fixture()
        probe.write_fixture_settings(fixture, "claude-config/settings.json", {"modelSettings": model_settings})
        provider = fallbacks._Provider()
        with provider:
            result = probe.run_native(
                ("-p", "client check watch effort", "--settings", str(self._settings(fixture, _OPUS_1M, [_OPUS_1M])),
                 "--model", _OPUS_1M, *extra),
                trusted=self.trusted, fixture=fixture, provider=provider, timeout=150, allow_real=True,
                environ=self.environ)
        if result.timed_out or result.returncode != 0:
            raise fallbacks._Inconclusive(f"a headless run did not complete (rc={result.returncode})")
        efforts = {row.get("effort") for row in fallbacks._main(provider.rows)}
        return efforts.pop() if len(efforts) == 1 else f"mixed:{sorted(map(str, efforts))}"

    def test_effort_is_saved_for_every_session(self) -> None:
        pause = lambda: time.sleep(1.5)  # noqa: E731

        def run() -> dict[str, Any]:
            observed: dict[str, Any] = {}
            fixture = self._fixture()
            provider = fallbacks._Provider()
            with provider:
                self._pty(fixture, provider, [
                    probe.PTYInteraction(b"Choose", b"2\r"), probe.PTYInteraction(b"Press", b"\r"),
                    probe.PTYInteraction(_PROMPT, b"/effort low\r", before_send=pause),
                    probe.PTYInteraction(_PROMPT, b"client check after effort\r", before_send=pause),
                    probe.PTYInteraction(b"PROBE-OK", b"", preserve_after_wait=True),
                    probe.PTYInteraction(_PROMPT, b"/exit\r", before_send=pause)], "--effort", "high")
            observed["this-session"] = [row.get("effort") for row in fallbacks._main(provider.rows)]
            observed["saved"] = self._saved(fixture)
            if not observed["saved"]:
                raise fallbacks._Inconclusive(f"/effort saved nothing to the user's settings ({observed})")
            saved = {key: {"effortLevel": "low"} for key in observed["saved"]}
            observed["launch-states-effort"] = self._headless(saved, "--effort", "high")
            observed["launch-without-effort"] = self._headless(saved)
            # A running session (stated --effort high) whose user settings get
            # the value between its two turns, as another session's /effort does.
            fixture = self._fixture()
            target = fixture.claude_config_dir / "settings.json"

            def write_and_wait() -> None:
                current = json.loads(target.read_text(encoding="utf-8")) if target.is_file() else {}
                current["modelSettings"] = saved
                state.atomic_write(target, strict_json.canonical_file_bytes(current))
                time.sleep(3.0)

            provider = fallbacks._Provider()
            with provider:
                self._pty(fixture, provider, [
                    probe.PTYInteraction(b"Choose", b"2\r"), probe.PTYInteraction(b"Press", b"\r"),
                    probe.PTYInteraction(_PROMPT, b"client check one\r", before_send=pause),
                    probe.PTYInteraction(b"PROBE-OK", b"", preserve_after_wait=True),
                    probe.PTYInteraction(_PROMPT, b"client check two\r", before_send=write_and_wait),
                    probe.PTYInteraction(b"PROBE-OK", b"", preserve_after_wait=True),
                    probe.PTYInteraction(_PROMPT, b"/exit\r", before_send=pause)], "--effort", "high")
            observed["running-session"] = [row.get("effort") for row in fallbacks._main(provider.rows)]
            return observed

        observed = self._guard(run)
        reproduced = any(value == "low" for value in observed["saved"].values())
        observed_class = ("EF-shared" if reproduced else "EF-session") + (
            "-launch-holds" if observed["launch-states-effort"] == "high" else "-launch-overridden")
        detail = (f"/effort low in a session launched --effort high: its next turn -> {observed['this-session']}, "
                  f"saved modelSettings={observed['saved']}; launch --effort high against it -> "
                  f"{observed['launch-states-effort']}; launch without --effort -> {observed['launch-without-effort']}; "
                  f"running session (--effort high) turns -> {observed['running-session']} "
                  f"client={self.gate.version} sha256={self.trusted.sha256[:12]}")
        type(self).verdict = ("REPRODUCED" if reproduced else "NOT-REPRODUCED", observed_class, detail)
        # Measured on 2.1.311: /effort writes the shared value; a launch that
        # states --effort keeps its own.
        self.assertEqual(observed_class, "EF-shared-launch-holds", detail)
        self.assertEqual(observed["this-session"], ["low"], detail)
        self.assertEqual(observed["launch-without-effort"], "low", detail)
        self.assertEqual(observed["running-session"], ["high", "high"], detail)


# ------------------------------------------------------------- scratchpad

_SCRATCH_RE = re.compile(r"(/[^\s\"'`\\]*scratchpad[^\s\"'`\\]*)")
# Measured on the pin (re-recorded on a re-pin).
_SCRATCH_MEASURED = "SP-headless:remains-terminal:remains"


class _ScratchResponder(probe.FakeAnthropicProvider):
    """The first main request (``write``): write a file into the scratchpad
    the system prompt names; then text."""

    def __init__(self, write: bool) -> None:
        super().__init__(responder=self)
        self.lock = threading.Lock()
        self.path: str | None = None
        self.written = not write

    def respond(self, document, path, context):
        if path.split("?", 1)[0].endswith("/count_tokens"):
            return 200, {"input_tokens": 1}
        text = json.dumps(document.get("system")) + json.dumps(document.get("messages"))
        with self.lock:
            if self.path is None:
                found = _SCRATCH_RE.findall(text)
                self.path = found[0].rstrip(".,:;)") if found else None
            if document.get("tools") and self.path and not self.written:
                self.written = True
                return 200, fallbacks._payload(document, [{"type": "tool_use", "id": "toolu_watch_scratch",
                                                           "name": "Write", "input": {
                                                               "file_path": f"{self.path}/watch.txt",
                                                               "content": "client check watch\n"}}],
                                               "tool_use", context.ordinal)
        return 200, fallbacks._payload(document, [{"type": "text", "text": "PROBE-OK"}], "end_turn", context.ordinal)


class ScratchpadWatchProbe(fallbacks._GatedCase):
    probe_id = "watch-scratchpad"

    def test_the_scratchpad_after_the_session(self) -> None:
        def run() -> dict[str, Any]:
            observed: dict[str, Any] = {}
            for mode in ("headless", "terminal"):
                fixture = self._fixture()
                settings = self._settings(fixture, fallbacks._LEAD, [fallbacks._LEAD])
                # The terminal session only answers (a write there would wait
                # on a permission prompt): its scratchpad is whatever the
                # client itself creates.
                provider = _ScratchResponder(write=mode == "headless")
                during: dict[str, bool] = {}

                def before_exit() -> None:
                    time.sleep(1.5)
                    during["exists"] = bool(provider.path) and Path(str(provider.path)).is_dir()

                with provider:
                    if mode == "headless":
                        result = probe.run_native(
                            ("-p", "client check watch scratchpad", "--settings", str(settings), "--model",
                             fallbacks._LEAD, "--dangerously-skip-permissions"),
                            trusted=self.trusted, fixture=fixture, provider=provider, timeout=150,
                            allow_real=True, environ=self.environ)
                        during["exists"] = provider.written
                    else:
                        pause = lambda: time.sleep(1.5)  # noqa: E731
                        result = probe.run_native_pty(
                            ("--settings", str(settings), "--model", fallbacks._LEAD),
                            [probe.PTYInteraction(b"Choose", b"2\r"), probe.PTYInteraction(b"Press", b"\r"),
                             probe.PTYInteraction(_PROMPT, b"client check watch scratchpad\r", before_send=pause),
                             probe.PTYInteraction(b"PROBE-OK", b"", preserve_after_wait=True),
                             probe.PTYInteraction(_PROMPT, b"/exit\r", before_send=before_exit)],
                            trusted=self.trusted, fixture=fixture, provider=provider, timeout=150,
                            allow_real=True, environ=self.environ)
                if result.timed_out or result.returncode != 0:
                    raise fallbacks._Inconclusive(f"{mode}: the run did not complete (rc={result.returncode})")
                named = Path(provider.path) if provider.path else None
                inside = named is not None and fixture.root in named.parents
                after = bool(inside and named.is_dir())
                observed[mode] = {
                    "named": "fixture" if inside else ("elsewhere" if named else "none"),
                    "state": ("remains" if after else "removed") if during.get("exists") else
                             ("created-after" if after else "not-created"),
                }
            return observed

        observed = self._guard(run)
        if any(row["named"] != "fixture" for row in observed.values()) or observed["headless"]["state"] not in (
                "remains", "removed"):
            type(self).verdict = ("INCONCLUSIVE", "unavailable", f"no fixture scratchpad was used ({observed})")
            raise fallbacks._Inconclusive(f"no fixture scratchpad was used ({observed})")
        remains = {mode for mode, row in observed.items() if row["state"] == "remains"}
        observed_class = "SP-" + "-".join(f"{mode}:{row['state']}" for mode, row in observed.items())
        detail = " ".join(f"{mode}={row}" for mode, row in observed.items()) + (
            f" client={self.gate.version} sha256={self.trusted.sha256[:12]}")
        type(self).verdict = ("REPRODUCED" if remains else "NOT-REPRODUCED", observed_class, detail)
        self.assertEqual(observed_class, _SCRATCH_MEASURED, detail)


# ------------------------------------------------------- large MCP results

_MCP_SERVER = "wfx"
_MCP_TOOL = f"mcp__{_MCP_SERVER}__big"
_PARTS = ("one", "two")
_PART_CHARS = 160_000


def _part_marker(part: str) -> str:
    return f"WATCH-PART-{part.upper()}"


class _BigMcpHandler(probe._ProviderHandler):
    """``/mcp/wfx``: one read-only tool whose answer is ``_PART_CHARS`` long;
    the two parallel calls are answered together."""

    def do_GET(self) -> None:  # noqa: N802
        if not self.path.startswith("/mcp/"):
            super().do_GET()
            return
        self.send_response(405)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_DELETE(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.startswith("/mcp/"):
            super().do_POST()
            return
        provider = self.server.provider  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        message = json.loads(self.rfile.read(length) or b"{}")
        method, ident = message.get("method"), message.get("id")
        if ident is None:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if method == "initialize":
            result = {"protocolVersion": message.get("params", {}).get("protocolVersion", "2025-06-18"),
                      "capabilities": {"tools": {}}, "serverInfo": {"name": _MCP_SERVER, "version": "1.0.0"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "big", "description": "client check large result",
                                 "inputSchema": {"type": "object", "properties": {"part": {"type": "string"}}},
                                 "annotations": {"readOnlyHint": True}}]}
        elif method == "tools/call":
            part = str((message.get("params", {}).get("arguments") or {}).get("part"))
            with provider.mcp_lock:
                provider.calls.append(part)
            try:
                provider.barrier.wait(15)
            except threading.BrokenBarrierError:
                pass
            marker = _part_marker(part)
            text = (marker + " ") * (_PART_CHARS // (len(marker) + 1))
            result = {"content": [{"type": "text", "text": text}]}
        else:
            result = None
        answer = {"result": result} if result is not None else {"error": {"code": -32601, "message": "unknown"}}
        data = json.dumps({"jsonrpc": "2.0", "id": ident, **answer}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


_PATH_RE = re.compile(r"(/[^\s\"'`\\)\]]+)")


class _BigMcpProvider(probe.FakeAnthropicProvider):
    def __init__(self) -> None:
        super().__init__(responder=self)
        self.mcp_lock = threading.Lock()
        self.barrier = threading.Barrier(len(_PARTS))
        self.calls: list[str] = []
        self.searched = False
        self.called = False
        self.results: dict[str, dict[str, Any]] = {}

    def start(self):
        super().start()
        self._server.RequestHandlerClass = _BigMcpHandler
        return self

    def unix_socket(self, directory):
        path = super().unix_socket(directory)
        self._unix_servers[path][0].RequestHandlerClass = _BigMcpHandler
        return path

    def respond(self, document, path, context):
        if path.split("?", 1)[0].endswith("/count_tokens"):
            return 200, {"input_tokens": 1}
        offered = {tool.get("name") for tool in document.get("tools") or [] if isinstance(tool, dict)}
        for tool_id, block in _results(document).items():
            part = tool_id.removeprefix("toolu_watch_big_")
            if part in _PARTS and part not in self.results:
                text = agents._text_of(block.get("content")) if not isinstance(block.get("content"), str) \
                    else block["content"]
                self.results[part] = {
                    "chars": len(text),
                    "own": _part_marker(part) in text,
                    "other": any(_part_marker(other) in text for other in _PARTS if other != part),
                    "paths": sorted(set(_PATH_RE.findall(text)))[:4],  # in memory only
                }
        if not self.searched and "ToolSearch" in offered and _MCP_TOOL not in offered:
            self.searched = True
            return 200, fallbacks._payload(document, [{"type": "tool_use", "id": "toolu_watch_search",
                                                       "name": "ToolSearch", "input": {"query": f"select:{_MCP_TOOL}"}}],
                                           "tool_use", context.ordinal)
        if not self.called and _MCP_TOOL in offered:
            self.called = True
            calls = [{"type": "tool_use", "id": f"toolu_watch_big_{part}", "name": _MCP_TOOL,
                      "input": {"part": part}} for part in _PARTS]
            return 200, fallbacks._payload(document, calls, "tool_use", context.ordinal)
        return 200, fallbacks._payload(document, [{"type": "text", "text": "PROBE-OK"}], "end_turn", context.ordinal)


def mcp_large_class(results: dict[str, dict[str, Any]], files: dict[str, dict[str, bool]]) -> str:
    if set(results) != set(_PARTS):
        raise fallbacks._Inconclusive(f"the two results did not both return ({sorted(results)})")
    if all(row["chars"] >= _PART_CHARS // 2 for row in results.values()):
        raise fallbacks._Inconclusive("both results stayed inline: the size limit was not reached")
    paths = [tuple(row["paths"]) for row in results.values()]
    if paths[0] and paths[0] == paths[1]:
        return "ML-same-file"
    if any(row["other"] and not row["own"] for row in results.values()):
        return "ML-other-result"
    if any(not file["own"] or file["other"] for file in files.values()):
        return "ML-other-result"
    return "ML-own-results"


class McpLargeVerdictTests(unittest.TestCase):
    def test_classes(self) -> None:
        own = {"one": {"chars": 900, "own": True, "other": False, "paths": ["/x/a"]},
               "two": {"chars": 900, "own": True, "other": False, "paths": ["/x/b"]}}
        files = {"one": {"own": True, "other": False}, "two": {"own": True, "other": False}}
        self.assertEqual(mcp_large_class(own, files), "ML-own-results")
        same = {part: {**row, "paths": ["/x/a"]} for part, row in own.items()}
        self.assertEqual(mcp_large_class(same, files), "ML-same-file")
        self.assertEqual(mcp_large_class(own, {**files, "two": {"own": False, "other": True}}), "ML-other-result")
        with self.assertRaises(fallbacks._Inconclusive):
            mcp_large_class({part: {**row, "chars": _PART_CHARS} for part, row in own.items()}, files)


class McpLargeWatchProbe(fallbacks._GatedCase):
    probe_id = "watch-mcp-large"

    def test_parallel_large_results_keep_their_own_files(self) -> None:
        def run() -> tuple[dict[str, Any], dict[str, Any], probe.ProbeFixture]:
            fixture = self._fixture()
            provider = _BigMcpProvider()
            with provider:
                config_path = fixture.claude_config_dir / ".claude.json"
                config = json.loads(state.read_private(config_path))
                config["mcpServers"] = {_MCP_SERVER: {"type": "http",
                                                      "url": f"http://127.0.0.1:{provider.port}/mcp/{_MCP_SERVER}"}}
                state.atomic_write(config_path, json.dumps(config).encode())
                result = probe.run_native(
                    ("-p", "client check watch mcp", "--settings",
                     str(self._settings(fixture, fallbacks._LEAD, [fallbacks._LEAD])), "--model", fallbacks._LEAD,
                     "--allowedTools", _MCP_TOOL),
                    trusted=self.trusted, fixture=fixture, provider=provider, timeout=150, allow_real=True,
                    environ=self.environ)
            if result.timed_out or result.returncode != 0:
                raise fallbacks._Inconclusive(f"the MCP run did not complete (rc={result.returncode})")
            files: dict[str, dict[str, bool]] = {}
            for part, row in provider.results.items():
                for named in row["paths"]:
                    candidate = Path(named)
                    if fixture.root in candidate.parents and candidate.is_file():
                        head = candidate.read_text(encoding="utf-8", errors="replace")[:4096]
                        files[part] = {"own": _part_marker(part) in head,
                                       "other": any(_part_marker(o) in head for o in _PARTS if o != part)}
                        break
            return dict(provider.results), files, {"calls": list(provider.calls)}

        results, files, calls = self._guard(run)
        detail = (" ".join(f"{part}=chars:{row['chars']},own:{row['own']},other:{row['other']},"
                           f"files:{len(row['paths'])}" for part, row in sorted(results.items()))
                  + f" saved-files={files} server-calls={calls['calls']} "
                  f"client={self.gate.version} sha256={self.trusted.sha256[:12]}")
        try:
            observed_class = mcp_large_class(results, files)
        except fallbacks._Inconclusive as exc:
            type(self).verdict = ("INCONCLUSIVE", "unavailable", f"{exc}; {detail}")
            raise
        type(self).verdict = ("NOT-REPRODUCED" if observed_class == "ML-own-results" else "REPRODUCED",
                              observed_class, detail)
        self.assertEqual(observed_class, "ML-own-results", detail)


# ---------------------------------------------------------- the Bash queue

_QUEUE_AGENTS = 8
_QUEUE_MARKERS = tuple(f"WATCH-QUEUE-{index:02d}" for index in range(_QUEUE_AGENTS))
_COMPILED_EVENTS = ("SessionStart", "UserPromptSubmit", "PreModelSwitch", "PostModelSwitch", "SubagentStart",
                    "SessionEnd")


class _QueueProvider(probe.FakeAnthropicProvider):
    """The lead runs one workflow of ``_QUEUE_AGENTS`` parallel agents; each
    agent runs ``Bash`` once (``true``); the wait for each result is kept."""

    def __init__(self) -> None:
        super().__init__(responder=self)
        self.lock = threading.Lock()
        self.forced = False
        self.sent: dict[str, float] = {}
        self.waits: dict[str, float] = {}

    def respond(self, document, path, context):
        if path.split("?", 1)[0].endswith("/count_tokens"):
            return 200, {"input_tokens": 1}
        offered = {tool.get("name") for tool in document.get("tools") or [] if isinstance(tool, dict)}
        text = json.dumps(document.get("messages"))
        found = [marker for marker in _QUEUE_MARKERS if marker in text]
        with self.lock:
            if "Workflow" in offered and not self.forced:
                self.forced = True
                calls = ", ".join(f"agent('Reply {marker}', {{label: 'q{index}'}})"
                                  for index, marker in enumerate(_QUEUE_MARKERS))
                script = ("export const meta = { name: 'client-check-watch-queue', description: 'client check' }\n"
                          f"const results = await Promise.all([{calls}])\nreturn results\n")
                return 200, fallbacks._payload(document, [{"type": "tool_use", "id": "toolu_watch_workflow",
                                                           "name": "Workflow", "input": {"script": script}}],
                                               "tool_use", context.ordinal)
            if "Workflow" not in offered and len(found) == 1:
                marker = found[0]
                tool_id = f"toolu_watch_q_{marker[-2:]}"
                if tool_id in _results(document):
                    self.waits.setdefault(marker, time.monotonic() - self.sent[marker])
                elif marker not in self.sent:
                    self.sent[marker] = time.monotonic()
                    return 200, fallbacks._payload(document, [_bash(tool_id, "true")], "tool_use", context.ordinal)
        return 200, fallbacks._payload(document, [{"type": "text", "text": "PROBE-OK"}], "end_turn", context.ordinal)


class BashQueueWatchProbe(fallbacks._GatedCase):
    probe_id = "watch-bash-queue"

    def test_parallel_workflow_agents_get_their_bash_results(self) -> None:
        def run() -> dict[str, float]:
            fixture = self._fixture()
            hook = fixture.root / "hook.sh"
            state.atomic_write(hook, b"#!/bin/sh\ncat >/dev/null\nprintf '{}\\n'\n")
            hook.chmod(0o700)
            settings = self._settings(fixture, fallbacks._LEAD, [fallbacks._LEAD])
            document = json.loads(settings.read_text(encoding="utf-8"))
            document["hooks"] = {event: [{"hooks": [{"type": "command", "command": str(hook), "timeout": 5}]}]
                                 for event in _COMPILED_EVENTS}
            state.atomic_write(settings, strict_json.canonical_file_bytes(document))
            provider = _QueueProvider()
            with provider:
                result = probe.run_native(
                    ("-p", "client check watch queue", "--settings", str(settings), "--model", fallbacks._LEAD,
                     "--dangerously-skip-permissions"),
                    trusted=self.trusted, fixture=fixture, provider=provider, timeout=420, allow_real=True,
                    environ=self.environ)
            if result.timed_out:
                raise fallbacks._Inconclusive(f"the workflow did not finish (waits so far {provider.waits})")
            if len(provider.waits) != _QUEUE_AGENTS:
                raise fallbacks._Inconclusive(f"not every agent ran its command ({len(provider.waits)} of "
                                              f"{_QUEUE_AGENTS}; rc={result.returncode})")
            return dict(provider.waits)

        waits = self._guard(run)
        longest = max(waits.values())
        observed_class = "BQ-prompt" if longest < 30 else ("BQ-queued" if longest > 120 else "BQ-slow")
        detail = (f"{_QUEUE_AGENTS} parallel workflow agents, one Bash each, hooks on {','.join(_COMPILED_EVENTS)}: "
                  f"longest wait {longest:.1f}s, median {sorted(waits.values())[len(waits) // 2]:.1f}s "
                  f"client={self.gate.version} sha256={self.trusted.sha256[:12]}")
        type(self).verdict = ("NOT-REPRODUCED" if observed_class == "BQ-prompt" else
                              "REPRODUCED" if observed_class == "BQ-queued" else "INCONCLUSIVE", observed_class, detail)
        self.assertEqual(observed_class, "BQ-prompt", detail)


# ------------------------------------------------------------- agent teams

_TEAM_TOOLS = ("TeamCreate", "TeamDelete", "SendMessage")


class _TeamsProvider(probe.FakeAnthropicProvider):
    def __init__(self) -> None:
        super().__init__(responder=self)
        self.lock = threading.Lock()
        self.agent_props: list[str] = []
        self.offered: list[str] = []
        self.searched = False
        self.found: list[str] | None = None

    def respond(self, document, path, context):
        if path.split("?", 1)[0].endswith("/count_tokens"):
            return 200, {"input_tokens": 1}
        tools = {tool.get("name"): tool for tool in document.get("tools") or [] if isinstance(tool, dict)}
        with self.lock:
            if tools and not self.offered:
                self.offered = sorted(map(str, tools))
                agent = tools.get("Agent") or {}
                self.agent_props = sorted(((agent.get("input_schema") or {}).get("properties") or {}))
            result = _results(document).get("toolu_watch_teams")
            if result is not None and self.found is None:
                content = result.get("content")
                self.found = sorted({str(part.get("tool_name")) for part in content
                                     if isinstance(part, dict) and part.get("type") == "tool_reference"}) \
                    if isinstance(content, list) else []
            if "ToolSearch" in tools and not self.searched:
                self.searched = True
                return 200, fallbacks._payload(document, [{"type": "tool_use", "id": "toolu_watch_teams",
                                                           "name": "ToolSearch",
                                                           "input": {"query": "select:" + ",".join(_TEAM_TOOLS)}}],
                                               "tool_use", context.ordinal)
        return 200, fallbacks._payload(document, [{"type": "text", "text": "PROBE-OK"}], "end_turn", context.ordinal)


class TeamsWatchProbe(fallbacks._GatedCase):
    probe_id = "watch-teams"

    def test_a_session_offers_no_agent_teams(self) -> None:
        def run() -> _TeamsProvider:
            fixture = self._fixture()
            provider = _TeamsProvider()
            with provider:
                result = probe.run_native(
                    ("-p", "client check watch teams", "--settings",
                     str(self._settings(fixture, fallbacks._LEAD, [fallbacks._LEAD])), "--model", fallbacks._LEAD),
                    trusted=self.trusted, fixture=fixture, provider=provider, timeout=150, allow_real=True,
                    environ=self.environ)
            if result.timed_out or result.returncode != 0 or provider.found is None:
                raise fallbacks._Inconclusive(f"the ToolSearch round trip did not complete (rc={result.returncode})")
            return provider

        provider = self._guard(run)
        team_tools = [name for name in provider.found if name.startswith("Team")] + \
            [name for name in provider.offered if name.startswith("Team")]
        team_params = [name for name in provider.agent_props if "team" in name.lower() or name == "name"]
        observed_class = "AT-absent" if not team_tools and not team_params else "AT-offered"
        detail = (f"Agent parameters={provider.agent_props} ToolSearch({','.join(_TEAM_TOOLS)}) found="
                  f"{provider.found} offered={provider.offered} client={self.gate.version} "
                  f"sha256={self.trusted.sha256[:12]}")
        type(self).verdict = ("NOT-APPLICABLE" if observed_class == "AT-absent" else "INCONCLUSIVE",
                              observed_class, detail)
        self.assertEqual(observed_class, "AT-absent", detail)


if __name__ == "__main__":
    unittest.main()
