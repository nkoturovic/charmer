"""The actual pinned Claude Code on the OpenAI routes, through the built
gateway and a fake Responses upstream (no provider request).

Host-only: it needs the built gateway (CHARMER_TEST_CLI_PROXY_API) and the
pinned client (``tools/test.py --claude``). The client and the gateway each
run in a loopback-only namespace, joined by the hint capture's relay, which
also steers the fake upstream per client request from the client's own
request-class hints. Bodies and headers stay in memory; only bounded facts
are printed.

- Output cap (patch 26): every request the client sends on the API-key route
  in a short journey (an interactive model switch and back, turns, a helper,
  token counting, a non-streaming fallback, compaction) reaches OpenAI with
  ``max_output_tokens`` equal to the client's own ``max_tokens``, or 16 for a
  cap below OpenAI's minimum. The fake upstream refuses less than 16 as
  OpenAI does and ends an answer at a limit of 16; the model switch, which
  validates each model with ``max_tokens: 1``, succeeds both ways. On the
  ChatGPT account route no limit is added.
- Context overflow (patch 27): when the upstream reports that the input
  exceeds the model's context window, before the response headers (HTTP 400)
  or after them (a failed stream), the client compacts its conversation and
  the retried turn succeeds, on the API key and on the ChatGPT account (the
  key route's rows discriminate the patch). Seed turns stay far below the
  client's own compaction threshold, so the recovery is reactive. An error
  the gateway does not identify as an overflow is not compacted (the
  unnormalised negative).
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from charmer import probe
from _catalog import SHIPPED_ROOT, uses_shipped_catalog
from _tier import real_binary_gate
import _gateway_harness as harness
import test_openai_key_route as route
from test_gateway_hint_client import _GatewayRelay

MODULE = "tests.test_openai_key_route_client"
PREFIX = "x-claude-code-"
CONTEXT_MESSAGE = "Your input exceeds the context window of this model. Please adjust your input and try again."
NATIVE_OVERFLOW = {"type": "error", "error": {"type": "invalid_request_error", "message": "prompt is too long"}}
# OpenAI's Responses API refuses max_output_tokens below 16; a request under
# a limit this small ends at it (incomplete) rather than completing.
MIN_OUTPUT_TOKENS = 16
SMALL_CAP = 16
FILLER = " ".join(f"seed-filler-{index:05d}" for index in range(700))
SEEDS = 2


def facts(case, **values):
    print("openai route client: " + json.dumps({"case": case, **values}, sort_keys=True))


def responses_reply(model, text, *, input_tokens=900, cap=None):
    """A Responses stream (the gateway asks for one either way): completed,
    or, under a small output limit, ended at that limit as OpenAI ends it
    (``response.incomplete``, reason ``max_output_tokens``)."""

    incomplete = cap is not None and cap <= SMALL_CAP
    output_tokens = cap if incomplete else 6
    item = {"id": "msg_kc", "type": "message", "role": "assistant",
            "status": "incomplete" if incomplete else "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}]}
    response = {"id": "resp_kc_" + uuid.uuid4().hex[:8], "object": "response", "model": model,
                "status": "incomplete" if incomplete else "completed", "output": [item],
                "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens,
                          "total_tokens": input_tokens + output_tokens}}
    if incomplete:
        response["incomplete_details"] = {"reason": "max_output_tokens"}
    terminal = "response.incomplete" if incomplete else "response.completed"
    frames = [("response.created", {"response": {**response, "status": "in_progress", "output": []}}),
              ("response.output_item.done", {"output_index": 0, "item": item}),
              (terminal, {"response": response})]
    return 200, "text/event-stream", b"".join(harness.sse_frame(kind, {"type": kind, **data}) for kind, data in frames)


def below_minimum(cap):
    """OpenAI's refusal of an output limit below its minimum of 16."""

    return 400, "application/json", json.dumps({"error": {
        "message": f"Invalid 'max_output_tokens': integer below minimum value. Expected a value >= 16, "
                   f"but got {cap} instead.",
        "type": "invalid_request_error", "param": "max_output_tokens", "code": "integer_below_min_value"}}).encode()


