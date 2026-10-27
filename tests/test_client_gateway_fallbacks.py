"""Client check gateway-fallbacks: what the pinned client sends to charmer's
gateway, and the resends Claude Code 2.1.304-2.1.310 added for a gateway that
refuses part of a request.

From 2.1.304 the client answers a gateway's refusal of a request field by
sending the request again without it: ``context_management`` (2.1.304), tool
search (2.1.304, repaired in 2.1.305), the effort beta with
``output_config.effort`` and mid-conversation system turns (2.1.310; an HTTP
400 or 422 the client recognises nowhere else turns the system turns off until
``/clear`` or ``/compact``). Behind charmer's gateway such a resend would cost
the session its effort, its reminders or its tool search without a word, so a
managed turn must never take one. Each case runs the pinned client against a
fixture gateway (``availableModels``, the compiled retry watchdog and
fast-mode disable) and keeps only bounded facts per request (route, model,
beta ids, field presence, tool names); prompts and bodies stay in memory.

* ``shape``: a canonical Anthropic lead and a gateway name the client does not
  know, fresh and on ``--resume``: one request per turn carrying the effort
  beta and ``output_config.effort``, ``context_management`` with its beta,
  tool search (``ToolSearch``, a deferred placeholder and the tools it defers)
  and mid-conversation system turns; 2.1.307 no longer sends the
  ``prompt-caching-scope`` beta behind a custom base URL. A ``ToolSearch`` call
  returns ``tool_reference`` blocks and the next request carries the fetched
  tool with ``defer_loading``.
  Every one of these parts is checked on each request: the fresh request, the
  ``ToolSearch`` continuation and the resumed request.
* ``effort-refused``, ``context-refused``, ``tools-refused``,
  ``system-refused`` (controls): the fixture gateway refuses the part with the
  error the client recognises and the client resends without it, so the shape
  case's single request per turn shows a request the gateway accepted, not a
  client that never resends.

``ShapeThroughGatewayTests`` (the candidate gateway; ``CHARMER_TEST_CLI_PROXY_API``)
runs the same lead through charmer's gateway on the Claude account route, fresh,
on ``--resume`` with a ``ToolSearch`` round trip, and in a terminal session
after ``/model``: every request is answered 200 and each turn is one request, so
the client takes no fallback; each client request carries every promised part,
and its one upstream request keeps the session's effort value with its beta,
``context_management``, the system turns and the ``tool_reference`` blocks. A captured
client request is replayed on the gateway's Claude key, ChatGPT (Codex) and
OpenAI-compatible routes: none answers 400 or 422.

``HttpMcpRetryProbe`` (Claude Code 2.1.307): an HTTP MCP server holds the
session's first ``tools/call`` unanswered while its server-message stream is
reset three times, so the client closes the connection with the call pending:
a held read-only call is sent again, a held call that changes something is sent
once (the previous pin sends both again). ``KeepaliveProbe`` (2.1.309): a
streamed answer that sends only ``ping`` events for 50 seconds before its
content completes as one request under the compiled retry watchdog.

The output lines: ``client check gateway-fallbacks: <STATUS> class=<class>
<detail>``, ``client check gateway-fallbacks-gateway: ...``, ``client check
mcp-http-retry: ...`` and ``client check keepalive: ...``. The fallback control
(``CHARMER_TEST_FALLBACK_CONTROL_CLAUDE``, an older client accepted only at its
recorded sha256; ``_client_roles``) is the control for the two removed
behaviours: it sends the caching beta and sends the changing MCP tool call
twice.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from typing import Any

from charmer import probe, scope, state, upgrade
from charmer.probe import ProbeError

import _client_roles
from _layout import RESOURCES_ROOT
from _tier import real_binary_gate

PASS_CLASS = "GF-no-fallback"
MCP_PASS_CLASS = "MR-changing-once"
KEEPALIVE_PASS_CLASS = "KA-one-request"
ESSENTIAL_RECORDS = (f"client check gateway-fallbacks: PASS class={PASS_CLASS} ",
                     f"client check mcp-http-retry: PASS class={MCP_PASS_CLASS} ")
_CONTRACT = RESOURCES_ROOT / "catalog" / "native-contract.json"
EFFORT_BETA = "effort-2025-11-24"
CONTEXT_BETA = "context-management-2025-06-27"
TOOL_SEARCH_BETA = "advanced-tool-use-2025-11-20"
CACHING_SCOPE_BETA = "prompt-caching-scope-2026-01-05"
_LEAD, _SWITCHED, _UNKNOWN = "claude-opus-5-5", "claude-fable-5-1", "gpt-multi-sol-high"
_EFFORT = "high"
# The refusals the client recognises (read from the client's own handling):
# each makes it resend the request once without the named part.
_REFUSALS = {
    "effort": "output_config.effort: Extra inputs are not permitted",
    "context": "context_management: Extra inputs are not permitted",
    "tools": "tools.0.defer_loading: Extra inputs are not permitted",
    "system": 'messages.1: role "system" is not supported',
}
# The fallback control: an older client that still has the behaviours the
# pin removed (the caching beta, the resent changing MCP call). It is not the
# previous release's pin (2.1.310 already dropped both), which is the
# downgrade-resume control instead (_client_roles).
FALLBACK_CONTROL_ENV = _client_roles.FALLBACK_CONTROL_ENV
_FALLBACK_CONTROL = _client_roles.FALLBACK_CONTROL


def fallback_control_client() -> probe.TrustedExecutable | None:
    """The fallback control named by ``CHARMER_TEST_FALLBACK_CONTROL_CLAUDE``
    when it is that exact build (absolute, a regular file, the recorded
    sha256)."""

    return _client_roles.named_control(FALLBACK_CONTROL_ENV)


class _Inconclusive(AssertionError):
    """A case did not exercise its path: no class can be chosen."""


def _betas(headers: Any) -> list[str]:
    return sorted({value.strip() for value in (headers.get("anthropic-beta") or "").split(",") if value.strip()})


def request_row(path: str, headers: Any, document: Any) -> dict[str, Any]:
    """Bounded facts of one request: never a prompt, a body or a header value
    other than the public beta ids."""

    row: dict[str, Any] = {"route": path.split("?", 1)[0], "betas": _betas(headers)}
    if not isinstance(document, dict):
        return row
    tools = [tool for tool in document.get("tools") or [] if isinstance(tool, dict)]
    messages = [message for message in document.get("messages") or [] if isinstance(message, dict)]
    output = document.get("output_config") if isinstance(document.get("output_config"), dict) else {}
    references = 0
    for message in messages:
        for block in message.get("content") if isinstance(message.get("content"), list) else ():
            if isinstance(block, dict) and block.get("type") == "tool_result" and isinstance(block.get("content"), list):
                references += sum(1 for part in block["content"]
                                  if isinstance(part, dict) and part.get("type") == "tool_reference")
    row.update({
        "model": document.get("model"),
        "stream": bool(document.get("stream")),
        "effort": output.get("effort"),
        "context_management": "context_management" in document,
        "tools": [str(tool.get("name")) for tool in tools],
        "deferred": [str(tool.get("name")) for tool in tools if tool.get("defer_loading")],
        "system_turns": sum(1 for message in messages if message.get("role") == "system"),
        "tool_references": references,
    })
    return row


def _main(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row["route"].endswith("/v1/messages") and row.get("tools")]


def _payload(document: dict[str, Any], content: list[dict[str, Any]], stop: str, ordinal: int) -> Any:
    payload = probe._message_payload(content, model=document.get("model"), stop_reason=stop,
                                     message_id=f"msg_gf_{ordinal:04d}_{uuid.uuid4().hex[:8]}")
    return probe._sse_message(payload) if document.get("stream") else payload


class _Provider(probe.FakeAnthropicProvider):
    """The fixture gateway: bounded facts per request, and the refusal a case
    asks for (``refuse``: a key of ``_REFUSALS``) on every request carrying
    the refused part. ``search_once`` answers the first request offering
    ``ToolSearch`` with a ``ToolSearch`` call for ``SendMessage``."""

    def __init__(self, *, refuse: str | None = None, search_once: bool = False) -> None:
        super().__init__(responder=self)
        self.refuse = refuse
        self.search_once = search_once
        self.rows: list[dict[str, Any]] = []
        self.captured: list[tuple[dict[str, str], bytes]] = []
        self._facts_lock = threading.Lock()

    def _record(self, method, path, headers, body, document):  # noqa: ANN001
        context = super()._record(method, path, headers, body, document)
        row = request_row(path, headers, document)
        with self._facts_lock:
            self.rows.append({**row, "ordinal": context.ordinal})
            if row.get("tools"):
                # Kept in memory for the gateway replay only.
                self.captured.append(({name.lower(): value for name, value in headers.items()}, body))
        return context

    def _refused(self, row: dict[str, Any]) -> bool:
        if self.refuse == "effort":
            return row.get("effort") is not None or EFFORT_BETA in row["betas"]
        if self.refuse == "context":
            return bool(row.get("context_management"))
        if self.refuse == "tools":
            return "ToolSearch" in row.get("tools", ()) or bool(row.get("deferred"))
        if self.refuse == "system":
            return bool(row.get("system_turns"))
        return False

    def respond(self, document: dict[str, Any], path: str, context: probe.RequestContext) -> tuple[int, Any]:
        if path.split("?", 1)[0].endswith("/count_tokens"):
            return 200, {"input_tokens": 1}
        with self._facts_lock:
            row = next(item for item in self.rows if item["ordinal"] == context.ordinal)
        if self._refused(row):
            return 400, {"type": "error", "error": {"type": "invalid_request_error",
                                                    "message": _REFUSALS[str(self.refuse)]}}
        if self.search_once and "ToolSearch" in row.get("tools", ()):
            self.search_once = False
            return 200, _payload(document, [{"type": "tool_use", "id": "toolu_gf_search", "name": "ToolSearch",
                                             "input": {"query": "select:SendMessage", "max_results": 5}}],
                                 "tool_use", context.ordinal)
        return 200, _payload(document, [{"type": "text", "text": "PROBE-OK"}], "end_turn", context.ordinal)


def missing_parts(row: dict[str, Any]) -> list[str]:
    """The promised parts a managed request lacks: the session's effort
    (``output_config.effort`` at its value, with the effort beta),
    ``context_management`` with its beta, tool search (``ToolSearch`` and a
    deferred tool, with the advanced-tool-use beta) and mid-conversation
    system turns."""

    betas = row.get("betas") or ()
    gaps = []
    if row.get("effort") != _EFFORT or EFFORT_BETA not in betas:
        gaps.append("effort")
    if not row.get("context_management") or CONTEXT_BETA not in betas:
        gaps.append("context")
    if "ToolSearch" not in row.get("tools", ()) or not row.get("deferred") or TOOL_SEARCH_BETA not in betas:
        gaps.append("tool-search")
    if not row.get("system_turns"):
        gaps.append("system-turns")
    return gaps


_CONTROLS = (("effort-refused", "effort"), ("context-refused", "context_management"),
             ("tools-refused", "tools"), ("system-refused", "system_turns"))
# The most requests one refusal control may take: the refused request and
# its resends (2.1.311 measured: four for a refused tool search, the first
# resend without the thinking-token-count beta; 2.1.310: three).
_CONTROL_REQUESTS = 4


def verdict(observed: dict[str, Any]) -> str:
    """The class from each case's request rows: every request of the fresh
    turn (the first request and the ToolSearch continuation) and of the
    resumed turn carries every promised part, one request per step, and each
    control ends without its refused part within a bounded resend."""

    for label in ("known", "unknown"):
        turns = observed["shape"][label]["turns"]
        if len(turns) != 2 or any(not turn for turn in turns):
            raise _Inconclusive(f"shape {label}: a turn sent no main request")
        for turn in turns:
            for row in turn:
                gaps = missing_parts(row)
                if gaps:
                    return f"GF-{gaps[0]}-missing"
        # Turn one: the ToolSearch call and its continuation; turn two: one.
        if [len(turn) for turn in turns] != [2, 1]:
            return "GF-resent"
        last = turns[0][-1]
        if not last.get("tool_references") or "SendMessage" not in last.get("deferred", ()):
            raise _Inconclusive(f"shape {label}: the ToolSearch round trip fetched no deferred tool")
    for case, field in _CONTROLS:
        rows = observed[case]
        if len(rows) < 2:
            raise _Inconclusive(f"{case}: the client did not resend after the refusal")
        if field == "system_turns" and not rows[0].get("system_turns"):
            raise _Inconclusive(f"{case}: the refused request carried no system turn")
        final = rows[-1]
        still = (final.get("effort") is not None or EFFORT_BETA in final["betas"] if field == "effort"
                 else final.get("context_management") if field == "context_management"
                 else bool(final.get("system_turns")) if field == "system_turns"
                 else "ToolSearch" in final.get("tools", ()))
        # The client may first strip other parts (for an unrecognised 400,
        # 2.1.311 first drops the thinking-token-count beta, then one more
        # part, before tool search), but it ends without the refused part
        # within a bounded number of resends.
        if still or len(rows) > _CONTROL_REQUESTS:
            return f"GF-{case}-not-resent-without"
    return PASS_CLASS


class _GatedCase(unittest.TestCase):
    probe_id = "gateway-fallbacks"
    verdict: tuple[str, str, str] = ("INCONCLUSIVE", "unavailable", "check did not complete")

    @classmethod
    def setUpClass(cls) -> None:
        cls.gate = real_binary_gate(_CONTRACT, what=f"the {cls.probe_id} check")
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
        self.root = Path(tempfile.mkdtemp(prefix=f"charmer-{self.probe_id}-"))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.environ = {"HOME": str(self.root / "ambient"), "PATH": "/usr/bin:/bin"}
        self.count = 0

    def _fixture(self) -> probe.ProbeFixture:
        self.count += 1
        fixture = probe.build_fixture(self.root / f"run-{self.count}", environ=self.environ)
        probe.seed_fixture_project_trust(fixture, fixture.project_dir)
        return fixture

    def _settings(self, fixture: probe.ProbeFixture, lead: str, fence: list[str]) -> Path:
        # The compiled scope's request-shaping env: the retry watchdog and the
        # fast-mode disable (scope._lifecycle_settings_v2, compile_lineup_scope).
        return probe.write_fixture_settings(fixture, "scope/settings.json", {
            "availableModels": fence, "model": lead,
            "permissions": {"defaultMode": scope.PERMISSION_DEFAULT_MODE},
            "env": {"CLAUDE_CODE_RETRY_WATCHDOG": "1", scope.FAST_MODE_ENV: "1",
                    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "800000"}})

    def _headless(self, fixture, settings, lead, provider, prompts, *, trusted=None) -> list[list[dict[str, Any]]]:
        """Headless turns of one session (fresh, then ``--resume``); the main
        requests of each turn."""

        session = str(uuid.uuid4())
        turns = []
        for index, prompt in enumerate(prompts):
            before = len(provider.rows)
            result = probe.run_native(
                ("-p", prompt, "--settings", str(settings), "--model", lead, "--effort", _EFFORT,
                 *(("--session-id", session) if index == 0 else ("--resume", session))),
                trusted=trusted or self.trusted, fixture=fixture, provider=provider, timeout=150,
                allow_real=True, environ=self.environ)
            if result.timed_out or result.returncode != 0:
                raise _Inconclusive(f"a turn of {lead} did not complete (rc={result.returncode})")
            turns.append(_main(provider.rows[before:]))
        return turns

    def _guard(self, run):
        try:
            return run()
        except _Inconclusive as exc:
            type(self).verdict = ("INCONCLUSIVE", "unavailable", str(exc))
            raise
        except ProbeError as exc:
            if "live daemon domain touched" in str(exc):
                type(self).verdict = ("BOUNDARY", "unavailable", "live daemon-domain tripwire hit")
                self.skipTest("BOUNDARY: live daemon-domain tripwire hit")
            type(self).verdict = ("INCONCLUSIVE", "unavailable", "probe execution unavailable")
            raise


def _summary(rows: list[dict[str, Any]]) -> str:
    return "/".join(f"{row.get('model')}:{row.get('effort') or '-'}"
                    f"{'+ctx' if row.get('context_management') else ''}"
                    f"{'+search' if 'ToolSearch' in row.get('tools', ()) else ''}"
                    f"{'+refs' if row.get('tool_references') else ''}"
                    f"{'+sys' if row.get('system_turns') else ''}" for row in rows)


class GatewayFallbackProbe(_GatedCase):
    probe_id = "gateway-fallbacks"

    def test_managed_turns_send_what_the_gateway_accepts_once(self) -> None:
        def run() -> dict[str, Any]:
            observed: dict[str, Any] = {"shape": {}}
            for label, lead in (("known", _LEAD), ("unknown", _UNKNOWN)):
                fixture = self._fixture()
                provider = _Provider(search_once=True)
                with provider:
                    turns = self._headless(fixture, self._settings(fixture, lead, [lead]), lead, provider,
                                           ("client check one", "client check two"))
                observed["shape"][label] = {"turns": turns, "rows": list(provider.rows)}
            for case, _field in _CONTROLS:
                fixture = self._fixture()
                provider = _Provider(refuse=case.split("-", 1)[0])
                with provider:
                    observed[case] = self._headless(fixture, self._settings(fixture, _LEAD, [_LEAD]), _LEAD,
                                                    provider, ("client check",))[0]
            return observed

        observed = self._guard(run)
        observed_class = self._guard(lambda: verdict(observed))
        known = observed["shape"]["known"]["turns"]
        first = known[0][0]
        caching = any(CACHING_SCOPE_BETA in row["betas"]
                      for label in ("known", "unknown") for row in observed["shape"][label]["rows"])
        previous = fallback_control_client()
        control = (f"fallback control unavailable ({_FALLBACK_CONTROL[0]} not named by "
                   f"{FALLBACK_CONTROL_ENV})")
        if previous is not None:
            fixture = self._fixture()
            provider = _Provider()
            with provider:
                self._guard(lambda: self._headless(fixture, self._settings(fixture, _LEAD, [_LEAD]), _LEAD,
                                                   provider, ("client check",), trusted=previous))
            sent = any(CACHING_SCOPE_BETA in row["betas"] for row in provider.rows)
            self.assertTrue(sent, "the fallback control no longer sends the caching beta: the control is void")
            offered = _main(provider.rows)[0]["tools"]
            control = (f"fallback control {_FALLBACK_CONTROL[0]} sends {CACHING_SCOPE_BETA} and offers "
                       f"{len(offered)} tools {'with' if 'ToolSearch' in offered else 'without'} ToolSearch")
        detail = (
            f"turns(known)=[{' | '.join(_summary(turn) for turn in known)}] "
            f"turns(unknown)=[{' | '.join(_summary(turn) for turn in observed['shape']['unknown']['turns'])}] "
            f"betas={first['betas']} system-turns={first['system_turns']} "
            f"deferred={first['deferred']} offered={first['tools']} "
            f"caching-scope-beta={'sent' if caching else 'absent'} ({control}) "
            + " ".join(f"{case}=[{_summary(observed[case])}]" for case, _field in _CONTROLS)
            + f" client={self.gate.version} sha256={self.trusted.sha256[:12]}")
        if caching and observed_class == PASS_CLASS:
            observed_class = "GF-caching-beta-sent"
        type(self).verdict = ("PASS" if observed_class == PASS_CLASS else "FAIL", observed_class, detail)
        self.assertEqual(observed_class, PASS_CLASS, detail)

    def test_the_records_are_essential(self) -> None:
        for record in ESSENTIAL_RECORDS:
            self.assertIn(record, upgrade.ESSENTIAL_EVIDENCE_PREFIXES)


_ALL_BETAS = [EFFORT_BETA, CONTEXT_BETA, TOOL_SEARCH_BETA]


def _row(effort=_EFFORT, betas=None, ctx=True, search=True, refs=0, deferred=("DeferredToolPlaceholder",),
         system=1, model=_LEAD) -> dict[str, Any]:
    """A client request's facts as ``request_row`` keeps them (self-tests)."""

    return {"route": "/v1/messages", "model": model, "effort": effort, "context_management": ctx,
            "betas": list(_ALL_BETAS if betas is None else betas),
            "tools": ["Read", *(["ToolSearch", "DeferredToolPlaceholder"] if search else [])],
            "deferred": list(deferred) if search else [], "tool_references": refs, "system_turns": system}


