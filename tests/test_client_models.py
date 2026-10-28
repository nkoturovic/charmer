"""Client checks S7-S9 against the pinned Claude Code: the model probes.

Real-binary probes against the pinned Claude client (resolved from
``catalog/native-contract.json``: exact path plus full sha256, never a
hard-coded version). Every run goes through the probe harness with
``allow_real=True``: a fresh disposable fixture HOME/CLAUDE_CONFIG_DIR per
run, the loopback fake provider as the only endpoint, and the live
daemon-domain tripwire pointed at the real uid-shared domain. A tripped
tripwire is an explicit INCONCLUSIVE (boundary skip), never a pass. Every
real run is hermetic: a loopback-only network namespace (fake provider
bridged over a fixture unix socket) and a fixture-private
``CLAUDE_CODE_TMPDIR``; without isolation the classes skip with a BOUNDARY
message. The tests pass a SYNTHETIC ambient ``environ`` (fake HOME under the
scratch root, fixed PATH), which disables the harness credential scan and
live-roots check against the real environment (the ``test_scope_probe.py``
precedent); the child never inherits it.

- S7 which wire the client requests for bare ``opus``/``fable`` aliases,
  canonical ``claude-<wire>[1m]`` vs ``charmer-<wire>[1m]`` selectors
  (output cap, effort, thinking, prompt-bundle size class, recognition betas,
  model-not-found chain) and the refusal fallback (``stop_reason: refusal`` +
  ``stop_details.category``) with and without its target in the fence.
- S8 agent frontmatter ``effort`` values on the subagent's wire.
- S9 native helper wires (Explore, statusline-setup, claude-code-guide)
  under non-Opus leads and production-shaped fences.
- refusal-routing: the refusal matrix with the compiled managed session
  (fence, flag settings env, lifecycle hooks through charmer's launcher):
  every source kind a lineup binds x refusal category x fence shape x lead,
  subagent, workflow and resumed turns x the switch controls; each case must
  give its specified target or no switch.

Evidence is metadata only: model ids, integers, beta token names, hint
header safe ids, marker names and booleans. Request bodies are inspected in
memory by the test-local responder; no prompt, response, transcript or
stdio text is persisted or printed. Each probe prints exactly one line
``client check S<n>: <verdict> <detail>``; PASS/FAIL is the design verdict,
while the assertions pin the observed client behaviour (a client flip fails
the test).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock
from pathlib import Path
from typing import Any

from charmer import catalog, cli, compiler, probe, profile, scope, state, strict_json, upgrade
from charmer import settings as settings_mod
from charmer.probe import ProbeError

import _v3
from _catalog import uses_shipped_catalog
from _tier import real_binary_gate
from _layout import REPO_ROOT, RESOURCES_ROOT  # the one root source


_CONTRACT_PATH = (
    RESOURCES_ROOT / "catalog" / "native-contract.json"
)
_TURN = "client check probe turn"
_RUN_TIMEOUT = 120.0
_TRIPWIRE = "live daemon domain touched"
_UNRECOGNIZED_MARKER = "[claude-code:unrecognized_model]"
_BETA_RE = re.compile(r"[a-z0-9][a-z0-9.-]{0,63}")
_BETA_CAP = 32
_HINT_ENV = {"CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1"}
_EXPLORE_CAP_OFF = "CLAUDE_CODE_DISABLE_EXPLORE_INHERIT_CAP"

# The compiled family defaults (ANTHROPIC_DEFAULT_*_MODEL)
# derived from the shipped catalog, never a native-generation literal; the
# real-binary spikes read the shipped catalog by rule (AGENTS §4).
_SHIPPED = catalog.load_catalog(RESOURCES_ROOT)
_FAMILY = compiler.family_default_env(
    {key: dict(entry) for key, entry in _SHIPPED.lines.items()}, dict(_SHIPPED.providers)
)
_OPUS = _FAMILY["ANTHROPIC_DEFAULT_OPUS_MODEL"]
_FABLE = _FAMILY["ANTHROPIC_DEFAULT_FABLE_MODEL"]
_SONNET = _FAMILY["ANTHROPIC_DEFAULT_SONNET_MODEL"]
# The client's categorized-refusal route table (a client fact, U23): the
# targets are exactly catalog.REFUSAL_FALLBACK_WIRES (fenced fallback-only).
_OPUS48 = "claude-opus-4-8[1m]"
_OPUS5 = "claude-opus-5[1m]"
_SONNET5 = "claude-sonnet-5[1m]"
_MULTI_OPUS = "charmer-" + _OPUS.removeprefix("claude-")
_MULTI_FABLE = "charmer-" + _FABLE.removeprefix("claude-")
# A managed scope configured nowhere compiles this default mode;
# without it 2.1.284+ starts in auto mode, which (through a gateway) cannot
# classify Agent spawns and refuses them.
_MANAGED_PERMISSIONS = {"defaultMode": scope.PERMISSION_DEFAULT_MODE}
_LEAD_GPT = "gpt-multi-sol-high"
# The completion records the re-pin gate requires (charmer.upgrade), one per
# refusal-routing part; release evidence finds this module as their producer
# by these literals. A part that skips at a boundary or prints FAIL prints
# none of them.
REFUSAL_ROUTING_RECORDS = (
    "client check refusal-routing-lead: PASS ",
    "client check refusal-routing-shapes: PASS ",
    "client check refusal-routing-agents: PASS ",
    "client check refusal-routing-resume: PASS ",
    "client check refusal-routing-controls: PASS ",
    "client check refusal-routing-counter: PASS ",
    "client check refusal-routing-uncountered: PASS ",
)
_OPUS_MARKER = "client-check-marker-opus"
_FABLE_MARKER = "client-check-marker-fable"
_SONNET_MARKER = "client-check-marker-sonnet"


def _retry_s9_pty(runner):
    """Retry just the S9 helper, once, for the known exit-timeout flake."""

    try:
        return runner(timeout=240), False
    except probe.PTYTimeoutError as exc:
        if exc.phase != "client-exit":
            raise
    return runner(timeout=240), True


class S9RetryTests(unittest.TestCase):
    def test_one_timeout_then_success_is_marked(self):
        from unittest.mock import Mock, call

        runner = Mock(side_effect=[
            probe.PTYTimeoutError("client-exit", 1, 1, 240), "ok"
        ])
        self.assertEqual(_retry_s9_pty(runner), ("ok", True))
        self.assertEqual(runner.call_args_list, [call(timeout=240), call(timeout=240)])

    def test_second_timeout_propagates(self):
        from unittest.mock import Mock, call

        first = probe.PTYTimeoutError("client-exit", 1, 1, 240)
        second = probe.PTYTimeoutError("client-exit", 1, 1, 241)
        runner = Mock(side_effect=[first, second])
        with self.assertRaises(probe.PTYTimeoutError) as caught:
            _retry_s9_pty(runner)
        self.assertIs(caught.exception, second)
        self.assertEqual(runner.call_args_list, [call(timeout=240), call(timeout=240)])

    def test_marker_timeout_propagates_without_retry(self):
        from unittest.mock import Mock

        error = probe.PTYTimeoutError("marker", 1, 2, 240)
        runner = Mock(side_effect=error)
        with self.assertRaises(probe.PTYTimeoutError) as caught:
            _retry_s9_pty(runner)
        self.assertIs(caught.exception, error)
        runner.assert_called_once_with(timeout=240)

    def test_plain_probe_errors_propagate_without_retry(self):
        from unittest.mock import Mock

        for message in (
            "some other probe error",
            "probe PTY timed out waiting for client exit",
        ):
            with self.subTest(message=message):
                error = ProbeError(message)
                runner = Mock(side_effect=error)
                with self.assertRaises(ProbeError) as caught:
                    _retry_s9_pty(runner)
                self.assertIs(caught.exception, error)
                runner.assert_called_once_with(timeout=240)


class RefusalRoutingRecordTests(unittest.TestCase):
    """The refusal-routing parts gate every re-pin: each part's PASS record
    is essential, and its boundary skip, FAIL verdict or case rows never
    satisfy it."""

    def test_the_records_are_essential(self) -> None:
        for record in REFUSAL_ROUTING_RECORDS:
            self.assertIn(record, upgrade.ESSENTIAL_EVIDENCE_PREFIXES)
        self.assertEqual(len(set(REFUSAL_ROUTING_RECORDS)), 7)

    def test_a_skipped_or_failed_part_never_satisfies_its_record(self) -> None:
        for record in REFUSAL_ROUTING_RECORDS:
            part = record.removeprefix("client check refusal-routing-").removesuffix(": PASS ")
            others = "\n".join(prefix + "fixture" for prefix in upgrade.ESSENTIAL_EVIDENCE_PREFIXES
                               if prefix != record)
            for line in (
                f"test_{part} (tests.test_client_models.RefusalRoutingMatrixProbe) ... skipped "
                f"'BOUNDARY: refusal-routing INCONCLUSIVE: {_TRIPWIRE} (fixture)'",
                f"client check refusal-routing-{part}: FAIL client=fixture 1 cases, 1 off the specified outcome",
                "client check refusal-routing case: fixture expected=none observed=none rc=1 OK",
                f"note {record}fixture",
            ):
                with self.subTest(part=part, line=line):
                    self.assertEqual(upgrade._missing_evidence_prefixes(others + "\n" + line + "\n"), (record,))
            self.assertEqual(upgrade._missing_evidence_prefixes(others + "\n" + record + "fixture\n"), ())


def _wire(selector: str) -> str:
    return selector[: -len("[1m]")] if selector.endswith("[1m]") else selector


def _has_beta(row: dict[str, Any], prefix: str) -> bool:
    return any(name.startswith(prefix) for name in row["betas"])


class _BetaRecordingProvider(probe.FakeAnthropicProvider):
    """Loopback fake that additionally keeps ``anthropic-beta`` token names.

    Only tokens matching a short safe-id shape are kept, keyed by the
    request ordinal; no other header value is read.
    """

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        self.beta_names: dict[int, tuple[str, ...]] = {}

    def _record(self, method, path, headers, body, document):  # type: ignore[override]
        context = super()._record(method, path, headers, body, document)
        raw = headers.get("anthropic-beta") or ""
        names = sorted(
            {token.strip() for token in raw.split(",") if _BETA_RE.fullmatch(token.strip())}
        )
        self.beta_names[context.ordinal] = tuple(names[:_BETA_CAP])
        return context


def _refusal_reply(model: Any, category: str | None, stream: bool,
                   server_fallback: str | None = None) -> Any:
    """Canned refusal: ``stop_reason: refusal`` plus optional category (and,
    for the refusal probe, an API-lane served-fallback record naming another model)."""

    payload = probe._message_payload(
        [], model=model, stop_reason="refusal", message_id="msg_check_refusal"
    )
    if server_fallback is not None:
        payload["usage"] = {**payload.get("usage", {}), "iterations": [
            {"type": "message", "model": model, "input_tokens": 1, "output_tokens": 1},
            {"type": "fallback_message", "model": server_fallback, "input_tokens": 1, "output_tokens": 1}]}
    details = (
        None
        if category is None
        else {"type": "refusal", "category": category, "explanation": None}
    )
    if details is not None:
        payload["stop_details"] = details
    if not stream:
        return payload
    events = []
    for name, data in probe._sse_message(payload).events:
        if name == "message_delta" and details is not None:
            data = {**data, "delta": {**data["delta"], "stop_details": details}}
        events.append((name, data))
    return probe.SseResponse(tuple(events))


class _SpikeResponder:
    """Context-aware scripted provider; keeps per-request metadata rows.

    ``agents`` forces one parallel Agent call per listed subagent type on the
    first main-thread request offering the Agent tool; ``refuse`` maps wire
    models to a refusal category (None = uncategorized); ``not_found`` wire
    models get a 404 ``not_found_error``. Everything else gets PROBE-OK.
    """

    def __init__(
        self,
        *,
        agents: tuple[str, ...] = (),
        agent_efforts: dict[str, str] | None = None,
        refuse: dict[str, str | None] | None = None,
        not_found: tuple[str, ...] = (),
        server_fallback: str | None = None,
    ):
        self._agents = tuple(agents)
        self._agent_efforts = dict(agent_efforts or {})
        self._refuse = dict(refuse or {})
        self._not_found = frozenset(not_found)
        # A refusal may also carry the API lane's served-fallback
        # record (usage.iterations[].type == "fallback_message").
        self._server_fallback = server_fallback
        # Requests that carried a ``fallbacks`` field (implicit server-side
        # fallback opt-in, which a managed session never sends): their ordinals.
        self.fallbacks_fields: list[int] = []
        self._forced = False
        self._lock = threading.Lock()
        self.rows: list[dict[str, Any]] = []

    def respond(self, document: dict[str, Any], path: str, context: Any) -> Any:
        route = path.split("?", 1)[0].rstrip("/")
        if route.endswith("/count_tokens"):
            return 200, {"input_tokens": 1}
        if not route.endswith("/messages"):
            return 404, {
                "type": "error",
                "error": {"type": "not_found_error", "message": "probe: unknown path"},
            }
        record = context.record
        hints = dict(record.hint_header_values)
        model = document.get("model")
        stream = bool(document.get("stream"))
        with self._lock:
            if "fallbacks" in document:
                self.fallbacks_fields.append(context.ordinal)
            self.rows.append(
                {
                    "ordinal": context.ordinal,
                    "model": record.model,
                    "max_tokens": record.max_tokens,
                    "effort": record.effort,
                    "thinking_type": record.thinking_type,
                    "system_bytes": record.system_json_bytes,
                    "messages_bytes": record.messages_json_bytes,
                    "tool_count": record.tool_count,
                    "stream": stream,
                    "agent_type": hints.get("x-claude-code-agent-type"),
                    "request_class": hints.get("x-claude-code-request-class"),
                    "markers": record.markers_found,
                }
            )
            force = (
                bool(self._agents)
                and not self._forced
                and "Agent" in record.tool_names
                and hints.get("x-claude-code-request-class", "main") != "subagent"
            )
            if force:
                self._forced = True
        if isinstance(model, str) and model in self._not_found:
            return 404, {
                "type": "error",
                "error": {"type": "not_found_error", "message": "probe: model not found"},
            }
        if isinstance(model, str) and model in self._refuse:
            return 200, _refusal_reply(model, self._refuse[model], stream, self._server_fallback)
        if force:
            content = [
                {
                    "type": "tool_use",
                    "id": f"toolu_client_check_{index:02d}",
                    "name": "Agent",
                    "input": {
                        "description": f"client check {index}",
                        "subagent_type": agent,
                        "prompt": "Reply with exactly: PROBE-OK",
                        **({"effort": self._agent_efforts[agent]}
                           if agent in self._agent_efforts else {}),
                    },
                }
                for index, agent in enumerate(self._agents)
            ]
            stop = "tool_use"
        else:
            content = [{"type": "text", "text": "PROBE-OK"}]
            stop = "end_turn"
        payload = probe._message_payload(
            content, model=model, stop_reason=stop, message_id="msg_client_check"
        )
        return 200, probe._sse_message(payload) if stream else payload


class _SpikeTestCase(unittest.TestCase):
    """Operator-host boundary, per-run fixtures and the gated runner."""

    trusted: probe.TrustedExecutable
    version: str = "unknown"
    boundary_skip: str | None = None

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        gate = real_binary_gate(_CONTRACT_PATH, what="client check probes")
        cls.boundary_skip = gate.boundary
        if gate.trusted is None:
            return
        cls.trusted = gate.trusted
        cls.version = gate.version

    def setUp(self) -> None:
        super().setUp()
        if self.boundary_skip is not None:
            self.skipTest(self.boundary_skip)
        self.root = Path(tempfile.mkdtemp(prefix="charmer-client-check-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        # Simulated live roots: the fixture stays disjoint from them and the
        # ambient credential scan sees none (the child never inherits it).
        self.environ = {"HOME": str(self.root / "live" / "home"), "PATH": "/usr/bin:/bin"}
        self._run_count = 0

    def _identity(self) -> str:
        return f"client={self.version} sha256={self.trusted.sha256[:12]}"

    def _fixture(self) -> probe.ProbeFixture:
        self._run_count += 1
        fixture = probe.build_fixture(
            self.root / f"run-{self._run_count:02d}", environ=self.environ
        )
        probe.seed_fixture_project_trust(fixture, fixture.project_dir)
        return fixture

    @staticmethod
    def _write_agent(
        fixture: probe.ProbeFixture,
        name: str,
        *,
        model: str,
        effort: str | None,
        marker: str,
    ) -> None:
        claude_dir = state.ensure_private_dir(fixture.project_dir / ".claude")
        agents = state.ensure_private_dir(claude_dir / "agents")
        effort_line = "" if effort is None else f"effort: {effort}\n"
        body = (
            f"---\nname: {name}\ndescription: client check effort probe agent\n"
            f"model: {model}\n{effort_line}---\nProbe agent body {marker}.\n"
        )
        state.atomic_write(agents / f"{name}.md", body.encode("utf-8"))

    @staticmethod
    def _settings_argv(
        fixture: probe.ProbeFixture, lead: str, fence: tuple[str, ...] | None,
        settings_env: dict[str, str] | None = None,
    ) -> tuple[str, ...]:
        """Production-shaped flag settings: fence = lead + extra selectors
        (none: no fence), always with the managed permission default;
        ``settings_env`` is the scope ``env`` (where managed sessions also
        put CLAUDE_CODE_DISABLE_FAST_MODE)."""

        directory = state.ensure_private_dir(fixture.root / "scope")
        path = directory / "settings.json"
        document: dict[str, Any] = {"permissions": dict(_MANAGED_PERMISSIONS)}
        if settings_env:
            document["env"] = dict(settings_env)
        if fence is not None:
            document.update(availableModels=sorted({lead, *fence}), model=lead)
        state.atomic_write(path, strict_json.canonical_file_bytes(document))
        return ("--settings", str(path))

    def _run(
        self,
        probe_id: str,
        model: str,
        *,
        responder: _SpikeResponder,
        fence: tuple[str, ...] | None = None,
        child_env: dict[str, str] | None = None,
        extra: tuple[str, ...] = (),
        markers: dict[str, str] | None = None,
        fixture: probe.ProbeFixture | None = None,
        pty: bool = False,
        timeout: float = _RUN_TIMEOUT,
        settings_env: dict[str, str] | None = None,
    ) -> tuple[probe.NativeRunResult, list[dict[str, Any]]]:
        fixture = fixture if fixture is not None else self._fixture()
        argv = (
            *(() if pty else ("-p", _TURN)),
            "--model",
            model,
            *self._settings_argv(fixture, model, fence, settings_env),
            *extra,
        )
        provider = _BetaRecordingProvider(responder=responder, content_markers=markers)
        common = {
            "trusted": self.trusted,
            "fixture": fixture,
            "provider": provider,
            "timeout": timeout,
            "allow_real": True,
            "environ": self.environ,
            "child_env": child_env,
        }
        with provider:
            try:
                if pty:
                    prompt = "❯".encode("utf-8")
                    steps = (
                        probe.PTYInteraction(b"Choose", b"2\r"),
                        probe.PTYInteraction(b"Press", b"\r"),
                        probe.PTYInteraction(prompt, _TURN.encode("utf-8") + b"\r"),
                        probe.PTYInteraction(b"PROBE-OK", b"", preserve_after_wait=True),
                        probe.PTYInteraction(prompt, b"/exit\r"),
                    )
                    result = probe.run_native_pty(argv, steps, **common)
                else:
                    result = probe.run_native(argv, **common)
            except ProbeError as exc:
                if _TRIPWIRE in str(exc):
                    print(
                        f"client check {probe_id}: INCONCLUSIVE live daemon domain "
                        "touched (observe-only tripwire; cannot be decided offline)"
                    )
                    self.skipTest(f"BOUNDARY: {probe_id} INCONCLUSIVE: {exc}")
                raise
        self.assertFalse(result.timed_out, f"{probe_id} run timed out")
        self.assertIsNotNone(result.daemon)
        assert result.daemon is not None
        self.assertTrue(result.daemon.unchanged)
        rows = [
            {**row, "betas": provider.beta_names.get(row["ordinal"], ())}
            for row in responder.rows
        ]
        self.assertTrue(rows, f"{probe_id}: the client sent no message request")
        return result, rows


class SelectorRecognitionProbe(_SpikeTestCase):
    """S7: anthropic selectors, bare aliases and refusal fallback."""

    def _bare_aliases(self) -> str:
        observed: dict[str, dict[str, Any]] = {}
        markers = {
            "ANTHROPIC_DEFAULT_OPUS_MODEL": _OPUS_MARKER,
            "ANTHROPIC_DEFAULT_FABLE_MODEL": _FABLE_MARKER,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": _SONNET_MARKER,
        }
        for alias in ("opus", "fable", "sonnet"):
            for label, env in (("plain", None), ("env", markers)):
                _result, rows = self._run(
                    "S7", alias, responder=_SpikeResponder(), child_env=env
                )
                observed[f"{alias}/{label}"] = rows[0]
        # Plain ANTHROPIC_BASE_URL is firstParty: bare aliases request the
        # CURRENT generation, which is the compiled family default's wire
        # (asserted against the catalog-derived default, no literal).
        self.assertEqual(observed["opus/plain"]["model"], _wire(_OPUS))
        self.assertEqual(observed["opus/plain"]["max_tokens"], 128000)
        self.assertFalse(_has_beta(observed["opus/plain"], "context-1m-"))
        self.assertEqual(observed["fable/plain"]["model"], _wire(_FABLE))
        self.assertEqual(observed["sonnet/plain"]["model"], _wire(_SONNET))
        # Each family override retargets only its own alias.
        self.assertEqual(observed["opus/env"]["model"], _OPUS_MARKER)
        self.assertEqual(observed["fable/env"]["model"], _FABLE_MARKER)
        self.assertEqual(observed["sonnet/env"]["model"], _SONNET_MARKER)
        return (
            f"bare opus->{observed['opus/plain']['model']} "
            f"fable->{observed['fable/plain']['model']} "
            f"sonnet->{observed['sonnet/plain']['model']} "
            "(= the compiled family defaults; DEFAULT_OPUS/FABLE/SONNET_MODEL retarget each alias)"
        )

    def _selector_classes(self) -> str:
        observed: dict[str, dict[str, Any]] = {}
        unrecognized: dict[str, bool] = {}
        for selector in (_OPUS, _MULTI_OPUS, _FABLE, _MULTI_FABLE, _SONNET):
            result, rows = self._run("S7", selector, responder=_SpikeResponder())
            observed[selector] = rows[0]
            unrecognized[selector] = _UNRECOGNIZED_MARKER in result.stderr
        for selector, row in observed.items():
            self.assertEqual(row["model"], _wire(selector))
            self.assertEqual(row["thinking_type"], "adaptive")
            # From 2.1.311 the client drops an ineffective [1m]: a canonical
            # id that is 1M on its own sends no 1M beta; an unknown id still
            # needs the beta for its 1M window (client check one-m-suffix).
            self.assertEqual(_has_beta(row, "context-1m-"), selector.startswith("charmer-"), selector)
        canonical_opus, multi_opus = observed[_OPUS], observed[_MULTI_OPUS]
        canonical_fable, multi_fable = observed[_FABLE], observed[_MULTI_FABLE]
        canonical_sonnet = observed[_SONNET]
        # Canonical ids are recognised: catalog output cap, the client's
        # default effort for the model, no warning. (2.1.284+ no longer sends
        # the fallback-credit beta on the request; the refusal fallback is
        # proven by the matrix below.)
        self.assertEqual(canonical_opus["max_tokens"], 128000)
        self.assertEqual(canonical_opus["effort"], "medium")
        self.assertEqual(canonical_fable["max_tokens"], 64000)
        self.assertEqual(canonical_fable["effort"], "high")
        self.assertEqual(canonical_sonnet["max_tokens"], 128000)
        self.assertEqual(canonical_sonnet["effort"], "medium")
        self.assertFalse(unrecognized[_OPUS] or unrecognized[_FABLE] or unrecognized[_SONNET])
        # charmer-* ids are unknown: generic 32000 cap, generic effort,
        # the unrecognized_model stderr marker.
        for selector in (_MULTI_OPUS, _MULTI_FABLE):
            self.assertEqual(observed[selector]["max_tokens"], 32000)
            self.assertEqual(observed[selector]["effort"], "high")
            self.assertTrue(unrecognized[selector])
        # The Fable prompt bundle is only sent for the canonical id.
        self.assertGreater(
            canonical_fable["system_bytes"], multi_fable["system_bytes"] * 3 // 2
        )
        return (
            f"canonical opus max_tokens={canonical_opus['max_tokens']} "
            f"effort={canonical_opus['effort']} fable max_tokens="
            f"{canonical_fable['max_tokens']} effort={canonical_fable['effort']} "
            f"sonnet max_tokens={canonical_sonnet['max_tokens']} effort={canonical_sonnet['effort']}; "
            f"charmer max_tokens={multi_opus['max_tokens']} effort={multi_opus['effort']} "
            "unrecognized_model; system bytes opus "
            f"{canonical_opus['system_bytes']}/{multi_opus['system_bytes']} fable "
            f"{canonical_fable['system_bytes']}/{multi_fable['system_bytes']}"
        )

    def _not_found_chain(self) -> str:
        chains: dict[str, list[int | None]] = {}
        for selector in (_OPUS, _MULTI_OPUS, _FABLE, _MULTI_FABLE):
            wire = _wire(selector)
            result, rows = self._run(
                "S7", selector, responder=_SpikeResponder(not_found=(wire,))
            )
            # No automatic model switch on 404: only same-wire retries, then
            # a visible "issue with the selected model" exit.
            self.assertEqual({row["model"] for row in rows}, {wire}, selector)
            self.assertNotEqual(result.returncode, 0, selector)
            chains[selector] = [row["max_tokens"] for row in rows]
        return (
            "not-found: same-wire retry only (max_tokens "
            f"opus {chains[_OPUS]} multi {chains[_MULTI_OPUS]} fable "
            f"{chains[_FABLE]} multi {chains[_MULTI_FABLE]}), no model switch"
        )

    def _refusal_fallback(self) -> str:
        """The refusal probe on the exact client: the category matrix with the
        compiled family defaults in the env (every managed scope pins
        ANTHROPIC_DEFAULT_OPUS_MODEL) and the production fallback-only fence.
        Through a non-first-party base URL the client lane decides; the
        refused turn is retried once on the same wire, then switches only to
        a fenced route-table target, never implicitly opting into the API's
        server-side fallback (no ``fallbacks`` field on any request)."""

        production = tuple(sorted({wire + "[1m]" for wire in catalog.REFUSAL_FALLBACK_WIRES}))
        self.assertEqual(set(production), {_OPUS48, _OPUS5, _SONNET5})
        cases = (
            # (label, lead, category, fence extras or None, expected target)
            ("opus-cyber-nofence", _OPUS, "cyber", None, "claude-opus-4-8"),
            ("opus-cyber-prod", _OPUS, "cyber", production, "claude-opus-4-8"),
            ("opus-cyber-leadonly", _OPUS, "cyber", (), None),
            ("opus-cyber-plain48", _OPUS, "cyber", ("claude-opus-4-8",), "claude-opus-4-8"),
            ("opus-bio-prod", _OPUS, "bio", production, "claude-opus-5"),
            ("opus-bio-with48", _OPUS, "bio", (_OPUS48,), None),
            ("opus-frontier-prod", _OPUS, "frontier_llm", production, "claude-opus-5"),
            ("opus-extraction-prod", _OPUS, "reasoning_extraction", production, None),
            ("opus-uncategorized", _OPUS, None, production, None),
            ("opus-unknown-category", _OPUS, "check_unknown", production, None),
            ("sonnet-cyber-prod", _SONNET, "cyber", production, "claude-sonnet-5"),
            ("sonnet-cyber-leadonly", _SONNET, "cyber", (), None),
            ("sonnet-frontier-prod", _SONNET, "frontier_llm", production, "claude-sonnet-5"),
            # Biology on Sonnet 5.5: terminal, ZERO fallback requests.
            ("sonnet-bio-prod", _SONNET, "bio", production, None),
            ("sonnet-extraction-prod", _SONNET, "reasoning_extraction", production, None),
            ("fable-cyber-prod", _FABLE, "cyber", production, "claude-opus-4-8"),
            ("fable-cyber-leadonly", _FABLE, "cyber", (), None),
            ("fable-bio-prod", _FABLE, "bio", production, "claude-opus-5"),
            ("fable-frontier-prod", _FABLE, "frontier_llm", production, "claude-opus-5"),
            # From 2.1.312 an unknown id gets the client's default route table
            # too; the capability table a managed scope compiles keeps it on
            # its own model (the refusal-routing check measures every source).
            ("multi-opus-cyber-uncountered", _MULTI_OPUS, "cyber", None, "claude-opus-4-8"),
            ("multi-opus-cyber", _MULTI_OPUS, "cyber", None, None),
        )
        observed: dict[str, str] = {}
        for label, lead, category, fence, expected in cases:
            wire = _wire(lead)
            responder = _SpikeResponder(refuse={wire: category})
            counter = {} if label.endswith("-uncountered") or lead.startswith("claude-") else {
                scope.MODEL_CAPABILITIES_ENV: f"{wire}={scope.REFUSAL_FALLBACK_OFF}"}
            result, rows = self._run("S7", lead, responder=responder, fence=fence, child_env=dict(_FAMILY),
                                     settings_env=counter or None)
            self.assertEqual(rows[0]["model"], wire, label)
            lead_rows = [row for row in rows if row["model"] == wire]
            switched = sorted({row["model"] for row in rows} - {wire})
            self.assertEqual(switched, [] if expected is None else [expected], label)
            self.assertEqual(result.returncode == 0, expected is not None, label)
            if expected is not None:
                self.assertIn(expected, catalog.REFUSAL_FALLBACK_WIRES, label)
                # One same-wire retry of the refused turn precedes the switch.
                self.assertEqual(len(lead_rows), 2, label)
            self.assertEqual(responder.fallbacks_fields, [], label)
            observed[label] = switched[0] if switched else "none"
        # A server-named target (the API lane's usage.iterations
        # fallback_message) outside the route table is never followed through
        # the gateway; the client lane keeps its fenced route-table target.
        responder = _SpikeResponder(refuse={_wire(_OPUS): "cyber"}, server_fallback="claude-check-server-chosen")
        result, rows = self._run("S7", _OPUS, responder=responder, fence=production, child_env=dict(_FAMILY))
        self.assertEqual(sorted({row["model"] for row in rows} - {_wire(_OPUS)}), ["claude-opus-4-8"])
        self.assertEqual(responder.fallbacks_fields, [])
        observed["opus-cyber-server-named"] = "claude-opus-4-8"
        # The user setting switchModelsOnFlag:false disables the switch.
        fixture = self._fixture()
        state.atomic_write(
            fixture.claude_config_dir / "settings.json",
            strict_json.canonical_file_bytes({"switchModelsOnFlag": False}),
        )
        result, rows = self._run(
            "S7",
            _OPUS,
            responder=_SpikeResponder(refuse={_wire(_OPUS): "cyber"}),
            fixture=fixture,
        )
        self.assertEqual({row["model"] for row in rows}, {_wire(_OPUS)})
        self.assertNotEqual(result.returncode, 0)
        observed["switchModelsOnFlag=false"] = "none"
        return (
            "refusal (stop_reason=refusal + stop_details.category; compiled family defaults; "
            "one same-wire retry, no fallbacks opt-in) switch target per case: "
            + " ".join(f"{label}->{target}" for label, target in observed.items())
            + " (a fence entry permits the switch with or without [1m]; a "
            "lead-only fence never switches; server-named targets are not followed; an unknown id "
            "switches unless the capability table turns it off)"
        )

    def test_s7_selector_recognition(self) -> None:
        details = [
            self._bare_aliases(),
            self._selector_classes(),
            self._not_found_chain(),
            self._refusal_fallback(),
        ]
        print(
            f"client check S7: PASS {self._identity()} canonical anthropic "
            "selectors recognised, charmer-* unknown; " + "; ".join(details)
        )


class _ModelLimitsHandler(probe._ProviderHandler):
    """Advertise discriminating limits on the fixture's Models API only."""

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/v1/models" or path.startswith("/v1/models/"):
            provider = self.server.provider
            provider.limit_queries.append(path)
            models = [
                {"id": _wire(selector), "type": "model",
                 "display_name": "fixture", "created_at": "2026-01-01T00:00:00Z",
                 "max_input_tokens": 300000, "max_tokens": 16000}
                for selector in (_OPUS, _SONNET)
            ]
            payload = ({"data": models, "has_more": False,
                        "first_id": models[0]["id"], "last_id": models[-1]["id"]}
                       if path == "/v1/models" else
                       next((row for row in models if path.endswith("/" + row["id"])), models[0]))
            self._send_json(200, payload)
            return
        super().do_GET()