def failed_stream(model, error):
    """A stream that starts, then fails with ``error`` (after the headers)."""

    started = {"id": "resp_kc_failed", "object": "response", "model": model, "status": "in_progress", "output": []}
    frames = [("response.created", {"response": started}),
              ("response.failed", {"response": {**started, "status": "failed", "error": error}})]
    return 200, "text/event-stream", b"".join(harness.sse_frame(kind, {"type": kind, **data}) for kind, data in frames)


def overflow_before_headers(_model):
    return 400, "application/json", json.dumps({"error": {
        "message": CONTEXT_MESSAGE, "type": "invalid_request_error", "param": "input",
        "code": "context_length_exceeded"}}).encode()


def overflow_after_headers(model):
    return failed_stream(model, {"code": "context_length_exceeded", "message": CONTEXT_MESSAGE})


def unidentified_context_error(_model):
    # Loose context wording with no code: never presented as an overflow.
    return 400, "application/json", json.dumps({"error": {
        "message": "context length exceeded for this request", "type": "invalid_request_error"}}).encode()


class Scenario:
    """The fake Responses upstream, steered per client request.

    ``plan`` runs in the relay (under its lock) with the client's request and
    sets the action the upstream's next answers take. Main turns answer
    normally for ``seeds`` turns; then, with ``overflow`` set, every main
    request is refused with it until a compaction request arrives (it gets a
    summary), and later main turns answer PROBE-RECOVERED. ``fail_next_stream``
    fails the next streaming main request after its headers with an ordinary
    request error, so the client retries it without streaming."""

    def __init__(self, *, overflow=None, seeds=SEEDS):
        self.overflow, self.seeds = overflow, seeds
        self.action = "aux"
        self.main = 0
        self.armed = self.disarmed = False
        self.rejections = self.summaries = self.recovered = 0
        self.fail_next_stream = False
        self.fallbacks = 0

    def plan(self, path, headers, document):
        if path.split("?", 1)[0].endswith("/count_tokens"):
            self.action = "aux"
            return "count-tokens"
        kind = headers.get(PREFIX + "request-class")
        stream = bool(document.get("stream"))
        if kind == "compaction":
            self.summaries += 1
            if self.armed:
                self.armed, self.disarmed = False, True
            self.action = "summary"
            return "compaction"
        if kind == "main" or (kind is None and document.get("tools")):
            if stream and self.fail_next_stream:
                self.fail_next_stream = False
                self.fallbacks += 1
                self.action = "fail-stream"
                return "main"
            if stream or not self.armed:
                self.main += 1
            if self.overflow and not self.armed and not self.disarmed and self.main > self.seeds:
                self.armed = True
            if self.armed:
                self.rejections += 1
                self.action = "overflow"
            elif self.disarmed:
                self.recovered += 1
                self.action = "recovered"
            else:
                self.action = "seed"
            return "main"
        self.action = "aux"
        return kind or "unclassified"

    def respond(self, path, body, raw, headers):
        if not path.endswith("/responses"):
            return 404, "application/json", b'{"error":"unexpected fake upstream path"}'
        model = body.get("model", "gwtest-wire")
        cap = body.get("max_output_tokens")
        if type(cap) is int and cap < MIN_OUTPUT_TOKENS:
            return below_minimum(cap)
        cap = cap if type(cap) is int else None
        action = self.action
        if action == "overflow":
            return self.overflow(model)
        if action == "fail-stream":
            return failed_stream(model, {"type": "invalid_request_error", "code": "invalid_prompt",
                                         "message": "The fixture stream failed."})
        if action == "summary":
            return responses_reply(model, "<summary>PROBE-SUMMARY of the seed turns</summary>", cap=cap)
        if action == "recovered":
            return responses_reply(model, "PROBE-RECOVERED", cap=cap)
        if action == "seed":
            return responses_reply(model, f"PROBE-OK-{self.main}" + FILLER, cap=cap)
        return responses_reply(model, "PROBE-AUX", cap=cap)

    def summary(self):
        return {"main": self.main, "rejections": self.rejections, "summaries": self.summaries,
                "recovered": self.recovered, "fallbacks": self.fallbacks, "armed_at_end": self.armed}