class VerdictTests(unittest.TestCase):
    def rows(self) -> dict[str, Any]:
        searched = _row(refs=1, deferred=("SendMessage", "DeferredToolPlaceholder"), system=2)
        shape = {"turns": [[_row(), searched], [_row(refs=1, system=3)]]}
        return {"shape": {"known": shape, "unknown": shape},
                "effort-refused": [_row(), _row(effort=None, betas=[CONTEXT_BETA, TOOL_SEARCH_BETA])],
                "context-refused": [_row(), _row(ctx=False, betas=[EFFORT_BETA, TOOL_SEARCH_BETA])],
                "tools-refused": [_row(), _row(search=False, betas=[EFFORT_BETA, CONTEXT_BETA])],
                "system-refused": [_row(), _row(system=0)]}

    @staticmethod
    def changed(rows, label, turn, index, **fields) -> dict[str, Any]:
        rows = json.loads(json.dumps(rows))
        rows["shape"][label]["turns"][turn][index].update(fields)
        return rows

    def test_the_class_needs_every_promised_part_and_a_bounded_resend_per_control(self) -> None:
        rows = self.rows()
        self.assertEqual(verdict(rows), PASS_CLASS)
        resent = json.loads(json.dumps(rows))
        resent["shape"]["unknown"]["turns"][1].append(resent["shape"]["unknown"]["turns"][1][0])
        self.assertEqual(verdict(resent), "GF-resent")
        kept = json.loads(json.dumps(rows))
        kept["effort-refused"][1]["effort"] = _EFFORT
        self.assertEqual(verdict(kept), "GF-effort-refused-not-resent-without")
        looped = json.loads(json.dumps(rows))
        looped["tools-refused"] = [looped["tools-refused"][0]] * _CONTROL_REQUESTS + [looped["tools-refused"][1]]
        self.assertEqual(verdict(looped), "GF-tools-refused-not-resent-without")
        chained = json.loads(json.dumps(rows))
        chained["tools-refused"].insert(1, chained["tools-refused"][0])
        self.assertEqual(verdict(chained), PASS_CLASS)
        # The measured 2.1.311 shape: two resends as sent (the first without
        # the thinking-token-count beta), then one without tool search.
        walked = json.loads(json.dumps(rows))
        walked["tools-refused"] = [walked["tools-refused"][0]] * 3 + [walked["tools-refused"][1]]
        self.assertEqual(verdict(walked), PASS_CLASS)
        system_kept = json.loads(json.dumps(rows))
        system_kept["system-refused"][1]["system_turns"] = 1
        self.assertEqual(verdict(system_kept), "GF-system-refused-not-resent-without")
        never = json.loads(json.dumps(rows))
        never["context-refused"] = never["context-refused"][:1]
        with self.assertRaises(_Inconclusive):
            verdict(never)
        no_system = json.loads(json.dumps(rows))
        no_system["system-refused"][0]["system_turns"] = 0
        with self.assertRaises(_Inconclusive):
            verdict(no_system)
        unsearched = self.changed(rows, "known", 0, 1, tool_references=0)
        with self.assertRaises(_Inconclusive):
            verdict(unsearched)

    def test_each_omission_on_each_request_fails(self) -> None:
        """The fresh request, the ToolSearch continuation and the resumed
        request of either lead: losing any promised part, or its beta alone,
        while the effort and the counts stay, is not a pass."""

        rows = self.rows()
        omissions = {
            "GF-effort-missing": ({"effort": "low"}, {"effort": None},
                                  {"betas": [CONTEXT_BETA, TOOL_SEARCH_BETA]}),
            "GF-context-missing": ({"context_management": False}, {"betas": [EFFORT_BETA, TOOL_SEARCH_BETA]}),
            "GF-tool-search-missing": ({"tools": ["Read"]}, {"deferred": []},
                                       {"betas": [EFFORT_BETA, CONTEXT_BETA]}),
            "GF-system-turns-missing": ({"system_turns": 0},),
        }
        for expected, changes in omissions.items():
            for change in changes:
                for label in ("known", "unknown"):
                    for turn, index in ((0, 0), (0, 1), (1, 0)):
                        with self.subTest(expected=expected, change=change, label=label, turn=turn, index=index):
                            self.assertEqual(verdict(self.changed(rows, label, turn, index, **change)), expected)

    def test_a_request_row_keeps_the_promised_facts(self) -> None:
        document = {"model": _LEAD, "output_config": {"effort": "high"}, "context_management": {"edits": []},
                    "tools": [{"name": "Read"}, {"name": "ToolSearch"},
                              {"name": "DeferredToolPlaceholder", "defer_loading": True}],
                    "messages": [{"role": "user", "content": "x"}, {"role": "system", "content": "y"},
                                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t",
                                                               "content": [{"type": "tool_reference",
                                                                            "tool_name": "SendMessage"}]}]}]}
        row = request_row("/v1/messages?beta=true", {"anthropic-beta": ",".join(_ALL_BETAS)}, document)
        self.assertEqual(missing_parts(row), [])
        self.assertEqual((row["system_turns"], row["tool_references"], row["deferred"]),
                         (1, 1, ["DeferredToolPlaceholder"]))