class SelectorLimitsProbe(_SpikeTestCase):
    """Canonical suffixless/[1m] limits on the first turn, fresh or switched."""

    def _case(self, selector, *, switched):
        fixture = self._fixture()
        lead = _FABLE if switched else selector
        settings_argv = self._settings_argv(fixture, lead, (_OPUS, _wire(_OPUS), _FABLE))
        settings_path = Path(settings_argv[1])
        settings = strict_json.load(settings_path)
        settings["modelPicker"] = {
            "options": [{"model": model, "label": f"CHECKS7MODEL{index}", "description": "fixture"}
                        for index, model in enumerate((_wire(_OPUS), _OPUS, _FABLE))],
            "replaceBuiltInOptions": True,
        }
        # A fixture status line reports only model/window metadata, so the
        # in-process switch is measured without reading a transcript or
        # inferring the window from a beta header.
        window_path = fixture.root / "window.json"
        script = fixture.root / "window.py"
        state.atomic_write(script, (
            "import json, pathlib, sys\n"
            "data = json.load(sys.stdin)\n"
            "row = {'model': data.get('model', {}).get('id'), "
            "'window': data.get('context_window', {}).get('context_window_size')}\n"
            "pathlib.Path(sys.argv[1]).write_text(json.dumps(row))\n"
            "print('CHECKS7-WINDOW')\n"
        ).encode())
        settings["statusLine"] = {"type": "command", "command": " ".join(
            shlex.quote(str(part)) for part in (sys.executable, script, window_path))}
        state.atomic_write(settings_path, strict_json.canonical_file_bytes(settings))
        prompt = "❯".encode()
        steps = [probe.PTYInteraction(b"Choose", b"2\r"),
                 probe.PTYInteraction(b"Press", b"\r")]
        if switched:
            # Select another canonical model before the first turn, so this
            # measures the first request after /model, not a cached later one.
            steps.append(probe.PTYInteraction(prompt, f"/model {selector}\r".encode(),
                                              before_send=lambda: time.sleep(0.5)))
        steps.extend([
            probe.PTYInteraction(prompt, b"CHECKS7-TURN\r", before_send=lambda: time.sleep(0.5)),
            probe.PTYInteraction(b"PROBE-OK", b"", preserve_after_wait=True),
        ])
        window = {}

        def capture_window():
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    row = json.loads(window_path.read_bytes())
                except (OSError, ValueError):
                    row = {}
                # From 2.1.311 the status line names a canonical 1M id
                # without its ineffective [1m].
                if row.get("model") == _wire(selector) and isinstance(row.get("window"), int):
                    window.update(row)
                    return
                time.sleep(0.05)
            self.fail(f"status line did not report the selected model/window: {row}")

        steps.append(probe.PTYInteraction(prompt, b"/exit\r", before_send=capture_window))
        responder = _SpikeResponder()
        provider = _BetaRecordingProvider(responder=responder)
        provider.limit_queries = []
        with mock.patch.object(probe, "_ProviderHandler", _ModelLimitsHandler), provider:
            result = probe.run_native_pty(
                ("--model", lead, *settings_argv), steps,
                trusted=self.trusted, fixture=fixture, provider=provider,
                timeout=_RUN_TIMEOUT, allow_real=True, environ=self.environ,
            )
        self.assertEqual(result.returncode, 0)
        self.assertFalse(result.timed_out)
        self.assertTrue(result.daemon.unchanged)
        rows = [row for row in responder.rows if row["model"] == _wire(selector) and row["tool_count"]]
        self.assertTrue(rows, "selected model sent no turn")
        first = rows[0]
        return {"window": window["window"], "max_tokens": first["max_tokens"],
                "long_context": any(name.startswith("context-1m-")
                                    for name in provider.beta_names[first["ordinal"]]),
                "limit_queries": provider.limit_queries}

    def test_s7_canonical_limits_fresh_and_after_switch(self):
        observed = {}
        for selector in (_wire(_OPUS), _OPUS):
            for switched in (False, True):
                label = f"{selector}/{'switch' if switched else 'fresh'}"
                observed[label] = self._case(selector, switched=switched)
        # This custom endpoint does not get Models API lookups: advertised
        # 300K/16K limits do not replace the client's 1M/128K defaults. From
        # 2.1.311 the [1m] suffix changes nothing on the canonical Opus id
        # (2.1.310 added the beta for it): same window, no beta.
        for selector in (_wire(_OPUS), _OPUS):
            for kind in ("fresh", "switch"):
                self.assertEqual(observed[f"{selector}/{kind}"], {
                    "window": 1000000,
                    "max_tokens": 128000, "long_context": False,
                    "limit_queries": [],
                })
        print(f"client check S7-limits: PASS {self._identity()} {observed}")