def sse_events(raw):
    events = []
    for frame in raw.decode(errors="replace").split("\n\n"):
        kind = next((line[7:] for line in frame.splitlines() if line.startswith("event: ")), None)
        data = next((line[6:] for line in frame.splitlines() if line.startswith("data: ")), None)
        if kind and data:
            events.append((kind, json.loads(data)))
    return events


class ClientRelay(_GatewayRelay):
    """The hint capture's relay, letting the scenario plan each client
    request before it is forwarded, and keeping bounded facts per request:
    its class and mode, the client's ``max_tokens``, the ``max_output_tokens``
    of each upstream request it caused, the status and any error body."""

    def __init__(self, gateway, scenario):
        super().__init__(gateway)
        self.scenario = scenario

    def _respond(self, document, path, context=None):
        with self.lock:
            raw, headers = self.captured.pop(context.ordinal)
            before = len(self.gateway.upstream.hits)
            kind = self.scenario.plan(path, headers, document)
            action = self.scenario.action
            forwarded = {**headers, "host": f"127.0.0.1:{self.gateway.port}",
                         "authorization": "Bearer " + harness.FIXTURE_GATEWAY_TOKEN}
            forwarded.pop("x-api-key", None)
            reply = harness.unix_request(self.gateway.socket, "POST", path, headers=forwarded, body=raw, timeout=45)
            hits = self.gateway.upstream.hits[before:]
            streamed = "text/event-stream" in reply.headers.get("content-type", "")
            events = sse_events(reply.raw) if streamed else []
            errors = [data for kind_, data in events if kind_ == "error"]
            if not streamed and reply.status >= 400:
                errors.append(reply.json())
            max_tokens = document.get("max_tokens")
            self.rows.append({
                "run": self.run_name, "route": path.split("?", 1)[0], "class": kind, "action": action,
                "compaction": headers.get(PREFIX + "compaction"), "stream": bool(document.get("stream")),
                "model": document.get("model"), "max_tokens": max_tokens if type(max_tokens) is int else None,
                "upstream": [{"path": hit.path, "max_output_tokens": hit.body.get("max_output_tokens"),
                              "limits": sorted({"max_tokens", "max_completion_tokens"} & set(hit.body))}
                             for hit in hits],
                "status": reply.status, "errors": errors})
            if streamed:
                return reply.status, probe.SseResponse(tuple(events))
            return reply.status, reply.json()


def result_document(stdout):
    for line in reversed(stdout.strip().splitlines()):
        try:
            document = json.loads(line)
        except ValueError:
            continue
        if isinstance(document, dict) and document.get("type") == "result":
            return document
    return {}


