"""Client checks headless-output and stream-idle: the pinned client's headless
output per turn, and its stream idle limit behind a custom base URL.

* ``headless-output``: a headless session (``-p``) of two turns: the lead
  starts a background agent and ends its first turn; the agent's completion
  notice starts a second turn. Text output prints the text of every completed
  turn, not only the last one; within a turn only the final text is printed
  (a step's text before a tool call is not). JSON output carries one result,
  the last turn's (asserted). Charmer's own parsers of headless output (the
  probe helpers, ``models qualify``) read the JSON form of one-turn sessions.
* ``stream-idle``: a streamed answer that sends ``message_start`` and then
  nothing, not even ``ping`` events. Under the compiled retry watchdog, a
  silence shorter than the client's stream idle limit (300 seconds behind a
  custom base URL) completes as one request; a longer one is abandoned at the
  limit and the request is sent again, which the fixture answers at once.
  Separate from the keepalive check, whose stream stays alive with pings.

Hermetic like every real-client check (loopback fixture provider, fixture
homes); only counts, timings and fixed marker strings are kept. The output
lines: ``client check headless-output: <STATUS> class=<class> <detail>`` and
``client check stream-idle: <STATUS> class=<class> <detail>``. The idle case
takes about ten minutes: it is not part of the essential evidence.
"""

from __future__ import annotations

import json
import threading
import time
import unittest
from typing import Any

from charmer import probe, scope

import test_client_gateway_fallbacks as fallbacks

_LEAD = fallbacks._LEAD
OUTPUT_PASS_CLASS = "HO-every-turn"
IDLE_PASS_CLASS = "SI-limit-then-retry"
# The client's stream idle limit behind a custom base URL, and the two
# silences that straddle it.
IDLE_LIMIT = 300.0
_SHORT_SILENCE, _LONG_SILENCE = 240.0, 345.0
_STEP, _TURN_ONE, _TURN_TWO = "PROBE-STEP-TEXT", "PROBE-TURN-ONE", "PROBE-TURN-TWO"
_AGENT_TASK = "PROBE-AGENT-TASK"


def _user_texts(document: dict[str, Any]) -> list[str]:
    texts = []
    for message in document.get("messages") or ():
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            texts.append(content if isinstance(content, str) else json.dumps(content))
    return texts


class _TwoTurns(probe.FakeAnthropicProvider):
    """The lead's first turn starts a background agent (with a step text
    before the call) and ends; the agent answers; the completion notice's
    turn ends the session."""

    def __init__(self) -> None:
        super().__init__(responder=self)
        self.lock = threading.Lock()
        self.spawned = False
        self.agent = 0
        self.turns: list[str] = []

    def respond(self, document, path, context):
        if path.split("?", 1)[0].endswith("/count_tokens"):
            return 200, {"input_tokens": 1}
        texts = _user_texts(document)
        offered = {tool.get("name") for tool in document.get("tools") or [] if isinstance(tool, dict)}

        def text(value: str):
            return 200, fallbacks._payload(document, [{"type": "text", "text": value}], "end_turn", context.ordinal)

        with self.lock:
            if texts and _AGENT_TASK in texts[0]:
                self.agent += 1
                return text("PROBE-AGENT-DONE")
            if not offered:
                return text("PROBE-AUX")
            if texts and "<task-notification>" in texts[-1]:
                self.turns.append("two")
                return text(_TURN_TWO)
            if texts and "toolu_ho_agent" in texts[-1]:
                self.turns.append("one")
                return text(_TURN_ONE)
            if "Agent" in offered and not self.spawned:
                self.spawned = True
                content = [{"type": "text", "text": _STEP},
                           {"type": "tool_use", "id": "toolu_ho_agent", "name": "Agent",
                            "input": {"description": "client check headless output", "prompt": _AGENT_TASK,
                                      "subagent_type": "general-purpose", "run_in_background": True}}]
                return 200, fallbacks._payload(document, content, "tool_use", context.ordinal)
        return text("PROBE-AUX")


def output_verdict(observed: dict[str, dict[str, Any]]) -> str:
    for name, row in observed.items():
        if row["rc"] != 0 or row["turns"] != ["one", "two"] or row["agent"] < 1:
            raise fallbacks._Inconclusive(f"{name}: the session did not take its two turns ({row})")
    text = observed["text"]
    if not text["two"]:
        return "HO-first-turn-only"
    if not text["one"]:
        return "HO-last-turn-only"
    if text["step"]:
        return "HO-every-step"
    # JSON output: exactly one result, the last turn's.
    results = observed["json"].get("results")
    if results != [_TURN_TWO]:
        if not results:
            return "HO-json-no-result"
        if len(results) > 1:
            return "HO-json-several-results"
        return "HO-json-other-result"
    return OUTPUT_PASS_CLASS