class SafetySwitchTests(_SpikeTestCase):
    """The refusal probe and the 2.1.282-2.1.286 release-note refusal shapes on the
    exact client, offline, with the compiled family defaults (every managed
    scope pins ANTHROPIC_DEFAULT_OPUS/SONNET_MODEL) and the production
    fallback-only fence. Records (a) any implicit server-side fallback
    opt-in (a ``fallbacks`` body field; the post-refusal
    ``fallback-credit-*`` beta is recorded separately), (b) the next target
    and (c) the fence outcome: a fenced fallback-only switch or a visible
    refusal (non-zero exit), never a silent run on the lead."""

    _SERVER_OUT = "claude-check-server-chosen"

    def _case(self, label, lead, category, expected, *, fence=None, server=None,
              extra=(), settings_env=None, user_settings=None):
        production = tuple(sorted({wire + "[1m]" for wire in catalog.REFUSAL_FALLBACK_WIRES}))
        fence = production if fence is None else fence
        wire = _wire(lead)
        fixture = None
        if user_settings is not None:
            fixture = self._fixture()
            state.atomic_write(fixture.claude_config_dir / "settings.json",
                               strict_json.canonical_file_bytes(user_settings))
        responder = _SpikeResponder(refuse={wire: category}, server_fallback=server)
        result, rows = self._run("refusal", lead, responder=responder, fence=fence, child_env=dict(_FAMILY),
                                 extra=extra, fixture=fixture, settings_env=settings_env)
        lead_rows = [row for row in rows if row["model"] == wire]
        switched = [row for row in rows if row["model"] != wire]
        self.assertEqual(rows[0]["model"], wire, label)
        # (a) no implicit server-side fallback opt-in, no credit beta up front.
        self.assertEqual(responder.fallbacks_fields, [], label)
        self.assertFalse(any("fallback" in name for name in rows[0]["betas"]), label)
        # One same-wire retry of the refused turn, never more lead requests.
        self.assertEqual(len(lead_rows), 2, label)
        # (b) next target; (c) fence outcome.
        self.assertEqual(sorted({row["model"] for row in switched}), [] if expected is None else [expected], label)
        if expected is None:
            self.assertNotEqual(result.returncode, 0, f"{label}: refusal must stay visible")
            return label, "visible-refusal", rows
        self.assertEqual(result.returncode, 0, label)
        self.assertIn(expected, catalog.REFUSAL_FALLBACK_WIRES, label)
        self.assertTrue({expected, expected + "[1m]"} & set(fence), f"{label}: target outside the fence")
        return label, f"{expected}(fallback-only)", rows

    def _credit_beta(self, rows) -> bool:
        return all(any(name.startswith("fallback-credit-") for name in row["betas"]) for row in rows[1:])

    def test_api_chosen_target_with_compiled_opus_pin(self) -> None:
        out = self._SERVER_OUT
        cases = (
            ("opus-cyber-server-out", _OPUS, "cyber", "claude-opus-4-8", {"server": out}),
            ("opus-cyber-server-in-set", _OPUS, "cyber", "claude-opus-4-8", {"server": _wire(_SONNET5)}),
            ("opus-bio-server-out", _OPUS, "bio", "claude-opus-5", {"server": out}),
            ("opus-cyber-leadonly-server", _OPUS, "cyber", None, {"server": out, "fence": ()}),
            ("sonnet-cyber-server-out", _SONNET, "cyber", "claude-sonnet-5", {"server": out}),
            ("sonnet-bio-server-out", _SONNET, "bio", None, {"server": out}),
        )
        observed, credit = [], []
        for label, lead, category, expected, kwargs in cases:
            label, outcome, rows = self._case(label, lead, category, expected, **kwargs)
            observed.append(f"{label}->{outcome}")
            credit.append(self._credit_beta(rows))
        print(f"client check refusal: PASS {self._identity()} API-named targets never followed (in or out of the "
              f"route table); client route table + fence decide: " + " ".join(observed)
              + f"; no fallbacks field; fallback-credit beta on every post-refusal request={all(credit)}, "
              "never on the first")

    def test_refusal_category_and_nonfast_fallback_matrix(self) -> None:
        nonfast = {scope.FAST_MODE_ENV: "1"}
        cases = (
            ("nonfast-cyber", _OPUS, "cyber", "claude-opus-4-8", {}),
            ("nonfast-bio", _OPUS, "bio", "claude-opus-5", {}),
            ("nonfast-frontier", _OPUS, "frontier_llm", "claude-opus-5", {}),
            ("nonfast-extraction", _OPUS, "reasoning_extraction", None, {}),
            ("nonfast-uncategorized", _OPUS, None, None, {}),
            ("nonfast-cyber-fallback-model", _OPUS, "cyber", "claude-opus-4-8", {"extra": ("--fallback-model", _SONNET)}),
            ("nonfast-uncategorized-fallback-model", _OPUS, None, None, {"extra": ("--fallback-model", _SONNET)}),
            ("nonfast-sonnet-frontier", _SONNET, "frontier_llm", "claude-sonnet-5", {}),
        )
        observed = []
        for label, lead, category, expected, kwargs in cases:
            label, outcome, rows = self._case(label, lead, category, expected, settings_env=nonfast, **kwargs)
            observed.append(f"{label}->{outcome}")
        # 2.1.282: the safety switch at xhigh keeps the effort; with thinking
        # off (settings or MAX_THINKING_TOKENS=0) the switched turn sends
        # thinking disabled.
        label, outcome, rows = self._case("xhigh-cyber", _OPUS, "cyber", "claude-opus-4-8", extra=("--effort", "xhigh"))
        self.assertEqual({row["effort"] for row in rows}, {"xhigh"})
        observed.append(f"{label}->{outcome}(effort xhigh kept)")
        for label, kwargs in (("thinking-off-settings", {"user_settings": {"alwaysThinkingEnabled": False}}),
                              ("thinking-off-env", {"settings_env": {"MAX_THINKING_TOKENS": "0"}})):
            label, outcome, rows = self._case(label, _OPUS, "cyber", "claude-opus-4-8", **kwargs)
            self.assertNotIn(rows[-1]["thinking_type"], ("adaptive", "enabled"), label)
            observed.append(f"{label}->{outcome}(thinking {rows[-1]['thinking_type']})")
        print(f"client check refusal-switches: PASS {self._identity()} non-fast (scope env {scope.FAST_MODE_ENV}=1) "
              "categories, --fallback-model ignored for refusals, xhigh and thinking-off switches: "
              + " ".join(observed))

    def test_sonnet_subagent_under_non_anthropic_lead_set(self) -> None:
        """Refusal case: a non-Anthropic lead set compiles no fallback-only
        selectors (``scope.fallback_only``), so a refused Sonnet subagent
        turn stays a visible refusal; it never re-runs on the lead model.
        With the fallback-only set fenced it switches to its route target."""

        production = tuple(sorted({wire + "[1m]" for wire in catalog.REFUSAL_FALLBACK_WIRES}))
        observed = []
        for label, category, fence, expected in (
            ("gpt-lead-cyber-no-fallback-set", "cyber", (_SONNET,), None),
            ("gpt-lead-bio-no-fallback-set", "bio", (_SONNET,), None),
            ("gpt-lead-cyber-fallback-set", "cyber", (_SONNET, *production), "claude-sonnet-5"),
        ):
            fixture = self._fixture()
            self._write_agent(fixture, "cm-safety", model=_SONNET, effort="high", marker="check-safety")
            responder = _SpikeResponder(agents=("cm-safety",), refuse={_wire(_SONNET): category})
            result, rows = self._run("refusal", _LEAD_GPT, responder=responder, fence=fence,
                                     child_env={**_FAMILY, **_HINT_ENV}, fixture=fixture)
            child = [row["model"] for row in rows if row["request_class"] == "subagent"]
            self.assertEqual(child[:2], [_wire(_SONNET)] * 2, label)
            self.assertEqual(sorted(set(child) - {_wire(_SONNET)}), [] if expected is None else [expected], label)
            self.assertNotIn(_wire(_LEAD_GPT), child, label)
            self.assertEqual(responder.fallbacks_fields, [], label)
            self.assertEqual(result.returncode, 0, label)
            observed.append(f"{label}->{expected or 'visible-agent-refusal'}")
        print(f"client check refusal-subagent: PASS {self._identity()} " + " ".join(observed)
              + " (a subagent refusal never runs on the lead)")


