"""Client check background-service: a managed session never reaches Claude
Code's background service.

``/background`` (``/bg``) and the exit dialog's "Move to background and
exit" hand a session to Claude Code's background service: its own daemon
under ``/tmp/cc-daemon-<uid>/``, its own binary and environment, without the
lead prompt or charmer's launch environment. The compiled flag settings turn
the client's agent view off (``CLAUDE_CODE_DISABLE_AGENT_VIEW``: "agent
view, ``--bg``, /background, the on-demand daemon"). With it off the client
registers no ``/background`` and offers the fork-subagent ``/fork
<directive>`` instead of the fleet ``/fork`` (the compiled fork deny covers
that one).

Every run is the production compile of the balanced seed in a terminal, with
the launch's lead-prompt file, inside a namespace whose ``/tmp`` is a private
folder of this test (no daemon or job folder of this machine is visible; a
daemon the client starts lands there and dies with the namespace). Outside
such a namespace the check reports BOUNDARY and never runs.

* ``compiled``: a first turn starts a background shell (``sleep``);
  ``/background`` leaves the session where it is, ``/fork`` shows what the
  client offers instead (recorded), and ``/exit`` asks about the background
  work without a "Move to background and exit" choice; no daemon folder
  appears. ``--agents-view-if-allowed`` passed through keeps the compiled
  session (its lead on the wire) and starts no daemon either.
* ``control``: the compile without the switch, the same first turn:
  ``/exit`` offers "Move to background and exit"; ``Stay``, then
  ``/background`` hands the session over and a daemon folder appears.

The line: ``client check background-service: <STATUS> class=<class>
<detail>``; the screen excerpts kept are the client's own short messages.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from typing import Any, Iterator
from unittest import mock

from charmer import probe, scope, state
from charmer.probe import ProbeError

import test_client_agents as agents
import test_client_models as models
from _catalog import uses_shipped_catalog
from _layout import RESOURCES_ROOT
from _tier import real_binary_gate

PASS_CLASS = "BG-closed"
_CONTRACT = RESOURCES_ROOT / "catalog" / "native-contract.json"
_PROMPT = "❯".encode()
_MOVE = "Move to background and exit"
_FORK_REFUSAL = re.compile(r"Can.t fork: [^.]{0,240}\.")


@contextlib.contextmanager
def private_tmp_folder(folder: Path) -> Iterator[Path]:
    """Real runs started inside the block see ``folder`` as their ``/tmp``,
    with this test run's own work root (its HOME and temporary folder) bound
    back; anything the client writes under ``/tmp`` stays in ``folder``."""

    work = os.path.realpath(os.path.commonpath([os.environ.get("HOME", "/"), tempfile.gettempdir()]))
    if work in ("/", "/tmp") or not os.path.isdir(work):
        raise unittest.SkipTest("BOUNDARY: no private work root to keep under a private /tmp")
    original = probe._isolation_prefix

    def prefix(bwrap: Path, cwd: Path | str | None, *, pid_namespace: bool = False) -> list[str]:
        argv = original(bwrap, cwd, pid_namespace=pid_namespace)
        at = argv.index("--dev-bind") + 3
        return [*argv[:at], "--bind", str(folder), "/tmp", "--bind", work, work, *argv[at:]]

    with mock.patch.object(probe, "_isolation_prefix", prefix):
        yield folder


def daemon_folders(folder: Path) -> list[str]:
    return sorted(entry.name for entry in folder.iterdir() if entry.name.startswith("cc-daemon-"))


class _Responder:
    """PROBE-OK for every request; the first main request of the session gets
    one background shell call (``sleep``)."""

    def __init__(self) -> None:
        self.started = False
        self._lock = threading.Lock()

    def respond(self, document: dict[str, Any], path: str, context: Any) -> Any:
        if path.split("?", 1)[0].rstrip("/").endswith("/count_tokens"):
            return 200, {"input_tokens": 1}
        record = context.record
        with self._lock:
            start = not self.started and "Bash" in record.tool_names
            self.started = self.started or start
        message_id = f"msg_bgcheck_{uuid.uuid4().hex[:8]}"
        if start:
            content = [{"type": "tool_use", "id": "toolu_bgcheck_0", "name": "Bash",
                        "input": {"command": "sleep 120", "description": "client check background",
                                  "run_in_background": True}}]
            payload = probe._message_payload(content, model=document.get("model"), stop_reason="tool_use",
                                             message_id=message_id)
        else:
            payload = probe._message_payload([{"type": "text", "text": "PROBE-OK"}], model=document.get("model"),
                                             stop_reason="end_turn", message_id=message_id)
        return 200, probe._sse_message(payload) if document.get("stream") else payload


def background_class(compiled: dict[str, Any], control: dict[str, Any]) -> str:
    """``BG-closed`` when the control reaches the background service (the
    dialog offers it and ``/background`` starts a daemon) and the compile
    with the switch does neither, ``/background`` and ``/fork`` included (a
    later client whose ``/fork`` starts a daemon turns this red). Raises when
    the control does not reach it."""

    if not control["offered"] or not control["daemon"]:
        raise AssertionError(f"the control never reached the background service: {control}")
    if compiled["daemon"]:
        return "BG-daemon-started"
    if compiled["offered"]:
        return "BG-dialog-offers-background"
    if not compiled["stayed"]:
        return "BG-session-left"
    return PASS_CLASS


@uses_shipped_catalog
class BackgroundServiceProbe(unittest.TestCase):
    probe_id = "background-service"
    verdict: tuple[str, str, str] = ("INCONCLUSIVE", "unavailable", "check did not complete")

    @classmethod
    def setUpClass(cls) -> None:
        cls.gate = real_binary_gate(_CONTRACT, what="the background-service check")
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
        self.root = Path(tempfile.mkdtemp(prefix="charmer-background-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.environ = {"HOME": str(self.root / "live" / "home"), "PATH": "/usr/bin:/bin"}
        self.count = 0

    def _scope(self, *, switch: bool) -> tuple[probe.ProbeFixture, dict[str, Any], Path, Path]:
        self.count += 1
        fixture = probe.build_fixture(self.root / f"run-{self.count:02d}", environ=self.environ)
        probe.seed_fixture_project_trust(fixture, fixture.project_dir)
        # The first turn's background shell runs without a question.
        state.atomic_write(fixture.claude_config_dir / "settings.json", json.dumps(
            {"permissions": {"allow": ["Bash(sleep 120)"]}}).encode("utf-8"))
        managed = models.compiled_managed_scope(fixture, "balanced")
        path = Path(managed["settings"])
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("env", {}).get(scope.AGENT_VIEW_ENV) != "1":
            raise AssertionError("the compiled scope lacks the agent-view switch")
        if not switch:
            document["env"].pop(scope.AGENT_VIEW_ENV)
        state.atomic_write(path, json.dumps(document, sort_keys=True).encode("utf-8"))
        lead = state.ensure_private_dir(fixture.root / "lead") / "lead.md"
        state.atomic_write(lead, b"You are the lead of a client check session.\n")
        tmp = state.ensure_private_dir(fixture.root / "private-tmp")
        return fixture, managed, tmp, lead

    def _run(self, fixture, managed, tmp: Path, lead: Path, steps) -> tuple[str, bool]:
        argv = ("--settings", str(managed["settings"]), "--model", managed["lead"], "--effort", managed["effort"],
                "--add-dir", str(managed["live"]), "--append-system-prompt-file", str(lead))
        provider = models._BetaRecordingProvider(responder=_Responder())
        exited = True
        with provider, private_tmp_folder(tmp):
            try:
                result = probe.run_native_pty(argv, steps, trusted=self.trusted, fixture=fixture, provider=provider,
                                              timeout=150, allow_real=True, environ=self.environ,
                                              child_env=dict(models._HINT_ENV), pid_namespace=True)
                screen = agents._screen_text(result.stdout)
            except probe.PTYTimeoutError as exc:
                if exc.phase != "client-exit":
                    raise AssertionError(f"the session did not reach its last step: {exc}") from exc
                screen, exited = "", False
            except ProbeError as exc:
                if models._TRIPWIRE in str(exc):
                    type(self).verdict = ("BOUNDARY", "unavailable", "live daemon-domain tripwire hit")
                    self.skipTest("BOUNDARY: live daemon-domain tripwire hit")
                raise
        return screen, exited

    def _compiled(self) -> dict[str, Any]:
        fixture, managed, tmp, lead = self._scope(switch=True)
        steps = [probe.PTYInteraction(b"Choose", b"2\r"), probe.PTYInteraction(b"Press", b"\r"),
                 probe.PTYInteraction(_PROMPT, b"client check background one\r"),
                 probe.PTYInteraction(b"PROBE-OK", b"", preserve_after_wait=True),
                 probe.PTYInteraction(_PROMPT, b"/background\r"),
                 probe.PTYInteraction(_PROMPT, b"/fork\r"),
                 probe.PTYInteraction(_PROMPT, b"/exit\r"),
                 probe.PTYInteraction(b"Exit and stop tasks", b"\r")]
        screen, exited = self._run(fixture, managed, tmp, lead, steps)
        refusal = _FORK_REFUSAL.search(screen)
        return {"offered": _MOVE in screen, "daemon": daemon_folders(tmp), "stayed": exited,
                "fork_said": refusal.group(0) if refusal else _excerpt(screen, "/fork"),
                "background_said": _excerpt(screen, "/background")}

    def _control(self) -> dict[str, Any]:
        fixture, managed, tmp, lead = self._scope(switch=False)
        # Escape answers the exit dialog with "Stay"; the dialog's choices are
        # read from the screen afterwards.
        steps = [probe.PTYInteraction(b"Choose", b"2\r"), probe.PTYInteraction(b"Press", b"\r"),
                 probe.PTYInteraction(_PROMPT, b"client check background one\r"),
                 probe.PTYInteraction(b"PROBE-OK", b"", preserve_after_wait=True),
                 probe.PTYInteraction(_PROMPT, b"/exit\r"),
                 probe.PTYInteraction(b"Exit and stop tasks", b"\x1b", preserve_after_wait=True),
                 probe.PTYInteraction(_PROMPT, b"/background\r")]
        screen, exited = self._run(fixture, managed, tmp, lead, steps)
        return {"offered": _MOVE in screen, "daemon": daemon_folders(tmp), "exited": exited,
                "background_said": _excerpt(screen, "/background")}

    def test_a_managed_session_never_reaches_the_background_service(self) -> None:
        try:
            compiled = self._compiled()
            print(f"client check {self.probe_id} case compiled {compiled}", flush=True)
            control = self._control()
            print(f"client check {self.probe_id} case control {control}", flush=True)
            observed = background_class(compiled, control)
        except AssertionError as exc:
            type(self).verdict = ("INCONCLUSIVE", "unavailable", str(exc))
            raise
        detail = (f"compiled: dialog-offers-background={compiled['offered']} daemon={compiled['daemon'] or 'none'} "
                  f"background={compiled['background_said']!r} fork={compiled['fork_said']!r}; control: "
                  f"dialog-offers-background={control['offered']} daemon={control['daemon']} "
                  f"client={self.gate.version} sha256={self.trusted.sha256[:12]}")
        type(self).verdict = ("PASS" if observed == PASS_CLASS else "FAIL", observed, detail)
        self.assertEqual(observed, PASS_CLASS, detail)


    def test_the_agents_view_flag_keeps_the_managed_launch(self) -> None:
        """``--agents-view-if-allowed`` (a hidden flag a user can pass
        through): the session is still the compiled one (its lead model on
        the wire, its flag settings in force) and no daemon folder appears."""

        fixture, managed, tmp, lead = self._scope(switch=True)
        responder = models._SpikeResponder()
        provider = models._BetaRecordingProvider(responder=responder)
        argv = ("-p", "client check agents view", "--settings", str(managed["settings"]), "--model",
                managed["lead"], "--effort", managed["effort"], "--add-dir", str(managed["live"]),
                "--append-system-prompt-file", str(lead), "--agents-view-if-allowed")
        with provider, private_tmp_folder(tmp):
            try:
                result = probe.run_native(argv, trusted=self.trusted, fixture=fixture, provider=provider, timeout=150,
                                          allow_real=True, environ=self.environ, child_env=dict(models._HINT_ENV))
            except ProbeError as exc:
                if models._TRIPWIRE in str(exc):
                    self.skipTest("BOUNDARY: live daemon-domain tripwire hit")
                raise
        main = [row for row in responder.rows if row["tool_count"]]
        self.assertEqual(result.returncode, 0)
        self.assertTrue(main)
        self.assertEqual({row["model"] for row in main}, {models._wire(managed["lead"])})
        self.assertEqual(daemon_folders(tmp), [])
        print(f"client check agents-view-flag: PASS lead={main[0]['model']} daemon=none "
              f"client={self.gate.version} sha256={self.trusted.sha256[:12]}", flush=True)


def _excerpt(screen: str, after: str, size: int = 160) -> str:
    at = screen.rfind(after)
    return screen[at:at + size] if at >= 0 else ""


class VerdictTests(unittest.TestCase):
    def test_the_class_needs_the_control_and_every_closed_door(self) -> None:
        closed = {"offered": False, "daemon": [], "stayed": True}
        control = {"offered": True, "daemon": ["cc-daemon-1000"]}
        self.assertEqual(background_class(closed, control), PASS_CLASS)
        for change, expected in (({"daemon": ["cc-daemon-1000"]}, "BG-daemon-started"),
                                 ({"offered": True}, "BG-dialog-offers-background"),
                                 ({"stayed": False}, "BG-session-left")):
            with self.subTest(expected):
                self.assertEqual(background_class({**closed, **change}, control), expected)
        for broken in ({**control, "offered": False}, {**control, "daemon": []}):
            with self.assertRaises(AssertionError):
                background_class(closed, broken)

    def test_the_private_tmp_folder_replaces_tmp(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bg-tmp-") as folder:
            with private_tmp_folder(Path(folder)):
                argv = probe._isolation_prefix(Path("/usr/bin/bwrap"), None)
            at = argv.index("--dev-bind")
            self.assertEqual(argv[at + 3:at + 6], ["--bind", folder, "/tmp"])
            self.assertEqual(daemon_folders(Path(folder)), [])


if __name__ == "__main__":
    unittest.main()
