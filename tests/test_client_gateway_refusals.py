"""Client check refusal-routing-gateway: the refusal matrix's
gateway-in-the-loop rows, the pinned client through the candidate gateway
(``CHARMER_TEST_CLI_PROXY_API``; BOUNDARY without one).

Every row runs the balanced seed's managed compile (its fence, its flag
settings env with the refusal-switch counter, the six lifecycle hooks
through charmer's launcher: ``test_client_models.compiled_managed_scope``)
headless on its Opus lead, through ``_client_gateway_stream.StreamRelay``
(bytes passed on as they arrive) and the gateway's Claude OAuth pool to a
scripted upstream. The upstream answers the main requests on the lead's
upstream model as the row says and everything else with PROBE-OK:

* ``stream-refusal``: a streamed ``stop_reason: refusal`` with category
  ``cyber`` on every such request. Specified as directly: the turn switches
  to the route table's target when the fence admits it
  (``test_client_models.expected_switch``) and to nothing else. The same
  case straight against the fake provider (``direct``) is its control.
* ``refusal-400``: the refusal as an HTTP 400 error on every such request.
  No switch, no request on another model; the turn fails.
* ``mid-stream-cut``: the first such request's stream is cut after its first
  text delta (no ``message_stop``); later ones are answered. No switch, no
  request on another model.

Rows are metadata (models, request classes, counts, statuses, the
PreModelSwitch decisions charmer's hook saw). The output line:
``client check refusal-routing-gateway: <STATUS> <identity> <rows>``.
"""

from __future__ import annotations

import hashlib
import json
import threading
import unittest
import uuid
from pathlib import Path
from typing import Any, Callable

from charmer import probe, state
from charmer.probe import ProbeError

import _client_gateway_stream as stream
import test_client_gateway_fallbacks as fallbacks  # module import: no tests re-exported
import test_client_models as models  # module import: no tests re-exported
from _catalog import uses_shipped_catalog

_SOURCE = models._ROUTING_SOURCES["opus"]
_CATEGORY = "cyber"
_TIMEOUT = 180.0
_REFUSAL_400 = {"type": "error", "error": {"type": "invalid_request_error",
                                           "message": "The model refused this request (stop_reason: refusal)."}}

Behaviour = Callable[[stream.UpstreamTurn, dict[str, Any], int], None]


def _refusal(turn: stream.UpstreamTurn, body: dict[str, Any], _nth: int) -> None:
    reply = models._refusal_reply(body.get("model"), _CATEGORY, bool(body.get("stream")))
    if isinstance(reply, probe.SseResponse):
        turn.headers()
        for kind, data in reply.events:
            turn.chunk(stream.harness.sse_frame(kind, data))
        turn.end()
    else:
        turn.full(200, json.dumps(reply).encode(), "application/json")


def _refusal_400(turn: stream.UpstreamTurn, _body: dict[str, Any], _nth: int) -> None:
    turn.full(400, json.dumps(_REFUSAL_400).encode(), "application/json")


def _cut(turn: stream.UpstreamTurn, body: dict[str, Any], nth: int) -> None:
    if nth > 1 or not body.get("stream"):
        stream.native_text(turn, body)
        return
    turn.headers()
    for frame in turn.events(body, [{"type": "text", "text": "PROBE-PARTIAL"}], "end_turn"):
        turn.chunk(frame)
        if frame.startswith(b"event: content_block_delta"):
            break
    turn.cut()


ROWS: dict[str, tuple[Behaviour, bool]] = {
    # name: (upstream behaviour on the lead's main requests, a switch is specified)
    "stream-refusal": (_refusal, True),
    "refusal-400": (_refusal_400, False),
    "mid-stream-cut": (_cut, False),
}


def parts(request: dict[str, Any]) -> str:
    """The request parts the gateway-fallbacks lane follows, as one token:
    effort, context management, tool search, system turns."""

    return "+".join(name for name, present in (
        ("effort", bool(request.get("effort"))), ("ctx", bool(request.get("context_management"))),
        ("search", "ToolSearch" in request.get("tools", ())), ("sys", bool(request.get("system_turns"))))
        if present) or "none"


def row_problems(name: str, observed: dict[str, Any], expected: str | None) -> list[str]:
    """What keeps a row off its specified outcome (empty: met)."""

    wire = models._wire(_SOURCE)
    main = observed["main_models"]
    switched = sorted(set(main) - {wire})
    problems = []
    if not main or main[0] != wire:
        problems.append(f"first main request on {main[:1]}, not the source {wire}")
    if switched != ([] if expected is None else [expected]):
        problems.append(f"switched to {switched or 'nothing'}")
    if name == "stream-refusal" and (observed["rc"] == 0) != (expected is not None):
        problems.append(f"exit {observed['rc']}")
    if name == "refusal-400" and observed["rc"] == 0:
        problems.append("the turn succeeded")
    if observed["timed_out"]:
        problems.append("the run timed out")
    if not {"start", "end"} <= set(observed["hook_events"]):
        problems.append(f"the compiled hooks did not run (events {observed['hook_events']})")
    return problems


