"""Client check first-byte: what the pinned client does when the upstream
behind charmer's gateway holds the first byte of an answer for 11 minutes.

Opt-in only (``CHARMER_TEST_LONG_FIRST_BYTE=1``; it skips otherwise, and it
is never in the battery or CI): it takes about 20 minutes, with the candidate
gateway (``CHARMER_TEST_CLI_PROXY_API``) and the pinned client.

Every scenario runs the balanced seed's managed compile headless
(``--output-format stream-json``, so the client's own ``api_retry`` records
are kept) through ``_client_gateway_stream.StreamRelay`` (bytes passed on as
they arrive, no relay deadline) and its own gateway's Claude OAuth pool to a
scripted upstream. The upstream answers helpers at once and holds each main
request's answer 660 s from that request's own arrival, in one of four ways,
then sends the whole answer:

* ``silent``: nothing at all;
* ``headers``: 200 and the event-stream headers at once;
* ``comments``: the headers, then an SSE comment line every 20 s (bytes, no
  event);
* ``pings``: the headers, then a ``ping`` event every 20 s (parsed events).

The scenarios run at once (``CHARMER_TEST_FIRST_BYTE_SCENARIOS`` names a
subset): silent with the defaults, with ``API_TIMEOUT_MS=1800000``, with that
and ``CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS=1800000`` or ``300000``, with that
and ``CLAUDE_STREAM_IDLE_TIMEOUT_MS=1800000``, with that and the byte
watchdog off (``CLAUDE_ENABLE_BYTE_WATCHDOG=0``), and without the compiled
retry watchdog; headers, comments and pings with the defaults. The values go
into the compiled flag settings env, as a managed session would carry them.
Each run is bounded (800 s; 1200 s with the short first-byte window).

Recorded per scenario, in seconds from the client's start: each upstream
request's arrival and, when the gateway closed it first, when; what reached
the relay of each client request when, and when the client closed it; the
client's ``api_retry`` records (attempt, max_retries, retry_delay_ms,
error_status, the ``no_response`` waits) and its result. One line per
scenario: ``client check first-byte <scenario>: MEASURED ...``. Nothing is
asserted beyond each run reaching its result or its bound.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import unittest
import uuid
from pathlib import Path
from typing import Any

from charmer import probe, state
from charmer.probe import ProbeError

import _client_gateway_stream as stream
import test_client_models as models  # module import: no tests re-exported
from _catalog import uses_shipped_catalog

OPT_IN_ENV = "CHARMER_TEST_LONG_FIRST_BYTE"
# A comma-separated subset of SCENARIOS to run (all when unset).
SCENARIOS_ENV = "CHARMER_TEST_FIRST_BYTE_SCENARIOS"
HOLD_SECONDS = 660.0
TICK_SECONDS = 20.0
_THIRTY_MINUTES = "1800000"
SCENARIOS: dict[str, tuple[str, dict[str, str], float]] = {
    # name: (upstream mode, flag settings env, run bound in seconds)
    "silent-defaults": ("silent", {}, 800.0),
    "silent-api-timeout": ("silent", {"API_TIMEOUT_MS": _THIRTY_MINUTES}, 800.0),
    "silent-api-timeout-first-byte": ("silent", {"API_TIMEOUT_MS": _THIRTY_MINUTES,
                                                 "CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS": _THIRTY_MINUTES}, 800.0),
    "silent-api-timeout-short-first-byte": ("silent", {"API_TIMEOUT_MS": _THIRTY_MINUTES,
                                                       "CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS": "300000"}, 1200.0),
    # The client's own idle watchdogs, each raised or off beside the API timeout.
    "silent-api-timeout-stream-idle": ("silent", {"API_TIMEOUT_MS": _THIRTY_MINUTES,
                                                  "CLAUDE_STREAM_IDLE_TIMEOUT_MS": _THIRTY_MINUTES}, 800.0),
    "silent-api-timeout-no-byte-watchdog": ("silent", {"API_TIMEOUT_MS": _THIRTY_MINUTES,
                                                       "CLAUDE_ENABLE_BYTE_WATCHDOG": "0"}, 800.0),
    # Without the compiled retry watchdog (the client's own retry budget).
    "silent-no-retry-watchdog": ("silent", {"CLAUDE_CODE_RETRY_WATCHDOG": "0"}, 800.0),
    "headers-defaults": ("headers", {}, 800.0),
    "comments-defaults": ("comments", {}, 800.0),
    "pings-defaults": ("pings", {}, 800.0),
}


def upstream_script(mode: str):
    """The scripted upstream of one scenario."""

    def script(turn: stream.UpstreamTurn, route: str, body: dict[str, Any]) -> None:
        if route.endswith("/count_tokens"):
            turn.full(200, b'{"input_tokens": 1}', "application/json")
            return
        if not body.get("tools") or not body.get("stream"):
            stream.native_text(turn, body)
            return
        frames = turn.events(body, [{"type": "text", "text": "PROBE-FIRST-BYTE-OK"}], "end_turn")
        deadline = time.monotonic() + HOLD_SECONDS
        if mode != "silent":
            turn.headers()
        while (remaining := deadline - time.monotonic()) > 0:
            if not turn.hold(min(TICK_SECONDS, remaining)):
                return
            if deadline - time.monotonic() > 1.0:
                if mode == "comments":
                    turn.chunk(b": keepalive\n\n")
                elif mode == "pings":
                    turn.chunk(stream.harness.sse_frame("ping", {"type": "ping"}))
        if mode == "silent":
            turn.headers()
        for frame in frames:
            turn.chunk(frame)
        turn.end()

    return script


def client_records(stdout: str) -> dict[str, Any]:
    """The client's own records from its stream-json output: each
    ``api_retry`` (bounded fields) and the result."""

    retries, result = [], None
    for line in stdout.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        if record.get("type") == "system" and record.get("subtype") == "api_retry":
            error = record.get("error")
            retries.append({"attempt": record.get("attempt"), "max_retries": record.get("max_retries"),
                            "retry_delay_ms": record.get("retry_delay_ms"),
                            "error_status": record.get("error_status"),
                            "error": str(error)[:60] if error is not None else None,
                            "no_response": record.get("no_response")})
        elif record.get("type") == "result":
            result = {"subtype": record.get("subtype"), "is_error": record.get("is_error"),
                      "duration_ms": record.get("duration_ms"), "duration_api_ms": record.get("duration_api_ms")}
    return {"retries": retries, "result": result}


def timeline(relay_rows: list[dict[str, Any]], hits: list[dict[str, Any]]) -> dict[str, Any]:
    """The main requests on both sides, as bounded rows."""

    keys = ("at", "status", "headers", "first_body", "first_event", "first_comment", "events", "comments",
            "client_closed", "gateway_closed", "ended", "error")
    client = [{key: row[key] for key in keys if key in row}
              for row in relay_rows if row.get("main") and row["route"].endswith("/messages")]
    upstream = [{key: hit[key] for key in ("at", "headers", "first_chunk", "ended", "closed") if key in hit}
                for hit in hits if hit.get("main")]
    return {"client": client, "upstream": upstream}


@uses_shipped_catalog
class FirstByteProbe(models._SpikeTestCase):
    def test_a_long_first_byte_through_the_gateway(self) -> None:
        if os.environ.get(OPT_IN_ENV) != "1":
            self.skipTest(f"opt-in: set {OPT_IN_ENV}=1 (about 20 minutes)")
        chosen = [name.strip() for name in os.environ.get(SCENARIOS_ENV, "").split(",") if name.strip()]
        unknown = sorted(set(chosen) - set(SCENARIOS))
        if unknown:
            raise AssertionError(f"{SCENARIOS_ENV} names no scenario: {unknown}")
        runs: dict[str, dict[str, Any]] = {}
        for name, (mode, env, bound) in SCENARIOS.items():
            if chosen and name not in chosen:
                continue
            clock = stream.Clock()
            fixture = self._fixture()
            managed = models.compiled_managed_scope(fixture, "balanced", flag_env=env)
            gateway = stream.start_gateway(state.ensure_private_dir(self.root / f"gateway-{name}"),
                                           upstream_script(mode), clock)
            self.addCleanup(gateway.close)
            relay = stream.StreamRelay(gateway, clock).start()
            self.addCleanup(relay.stop)
            argv = ("-p", "client check first-byte", "--session-id", str(uuid.uuid4()), "--output-format",
                    "stream-json", "--verbose", "--settings", str(managed["settings"]), "--model",
                    managed["lead"], "--effort", managed["effort"], "--add-dir", str(managed["live"]))
            runs[name] = {"clock": clock, "fixture": fixture, "gateway": gateway, "relay": relay,
                          "argv": argv, "bound": bound}
        results: dict[str, Any] = {}

        def run(name: str) -> None:
            spec = runs[name]
            spec["clock"].start = time.monotonic()
            try:
                results[name] = probe.run_native(spec["argv"], trusted=self.trusted, fixture=spec["fixture"],
                                                 provider=spec["relay"], timeout=spec["bound"], allow_real=True,
                                                 environ=self.environ, child_env=dict(models._HINT_ENV))
            except ProbeError as exc:
                results[name] = exc

        workers = [threading.Thread(target=run, args=(name,), daemon=True) for name in runs]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(max(spec["bound"] for spec in runs.values()) + 120)
        gateway = next(iter(runs.values()))["gateway"]
        identity = (f"client={self.version} sha256={self.trusted.sha256[:12]} gateway={gateway.version} "
                    f"sha256={hashlib.sha256(Path(gateway.binary).read_bytes()).hexdigest()[:12]}")
        for name, spec in runs.items():
            result = results.get(name)
            if isinstance(result, ProbeError) or result is None:
                print(f"client check first-byte {name}: INCONCLUSIVE {identity} {result!r}", flush=True)
                continue
            records = client_records(result.stdout)
            rows = timeline(spec["relay"].rows, spec["gateway"].upstream.hits)
            print(f"client check first-byte {name}: MEASURED {identity} env={SCENARIOS[name][1] or 'defaults'} "
                  f"rc={result.returncode} timed_out={result.timed_out} bound={spec['bound']:.0f}s "
                  f"result={json.dumps(records['result'])} retries={json.dumps(records['retries'])} "
                  f"client-requests={json.dumps(rows['client'])} upstream-requests={json.dumps(rows['upstream'])}",
                  flush=True)


class ScenarioTests(unittest.TestCase):
    def test_the_client_records_keep_bounded_fields(self) -> None:
        stdout = "\n".join([
            json.dumps({"type": "system", "subtype": "api_retry", "attempt": 1, "max_retries": 1,
                        "retry_delay_ms": 500, "error_status": None, "error": "server_error",
                        "no_response": {"waited_ms": 300000, "retry_wait_ms": 1799000}, "uuid": "u",
                        "session_id": "s"}),
            "not json",
            json.dumps({"type": "result", "subtype": "success", "is_error": False, "duration_ms": 5,
                        "duration_api_ms": 4, "result": "PROBE-FIRST-BYTE-OK"})])
        records = client_records(stdout)
        self.assertEqual(records["retries"], [{"attempt": 1, "max_retries": 1, "retry_delay_ms": 500,
                                               "error_status": None, "error": "server_error",
                                               "no_response": {"waited_ms": 300000, "retry_wait_ms": 1799000}}])
        self.assertEqual(records["result"], {"subtype": "success", "is_error": False, "duration_ms": 5,
                                             "duration_api_ms": 4})

    def test_every_scenario_is_bounded_past_the_hold(self) -> None:
        for name, (mode, env, bound) in SCENARIOS.items():
            with self.subTest(name):
                self.assertIn(mode, ("silent", "headers", "comments", "pings"))
                self.assertGreater(bound, HOLD_SECONDS)
                self.assertTrue(set(env) <= {"API_TIMEOUT_MS", "CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS",
                                             "CLAUDE_STREAM_IDLE_TIMEOUT_MS", "CLAUDE_ENABLE_BYTE_WATCHDOG",
                                             "CLAUDE_CODE_RETRY_WATCHDOG"})


if __name__ == "__main__":
    unittest.main()