@uses_shipped_catalog
class _PinnedClientCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gate = real_binary_gate(SHIPPED_ROOT / "catalog/native-contract.json",
                                what="the OpenAI route client journeys")
        if gate.boundary:
            harness.boundary(gate.boundary)
        cls.trusted, cls.version = gate.trusted, gate.version

    def journey(self, scenario, prompts, *, account=False):
        """Run the client once per prompt in one session; returns the runs
        and the relay's per-request facts."""

        root = Path(tempfile.mkdtemp(prefix="gwtest-route-client-"))
        self.addCleanup(shutil.rmtree, root, True)
        if account:
            gateway = route.account_gateway(root, respond=scenario.respond)
        else:
            gateway = harness.GatewayHarness(
                document_factory=route.gateway_document,
                upstream_factory=lambda path: harness.FakeUpstream(path, respond=scenario.respond))
        self.addCleanup(gateway.close)
        alias = sorted(gateway.info["reviewed"])[0]
        environ = {"HOME": str(root / "ambient/home"), "PATH": "/usr/bin:/bin"}
        fixture = probe.build_fixture(root / "client", environ=environ)
        probe.seed_fixture_project_trust(fixture, fixture.project_dir)
        settings = probe.write_fixture_settings(fixture, "scope/settings.json", {
            "permissions": {"defaultMode": "default"},
            "env": {"CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL": "1"}})
        child_env = {"CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1", "ANTHROPIC_SMALL_FAST_MODEL": alias,
                     "ANTHROPIC_DEFAULT_HAIKU_MODEL": alias}
        session = str(uuid.uuid4())
        runs = []
        with ClientRelay(gateway, scenario) as relay:
            for index, (name, prompt) in enumerate(prompts):
                relay.run_name = name
                if name == "fallback":
                    scenario.fail_next_stream = True
                common = ("--model", alias, "--settings", str(settings),
                          "--session-id" if index == 0 else "--resume", session)
                if callable(prompt):
                    # An interactive run: a bounded PTY script.
                    try:
                        result = probe.run_native_pty(
                            common, prompt(relay, sorted(gateway.info["reviewed"])), trusted=self.trusted,
                            fixture=fixture, provider=relay, allow_real=True, environ=environ, timeout=90,
                            child_env=child_env)
                    except probe.PTYTimeoutError as error:
                        # A failed run, never a pass: assert_completed refuses it.
                        runs.append({"run": name, "returncode": None, "timed_out": True, "daemon_unchanged": None,
                                     "is_error": None, "result": str(error)[:200]})
                        continue
                else:
                    result = probe.run_native(
                        ("-p", prompt, "--output-format", "json", *common),
                        trusted=self.trusted, fixture=fixture, provider=relay, allow_real=True,
                        environ=environ, timeout=120, child_env=child_env)
                document = result_document(result.stdout)
                runs.append({"run": name, "returncode": result.returncode, "timed_out": result.timed_out,
                             "daemon_unchanged": result.daemon is not None and result.daemon.unchanged,
                             "is_error": document.get("is_error"), "result": str(document.get("result"))[:200]})
        self.assertEqual(relay.completion_errors(), [])
        self.assertTrue(all(run["daemon_unchanged"] is not False for run in runs), "client daemon tripwire")
        return runs, relay.rows

    def assert_completed(self, runs):
        for run in runs:
            self.assertFalse(run["timed_out"], run)
            self.assertEqual(run["returncode"], 0, run)
            self.assertFalse(run["is_error"], run)


PTY_PROMPT = "❯".encode()


def settled(relay, quiet=1.5, limit=20.0):
    """A PTY barrier: the client has sent no request for ``quiet`` seconds."""

    def wait():
        deadline = time.monotonic() + limit
        seen, since = len(relay.rows), time.monotonic()
        while time.monotonic() < deadline:
            time.sleep(0.1)
            if len(relay.rows) != seen:
                seen, since = len(relay.rows), time.monotonic()
            elif time.monotonic() - since >= quiet:
                return
    return wait


def model_switch(relay, aliases):
    """Interactive: switch to another key-route model and back, with a turn
    between (whatever the client sends for a switch is relayed). Switching
    back after a turn asks for confirmation; Enter takes the first choice
    (on an empty prompt it does nothing). Steps wait for the client to go
    quiet: its screen text is not contiguous in the terminal stream."""

    first, other = aliases[0], aliases[1]
    return [probe.PTYInteraction(b"Choose", b"2\r"), probe.PTYInteraction(b"Press", b"\r"),
            probe.PTYInteraction(PTY_PROMPT, f"/model {other}\r".encode(), before_send=settled(relay)),
            probe.PTYInteraction(PTY_PROMPT, b"interactive turn\r", before_send=settled(relay)),
            probe.PTYInteraction(b"PROBE-OK", b"", preserve_after_wait=True),
            probe.PTYInteraction(PTY_PROMPT, f"/model {first}\r".encode(), before_send=settled(relay)),
            probe.PTYInteraction(PTY_PROMPT, b"\r", before_send=settled(relay)),
            probe.PTYInteraction(PTY_PROMPT, b"/exit\r", before_send=settled(relay))]


