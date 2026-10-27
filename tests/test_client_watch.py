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
* ``watch-mcp-large``: four parallel calls of one MCP tool whose results are
  each too large to stay inline, in three trials: whether each call gets its
  own saved result. A trial counts only when every call returned
  successfully and each result names one saved file inside the fixture, read
  for the markers.
* ``watch-bash-queue``: many workflow agents at once, each running one
  ``Bash`` command, in a fixture-only managed session whose compiled
  lifecycle hooks run charmer's shim and launcher (every launcher run is
  logged and must succeed): how long each command waits for its result.
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
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from typing import Any

from charmer import catalog, cli, compiler, probe, profile, scope, state, strict_json
from charmer import settings as settings_mod
from charmer.probe import ProbeError

import _v3
import test_client_agents as agents
import test_client_gateway_fallbacks as fallbacks
from _catalog import FIXTURE_ROOT, uses_shipped_catalog
from _layout import REPO_ROOT, RESOURCES_ROOT

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
# Four parallel calls per trial: with two, results share a saved file in
# about one trial of ten on both 2.1.310 and 2.1.311; with four, in nearly
# every trial (measured over 20 trials each).
_PARTS = ("one", "two", "three", "four")
_PART_CHARS = 160_000
_MCP_TRIALS = 3
# Measured on the pin (re-recorded on a re-pin): parallel results are handed
# one saved file (2.1.310 the same).
_MCP_MEASURED = "ML-same-file"


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
# The most paths kept from one result's text (in memory only).
_PATHS_KEPT = 16
# How much of a saved file is read for the markers.
_SAVED_HEAD = 4096


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
                    "error": bool(block.get("is_error")),
                    "paths": sorted(set(_PATH_RE.findall(text)))[:_PATHS_KEPT],  # in memory only
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


def saved_files(results: dict[str, dict[str, Any]], root: Path) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    """The saved file each result names, validated: a path that resolves
    inside the fixture ``root`` to a regular, readable file. A part gets an
    entry only when its result names exactly one such file: its markers
    (``own``, ``other``) and its resolved path (``file``, in memory only, for
    comparison). Also returns how many validated files each result named."""

    base = Path(os.path.realpath(root))
    files: dict[str, dict[str, Any]] = {}
    counts: dict[str, int] = {}
    for part, row in results.items():
        found: dict[str, str] = {}
        for named in row.get("paths") or ():
            resolved = Path(os.path.realpath(named))
            if base not in resolved.parents or not resolved.is_file():
                continue
            try:
                with resolved.open("r", encoding="utf-8", errors="replace") as handle:
                    found[str(resolved)] = handle.read(_SAVED_HEAD)
            except OSError:
                continue
        counts[part] = len(found)
        if len(found) == 1:
            (path, head), = found.items()
            files[part] = {"own": _part_marker(part) in head,
                           "other": any(_part_marker(o) in head for o in _PARTS if o != part), "file": path}
    return files, counts


