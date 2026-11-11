"""Client check U11: what makes a running client use an edited agent file.

The S1 setup of ``tests/test_client_agents.py`` (one synthetic ``cm-spike-x``
agent in an ``--add-dir`` scope, the loopback fake provider, the fixture
fence), with the agent file changed four ways while the session runs:

* ``in-place``: the same file rewritten (same inode);
* ``atomic``: a new file renamed over it (charmer's own scope writes);
* ``directory``: a new agents folder renamed over the old one (a scope swap);
* ``rapid``: three writes within a few milliseconds, the last one the edit.

Each way, in one terminal session: a spawn on the old definition, the edit
and a settle, a spawn without any reload, ``/reload-plugins``, a spawn, a
SendMessage to the first agent (a resumed agent) and its completion, exit
(no background work left to ask about); then a process resume of the same
session and a spawn. Gated: after ``/reload-plugins`` and after a process
resume a spawn carries the edit. Measured and printed for unit P: what a
spawn without a reload and a resumed agent carry. The line:
``client check agent-reload: <STATUS> <detail>``. Metadata only (models and
definition labels).
"""

from __future__ import annotations

import os
import time
import unittest

from charmer import probe, state
from charmer.probe import ProbeError

import test_client_agents as agents

_SETTLE_SECONDS = 3.0
_WAYS = ("in-place", "atomic", "directory", "rapid")


class AgentFileReloadTests(agents._SpikeRealBinaryTestCase):
    def _edit(self, way: str) -> None:
        body = agents._AGENT_TEMPLATE.format(model=agents._MODEL_B, body=agents._BODY_MARKER,
                                             definition=agents._DEFINITION_MARKERS["B"])
        target = self.agents_dir / f"{agents._AGENT_TYPE}.md"
        if way == "in-place":
            with open(target, "r+", encoding="utf-8") as handle:
                handle.seek(0)
                handle.write(body)
                handle.truncate()
        elif way == "atomic":
            state.atomic_write(target, body.encode("utf-8"))
        elif way == "directory":
            fresh = state.ensure_private_dir(self.agents_dir.parent / "agents.new")
            state.atomic_write(fresh / target.name, body.encode("utf-8"))
            os.rename(self.agents_dir, self.agents_dir.parent / "agents.old")
            os.rename(fresh, self.agents_dir)
        else:
            old = agents._AGENT_TEMPLATE.format(model=agents._MODEL_A, body=agents._BODY_MARKER,
                                                definition=agents._DEFINITION_MARKERS["A"])
            for text in (body, old, body):
                state.atomic_write(target, text.encode("utf-8"))
        time.sleep(_SETTLE_SECONDS)

    def _run(self, argv: tuple[str, ...], steps: list[probe.PTYInteraction],
             provider: probe.FakeAnthropicProvider) -> probe.NativeRunResult:
        try:
            result = probe.run_native_pty(
                (*argv, "--model", agents._LEAD, "--add-dir", str(self.scope)), steps,
                trusted=self.trusted, fixture=self.fixture, provider=provider, timeout=150,
                allow_real=True, environ=self.environ)
        except ProbeError as exc:
            self._boundary_if_live_domain("agent-reload", exc)
            raise
        self.assertFalse(result.timed_out)
        self.assertEqual(result.returncode, 0)
        assert result.daemon is not None
        self.assertTrue(result.daemon.unchanged)
        return result

    def _way(self, way: str, index: int) -> dict[str, str]:
        self._prepare_fixture(f"fixture-{way}")
        session = f"34343434-0011-4011-8011-{index:012d}"
        responder = agents._SpikeResponder()
        prompt = agents._PROMPT_GLYPH
        steps = [
            *self._onboarding(),
            probe.PTYInteraction(prompt, b"CLIENTCHECK-SPAWN-1 go\r"),
            probe.PTYInteraction(b"CLIENTCHECK-NOTIFIED-spawn1", b"CLIENTCHECK-SPAWN-2 go\r",
                                 before_send=lambda: self._edit(way)),
            probe.PTYInteraction(b"CLIENTCHECK-NOTIFIED-spawn2", b"/reload-plugins\r"),
            probe.PTYInteraction(b"Reloaded", b"CLIENTCHECK-SPAWN-3 go\r"),
            probe.PTYInteraction(b"CLIENTCHECK-NOTIFIED-spawn3", b"CLIENTCHECK-SEND-4 go\r"),
            # The SendMessage result (DONE) comes back while the resumed agent
            # still runs in the background; /exit then would open the client's
            # exit dialog about that work, which nothing answers. Leave after
            # its completion notification, as the S lanes do.
            probe.PTYInteraction(b"CLIENTCHECK-DONE-send4", b"", preserve_after_wait=True),
            probe.PTYInteraction(b"CLIENTCHECK-NOTIFIED-send4", b"/exit\r"),
        ]
        with probe.FakeAnthropicProvider(responder=responder) as provider:
            self._run(("--session-id", session), steps, provider)
        # The process resume (headless, as S2's relaunch): a new client
        # process reads the agent folder at its start.
        resumed = agents._SpikeResponder()
        with probe.FakeAnthropicProvider(responder=resumed) as provider:
            try:
                result = probe.run_native(
                    ("-p", "CLIENTCHECK-SPAWN-5 go", "--resume", session, "--model", agents._LEAD,
                     "--add-dir", str(self.scope)),
                    trusted=self.trusted, fixture=self.fixture, provider=provider, timeout=120,
                    allow_real=True, environ=self.environ)
            except ProbeError as exc:
                self._boundary_if_live_domain("agent-reload", exc)
                raise
        self.assertFalse(result.timed_out)
        self.assertEqual(result.returncode, 0)

        def label(calls) -> str:
            return "+".join(sorted({f"{call.model}/{call.definition}" for call in calls})) or "none"

        return {"spawn": label(responder.calls_for("spawn1")),
                "no-reload": label(responder.calls_for("spawn2")),
                "reload": label(responder.calls_for("spawn3")),
                "resumed-agent": label(responder.calls_for("send4")),
                "process-resume": label(resumed.calls_for("spawn5"))}

    def test_only_a_reload_or_a_new_process_uses_the_edit(self) -> None:
        observed = {way: self._way(way, index) for index, way in enumerate(_WAYS, start=1)}
        old, new = f"{agents._MODEL_A}/A", f"{agents._MODEL_B}/B"
        detail = " ".join(f"{way}: " + ",".join(f"{key}={value}" for key, value in row.items())
                          for way, row in observed.items())
        for way, row in observed.items():
            with self.subTest(way=way):
                self.assertEqual(row["spawn"], old)
                self.assertEqual(row["reload"], new)
                self.assertEqual(row["process-resume"], new)
        print(f"client check agent-reload: PASS {self.evidence_label} {detail}", flush=True)


if __name__ == "__main__":
    unittest.main()