class AgentEffortProbe(_SpikeTestCase):
    """S8: agent frontmatter effort on the subagent's wire."""

    _EFFORTS = ("none", "ultracode", "low", "medium", "high", "xhigh", "max")

    def test_s8_agent_effort_vocabulary(self) -> None:
        fixture = self._fixture()
        markers: dict[str, str] = {}
        agents: list[str] = []
        for effort in self._EFFORTS:
            name = f"client-check-effort-{effort}"
            marker = f"CLIENT-CHECK-S8-{effort.upper()}-MARK"
            self._write_agent(
                fixture,
                name,
                model=_OPUS,
                effort=None if effort == "none" else effort,
                marker=marker,
            )
            markers[effort] = marker
            agents.append(name)
        # Lead at session effort ultracode (-> xhigh wire) so an agent that
        # inherits is distinguishable from the model default (medium).
        result, rows = self._run(
            "S8",
            _OPUS,
            responder=_SpikeResponder(agents=tuple(agents)),
            extra=("--effort", "ultracode", "--dangerously-skip-permissions"),
            markers=markers,
            fixture=fixture,
        )
        lead = rows[0]
        self.assertEqual(lead["effort"], "xhigh")
        wire: dict[str, dict[str, Any]] = {}
        for effort in self._EFFORTS:
            tagged = [row for row in rows if row["markers"] == (effort,)]
            self.assertEqual(len(tagged), 1, f"agent effort {effort} request")
            wire[effort] = tagged[0]
            self.assertEqual(tagged[0]["model"], "claude-opus-5-5")
            self.assertEqual(tagged[0]["thinking_type"], "adaptive")
        # Valid frontmatter efforts reach the wire verbatim; ultracode is
        # rejected by the loader (debug-log only) and silently inherits the
        # session effort like "none".
        for effort in ("low", "medium", "high", "xhigh", "max"):
            self.assertEqual(wire[effort]["effort"], effort)
        self.assertEqual(wire["ultracode"]["effort"], lead["effort"])
        self.assertEqual(wire["none"]["effort"], lead["effort"])
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("invalid effort", result.stderr)
        summary = " ".join(f"{effort}->{wire[effort]['effort']}" for effort in self._EFFORTS)
        print(
            f"client check S8: PASS {self._identity()} lead --effort ultracode "
            f"wire={lead['effort']}; agent frontmatter {summary} "
            "(ultracode rejected silently: debug-log only, inherits session effort)"
        )

    def test_s8_explicit_agent_effort_precedence(self) -> None:
        fixture = self._fixture()
        cases = {
            "omitted": ("low", None),
            "high": ("low", "high"),
            "max": ("low", "max"),
            "no-frontmatter": (None, "high"),
        }
        markers, agents, explicit = {}, [], {}
        for label, (frontmatter, argument) in cases.items():
            name = f"client-check-explicit-{label}"
            marker = f"CLIENT-CHECK-S8-EXPLICIT-{label.upper()}-MARK"
            self._write_agent(fixture, name, model=_OPUS, effort=frontmatter, marker=marker)
            markers[label] = marker
            agents.append(name)
            if argument is not None:
                explicit[name] = argument
        result, rows = self._run(
            "S8-explicit", _OPUS,
            responder=_SpikeResponder(agents=tuple(agents), agent_efforts=explicit),
            extra=("--effort", "medium", "--dangerously-skip-permissions"),
            fixture=fixture, markers=markers,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(rows[0]["effort"], "medium")
        observed = {}
        for label, (frontmatter, argument) in cases.items():
            tagged = [row for row in rows if row["markers"] == (label,)]
            self.assertEqual(len(tagged), 1, label)
            self.assertEqual(tagged[0]["model"], _wire(_OPUS), label)
            self.assertEqual(tagged[0]["thinking_type"], "adaptive", label)
            observed[label] = tagged[0]["effort"]
        self.assertEqual(observed, {
            "omitted": "low", "high": "high", "max": "max", "no-frontmatter": "high",
        })
        print(f"client check S8-explicit: PASS {self._identity()} session=medium "
              f"frontmatter=low; Agent argument wire={observed}")


class NativeAgentWireProbe(_SpikeTestCase):
    """S9: native helper wires under non-Opus leads and fences."""

    def _helper_wire(
        self,
        helper: str,
        lead: str,
        *,
        cap_off: bool,
        fence: tuple[str, ...] | None = None,
        opus_marker: bool = False,
        pty: bool = False,
    ) -> str | None:
        env = dict(_HINT_ENV)
        if cap_off:
            env[_EXPLORE_CAP_OFF] = "1"
        if opus_marker:
            env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = _OPUS_MARKER
        extra = () if pty else ("--dangerously-skip-permissions",)
        def run_helper(*, timeout=_RUN_TIMEOUT):
            # A retry gets a fresh fixture, provider and scripted responder.
            return self._run(
                "S9", lead, responder=_SpikeResponder(agents=(helper,)),
                fence=fence, child_env=env, extra=extra, pty=pty, timeout=timeout,
            )

        if pty:
            (_result, rows), retried = _retry_s9_pty(run_helper)
            self._pty_retried = getattr(self, "_pty_retried", False) or retried
        else:
            _result, rows = run_helper()
        helper_rows = [row for row in rows if row["agent_type"] == helper]
        self.assertTrue(helper_rows, f"{helper} was never spawned (lead {lead})")
        self.assertTrue(all(row["request_class"] == "subagent" for row in helper_rows))
        models = {row["model"] for row in helper_rows}
        self.assertEqual(len(models), 1, f"{helper} used several wires")
        return models.pop()

    def _explore(self) -> dict[str, str | None]:
        lead_wire = _LEAD_GPT
        observed = {
            "gpt": self._helper_wire("Explore", _LEAD_GPT, cap_off=False),
            "gpt+off": self._helper_wire("Explore", _LEAD_GPT, cap_off=True),
            "gpt+marker": self._helper_wire(
                "Explore", _LEAD_GPT, cap_off=False, opus_marker=True
            ),
            "gpt+marker+off": self._helper_wire(
                "Explore", _LEAD_GPT, cap_off=True, opus_marker=True
            ),
            "gpt fence+opus": self._helper_wire(
                "Explore", _LEAD_GPT, cap_off=False, fence=(_OPUS,)
            ),
            "gpt fence lead-only": self._helper_wire(
                "Explore", _LEAD_GPT, cap_off=False, fence=()
            ),
            "fable": self._helper_wire("Explore", _FABLE, cap_off=False),
            "fable+off": self._helper_wire("Explore", _FABLE, cap_off=True),
            "sonnet": self._helper_wire("Explore", _SONNET, cap_off=False),
        }
        # 2.1.284+ (re-measured on the pin): Explore under an
        # unrecognized proxy lead (GPT) inherits the lead whatever the cap
        # flag or the Opus default (C24 no longer holds for it). A recognized
        # non-Opus Anthropic lead (Fable) is still capped to the Opus family
        # default unless the cap flag is set (the production default, so the
        # compiled Explore override stays); Sonnet keeps its own model.
        for label in ("gpt", "gpt+off", "gpt+marker", "gpt+marker+off", "gpt fence+opus",
                      "gpt fence lead-only"):
            self.assertEqual(observed[label], lead_wire, label)
        self.assertEqual(observed["fable"], _wire(_OPUS))
        self.assertEqual(observed["fable+off"], _wire(_FABLE))
        self.assertEqual(observed["sonnet"], _wire(_SONNET))
        return observed

    def _statusline(self) -> dict[str, str | None]:
        observed = {
            "nofence": self._helper_wire("statusline-setup", _LEAD_GPT, cap_off=True),
            "fence+sonnet": self._helper_wire(
                "statusline-setup", _LEAD_GPT, cap_off=True, fence=(_SONNET,)
            ),
            "fence+opus": self._helper_wire(
                "statusline-setup", _LEAD_GPT, cap_off=True, fence=(_OPUS,)
            ),
        }
        # Pinned "sonnet" resolves to the current Sonnet wire, which is the
        # compiled Sonnet family default, when it is fenced (a
        # cross-provider Anthropic run under a GPT lead) and falls back to
        # the lead when no Sonnet selector is in the fence.
        self.assertEqual(observed["nofence"], _wire(_SONNET))
        self.assertEqual(observed["fence+sonnet"], _wire(_SONNET))
        self.assertEqual(observed["fence+opus"], _LEAD_GPT)
        return observed

    def _guide(self) -> dict[str, str | None]:
        # claude-code-guide is not registered for the sdk-cli entrypoint
        # (headless -p), so it is driven through the interactive PTY.
        observed = {
            "nofence": self._helper_wire(
                "claude-code-guide", _LEAD_GPT, cap_off=True, pty=True
            ),
            "fence+sonnet": self._helper_wire(
                "claude-code-guide", _LEAD_GPT, cap_off=True, fence=(_SONNET,), pty=True
            ),
        }
        # Unfenced, the guide's "haiku" is the client's default Haiku: Haiku
        # 5.5 since 2.1.293 (re-measured on 2.1.302; 4.5 before). A fence
        # without a Haiku selector (every compiled scope: the catalog has no
        # Haiku line) still runs it on the lead.
        self.assertTrue(str(observed["nofence"]).startswith("claude-haiku-5-5"))
        self.assertEqual(observed["fence+sonnet"], _LEAD_GPT)
        return observed

    def test_s9_native_agent_wires(self) -> None:
        explore = self._explore()
        statusline = self._statusline()
        guide = self._guide()

        def fmt(values: dict[str, str | None]) -> str:
            return ", ".join(f"{key}->{value}" for key, value in values.items())

        print(
            f"client check S9: PASS {self._identity()} Explore [{fmt(explore)}]; "
            f"statusline-setup [{fmt(statusline)}]; claude-code-guide "
            f"[{fmt(guide)}]; no errors, SPEC 1.3 table holds with "
            f"{_EXPLORE_CAP_OFF}=1"
            + (" (retried after a PTY timeout)"
               if getattr(self, "_pty_retried", False) else "")
        )


# The client's own default Haiku (a client fact, not a catalog line: the
# compiled family defaults name Fable, Opus and Sonnet only).
_HAIKU = "claude-haiku-5-5"
_HAIKU_MARKER = "client-check-marker-haiku"


def _measured_turn(case: _SpikeTestCase, selector: str, *, fence: tuple[str, ...] | None,
                   child_env: dict[str, str] | None = None) -> dict[str, Any]:
    """One headless turn on ``selector``: the wire, the 1M beta, the output
    cap and the context window the client books (``modelUsage``)."""

    result, rows = case._run("1M", selector, responder=_SpikeResponder(), fence=fence,
                             child_env=child_env, extra=("--output-format", "json"))
    main = [row for row in rows if row["tool_count"]] or rows
    try:
        usage = json.loads(result.stdout or "{}").get("modelUsage") or {}
    except ValueError:
        usage = {}
    return {"rc": result.returncode, "wire": main[0]["model"], "beta": _has_beta(main[0], "context-1m-"),
            "max_tokens": main[0]["max_tokens"],
            "window": sorted({value.get("contextWindow") for value in usage.values()
                              if isinstance(value, dict) and isinstance(value.get("contextWindow"), int)})}


class OneMillionSuffixProbe(_SpikeTestCase):
    """What the ``[1m]`` suffix does on the pin, per selector kind.

    Claude Code 2.1.311 drops the suffix from a model's name and id where it
    changes nothing. The compiled selectors keep it on every 1M line; what
    the lineup stands on is that each of them still books the 1M window (the
    compaction and window accounting) and reaches the wire without the
    suffix. The 1M beta is recorded per selector, not required: a canonical
    id that is 1M on its own needs none.
    """

    def test_every_compiled_1m_selector_books_1m(self) -> None:
        cases = {selector: (selector,) for family in (_OPUS, _FABLE, _SONNET, _MULTI_OPUS)
                 for selector in (family, _wire(family))}
        cases.update({_HAIKU: (_HAIKU,), f"{_HAIKU}[1m]": (f"{_HAIKU}[1m]",)})
        observed = {selector: _measured_turn(self, selector, fence=fence) for selector, fence in cases.items()}
        for selector, row in observed.items():
            self.assertEqual(row["rc"], 0, selector)
            self.assertEqual(row["wire"], _wire(selector), selector)
        # Every compiled 1M selector books 1M.
        for selector in (_OPUS, _FABLE, _SONNET, _MULTI_OPUS):
            self.assertEqual(observed[selector]["window"], [1000000], selector)
        # An unknown id is 200K without the suffix and needs the beta for 1M.
        self.assertEqual(observed[_wire(_MULTI_OPUS)]["window"], [200000])
        self.assertTrue(observed[_MULTI_OPUS]["beta"])
        self.assertFalse(observed[_wire(_MULTI_OPUS)]["beta"])
        # A canonical id is 1M on its own: with or without the suffix the
        # request is the same (2.1.310 still added the beta for the suffix).
        for family in (_OPUS, _FABLE, _SONNET, f"{_HAIKU}[1m]"):
            self.assertEqual(observed[family], observed[_wire(family)], family)
            self.assertFalse(observed[family]["beta"], family)
        detail = " ".join(
            f"{selector}->{row['wire']}:window={'/'.join(map(str, row['window'])) or '?'}"
            f"{'+1m-beta' if row['beta'] else ''}:max={row['max_tokens']}"
            for selector, row in observed.items())
        print(f"client check one-m-suffix: PASS class=1M-booked {detail} {self._identity()}")


class HaikuNativeProbe(_SpikeTestCase):
    """The client-native Haiku, measured without the compile's
    ``ANTHROPIC_DEFAULT_HAIKU_MODEL`` (the compile states it; the compiled value is
    checked in ``tests/test_compiler_env_probe.py``).

    Measured: the ``haiku`` alias and ``ANTHROPIC_DEFAULT_HAIKU_MODEL`` on the
    lead, the native ``claude-code-guide`` agent (the client's own Haiku
    user) under fences with and without Anthropic routes and with a Haiku
    selector (with and without ``[1m]``), the client's auxiliary requests in a
    terminal session (they follow the variable, never the fence alone, even to
    a model outside the fence named in the user's settings env; the variable
    in the flag settings env outranks it), and the window ``claude-haiku-5-5``
    books without ``[1m]``. The line is
    ``client check haiku: MEASURED <detail>``; the assertions pin each
    observation.
    """

    def _guide(self, lead: str, fence: tuple[str, ...] | None, env: dict[str, str] | None = None) -> str | None:
        child_env = {**_HINT_ENV, _EXPLORE_CAP_OFF: "1", **(env or {})}

        def run_helper(*, timeout=_RUN_TIMEOUT):
            return self._run("haiku", lead, responder=_SpikeResponder(agents=("claude-code-guide",)),
                             fence=fence, child_env=child_env, pty=True, timeout=timeout)

        (_result, rows), _retried = _retry_s9_pty(run_helper)
        helper = [row for row in rows if row["agent_type"] == "claude-code-guide"]
        self.assertTrue(helper, f"claude-code-guide was never spawned (lead {lead}, fence {fence})")
        models = {row["model"] for row in helper}
        self.assertEqual(len(models), 1)
        return models.pop()

    def _background(self, lead: str, fence: tuple[str, ...], env: dict[str, str] | None = None, *,
                    user_env: dict[str, str] | None = None, flag_env: dict[str, str] | None = None
                    ) -> dict[str, list[str]]:
        """A terminal turn: the models of the client's own requests that are
        not the lead's turn (by request class). ``user_env`` is the user's
        settings env (a layer a managed launch does not scrub), ``flag_env``
        the flag settings env (where a managed session's compiled env lives)."""

        def run_turn(*, timeout=_RUN_TIMEOUT):
            fixture = self._fixture()
            if user_env is not None:
                state.atomic_write(fixture.claude_config_dir / "settings.json",
                                   strict_json.canonical_file_bytes({"env": dict(user_env)}))
            return self._run("haiku", lead, responder=_SpikeResponder(), fence=fence,
                             child_env={**_HINT_ENV, **(env or {})}, pty=True, timeout=timeout,
                             fixture=fixture, settings_env=flag_env)

        (_result, rows), _retried = _retry_s9_pty(run_turn)
        classes: dict[str, set[str]] = {}
        for row in rows:
            if row["request_class"] not in (None, "main"):
                classes.setdefault(str(row["request_class"]), set()).add(str(row["model"]))
        return {name: sorted(models) for name, models in sorted(classes.items())}

    def test_haiku_is_client_native(self) -> None:
        anthropic = (_OPUS, _FABLE, _SONNET)
        lead = {
            "alias": _measured_turn(self, "haiku", fence=None),
            "alias+fence": _measured_turn(self, "haiku", fence=anthropic),
            "alias+env": _measured_turn(self, "haiku", fence=None,
                                        child_env={"ANTHROPIC_DEFAULT_HAIKU_MODEL": _HAIKU_MARKER}),
            "id": _measured_turn(self, _HAIKU, fence=(_HAIKU,)),
        }
        guide = {
            "opus-fence-anthropic-no-haiku": self._guide(_OPUS, anthropic),
            "opus-fence-with-haiku": self._guide(_OPUS, (*anthropic, _HAIKU)),
            "opus-fence-with-alias": self._guide(_OPUS, (*anthropic, "haiku")),
            # The shape a compiled 1M Anthropic line would take: [1m] in the fence.
            "opus-fence-with-haiku-1m": self._guide(_OPUS, (*anthropic, f"{_HAIKU}[1m]")),
            "env-haiku-1m-fenced": self._guide(_OPUS, (*anthropic, f"{_HAIKU}[1m]"),
                                               {"ANTHROPIC_DEFAULT_HAIKU_MODEL": f"{_HAIKU}[1m]"}),
            "env-marker-unfenced": self._guide(_OPUS, None, {"ANTHROPIC_DEFAULT_HAIKU_MODEL": _HAIKU_MARKER}),
            "env-marker-fenced": self._guide(_OPUS, (*anthropic, _HAIKU_MARKER),
                                             {"ANTHROPIC_DEFAULT_HAIKU_MODEL": _HAIKU_MARKER}),
        }
        # The client's own background requests: on the lead, with or without
        # Haiku in the fence; a Haiku variable moves them, inside the fence or
        # not, also from the user's settings env; the same variable in the
        # flag settings env, naming the lead, keeps them there.
        marker = {"ANTHROPIC_DEFAULT_HAIKU_MODEL": _HAIKU_MARKER}
        background = {
            "opus-fence-no-haiku": self._background(_OPUS, ()),
            "opus-fence-with-haiku": self._background(_OPUS, (_HAIKU,)),
            "gpt-fence-with-haiku": self._background(_LEAD_GPT, (_HAIKU,)),
            "opus-fence-with-haiku+variable": self._background(
                _OPUS, (_HAIKU,), {"ANTHROPIC_DEFAULT_HAIKU_MODEL": _HAIKU}),
            "opus-fence-no-haiku+user-variable": self._background(_OPUS, (), user_env=marker),
            "gpt-fence-no-haiku+user-variable": self._background(_LEAD_GPT, (), user_env=marker),
            "opus-fence-no-haiku+user-variable+flag-lead": self._background(
                _OPUS, (), user_env=marker, flag_env={"ANTHROPIC_DEFAULT_HAIKU_MODEL": _OPUS}),
        }
        for label, row in lead.items():
            self.assertEqual(row["rc"], 0, label)
        # The alias is the client's current Haiku through the custom base URL,
        # with or without Anthropic routes in the fence; the variable retargets
        # it. claude-haiku-5-5 books 1M without [1m].
        self.assertEqual(lead["alias"]["wire"], _HAIKU)
        self.assertEqual(lead["alias+fence"]["wire"], _HAIKU)
        self.assertEqual(lead["alias+env"]["wire"], _HAIKU_MARKER)
        self.assertEqual([lead[label]["window"] for label in ("alias", "alias+fence", "id")], [[1000000]] * 3)
        self.assertFalse(any(row["beta"] for row in lead.values()))
        # The client's own Haiku user runs on Haiku only when the fence admits
        # it (by id or alias), else on the lead; the variable retargets it too.
        self.assertEqual(guide, {
            "opus-fence-anthropic-no-haiku": _wire(_OPUS), "opus-fence-with-haiku": _HAIKU,
            "opus-fence-with-alias": _HAIKU, "opus-fence-with-haiku-1m": _HAIKU, "env-haiku-1m-fenced": _HAIKU,
            "env-marker-unfenced": _HAIKU_MARKER, "env-marker-fenced": _HAIKU_MARKER})
        # The client's auxiliary requests follow the variable, not the fence:
        # a model outside the fence named in the user's settings env gets them.
        self.assertEqual(background, {
            "opus-fence-no-haiku": {"auxiliary": [_wire(_OPUS)]},
            "opus-fence-with-haiku": {"auxiliary": [_wire(_OPUS)]},
            "gpt-fence-with-haiku": {"auxiliary": [_LEAD_GPT]},
            "opus-fence-with-haiku+variable": {"auxiliary": [_HAIKU]},
            "opus-fence-no-haiku+user-variable": {"auxiliary": [_HAIKU_MARKER]},
            "gpt-fence-no-haiku+user-variable": {"auxiliary": [_HAIKU_MARKER]},
            "opus-fence-no-haiku+user-variable+flag-lead": {"auxiliary": [_wire(_OPUS)]}})
        detail = ("lead " + " ".join(
            f"{label}->{row['wire']}:window={'/'.join(map(str, row['window'])) or '?'}"
            f"{'+1m-beta' if row['beta'] else ''}" for label, row in lead.items())
            + "; claude-code-guide " + " ".join(f"{label}->{model}" for label, model in guide.items())
            + "; background requests " + " ".join(f"{label}->{row}" for label, row in background.items()))
        print(f"client check haiku: MEASURED {detail} {self._identity()}", flush=True)


# ---------------------------------------------------- refusal routing matrix

# The switches charmer's policy specifies: the client's categorized-refusal
# route table for the canonical Anthropic sources (U23). Every other source a
# lineup can bind (a charmer-* alias, a gateway selector of another family, an
# Anthropic line served by another provider, Haiku) has the specified outcome
# "no switch" (U17): a refused turn stays a visible refusal on its own model.
_REFUSAL_ROUTES = {
    _wire(_OPUS): {"cyber": "claude-opus-4-8", "bio": "claude-opus-5", "frontier_llm": "claude-opus-5"},
    _wire(_FABLE): {"cyber": "claude-opus-4-8", "bio": "claude-opus-5", "frontier_llm": "claude-opus-5"},
    _wire(_SONNET): {"cyber": "claude-sonnet-5", "frontier_llm": "claude-sonnet-5"},
}
_REFUSAL_CATEGORIES = ("cyber", "bio", "frontier_llm", "reasoning_extraction", None, "check_unknown")
_DISABLE_REFUSAL_FALLBACK = "CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK"
_MODEL_CAPABILITIES = "CLAUDE_CODE_MODEL_CAPABILITIES"
# Real selectors of the shipped catalog, by kind (every one is in the
# compiled fence of every shipped seed).
_ROUTING_SOURCES = {
    "opus": _OPUS,
    "fable": _FABLE,
    "sonnet": _SONNET,
    "haiku": f"{_HAIKU}[1m]",
    "or-opus": "charmer-or-opus-xhigh[1m]",
    "astra": "gpt-multi-astra-high[1m]",
    "kimi": "charmer-kimi-code-high[1m]",
    "or-gpt": "charmer-or-gpt-high[1m]",
    "or-grok": "charmer-or-grok-high",
}


def expected_switch(source: str, category: str | None, fence: tuple[str, ...] | list[str], *,
                    switches: bool = True) -> str | None:
    """The specified outcome of a refused turn on ``source``: its route-table
    target when the fence admits it (with or without ``[1m]``), else None (no
    switch). ``switches=False`` is a session where the switch is off."""

    target = _REFUSAL_ROUTES.get(_wire(source), {}).get(category) if switches else None
    if target is None or not ({target, target + "[1m]"} & set(fence)):
        return None
    return target


# The compiled hooks reach charmer's launcher through the fixture-built
# protocol-3 shim, which runs this logger: it runs the checkout's launcher on
# the fixture state with the hook's argv and stdin, passes its output and
# status through, and appends one metadata row per event (event, status, the
# PreModelSwitch decision and the payload's switch source and models).
_HOOK_LOGGER = """#!{python}
import json, os, re, subprocess, sys, time
SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\\[\\]-]{{0,127}}")
def safe(value):
    return value if isinstance(value, str) and SAFE.fullmatch(value) else None
payload = sys.stdin.buffer.read()
try:
    document = json.loads(payload)
except ValueError:
    document = {{}}
if not isinstance(document, dict):
    document = {{}}
env = dict(os.environ, CHARMER_HOOK_COMMAND={wrapper!r}, PYTHONPATH={src!r})
started = time.monotonic()
done = subprocess.run([{python!r}, {launcher!r}, *sys.argv[1:]], input=payload, capture_output=True, env=env)
sys.stdout.buffer.write(done.stdout)
sys.stdout.flush()
try:
    answer = json.loads(done.stdout or b"{{}}")
    decision = (answer.get("hookSpecificOutput") or {{}}).get("permissionDecision")
except (ValueError, AttributeError):
    decision = "unparsable"
row = {{"event": safe(sys.argv[2]) if len(sys.argv) > 2 else None, "status": done.returncode,
       "ms": int((time.monotonic() - started) * 1000), "decision": safe(decision),
       "name": safe(document.get("hook_event_name")), "source": safe(document.get("source")),
       "from": safe(document.get("from_model")), "to": safe(document.get("to_model"))}}
with open({log!r}, "a", encoding="utf-8") as out:
    out.write(json.dumps(row, sort_keys=True) + "\\n")
sys.exit(done.returncode)
"""


def compiled_managed_scope(fixture: probe.ProbeFixture, seed: str, *,
                           flag_env: dict[str, str] | None = None, without_counter: bool = False) -> dict[str, Any]:
    """A fixture-only managed session of a shipped seed: the v3 record and the
    v2 scope (the managed compile, with its six lifecycle hooks, the
    permission default and the refusal-switch capability table) under the
    fixture's state root, the protocol-3 shim there running
    :data:`_HOOK_LOGGER`. The probe's own copy of the compiled flag settings
    drops only the gateway base URL (the fixture provider is the endpoint) and
    adds ``flag_env``; ``without_counter`` also drops the compiled capability
    table (the client's own behaviour, measured). Nothing outside the fixture
    is read or written."""

    # A git work tree with one commit: the writable roles are worktree-isolated.
    git_env = {"HOME": str(fixture.home), "PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_CONFIG_GLOBAL": "/dev/null", "LANG": "C.UTF-8"}
    for command in (["git", "init", "-q", "-b", "main"],
                    ["git", "-c", "user.name=client-check", "-c", "user.email=check@invalid",
                     "commit", "-q", "--allow-empty", "-m", "fixture"]):
        subprocess.run(command, cwd=fixture.project_dir, env=git_env, check=True, capture_output=True, timeout=30)
    state.ensure_private_dir(fixture.xdg_state_home)
    state_root = state.ensure_private_dir(fixture.xdg_state_home / "charmer")
    log = fixture.root / "hook-events.jsonl"
    logger = fixture.root / "charmer-hook-logger"
    logger.write_text(_HOOK_LOGGER.format(
        python=sys.executable, wrapper=str(logger.resolve()), src=str(REPO_ROOT / "src"),
        launcher=str(REPO_ROOT / "bin" / "charmer"), log=str(log)), encoding="utf-8")
    logger.chmod(0o700)
    resolved = str(logger.resolve())
    shim = scope.hook_shim_v3_path(state_root)
    state.ensure_private_dir(shim.parent)
    state.atomic_write(shim, scope.hook_shim_v3_text(state_root, resolved))
    shim.chmod(0o700)
    environ = fixture.environ()
    environ["CHARMER_HOOK_COMMAND"] = resolved
    runtime = cli.Runtime(asset_root=RESOURCES_ROOT, environ=environ, cwd=fixture.project_dir,
                          allow_state_writes=False)
    if runtime.session_store.root != state_root:
        raise AssertionError("the hook's state root is not the fixture's")
    managed = str(uuid.uuid4())
    document = _v3.v1_projection(_SHIPPED.seed_profiles[seed], _v3.v1_models_view(_SHIPPED.docs)["models"])
    record = _v3.make_record(
        managed_id=managed, cwd=str(fixture.project_dir), composition_name=document["name"],
        snapshot=_v3.snapshot_for(_SHIPPED.docs, document), catalog_version=runtime.catalog_version,
        catalog_hash=runtime.catalog.bundle_sha256, launcher_version=runtime.launcher_version, mode="durable",
        scope_generation=1, launch_epoch=1)
    _v3.save_any(runtime.session_store, record)
    lcat = profile.LineupCatalog.from_catalog(_SHIPPED)
    eff = settings_mod.effective({"version": 1}, provider_ids=_SHIPPED.providers, line_keys=_SHIPPED.lines)
    lineup = profile.resolve(_SHIPPED.seed_profiles[seed], lcat, effective=eff)
    fence = scope.compile_fence(lineup, lcat, eff)
    plan = scope.compile_lineup_scope(
        lineup, lcat, eff, _SHIPPED.prompt_bodies, scope.catalog_meta_v2(_SHIPPED.docs), lineup_generation=1,
        managed_id=managed, hook_command=str(shim), launch_epoch=1, default_mode=scope.PERMISSION_DEFAULT_MODE)
    live = scope.write_scope(state_root, managed, plan)
    probe.seed_fixture_project_trust(fixture, live)
    settings = json.loads(json.dumps(plan.settings))
    settings["env"].pop("ANTHROPIC_BASE_URL")
    anthropic = {row.selector for row in fence.lead_set if row.provider == "anthropic"} | set(fence.fallback_only)
    # The compile states the counter the measured candidate named.
    if settings["env"][_MODEL_CAPABILITIES] != model_capabilities_counter(
            tuple(settings["availableModels"]), tuple(anthropic)):
        raise AssertionError("the compiled capability table is not the measured counter")
    if without_counter:
        del settings["env"][_MODEL_CAPABILITIES]
    settings["env"].update(flag_env or {})
    path = fixture.root / "compiled-settings.json"
    state.atomic_write(path, strict_json.canonical_file_bytes(settings))
    return {"managed": managed, "settings": path, "live": live, "log": log, "lineup": lineup,
            "available": tuple(settings["availableModels"]), "fallback_only": fence.fallback_only,
            "canonical": tuple(sorted(anthropic)), "hooks": sorted(settings["hooks"]),
            "lead": lineup.lead.binding.selector, "effort": lineup.lead.session_effort}


def hook_rows(log: Path) -> list[dict[str, Any]]:
    text = log.read_text(encoding="utf-8") if log.exists() else ""
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def model_capabilities_counter(available: tuple[str, ...], canonical: tuple[str, ...]) -> str:
    """``CLAUDE_CODE_MODEL_CAPABILITIES`` turning the client's refusal fallback
    off for every fenced selector that is not a canonical Anthropic id, stated
    independently of the compile (a prefix-free exact list)."""

    names = sorted({_wire(selector) for selector in available} - {_wire(selector) for selector in canonical})
    return ";".join(f"{name}=-refusal_fallback" for name in names)


class _RoutingResponder:
    """Refuses (``stop_reason: refusal`` + category) or 404s the requests of
    one execution path (request class ``main``, ``subagent`` or ``workflow``)
    on one wire; with ``spawn`` the lead's first request offering the tool
    gets one Agent (subagent path) or Workflow ``agent()`` (workflow path)
    call of that cm-* type. Everything else gets PROBE-OK. Rows are metadata."""

    def __init__(self, wire: str | None, category: str | None, *, path: str = "main",
                 spawn: str | None = None, server_fallback: str | None = None, not_found: bool = False):
        self.wire, self.category, self.path, self.spawn = wire, category, path, spawn
        self.server_fallback, self.not_found = server_fallback, not_found
        self.fallbacks_fields: list[int] = []
        self.rows: list[dict[str, Any]] = []
        self._forced = False
        self._lock = threading.Lock()

    def _spawn_use(self) -> dict[str, Any]:
        if self.path == "workflow":
            script = ("export const meta = { name: 'refusal-routing', description: 'refusal routing probe' }\n"
                      f"const r = await agent('Reply with exactly: PROBE-OK', {{label: 'rr', agentType: "
                      f"'{self.spawn}'}})\nreturn [r]\n")
            return {"type": "tool_use", "id": "toolu_refusal_routing_wf", "name": "Workflow",
                    "input": {"script": script}}
        return {"type": "tool_use", "id": "toolu_refusal_routing_00", "name": "Agent",
                "input": {"description": "refusal routing", "subagent_type": self.spawn,
                          "prompt": "Reply with exactly: PROBE-OK"}}

    def respond(self, document: dict[str, Any], path: str, context: Any) -> Any:
        route = path.split("?", 1)[0].rstrip("/")
        if route.endswith("/count_tokens"):
            return 200, {"input_tokens": 1}
        if not route.endswith("/messages"):
            return 404, {"type": "error", "error": {"type": "not_found_error", "message": "probe: unknown path"}}
        hints = dict(context.record.hint_header_values)
        model = document.get("model")
        stream = bool(document.get("stream"))
        request_class = hints.get("x-claude-code-request-class") or "main"
        tool = "Workflow" if self.path == "workflow" else "Agent"
        with self._lock:
            if "fallbacks" in document:
                self.fallbacks_fields.append(context.ordinal)
            record = context.record
            self.rows.append({"ordinal": context.ordinal, "model": record.model, "request_class": request_class,
                              "agent_type": hints.get("x-claude-code-agent-type"),
                              "shape": (record.max_tokens, record.effort, record.thinking_type, record.tool_count)})
            force = (self.spawn is not None and not self._forced and request_class == "main"
                     and tool in context.record.tool_names)
            if force:
                self._forced = True
        if isinstance(model, str) and model == self.wire and request_class == self.path:
            if self.not_found:
                return 404, {"type": "error", "error": {"type": "not_found_error", "message": "probe: model not found"}}
            return 200, _refusal_reply(model, self.category, stream, self.server_fallback)
        content = [self._spawn_use()] if force else [{"type": "text", "text": "PROBE-OK"}]
        payload = probe._message_payload(content, model=model, stop_reason="tool_use" if force else "end_turn",
                                         message_id="msg_refusal_routing")
        return 200, probe._sse_message(payload) if stream else payload


@uses_shipped_catalog
class RefusalRoutingMatrixProbe(_SpikeTestCase):
    """Refusal routing on the pin, with the compiled managed session.

    Every case runs a shipped seed's managed compile (its fence, flag
    settings env and the six lifecycle hooks through charmer's own launcher),
    or a hand-shaped fence for the shapes no seed compiles, and refuses one
    execution path's requests on one wire. A case passes only when the wire
    models on that path are the source and its specified target
    (:func:`expected_switch`), or the source alone; fence membership is
    necessary, never sufficient. Each case prints one row
    ``client check refusal-routing case: <label> expected=<..> observed=<..>``
    with the PreModelSwitch events charmer's hook saw; each test then prints
    ``client check refusal-routing-<part>: PASS|FAIL <n> cases``.
    """

    def _launch(self, fixture: probe.ProbeFixture, argv: tuple[str, ...], responder: _RoutingResponder, *,
                compiled: bool = True, timeout: float = _RUN_TIMEOUT
                ) -> tuple[probe.NativeRunResult, list[dict[str, Any]]]:
        """One run. A compiled session states the family defaults in its flag
        settings env (the launch clears them from the process env); a
        hand-shaped fence gets them in the process env."""

        provider = _BetaRecordingProvider(responder=responder)
        with provider:
            try:
                result = probe.run_native(argv, trusted=self.trusted, fixture=fixture, provider=provider,
                                          timeout=timeout, allow_real=True, environ=self.environ,
                                          child_env=dict(_HINT_ENV) if compiled else {**_FAMILY, **_HINT_ENV})
            except ProbeError as exc:
                if _TRIPWIRE in str(exc):
                    self.skipTest(f"BOUNDARY: refusal-routing INCONCLUSIVE: {exc}")
                raise
        self.assertFalse(result.timed_out, "refusal-routing run timed out")
        self.assertTrue(result.daemon is not None and result.daemon.unchanged)
        return result, [{**row, "betas": provider.beta_names.get(row["ordinal"], ())} for row in responder.rows]

    @staticmethod
    def _compiled_argv(managed: dict[str, Any], model: str, session: tuple[str, str], *extra: str
                       ) -> tuple[str, ...]:
        return ("-p", _TURN, *session, "--settings", str(managed["settings"]), "--model", model,
                "--effort", managed["effort"], "--add-dir", str(managed["live"]), *extra)

    def _observe(self, label: str, source: str, path: str, expected: str | None, result: Any,
                 rows: list[dict[str, Any]], responder: _RoutingResponder, hooks: list[dict[str, Any]],
                 *, lead: str | None = None, compiled: bool = True) -> dict[str, Any]:
        wire = _wire(source)
        on_path = [row["model"] for row in rows if row["request_class"] == path]
        switched = sorted(set(on_path) - {wire})
        problems = []
        if not on_path or on_path[0] != wire:
            problems.append(f"first {path} request on {on_path[:1]}, not the source {wire}")
        if switched != ([] if expected is None else [expected]):
            problems.append(f"switched to {switched or 'nothing'}")
        if responder.fallbacks_fields:
            problems.append("a request carried a fallbacks field")
        if path == "main" and (result.returncode == 0) != (expected is not None):
            problems.append(f"exit {result.returncode}")
        if path != "main" and lead is not None and _wire(lead) != wire and _wire(lead) in on_path:
            problems.append("the refused agent ran on the lead's model")
        switches = [f"{row['event']}:{row.get('source')}:{row.get('to')}:{row.get('decision')}"
                    for row in hooks if row.get("event") in ("premodel", "postmodel")]
        failed = sorted({row["event"] for row in hooks if row.get("status") not in (0, None)})
        if compiled:
            # Positive control: the compiled hooks ran through charmer's
            # launcher (the shim answers a prompt whose notice was already
            # seen itself, so ``prompt`` need not reach the launcher).
            events = {row.get("event") for row in hooks}
            if not {"start", "end"} <= events:
                problems.append(f"the compiled hooks did not run (events {sorted(map(str, events))})")
            if failed:
                problems.append(f"the launcher failed hook events {failed}")
        observed = {"label": label, "expected": expected or "none", "observed": switched[0] if switched else "none",
                    "rc": result.returncode, "requests": len(on_path), "switch_hooks": switches,
                    "hook_failures": failed, "problems": problems}
        print(f"client check refusal-routing case: {label} expected={observed['expected']} "
              f"observed={'+'.join(switched) or 'none'} rc={result.returncode} {path}-requests={len(on_path)} "
              f"switch-hooks={switches or 'none'} {'MISMATCH ' + '; '.join(problems) if problems else 'OK'}",
              flush=True)
        return observed

    def _compiled_case(self, label: str, seed: str, source: str, category: str | None, *, path: str = "main",
                       spawn: str | None = None, flag_env: dict[str, str] | None = None,
                       user_settings: dict[str, Any] | None = None, server: str | None = None,
                       not_found: bool = False, switches: bool = True, without_counter: bool = False,
                       project_settings: dict[str, Any] | None = None,
                       expected: str | None | bool = True) -> dict[str, Any]:
        """One compiled case; ``expected`` True means :func:`expected_switch`."""

        fixture = self._fixture()
        if user_settings is not None:
            state.atomic_write(fixture.claude_config_dir / "settings.json",
                               strict_json.canonical_file_bytes(user_settings))
        managed = compiled_managed_scope(fixture, seed, flag_env=flag_env, without_counter=without_counter)
        if project_settings is not None:
            directory = state.ensure_private_dir(fixture.project_dir / ".claude")
            state.atomic_write(directory / "settings.json", strict_json.canonical_file_bytes(project_settings))
        self.assertEqual(managed["hooks"], sorted(name for name, _event in scope.HOOK_EVENTS_V3))
        self.assertIn(source, managed["available"], label)
        model = source if path == "main" else managed["lead"]
        responder = _RoutingResponder(_wire(source), category, path=path, spawn=spawn,
                                      server_fallback=server, not_found=not_found)
        extra = ("--dangerously-skip-permissions",) if path == "workflow" else ()
        result, rows = self._launch(fixture, self._compiled_argv(
            managed, model, ("--session-id", str(uuid.uuid4())), *extra), responder)
        if expected is True:
            expected = None if not_found else expected_switch(source, category, managed["available"],
                                                              switches=switches)
        return self._observe(label, source, path, expected or None, result, rows, responder,
                             hook_rows(managed["log"]), lead=managed["lead"])

    def _shaped_case(self, label: str, source: str, category: str | None, fence: tuple[str, ...]
                     ) -> dict[str, Any]:
        """A hand-shaped fence (the compiled family defaults in the env, the
        managed permission default), for shapes no shipped seed compiles."""

        fixture = self._fixture()
        responder = _RoutingResponder(_wire(source), category)
        argv = ("-p", _TURN, "--model", source, *self._settings_argv(fixture, source, fence))
        result, rows = self._launch(fixture, argv, responder, compiled=False)
        return self._observe(label, source, "main", expected_switch(source, category, (source, *fence)),
                             result, rows, responder, [], compiled=False)

    def _verdict(self, part: str, cases: list[dict[str, Any]]) -> None:
        self.assertIn(f"client check refusal-routing-{part}: PASS ", REFUSAL_ROUTING_RECORDS)
        failed = [case for case in cases if case["problems"]]
        hooks = sorted({hook for case in cases for hook in case["switch_hooks"]})
        print(f"client check refusal-routing-{part}: {'FAIL' if failed else 'PASS'} {self._identity()} "
              f"{len(cases)} cases, {len(failed)} off the specified outcome"
              + (": " + " ".join(f"{case['label']}->{case['observed']}(expected {case['expected']})"
                                 for case in failed) if failed else "")
              + f"; switch hooks seen: {hooks or 'none'}", flush=True)
        self.assertEqual([case["label"] for case in failed], [], part)

    def test_lead_path_compiled_fence(self) -> None:
        """Every source kind x every refusal category on the lead path, in the
        compiled fence of the balanced seed (mixed providers, the
        fallback-only targets fenced)."""

        cases = [self._compiled_case(f"lead-{kind}-{category or 'uncategorized'}-balanced", "balanced", source,
                                     category)
                 for kind, source in _ROUTING_SOURCES.items() for category in _REFUSAL_CATEGORIES]
        self._verdict("lead", cases)

    def test_lead_path_fence_shapes_and_controls(self) -> None:
        """Lead-only and non-Anthropic lead-set fences, the openai and
        openrouter seeds' compiled leads, server-named targets and 404s."""

        no_fallback = ("gpt-multi-astra-high[1m]", "charmer-kimi-code-high[1m]", "charmer-or-gpt-high[1m]",
                       "charmer-or-opus-xhigh[1m]", _SONNET)
        cases = [self._shaped_case(f"leadonly-{kind}-cyber", source, "cyber", ())
                 for kind, source in _ROUTING_SOURCES.items()]
        cases += [self._shaped_case(f"nofallback-{kind}-{category}", _ROUTING_SOURCES[kind], category, no_fallback)
                  for kind in ("astra", "kimi", "or-opus") for category in ("cyber", "frontier_llm")]
        cases += [self._compiled_case(f"seed-{seed}-lead-{category}", seed, managed_lead, category)
                  for seed, managed_lead in (("openai", "gpt-multi-astra-high[1m]"),
                                             ("openrouter", "charmer-or-opus-xhigh[1m]"))
                  for category in ("cyber", "bio", "frontier_llm")]
        for kind in ("opus", "astra", "or-opus"):
            source = _ROUTING_SOURCES[kind]
            cases.append(self._compiled_case(f"server-out-{kind}-cyber", "balanced", source, "cyber",
                                             server="claude-check-server-chosen"))
            cases.append(self._compiled_case(f"server-in-set-{kind}-cyber", "balanced", source, "cyber",
                                             server=_wire(_SONNET5)))
            cases.append(self._compiled_case(f"not-found-{kind}", "balanced", source, None, not_found=True))
        self._verdict("shapes", cases)

    def test_switch_controls(self) -> None:
        """``switchModelsOnFlag:false``, the client's
        ``CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK`` in the flag and the user
        settings env, measured in the compiled balanced fence."""

        cases = []
        for kind in ("opus", "sonnet", "astra", "or-opus", "kimi"):
            source = _ROUTING_SOURCES[kind]
            cases.append(self._compiled_case(f"switch-off-{kind}-cyber", "balanced", source, "cyber",
                                             user_settings={"switchModelsOnFlag": False}, switches=False))
            cases.append(self._compiled_case(f"disable-flag-env-{kind}-cyber", "balanced", source, "cyber",
                                             flag_env={_DISABLE_REFUSAL_FALLBACK: "1"}, switches=False))
            cases.append(self._compiled_case(f"disable-user-env-{kind}-cyber", "balanced", source, "cyber",
                                             user_settings={"env": {_DISABLE_REFUSAL_FALLBACK: "1"}},
                                             switches=False))
        self._verdict("controls", cases)

    def test_subagent_and_workflow_paths(self) -> None:
        """A refused cm-* agent of each source kind the seeds bind (subagent
        and workflow paths): its wire, a specified target or none, never the
        lead's model."""

        agents = (
            ("balanced", "cm-explorer", "gpt-multi-sol-6-1-high[1m]"),
            ("balanced", "cm-analyst-strong", _OPUS),
            ("claude", "cm-explorer", _SONNET),
            ("claude", "cm-reviewer-strong", _FABLE),
            ("openrouter", "cm-implementer-strong", "charmer-or-opus-xhigh[1m]"),
            ("openrouter", "cm-analyst", "charmer-or-gpt-high[1m]"),
            ("openrouter", "cm-reviewer-strong", "charmer-or-grok-xhigh"),
        )
        cases = [self._compiled_case(f"subagent-{seed}-{agent}-{category or 'uncategorized'}", seed, source,
                                     category, path="subagent", spawn=agent)
                 for seed, agent, source in agents for category in ("cyber", "bio", "frontier_llm", None)]
        cases += [self._compiled_case(f"workflow-{seed}-{agent}-{category}", seed, source, category,
                                      path="workflow", spawn=agent)
                  for seed, agent, source in (agents[0], agents[1], agents[4])
                  for category in ("cyber", "frontier_llm")]
        self._verdict("agents", cases)

    def _resume_case(self, kind: str, first_refused: bool) -> dict[str, Any]:
        """Resume on the compiled balanced scope: a refusal on the resumed
        turn, or the first resumed turn after a refused one (it runs on the
        relaunch argv's model, the source)."""

        source = _ROUTING_SOURCES[kind]
        label = f"resume-{kind}-{'after-refusal' if first_refused else 'then-refusal'}-cyber"
        fixture = self._fixture()
        managed = compiled_managed_scope(fixture, "balanced")
        session = str(uuid.uuid4())
        refuse = _RoutingResponder(_wire(source), "cyber")
        plain = _RoutingResponder(None, None)
        first, second = (refuse, plain) if first_refused else (plain, refuse)
        result_one, rows_one = self._launch(fixture, self._compiled_argv(
            managed, source, ("--session-id", session)), first)
        result_two, rows_two = self._launch(fixture, self._compiled_argv(
            managed, source, ("--resume", session)), second)
        hooks = hook_rows(managed["log"])
        expected = expected_switch(source, "cyber", managed["available"])
        if not first_refused:
            self.assertEqual(result_one.returncode, 0, label)
            return self._observe(label, source, "main", expected, result_two, rows_two, refuse, hooks)
        observed = self._observe(label + "/first", source, "main", expected, result_one, rows_one, refuse, hooks)
        resumed = [row["model"] for row in rows_two if row["request_class"] == "main"]
        if resumed[:1] != [_wire(source)]:
            observed["problems"].append(f"the resumed turn ran on {resumed[:1]}, not {_wire(source)}")
        print(f"client check refusal-routing case: {label}/resumed first-wire={resumed[:1]} "
              f"rc={result_two.returncode} {'MISMATCH' if observed['problems'] else 'OK'}", flush=True)
        observed["label"] = label
        return observed

    def test_resume_paths(self) -> None:
        cases = [self._resume_case(kind, first_refused) for kind in ("opus", "astra", "or-opus")
                 for first_refused in (False, True)]
        self._verdict("resume", cases)

    def _shape(self, seed: str, source: str, *, without_counter: bool) -> tuple[Any, ...]:
        """The first main request of a plain turn: wire, output cap, effort,
        thinking, tool count and beta names."""

        fixture = self._fixture()
        managed = compiled_managed_scope(fixture, seed, without_counter=without_counter)
        result, rows = self._launch(fixture, self._compiled_argv(
            managed, source, ("--session-id", str(uuid.uuid4()))), _RoutingResponder(None, None))
        self.assertEqual(result.returncode, 0, source)
        main = [row for row in rows if row["request_class"] == "main"]
        return (main[0]["model"], *main[0]["shape"], main[0]["betas"])

    def test_the_compiled_counter_holds_against_every_layer(self) -> None:
        """The compiled capability table against the other layers: a user or
        project settings env naming the capability on for every model does
        not turn the switch back on (and the user's layer does reach the
        client: its own entry alone keeps an unknown source on its model); the
        client's catch-all in the user's settings env still leaves an unknown
        source on its model; an unrefused turn is the same request with and
        without the table."""

        enable = {"env": {_MODEL_CAPABILITIES: "refusal_fallback"}}
        astra = _ROUTING_SOURCES["astra"]
        cases = [self._compiled_case(
            "user-env-entry-alone-astra-cyber", "balanced", astra, "cyber", without_counter=True,
            user_settings={"env": {_MODEL_CAPABILITIES: f"{_wire(astra)}=-refusal_fallback"}})]
        for kind in ("astra", "or-opus", "opus"):
            source = _ROUTING_SOURCES[kind]
            cases.append(self._compiled_case(f"user-env-enable-{kind}-cyber", "balanced", source, "cyber",
                                             user_settings=enable))
            cases.append(self._compiled_case(f"project-env-enable-{kind}-cyber", "balanced", source, "cyber",
                                             project_settings=enable))
        # The client's catch-all: an uncategorized refusal switches to the
        # session's armed fallback (the compiled Opus family default).
        catch_all = {"env": {"CLAUDE_CODE_REFUSAL_FALLBACK_CATCH_ALL": "1"}}
        for kind in ("astra", "or-opus", "opus"):
            cases.append(self._compiled_case(f"catch-all-{kind}-uncategorized", "balanced",
                                             _ROUTING_SOURCES[kind], None, user_settings=catch_all))
        shapes = {kind: (self._shape("balanced", source, without_counter=True),
                         self._shape("balanced", source, without_counter=False))
                  for kind, source in _ROUTING_SOURCES.items()}
        changed = sorted(kind for kind, (plain, countered) in shapes.items() if plain != countered)
        print("client check refusal-routing case: counter-request-shape "
              f"changed={changed or 'none'} " + " ".join(f"{kind}={plain}" for kind, (plain, _c) in shapes.items()),
              flush=True)
        cases.append({"label": "counter-request-shape", "expected": "unchanged",
                      "observed": "changed:" + ",".join(changed) if changed else "unchanged",
                      "switch_hooks": [], "problems": [f"request changed for {changed}"] if changed else []})
        self._verdict("counter", cases)

    def test_without_the_compiled_counter(self) -> None:
        """Why the compile states the table: without it the pin gives a model
        it does not recognise its default route table (cyber ->
        claude-opus-4-8, bio and frontier_llm -> claude-opus-5) inside the
        fence, on the lead, a subagent and a workflow agent, and the
        catch-all moves an uncategorized refusal to the compiled Opus family
        default; canonical sources are the same with or without it. A client
        that stops switching fails this check: re-measure the counter."""

        default_route = {"cyber": "claude-opus-4-8", "bio": "claude-opus-5", "frontier_llm": "claude-opus-5"}
        cases = []
        for kind in ("or-opus", "astra", "kimi", "or-grok"):
            for category in ("cyber", "bio", "frontier_llm", None):
                cases.append(self._compiled_case(
                    f"uncountered-lead-{kind}-{category or 'uncategorized'}", "balanced", _ROUTING_SOURCES[kind],
                    category, without_counter=True, expected=default_route.get(category)))
        for kind in ("opus", "sonnet"):
            cases.append(self._compiled_case(f"uncountered-lead-{kind}-cyber", "balanced", _ROUTING_SOURCES[kind],
                                             "cyber", without_counter=True))
        cases.append(self._compiled_case("uncountered-subagent-balanced-cm-explorer-cyber", "balanced",
                                         "gpt-multi-sol-6-1-high[1m]", "cyber", path="subagent", spawn="cm-explorer",
                                         without_counter=True, expected="claude-opus-4-8"))
        cases.append(self._compiled_case("uncountered-workflow-balanced-cm-explorer-bio", "balanced",
                                         "gpt-multi-sol-6-1-high[1m]", "bio", path="workflow", spawn="cm-explorer",
                                         without_counter=True, expected="claude-opus-5"))
        cases.append(self._compiled_case(
            "uncountered-catch-all-astra-uncategorized", "balanced", _ROUTING_SOURCES["astra"], None,
            without_counter=True, user_settings={"env": {"CLAUDE_CODE_REFUSAL_FALLBACK_CATCH_ALL": "1"}},
            expected=_wire(_OPUS)))
        self._verdict("uncountered", cases)