def mcp_large_class(results: dict[str, dict[str, Any]], files: dict[str, dict[str, Any]]) -> str:
    """One trial: every call must have returned successfully and been saved
    (each result naming one validated file, :func:`saved_files`) before a
    class is chosen; missing, failed or unreadable evidence is inconclusive."""

    if set(results) != set(_PARTS):
        raise fallbacks._Inconclusive(f"not every result returned ({sorted(results)} of {list(_PARTS)})")
    failed = sorted(part for part, row in results.items() if row.get("error"))
    if failed:
        raise fallbacks._Inconclusive(f"the call(s) {failed} returned an error result")
    inline = sorted(part for part, row in results.items() if row["chars"] >= _PART_CHARS // 2)
    if inline:
        raise fallbacks._Inconclusive(f"the result(s) {inline} stayed inline: the size limit was not reached")
    unsaved = sorted(set(_PARTS) - set(files))
    if unsaved:
        raise fallbacks._Inconclusive(f"no validated saved file for {unsaved}")
    # Every saved file must hold some call's answer before a class is chosen:
    # an unmarked file is missing evidence, never a collision.
    unmarked = sorted(part for part, file in files.items() if not (file["own"] or file["other"]))
    if unmarked:
        raise fallbacks._Inconclusive(f"the saved file for {unmarked} holds no call's answer")
    if len({file["file"] for file in files.values()}) < len(_PARTS):
        return "ML-same-file"
    if any(row["other"] and not row["own"] for row in results.values()):
        return "ML-other-result"
    if any(file["other"] for file in files.values()):
        return "ML-other-result"
    return "ML-own-results"


def mcp_large_verdict(trials: list[str]) -> str:
    """Over the trials: the first class other than own results, else own
    results (every trial's evidence already validated)."""

    if len(trials) != _MCP_TRIALS:
        raise fallbacks._Inconclusive(f"{len(trials)} of {_MCP_TRIALS} trials completed")
    return next((observed for observed in trials if observed != "ML-own-results"), "ML-own-results")


class McpLargeVerdictTests(unittest.TestCase):
    OWN = {part: {"chars": 900, "own": False, "other": False, "error": False, "paths": [f"/x/{part}"]}
           for part in _PARTS}
    FILES = {part: {"own": True, "other": False, "file": f"/x/{part}"} for part in _PARTS}

    def test_classes(self) -> None:
        own, files = self.OWN, self.FILES
        self.assertEqual(mcp_large_class(own, files), "ML-own-results")
        shared = {**files, "two": {"own": False, "other": True, "file": "/x/one"}}
        self.assertEqual(mcp_large_class(own, shared), "ML-same-file")
        crossed = {**files, "two": {"own": False, "other": True, "file": "/x/two"}}
        self.assertEqual(mcp_large_class(own, crossed), "ML-other-result")
        with self.assertRaises(fallbacks._Inconclusive):
            mcp_large_class({part: {**row, "chars": _PART_CHARS} for part, row in own.items()}, files)

    def test_a_shared_unmarked_file_is_inconclusive(self) -> None:
        unmarked = {part: {"own": False, "other": False, "file": "/x/one"} for part in _PARTS}
        with self.assertRaises(fallbacks._Inconclusive):
            mcp_large_class(self.OWN, unmarked)

    def test_a_collision_beside_an_unmarked_file_is_inconclusive(self) -> None:
        mixed = {**self.FILES, "two": {"own": False, "other": True, "file": "/x/one"},
                 "three": {"own": False, "other": False, "file": "/x/three"}}
        with self.assertRaises(fallbacks._Inconclusive):
            mcp_large_class(self.OWN, mixed)

    def test_trials(self) -> None:
        self.assertEqual(mcp_large_verdict(["ML-own-results"] * _MCP_TRIALS), "ML-own-results")
        self.assertEqual(mcp_large_verdict(["ML-own-results"] * (_MCP_TRIALS - 1) + ["ML-same-file"]),
                         "ML-same-file")
        with self.assertRaises(fallbacks._Inconclusive):
            mcp_large_verdict(["ML-own-results"] * (_MCP_TRIALS - 1))

    def test_short_error_results_without_files_are_inconclusive(self) -> None:
        errors = {part: {"chars": 40, "own": False, "other": False, "error": True, "paths": []} for part in _PARTS}
        with self.assertRaises(fallbacks._Inconclusive):
            mcp_large_class(errors, {})

    def test_named_files_that_do_not_exist_are_inconclusive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            named = {part: {**row, "paths": [str(root / f"missing-{part}.txt")]} for part, row in self.OWN.items()}
            files, counts = saved_files(named, root)
            self.assertEqual((files, counts), ({}, {part: 0 for part in _PARTS}))
            with self.assertRaises(fallbacks._Inconclusive):
                mcp_large_class(named, files)

    def test_one_validated_file_is_inconclusive(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            (root / "one.txt").write_text(_part_marker("one") + " saved", encoding="utf-8")
            # Every other result names a file outside the fixture (by a link
            # inside it): never read, never counted.
            named = {"one": {**self.OWN["one"], "paths": [str(root / "one.txt")]}}
            for part in _PARTS[1:]:
                (Path(outside) / f"{part}.txt").write_text(_part_marker(part) + " saved", encoding="utf-8")
                (root / f"{part}.txt").symlink_to(Path(outside) / f"{part}.txt")
                named[part] = {**self.OWN[part], "paths": [str(root / f"{part}.txt")]}
            files, counts = saved_files(named, root)
            self.assertEqual(sorted(files), ["one"])
            self.assertEqual(counts, {"one": 1, **{part: 0 for part in _PARTS[1:]}})
            with self.assertRaises(fallbacks._Inconclusive):
                mcp_large_class(named, files)

    def test_saved_files_carry_their_markers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for part in _PARTS:
                (root / f"{part}.txt").write_text(_part_marker(part) + " saved", encoding="utf-8")
            named = {part: {**row, "paths": [str(root), str(root / f"{part}.txt")]} for part, row in self.OWN.items()}
            files, counts = saved_files(named, root)
            self.assertEqual(counts, {part: 1 for part in _PARTS})
            self.assertEqual(mcp_large_class(named, files), "ML-own-results")
            (root / "two.txt").write_text("nothing recognisable", encoding="utf-8")
            with self.assertRaises(fallbacks._Inconclusive):
                mcp_large_class(named, saved_files(named, root)[0])
            # Two results naming one file that holds one call's answer.
            named["two"] = {**named["two"], "paths": [str(root / "one.txt")]}
            self.assertEqual(mcp_large_class(named, saved_files(named, root)[0]), "ML-same-file")


class McpLargeWatchProbe(fallbacks._GatedCase):
    probe_id = "watch-mcp-large"

    def test_parallel_large_results_and_their_saved_files(self) -> None:
        def trial() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
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
            files, counts = saved_files(provider.results, fixture.root)
            return dict(provider.results), files, {"calls": sorted(provider.calls), "validated": counts}

        rows: list[str] = []
        trials: list[str] = []
        try:
            for index in range(_MCP_TRIALS):
                results, files, calls = self._guard(trial)
                distinct = len({file["file"] for file in files.values()})
                rows.append(f"trial{index + 1}=[" + " ".join(
                    f"{part}:chars={row['chars']},error={row['error']},validated={calls['validated'].get(part, 0)},"
                    f"own-file={files[part]['own'] if part in files else None},"
                    f"other-file={files[part]['other'] if part in files else None}"
                    for part, row in sorted(results.items())) + f" distinct-files={distinct}]")
                trials.append(mcp_large_class(results, files))
            observed_class = mcp_large_verdict(trials)
        except fallbacks._Inconclusive as exc:
            type(self).verdict = ("INCONCLUSIVE", "unavailable", f"{exc}; {' '.join(rows)}")
            raise
        shared = sum(observed != "ML-own-results" for observed in trials)
        detail = (f"{len(_PARTS)} parallel calls per trial: classes {trials} (results sharing or crossing a file in "
                  f"{shared} of {_MCP_TRIALS} trials) {' '.join(rows)} "
                  f"client={self.gate.version} sha256={self.trusted.sha256[:12]}")
        type(self).verdict = ("NOT-REPRODUCED" if observed_class == "ML-own-results" else "REPRODUCED",
                              observed_class, detail)
        self.assertEqual(observed_class, _MCP_MEASURED, detail)


# ---------------------------------------------------------- the Bash queue

_QUEUE_AGENTS = 8
_QUEUE_MARKERS = tuple(f"WATCH-QUEUE-{index:02d}" for index in range(_QUEUE_AGENTS))
_COMPILED_EVENTS = ("SessionStart", "UserPromptSubmit", "PreModelSwitch", "PostModelSwitch", "SubagentStart",
                    "SessionEnd")


class _QueueProvider(probe.FakeAnthropicProvider):
    """The lead runs one workflow of ``_QUEUE_AGENTS`` parallel agents; each
    agent runs ``Bash`` once (``true``); the wait for each result is kept, and
    whether the lead's request carried the session-start lineup notice."""

    def __init__(self) -> None:
        super().__init__(responder=self)
        self.lock = threading.Lock()
        self.forced = False
        self.notice = False
        self.sent: dict[str, float] = {}
        self.waits: dict[str, float] = {}

    def respond(self, document, path, context):
        if path.split("?", 1)[0].endswith("/count_tokens"):
            return 200, {"input_tokens": 1}
        offered = {tool.get("name") for tool in document.get("tools") or [] if isinstance(tool, dict)}
        text = json.dumps(document.get("messages"))
        found = [marker for marker in _QUEUE_MARKERS if marker in text]
        with self.lock:
            if "Workflow" in offered and _QUEUE_NOTICE in text:
                self.notice = True
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


# charmer's launcher as the compiled hooks reach it: the fixture-built
# protocol-3 shim (under the fixture's own state root) runs this wrapper,
# which runs the checkout's launcher on the fixture state and appends the
# event, its exit status and its duration (ms) to a fixture log.
_HOOK_WRAPPER = """#!/bin/sh
CHARMER_HOOK_COMMAND={wrapper}
export CHARMER_HOOK_COMMAND
PYTHONPATH={src}
export PYTHONPATH
started=$(date +%s%N)
{python} {launcher} "$@"
status=$?
ended=$(date +%s%N)
printf '%s %s %s\\n' "${{2-}}" "$status" "$(( (ended - started) / 1000000 ))" >> {log}
exit "$status"
"""
_QUEUE_GENERATION = 4243
# The head of the lineup notice the SessionStart hook gives the lead.
_QUEUE_NOTICE = f"lineup_generation {_QUEUE_GENERATION}]"


def managed_hook_scope(fixture: probe.ProbeFixture) -> dict[str, Any]:
    """A fixture-only managed session for the compiled lifecycle hooks: the
    v3 record (built from the package catalog the hook's launcher loads) and
    the v2 scope (managed compile of the fixture seed ``balanced``) under the
    fixture's state root, the protocol-3 shim there, and the compiled hooks
    naming it (``session-event`` with the managed id, launch epoch and hook
    protocol). Nothing outside the fixture is read or written."""

    state.ensure_private_dir(fixture.xdg_state_home)
    state_root = state.ensure_private_dir(fixture.xdg_state_home / "charmer")
    log = fixture.root / "launcher-events.log"
    wrapper = fixture.root / "charmer-wrapper"
    wrapper.write_text(_HOOK_WRAPPER.format(
        log=shlex.quote(str(log)), wrapper=shlex.quote(str(wrapper.resolve())),
        src=shlex.quote(str(REPO_ROOT / "src")), python=shlex.quote(sys.executable),
        launcher=shlex.quote(str(REPO_ROOT / "bin" / "charmer"))), encoding="utf-8")
    wrapper.chmod(0o700)
    resolved = str(wrapper.resolve())
    shim = scope.hook_shim_v3_path(state_root)
    state.ensure_private_dir(shim.parent)
    state.atomic_write(shim, scope.hook_shim_v3_text(state_root, resolved))
    shim.chmod(0o700)
    environ = fixture.environ()
    environ["CHARMER_HOOK_COMMAND"] = resolved
    runtime = cli.Runtime(asset_root=RESOURCES_ROOT, environ=environ, cwd=fixture.project_dir,
                          allow_state_writes=False)
    if runtime.session_store.root != state_root:
        raise fallbacks._Inconclusive("the hook's state root is not the fixture's")
    managed = str(uuid.uuid4())
    document = _v3.v1_projection(runtime.catalog.seed_profiles["balanced"],
                                 _v3.v1_models_view(runtime.catalog.docs)["models"])
    record = _v3.make_record(
        managed_id=managed, cwd=str(fixture.project_dir), composition_name=document["name"],
        snapshot=_v3.snapshot_for(runtime.catalog.docs, document), catalog_version=runtime.catalog_version,
        catalog_hash=runtime.catalog.bundle_sha256, launcher_version=runtime.launcher_version, mode="durable",
        scope_generation=1, launch_epoch=1)
    _v3.save_any(runtime.session_store, record)
    cat = catalog.load_catalog(FIXTURE_ROOT)
    lcat = profile.LineupCatalog.from_catalog(cat)
    eff = settings_mod.effective({"version": 1}, provider_ids=cat.providers, line_keys=cat.lines)
    lineup = profile.resolve(cat.seed_profiles["balanced"], lcat, effective=eff)
    plan = scope.compile_lineup_scope(
        lineup, lcat, eff, cat.prompt_bodies, scope.catalog_meta_v2(cat.docs), lineup_generation=_QUEUE_GENERATION,
        managed_id=managed, hook_command=str(shim), launch_epoch=1)
    scope.write_scope(state_root, managed, plan)
    lifecycle_env = {key: value for key, value in plan.settings["env"].items() if key.startswith("CHARMER_")}
    return {"managed": managed, "hooks": plan.settings["hooks"], "env": lifecycle_env, "log": log,
            "shim": shim, "shim_bytes": shim.read_bytes(), "model": record["snapshot"]["lead"]["client_selector"]}


def launcher_runs(log: Path) -> list[tuple[str, int, int]]:
    """``(event, exit status, milliseconds)`` per launcher run the shim made."""

    runs = []
    text = log.read_text(encoding="utf-8") if log.exists() else ""
    for row in text.splitlines():
        parts = row.split()
        if len(parts) == 3 and parts[1].lstrip("-").isdigit() and parts[2].lstrip("-").isdigit():
            runs.append((parts[0], int(parts[1]), int(parts[2])))
    return runs


def queue_class(waits: dict[str, float], runs: list[tuple[str, int, int]], notice: bool) -> str:
    """The Bash queue, counted only when charmer's hooks provably ran: the
    session's start and end and one ``SubagentStart`` per workflow agent went
    through the launcher, each with exit status 0, the start hook's lineup
    notice reached the lead, and every agent got its command's result."""

    events = [event for event, _status, _ms in runs]
    if "start" not in events or "end" not in events:
        raise fallbacks._Inconclusive(f"the compiled session hooks did not run through the launcher ({events})")
    if not notice:
        raise fallbacks._Inconclusive("the lead never saw the session-start lineup notice")
    if events.count("subagent") < _QUEUE_AGENTS:
        raise fallbacks._Inconclusive(f"SubagentStart reached the launcher {events.count('subagent')} times "
                                      f"for {_QUEUE_AGENTS} agents")
    failed = sorted({event for event, status, _ms in runs if status != 0})
    if failed:
        raise fallbacks._Inconclusive(f"the launcher failed the hook event(s) {failed}")
    if len(waits) != _QUEUE_AGENTS:
        raise fallbacks._Inconclusive(f"not every agent ran its command ({len(waits)} of {_QUEUE_AGENTS})")
    longest = max(waits.values())
    return "BQ-prompt" if longest < 30 else ("BQ-queued" if longest > 120 else "BQ-slow")


class QueueVerdictTests(unittest.TestCase):
    RUNS = [("start", 0, 400), *[("subagent", 0, 300)] * _QUEUE_AGENTS, ("end", 0, 200)]
    WAITS = {marker: 0.2 for marker in _QUEUE_MARKERS}

    def test_classes(self) -> None:
        self.assertEqual(queue_class(self.WAITS, self.RUNS, True), "BQ-prompt")
        self.assertEqual(queue_class({**self.WAITS, _QUEUE_MARKERS[0]: 480.0}, self.RUNS, True), "BQ-queued")
        self.assertEqual(queue_class({**self.WAITS, _QUEUE_MARKERS[0]: 60.0}, self.RUNS, True), "BQ-slow")

    def test_the_hooks_must_have_run(self) -> None:
        for runs in ([], [run for run in self.RUNS if run[0] != "start"], [run for run in self.RUNS if run[0] != "end"],
                     [run for run in self.RUNS if run[0] != "subagent"] + [("subagent", 0, 300)],
                     [*self.RUNS[:-1], ("end", 1, 200)]):
            with self.subTest(runs=[run[:2] for run in runs]), self.assertRaises(fallbacks._Inconclusive):
                queue_class(self.WAITS, runs, True)
        with self.assertRaises(fallbacks._Inconclusive):
            queue_class(self.WAITS, self.RUNS, False)

    def test_every_agent_must_have_its_result(self) -> None:
        with self.assertRaises(fallbacks._Inconclusive):
            queue_class(dict(list(self.WAITS.items())[:-1]), self.RUNS, True)

    def test_the_launcher_log_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "log"
            self.assertEqual(launcher_runs(log), [])
            log.write_text("start 0 412\nsubagent 0 300\ngarbage\nend 1 20\n", encoding="utf-8")
            self.assertEqual(launcher_runs(log), [("start", 0, 412), ("subagent", 0, 300), ("end", 1, 20)])


@uses_shipped_catalog
class BashQueueWatchProbe(fallbacks._GatedCase):
    """#105012 with charmer's hooks installed: a fixture-only managed session
    (record and scope under the fixture's state root), the compiled lifecycle
    hooks running the fixture-built shim and the checkout's launcher."""

    probe_id = "watch-bash-queue"

    def test_parallel_workflow_agents_get_their_bash_results(self) -> None:
        def run() -> tuple[dict[str, float], list[tuple[str, int, int]], bool]:
            fixture = self._fixture()
            managed = managed_hook_scope(fixture)
            lead = managed["model"]
            settings = self._settings(fixture, lead, [lead])
            document = json.loads(settings.read_text(encoding="utf-8"))
            document["hooks"] = managed["hooks"]
            document["env"] = {**document["env"], **managed["env"]}
            state.atomic_write(settings, strict_json.canonical_file_bytes(document))
            if sorted(managed["hooks"]) != sorted(_COMPILED_EVENTS):
                raise fallbacks._Inconclusive(f"the compile names other hook events ({sorted(managed['hooks'])})")
            provider = _QueueProvider()
            with provider:
                result = probe.run_native(
                    ("-p", "client check watch queue", "--settings", str(settings), "--model", lead,
                     "--session-id", managed["managed"], "--dangerously-skip-permissions"),
                    trusted=self.trusted, fixture=fixture, provider=provider, timeout=420, allow_real=True,
                    environ=self.environ)
            if result.timed_out:
                raise fallbacks._Inconclusive(f"the workflow did not finish (waits so far {provider.waits})")
            if managed["shim"].read_bytes() != managed["shim_bytes"]:
                raise fallbacks._Inconclusive("the hook shim changed during the run")
            return dict(provider.waits), launcher_runs(managed["log"]), provider.notice

        waits, runs, notice = self._guard(run)
        events = [event for event, _status, _ms in runs]
        counts = {event: events.count(event) for event in sorted(set(events))}
        slowest = max((ms for _event, _status, ms in runs), default=0)
        values = sorted(waits.values())
        detail = (f"{_QUEUE_AGENTS} parallel workflow agents, one Bash each, under charmer's compiled hooks on "
                  f"{','.join(_COMPILED_EVENTS)} (fixture managed scope and record, fixture-built shim, the "
                  f"checkout's launcher): launcher runs {counts} statuses {sorted({s for _e, s, _m in runs})} "
                  f"slowest hook {slowest / 1000:.1f}s, lead notice {'seen' if notice else 'absent'}; "
                  + (f"longest wait {values[-1]:.1f}s, median {values[len(values) // 2]:.1f}s " if values else "")
                  + f"client={self.gate.version} sha256={self.trusted.sha256[:12]}")
        try:
            observed_class = queue_class(waits, runs, notice)
        except fallbacks._Inconclusive as exc:
            type(self).verdict = ("INCONCLUSIVE", "unavailable", f"{exc}; {detail}")
            raise
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
