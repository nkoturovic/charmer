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
        turn.hit["sent"] = "refusal-event"
        turn.headers()
        for kind, data in reply.events:
            turn.chunk(stream.harness.sse_frame(kind, data))
        turn.end()
    else:
        turn.hit["sent"] = "refusal-json"
        turn.full(200, json.dumps(reply).encode(), "application/json")


def _refusal_400(turn: stream.UpstreamTurn, _body: dict[str, Any], _nth: int) -> None:
    turn.hit["sent"] = "status-400"
    turn.full(400, json.dumps(_REFUSAL_400).encode(), "application/json")


def _cut(turn: stream.UpstreamTurn, body: dict[str, Any], nth: int) -> None:
    if nth > 1 or not body.get("stream"):
        stream.native_text(turn, body)
        return
    turn.headers()
    for frame in turn.events(body, [{"type": "text", "text": "PROBE-PARTIAL"}], "end_turn"):
        turn.chunk(frame)
        if frame.startswith(b"event: content_block_delta"):
            turn.hit["sent"] = "cut-after-delta"
            break
    turn.cut()


ROWS: dict[str, tuple[Behaviour, bool]] = {
    # name: (upstream behaviour on the lead's main requests, a switch is specified)
    "stream-refusal": (_refusal, True),
    "refusal-400": (_refusal_400, False),
    "mid-stream-cut": (_cut, False),
}
# Each row's stimulus, stated before the run: what the upstream sends on the
# lead's model ("sent", recorded by the behaviour as it sends it) and the
# status the client's request on that model must then get through the gateway.
# Its evidence is required: a row without it is never a pass.
STIMULI: dict[str, dict[str, Any]] = {
    "stream-refusal": {"sent": "refusal-event", "status": 200},
    "refusal-400": {"sent": "status-400", "status": 400},
    "mid-stream-cut": {"sent": "cut-after-delta", "status": 200},
}


def parts(request: dict[str, Any]) -> str:
    """The request parts the gateway-fallbacks lane follows, as one token:
    effort, context management, tool search, system turns."""

    return "+".join(name for name, present in (
        ("effort", bool(request.get("effort"))), ("ctx", bool(request.get("context_management"))),
        ("search", "ToolSearch" in request.get("tools", ())), ("sys", bool(request.get("system_turns"))))
        if present) or "none"


def stimulus_problems(name: str, observed: dict[str, Any]) -> list[str]:
    """What shows the row's stimulus did not happen as stated
    (:data:`STIMULI`), from the recorded facts, never from the script's
    intent alone: an upstream request on the source's model; every one of
    its replies with the stated status, and one of them sending the
    stimulus (``sent`` supplements the status); a client request on that
    model for each, every one answered with the stated status, its body's
    framing well formed and complete. The refusal's stop reason reaches the
    client; the cut row's first upstream reply was recorded cut after a
    text delta and the client's first stream stopped short after a text
    delta (its connection ending mid-chunk, or a complete framing without
    ``message_stop``). A framing error is never that cut. A refused 400 may
    be resent on the same model any number of times, each refused."""

    spec, wire = STIMULI[name], models._wire(_SOURCE)
    upstream = [hit for hit in observed["upstream_lead"] if hit.get("model") == wire]
    if not upstream:
        return [f"no upstream request on {wire}"]
    problems = []
    statuses = [hit.get("status") for hit in upstream]
    if any(status != spec["status"] for status in statuses):
        problems.append(f"the upstream answered the requests on {wire} with {statuses}, not {spec['status']} each")
    if not any(hit.get("sent") == spec["sent"] for hit in upstream):
        problems.append(f"the upstream never sent {spec['sent']} (sent {[hit.get('sent') for hit in upstream]})")
    if name == "refusal-400" and any(hit.get("sent") != spec["sent"] for hit in upstream):
        problems.append("a request on the source's model was answered other than with the 400")
    client = [row for row in observed["main_rows"] if row.get("model") == wire]
    if not client:
        return [*problems, f"no client request on {wire}"]
    if len(client) != len(upstream):
        problems.append(f"{len(client)} client requests on {wire}, {len(upstream)} upstream")
    client_statuses = [row.get("status") for row in client]
    if any(status != spec["status"] for status in client_statuses):
        problems.append(f"the client's requests on {wire} got {client_statuses}, not {spec['status']} each")
    for index, row in enumerate(client, start=1):
        framing = row.get("framing")
        if framing == "error":
            problems.append(f"client request {index} on {wire}: malformed framing ({row.get('framing_error')})")
        elif framing != "complete" and not (name == "mid-stream-cut" and index == 1):
            problems.append(f"client request {index} on {wire} ended {framing}, not complete")
    first = client[0]
    if name == "stream-refusal" and "refusal" not in first.get("stop_reasons", ()):
        problems.append("the refusal's stop reason never reached the client")
    if name == "mid-stream-cut":
        if upstream[0].get("sent") != spec["sent"] or "cut" not in upstream[0]:
            problems.append("the upstream did not cut its first reply on the source's model")
        types, framing = list(first.get("event_types") or ()), str(first.get("framing"))
        stopped_short = framing.startswith("truncated-") or (framing == "complete" and "message_stop" not in types)
        if "content_block_delta" not in types or "message_stop" in types or not stopped_short:
            problems.append(f"the client's first stream was not cut after a text delta "
                            f"(framing {framing}, events {types})")
    return problems