class _IdleHandler(probe._ProviderHandler):
    """The held answer: ``message_start``, then silence for the provider's
    ``silence`` seconds (or until the client sends the request again), then
    the content."""

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
        if provider.retried.wait(provider.silence):
            return  # the client gave up on this stream and sent the request again
        try:
            for event in events[1:]:
                chunk(*event)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except OSError:
            pass


class _IdleProvider(probe.FakeAnthropicProvider):
    def __init__(self, silence: float) -> None:
        super().__init__(responder=self)
        self.silence = silence
        self.hold_next = True
        self.hold_lock = threading.Lock()
        self.retried = threading.Event()
        self.main: list[float] = []

    def start(self):
        super().start()
        self._server.RequestHandlerClass = _IdleHandler
        return self

    def unix_socket(self, directory):
        path = super().unix_socket(directory)
        self._unix_servers[path][0].RequestHandlerClass = _IdleHandler
        return path

    def respond(self, document, path, context):
        if path.split("?", 1)[0].endswith("/count_tokens"):
            return 200, {"input_tokens": 1}
        if document.get("tools"):
            with self.hold_lock:
                self.main.append(time.monotonic())
                if len(self.main) > 1:
                    self.retried.set()
        return 200, fallbacks._payload(document, [{"type": "text", "text": "PROBE-IDLE-OK"}], "end_turn",
                                       context.ordinal)


def idle_verdict(observed: dict[str, dict[str, Any]]) -> str:
    short, long = observed["short"], observed["long"]
    for name, row in observed.items():
        if row["rc"] != 0 or row["result"] != "PROBE-IDLE-OK":
            return f"SI-{name}-failed"
    if short["elapsed"] < _SHORT_SILENCE:
        raise fallbacks._Inconclusive(f"the short silence was not exercised ({short})")
    if short["main"] != 1:
        return "SI-short-abandoned"
    if long["main"] < 2 or long["abandoned_after"] is None:
        return "SI-long-held"
    if not IDLE_LIMIT - 15 <= long["abandoned_after"] <= _LONG_SILENCE:
        return "SI-limit-moved"
    return IDLE_PASS_CLASS


class VerdictTests(unittest.TestCase):
    def test_output(self) -> None:
        both = {"rc": 0, "turns": ["one", "two"], "agent": 1}
        rows = {"text": {**both, "one": True, "two": True, "step": False}, "json": {**both, "results": [_TURN_TWO]}}
        self.assertEqual(output_verdict(rows), OUTPUT_PASS_CLASS)
        self.assertEqual(output_verdict(dict(rows, text={**rows["text"], "one": False})), "HO-last-turn-only")
        self.assertEqual(output_verdict(dict(rows, text={**rows["text"], "two": False})), "HO-first-turn-only")
        self.assertEqual(output_verdict(dict(rows, text={**rows["text"], "step": True})), "HO-every-step")
        with self.assertRaises(fallbacks._Inconclusive):
            output_verdict(dict(rows, json={**rows["json"], "turns": ["one"]}))

    def test_output_json_carries_the_last_turn_only(self) -> None:
        both = {"rc": 0, "turns": ["one", "two"], "agent": 1}
        text = {**both, "one": True, "two": True, "step": False}
        missing = {key: value for key, value in both.items()}
        for json_row, expected in (({**both, "results": []}, "HO-json-no-result"),
                                   (missing, "HO-json-no-result"),
                                   ({**both, "results": ["wrong-turn"]}, "HO-json-other-result"),
                                   ({**both, "results": [_TURN_ONE]}, "HO-json-other-result"),
                                   ({**both, "results": [_TURN_ONE, _TURN_TWO]}, "HO-json-several-results")):
            with self.subTest(results=json_row.get("results")):
                self.assertEqual(output_verdict({"text": text, "json": json_row}), expected)

    def test_idle(self) -> None:
        rows = {"short": {"rc": 0, "result": "PROBE-IDLE-OK", "main": 1, "elapsed": 241, "abandoned_after": None},
                "long": {"rc": 0, "result": "PROBE-IDLE-OK", "main": 2, "elapsed": 302, "abandoned_after": 300.4}}
        self.assertEqual(idle_verdict(rows), IDLE_PASS_CLASS)
        self.assertEqual(idle_verdict(dict(rows, short={**rows["short"], "main": 2})), "SI-short-abandoned")
        self.assertEqual(idle_verdict(dict(rows, long={**rows["long"], "main": 1, "abandoned_after": None})),
                         "SI-long-held")
        self.assertEqual(idle_verdict(dict(rows, long={**rows["long"], "abandoned_after": 180.2})), "SI-limit-moved")
        self.assertEqual(idle_verdict(dict(rows, long={**rows["long"], "rc": 1})), "SI-long-failed")
        with self.assertRaises(fallbacks._Inconclusive):
            idle_verdict(dict(rows, short={**rows["short"], "elapsed": 10}))


