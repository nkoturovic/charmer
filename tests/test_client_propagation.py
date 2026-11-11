"""Offline PTY measurements of hook messages and agent bind/unbind reloads.

Only the runner-staged pin and the hash-verified previous client run. The
probe harness supplies a private HOME and network namespace with a fake
provider; daemon observations are confined to a synthetic fixture path.
No transcripts are read. Terminal output and request bodies stay in memory;
only marker presence, hook counts and reload counters leave the fixture.
"""

from __future__ import annotations

import io
import json
import os
import shlex
import shutil
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

from charmer import probe, scope, state, strict_json
from charmer import hooks as notice_hooks, lineup as lineup_mod

from _client_roles import PREVIOUS, PREVIOUS_ENV, named_control
from _layout import REPO_ROOT, RESOURCES_ROOT
from _catalog import FIXTURE_ROOT
import _tui_fixture as fx
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


_MANAGED = "45454545-4545-4545-8545-454545454545"
_PRODUCT_HOOK = """\
import io, json, os, sys, types
from pathlib import Path
config = json.load(open(sys.argv[1], encoding="utf-8"))
sys.path.insert(0, config["source"])
from charmer import catalog, hooks, profile, sessions
from charmer.cli import session_events
payload = hooks.read_payload(sys.stdin)
event = {"SessionStart": "start", "UserPromptSubmit": "prompt", "PostModelSwitch": "postmodel"}[payload["hook_event_name"]]
args = types.SimpleNamespace(managed_id=config["managed_id"], launch_epoch=config["epoch"],
                             event=event, hook_protocol=3)
class Output:
    def __init__(self):
        self.parts = []
    def write(self, text):
        self.parts.append(text)
        return sys.stdout.write(text)
    def flush(self):
        sys.stdout.flush()
output = Output()
input_stream = io.StringIO(json.dumps(payload))
client_env = {**os.environ, "CHARMER_ASSETS": config["client_assets"]}
if payload["hook_event_name"] == "SessionStart":
    cat = catalog.load_catalog(Path(config["assets"]))
    runtime = types.SimpleNamespace(session_store=sessions.SessionStore(config["state"], sessions.default_schema()),
                                    catalog=cat, lineup_catalog=lambda: profile.LineupCatalog.from_catalog(cat))
    session_events._session_start_v3(args, state_path=Path(config["state"]),
        runtime_factory=lambda: runtime, input_stream=input_stream, output_stream=output,
        error_stream=sys.stderr, environ=client_env)
else:
    hooks.dispatch(args, state_root=config["state"], environ=client_env,
        input_stream=input_stream, output_stream=output, error_stream=sys.stderr)
document = json.loads("".join(output.parts)) if output.parts else {}
context = document.get("hookSpecificOutput", {}).get("additionalContext", "")
with open(config["events"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"event": payload["hook_event_name"], "source": payload.get("source"),
        "user_notice": bool(document.get("systemMessage")),
        "conditional": "was applied while this session was running" in context,
        "staged": "staged — active after /reload-plugins" in context}) + "\\n")
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
        cls.client_version = version
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

    def _run(self, settings, steps, provider, *, extra=()):
        result = probe.run_native_pty(
            ("--model", hooks._OPUS, "--settings", str(settings),
             "--add-dir", str(self.scope), *extra),
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


    def _product_fixture(self):
        runtime = fx.fixture_runtime(self.fixture.root / "product")
        with fx.hermetic(runtime):
            document = runtime.profiles.load("balanced")
            document.pop("seed", None)
            document["name"] = "notice"
            runtime.profiles.save(document)
            record = fx.v4_record(runtime, "notice", managed_id=_MANAGED)
        self.scope = scope.scope_dir(runtime.session_store.root, _MANAGED)
        probe.seed_fixture_project_trust(self.fixture, self.scope)
        # The hook's version-only warning reader sees the pin of this
        # fixture's client role. Binary admission is still the runner's
        # packaged-pin or hash-verified named-control gate, never this input.
        client_assets = self.fixture.root / "client-warning-assets"
        (client_assets / "catalog").mkdir(parents=True)
        (client_assets / "catalog" / "native-contract.json").write_text(
            json.dumps({"verified": [{"version": self.client_version}]}), encoding="utf-8")
        config = self.fixture.root / "product-hook-config.json"
        config.write_text(json.dumps({
            "source": str(REPO_ROOT / "src"), "assets": str(FIXTURE_ROOT),
            "client_assets": str(client_assets),
            "state": str(runtime.session_store.root), "managed_id": _MANAGED,
            "epoch": record["launch_epoch"], "events": str(self.events),
        }), encoding="utf-8")
        script = self.fixture.root / "product-hook.py"
        script.write_text(_PRODUCT_HOOK, encoding="utf-8")
        command = " ".join(shlex.quote(str(value)) for value in (sys.executable, script, config))
        settings = self._settings()
        flag = strict_json.loads(settings.read_bytes())
        flag["hooks"] = {event: [{"hooks": [{"type": "command", "command": command, "timeout": 10}]}]
                         for event in ("SessionStart", "UserPromptSubmit")}
        state.atomic_write(settings, strict_json.canonical_file_bytes(flag))
        return runtime, settings

    def _until_source(self, source):
        def wait():
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if self.events.exists() and any(row.get("source") == source for row in self._event_rows()):
                    return
                time.sleep(0.05)
            raise AssertionError(f"the product hook did not report source {source}")
        return wait

    def _product_notice_lane(self, *, apply_live):
        runtime, settings = self._product_fixture()
        before = notice_hooks.read_gen(self.scope)
        facts = []

        def respond(document, path, context):
            if "Agent" in context.record.tool_names:
                body = json.dumps(document.get("messages", []))
                facts.append({"current": "lineup_generation 2" in body,
                              "conditional": "was applied while this session was running" in body,
                              "staged": "active after /reload-plugins" in body})
            return hooks._sse_responder(document, path)

        def change():
            marker, _raw = notice_hooks.read_marker(runtime.session_store.root, _MANAGED)
            self.assertEqual(marker.start, before, "the process start generation was not recorded")
            with fx.hermetic(runtime):
                runtime.profiles.update("notice", lambda doc: doc["agents"].update(
                    {"cm-implementer": {"model": "opus55", "effort": "xhigh"}}))
                report = io.StringIO()
                lineup_mod.on_saved(runtime, ["notice"], apply_live=apply_live, out=report)
                self.assertIn("applied live" if apply_live else "not applied", report.getvalue())

        steps = [
            probe.PTYInteraction(_PROMPT, b"PROPCHECK-PRODUCT-ONE\r"),
            probe.PTYInteraction(b"PROBE-OK", b"PROPCHECK-PRODUCT-TWO\r", before_send=change),
            probe.PTYInteraction(b"lineup gen 2" if apply_live else b"profile notice changed", b"",
                                 preserve_after_wait=True),
        ]
        if apply_live:
            steps.extend([
                probe.PTYInteraction(b"PROBE-OK", b"PROPCHECK-PRODUCT-THREE\r"),
                probe.PTYInteraction(b"PROBE-OK", b"/exit\r"),
            ])
        else:
            steps.extend([
                probe.PTYInteraction(b"PROBE-OK", b"/compact\r"),
                probe.PTYInteraction(b"Compacted", b"", preserve_after_wait=True),
                probe.PTYInteraction(_PROMPT, b"PROPCHECK-PRODUCT-THREE\r",
                                     before_send=self._until_source("compact")),
                probe.PTYInteraction(b"PROBE-OK", b"/exit\r"),
            ])
        with probe.FakeAnthropicProvider(responder=types.SimpleNamespace(respond=respond)) as provider:
            self._run(settings, steps, provider, extra=("--session-id", _MANAGED))
        events = self._event_rows()
        prompts = [row for row in events if row["event"] == "UserPromptSubmit"]
        self.assertEqual([row["user_notice"] for row in prompts], [False, True, False])
        current_record = runtime.session_store.load(_MANAGED)
        marker, _raw = notice_hooks.read_marker(runtime.session_store.root, current_record["runtime_session_id"])
        self.assertEqual(marker.start, before)
        self.assertFalse(marker.message)
        if apply_live:
            self.assertEqual(current_record["lineup_generation"], 2)
            self.assertTrue(any(row["current"] for row in facts))
            self.assertTrue(prompts[1]["conditional"])
        else:
            compactions = [row for row in events if row.get("source") == "compact"]
            self.assertEqual(len(compactions), 1)
            self.assertFalse(compactions[0]["conditional"])
            self.assertFalse(compactions[0]["staged"])
            self.assertFalse(any(row["conditional"] or row["staged"] for row in facts))
            self.assertEqual(marker.seen, before)
        kind = "external-live" if apply_live else "unchanged-compaction"
        print(f"client check propagation-{kind}: PASS user-notice-deliveries=1 screen-visible=True "
              f"start-generation-preserved=True {self.identity}")


    def _product_pin_lane(self):
        runtime, settings = self._product_fixture()
        record = runtime.session_store.load(_MANAGED)
        lead_set = notice_hooks.load_lead_set(self.scope)
        target = next(row for row in lead_set["rows"]
                      if row["key"] != record["applied"]["lead"]["key"] and row["key"].startswith("opus"))
        flag = strict_json.loads(settings.read_bytes())
        flag["availableModels"] = [hooks._OPUS, target["selector"]]
        flag["modelPicker"]["options"] = [
            {"model": target["selector"], "label": "CMPROBETARGET", "description": "fixture"},
            {"model": hooks._OPUS, "label": "CMPROBEOPUS", "description": "fixture"},
        ]
        flag["hooks"]["PostModelSwitch"] = flag["hooks"]["UserPromptSubmit"]
        state.atomic_write(settings, strict_json.canonical_file_bytes(flag))

        def recorded_pin():
            self._until_source("picker")()
            self.assertFalse(runtime.session_store.load(_MANAGED)["follow"])
            marker, _raw = notice_hooks.read_marker(runtime.session_store.root, _MANAGED)
            self.assertFalse(marker.message, "the switch must not also queue an outside-change message")

        steps = [
            probe.PTYInteraction(_PROMPT, b"/model\r", before_send=hooks._pause(0.5)),
            probe.PTYInteraction(b"Select model", b"", preserve_after_wait=True),
            probe.PTYInteraction(b"cancel", hooks._UP, before_send=hooks._pause(0.4)),
            probe.PTYInteraction(b"CMPROBETARGET", b"s", before_send=hooks._pause(0.4)),
            probe.PTYInteraction(_PROMPT, b"PROPCHECK-PIN-ONE\r", before_send=recorded_pin),
            probe.PTYInteraction(b"now keeps lead", b"", preserve_after_wait=True),
            probe.PTYInteraction(b"PROBE-OK", b"PROPCHECK-PIN-TWO\r"),
            probe.PTYInteraction(b"PROBE-OK", b"/exit\r"),
        ]
        with probe.FakeAnthropicProvider(responder=hooks._sse_responder) as provider:
            self._run(settings, steps, provider, extra=("--session-id", _MANAGED))
        events = self._event_rows()
        self.assertEqual([row["user_notice"] for row in events if row["event"] == "PostModelSwitch"], [True])
        self.assertEqual([row["user_notice"] for row in events if row["event"] == "UserPromptSubmit"], [False, False])
        self.assertEqual(runtime.session_store.load(_MANAGED)["applied"]["lead"]["key"], target["key"])
        print(f"client check S6P-product: PASS pin-visible-next-prompt=True marker-message=False {self.identity}")


class CandidatePropagationClientTests(_PropagationFixture):
    def test_product_pin_warning_reaches_the_next_prompt_without_duplicate(self):
        self._product_pin_lane()

    def test_external_change_uses_the_product_notice_once(self):
        self._product_notice_lane(apply_live=True)

    def test_unchanged_compaction_has_no_reload_claim(self):
        self._product_notice_lane(apply_live=False)

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