# ------------------------------------------------------------ through the gateway


def gateway_verdict(observed: dict[str, list[dict[str, Any]]], replay: dict[str, Any]) -> list[str]:
    """The failure classes of the run through the gateway (empty: a pass).
    Client side: the fresh request and the ToolSearch continuation
    (``headless-1``), the resumed request (``headless-2``) and the terminal
    session's request before and after ``/model`` each carry every promised
    part, one request per step, every one answered 200. Upstream side (one
    request per client request): the session's effort value with its beta,
    ``context_management``, the system turns and the ``tool_reference``
    blocks the client sent. Replay: each route answers 200 once with the
    fetched tool."""

    rows = [row for case in observed.values() for row in case]
    main = _main(rows)
    terminal = _main(observed["terminal"])
    failed = []
    if any(row["status"] != 200 for row in rows) or any(row["status"] != 200 for row in replay.values()):
        failed.append("GFG-refused")
    if any(row["upstream"] != 1 or not row["fetched-tool"] for row in replay.values()):
        failed.append("GFG-fetched-tool-dropped")
    if [len(_main(observed["headless-1"])), len(_main(observed["headless-2"]))] != [2, 1]:
        failed.append("GFG-resent")
    if [row["model"] for row in terminal] != [_LEAD, _SWITCHED]:
        failed.append("GFG-switch-resent")
    for row in main:
        failed += [f"GFG-{part}-missing" for part in missing_parts(row)]
    if not main:
        failed.append("GFG-no-request")
    for row in main:
        hits = row.get("upstream") or []
        if len(hits) != 1:
            failed.append("GFG-upstream-resent" if hits else "GFG-no-upstream")
        for hit in hits:
            if hit.get("effort") != _EFFORT:
                failed.append("GFG-effort-changed-upstream")
            if EFFORT_BETA not in hit.get("betas", ()):
                failed.append("GFG-effort-beta-dropped-upstream")
            if not hit.get("context_management"):
                failed.append("GFG-context-dropped-upstream")
            if hit.get("system_turns") != row.get("system_turns"):
                failed.append("GFG-system-turns-changed-upstream")
            if hit.get("tool_references") != row.get("tool_references"):
                failed.append("GFG-tool-references-changed-upstream")
    return list(dict.fromkeys(failed))