JOURNEY = (("interactive", model_switch), ("turn", "journey turn"), ("helper", "/rename"), ("count", "/context"),
           ("fallback", "fallback turn"), ("compact", "/compact"))


class PinnedClientKeyRouteTests(_PinnedClientCase):
    """The pinned client on the OpenAI API-key route."""

    def test_the_actual_clients_output_cap_reaches_the_key_route(self):
        scenario = Scenario()
        runs, rows = self.journey(scenario, JOURNEY)
        self.assert_completed(runs)
        caps = sorted({row["max_tokens"] for row in rows if row["max_tokens"] is not None})
        generating = [row for row in rows if row["route"] == "/v1/messages"]
        facts("output-cap-key", runs=[run["run"] for run in runs], scenario=scenario.summary(), ingress_caps=caps,
              low_caps=[cap for cap in caps if 1 <= cap < MIN_OUTPUT_TOKENS],
              count_tokens_requests=len(rows) - len(generating),
              requests=[{key: row[key] for key in ("run", "class", "compaction", "stream", "max_tokens", "status")}
                        | {"egress": [hit["max_output_tokens"] for hit in row["upstream"]]} for row in generating])
        self.assertTrue(generating)
        for row in generating:
            with self.subTest(run=row["run"], cls=row["class"], stream=row["stream"]):
                self.assertIsNotNone(row["max_tokens"], "the client sent no output cap")
                self.assertTrue(row["upstream"], "the request never reached the upstream")
                self.assertEqual(row["status"], 200)
                for hit in row["upstream"]:
                    # A cap below OpenAI's minimum is sent as 16.
                    self.assertEqual(hit["max_output_tokens"], max(row["max_tokens"], MIN_OUTPUT_TOKENS))
                    self.assertEqual(hit["limits"], [])
        # The interactive switch to another key-route model and back: each
        # switch validates the model with max_tokens 1, which the upstream
        # accepts as 16 (ending the answer there), and the turn in between
        # runs on the model switched to.
        interactive = [row for row in generating if row["run"] == "interactive"]
        checks = [row for row in interactive if row["max_tokens"] == 1 and not row["stream"]]
        self.assertEqual([row["status"] for row in checks], [200, 200])
        aliases = sorted({row["model"] for row in checks})
        self.assertEqual(len(aliases), 2, "the switch did not change the model and back")
        turns = [row for row in interactive if row["class"] == "main"]
        self.assertTrue(turns)
        self.assertTrue(all(row["model"] == checks[0]["model"] for row in turns), "the turn ran on another model")
        # The journey covered what it names: turns streaming and not (the
        # fallback after a failed stream), a helper, compaction, token counting.
        self.assertEqual(scenario.fallbacks, 1)
        self.assertTrue(any(row["class"] == "main" and not row["stream"] for row in generating))
        self.assertTrue(any(row["class"] == "main" and row["stream"] for row in generating))
        self.assertTrue(any(row["class"] == "compaction" for row in generating))
        self.assertTrue(any(row["class"] not in ("main", "compaction") for row in generating))
        self.assertTrue(any(row["route"].endswith("/count_tokens") for row in rows))

    def recover(self, overflow, case):
        scenario = Scenario(overflow=overflow)
        prompts = [(f"seed-{index + 1}", f"seed turn {index + 1}") for index in range(SEEDS)]
        runs, rows = self.journey(scenario, (*prompts, ("trigger", "trigger turn")))
        facts(case, runs=runs, scenario=scenario.summary(),
              requests=[{key: row[key] for key in ("run", "class", "action", "compaction", "stream", "status")}
                        | {"errors": [error.get("error", {}).get("message") for error in row["errors"]]}
                        for row in rows])
        return runs, rows, scenario

    def assert_recovered(self, runs, rows, scenario):
        self.assert_completed(runs)
        self.assertIn("PROBE-RECOVERED", runs[-1]["result"])
        self.assertGreaterEqual(scenario.rejections, 1)
        self.assertEqual(scenario.summaries, 1)
        self.assertGreaterEqual(scenario.recovered, 1)
        compaction = [row for row in rows if row["class"] == "compaction"]
        self.assertEqual([row["status"] for row in compaction], [200])
        self.assertEqual(compaction[0]["compaction"], "reactive")
        refused = [row for row in rows if row["action"] == "overflow"]
        self.assertTrue(refused)
        for row in refused:
            self.assertEqual(row["errors"], [NATIVE_OVERFLOW], row)
        return refused

    def test_the_actual_client_compacts_on_an_overflow_before_headers(self):
        refused = self.assert_recovered(*self.recover(overflow_before_headers, "overflow-before-headers"))
        self.assertTrue(all(row["status"] == 400 for row in refused))

    def test_the_actual_client_compacts_on_an_overflow_after_headers(self):
        refused = self.assert_recovered(*self.recover(overflow_after_headers, "overflow-after-headers"))
        # The stream had started (200) and ended with the native error event;
        # the client's non-streaming retry got the same refusal as HTTP 400.
        self.assertTrue(any(row["stream"] and row["status"] == 200 for row in refused))

    def test_an_unidentified_context_error_is_not_compacted(self):
        runs, rows, scenario = self.recover(unidentified_context_error, "unidentified-context-error")
        self.assertTrue(runs[-1]["is_error"], runs[-1])
        self.assertEqual(scenario.summaries, 0)
        self.assertEqual(scenario.recovered, 0)
        refused = [row for row in rows if row["action"] == "overflow"]
        self.assertTrue(refused)
        for row in refused:
            self.assertNotIn("prompt is too long", json.dumps(row["errors"]))


