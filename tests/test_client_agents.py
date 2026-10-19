"""Real-client probes S1-S3: live agent reload, durability, SendMessage.

Real-binary probes against the pinned Claude Code binary (resolved from
``catalog/native-contract.json`` exactly like ``RealPinnedBinaryTests``:
path + full SHA-256, never a hard-coded version). Each run goes through the
probe harness narrow allowance: disposable fixture HOME/CLAUDE_CONFIG_DIR,
the loopback fake provider, and the live-daemon-domain tripwire. A tripped
tripwire is INCONCLUSIVE (a BOUNDARY skip), never a pass.

The scope directory is synthetic: one agent file ``cm-spike-x.md`` passed via
``--add-dir`` whose frontmatter model is rewritten mid-session by a PTY
``before_send`` callback. The fixture settings carry the production-shaped
fence (lead + both agent selectors, lead pinned).

Design questions:

- S1: does ``/reload-plugins`` make the next spawn use the rewritten
  frontmatter model? (PASS => live reload is primary; FAIL => assisted relaunch)
- S2: after a live change, ``/compact``, exit, and a relaunch with the same
  ``--resume`` argv, does a spawn keep the new model? (PASS => durability
  holds; FAIL => live changes are blocked). A real supervisor takeover is not probed
  offline; it stays user acceptance, like U1.
- S3: does a SendMessage-resumed agent keep its original model after a
  reload? (shapes the lead-prompt wording)
- S15: do the compiled permissions refuse fork subagents
  (``subagent_type: "fork"``, the caller's own context and model), the
  lead's and a cm agent's, headless and in a terminal, against competing
  settings layers and on ``--resume``? (PASS => the ``Agent(fork)`` deny
  holds; the controls without it show forks running)

Hermetic: every real run executes inside a loopback-only network namespace
(the harness's bwrap isolation, fake provider bridged over a fixture unix
socket) with a fixture-private ``CLAUDE_CODE_TMPDIR``; without isolation the
class skips with a BOUNDARY message. The tests pass a SYNTHETIC ambient
``environ`` (fake HOME under the scratch root, fixed PATH): that disables the
harness credential scan and live-roots check against the real environment
(the ``test_scope_probe.py`` precedent); the child never inherits it.

Every test asserts the observed client behaviour, so a future client flip
fails the suite. Request bodies are inspected in memory inside the
test-local responder only; what is kept and printed is metadata (models,
definition labels, hash prefixes, counters, booleans) — never prompt,
response, or transcript text.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import unittest
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

from charmer import probe, scope, state, strict_json
from charmer.probe import ProbeError

from _tier import real_binary_gate
from _layout import RESOURCES_ROOT  # the one root source


_CONTRACT_PATH = (
    RESOURCES_ROOT / "catalog" / "native-contract.json"
)

_LEAD = "spike-lead"
_MODEL_A = "spike-model-a"
_MODEL_B = "spike-model-b"
_AGENT_TYPE = "cm-spike-x"
_BODY_MARKER = "CLIENT-CHECK-X-BODY"
_DEFINITION_MARKERS = {"A": "CLIENT-CHECK-DEF-A", "B": "CLIENT-CHECK-DEF-B"}
_SUMMARY_MARKER = "CLIENTCHECK-COMPACT-SUMMARY"
_AGENT_TEMPLATE = """---
name: cm-spike-x
description: Synthetic probe agent for the live-reload probes.
model: {model}
---
You are the cm-spike-x fixture agent. {body} {definition}.
"""
_DIRECTIVE_RE = re.compile(r"CLIENTCHECK-(SPAWN|SEND)-(\d{1,2})")
_AGENT_ID_RE = re.compile(r"agentId: ([A-Za-z0-9_-]{4,64})")
_TOOL_ID_PREFIX = "toolu_check_"
_PROMPT_GLYPH = "❯".encode("utf-8")
_RELOAD_RE = re.compile(
    r"Reloaded:\s*(\d+)\s*plugins?\s*·\s*(\d+)\s*skills?\s*·\s*"
    r"(\d+)\s*agents?\s*·\s*(\d+)\s*hooks?\s*·\s*"
    r"(\d+)\s*plugin\s*MCP\s*servers?\s*·\s*(\d+)\s*plugin\s*LSP\s*servers?"
)
_RELOAD_COUNTERS = (
    "plugin",
    "skill",
    "agent",
    "hook",
    "plugin MCP server",
    "plugin LSP server",
)
_OSC_RE = re.compile(r"\x1b\][^\x07]*\x07")
_CURSOR_COLUMN_RE = re.compile(r"\x1b\[\d*G")
_CSI_RE = re.compile(r"\x1b\[[0-9;?<>=]*[A-Za-z~]")


def _screen_text(raw: str) -> str:
    """Flatten a PTY capture: cursor-column jumps become spaces."""

    text = _OSC_RE.sub("", raw)
    text = _CURSOR_COLUMN_RE.sub(" ", text)
    text = _CSI_RE.sub("", text)
    return " ".join(text.split())


def _reload_counters(raw: str) -> dict[str, int] | None:
    match = _RELOAD_RE.search(_screen_text(raw))
    if match is None:
        return None
    return {
        label: int(value) for label, value in zip(_RELOAD_COUNTERS, match.groups())
    }


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
    return "\n".join(parts)


@dataclass(frozen=True)
class _SubagentCall:
    """Metadata of one request made by the cm-spike-x subagent."""

    tag: str | None  # the directive that was active when it arrived
    model: str | None
    definition: str  # "A" | "B" | "?" (which definition body it carried)
    system_sha12: str
    message_count: int


class _SpikeResponder:
    """Directive-driven fake provider script for S1-S3.

    User turns carry directives (``CLIENTCHECK-SPAWN-<n>`` / ``CLIENTCHECK-SEND-<n>``).
    The first lead request for a new directive gets a forced ``Agent`` (or
    ``SendMessage`` to the agent spawned first) tool_use; the tool_result gets
    a ``CLIENTCHECK-DONE-<tag>`` reply and the background-completion notification
    a ``CLIENTCHECK-NOTIFIED-<tag>`` reply, so a PTY script can wait for the
    subagent to finish before its next step. Requests whose system prompt
    carries the agent body marker are the subagent's own; their model,
    definition label, and system-prompt hash prefix are recorded. While
    ``compacting`` is set every lead request gets a fixed summary text.
    Only metadata leaves the responder.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._issued: list[str] = []
        self._done: set[str] = set()
        self.current_tag: str | None = None
        self.subagent_calls: list[_SubagentCall] = []
        self.agent_ids: dict[str, str] = {}  # in memory only, never printed
        self.send_outcomes: dict[str, dict[str, bool]] = {}
        self.tool_surface: dict[str, Any] | None = None
        self.compacting = False
        self.compaction_requests = 0
        self.notified: list[str] = []

    def set_compacting(self, value: bool) -> None:
        with self._lock:
            self.compacting = value

    def calls_for(self, tag: str) -> list[_SubagentCall]:
        with self._lock:
            return [call for call in self.subagent_calls if call.tag == tag]

    def respond(
        self, document: dict[str, Any], path: str, context: Any
    ) -> tuple[int, Any]:
        route = urllib.parse.urlsplit(path).path.rstrip("/")
        if route.endswith("/count_tokens"):
            return 200, {"input_tokens": 1}
        if not route.endswith("/messages"):
            return 404, {
                "type": "error",
                "error": {"type": "not_found_error", "message": "probe: unknown path"},
            }
        with self._lock:
            return self._respond_locked(document, context.ordinal)

    @staticmethod
    def _reply(
        document: dict[str, Any], blocks: list[dict[str, Any]], stop: str, ordinal: int
    ) -> tuple[int, Any]:
        payload = probe._message_payload(
            blocks,
            model=document.get("model"),
            stop_reason=stop,
            message_id=f"msg_client_check_{ordinal:04d}",
        )
        if document.get("stream"):
            return 200, probe._sse_message(payload)
        return 200, payload

    def _text(self, document: dict[str, Any], text: str, ordinal: int):
        return self._reply(document, [{"type": "text", "text": text}], "end_turn", ordinal)

    def _respond_locked(self, document: dict[str, Any], ordinal: int):
        model = document.get("model")
        system = strict_json.canonical_bytes(document.get("system", "")).decode(
            "utf-8"
        )
        messages = document.get("messages")
        messages = messages if isinstance(messages, list) else []
        if _BODY_MARKER in system:
            has_a = _DEFINITION_MARKERS["A"] in system
            has_b = _DEFINITION_MARKERS["B"] in system
            definition = "A" if has_a and not has_b else "B" if has_b and not has_a else "?"
            self.subagent_calls.append(
                _SubagentCall(
                    tag=self.current_tag,
                    model=model if isinstance(model, str) else None,
                    definition=definition,
                    system_sha12=hashlib.sha256(system.encode("utf-8")).hexdigest()[:12],
                    message_count=len(messages),
                )
            )
            return self._text(document, "CLIENTCHECK-SUBAGENT-OK", ordinal)
        if self.compacting:
            self.compaction_requests += 1
            return self._text(document, f"{_SUMMARY_MARKER} fixture summary", ordinal)
        tools = [
            tool for tool in document.get("tools") or [] if isinstance(tool, dict)
        ]
        names = [tool.get("name") for tool in tools]
        directives: list[tuple[str, str]] = []
        results: dict[str, dict[str, Any]] = {}
        last_user_text = ""
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            texts: list[str] = []
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    texts.append(block["text"])
                    directives.extend(_DIRECTIVE_RE.findall(block["text"]))
                elif block.get("type") == "tool_result":
                    tool_id = str(block.get("tool_use_id", ""))
                    if tool_id.startswith(_TOOL_ID_PREFIX):
                        results[tool_id[len(_TOOL_ID_PREFIX):]] = block
            last_user_text = "\n".join(texts)
        if "Agent" not in names or not directives:
            return self._text(document, "PROBE-OK", ordinal)
        kind, number = directives[-1]
        tag = f"{kind.lower()}{number}"
        if tag not in self._issued:
            self._issued.append(tag)
            self.current_tag = tag
            if self.tool_surface is None:
                self.tool_surface = self._surface(tools)
            return self._reply(document, [self._tool_use(kind, tag)], "tool_use", ordinal)
        if tag in results and tag not in self._done:
            self._done.add(tag)
            self._absorb_result(kind, tag, results[tag])
            return self._text(document, f"CLIENTCHECK-DONE-{tag}", ordinal)
        if "<task-notification>" in last_user_text:
            self.notified.append(tag)
            return self._text(document, f"CLIENTCHECK-NOTIFIED-{tag}", ordinal)
        return self._text(document, "PROBE-OK", ordinal)

    @staticmethod
    def _surface(tools: list[dict[str, Any]]) -> dict[str, Any]:
        def props(name: str) -> tuple[str, ...] | None:
            tool = next((item for item in tools if item.get("name") == name), None)
            if tool is None:
                return None
            schema = tool.get("input_schema") or {}
            return tuple(sorted((schema.get("properties") or {}).keys()))

        return {
            "names": tuple(sorted(str(tool.get("name")) for tool in tools)),
            "agent_props": props("Agent"),
            "send_message_props": props("SendMessage"),
        }

    def _tool_use(self, kind: str, tag: str) -> dict[str, Any]:
        if kind == "SPAWN":
            return {
                "type": "tool_use",
                "id": f"{_TOOL_ID_PREFIX}{tag}",
                "name": "Agent",
                "input": {
                    "description": "client check delegation",
                    "subagent_type": _AGENT_TYPE,
                    "prompt": "Reply with exactly one short line.",
                },
            }
        target = next(iter(self.agent_ids.values()), "client-check-missing-agent")
        return {
            "type": "tool_use",
            "id": f"{_TOOL_ID_PREFIX}{tag}",
            "name": "SendMessage",
            "input": {
                "to": target,
                "summary": "client check follow-up",
                "message": "Reply again with one short line.",
            },
        }

    def _absorb_result(self, kind: str, tag: str, block: dict[str, Any]) -> None:
        text = _text_of(block.get("content"))
        if kind == "SPAWN":
            match = _AGENT_ID_RE.search(text)
            if match:
                self.agent_ids[tag] = match.group(1)
            return
        target = next(iter(self.agent_ids.values()), None)
        try:
            outcome = strict_json.loads(text.encode("utf-8"))
        except strict_json.StrictJSONError:
            outcome = None
        success = isinstance(outcome, dict) and outcome.get("success") is True
        resumed = (
            isinstance(outcome, dict)
            and target is not None
            and outcome.get("resumedAgentId") == target
        )
        self.send_outcomes[tag] = {
            "is_error": bool(block.get("is_error")),
            "success": success,
            "resumed_target": resumed,
        }