class GatewayVerdictTests(unittest.TestCase):
    def observed(self) -> tuple[dict[str, Any], dict[str, Any]]:
        def relayed(**fields) -> dict[str, Any]:
            row = _row(**fields)
            hit = {key: row[key] for key in ("route", "model", "effort", "context_management", "betas",
                                             "system_turns", "tool_references")}
            return {**row, "status": 200, "hits": 1, "upstream": [hit]}

        observed = {"headless-1": [relayed(), relayed(refs=1, system=2, deferred=("SendMessage", "DeferredToolPlaceholder"))],
                    "headless-2": [relayed(refs=1, system=3)],
                    "terminal": [relayed(), relayed(model=_SWITCHED, system=2)]}
        replay = {alias: {"status": 200, "upstream": 1, "fetched-tool": True}
                  for alias in ("gwtest-cc-output-config-high", "gwtest-codex-reasoning-effort-high", "gwtest-compat-plain")}
        return observed, replay

    def test_a_clean_run_passes_and_each_loss_names_its_class(self) -> None:
        observed, replay = self.observed()
        self.assertEqual(gateway_verdict(observed, replay), [])
        cases = [
            ("headless-2", 0, "client", {"context_management": False}, "GFG-context-missing"),
            ("headless-1", 1, "client", {"system_turns": 0}, "GFG-system-turns-missing"),
            ("headless-2", 0, "client", {"tools": ["Read"]}, "GFG-tool-search-missing"),
            ("terminal", 1, "client", {"betas": [EFFORT_BETA, TOOL_SEARCH_BETA]}, "GFG-context-missing"),
            ("terminal", 1, "client", {"effort": "low"}, "GFG-effort-missing"),
            ("headless-1", 0, "upstream", {"effort": "low"}, "GFG-effort-changed-upstream"),
            ("terminal", 1, "upstream", {"effort": None}, "GFG-effort-changed-upstream"),
            ("headless-2", 0, "upstream", {"betas": []}, "GFG-effort-beta-dropped-upstream"),
            ("headless-2", 0, "upstream", {"context_management": False}, "GFG-context-dropped-upstream"),
            ("headless-1", 1, "upstream", {"system_turns": 0}, "GFG-system-turns-changed-upstream"),
            ("headless-1", 1, "upstream", {"tool_references": 0}, "GFG-tool-references-changed-upstream"),
        ]
        for case, index, side, change, expected in cases:
            with self.subTest(case=case, index=index, side=side, change=change):
                broken = json.loads(json.dumps(observed))
                target = broken[case][index] if side == "client" else broken[case][index]["upstream"][0]
                target.update(change)
                self.assertEqual(gateway_verdict(broken, replay)[:1], [expected])
        resent = json.loads(json.dumps(observed))
        resent["headless-2"][0]["upstream"].append(resent["headless-2"][0]["upstream"][0])
        self.assertIn("GFG-upstream-resent", gateway_verdict(resent, replay))
        switched = json.loads(json.dumps(observed))
        switched["terminal"][1]["model"] = _LEAD
        self.assertIn("GFG-switch-resent", gateway_verdict(switched, replay))
        dropped = json.loads(json.dumps(replay))
        dropped["gwtest-compat-plain"]["fetched-tool"] = False
        self.assertEqual(gateway_verdict(observed, dropped), ["GFG-fetched-tool-dropped"])


