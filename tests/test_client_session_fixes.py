"""Client checks for three session behaviours the pin's release notes change.

Real pinned client through the probe harness (network-hermetic, loopback
fake provider, fixture homes and projects; zero provider calls). Evidence is
metadata only: marker names, counts and exit statuses; request bodies are
read in memory for fixed synthetic markers, never kept.

* ``skill-discovery``: a skill under a subdirectory's ``.claude/skills`` and
  a ``paths:`` skill stay invocable after the session's Bash moves deeper
  into the repository (``cd``); a project-root skill is the positive control,
  and the same run without the ``cd`` the comparison.
* ``malformed-command``: user and project commands and a skill whose names
  the client cannot load sit beside a scope skill like ``/cm``: the scope
  skill still runs its launcher at expansion in a headless (``-p``) session,
  and the session still prints its answer.
* ``resume-twice``: a terminal session, a terminal ``--resume`` with one turn
  exited right after its reply, then a headless ``--resume``: the second
  resume carries the first resumed turn's reply (three trials).

Each check prints ``client check <name>: <PASS|FAIL> <detail>``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from typing import Any

from charmer import probe, scope, state, strict_json
from charmer.probe import ProbeError

import test_client_skill_workflow as skill_workflow
from _layout import RESOURCES_ROOT
from _tier import real_binary_gate

_CONTRACT_PATH = RESOURCES_ROOT / "catalog" / "native-contract.json"
_LEAD = "client-check-lead"
_PROMPT = "❯".encode("utf-8")
_LIVE_TOUCH = "live daemon domain touched"


def _payload(document: dict[str, Any], content: list[dict[str, Any]], ordinal: int) -> Any:
    stop = "tool_use" if any(block.get("type") == "tool_use" for block in content) else "end_turn"
    payload = probe._message_payload(content, model=document.get("model"), stop_reason=stop,
                                     message_id=f"msg_check_fixes_{ordinal:04d}")
    return probe._sse_message(payload) if document.get("stream") else payload


def _tool_results(document: dict[str, Any]) -> int:
    return sum(1 for message in document.get("messages") or () if isinstance(message, dict)
               for block in (message.get("content") if isinstance(message.get("content"), list) else ())
               if isinstance(block, dict) and block.get("type") == "tool_result")


class _Case(unittest.TestCase):
    trusted: probe.TrustedExecutable
    version = "unknown"
    boundary_skip: str | None = None

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        gate = real_binary_gate(_CONTRACT_PATH, what="the session checks")
        cls.boundary_skip = gate.boundary
        if gate.trusted is not None:
            cls.trusted, cls.version = gate.trusted, gate.version

    def setUp(self) -> None:
        super().setUp()
        if self.boundary_skip is not None:
            self.skipTest(self.boundary_skip)
        self.root = Path(tempfile.mkdtemp(prefix="charmer-client-fixes-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.environ = {"HOME": str(self.root / "live" / "home"), "PATH": "/usr/bin:/bin"}
        self.count = 0

    def _identity(self) -> str:
        return f"client={self.version} sha256={self.trusted.sha256[:12]}"

    def _fixture(self) -> probe.ProbeFixture:
        self.count += 1
        fixture = probe.build_fixture(self.root / f"run-{self.count:02d}", environ=self.environ)
        probe.seed_fixture_project_trust(fixture, fixture.project_dir)
        return fixture

    def _guard(self, run: Any) -> Any:
        try:
            return run()
        except ProbeError as exc:
            if _LIVE_TOUCH in str(exc):
                self.skipTest(f"BOUNDARY: {_LIVE_TOUCH}; inconclusive, never a pass ({exc})")
            raise

    def _headless(self, fixture: probe.ProbeFixture, argv: tuple[str, ...], provider: Any, **kwargs: Any
                  ) -> probe.NativeRunResult:
        result = self._guard(lambda: probe.run_native(
            argv, trusted=self.trusted, fixture=fixture, provider=provider, timeout=150, allow_real=True,
            environ=self.environ, **kwargs))
        self.assertFalse(result.timed_out)
        self.assertTrue(result.daemon is not None and result.daemon.unchanged)
        return result


# ------------------------------------------------------- skill discovery

_DISCOVERY_SKILLS = {
    # name: (path under the project, extra frontmatter, body marker)
    "check-root": (".claude/skills/check-root/SKILL.md", "", "CHECK-DISC-ROOT-BODY"),
    "check-nested": ("pkg/.claude/skills/check-nested/SKILL.md", "", "CHECK-DISC-NESTED-BODY"),
    "check-paths": (".claude/skills/check-paths/SKILL.md", "paths:\n  - \"pkg/**\"\n", "CHECK-DISC-PATHS-BODY"),
}


class _DiscoveryResponder:
    """Main-thread script by the number of tool results so far: Read a file
    under ``pkg/``, run the Bash ``move`` command, call each fixture skill,
    then end the turn."""

    def __init__(self, move: str, read: Path, *, move_first: bool = False) -> None:
        steps = [("Read", {"file_path": str(read)}), ("Bash", {"command": move, "description": "move"})]
        self.steps = [*(steps[::-1] if move_first else steps),
                      *(("Skill", {"skill": name}) for name in _DISCOVERY_SKILLS)]
        self.lock = threading.Lock()
        self.ordinal = 0

    def respond(self, document: dict[str, Any], path: str, context: Any) -> Any:
        if path.split("?", 1)[0].endswith("/count_tokens"):
            return 200, {"input_tokens": 1}
        with self.lock:
            self.ordinal += 1
            ordinal = self.ordinal
        step = _tool_results(document)
        if step < len(self.steps) and context.record.tool_count:
            name, tool_input = self.steps[step]
            return 200, _payload(document, [{"type": "tool_use", "id": f"toolu_check_disc_{step}", "name": name,
                                             "input": tool_input}], ordinal)
        return 200, _payload(document, [{"type": "text", "text": "PROBE-OK"}], ordinal)


class SkillDiscoveryAfterCdProbe(_Case):
    """Directory-scoped and ``paths:`` skills after the Bash moves deeper."""

    def _invocable(self, move: str, *, move_first: bool = False) -> list[str]:
        fixture = self._fixture()
        project = fixture.project_dir
        for directory in ("pkg/inner/deeper",):
            (project / directory).mkdir(parents=True, exist_ok=True)
        (project / "pkg" / "file.txt").write_text("fixture\n", encoding="utf-8")
        for name, (relative, extra, marker) in _DISCOVERY_SKILLS.items():
            target = project / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"---\nname: {name}\ndescription: client check discovery fixture {name}\n{extra}---\n"
                              f"{marker} Reply PROBE-OK.\n", encoding="utf-8")
        env = {"HOME": str(fixture.home), "PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_CONFIG_GLOBAL": "/dev/null", "LANG": "C.UTF-8"}
        for command in (["git", "init", "-q", "-b", "main"], ["git", "add", "-A"],
                        ["git", "-c", "user.name=check", "-c", "user.email=check@invalid", "commit", "-q", "-m", "f"]):
            subprocess.run(command, cwd=project, env=env, check=True, capture_output=True, timeout=30)
        settings = probe.write_fixture_settings(fixture, "scope/settings.json", {
            "permissions": {"defaultMode": scope.PERMISSION_DEFAULT_MODE}})
        markers = {name: marker for name, (_r, _e, marker) in _DISCOVERY_SKILLS.items()}
        responder = _DiscoveryResponder(move, project / "pkg" / "file.txt", move_first=move_first)
        with probe.FakeAnthropicProvider(responder=responder, content_markers=markers) as provider:
            result = self._headless(fixture, ("-p", "client check discovery", "--model", _LEAD, "--settings",
                                              str(settings), "--dangerously-skip-permissions"), provider)
            main = [record for record in provider.requests if record.tool_count]
        self.assertEqual(result.returncode, 0)
        last = main[-1].markers_found if main else ()
        return sorted(name for name in _DISCOVERY_SKILLS if name in last)

    def test_scoped_skills_stay_invocable_after_cd(self) -> None:
        stay = self._invocable("true")
        deeper = self._invocable("cd pkg/inner/deeper")
        # The cd first, then the file under pkg/ is read (the path-scoped
        # skill's globs must match the file, not the shell's directory).
        moved_first = self._invocable("cd pkg/inner/deeper", move_first=True)
        everything = sorted(_DISCOVERY_SKILLS)
        ok = stay == deeper == moved_first == everything
        print(f"client check skill-discovery: {'PASS' if ok else 'FAIL'} invocable without cd={stay} "
              f"after cd pkg/inner/deeper={deeper} cd-then-read={moved_first} {self._identity()}", flush=True)
        self.assertIn("check-root", stay)
        self.assertEqual((stay, deeper, moved_first), (everything,) * 3)


# ----------------------------------------------------- malformed command

# Names the client cannot load as commands or skills (whitespace, a
# leading dash, a doubled dot, a colon run, an invalid skill name).
_MALFORMED_COMMANDS = ("bad name.md", "-dash.md", "a..b.md", "x::y.md")
_MALFORMED_SKILL = "Bad Skill!"


class MalformedCommandProbe(_Case):
    """An unrelated unloadable command never hides a scope skill like
    ``/cm`` or a headless session's answer."""

    def _run(self, malformed: bool) -> dict[str, Any]:
        fixture = self._fixture()
        scope_dir = state.ensure_private_dir(fixture.root / "scope")
        skills = state.ensure_private_dir(scope_dir / ".claude" / "skills")
        directory = state.ensure_private_dir(skills / "cmq")
        state.atomic_write(directory / "SKILL.md", (
            skill_workflow._SKILL_FRONTMATTER.format(name="cmq", marker=skill_workflow._SKILL_DESC_MARKER)
            + skill_workflow._SKILL_BODIES["cmq"]).encode("utf-8"))
        if malformed:
            for commands in (fixture.claude_config_dir / "commands", fixture.project_dir / ".claude" / "commands"):
                commands.mkdir(parents=True, exist_ok=True)
                for name in _MALFORMED_COMMANDS:
                    (commands / name).write_text("---\ndescription: unloadable\n---\nunloadable command\n",
                                                 encoding="utf-8")
            bad = fixture.project_dir / ".claude" / "skills" / _MALFORMED_SKILL
            bad.mkdir(parents=True, exist_ok=True)
            (bad / "SKILL.md").write_text(f"---\nname: {_MALFORMED_SKILL}\ndescription: x\n---\nx\n", encoding="utf-8")
        bin_dir = state.ensure_private_dir(fixture.root / "bin")
        record = fixture.root / "launcher-argv.jsonl"
        probe.write_fixture_executable(fixture, bin_dir, "charmer", (
            f"#!{sys.executable}\nimport json, sys\n"
            f"open({str(record)!r}, 'a', encoding='utf-8').write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "print('CHECK-LINEUP-OUT-0001')\n"))
        settings = probe.write_fixture_settings(fixture, "scope-settings.json", {
            "permissions": {"defaultMode": scope.PERMISSION_DEFAULT_MODE}})
        responder = skill_workflow._SkillResponder()
        with probe.FakeAnthropicProvider(responder=responder) as provider:
            result = self._headless(fixture, ("-p", "/cmq show", "--model", _LEAD, "--settings", str(settings),
                                              "--add-dir", str(scope_dir), "--output-format", "json"),
                                    provider, path_dirs=(bin_dir,))
        calls = len(record.read_text(encoding="utf-8").splitlines()) if record.exists() else 0
        try:
            answer = json.loads(result.stdout or "{}").get("result")
        except ValueError:
            answer = None
        return {"rc": result.returncode, "launcher_calls": calls, "answered": answer == "CHECK-ACK-0001"}

    def test_an_unloadable_command_hides_nothing(self) -> None:
        control, malformed = self._run(False), self._run(True)
        ok = control == malformed == {"rc": 0, "launcher_calls": 1, "answered": True}
        print(f"client check malformed-command: {'PASS' if ok else 'FAIL'} control={control} "
              f"with-unloadable-commands={malformed} {self._identity()}", flush=True)
        self.assertEqual(control, {"rc": 0, "launcher_calls": 1, "answered": True})
        self.assertEqual(malformed, control)


# ---------------------------------------------------------- resume twice

_TURNS = {"CHECK-RESUME-TURN-ONE": "CHECK-REPLY-ONE", "CHECK-RESUME-TURN-TWO": "CHECK-REPLY-TWO",
          "CHECK-RESUME-TURN-THREE": "CHECK-REPLY-THREE"}


class _TurnResponder:
    """Answers each turn with its fixed reply marker (by the last prompt)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.ordinal = 0

    def respond(self, document: dict[str, Any], path: str, context: Any) -> Any:
        if path.split("?", 1)[0].endswith("/count_tokens"):
            return 200, {"input_tokens": 1}
        with self.lock:
            self.ordinal += 1
            ordinal = self.ordinal
        messages = document.get("messages") or []
        # The current user turn: every message after the last assistant one.
        start = max((index + 1 for index, message in enumerate(messages)
                     if isinstance(message, dict) and message.get("role") == "assistant"), default=0)
        last = json.dumps(messages[start:])
        reply = next((answer for turn, answer in _TURNS.items() if turn in last), "PROBE-OK")
        return 200, _payload(document, [{"type": "text", "text": reply}], ordinal)


class ResumeTwiceProbe(_Case):
    """Terminal session -> terminal resume with one turn -> resume again."""

    def _trial(self) -> tuple[str, ...]:
        fixture = self._fixture()
        session = str(uuid.uuid4())
        responder = _TurnResponder()
        # The managed permission default (2.1.284+ otherwise starts in auto mode).
        settings = ("--settings", str(probe.write_fixture_settings(fixture, "scope/settings.json", {
            "permissions": {"defaultMode": scope.PERMISSION_DEFAULT_MODE}})))
        step = probe.PTYInteraction
        runs = (
            (("--session-id", session), (step(b"Choose", b"2\r"), step(b"Press", b"\r"),
                                         step(_PROMPT, b"CHECK-RESUME-TURN-ONE\r"),
                                         step(b"CHECK-REPLY-ONE", b"/exit\r"))),
            (("--resume", session), (step(_PROMPT, b"CHECK-RESUME-TURN-TWO\r"),
                                     step(b"CHECK-REPLY-TWO", b"/exit\r"))),
        )
        markers = {"one": "CHECK-REPLY-ONE", "two": "CHECK-REPLY-TWO"}
        with probe.FakeAnthropicProvider(responder=responder, content_markers=markers) as provider:
            for argv, steps in runs:
                result = self._guard(lambda argv=argv, steps=steps: probe.run_native_pty(
                    ("--model", _LEAD, *settings, *argv), steps, trusted=self.trusted, fixture=fixture,
                    provider=provider, timeout=120, allow_real=True, environ=self.environ))
                self.assertFalse(result.timed_out)
                self.assertEqual(result.returncode, 0)
            before = len(provider.requests)
            result = self._headless(fixture, ("-p", "CHECK-RESUME-TURN-THREE", "--resume", session, "--model", _LEAD,
                                              *settings), provider)
            self.assertEqual(result.returncode, 0)
            third = [record for record in provider.requests[before:] if record.tool_count]
        self.assertTrue(third, "the second resume sent no turn")
        return tuple(sorted(third[0].markers_found))

    def test_the_first_resumed_reply_survives_the_next_resume(self) -> None:
        trials = [self._trial() for _ in range(3)]
        ok = all(trial == ("one", "two") for trial in trials)
        print(f"client check resume-twice: {'PASS' if ok else 'FAIL'} replies carried by the second resume per "
              f"trial={trials} {self._identity()}", flush=True)
        self.assertEqual(trials, [("one", "two")] * 3)