class _Direct400:
    """The refusal-400 row's control: every main request on the source's
    wire answered with the same HTTP 400; the rest PROBE-OK. Rows are
    metadata."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def respond(self, document: dict[str, Any], path: str, context: Any) -> Any:
        if path.split("?", 1)[0].endswith("/count_tokens"):
            return 200, {"input_tokens": 1}
        hints = dict(context.record.hint_header_values)
        self.rows.append({"model": document.get("model"),
                          "request_class": hints.get("x-claude-code-request-class") or "main",
                          "request": fallbacks.request_row(path, {}, document)})
        if document.get("tools") and document.get("model") == models._wire(_SOURCE):
            return 400, _REFUSAL_400
        payload = probe._message_payload([{"type": "text", "text": "PROBE-OK"}], model=document.get("model"),
                                         stop_reason="end_turn", message_id="msg_direct_400")
        return 200, probe._sse_message(payload) if document.get("stream") else payload


@uses_shipped_catalog
class GatewayRefusalRowsProbe(models._SpikeTestCase):
    probe_id = "refusal-routing-gateway"
    verdict: tuple[str, str] = ("INCONCLUSIVE", "check did not complete")

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        if cls.boundary_skip is not None:
            cls.verdict = ("BOUNDARY", cls.boundary_skip)

    @classmethod
    def tearDownClass(cls) -> None:
        status, detail = cls.verdict
        print(f"client check {cls.probe_id}: {status} {detail}".rstrip(), flush=True)
        super().tearDownClass()

    def _argv(self, managed: dict[str, Any]) -> tuple[str, ...]:
        return ("-p", models._TURN, "--session-id", str(uuid.uuid4()), "--settings", str(managed["settings"]),
                "--model", _SOURCE, "--effort", managed["effort"], "--add-dir", str(managed["live"]))

    def _row(self, name: str, behaviour: Behaviour) -> dict[str, Any]:
        clock = stream.Clock()
        fixture = self._fixture()
        managed = models.compiled_managed_scope(fixture, "balanced")
        self.assertIn(_SOURCE, managed["available"])
        lead = {"model": None, "count": 0}
        lock = threading.Lock()

        def script(turn: stream.UpstreamTurn, route: str, body: dict[str, Any]) -> None:
            if route.endswith("/count_tokens"):
                turn.full(200, b'{"input_tokens": 1}', "application/json")
                return
            with lock:
                if body.get("tools") and lead["model"] is None:
                    lead["model"] = body.get("model")
                ours = bool(body.get("tools")) and body.get("model") == lead["model"]
                if ours:
                    lead["count"] += 1
                nth = lead["count"]
            if ours:
                behaviour(turn, body, nth)
            else:
                stream.native_text(turn, body)

        gateway = stream.start_gateway(state.ensure_private_dir(self.root / f"gateway-{name}"), script, clock)
        self.addCleanup(gateway.close)
        self.gateway = gateway
        relay = stream.StreamRelay(gateway, clock, describe=fallbacks.request_row)
        with relay:
            result = probe.run_native(self._argv(managed), trusted=self.trusted, fixture=fixture, provider=relay,
                                      timeout=_TIMEOUT, allow_real=True, environ=self.environ,
                                      child_env=dict(models._HINT_ENV))
        main = [row for row in relay.rows if row["class"] == "main" and row["route"].endswith("/messages")]
        hooks = models.hook_rows(managed["log"])
        return {"rc": result.returncode, "timed_out": result.timed_out,
                "main_models": [row["model"] for row in main],
                "main_statuses": [row.get("status") for row in main],
                "main_parts": [parts(row["request"]) for row in main],
                "upstream_models": [hit["model"] for hit in gateway.upstream.hits if hit.get("main")],
                "upstream_lead": lead["model"],
                "hook_events": sorted({str(row.get("event")) for row in hooks}),
                "switch_hooks": [f"{row['event']}:{row.get('source')}:{row.get('to')}:{row.get('decision')}"
                                 for row in hooks if row.get("event") in ("premodel", "postmodel")],
                "available": managed["available"]}

    def _direct(self, responder: Any) -> dict[str, Any]:
        """A row straight against the fake provider: its control."""

        fixture = self._fixture()
        managed = models.compiled_managed_scope(fixture, "balanced")
        provider = models._BetaRecordingProvider(responder=responder)
        with provider:
            result = probe.run_native(self._argv(managed), trusted=self.trusted, fixture=fixture, provider=provider,
                                      timeout=_TIMEOUT, allow_real=True, environ=self.environ,
                                      child_env=dict(models._HINT_ENV))
        hooks = models.hook_rows(managed["log"])
        return {"rc": result.returncode, "timed_out": result.timed_out,
                "main_models": [row["model"] for row in responder.rows if row["request_class"] == "main"],
                "main_parts": [parts(row["request"]) for row in responder.rows
                               if row["request_class"] == "main" and "request" in row],
                "hook_events": sorted({str(row.get("event")) for row in hooks})}

    def test_the_gateway_rows_route_as_specified(self) -> None:
        try:
            observed = {name: self._row(name, behaviour) for name, (behaviour, _switches) in ROWS.items()}
            direct = self._direct(models._RoutingResponder(models._wire(_SOURCE), _CATEGORY))
            direct_400 = self._direct(_Direct400())
        except ProbeError as exc:
            if models._TRIPWIRE in str(exc):
                type(self).verdict = ("BOUNDARY", "live daemon-domain tripwire hit")
                self.skipTest("BOUNDARY: live daemon-domain tripwire hit")
            type(self).verdict = ("INCONCLUSIVE", f"probe execution unavailable: {exc}")
            raise
        except unittest.SkipTest as exc:
            type(self).verdict = ("BOUNDARY", str(exc))
            raise
        available = observed["stream-refusal"]["available"]
        problems = {}
        for name, (_behaviour, switches) in ROWS.items():
            expected = models.expected_switch(_SOURCE, _CATEGORY, available) if switches else None
            problems[name] = row_problems(name, observed[name], expected)
        expected = models.expected_switch(_SOURCE, _CATEGORY, available)
        problems["direct"] = row_problems("stream-refusal", {**direct, "main_statuses": []}, expected)
        if direct["main_models"] != observed["stream-refusal"]["main_models"]:
            problems["direct"].append(f"the gateway row sent {observed['stream-refusal']['main_models']}, "
                                      f"directly {direct['main_models']}")
        problems["direct-400"] = row_problems("refusal-400", direct_400, None)
        binary = Path(self.gateway.binary)
        identity = (f"client={self.version} sha256={self.trusted.sha256[:12]} gateway={self.gateway.version} "
                    f"sha256={hashlib.sha256(binary.read_bytes()).hexdigest()[:12]}")
        rows = " ".join(
            f"{name}: main={'/'.join(map(str, row['main_models']))} statuses={row.get('main_statuses')} "
            f"parts={'/'.join(row.get('main_parts', []))} "
            f"upstream={'/'.join(map(str, row.get('upstream_models', [])))} rc={row['rc']} "
            f"switch-hooks={row.get('switch_hooks') or 'none'} "
            f"{'MISMATCH ' + '; '.join(problems[name]) if problems[name] else 'OK'};"
            for name, row in (*observed.items(), ("direct", direct), ("direct-400", direct_400)))
        failed = sorted(name for name, found in problems.items() if found)
        type(self).verdict = ("FAIL" if failed else "PASS", f"{identity} {rows}")
        self.assertEqual(failed, [], rows)


class RowVerdictTests(unittest.TestCase):
    def test_each_row_needs_its_specified_outcome(self) -> None:
        wire = models._wire(_SOURCE)
        good = {"main_models": [wire, "claude-opus-4-8"], "rc": 0, "timed_out": False,
                "hook_events": ["end", "start"]}
        self.assertEqual(row_problems("stream-refusal", good, "claude-opus-4-8"), [])
        self.assertTrue(row_problems("stream-refusal", {**good, "main_models": [wire]}, "claude-opus-4-8"))
        self.assertTrue(row_problems("stream-refusal", {**good, "main_models": [wire, "claude-opus-5"]},
                                     "claude-opus-4-8"))
        failed = {**good, "main_models": [wire], "rc": 1}
        self.assertEqual(row_problems("refusal-400", failed, None), [])
        self.assertTrue(row_problems("refusal-400", {**failed, "rc": 0}, None))
        self.assertTrue(row_problems("refusal-400", {**failed, "main_models": [wire, "claude-opus-4-8"]}, None))
        self.assertEqual(row_problems("mid-stream-cut", {**good, "main_models": [wire, wire]}, None), [])
        self.assertTrue(row_problems("mid-stream-cut", good, None))
        self.assertTrue(row_problems("mid-stream-cut", {**good, "main_models": [wire], "timed_out": True}, None))
        self.assertTrue(row_problems("mid-stream-cut", {**good, "main_models": [wire], "hook_events": []}, None))


if __name__ == "__main__":
    unittest.main()