class ShapeThroughGatewayTests(_GatedCase):
    """The same managed turns through the candidate gateway (BOUNDARY without
    one): the Claude account route end to end, and a captured request replayed
    on the key and translation routes."""

    probe_id = "gateway-fallbacks-gateway"

    def test_the_gateway_answers_every_managed_request_once(self) -> None:
        import _gateway_harness as harness
        import test_gateway_hint_client as recognition  # the gateway fixtures (module import: no tests re-exported)

        forced = {"search": True}

        def respond(route, body):
            if route.endswith("/count_tokens"):
                return 200, b'{"input_tokens": 1}', "application/json"
            names = [tool.get("name") for tool in body.get("tools") or [] if isinstance(tool, dict)]
            if forced["search"] and "ToolSearch" in names and body.get("stream"):
                forced["search"] = False
                return recognition.native_reply(body, [{"type": "tool_use", "id": "toolu_gf_gateway",
                                                        "name": "ToolSearch",
                                                        "input": {"query": "select:SendMessage"}}], "tool_use")
            return recognition.native_reply(body, [{"type": "text", "text": "PROBE-OK"}], "end_turn")

        gateway = recognition.start_oauth_gateway(state.ensure_private_dir(self.root / "gateway"), respond)
        self.addCleanup(gateway.close)

        class Relay(recognition._GatewayRelay):
            def _observe(self, path, headers, hits, status, raw, document):
                row = request_row(path, headers, document)
                upstream = [request_row(route, upstream_headers, json.loads(body) if body else {})
                            for upstream_headers, body, route in hits]
                self.rows.append({**row, "status": status, "hits": len(hits), "upstream": upstream})

        def run() -> dict[str, Any]:
            observed: dict[str, Any] = {}
            fixture = self._fixture()
            settings = self._settings(fixture, _LEAD, [_LEAD, _SWITCHED])
            with Relay(gateway) as relay:
                session = str(uuid.uuid4())
                for index, prompt in enumerate(("client check one", "client check two")):
                    before = len(relay.rows)
                    result = probe.run_native(
                        ("-p", prompt, "--settings", str(settings), "--model", _LEAD, "--effort", _EFFORT,
                         *(("--session-id", session) if index == 0 else ("--resume", session))),
                        trusted=self.trusted, fixture=fixture, provider=relay, timeout=150,
                        allow_real=True, environ=self.environ)
                    if result.timed_out or result.returncode != 0:
                        raise _Inconclusive(f"a gateway turn did not complete (rc={result.returncode})")
                    observed[f"headless-{index + 1}"] = list(relay.rows[before:])
                terminal_fixture = self._fixture()
                terminal_settings = self._settings(terminal_fixture, _LEAD, [_LEAD, _SWITCHED])
                step = probe.PTYInteraction

                def pause() -> None:
                    time.sleep(2.5)

                steps = [step(b"Choose", b"2\r"), step(b"Press", b"\r"),
                         step("❯".encode(), b"client check three\r"),
                         step(b"PROBE-OK", b"", preserve_after_wait=True),
                         step("❯".encode(), f"/model {_SWITCHED}\r".encode()),
                         step("❯".encode(), b"\r", before_send=pause),
                         step("❯".encode(), b"client check four\r", before_send=pause),
                         step(b"PROBE-OK", b"", preserve_after_wait=True),
                         step("❯".encode(), b"/exit\r", before_send=pause)]
                before = len(relay.rows)
                result = probe.run_native_pty(
                    ("--settings", str(terminal_settings), "--model", _LEAD, "--effort", _EFFORT), steps,
                    trusted=self.trusted, fixture=terminal_fixture, provider=relay, timeout=180,
                    allow_real=True, environ=self.environ)
                if result.timed_out or result.returncode != 0:
                    raise _Inconclusive("the terminal /model run through the gateway did not complete")
                observed["terminal"] = list(relay.rows[before:])
                errors = relay.completion_errors()
                if errors:
                    raise _Inconclusive("; ".join(errors))
            return observed

        observed = self._guard(run)
        rows = [row for case in observed.values() for row in case]
        main = _main(rows)
        terminal = _main(observed["terminal"])
        upstream = [hit for row in main for hit in row["upstream"]]
        replay = self._guard(lambda: self._replay(harness))
        detail = (
            f"client requests: headless-1=[{_summary(_main(observed['headless-1']))}] "
            f"headless-2=[{_summary(_main(observed['headless-2']))}] terminal=[{_summary(terminal)}] "
            f"statuses={sorted({row['status'] for row in rows})} "
            f"upstream effort={sorted({str(hit.get('effort')) for hit in upstream})} "
            f"upstream effort-beta={sorted({EFFORT_BETA in hit['betas'] for hit in upstream})} "
            f"upstream context_management={sorted({bool(hit.get('context_management')) for hit in upstream})} "
            f"upstream system-turns={sorted({int(hit.get('system_turns') or 0) for hit in upstream})} "
            f"upstream tool_references={sorted({int(hit.get('tool_references') or 0) for hit in upstream})} "
            f"replay={replay} gateway={gateway.version} client={self.gate.version}")
        failed = gateway_verdict(observed, replay)
        observed_class = failed[0] if failed else "GFG-accepted-once"
        type(self).verdict = ("PASS" if not failed else "FAIL", observed_class, detail)
        self.assertEqual(failed, [], detail)

    def _replay(self, harness) -> dict[str, Any]:
        """A captured client request (the ToolSearch continuation: deferred
        tool, tool_reference result, system turns, context_management,
        effort) replayed on the harness gateway's Claude key, Codex and
        OpenAI-compatible routes: the status each answers and whether the
        fetched deferred tool reaches the upstream's tool list."""

        fixture = self._fixture()
        provider = _Provider(search_once=True)
        with provider:
            self._headless(fixture, self._settings(fixture, _LEAD, [_LEAD]), _LEAD, provider, ("client check",))
        captured = [(headers, body) for headers, body in provider.captured
                    if request_row("/v1/messages", headers, json.loads(body)).get("tool_references")]
        if not captured:
            raise _Inconclusive("no ToolSearch continuation was captured for the replay")
        headers, body = captured[0]
        gateway = harness.GatewayHarness()
        self.addCleanup(gateway.close)
        statuses = {}
        for alias in ("gwtest-cc-output-config-high", "gwtest-codex-reasoning-effort-high", "gwtest-compat-plain"):
            document = json.loads(body)
            document["model"] = alias
            forwarded = {name: value for name, value in headers.items()
                         if name not in {"host", "authorization", "x-api-key", "content-length"}}
            before = len(gateway.upstream.hits)
            reply = harness.unix_request(gateway.socket, "POST", "/v1/messages",
                                         headers={**forwarded, **gateway.headers()},
                                         body=json.dumps(document).encode(), timeout=45)
            sent = [hit.body for hit in gateway.upstream.hits[before:]]
            names = {str(tool.get("name") or (tool.get("function") or {}).get("name"))
                     for body in sent for tool in body.get("tools") or [] if isinstance(tool, dict)}
            statuses[alias] = {"status": reply.status, "upstream": len(sent),
                               "fetched-tool": "SendMessage" in names}
        return statuses


# --------------------------------------------------------------- HTTP MCP retry


_MCP_SERVER = "httpfx"
_MCP_TOOLS = {"change": {"destructiveHint": True, "readOnlyHint": False, "idempotentHint": False},
              "lookup": {"readOnlyHint": True}}
_MCP_HOLD = 8.0