def first_on_source(observed: dict[str, Any]) -> str:
    """The client's first request on the source's model as the relay saw it:
    status, last event type, stop reasons, framing."""

    rows = [row for row in observed.get("main_rows", ()) if row.get("model") == models._wire(_SOURCE)]
    if not rows:
        return "-"
    first = rows[0]
    types = first.get("event_types") or ["-"]
    return (f"{first.get('status')}:{types[-1]}:{','.join(first.get('stop_reasons') or ['-'])}"
            f":{first.get('framing')}")


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
            self.rows[-1]["sent"] = "status-400"
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
        lead_hits = [hit for hit in gateway.upstream.hits if hit.get("main") and hit.get("model") == lead["model"]]
        return {"rc": result.returncode, "timed_out": result.timed_out,
                "main_models": [row["model"] for row in main],
                "main_statuses": [row.get("status") for row in main],
                "main_parts": [parts(row["request"]) for row in main],
                "main_rows": [{key: row.get(key) for key in ("model", "status", "event_types", "stop_reasons",
                                                             "framing", "framing_error")}
                              for row in main],
                "upstream_models": [hit["model"] for hit in gateway.upstream.hits if hit.get("main")],
                "upstream_lead": [{key: hit.get(key) for key in ("model", "sent", "status", "cut")
                                   if key in hit} for hit in lead_hits],
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
                "sent": [row["sent"] for row in responder.rows if "sent" in row],
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
            problems[name] = stimulus_problems(name, observed[name]) + row_problems(name, observed[name], expected)
        expected = models.expected_switch(_SOURCE, _CATEGORY, available)
        problems["direct"] = row_problems("stream-refusal", {**direct, "main_statuses": []}, expected)
        if direct["main_models"] != observed["stream-refusal"]["main_models"]:
            problems["direct"].append(f"the gateway row sent {observed['stream-refusal']['main_models']}, "
                                      f"directly {direct['main_models']}")
        problems["direct-400"] = row_problems("refusal-400", direct_400, None)
        if "status-400" not in direct_400["sent"]:
            problems["direct-400"].append("the control never answered the 400")
        binary = Path(self.gateway.binary)
        identity = (f"client={self.version} sha256={self.trusted.sha256[:12]} gateway={self.gateway.version} "
                    f"sha256={hashlib.sha256(binary.read_bytes()).hexdigest()[:12]}")
        rows = " ".join(
            f"{name}: main={'/'.join(map(str, row['main_models']))} statuses={row.get('main_statuses')} "
            f"parts={'/'.join(row.get('main_parts', []))} "
            f"upstream={'/'.join(map(str, row.get('upstream_models', [])))} "
            f"sent={'/'.join(str(hit.get('sent')) for hit in row.get('upstream_lead', []))} "
            f"client-first={first_on_source(row)} rc={row['rc']} "
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

    def test_a_row_needs_its_stimulus_to_have_happened(self) -> None:
        wire = models._wire(_SOURCE)
        evidence = _evidence()
        for name, observed in evidence.items():
            with self.subTest(name):
                self.assertEqual(stimulus_problems(name, observed), [])
                # No upstream hits (a client stopped before the gateway, e.g. a 401).
                self.assertTrue(stimulus_problems(name, {**observed, "upstream_lead": []}))
                # A wrong response status on the client's first request.
                wrong = [{**observed["main_rows"][0], "status": 401}, *observed["main_rows"][1:]]
                self.assertTrue(stimulus_problems(name, {**observed, "main_rows": wrong}))
                # Upstream requests on another model only.
                other = [{**hit, "model": "claude-opus-5"} for hit in observed["upstream_lead"]]
                self.assertTrue(stimulus_problems(name, {**observed, "upstream_lead": other}))
        # An uncut, complete answer is no cut.
        uncut = {"upstream_lead": [{"model": wire, "sent": "answer", "status": 200}],
                 "main_rows": [{"model": wire, "status": 200, "stop_reasons": ["end_turn"], "framing": "complete",
                                "event_types": ["message_start", "content_block_delta", "message_delta",
                                                "message_stop"]}]}
        self.assertTrue(stimulus_problems("mid-stream-cut", uncut))
        self.assertTrue(stimulus_problems("mid-stream-cut", {**evidence["mid-stream-cut"],
                                                             "main_rows": [*uncut["main_rows"], _COMPLETE]}))
        stated = {**evidence["mid-stream-cut"], "upstream_lead": [
            {"model": wire, "sent": "cut-after-delta", "status": 200}, {"model": wire, "sent": "answer",
                                                                         "status": 200}]}
        self.assertTrue(stimulus_problems("mid-stream-cut", stated))  # stated, never cut
        # A refusal that never reached the client, a 400 row with one answer.
        refused = evidence["stream-refusal"]
        self.assertTrue(stimulus_problems("stream-refusal", {**refused, "main_rows": [
            {**refused["main_rows"][0], "stop_reasons": ["end_turn"]}, *refused["main_rows"][1:]]}))
        self.assertTrue(stimulus_problems("refusal-400", {**evidence["refusal-400"], "upstream_lead": [
            {"model": wire, "sent": "status-400", "status": 400}, {"model": wire, "sent": "answer", "status": 200},
            *[{"model": wire, "sent": "status-400", "status": 400}] * 4]}))

    def test_every_source_reply_needs_the_stated_status(self) -> None:
        """A 400 first, then 422 on the later requests on the source's model,
        is no 400 row: every recorded upstream status and every client status
        counts, whatever ``sent`` says, and however many resends."""

        wire = models._wire(_SOURCE)
        good = _evidence()["refusal-400"]
        later_422 = [{"model": wire, "sent": "status-400", "status": 400},
                     *[{"model": wire, "sent": "status-400", "status": 422}] * 5]
        client_422 = [{**good["main_rows"][0]}, *[{**row, "status": 422} for row in good["main_rows"][1:]]]
        for label, observed in (
                ("upstream and client 400 then 422", {"upstream_lead": later_422, "main_rows": client_422}),
                ("client 400 then 422", {**good, "main_rows": client_422}),
                ("upstream 400 then 422", {**good, "upstream_lead": later_422})):
            with self.subTest(label):
                self.assertTrue(stimulus_problems("refusal-400", observed))
        for count in (1, 2, 9):
            with self.subTest(resends=count):
                self.assertEqual(stimulus_problems("refusal-400", {"upstream_lead": good["upstream_lead"][:1] * count,
                                                                   "main_rows": good["main_rows"][:1] * count}), [])
        self.assertTrue(stimulus_problems("refusal-400", {**good, "main_rows": good["main_rows"][:5]}))

    def test_a_malformed_or_unexpected_framing_is_never_a_pass(self) -> None:
        evidence = _evidence()
        for name, observed in evidence.items():
            for index in range(len(observed["main_rows"])):
                if observed["main_rows"][index].get("model") != models._wire(_SOURCE):
                    continue
                broken = [dict(row) for row in observed["main_rows"]]
                broken[index].update(framing="error", framing_error="a malformed chunk-size line")
                with self.subTest(name, request=index + 1, framing="error"):
                    self.assertTrue(stimulus_problems(name, {**observed, "main_rows": broken}))
                if name == "mid-stream-cut" and index == 0:
                    continue
                short = [dict(row) for row in observed["main_rows"]]
                short[index]["framing"] = "truncated-mid-chunk"
                with self.subTest(name, request=index + 1, framing="truncated"):
                    self.assertTrue(stimulus_problems(name, {**observed, "main_rows": short}))
        cut = evidence["mid-stream-cut"]
        for framing in ("truncated-mid-chunk", "truncated-between-chunks", "complete"):
            first = {**cut["main_rows"][0], "framing": framing}
            with self.subTest("the intended cut", framing=framing):
                self.assertEqual(stimulus_problems("mid-stream-cut",
                                                   {**cut, "main_rows": [first, *cut["main_rows"][1:]]}), [])


_COMPLETE = {"model": models._wire(_SOURCE), "status": 200, "framing": "complete", "stop_reasons": ["end_turn"],
             "event_types": ["message_start", "content_block_delta", "message_delta", "message_stop"]}


def _evidence() -> dict[str, dict[str, Any]]:
    """Each row's recorded facts as a passing run shows them."""

    wire = models._wire(_SOURCE)
    refused = {"model": wire, "status": 200, "stop_reasons": ["refusal"], "framing": "complete",
               "event_types": ["message_start", "message_delta", "message_stop"]}
    return {
        "stream-refusal": {"upstream_lead": [{"model": wire, "sent": "refusal-event", "status": 200}] * 2,
                           "main_rows": [refused, refused, {**_COMPLETE, "model": "claude-opus-4-8"}]},
        "refusal-400": {"upstream_lead": [{"model": wire, "sent": "status-400", "status": 400}] * 6,
                        "main_rows": [{"model": wire, "status": 400, "framing": "complete", "event_types": [],
                                       "stop_reasons": []}] * 6},
        "mid-stream-cut": {"upstream_lead": [{"model": wire, "sent": "cut-after-delta", "status": 200, "cut": 1.0},
                                             {"model": wire, "sent": "answer", "status": 200}],
                           "main_rows": [{"model": wire, "status": 200, "stop_reasons": [], "framing": "complete",
                                          "event_types": ["message_start", "content_block_start",
                                                          "content_block_delta", "error"]},
                                         _COMPLETE]},
    }


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def now(self) -> float:
        return self.value


class RelayObservationTests(unittest.TestCase):
    """The relay's body observations decode the transfer framing."""

    HEAD = b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n"

    def test_a_separately_delivered_chunk_size_line_is_no_body_byte(self) -> None:
        clock, row = _Clock(), {}
        observer = stream.BodyObserver(row, clock)
        observer.feed(self.HEAD + b"c\r\n")
        self.assertEqual((row.get("status"), row.get("headers"), row.get("first_body")), (200, 0.0, None))
        clock.value = 20.0
        observer.feed(b": keepalive\n\r\n")
        clock.value = 21.0
        observer.feed(b"0\r\n\r\n")
        observer.finish()
        self.assertEqual((row["first_body"], row["first_comment"], row["bytes"], row["comments"]), (20.0, 20.0, 12, 1))
        self.assertNotIn("first_event", row)

    def test_an_sse_marker_split_across_chunks_counts_when_whole(self) -> None:
        clock, row = _Clock(), {}
        observer = stream.BodyObserver(row, clock)
        first, second = b"event: message_delta\nda", b'ta: {"stop_reason": "refusal"}\n\n'
        observer.feed(self.HEAD + f"{len(first):x}\r\n".encode() + first[:5])
        clock.value = 1.0
        observer.feed(first[5:] + b"\r\n")
        self.assertEqual((row["first_body"], row.get("first_event")), (0.0, None))
        clock.value = 2.0
        observer.feed(f"{len(second):x}\r\n".encode() + second + b"\r\n0\r\n\r\n")
        observer.finish()
        self.assertEqual(row["first_event"], 2.0)
        self.assertEqual((row["events"], row["event_types"], row["stop_reasons"]), (1, ["message_delta"], ["refusal"]))
        self.assertEqual(row["bytes"], len(first) + len(second))

    def test_an_unchunked_body_is_its_own_payload(self) -> None:
        clock, row = _Clock(), {}
        observer = stream.BodyObserver(row, clock)
        observer.feed(b"HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n")
        observer.feed(b"{}")
        observer.finish()
        self.assertEqual((row["status"], row["bytes"], row["events"], row["stop_reasons"]), (400, 2, 0, []))
        self.assertEqual(row["framing"], "complete")

    def _observe(self, *pieces: bytes) -> dict[str, Any]:
        clock, row = _Clock(), {}
        observer = stream.BodyObserver(row, clock)
        for piece in pieces:
            observer.feed(piece)
            clock.value += 1.0
        observer.finish()
        return row

    @staticmethod
    def _chunk(data: bytes) -> bytes:
        return f"{len(data):x}\r\n".encode() + data + b"\r\n"

    _PREFIX = (b"event: message_start\ndata: {}\n\n", b"event: content_block_start\ndata: {}\n\n",
               b'event: content_block_delta\ndata: {"delta": {"text": "PROBE"}}\n\n')

    def _cut_row(self, row: dict[str, Any]) -> dict[str, Any]:
        """The cut row's evidence with ``row`` as the client's first stream."""

        cut = _evidence()["mid-stream-cut"]
        first = {"model": models._wire(_SOURCE), **{key: row.get(key) for key in (
            "status", "event_types", "stop_reasons", "framing", "framing_error")}}
        return {**cut, "main_rows": [first, *cut["main_rows"][1:]]}

    def test_a_malformed_chunk_size_line_is_a_framing_error(self) -> None:
        """A valid prefix through a text delta, then a size line that is not
        hex: a framing error, never the cut."""

        row = self._observe(self.HEAD, *map(self._chunk, self._PREFIX), b"not-hex\r\nmore\r\n0\r\n\r\n")
        self.assertEqual((row["framing"], row["framing_error"]), ("error", "a malformed chunk-size line"))
        self.assertIn("content_block_delta", row["event_types"])
        self.assertTrue(stimulus_problems("mid-stream-cut", self._cut_row(row)))
        for line in (b"0x5", b"5 5", b"-1", b"", b"5\r", b"1" * 17):
            with self.subTest(line=line):
                self.assertEqual(self._observe(self.HEAD, line + b"\r\nhello\r\n0\r\n\r\n")["framing"], "error")
        self.assertEqual(self._observe(self.HEAD, b"5;name=value\r\nhello\r\n0\r\n\r\n")["framing"], "complete")

    def test_an_invalid_chunk_terminator_is_a_framing_error(self) -> None:
        row = self._observe(self.HEAD, b"1\r\nxZZ0\r\n\r\n")
        self.assertEqual((row["framing"], row["framing_error"]), ("error", "a chunk does not end with CRLF"))
        self.assertEqual(self._observe(self.HEAD, b"1\r\nx\n0\r\n\r\n")["framing"], "error")
        self.assertEqual(self._observe(self.HEAD, b"1\r\nx\r\n0\r\n\r\nextra")["framing"], "error")
        self.assertEqual(self._observe(self.HEAD, b"1\r\nx\r\n0\r\nbad trailer\r\n\r\n")["framing"], "error")
        self.assertEqual(self._observe(self.HEAD, b"1\r\nx\r\n0\r\nX-Trailer: 1\r\n\r\n")["framing"],
                         "complete")

    def test_truncated_framing_is_reported_and_only_the_intended_cut_passes(self) -> None:
        prefix = b"".join(map(self._chunk, self._PREFIX))
        for tail, framing in ((b"", "truncated-between-chunks"), (b"40\r\nevent: message_d", "truncated-mid-chunk"),
                              (b"4", "truncated-mid-size-line"), (self._chunk(b"x")[:-2], "truncated-before-chunk-end"),
                              (b"0\r\n", "truncated-in-trailer")):
            with self.subTest(framing):
                row = self._observe(self.HEAD, prefix + tail)
                self.assertEqual(row["framing"], framing)
                # The intended cut (the upstream recorded it) passes on a stream
                # that stopped short; an unexpected one fails any other row.
                self.assertEqual(stimulus_problems("mid-stream-cut", self._cut_row(row)), [])
                refused = _evidence()["stream-refusal"]
                short = {**refused, "main_rows": [{**refused["main_rows"][0], "framing": row["framing"]},
                                                  *refused["main_rows"][1:]]}
                self.assertTrue(stimulus_problems("stream-refusal", short))
                without_cut = self._cut_row(row)
                without_cut["upstream_lead"] = [{key: value for key, value in hit.items() if key != "cut"}
                                                for hit in without_cut["upstream_lead"]]
                self.assertTrue(stimulus_problems("mid-stream-cut", without_cut))
        whole = self._observe(self.HEAD, prefix + b"0\r\n\r\n")
        self.assertEqual(whole["framing"], "complete")

    def test_a_content_length_body_must_be_whole(self) -> None:
        head = b"HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\nContent-Length: 4\r\n\r\n"
        self.assertEqual(self._observe(head, b"{}")["framing"], "truncated-mid-body")
        self.assertEqual(self._observe(head, b"{  }")["framing"], "complete")
        self.assertEqual(self._observe(head, b"{    }")["framing"], "error")
        self.assertEqual(self._observe(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n")["framing"], "no-response")


if __name__ == "__main__":
    unittest.main()