class _SpikeRealBinaryTestCase(unittest.TestCase):
    """Operator-host-only base: pinned binary, fixture, synthetic scope."""

    trusted: probe.TrustedExecutable
    evidence_label: str = ""
    boundary_skip: str | None = None

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        gate = real_binary_gate(_CONTRACT_PATH, what="the real-client probes")
        cls.boundary_skip = gate.boundary
        if gate.trusted is None:
            return
        cls.trusted = gate.trusted
        cls.evidence_label = f"client={gate.version} sha256={gate.trusted.sha256[:12]}"

    def setUp(self) -> None:
        super().setUp()
        if self.boundary_skip is not None:
            self.skipTest(self.boundary_skip)
        self.root = Path(tempfile.mkdtemp(prefix="charmer-client-check-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        # Simulated live roots, as in the F-series probes: the fixture stays
        # disjoint and the ambient scan sees no provider credentials.
        self.environ = {
            "HOME": str(self.root / "live" / "home"),
            "PATH": "/usr/bin:/bin",
        }
        self._prepare_fixture("fixture")

    def _prepare_fixture(self, name: str) -> None:
        """Fresh fixture + synthetic scope (model A) as the active fixture."""

        self.fixture = probe.build_fixture(self.root / name, environ=self.environ)
        self.scope = state.ensure_private_dir(self.fixture.root / "scope")
        self.agents_dir = state.ensure_private_dir(self.scope / ".claude" / "agents")
        self._write_agent(_MODEL_A, "A")
        # Production-shaped fence: lead + every bound selector, lead
        # pinned, so neither agent model silently falls back to the lead.
        # Plus the managed permission default (2.1.284+ otherwise
        # starts in auto mode, which cannot classify an Agent spawn through a
        # gateway and blocks it).
        state.atomic_write(
            self.fixture.claude_config_dir / "settings.json",
            strict_json.canonical_file_bytes(
                {"availableModels": [_LEAD, _MODEL_A, _MODEL_B], "model": _LEAD,
                 "permissions": {"defaultMode": scope.PERMISSION_DEFAULT_MODE}}
            ),
        )
        probe.seed_fixture_project_trust(self.fixture, self.fixture.project_dir)
        probe.seed_fixture_project_trust(self.fixture, self.scope)

    def _write_agent(self, model: str, definition: str) -> None:
        body = _AGENT_TEMPLATE.format(
            model=model,
            body=_BODY_MARKER,
            definition=_DEFINITION_MARKERS[definition],
        )
        state.atomic_write(self.agents_dir / f"{_AGENT_TYPE}.md", body.encode("utf-8"))

    def _rewrite_to_b(self) -> None:
        self._write_agent(_MODEL_B, "B")

    def _boundary_entries(self, session_id: str) -> int:
        """Count ``system/compact_boundary`` entries in the FIXTURE transcript.

        Metadata only: each line of the synthetic fixture session file is
        parsed and only its ``type``/``subtype`` keys are compared; nothing
        is kept. Never touches a live transcript root.
        """

        projects = self.fixture.claude_config_dir / "projects"
        count = 0
        for path in projects.glob(f"*/{session_id}.jsonl"):
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    try:
                        entry = strict_json.loads(line.encode("utf-8"))
                    except strict_json.StrictJSONError:
                        continue
                    if (
                        isinstance(entry, dict)
                        and entry.get("type") == "system"
                        and entry.get("subtype") == "compact_boundary"
                    ):
                        count += 1
        return count

    def _await_boundary(self, session_id: str, timeout: float = 20.0) -> Any:
        """PTY callback: block ``/exit`` until the compact boundary is on disk.

        The client renders "Compacted" before it appends the boundary entry;
        exiting inside that window (about 0.5 s here) persists no boundary,
        so the relaunch replays the pre-compaction history. This barrier
        makes the resume observation deterministic and network-independent.
        """

        def callback() -> None:
            deadline = time.monotonic() + timeout
            while self._boundary_entries(session_id) < 1:
                if time.monotonic() > deadline:
                    raise RuntimeError("compact boundary was never persisted")
                time.sleep(0.1)

        return callback

    def _onboarding(self) -> list[probe.PTYInteraction]:
        return [
            probe.PTYInteraction(b"Choose", b"2\r"),
            probe.PTYInteraction(b"Press", b"\r"),
        ]

    def _run_pty(
        self,
        probe_id: str,
        session_id: str,
        interactions: list[probe.PTYInteraction],
        provider: probe.FakeAnthropicProvider,
    ) -> probe.NativeRunResult:
        try:
            result = probe.run_native_pty(
                (
                    "--session-id",
                    session_id,
                    "--model",
                    _LEAD,
                    "--add-dir",
                    str(self.scope),
                ),
                interactions,
                trusted=self.trusted,
                fixture=self.fixture,
                provider=provider,
                timeout=120,
                allow_real=True,
                environ=self.environ,
            )
        except ProbeError as exc:
            self._boundary_if_live_domain(probe_id, exc)
            raise
        self.assertFalse(result.timed_out)
        self.assertEqual(result.returncode, 0)
        self.assertIsNotNone(result.daemon)
        assert result.daemon is not None
        self.assertTrue(result.daemon.unchanged)
        return result

    def _boundary_if_live_domain(self, probe_id: str, exc: ProbeError) -> None:
        if "live daemon domain touched" in str(exc):
            print(
                f"client check {probe_id}: INCONCLUSIVE live daemon domain touched "
                f"({self.evidence_label})"
            )
            self.skipTest(
                "BOUNDARY: live daemon-domain churn prevented a clean "
                f"{probe_id} observation ({exc})"
            )

    def _single_call(self, responder: _SpikeResponder, tag: str) -> _SubagentCall:
        calls = responder.calls_for(tag)
        self.assertEqual(
            len(calls),
            1,
            {"tag": tag, "calls": [(c.tag, c.model, c.definition) for c in responder.subagent_calls]},
        )
        return calls[0]


class LiveAgentReloadSpikeTests(_SpikeRealBinaryTestCase):
    """S1: scope rewrite + ``/reload-plugins`` changes the next spawn's model."""

    def test_s1_reload_plugins_rebinds_next_spawn(self) -> None:
        responder = _SpikeResponder()
        steps = [
            *self._onboarding(),
            probe.PTYInteraction(_PROMPT_GLYPH, b"CLIENTCHECK-SPAWN-1 go\r"),
            # Rewrite the scope file, then spawn again WITHOUT a reload: the
            # control that add-dir agents are not hot-watched.
            probe.PTYInteraction(
                b"CLIENTCHECK-NOTIFIED-spawn1",
                b"CLIENTCHECK-SPAWN-2 go\r",
                before_send=self._rewrite_to_b,
            ),
            probe.PTYInteraction(b"CLIENTCHECK-NOTIFIED-spawn2", b"/reload-plugins\r"),
            probe.PTYInteraction(b"Reloaded", b"CLIENTCHECK-SPAWN-3 go\r"),
            probe.PTYInteraction(b"CLIENTCHECK-NOTIFIED-spawn3", b"/exit\r"),
        ]
        with probe.FakeAnthropicProvider(responder=responder) as provider:
            result = self._run_pty(
                "S1", "34343434-0001-4001-8001-000000000001", steps, provider
            )
        before = self._single_call(responder, "spawn1")
        stale = self._single_call(responder, "spawn2")
        after = self._single_call(responder, "spawn3")
        # Initial spawn: frontmatter model A through the fence.
        self.assertEqual((before.model, before.definition), (_MODEL_A, "A"))
        # Rewritten on disk but not reloaded: the old definition still wins.
        self.assertEqual((stale.model, stale.definition), (_MODEL_A, "A"))
        # After /reload-plugins the next spawn carries the new model and body.
        self.assertEqual((after.model, after.definition), (_MODEL_B, "B"))
        counters = _reload_counters(result.stdout)
        self.assertIsNotNone(counters, "the Reloaded counter line was not rendered")
        assert counters is not None
        self.assertGreaterEqual(counters["agent"], 1)
        screen = _screen_text(result.stdout)
        self.assertNotIn("installed but not applied", screen)
        self.assertNotIn("--force", screen)
        lead_models = {
            record.model
            for record in result.requests
            if record.model is not None and record.model not in (_MODEL_A, _MODEL_B)
        }
        self.assertEqual(lead_models, {_LEAD})
        print(
            "client check S1: PASS "
            f"spawn1={before.model} no-reload-spawn2={stale.model} "
            f"post-reload-spawn3={after.model}/def-{after.definition} "
            "reload-line=Reloaded[" + ",".join(
                f"{label}={value}" for label, value in counters.items()
            ) + "] staged-plugin-update=INCONCLUSIVE(marketplace install needed; "
            f"not stageable offline) {self.evidence_label}"
        )


class LiveChangeDurabilitySpikeTests(_SpikeRealBinaryTestCase):
    """S2: live change survives /compact + a same-argv ``--resume`` relaunch."""

    def test_s2_compact_and_resume_relaunch_keep_new_model(self) -> None:
        session_id = "34343434-0002-4002-8002-000000000002"
        responder = _SpikeResponder()
        steps = [
            *self._onboarding(),
            probe.PTYInteraction(_PROMPT_GLYPH, b"CLIENTCHECK-SPAWN-1 go\r"),
            probe.PTYInteraction(
                b"CLIENTCHECK-NOTIFIED-spawn1",
                b"/reload-plugins\r",
                before_send=self._rewrite_to_b,
            ),
            probe.PTYInteraction(b"Reloaded", b"CLIENTCHECK-SPAWN-2 go\r"),
            probe.PTYInteraction(
                b"CLIENTCHECK-NOTIFIED-spawn2",
                b"/compact\r",
                before_send=lambda: responder.set_compacting(True),
            ),
            probe.PTYInteraction(
                b"Compacted",
                b"/exit\r",
                before_send=lambda: (
                    responder.set_compacting(False),
                    self._await_boundary(session_id)(),
                ),
            ),
        ]
        with probe.FakeAnthropicProvider(
            responder=responder, content_markers={"summary": _SUMMARY_MARKER}
        ) as provider:
            self._run_pty("S2", session_id, steps, provider)
            first_phase = len(provider.requests)
            try:
                relaunch = probe.run_native(
                    (
                        "-p",
                        "CLIENTCHECK-SPAWN-3 go",
                        "--resume",
                        session_id,
                        "--model",
                        _LEAD,
                        "--add-dir",
                        str(self.scope),
                    ),
                    trusted=self.trusted,
                    fixture=self.fixture,
                    provider=provider,
                    timeout=120,
                    allow_real=True,
                    environ=self.environ,
                )
            except ProbeError as exc:
                self._boundary_if_live_domain("S2", exc)
                raise
        self.assertFalse(relaunch.timed_out)
        self.assertEqual(relaunch.returncode, 0)
        assert relaunch.daemon is not None
        self.assertTrue(relaunch.daemon.unchanged)
        self.assertGreaterEqual(responder.compaction_requests, 1)
        before = self._single_call(responder, "spawn1")
        live = self._single_call(responder, "spawn2")
        resumed = self._single_call(responder, "spawn3")
        self.assertEqual((before.model, before.definition), (_MODEL_A, "A"))
        self.assertEqual((live.model, live.definition), (_MODEL_B, "B"))
        # Binding durability (the design claim): the relaunch's spawn still
        # carries the live-changed binding.
        self.assertEqual((resumed.model, resumed.definition), (_MODEL_B, "B"))
        # Compacted-transcript resume, made deterministic by the boundary
        # barrier above (/exit only after system/compact_boundary is on
        # disk): every relaunch lead request starts from the summary. The
        # review's netns FAIL was /exit landing inside the ~0.5 s window
        # between the "Compacted" render and the boundary append (no
        # network dependency: the same hermetic run passes with the barrier).
        relaunch_lead = [
            record
            for record in relaunch.requests[first_phase:]
            if record.model == _LEAD and record.max_tokens is not None
        ]
        self.assertTrue(relaunch_lead, "the relaunch made no lead request")
        self.assertTrue(
            all("summary" in record.markers_found for record in relaunch_lead),
            [(r.message_count, r.markers_found) for r in relaunch_lead],
        )
        self.assertEqual(self._boundary_entries(session_id), 1)
        print(
            "client check S2: PASS "
            f"post-reload={live.model} compaction_requests="
            f"{responder.compaction_requests} resume-relaunch-spawn="
            f"{resumed.model}/def-{resumed.definition} "
            "compacted-resume=asserted(after-boundary-persisted; relaunch lead "
            f"message_counts={[r.message_count for r in relaunch_lead]}) "
            "compacted-resume-race=/exit-before-boundary-append-replays-"
            "pre-compaction-history "
            "takeover=user-acceptance(U1-like) "
            f"{self.evidence_label}"
        )


class CompactBoundaryRaceTests(_SpikeRealBinaryTestCase):
    """S2 root-cause evidence: /exit before the boundary append.

    Records (does not require) whether an immediate ``/exit`` after the
    "Compacted" render persisted the ``compact_boundary`` entry, and what a
    ``--resume`` then replays. This is the observed netns FAIL shape; it is
    timing-dependent by nature, so the outcome is printed, and only the
    consistency between "boundary persisted" and "relaunch carries the
    summary" is asserted.
    """

    def test_s2_race_exit_before_boundary_append(self) -> None:
        session_id = "34343434-0002-4002-8002-0000000000a2"
        responder = _SpikeResponder()
        steps = [
            *self._onboarding(),
            probe.PTYInteraction(_PROMPT_GLYPH, b"CLIENTCHECK-SPAWN-1 go\r"),
            probe.PTYInteraction(
                b"CLIENTCHECK-NOTIFIED-spawn1",
                b"/compact\r",
                before_send=lambda: responder.set_compacting(True),
            ),
            probe.PTYInteraction(
                b"Compacted",
                b"/exit\r",
                before_send=lambda: responder.set_compacting(False),
            ),
        ]
        with probe.FakeAnthropicProvider(
            responder=responder, content_markers={"summary": _SUMMARY_MARKER}
        ) as provider:
            self._run_pty("S2-race", session_id, steps, provider)
            persisted = self._boundary_entries(session_id)
            first_phase = len(provider.requests)
            try:
                relaunch = probe.run_native(
                    ("-p", "CLIENTCHECK-RACE go", "--resume", session_id, "--model", _LEAD),
                    trusted=self.trusted,
                    fixture=self.fixture,
                    provider=provider,
                    timeout=120,
                    allow_real=True,
                    environ=self.environ,
                )
            except ProbeError as exc:
                self._boundary_if_live_domain("S2-race", exc)
                raise
        self.assertEqual(relaunch.returncode, 0)
        lead = [
            record
            for record in relaunch.requests[first_phase:]
            if record.model == _LEAD and record.max_tokens is not None
        ]
        self.assertTrue(lead)
        carries = all("summary" in record.markers_found for record in lead)
        # Consistency: the relaunch starts from the summary exactly when the
        # boundary made it to disk before exit.
        self.assertEqual(carries, persisted >= 1)
        print(
            "client check S2-race: immediate /exit after 'Compacted' -> "
            f"boundary_persisted={persisted >= 1} relaunch_from_summary={carries} "
            f"relaunch_lead_message_counts={[r.message_count for r in lead]} "
            f"{self.evidence_label}"
        )


class SendMessageResumeSpikeTests(_SpikeRealBinaryTestCase):
    """S3: which model does a SendMessage-resumed agent use after a reload."""

    def _control_without_reload(self) -> tuple[_SubagentCall, _SubagentCall]:
        """Control: same spawn + SendMessage, no rewrite and no reload.

        Runs in its own fresh fixture (the onboarding prompts appear only on
        a fixture's first launch); the main fixture is restored afterwards.
        """

        saved = (self.fixture, self.scope, self.agents_dir)
        self._prepare_fixture("fixture-control")
        self.addCleanup(self._restore_fixture, saved)
        try:
            return self._control_run()
        finally:
            self._restore_fixture(saved)

    def _restore_fixture(self, saved: tuple[Any, Any, Any]) -> None:
        self.fixture, self.scope, self.agents_dir = saved

    def _control_run(self) -> tuple[_SubagentCall, _SubagentCall]:
        responder = _SpikeResponder()
        steps = [
            *self._onboarding(),
            probe.PTYInteraction(_PROMPT_GLYPH, b"CLIENTCHECK-SPAWN-1 go\r"),
            probe.PTYInteraction(b"CLIENTCHECK-NOTIFIED-spawn1", b"CLIENTCHECK-SEND-2 go\r"),
            probe.PTYInteraction(b"CLIENTCHECK-NOTIFIED-send2", b"/exit\r"),
        ]
        with probe.FakeAnthropicProvider(responder=responder) as provider:
            self._run_pty(
                "S3", "34343434-0003-4003-8003-0000000000c0", steps, provider
            )
        self.assertEqual(
            responder.send_outcomes.get("send2"),
            {"is_error": False, "success": True, "resumed_target": True},
        )
        return self._single_call(responder, "spawn1"), self._single_call(
            responder, "send2"
        )

    def test_s3_sendmessage_resume_takes_new_model_with_old_prompt(self) -> None:
        control_original, control_resumed = self._control_without_reload()
        # Without a rewrite and reload the resumed agent keeps model A and
        # its original prompt: the model switch below is the reload's doing.
        self.assertEqual(
            (control_original.model, control_original.definition), (_MODEL_A, "A")
        )
        self.assertEqual(
            (control_resumed.model, control_resumed.definition), (_MODEL_A, "A")
        )
        self.assertEqual(control_resumed.system_sha12, control_original.system_sha12)
        responder = _SpikeResponder()
        steps = [
            *self._onboarding(),
            probe.PTYInteraction(_PROMPT_GLYPH, b"CLIENTCHECK-SPAWN-1 go\r"),
            probe.PTYInteraction(
                b"CLIENTCHECK-NOTIFIED-spawn1",
                b"/reload-plugins\r",
                before_send=self._rewrite_to_b,
            ),
            probe.PTYInteraction(b"Reloaded", b"CLIENTCHECK-SEND-2 go\r"),
            probe.PTYInteraction(b"CLIENTCHECK-NOTIFIED-send2", b"/exit\r"),
        ]
        with probe.FakeAnthropicProvider(responder=responder) as provider:
            self._run_pty(
                "S3", "34343434-0003-4003-8003-000000000003", steps, provider
            )
        surface = responder.tool_surface
        self.assertIsNotNone(surface)
        assert surface is not None
        # The client offers SendMessage to the lead in this mode.
        self.assertIn("SendMessage", surface["names"])
        self.assertTrue({"to", "message"} <= set(surface["send_message_props"] or ()))
        self.assertIn("spawn1", responder.agent_ids)
        self.assertEqual(
            responder.send_outcomes.get("send2"),
            {"is_error": False, "success": True, "resumed_target": True},
        )
        original = self._single_call(responder, "spawn1")
        resumed = self._single_call(responder, "send2")
        self.assertEqual((original.model, original.definition), (_MODEL_A, "A"))
        # Observed: the resumed agent does NOT keep its original model — it
        # takes the reloaded frontmatter model — while its system prompt is
        # the original spawn's, byte for byte (old definition body).
        self.assertEqual(resumed.model, _MODEL_B)
        self.assertEqual(resumed.definition, "A")
        self.assertEqual(resumed.system_sha12, original.system_sha12)
        self.assertGreater(resumed.message_count, original.message_count)
        props = ",".join(surface["send_message_props"] or ())
        sha_relation = (
            "sha-equal" if resumed.system_sha12 == original.system_sha12 else "sha-differs"
        )
        print(
            "client check S3: FAIL "
            f"tool=SendMessage({props}) original={original.model}/def-"
            f"{original.definition} resumed={resumed.model} resumed-system="
            f"original-def-{resumed.definition}({sha_relation}) "
            f"no-reload-control={control_resumed.model}/def-"
            f"{control_resumed.definition} "
            f"{self.evidence_label}"
        )


# ------------------------------------------------------------------ S15


_FORK_WAIT = 30.0


def _fork_result_class(content: Any) -> str:
    """The class of one Agent tool result (its text never leaves memory)."""

    text = json.dumps(content)
    if "has been denied by permission rule 'Agent(fork)'" in text:
        return "denied"
    if "Agent type 'fork' not found" in text:
        return "unavailable"
    if "Fork started" in text or "[Subagent hand-back]" in text:
        return "started"
    return "other"


def _newest_user(messages: Any) -> dict[str, Any]:
    """The newest user message (the client may append a system-role one)."""

    users = [message for message in messages if isinstance(message, dict) and message.get("role") == "user"] \
        if isinstance(messages, list) else []
    return users[-1] if users else {}


def _last_results(message: dict[str, Any]) -> dict[str, Any]:
    """The tool results of one message (in memory only)."""

    blocks = message.get("content")
    return {block.get("tool_use_id"): block.get("content") for block in blocks if isinstance(block, dict)
            and block.get("type") == "tool_result"} if isinstance(blocks, list) else {}


class _ForkResponder:
    """The lead's new turn calls ``Agent`` twice: a fork, and (``spawn``) a
    spawn of the fixture cm agent; that agent's first request calls a fork
    itself. Every
    run has its own prompt marker and tool ids (a resumed history carries
    the earlier run's). Fork requests are recognised by the client's
    agent-type hint; only models, counts and result classes are kept. A
    started fork's caller waits (bounded) for the fork's request, so a
    background fork is seen before the turn ends."""

    def __init__(self, run: str, *, spawn: bool = True) -> None:
        self.spawn = spawn
        self.prompt = f"S15-LEAD-PROMPT-{run}"
        self.mark = f"S15-AGENT-MARK-{run}"
        self.ids = {name: f"toolu_s15_{run}_{name}" for name in ("lead", "spawn", "agent")}
        self.lock = threading.Lock()
        self.lead_called = False
        self.agent_called = False
        self.forks: list[str | None] = []  # the fork requests' models
        self.results: dict[str, str] = {}
        self.fork_seen = threading.Event()
        self.offered = False

    def _fork(self, name: str) -> dict[str, Any]:
        return {"type": "tool_use", "id": self.ids[name], "name": "Agent",
                "input": {"description": "client check S15", "prompt": f"Reply S15-{name.upper()}-FORK",
                          "subagent_type": "fork", "run_in_background": False}}

    def respond(self, document: dict[str, Any], path: str, context: probe.RequestContext) -> tuple[int, Any]:
        if urllib.parse.urlsplit(path).path.endswith("/count_tokens"):
            return 200, {"input_tokens": 1}
        hints = dict(context.record.hint_header_values)
        names = {tool.get("name") for tool in document.get("tools") or () if isinstance(tool, dict)}
        newest = _newest_user(document.get("messages"))
        last = json.dumps(newest)
        agent = _BODY_MARKER in json.dumps(document.get("system"))
        results = {call_id: _fork_result_class(content)
                   for call_id, content in _last_results(newest).items() if call_id in self.ids.values()}
        content: list[dict[str, Any]] = [{"type": "text", "text": "PROBE-OK"}]
        with self.lock:
            if hints.get("x-claude-code-agent-type") == "fork":
                self.forks.append(document.get("model"))
                self.fork_seen.set()
            elif not agent and self.prompt in last and not results:
                self.offered = self.offered or "Agent" in names
                if "Agent" in names and not self.lead_called:
                    self.lead_called = True
                    content = [self._fork("lead")]
                    if self.spawn:
                        content.append({"type": "tool_use", "id": self.ids["spawn"], "name": "Agent",
                                        "input": {"description": "client check S15", "prompt": self.mark,
                                                  "subagent_type": _AGENT_TYPE, "run_in_background": False}})
            elif agent and self.mark in last and "Agent" in names and not self.agent_called and not results:
                self.agent_called = True
                content = [self._fork("agent")]
            for call_id, outcome in results.items():
                self.results.setdefault(call_id, outcome)
        if content[0]["type"] == "text" and any(
                outcome == "started" and call_id != self.ids["spawn"] for call_id, outcome in results.items()):
            self.fork_seen.wait(_FORK_WAIT)
        stop = "tool_use" if content[0]["type"] == "tool_use" else "end_turn"
        payload = probe._message_payload(content, model=document.get("model"), stop_reason=stop,
                                         message_id=f"msg_s15_{context.ordinal:04d}")
        return 200, probe._sse_message(payload) if document.get("stream") else payload

    def outcome(self) -> dict[str, Any]:
        return {"lead": self.results.get(self.ids["lead"], "no-result"),
                "agent": self.results.get(self.ids["agent"], "no-result" if self.spawn else "not-attempted"),
                "fork_requests": len(self.forks), "fork_models": sorted({str(m) for m in self.forks})}


class ForkSubagentPolicyTests(_SpikeRealBinaryTestCase):
    """S15: fork subagents (``subagent_type: "fork"``) against the compiled
    ``Agent(fork)`` deny.

    Negative controls without the deny: the lead's fork and a cm agent's fork
    both run, on the caller's own model, headless (on by default since
    2.1.302) and in a terminal session. With the production compile's
    permissions the client refuses both forks naming the rule, headless and
    in a terminal; with competing user, project and local settings
    (``CLAUDE_CODE_FORK_SUBAGENT=1`` in ``env``, an ``Agent(fork)`` allow) and
    the same variable in the process environment, on a fresh start and on
    ``--resume``; ``--no-subagents`` offers no Agent tool at all."""

    responder_class = _ForkResponder

    def _settings(self, label: str, permissions: dict[str, Any]) -> Path:
        path = self.scope / f"settings-s15-{label}.json"
        state.atomic_write(path, strict_json.canonical_file_bytes(
            {"availableModels": [_LEAD, _MODEL_A, _MODEL_B], "model": _LEAD, "permissions": permissions}))
        return path

    def _competing_layers(self) -> None:
        layer = {"env": {"CLAUDE_CODE_FORK_SUBAGENT": "1"}, "permissions": {"allow": ["Agent(fork)"]}}
        user = {"availableModels": [_LEAD, _MODEL_A, _MODEL_B], "model": _LEAD,
                "permissions": {"defaultMode": scope.PERMISSION_DEFAULT_MODE, "allow": ["Agent(fork)"]},
                "env": layer["env"]}
        state.atomic_write(self.fixture.claude_config_dir / "settings.json", strict_json.canonical_file_bytes(user))
        project = state.ensure_private_dir(self.fixture.project_dir / ".claude")
        for name in ("settings.json", "settings.local.json"):
            state.atomic_write(project / name, strict_json.canonical_file_bytes(layer))

    def _headless(self, label: str, settings: Path, *, process: str | None = None,
                  extra: tuple[str, ...] = ()) -> _ForkResponder:
        responder = self.responder_class(label)
        original = probe.ProbeFixture.environ

        def environ(fixture_self: probe.ProbeFixture, **kwargs: Any) -> dict[str, str]:
            env = original(fixture_self, **kwargs)
            if process is not None:
                env["CLAUDE_CODE_FORK_SUBAGENT"] = process
            return env

        try:
            with probe.FakeAnthropicProvider(responder=responder) as provider, \
                    mock.patch.object(probe.ProbeFixture, "environ", environ):
                result = probe.run_native(
                    ("-p", responder.prompt, "--settings", str(settings), "--model", _LEAD,
                     "--add-dir", str(self.scope), "--dangerously-skip-permissions", *extra),
                    trusted=self.trusted, fixture=self.fixture, provider=provider, timeout=150,
                    allow_real=True, environ=self.environ, child_env={"CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1"},
                )
        except ProbeError as exc:
            self._boundary_if_live_domain("S15", exc)
            raise
        self.assertFalse(result.timed_out, label)
        self.assertEqual(result.returncode, 0, label)
        return responder

    def _terminal(self, label: str, settings: Path) -> _ForkResponder:
        # The lead's fork only (a terminal runs every agent in the
        # background); a started fork gets a moment to finish before /exit.
        responder = self.responder_class(label, spawn=False)

        def settle() -> None:
            if responder.fork_seen.is_set():
                time.sleep(3)

        steps = [*self._onboarding(),
                 probe.PTYInteraction("❯".encode(), responder.prompt.encode() + b"\r"),
                 probe.PTYInteraction(b"PROBE-OK", b"", preserve_after_wait=True),
                 probe.PTYInteraction("❯".encode(), b"/exit\r", before_send=settle)]
        try:
            with probe.FakeAnthropicProvider(responder=responder) as provider:
                result = probe.run_native_pty(
                    ("--settings", str(settings), "--model", _LEAD, "--add-dir", str(self.scope)),
                    steps, trusted=self.trusted, fixture=self.fixture, provider=provider, timeout=150,
                    allow_real=True, environ=self.environ, child_env={"CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1"},
                )
        except ProbeError as exc:
            self._boundary_if_live_domain("S15", exc)
            raise
        self.assertFalse(result.timed_out, label)
        self.assertEqual(result.returncode, 0, label)
        return responder

    def test_s15_fork_subagents_are_denied(self) -> None:
        import test_compiler_v2  # the production compile (module import: no tests re-exported)

        _lineup, compiled = test_compiler_v2._seed_launch("balanced", default_mode=scope.PERMISSION_DEFAULT_MODE)
        permissions = json.loads(json.dumps(compiled.scope_plan.settings["permissions"]))
        self.assertIn("Agent(fork)", permissions["deny"])
        control_permissions = {**permissions, "deny": [d for d in permissions["deny"] if d != "Agent(fork)"]}
        _lineup, alone = test_compiler_v2._seed_launch(
            "balanced", no_subagents=True, default_mode=scope.PERMISSION_DEFAULT_MODE)
        no_subagents = json.loads(json.dumps(alone.scope_plan.settings["permissions"]))
        observed: dict[str, dict[str, Any]] = {}

        control = self._headless("control", self._settings("control", control_permissions)).outcome()
        self.assertEqual((control["lead"], control["agent"]), ("started", "started"), control)
        self.assertEqual(control["fork_requests"], 2, control)
        self.assertEqual(control["fork_models"], sorted([_LEAD, _MODEL_A]), control)
        observed["control"] = control
        compiled_path = self._settings("compiled", permissions)
        observed["compiled"] = self._headless("compiled", compiled_path).outcome()
        alone_run = self._headless("no-subagents", self._settings("no-subagents", no_subagents))
        self.assertFalse(alone_run.offered)
        observed["no-subagents"] = {**alone_run.outcome(), "agent_tool": alone_run.offered}

        self._competing_layers()
        session = "5e15c0de-5e15-4c0d-8e15-5e15c0de0a01"
        observed["layers-fresh"] = self._headless(
            "layers-fresh", compiled_path, process="1", extra=("--session-id", session)).outcome()
        observed["layers-resume"] = self._headless(
            "layers-resume", compiled_path, process="1", extra=("--resume", session)).outcome()

        # A terminal session (forks on by default there before 2.1.302 too),
        # each in a fresh fixture so the first-run screens are the same.
        self._prepare_fixture("fixture-terminal-control")
        terminal_control = self._terminal("terminal-control", self._settings("t-control", control_permissions))
        observed["terminal-control"] = terminal_control.outcome()
        self._prepare_fixture("fixture-terminal")
        observed["terminal"] = self._terminal("terminal", self._settings("t-compiled", permissions)).outcome()

        self.assertEqual(observed["terminal-control"]["lead"], "started", observed)
        self.assertGreaterEqual(observed["terminal-control"]["fork_requests"], 1, observed)
        for label in ("compiled", "layers-fresh", "layers-resume"):
            self.assertEqual((label, observed[label]["lead"], observed[label]["agent"],
                              observed[label]["fork_requests"]), (label, "denied", "denied", 0), observed[label])
        self.assertEqual((observed["terminal"]["lead"], observed["terminal"]["fork_requests"]), ("denied", 0),
                         observed)
        self.assertEqual(observed["no-subagents"]["fork_requests"], 0, observed)
        print(
            "client check S15: PASS fork subagents: control without Agent(fork) -> lead and cm-agent forks "
            f"start on the caller's model (headless {control['fork_models']}; terminal lead fork "
            f"{observed['terminal-control']['fork_requests']} request(s)); the compiled deny refuses both "
            "headless, and the lead's in a terminal (0 fork requests), against user/project/local env "
            "CLAUDE_CODE_FORK_SUBAGENT=1 + Agent(fork) allow + process env on a fresh start and --resume; "
            f"--no-subagents offers no Agent tool {self.evidence_label}"
        )


# ------------------------------------------------------------- fork gate

FORK_GATE_PASS = "FG-off"
_FORK_ENV = "CLAUDE_CODE_FORK_SUBAGENT"
# The release before the 2.1.302 pin: its interactive sessions had the gap
# (no Agent(fork) deny; the fork gate on in a terminal since 2.1.232).
_FORK_PREVIOUS = ("2.1.292", "a967e7b1d8b4e47ee421d5433027880347952b0c0857abf880e2c942a4ec93b3")


def fork_gate_class(observed: dict[str, dict[str, Any]]) -> str:
    """The fork-gate class from each run's outcome.

    ``control*`` runs carry the compiled deny without the compiled gate
    variable, against the competing layers: the gate is on there, so the
    deny must be what refuses (``denied``), or the stimulus is not real
    (INCONCLUSIVE). ``compiled*`` runs carry both: every fork must be
    ``unavailable`` (the gate is off) with no fork request; a ``denied``
    one is the gate left on (``FG-gate-on``), a ``started`` one a fork
    that ran (``FG-fork-runs``). The previous pin's runs, when present,
    must show the gap (``previous-gap`` started) and its closing
    (``previous-off`` unavailable)."""

    def outcomes(row: dict[str, Any]) -> set[str]:
        return {row[key] for key in ("lead", "agent") if row.get(key) not in (None, "not-attempted")}

    for name, row in observed.items():
        if name.startswith("control") and outcomes(row) != {"denied"}:
            raise _ForkGateInconclusive(f"{name}: the competing layers did not turn the gate on ({row})")
    if "previous-gap" in observed and outcomes(observed["previous-gap"]) != {"started"}:
        raise _ForkGateInconclusive(f"previous-gap: the previous pin's fork did not start ({observed['previous-gap']})")
    compiled = {name: row for name, row in observed.items() if name.startswith(("compiled", "previous-off"))}
    if any("started" in outcomes(row) or row.get("fork_requests") for row in compiled.values()):
        return "FG-fork-runs"
    if any(outcomes(row) != {"unavailable"} for row in compiled.values()):
        return "FG-gate-on"
    return FORK_GATE_PASS


class _ForkGateInconclusive(AssertionError):
    """A control did not show the gate on: no class can be chosen."""


class _ForkGateResponder(_ForkResponder):
    """S15's responder, also keeping whether the lead's Agent tool offers
    ``run_in_background`` (a terminal session with the fork gate on forces
    every subagent into the background and drops the parameter)."""

    def __init__(self, run: str, *, spawn: bool = True) -> None:
        super().__init__(run, spawn=spawn)
        self.background_offered: bool | None = None

    def respond(self, document: dict[str, Any], path: str, context: probe.RequestContext) -> tuple[int, Any]:
        agent = next((tool for tool in document.get("tools") or () if isinstance(tool, dict)
                      and tool.get("name") == "Agent"), None)
        if agent is not None and _BODY_MARKER not in json.dumps(document.get("system")):
            with self.lock:
                if self.background_offered is None:
                    properties = (agent.get("input_schema") or {}).get("properties") or {}
                    self.background_offered = "run_in_background" in properties
        return super().respond(document, path, context)

    def outcome(self) -> dict[str, Any]:
        return {**super().outcome(), "background_offered": self.background_offered}


class ForkGateVerdictTests(unittest.TestCase):
    def test_the_class_needs_every_compiled_fork_unavailable(self) -> None:
        def row(lead: str, agent: str = "not-attempted", requests: int = 0) -> dict[str, Any]:
            return {"lead": lead, "agent": agent, "fork_requests": requests}

        rows = {"control": row("denied", "denied"), "compiled-fresh": row("unavailable", "unavailable"),
                "compiled-resume": row("unavailable", "unavailable"), "control-terminal": row("denied"),
                "compiled-terminal": row("unavailable")}
        self.assertEqual(fork_gate_class(rows), FORK_GATE_PASS)
        self.assertEqual(fork_gate_class({**rows, "compiled-resume": row("denied", "denied")}), "FG-gate-on")
        self.assertEqual(fork_gate_class({**rows, "compiled-terminal": row("started", requests=1)}), "FG-fork-runs")
        with self.assertRaises(_ForkGateInconclusive):
            fork_gate_class({**rows, "control": row("unavailable", "unavailable")})
        previous = {"previous-gap": row("started", requests=1), "previous-off": row("unavailable")}
        self.assertEqual(fork_gate_class({**rows, **previous}), FORK_GATE_PASS)
        self.assertEqual(fork_gate_class({**rows, **previous, "previous-off": row("started", requests=1)}),
                         "FG-fork-runs")
        with self.assertRaises(_ForkGateInconclusive):
            fork_gate_class({**rows, **previous, "previous-gap": row("unavailable")})


class ForkGateTests(ForkSubagentPolicyTests):
    """Fork gate: the compiled flag settings env turns the fork gate off
    (``CLAUDE_CODE_FORK_SUBAGENT=0`` beside the ``Agent(fork)`` deny).

    Every run competes with ``CLAUDE_CODE_FORK_SUBAGENT=1`` and an
    ``Agent(fork)`` allow in the user, project and local settings, and with
    the same variable in the process environment (a launch clears it; the
    flag settings must outrank it anyway). Controls: the compiled deny
    without the compiled gate variable leaves the gate on and refuses each
    fork naming the rule, headless and in a terminal. With the compiled
    variable the client offers no fork at all (``Agent type 'fork' not
    found``), headless on a fresh start and on ``--resume`` and in a
    terminal, and a terminal session offers ``run_in_background`` again
    (subagents are no longer forced into the background; recorded). The
    previous pin, when retained next to the pin, is the revert proof of the
    2.0.1 gap: with the 2.0.1 permissions (no fork deny) its terminal fork
    starts, and the compiled variable alone closes it."""

    test_s15_fork_subagents_are_denied = None
    probe_id = "fork-gate"
    responder_class = _ForkGateResponder
    verdict: tuple[str, str, str] = ("INCONCLUSIVE", "unavailable", "check did not complete")

    @classmethod
    def tearDownClass(cls) -> None:
        status, observed, detail = cls.verdict if cls.boundary_skip is None else (
            "BOUNDARY", "unavailable", cls.boundary_skip)
        print(f"client check {cls.probe_id}: {status} class={observed} {detail}".rstrip(), flush=True)
        super().tearDownClass()

    def _gate_settings(self, label: str, permissions: dict[str, Any], env: dict[str, str]) -> Path:
        path = self.scope / f"settings-fg-{label}.json"
        document: dict[str, Any] = {"availableModels": [_LEAD, _MODEL_A, _MODEL_B], "model": _LEAD,
                                    "permissions": permissions}
        if env:
            document["env"] = dict(env)
        state.atomic_write(path, strict_json.canonical_file_bytes(document))
        return path

    def _gate_headless(self, label: str, settings: Path, extra: tuple[str, ...] = ()) -> dict[str, Any]:
        return self._headless(label, settings, process="1", extra=extra).outcome()

    def _gate_terminal(self, label: str, settings: Path) -> dict[str, Any]:
        return self._terminal(label, settings).outcome()

    def test_fork_gate_is_off_against_every_layer(self) -> None:
        import test_compiler_v2  # the production compile (module import: no tests re-exported)

        _lineup, compiled = test_compiler_v2._seed_launch("balanced", default_mode=scope.PERMISSION_DEFAULT_MODE)
        permissions = json.loads(json.dumps(compiled.scope_plan.settings["permissions"]))
        # The compiled gate variable as the production compile states it
        # (absent from a compile that does not own it: the runs then show
        # the gate on, and the class fails).
        gate_env = {key: value for key, value in compiled.scope_plan.settings["env"].items() if key == _FORK_ENV}
        observed: dict[str, dict[str, Any]] = {}
        try:
            self._competing_layers()
            observed["control"] = self._gate_headless("fg-control", self._gate_settings("control", permissions, {}))
            compiled_path = self._gate_settings("compiled", permissions, gate_env)
            session = "f06ec0de-f06e-4c0d-8f06-f06ec0de0a01"
            observed["compiled-fresh"] = self._gate_headless("fg-fresh", compiled_path, ("--session-id", session))
            observed["compiled-resume"] = self._gate_headless("fg-resume", compiled_path, ("--resume", session))
            self._prepare_fixture("fixture-fg-terminal-control")
            self._competing_layers()
            observed["control-terminal"] = self._gate_terminal(
                "fg-terminal-control", self._gate_settings("t-control", permissions, {}))
            self._prepare_fixture("fixture-fg-terminal")
            self._competing_layers()
            observed["compiled-terminal"] = self._gate_terminal(
                "fg-terminal", self._gate_settings("t-compiled", permissions, gate_env))
            previous_note = self._previous_pin(observed, permissions, gate_env)
            observed_class = fork_gate_class(observed)
        except _ForkGateInconclusive as exc:
            type(self).verdict = ("INCONCLUSIVE", "unavailable", str(exc))
            raise
        detail = (
            f"gate env {gate_env or 'absent'}; "
            + "; ".join(f"{name}: lead={row['lead']} agent={row['agent']} fork_requests={row['fork_requests']} "
                        f"run_in_background={'offered' if row['background_offered'] else 'not offered'}"
                        for name, row in observed.items())
            + f"; {previous_note} {self.evidence_label}"
        )
        type(self).verdict = ("PASS" if observed_class == FORK_GATE_PASS else "FAIL", observed_class, detail)
        self.assertEqual(observed_class, FORK_GATE_PASS, detail)
        # The terminal trade-off, recorded so a client change is noticed:
        # with the gate on a terminal forces subagents into the background
        # and drops the parameter; with it off the parameter is offered.
        self.assertIs(observed["control-terminal"]["background_offered"], False, detail)
        self.assertIs(observed["compiled-terminal"]["background_offered"], True, detail)

    def _previous_pin(self, observed: dict[str, dict[str, Any]], permissions: dict[str, Any],
                      gate_env: dict[str, str]) -> str:
        """The previous pin's terminal runs, when it is retained next to the
        pin: the 2.0.1 permissions (no fork deny) without and with the gate
        variable, no competing layers."""

        path = self.trusted.resolved_path.parent / _FORK_PREVIOUS[0]
        if path == self.trusted.resolved_path or not path.is_file() or probe._sha256_file(path) != _FORK_PREVIOUS[1]:
            return f"previous pin {_FORK_PREVIOUS[0]} not retained next to the pin (its revert proof not run)"
        released = {**permissions, "deny": [rule for rule in permissions["deny"] if rule != "Agent(fork)"]}
        pinned = self.trusted
        type(self).trusted = probe.TrustedExecutable(path, _FORK_PREVIOUS[1])
        try:
            self._prepare_fixture("fixture-fg-previous-gap")
            observed["previous-gap"] = self._gate_terminal(
                "fg-previous-gap", self._gate_settings("p-gap", released, {}))
            self._prepare_fixture("fixture-fg-previous-off")
            observed["previous-off"] = self._gate_terminal(
                "fg-previous-off", self._gate_settings("p-off", released, gate_env))
        finally:
            type(self).trusted = pinned
        return f"previous pin {_FORK_PREVIOUS[0]} run"


if __name__ == "__main__":
    unittest.main()