class _McpHandler(probe._ProviderHandler):
    """``/mcp/<server>``: a streamable-HTTP MCP server on the provider's own
    port. The session's first ``tools/call`` is held unanswered while every
    server-message stream (``GET``) is reset: three such terminal errors make
    the client close the transport with the call pending (its "connection
    closed" during a call), then the held connection is dropped. Every later
    call is answered."""

    def _reset(self) -> None:
        self.close_connection = True
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def do_GET(self) -> None:  # noqa: N802
        if not self.path.startswith("/mcp/"):
            super().do_GET()
            return
        provider = self.server.provider  # type: ignore[attr-defined]
        if not provider.holding.is_set():
            # A live server-message stream until a call is held.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.flush()
            provider.holding.wait(60)
        self._reset()

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
            result = {"tools": [{"name": name, "description": f"client check {name} tool",
                                 "inputSchema": {"type": "object", "properties": {}}, "annotations": hints}
                                for name, hints in _MCP_TOOLS.items()]}
        elif method == "tools/call":
            name = str(message.get("params", {}).get("name"))
            with provider.mcp_lock:
                provider.mcp_calls[name] = provider.mcp_calls.get(name, 0) + 1
                held = not provider.held
                provider.held = True
            if held:
                provider.holding.set()
                time.sleep(_MCP_HOLD)
                provider.holding.clear()
                self._reset()
                return
            result = {"content": [{"type": "text", "text": "MCP-RETRY-ANSWERED"}]}
        else:
            result = None
        answer = {"result": result} if result is not None else {"error": {"code": -32601, "message": "unknown"}}
        data = json.dumps({"jsonrpc": "2.0", "id": ident, **answer}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class _McpProvider(probe.FakeAnthropicProvider):
    """The fixture gateway plus the MCP server; the model fetches the
    deferred MCP tools with ``ToolSearch``, calls ``order`` one by one, then
    answers."""

    def __init__(self, order: tuple[str, ...]) -> None:
        super().__init__(responder=self)
        self.mcp_calls: dict[str, int] = {}
        self.mcp_lock = threading.Lock()
        self.holding = threading.Event()
        self.held = False
        self.results: dict[str, str] = {}
        self.searched = False
        self._pending = list(order)

    def start(self):
        super().start()
        self._server.RequestHandlerClass = _McpHandler
        return self

    def unix_socket(self, directory):
        path = super().unix_socket(directory)
        self._unix_servers[path][0].RequestHandlerClass = _McpHandler
        return path

    def respond(self, document, path, context):
        if path.split("?", 1)[0].endswith("/count_tokens"):
            return 200, {"input_tokens": 1}
        offered = {tool.get("name") for tool in document.get("tools") or [] if isinstance(tool, dict)}
        for message in document.get("messages") or []:
            for block in message.get("content") if isinstance(message.get("content"), list) else ():
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tool = str(block.get("tool_use_id", "")).removeprefix("toolu_mr_")
                    if tool in _MCP_TOOLS:
                        self.results[tool] = ("answered" if "MCP-RETRY-ANSWERED" in json.dumps(block.get("content"))
                                              else "error")
        wanted = [f"mcp__{_MCP_SERVER}__{name}" for name in _MCP_TOOLS]
        if self._pending and not self.searched and "ToolSearch" in offered and not set(wanted) & offered:
            # Behind a custom base URL the client defers MCP tools: fetch them first.
            self.searched = True
            return 200, _payload(document, [{"type": "tool_use", "id": "toolu_mr_search", "name": "ToolSearch",
                                             "input": {"query": "select:" + ",".join(wanted)}}],
                                 "tool_use", context.ordinal)
        if self._pending and f"mcp__{_MCP_SERVER}__{self._pending[0]}" in offered:
            name = self._pending.pop(0)
            return 200, _payload(document, [{"type": "tool_use", "id": f"toolu_mr_{name}",
                                             "name": f"mcp__{_MCP_SERVER}__{name}", "input": {}}],
                                 "tool_use", context.ordinal)
        return 200, _payload(document, [{"type": "text", "text": "PROBE-OK"}], "end_turn", context.ordinal)


def mcp_verdict(observed: dict[str, dict[str, Any]]) -> str:
    """``change-first``: the changing call is the one held; ``lookup-first``:
    the read-only call is. A held call the client sends once more counts 2."""

    lookup = observed["lookup-first"]["calls"].get("lookup", 0)
    if lookup < 2:
        raise _Inconclusive(f"the held read-only call was not sent again ({observed['lookup-first']})")
    change = observed["change-first"]["calls"].get("change", 0)
    if change == 0:
        raise _Inconclusive(f"the changing call was never made ({observed['change-first']})")
    return MCP_PASS_CLASS if change == 1 else "MR-changing-resent"


class HttpMcpRetryProbe(_GatedCase):
    probe_id = "mcp-http-retry"

    def _run(self, trusted: probe.TrustedExecutable, order: tuple[str, ...]) -> dict[str, Any]:
        fixture = self._fixture()
        provider = _McpProvider(order)
        with provider:
            config_path = fixture.claude_config_dir / ".claude.json"
            config = json.loads(state.read_private(config_path))
            config["mcpServers"] = {_MCP_SERVER: {"type": "http",
                                                  "url": f"http://127.0.0.1:{provider.port}/mcp/{_MCP_SERVER}"}}
            state.atomic_write(config_path, json.dumps(config).encode())
            settings = self._settings(fixture, _LEAD, [_LEAD])
            result = probe.run_native(
                ("-p", "client check mcp", "--settings", str(settings), "--model", _LEAD,
                 "--allowedTools", ",".join(f"mcp__{_MCP_SERVER}__{name}" for name in _MCP_TOOLS)),
                trusted=trusted, fixture=fixture, provider=provider, timeout=150, allow_real=True,
                environ=self.environ)
        if result.timed_out or result.returncode != 0:
            raise _Inconclusive(f"the MCP run did not complete (rc={result.returncode})")
        if set(provider.results) != set(_MCP_TOOLS):
            raise _Inconclusive(f"the model's MCP calls did not both return ({provider.results}, "
                                f"server calls {provider.mcp_calls})")
        return {"calls": dict(provider.mcp_calls), "results": dict(provider.results),
                "deferred": provider.searched}

    def _observe(self, trusted: probe.TrustedExecutable) -> dict[str, dict[str, Any]]:
        return {"change-first": self._run(trusted, ("change", "lookup")),
                "lookup-first": self._run(trusted, ("lookup", "change"))}

    def test_a_changing_tool_call_is_sent_once(self) -> None:
        observed = self._guard(lambda: self._observe(self.trusted))
        observed_class = self._guard(lambda: mcp_verdict(observed))
        previous = fallback_control_client()
        control = (f"fallback control unavailable ({_FALLBACK_CONTROL[0]} not named by "
                   f"{FALLBACK_CONTROL_ENV})")
        if previous is not None:
            before = self._guard(lambda: self._observe(previous))
            self.assertEqual(self._guard(lambda: mcp_verdict(before)), "MR-changing-resent",
                             f"the fallback control no longer resends the held changing call: {before}")
            control = (f"fallback control {_FALLBACK_CONTROL[0]}: change-first {before['change-first']['calls']}, "
                       f"lookup-first {before['lookup-first']['calls']}")
        detail = (f"change-first calls={observed['change-first']['calls']} "
                  f"results={observed['change-first']['results']}; lookup-first "
                  f"calls={observed['lookup-first']['calls']} results={observed['lookup-first']['results']}; "
                  f"tools deferred behind ToolSearch={observed['change-first']['deferred']} ({control}) "
                  f"client={self.gate.version} sha256={self.trusted.sha256[:12]}")
        type(self).verdict = ("PASS" if observed_class == MCP_PASS_CLASS else "FAIL", observed_class, detail)
        self.assertEqual(observed_class, MCP_PASS_CLASS, detail)


class McpVerdictTests(unittest.TestCase):
    def test_the_class_needs_a_retried_read_only_call_and_a_single_changing_call(self) -> None:
        def case(change: int, lookup: int) -> dict[str, Any]:
            return {"calls": {"change": change, "lookup": lookup}}

        self.assertEqual(mcp_verdict({"change-first": case(1, 1), "lookup-first": case(1, 2)}), MCP_PASS_CLASS)
        self.assertEqual(mcp_verdict({"change-first": case(2, 1), "lookup-first": case(1, 2)}),
                         "MR-changing-resent")
        with self.assertRaises(_Inconclusive):
            mcp_verdict({"change-first": case(1, 1), "lookup-first": case(1, 1)})
        with self.assertRaises(_Inconclusive):
            mcp_verdict({"change-first": case(0, 1), "lookup-first": case(1, 2)})


# ------------------------------------------------------------------ keepalive


_PING_SECONDS, _PING_EVERY = 50.0, 5.0


class _KeepaliveHandler(probe._ProviderHandler):
    """A streamed main answer: ``message_start``, then only ``ping`` events
    for ``_PING_SECONDS``, then the content (chunked transfer)."""

    def _send_sse(self, status: int, payload: probe.SseResponse, headers=()) -> None:
        provider = self.server.provider  # type: ignore[attr-defined]
        with provider.hold_lock:
            hold, provider.hold_next = provider.hold_next, False
        if not hold:
            super()._send_sse(status, payload, headers)
            return
        self.send_response(status)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def chunk(event: str, data: dict[str, Any]) -> None:
            frame = f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()
            self.wfile.write(f"{len(frame):x}\r\n".encode() + frame + b"\r\n")
            self.wfile.flush()

        events = list(payload.events)
        chunk(*events[0])
        deadline = time.monotonic() + _PING_SECONDS
        while time.monotonic() < deadline:
            time.sleep(_PING_EVERY)
            chunk("ping", {"type": "ping"})
        for event in events[1:]:
            chunk(*event)
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


class _KeepaliveProvider(probe.FakeAnthropicProvider):
    def __init__(self) -> None:
        super().__init__(responder=self)
        self.hold_next = True
        self.hold_lock = threading.Lock()
        self.main = 0

    def start(self):
        super().start()
        self._server.RequestHandlerClass = _KeepaliveHandler
        return self

    def unix_socket(self, directory):
        path = super().unix_socket(directory)
        self._unix_servers[path][0].RequestHandlerClass = _KeepaliveHandler
        return path

    def respond(self, document, path, context):
        if path.split("?", 1)[0].endswith("/count_tokens"):
            return 200, {"input_tokens": 1}
        if document.get("tools"):
            self.main += 1
        return 200, _payload(document, [{"type": "text", "text": "PROBE-KEEPALIVE-OK"}], "end_turn",
                             context.ordinal)


class KeepaliveProbe(_GatedCase):
    probe_id = "keepalive"

    def test_a_ping_only_stretch_completes_as_one_request(self) -> None:
        def run() -> dict[str, Any]:
            fixture = self._fixture()
            provider = _KeepaliveProvider()
            with provider:
                started = time.monotonic()
                result = probe.run_native(
                    ("-p", "client check keepalive", "--settings", str(self._settings(fixture, _LEAD, [_LEAD])),
                     "--model", _LEAD, "--output-format", "json"),
                    trusted=self.trusted, fixture=fixture, provider=provider, timeout=_PING_SECONDS + 120,
                    allow_real=True, environ=self.environ)
                elapsed = time.monotonic() - started
            if result.timed_out:
                raise _Inconclusive("the keepalive run timed out")
            outcome = json.loads(result.stdout or "{}")
            return {"returncode": result.returncode, "main": provider.main, "elapsed": round(elapsed),
                    "is_error": outcome.get("is_error"), "result": outcome.get("result")}

        observed = self._guard(run)
        if observed["elapsed"] < _PING_SECONDS:
            type(self).verdict = ("INCONCLUSIVE", "unavailable", f"the held stream was not exercised ({observed})")
            raise _Inconclusive(f"the held stream was not exercised ({observed})")
        ok = (observed["returncode"] == 0 and observed["main"] == 1 and not observed["is_error"]
              and observed["result"] == "PROBE-KEEPALIVE-OK")
        observed_class = KEEPALIVE_PASS_CLASS if ok else ("KA-resent" if observed["main"] > 1 else "KA-failed")
        detail = (f"pings={int(_PING_SECONDS)}s every {int(_PING_EVERY)}s main-requests={observed['main']} "
                  f"elapsed={observed['elapsed']}s rc={observed['returncode']} is_error={observed['is_error']} "
                  f"(CLAUDE_CODE_RETRY_WATCHDOG=1) client={self.gate.version} sha256={self.trusted.sha256[:12]}")
        type(self).verdict = ("PASS" if ok else "FAIL", observed_class, detail)
        self.assertEqual(observed_class, KEEPALIVE_PASS_CLASS, detail)


# ------------------------------------------------------------ the watchdog's scope

WATCHDOG_SCOPE_PASS_CLASS = "WS-broad"
_WATCHDOG_ENV = "CLAUDE_CODE_RETRY_WATCHDOG"
_WATCHDOG_SCOPE_ENV = "CLAUDE_CODE_RETRY_WATCHDOG_SCOPE"
# The retry budget the client announces in its debug log on the first retry:
# 300 retries under the broad watchdog, 10 when its scope is "capacity".
_ATTEMPT_RE = re.compile(r"API error \(attempt 1/(\d+)\)")
_BROAD_BUDGET, _CAPACITY_BUDGET = 301, 11
_LAYERS = ("user", "project", "local")


def compiled_watchdog_env() -> dict[str, str]:
    """The retry watchdog lines of the production compile's flag settings env
    (a managed launch of the balanced seed)."""

    import test_compiler_v2  # the production compile (module import: no tests re-exported)

    _lineup, result = test_compiler_v2._seed_launch("balanced")
    env = result.scope_plan.settings["env"]
    return {key: env[key] for key in (_WATCHDOG_ENV, _WATCHDOG_SCOPE_ENV) if key in env}


def watchdog_cases() -> tuple[tuple[str, bool, str, bool], ...]:
    """``(name, compiled, the layer holding scope "capacity", resumed)``."""

    rows = []
    for compiled in (False, True):
        prefix = "compiled" if compiled else "control"
        rows += [(f"{prefix}-{layer}", compiled, layer, False) for layer in _LAYERS]
        rows.append((f"{prefix}-user-resume", compiled, "user", True))
    return tuple(rows)


def watchdog_verdict(observed: dict[str, int | None]) -> str:
    """Every control (the compiled watchdog without its scope line) must take
    the capacity budget from the conflicting layer; every compiled run must
    keep the broad budget."""

    for name, budget in observed.items():
        if name.startswith("control-") and budget != _CAPACITY_BUDGET:
            raise _Inconclusive(f"{name}: the capacity scope did not reach the client (budget {budget})")
    compiled = {name: budget for name, budget in observed.items() if name.startswith("compiled-")}
    if not compiled:
        raise _Inconclusive("no compiled run")
    if any(budget == _CAPACITY_BUDGET for budget in compiled.values()):
        return "WS-capacity-wins"
    if any(budget != _BROAD_BUDGET for budget in compiled.values()):
        raise _Inconclusive("a compiled run announced no retry budget")
    return WATCHDOG_SCOPE_PASS_CLASS


class WatchdogVerdictTests(unittest.TestCase):
    def test_the_compile_pins_the_broad_scope_beside_the_watchdog(self) -> None:
        self.assertEqual(_WATCHDOG_SCOPE_ENV, scope.RETRY_WATCHDOG_SCOPE_ENV)
        self.assertEqual(compiled_watchdog_env(), {_WATCHDOG_ENV: "1", _WATCHDOG_SCOPE_ENV: ""})

    def test_pass_needs_every_control_and_every_compiled_run(self) -> None:
        rows = {name: (_BROAD_BUDGET if compiled else _CAPACITY_BUDGET)
                for name, compiled, _layer, resumed in watchdog_cases()}
        rows.update({f"{name}-fresh": rows[name] for name, _c, _l, resumed in watchdog_cases() if resumed})
        self.assertEqual(watchdog_verdict(rows), WATCHDOG_SCOPE_PASS_CLASS)
        self.assertEqual(watchdog_verdict(dict(rows, **{"compiled-local": _CAPACITY_BUDGET})), "WS-capacity-wins")
        self.assertEqual(watchdog_verdict(dict(rows, **{"compiled-user-resume": _CAPACITY_BUDGET})),
                         "WS-capacity-wins")
        for broken in ({"control-project": _BROAD_BUDGET}, {"compiled-user": None}, {"control-user-resume": None}):
            with self.subTest(broken=broken), self.assertRaises(_Inconclusive):
                watchdog_verdict(dict(rows, **broken))
        with self.assertRaises(_Inconclusive):
            watchdog_verdict({name: budget for name, budget in rows.items() if name.startswith("control-")})

    def test_the_budget_is_read_from_the_first_retry_line(self) -> None:
        line = "2026-10-25T00:00:00.000Z [ERROR] API error (attempt 1/301): 500 fixture server error"
        self.assertEqual([int(match.group(1)) for match in _ATTEMPT_RE.finditer(line)], [301])
        self.assertEqual(list(_ATTEMPT_RE.finditer("API error (attempt 2/11): 500")), [])


class _OneServerError(probe.FakeAnthropicProvider):
    """Answers a turn's first main request with a server error (which every
    watchdog scope retries), then the content."""

    def __init__(self) -> None:
        super().__init__(responder=self)
        self.lock = threading.Lock()
        self.failed = False
        self.main = 0

    def respond(self, document, path, context):
        if path.split("?", 1)[0].endswith("/count_tokens"):
            return 200, {"input_tokens": 1}
        with self.lock:
            if document.get("tools"):
                self.main += 1
                if not self.failed:
                    self.failed = True
                    return 500, {"type": "error", "error": {"type": "api_error", "message": "fixture server error"}}
        return 200, _payload(document, [{"type": "text", "text": "PROBE-WATCHDOG-OK"}], "end_turn", context.ordinal)


class WatchdogScopeProbe(_GatedCase):
    """Claude Code 2.1.310 narrows the retry watchdog to capacity errors when
    ``CLAUDE_CODE_RETRY_WATCHDOG_SCOPE`` is ``capacity`` (its only value).
    The production compile's watchdog lines in the flag settings, against that
    value in the user, project or local settings env, fresh and on
    ``--resume``: the client keeps the broad budget. The control (the same
    lines without the scope pin) takes the capacity budget, so the conflicting
    layer is real. A launch clears an inherited process value
    (``compiler.V2_ENV_UNSET``); the probe harness admits no such override."""

    probe_id = "watchdog-scope"

    def _turn(self, fixture: probe.ProbeFixture, settings: Path, extra: tuple[str, ...]) -> int | None:
        debug = fixture.root / f"debug-{uuid.uuid4().hex[:8]}.log"
        provider = _OneServerError()
        with provider:
            result = probe.run_native(
                ("-p", "client check watchdog-scope", "--settings", str(settings), "--model", _LEAD,
                 "--debug-file", str(debug), *extra),
                trusted=self.trusted, fixture=fixture, provider=provider, timeout=150, allow_real=True,
                environ=self.environ)
        if result.timed_out or result.returncode != 0:
            raise _Inconclusive(f"a watchdog-scope turn did not complete (rc={result.returncode})")
        if provider.main < 2:
            raise _Inconclusive("the server error was not retried")
        text = debug.read_text(encoding="utf-8", errors="replace") if debug.is_file() else ""
        budgets = {int(match.group(1)) for match in _ATTEMPT_RE.finditer(text)}
        return budgets.pop() if len(budgets) == 1 else None

    def test_the_compiled_watchdog_keeps_its_broad_scope(self) -> None:
        compiled_env = compiled_watchdog_env()
        control_env = {key: value for key, value in compiled_env.items() if key != _WATCHDOG_SCOPE_ENV}

        def run() -> dict[str, int | None]:
            observed: dict[str, int | None] = {}
            for name, compiled, layer, resumed in watchdog_cases():
                fixture = self._fixture()
                target = {"user": fixture.claude_config_dir / "settings.json",
                          "project": fixture.project_dir / ".claude/settings.json",
                          "local": fixture.project_dir / ".claude/settings.local.json"}[layer]
                probe.write_fixture_settings(fixture, str(target.relative_to(fixture.root)),
                                             {"env": {_WATCHDOG_SCOPE_ENV: "capacity"}})
                settings = probe.write_fixture_settings(fixture, "scope/settings.json", {
                    "availableModels": [_LEAD], "model": _LEAD,
                    "permissions": {"defaultMode": scope.PERMISSION_DEFAULT_MODE},
                    "env": {**(compiled_env if compiled else control_env), scope.FAST_MODE_ENV: "1"}})
                extra: tuple[str, ...] = ()
                if resumed:
                    session = str(uuid.uuid4())
                    observed[f"{name}-fresh"] = self._turn(fixture, settings, ("--session-id", session))
                    extra = ("--resume", session)
                observed[name] = self._turn(fixture, settings, extra)
            return observed

        observed = self._guard(run)
        detail = (" ".join(f"{name}={budget}" for name, budget in observed.items())
                  + f" compiled={compiled_env} client={self.gate.version} sha256={self.trusted.sha256[:12]}")
        try:
            observed_class = watchdog_verdict(observed)
        except _Inconclusive as exc:
            type(self).verdict = ("INCONCLUSIVE", "unavailable", f"{exc}; {detail}")
            raise
        type(self).verdict = ("PASS" if observed_class == WATCHDOG_SCOPE_PASS_CLASS else "FAIL", observed_class,
                              detail)
        self.assertEqual(observed_class, WATCHDOG_SCOPE_PASS_CLASS, detail)


# ------------------------------------------------------- the API's "do not retry"

DO_NOT_RETRY_CLASS = "DNR-honored"
# (case, status, the error type, x-should-retry or None): the first main
# request of the turn gets this answer, every later one the content.
_DNR_CASES = (
    ("500", 500, "api_error", None),
    ("500-should-retry-false", 500, "api_error", "false"),
    ("529-should-retry-false", 529, "overloaded_error", "false"),
    ("429-should-retry-false", 429, "rate_limit_error", "false"),
    ("529-should-retry-true", 529, "overloaded_error", "true"),
)


class _OneRefusal(probe.FakeAnthropicProvider):
    """Answers a turn's first main request with ``status`` (and the header),
    then the content."""

    def __init__(self, status: int, kind: str, should_retry: str | None) -> None:
        super().__init__(responder=self)
        self.status, self.kind, self.should_retry = status, kind, should_retry
        self.lock = threading.Lock()
        self.failed = False
        self.main = 0

    def respond(self, document, path, context):
        if path.split("?", 1)[0].endswith("/count_tokens"):
            return 200, {"input_tokens": 1}
        with self.lock:
            if document.get("tools"):
                self.main += 1
                if not self.failed:
                    self.failed = True
                    headers = (("x-should-retry", self.should_retry),) if self.should_retry else ()
                    if self.status == 429:
                        headers += (("retry-after", "1"),)
                    return probe.ProviderReply(self.status, {"type": "error", "error": {
                        "type": self.kind, "message": "fixture refusal"}}, headers)
        return 200, _payload(document, [{"type": "text", "text": "PROBE-DNR-OK"}], "end_turn", context.ordinal)


def do_not_retry_verdict(observed: dict[str, dict[str, Any]]) -> str:
    """The control (a plain 500) must be retried under the compiled
    watchdog; the class says what an explicit ``x-should-retry: false`` does."""

    control = observed["500"]
    if control["main"] < 2 or control["rc"] != 0:
        raise _Inconclusive(f"the plain 500 was not retried ({control})")
    refused = [observed[name] for name in observed if name.endswith("should-retry-false")]
    if all(row["main"] == 1 and row["rc"] != 0 for row in refused):
        return DO_NOT_RETRY_CLASS
    if all(row["main"] >= 2 for row in refused):
        return "DNR-retried"
    return "DNR-mixed"


class DoNotRetryVerdictTests(unittest.TestCase):
    def test_the_class_follows_the_refused_cases(self) -> None:
        rows = {"500": {"main": 2, "rc": 0}, "500-should-retry-false": {"main": 1, "rc": 1},
                "529-should-retry-false": {"main": 1, "rc": 1}, "529-should-retry-true": {"main": 2, "rc": 0}}
        self.assertEqual(do_not_retry_verdict(rows), DO_NOT_RETRY_CLASS)
        self.assertEqual(do_not_retry_verdict(dict(rows, **{"500-should-retry-false": {"main": 2, "rc": 0},
                                                            "529-should-retry-false": {"main": 2, "rc": 0}})),
                         "DNR-retried")
        self.assertEqual(do_not_retry_verdict(dict(rows, **{"500-should-retry-false": {"main": 2, "rc": 0}})),
                         "DNR-mixed")
        with self.assertRaises(_Inconclusive):
            do_not_retry_verdict(dict(rows, **{"500": {"main": 1, "rc": 1}}))


class DoNotRetryProbe(_GatedCase):
    """From 2.1.311 the client honours an API answer that says not to retry
    (``x-should-retry: false``) on a 529 or 429 too, instead of the retry
    watchdog retrying it (2.1.310 already did on a 500). Each case runs the
    compiled watchdog lines (broad scope): a plain 500 is retried (the
    control); the class records what each refused answer does, with
    ``x-should-retry: true`` as the retried counterpart. Whether charmer's
    gateway passes such a header through from an upstream is the gateway's
    own row."""

    probe_id = "do-not-retry"

    def test_the_compiled_watchdog_against_do_not_retry(self) -> None:
        compiled_env = compiled_watchdog_env()

        def run() -> dict[str, dict[str, Any]]:
            observed: dict[str, dict[str, Any]] = {}
            for name, status, kind, should_retry in _DNR_CASES:
                fixture = self._fixture()
                settings = probe.write_fixture_settings(fixture, "scope/settings.json", {
                    "availableModels": [_LEAD], "model": _LEAD,
                    "permissions": {"defaultMode": scope.PERMISSION_DEFAULT_MODE},
                    "env": {**compiled_env, scope.FAST_MODE_ENV: "1"}})
                provider = _OneRefusal(status, kind, should_retry)
                started = time.monotonic()
                with provider:
                    result = probe.run_native(
                        ("-p", "client check do-not-retry", "--settings", str(settings), "--model", _LEAD),
                        trusted=self.trusted, fixture=fixture, provider=provider, timeout=180, allow_real=True,
                        environ=self.environ)
                if result.timed_out:
                    raise _Inconclusive(f"{name}: the turn timed out")
                observed[name] = {"main": provider.main, "rc": result.returncode,
                                  "seconds": round(time.monotonic() - started)}
            return observed

        observed = self._guard(run)
        detail = (" ".join(f"{name}=requests:{row['main']},rc:{row['rc']},{row['seconds']}s"
                           for name, row in observed.items())
                  + f" compiled={compiled_env} client={self.gate.version} sha256={self.trusted.sha256[:12]}")
        try:
            observed_class = do_not_retry_verdict(observed)
        except _Inconclusive as exc:
            type(self).verdict = ("INCONCLUSIVE", "unavailable", f"{exc}; {detail}")
            raise
        type(self).verdict = ("MEASURED", observed_class, detail)
        # Measured: 2.1.311 ends the turn on each refused answer; 2.1.310
        # still retried a 529 or 429 that said not to (DNR-mixed).
        self.assertEqual(observed_class, DO_NOT_RETRY_CLASS, detail)


if __name__ == "__main__":
    unittest.main()