class HeadlessOutputProbe(fallbacks._GatedCase):
    probe_id = "headless-output"

    def test_every_completed_turn_is_printed(self) -> None:
        def run() -> dict[str, dict[str, Any]]:
            observed: dict[str, dict[str, Any]] = {}
            for output in ("text", "json"):
                fixture = self._fixture()
                provider = _TwoTurns()
                with provider:
                    result = probe.run_native(
                        ("-p", "client check headless output", "--settings",
                         str(self._settings(fixture, _LEAD, [_LEAD])), "--model", _LEAD,
                         "--output-format", output, "--dangerously-skip-permissions"),
                        trusted=self.trusted, fixture=fixture, provider=provider, timeout=150, allow_real=True,
                        environ=self.environ)
                if result.timed_out:
                    raise fallbacks._Inconclusive(f"{output}: the session timed out")
                row: dict[str, Any] = {"rc": result.returncode, "turns": list(provider.turns),
                                       "agent": provider.agent}
                stdout = result.stdout or ""
                if output == "text":
                    row.update(one=_TURN_ONE in stdout, two=_TURN_TWO in stdout, step=_STEP in stdout)
                else:
                    documents = [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]
                    row.update(results=[document.get("result") for document in documents])
                observed[output] = row
            return observed

        observed = self._guard(run)
        detail = (" ".join(f"{name}={row}" for name, row in observed.items())
                  + f" client={self.gate.version} sha256={self.trusted.sha256[:12]}")
        try:
            observed_class = output_verdict(observed)
        except fallbacks._Inconclusive as exc:
            type(self).verdict = ("INCONCLUSIVE", "unavailable", f"{exc}; {detail}")
            raise
        type(self).verdict = ("PASS" if observed_class == OUTPUT_PASS_CLASS else "FAIL", observed_class, detail)
        self.assertEqual(observed_class, OUTPUT_PASS_CLASS, detail)


class StreamIdleProbe(fallbacks._GatedCase):
    probe_id = "stream-idle"

    def test_the_idle_limit_then_one_retry(self) -> None:
        compiled_env = fallbacks.compiled_watchdog_env()

        def run() -> dict[str, dict[str, Any]]:
            observed: dict[str, dict[str, Any]] = {}
            for name, silence in (("short", _SHORT_SILENCE), ("long", _LONG_SILENCE)):
                fixture = self._fixture()
                settings = probe.write_fixture_settings(fixture, "scope/settings.json", {
                    "availableModels": [_LEAD], "model": _LEAD,
                    "permissions": {"defaultMode": scope.PERMISSION_DEFAULT_MODE},
                    "env": {**compiled_env, scope.FAST_MODE_ENV: "1"}})
                provider = _IdleProvider(silence)
                with provider:
                    started = time.monotonic()
                    result = probe.run_native(
                        ("-p", "client check stream idle", "--settings", str(settings), "--model", _LEAD,
                         "--output-format", "json"),
                        trusted=self.trusted, fixture=fixture, provider=provider, timeout=silence + 180,
                        allow_real=True, environ=self.environ)
                    elapsed = time.monotonic() - started
                if result.timed_out:
                    raise fallbacks._Inconclusive(f"{name}: the idle run timed out")
                abandoned = (round(provider.main[1] - provider.main[0], 1) if len(provider.main) > 1 else None)
                observed[name] = {"rc": result.returncode, "main": len(provider.main), "elapsed": round(elapsed),
                                  "abandoned_after": abandoned,
                                  "result": json.loads(result.stdout or "{}").get("result")}
            return observed

        observed = self._guard(run)
        detail = (" ".join(f"{name}(silence {int(_SHORT_SILENCE if name == 'short' else _LONG_SILENCE)}s)="
                           f"requests:{row['main']},abandoned-after:{row['abandoned_after']}s,"
                           f"elapsed:{row['elapsed']}s,rc:{row['rc']}" for name, row in observed.items())
                  + f" compiled={compiled_env} client={self.gate.version} sha256={self.trusted.sha256[:12]}")
        try:
            observed_class = idle_verdict(observed)
        except fallbacks._Inconclusive as exc:
            type(self).verdict = ("INCONCLUSIVE", "unavailable", f"{exc}; {detail}")
            raise
        type(self).verdict = ("PASS" if observed_class == IDLE_PASS_CLASS else "FAIL", observed_class, detail)
        self.assertEqual(observed_class, IDLE_PASS_CLASS, detail)


if __name__ == "__main__":
    unittest.main()