class PinnedClientAccountRouteTests(_PinnedClientCase):
    """The pinned client on the ChatGPT account route (a real account
    credential behind a fixture TLS endpoint)."""

    def test_the_actual_client_compacts_on_an_account_overflow(self):
        for case, overflow in (("before-headers", overflow_before_headers), ("after-headers", overflow_after_headers)):
            with self.subTest(case=case):
                scenario = Scenario(overflow=overflow)
                prompts = [(f"seed-{index + 1}", f"seed turn {index + 1}") for index in range(SEEDS)]
                runs, rows = self.journey(scenario, (*prompts, ("trigger", "trigger turn")), account=True)
                facts("account-overflow-" + case, runs=runs, scenario=scenario.summary())
                self.assert_completed(runs)
                self.assertIn("PROBE-RECOVERED", runs[-1]["result"])
                self.assertEqual(scenario.summaries, 1)
                refused = [row for row in rows if row["action"] == "overflow"]
                self.assertTrue(refused)
                for row in refused:
                    self.assertEqual(row["errors"], [NATIVE_OVERFLOW], row)

    def test_the_actual_clients_output_cap_does_not_reach_the_account_route(self):
        scenario = Scenario()
        runs, rows = self.journey(scenario, (("turn", "journey turn"), ("compact", "/compact")), account=True)
        self.assert_completed(runs)
        caps = sorted({row["max_tokens"] for row in rows if row["max_tokens"] is not None})
        facts("output-cap-account", runs=[run["run"] for run in runs], ingress_caps=caps,
              requests=len(rows), upstream=sum(len(row["upstream"]) for row in rows))
        generating = [row for row in rows if row["route"] == "/v1/messages"]
        self.assertTrue(generating)
        for row in generating:
            with self.subTest(run=row["run"], cls=row["class"]):
                self.assertTrue(row["upstream"], "the request never reached the upstream")
                for hit in row["upstream"]:
                    self.assertEqual(hit["path"], "/backend-api/codex/responses")
                    self.assertIsNone(hit["max_output_tokens"])
                    self.assertEqual(hit["limits"], [])


if __name__ == "__main__":
    unittest.main()
