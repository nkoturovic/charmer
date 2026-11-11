"""Offline PTY measurements of hook messages and agent bind/unbind reloads.

Only the runner-staged pin and the hash-verified previous client run. The
probe harness supplies a private HOME and network namespace with a fake
provider; daemon observations are confined to a synthetic fixture path.
No transcripts are read. Terminal output and request bodies stay in memory;
only marker presence, hook counts and reload counters leave the fixture.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from charmer import probe, scope, state, strict_json

from _client_roles import PREVIOUS, PREVIOUS_ENV, named_control
from _layout import RESOURCES_ROOT
from _tier import real_binary_gate
import test_client_agents as agents
import test_client_hooks as hooks


PLATFORMS = ("linux",)
_PROMPT = "❯".encode()
_SYSTEM = "PROPCHECK-SYSTEM-MESSAGE"
_CONTEXT = "PROPCHECK-MODEL-CONTEXT"
_AGENT = "cm-propagation-fixture"
_HOOK = """\
import json, sys
config = json.load(open(sys.argv[1], encoding="utf-8"))
event = json.load(sys.stdin)
with open(config["events"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps({key: event.get(key) for key in (
        "hook_event_name", "source", "from_model", "to_model")}) + "\\n")
print(json.dumps({"systemMessage": config["system"], "hookSpecificOutput": {
    "hookEventName": event["hook_event_name"],
    "additionalContext": config["context"]}}))
"""


class _PropagationFixture(unittest.TestCase):
    previous = False

    @classmethod
    def setUpClass(cls) -> None:
        gate = real_binary_gate(
            RESOURCES_ROOT / "catalog" / "native-contract.json",
            what="the propagation client measurements",
        )
        if gate.boundary:
            raise unittest.SkipTest(gate.boundary)
        cls.trusted = gate.trusted
        version = gate.version
        if cls.previous:
            cls.trusted = named_control(PREVIOUS_ENV)
            version = PREVIOUS[0]
            if cls.trusted is None:
                raise unittest.SkipTest(f"BOUNDARY: {PREVIOUS_ENV} is not supplied")
        assert cls.trusted is not None
        cls.identity = f"client={version} sha256={cls.trusted.sha256[:12]}"

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="charmer-client-propagation-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root)
        self.environ = {"HOME": str(self.root / "ambient"), "PATH": "/usr/bin:/bin"}
        self.fixture = probe.build_fixture(self.root / "fixture", environ=self.environ)
        probe.seed_fixture_project_trust(self.fixture, self.fixture.project_dir)
        self.scope = state.ensure_private_dir(self.fixture.root / "scope")
        self.agents_dir = state.ensure_private_dir(self.scope / ".claude" / "agents")
        probe.seed_fixture_project_trust(self.fixture, self.scope)
        self.events = self.fixture.root / "hook-events.jsonl"

    def _settings(self, event: str | None = None) -> Path:
        document = {
            **hooks._S6_FENCE,
            "permissions": {"defaultMode": scope.PERMISSION_DEFAULT_MODE},
        }
        if event:
            config = self.fixture.root / "hook-config.json"
            config.write_text(json.dumps({
                "events": str(self.events), "system": _SYSTEM, "context": _CONTEXT,
            }), encoding="utf-8")
            script = self.fixture.root / "message-hook.py"
            script.write_text(_HOOK, encoding="utf-8")
            command = " ".join(shlex.quote(str(value)) for value in (
                sys.executable, script, config,
            ))
            document["hooks"] = {event: [{"hooks": [{
                "type": "command", "command": command, "timeout": 5,
            }]}]}
        path = self.fixture.root / "flag-settings.json"
        state.atomic_write(path, strict_json.canonical_file_bytes(document))
        return path

    def _run(self, settings, steps, provider):
        result = probe.run_native_pty(
            ("--model", hooks._OPUS, "--settings", str(settings),
             "--add-dir", str(self.scope)),
            [probe.PTYInteraction(b"Choose", b"2\r"),
             probe.PTYInteraction(b"Press", b"\r"), *steps],
            trusted=self.trusted, fixture=self.fixture, provider=provider,
            timeout=90, allow_real=True, environ=self.environ,
            live_daemon_domain=self.fixture.root / "synthetic-daemon-domain",
            rows=40, columns=160,
        )
        self.assertEqual(result.returncode, 0)
        self.assertFalse(result.timed_out)
        self.assertIsNotNone(result.daemon)
        self.assertTrue(result.daemon.unchanged)
        return result

    def _event_rows(self):
        return [json.loads(line) for line in self.events.read_text().splitlines()]

    def _postmodel(self, *, after_prompt=False):
        settings = self._settings("PostModelSwitch")
        steps = [
            probe.PTYInteraction(_PROMPT, b"/model\r", before_send=hooks._pause(0.5)),
            probe.PTYInteraction(b"Select model", b"", preserve_after_wait=True),
            probe.PTYInteraction(b"cancel", hooks._UP, before_send=hooks._pause(0.4)),
            probe.PTYInteraction(b"CMPROBESONNET", b"s", before_send=hooks._pause(0.4)),
            probe.PTYInteraction(_PROMPT, b"/exit\r", before_send=hooks._pause(1.5)),
        ]
        if after_prompt:
            steps[-1:] = [
                probe.PTYInteraction(_PROMPT, b"PROPCHECK-AFTER-SWITCH\r",
                                     before_send=hooks._pause(1.5)),
                probe.PTYInteraction(b"PROBE-OK", b"/exit\r",
                                     before_send=hooks._pause(0.5)),
            ]
        with probe.FakeAnthropicProvider(
            responder=hooks._sse_responder, content_markers={"context": _CONTEXT},
        ) as provider:
            result = self._run(settings, steps, provider)
        if after_prompt:
            requests = hooks._lead_requests(result.requests)
            self.assertEqual(len(requests), 1)
            self.assertIn("context", requests[0].markers_found)
        self.assertEqual(self._event_rows(), [{
            "hook_event_name": "PostModelSwitch", "source": "picker",
            "from_model": hooks._OPUS, "to_model": hooks._SONNET,
        }])
        screen = agents._screen_text(result.stdout)
        self.assertFalse(_CONTEXT in screen, "model-only context appeared on screen")
        # The switch hook runs immediately, but its systemMessage is only
        # drawn when the user submits a prompt, not when the picker closes.
        self.assertEqual(_SYSTEM in screen, after_prompt,
                         "PostModelSwitch message visibility changed")
        print(f"client check S6P: PASS systemMessage-visible={after_prompt} "
              f"hook-count=1 source=picker after-next-prompt={after_prompt} {self.identity}")

    def _prompt_message(self):
        settings = self._settings("UserPromptSubmit")
        steps = [
            probe.PTYInteraction(_PROMPT, b"PROPCHECK-USER-TURN\r"),
            probe.PTYInteraction(b"PROBE-OK", b"/exit\r", before_send=hooks._pause(0.5)),
        ]
        with probe.FakeAnthropicProvider(
            responder=hooks._sse_responder,
            content_markers={"context": _CONTEXT, "system": _SYSTEM},
        ) as provider:
            result = self._run(settings, steps, provider)
        self.assertEqual([row["hook_event_name"] for row in self._event_rows()],
                         ["UserPromptSubmit"])
        requests = hooks._lead_requests(result.requests)
        self.assertEqual(len(requests), 1)
        self.assertIn("context", requests[0].markers_found)
        screen = agents._screen_text(result.stdout)
        self.assertFalse(_CONTEXT in screen, "model-only context appeared on screen")
        self.assertTrue(_SYSTEM in screen, "UserPromptSubmit message was not rendered")
        print(f"client check S5M: PASS systemMessage-visible=True "
              f"additionalContext-on-wire=True hook-count=1 {self.identity}")

    def _reload(self, *, bind):
        settings = self._settings()
        path = self.agents_dir / f"{_AGENT}.md"
        content = (f"---\nname: {_AGENT}\ndescription: Synthetic propagation fixture.\n"
                   f"model: {hooks._SONNET}\n---\nReturn the fixture marker.\n").encode()
        if not bind:
            state.atomic_write(path, content)
        def change():
            if bind:
                state.atomic_write(path, content)
            else:
                path.unlink()

        steps = [
            probe.PTYInteraction(_PROMPT, b"/reload-plugins\r"),
            probe.PTYInteraction(b"Reloaded", b"PROPCHECK-BEFORE-RELOAD\r"),
            probe.PTYInteraction(b"PROBE-OK", b"/reload-plugins\r", before_send=change),
            probe.PTYInteraction(_PROMPT, b"PROPCHECK-AFTER-RELOAD\r",
                                 before_send=hooks._pause(1.0)),
            probe.PTYInteraction(b"PROBE-OK", b"/exit\r", before_send=hooks._pause(0.5)),
        ]
        with probe.FakeAnthropicProvider(responder=hooks._sse_responder) as provider:
            result = self._run(settings, steps, provider)
        screen = agents._screen_text(result.stdout)
        forced = "--force" in screen
        # The agent list is not part of the Agent tool schema. Compare the
        # client-rendered reload census instead; collapse terminal redraws.
        counts = []
        for match in agents._RELOAD_RE.finditer(screen):
            count = int(match.group(3))
            if not counts or counts[-1] != count:
                counts.append(count)
        self.assertTrue(counts, "no baseline reload outcome observed")
        self.assertEqual(len(hooks._lead_requests(result.requests)), 2)
        self.assertFalse(forced, "agent-only reload asked for --force")
        self.assertEqual(len(counts), 2, "changed reload census was not rendered")
        self.assertEqual(counts[1] - counts[0], 1 if bind else -1)
        action = "bind" if bind else "unbind"
        print(f"client check propagation-reload: PASS action={action} "
              f"force-requested={forced} agent-counts={counts} {self.identity}")


class CandidatePropagationClientTests(_PropagationFixture):
    def test_postmodel_system_message(self):
        self._postmodel()

    def test_postmodel_system_message_after_prompt(self):
        self._postmodel(after_prompt=True)

    def test_prompt_system_message(self):
        self._prompt_message()

    def test_reload_after_bind(self):
        self._reload(bind=True)

    def test_reload_after_unbind(self):
        self._reload(bind=False)


class PreviousPropagationClientTests(CandidatePropagationClientTests):
    previous = True
