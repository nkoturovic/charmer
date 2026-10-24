"""Traffic through the actual gateway and disposable upstreams only.

Cache counts are fixture values: this proves transport/replay, not provider cache
accuracy or a live qualification. Native runs use the pinned client and netns
harness; request bodies stay in memory and only bounded facts are printed.
"""
from __future__ import annotations

import copy
import json
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest import mock

from charmer import probe, render, state
from _catalog import SHIPPED_ROOT
from _tier import real_binary_gate
import _gateway_harness as harness
import test_gateway_hint_client as recognition
from test_gateway_contracts import reconstructed_blocks
from test_client_mcp_policy import _SERVER as MCP_COUNTER_SERVER

BINDING_BETA = "thinking-binding-controls-2026-08-01"
# Upstream's public, syntactically valid preservedThinkingTestSignature fixture;
# not an auth token. No real model call is made or implied by its syntax.
SIGNATURE = (
    "CAISqwIKiAEIEBgCKkBHRlRBsNiptQUWfPoOhuQKwi5LnncZVO9bB5jqOs76D7uBtgktML0zqJtNmLHXHHcgD6lk4MQu4QBXzFd1lbC3Mg5j"
    "bGF1ZGUtZmFibGUtNTgBQgh0aGlua2luZ1okZDk3NDM5NzUtNGJiMC00OTM2LTllMjgtZDViMGQyMWJkYzQ4EgxCGh+XVFFFeySAjtAaDL/A1Llt"
    "Gu6MMJ+eXSIwsN0oBpDrqLv22UBfkMnTotnIbkvkOyb9xZHgigG6OZVHaI3gThm+maLKmgO5PrFLKlDFYp+YZksy/wKwszJlnLTPzAK+NUlfzagOE1"
    "ymtZTXhAYK260XyFYmg/te/C231+Fr/hoX+EJoUBnrn0gD7hqMISOT+TaFEuOXYsN517GfaxgB"
)


def facts(gateway, case, **values):
    print("gateway v8 traffic: " + json.dumps({"case": case,
        "gateway": harness.binary_identity(gateway.binary, gateway.version), **values}, sort_keys=True))


def initial_message(payload):
    """Copy a completed fixture message into a nonterminal stream start."""
    return {**copy.deepcopy(payload), "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {**copy.deepcopy(payload["usage"]), "output_tokens": 0}}


def initial_fields(message):
    """Bounded wire facts; missing fields must not masquerade as explicit null."""
    return {"stop_reason": message.get("stop_reason", "missing"),
            "stop_sequence": message.get("stop_sequence", "missing"),
            "output_tokens": message.get("usage", {}).get("output_tokens", "missing")}


def message_reply(body, *, cache=0, thinking=False):
    content = ([{"type": "thinking", "thinking": "fixture reasoning", "signature": SIGNATURE}]
               if thinking else []) + [{"type": "text", "text": "PROBE-OK"}]
    payload = probe._message_payload(content, model=body["model"], stop_reason="end_turn",
                                     message_id="msg_v8_fixture", cache_read_input_tokens=cache)
    if not body.get("stream"):
        return 200, json.dumps(payload).encode(), "application/json"
    # The shared text/tool responder intentionally omits thinking. Emit its
    # real delta/signature wire shape here, rather than injecting replay data.
    events = [("message_start", {"type": "message_start", "message": initial_message(payload)})]
    for index, block in enumerate(content):
        kind = block["type"]
        events.append(("content_block_start", {"type": "content_block_start", "index": index,
                       "content_block": {"type": kind, kind: ""}}))
        events.append(("content_block_delta", {"type": "content_block_delta", "index": index,
                       "delta": {"type": kind + "_delta", kind: block[kind]}}))
        if kind == "thinking":
            events.append(("content_block_delta", {"type": "content_block_delta", "index": index,
                           "delta": {"type": "signature_delta", "signature": SIGNATURE}}))
        events.append(("content_block_stop", {"type": "content_block_stop", "index": index}))
    events.extend((("message_delta", {"type": "message_delta", "delta": {
                       "stop_reason": payload["stop_reason"], "stop_sequence": payload["stop_sequence"]},
                                     "usage": {"output_tokens": payload["usage"]["output_tokens"]}}),
                   ("message_stop", {"type": "message_stop"})))
    return 200, b"".join(harness.sse_frame(kind, value) for kind, value in events), "text/event-stream"


def text_tool_events(payload):
    """The text/tool fixture's SSE events; the shared helper stays untouched."""
    events = list(probe._sse_message(payload).events)
    events[0] = ("message_start", {"type": "message_start", "message": initial_message(payload)})
    events[-2] = ("message_delta", {"type": "message_delta", "delta": {
        "stop_reason": payload["stop_reason"], "stop_sequence": payload["stop_sequence"]},
        "usage": {"output_tokens": payload["usage"]["output_tokens"]}})
    return events


def direct_reply(_route, body, _raw, _headers):
    status, data, kind = message_reply(body)
    return status, kind, data


def post(gateway, body, *, timeout=45):
    return harness.unix_request(gateway.socket, "POST", "/v1/messages", headers=gateway.headers(),
                                body=json.dumps(body).encode(), timeout=timeout)


def request_body(model="claude-opus-5-5", **extra):
    return {"model": model, "max_tokens": 128, "messages": [{"role": "user", "content": "fixture turn"}], **extra}


class StreamFixtureTests(unittest.TestCase):
    def check_frames(self, events, payload):
        self.assertEqual(events[0]["type"], "message_start")
        start = events[0]["message"]
        self.assertEqual(start["content"], [])
        self.assertIsNone(start["stop_reason"])
        self.assertIsNone(start["stop_sequence"])
        self.assertEqual(start["usage"]["output_tokens"], 0)
        terminal = events[-2]
        self.assertEqual(terminal["type"], "message_delta")
        self.assertEqual(terminal["delta"].get("stop_reason"), payload["stop_reason"])
        self.assertEqual(terminal["delta"].get("stop_sequence"), payload["stop_sequence"])
        self.assertEqual(terminal["usage"]["output_tokens"], payload["usage"]["output_tokens"])
        self.assertEqual(events[-1]["type"], "message_stop")

    def test_thinking_stream_starts_nonterminal_and_json_stays_complete(self):
        for thinking in (False, True):
            with self.subTest(thinking=thinking):
                body = request_body()
                _, completed, _ = message_reply(body, cache=13, thinking=thinking)
                payload = json.loads(completed)
                self.assertEqual(payload["stop_reason"], "end_turn")
                self.assertEqual(payload["usage"]["output_tokens"], 1)
                _, wire, _ = message_reply({**body, "stream": True}, cache=13, thinking=thinking)
                events = harness.Reply(200, {}, wire).events()
                self.check_frames(events, payload)
                self.assertEqual(events[0]["message"]["usage"]["cache_read_input_tokens"], 13)

    def test_text_tool_stream_preserves_terminal_payload_and_actual_usage(self):
        for stop, sequence, output in (("end_turn", None, 1), ("tool_use", None, 7),
                                       ("stop_sequence", "fixture stop", 9)):
            with self.subTest(stop=stop):
                content = ([{"type": "tool_use", "id": "toolu_fixture", "name": "fixture", "input": {}}]
                           if stop == "tool_use" else [{"type": "text", "text": "fixture"}])
                payload = probe._message_payload(content, model="fixture", stop_reason=stop, message_id="msg_fixture")
                payload["stop_sequence"] = sequence
                payload["usage"]["output_tokens"] = output
                before = copy.deepcopy(payload)
                events = text_tool_events(payload)
                self.assertEqual(payload, before, "fixture construction mutated the intended terminal payload")
                self.check_frames([value for _kind, value in events], payload)


class WebSearchReceiptTests(unittest.TestCase):
    def test_duplicate_upstream_hits_are_rejected_in_json_and_sse(self):
        expected = [
            {"type": "server_tool_use", "id": "ws_fixture", "name": "web_search", "input": {"query": "fixture query"}},
            {"type": "web_search_tool_result", "tool_use_id": "ws_fixture", "content": [
                {"type": "web_search_result", "title": "Fixture", "url": "https://example.invalid/result", "page_age": None}]},
            {"type": "text", "text": "FIXTURE-SEARCH"}]
        for duplicate_stream in (False, True):
            with self.subTest(stream=duplicate_stream):
                gateway = mock.Mock()
                def request(_alias, *, stream, body):
                    raw = (b"".join(harness.sse_frame("content_block_start", {"type": "content_block_start",
                            "index": index, "content_block": block}) for index, block in enumerate(expected))
                           if stream else json.dumps({"content": expected}).encode())
                    return harness.Reply(200, {}, raw, hits=[object()] * (2 if stream == duplicate_stream else 1))
                gateway.request.side_effect = request
                case = CodexGoldenTests("test_web_search_response_golden")
                try:
                    with mock.patch.object(harness, "GatewayHarness", return_value=gateway), \
                            mock.patch(__name__ + ".facts") as receipt:
                        with self.assertRaisesRegex(AssertionError, "WebSearch upstream request count"):
                            case.test_web_search_response_golden()
                        self.assertTrue(all(call.kwargs["provider_requests"] == 1 for call in receipt.call_args_list))
                finally:
                    case.doCleanups()


class BindingClassifierTests(unittest.TestCase):
    def test_only_exact_source_supported_replay_addition_is_permitted(self):
        original = request_body(thinking={"type": "adaptive"}, messages=[{"role": "assistant", "content": [
            {"type": "thinking", "thinking": "fixture", "signature": SIGNATURE}]}])
        changed = copy.deepcopy(original)
        changed["thinking"]["block_binding"] = {"prefix_mismatch_behavior": "drop_block"}
        delta = lambda a, b: recognition.d11_body_delta(json.dumps(a).encode(), json.dumps(b).encode())
        self.assertEqual(delta(original, changed), (recognition.D11_THINKING_BINDING,))
        self.assertEqual(recognition.d11_disposition("main", delta(original, changed)), "confirmed")
        for field, value in (("model", "claude-fable-5"), ("messages", []),
                             ("thinking", {"type": "disabled"}), ("tool_choice", {"type": "any"})):
            before, after = copy.deepcopy(original), copy.deepcopy(changed)
            before[field] = value
            if field != "thinking":
                after[field] = value
            self.assertIn("thinking", delta(before, after), field)
            self.assertEqual(recognition.d11_disposition("main", delta(before, after)), "blocks")
        for mutation in ({"type": "adaptive", "display": "omitted", "block_binding": {"prefix_mismatch_behavior": "drop_block"}},
                         {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "error"}}):
            changed["thinking"] = mutation
            self.assertEqual(delta(original, changed), ("thinking",))
            self.assertEqual(recognition.d11_disposition("subagent", delta(original, changed)), "blocks")


class ThinkingWireTests(unittest.TestCase):
    def test_disabled_and_budget_zero_per_static_wire(self):
        gateway = harness.GatewayHarness(document_factory=recognition.native_document,
            upstream_factory=lambda path: harness.FakeUpstream(path, direct_reply))
        self.addCleanup(gateway.close)
        rows = []
        for wire in ("claude-opus-5-5", "claude-fable-5", "claude-fable-5-1",
                     "claude-sonnet-5-5", "claude-haiku-5-5"):
            for thinking in ({"type": "disabled"}, {"type": "enabled", "budget_tokens": 0}):
                for stream in (False, True):
                    with self.subTest(wire=wire, thinking=thinking, stream=stream):
                        before = len(gateway.upstream.hits)
                        reply = post(gateway, request_body(wire, thinking=thinking, stream=stream))
                        self.assertEqual(reply.status, 200)
                        self.assertEqual(len(gateway.upstream.hits) - before, 1)
                        sent = gateway.upstream.hits[-1].body
                        expected = ({"type": "between_tools"} if "sonnet" in wire else
                                    {"type": "disabled"} if "haiku" in wire else
                                    {"type": "adaptive", "display": "omitted"})
                        self.assertEqual(sent.get("thinking"), expected)
                        effort = (sent.get("output_config") or {}).get("effort")
                        self.assertEqual(effort, None if "sonnet" in wire or "haiku" in wire else "low")
                        self.assertNotIn("budget_tokens", sent["thinking"])
                        rows.append({"wire": wire, "input": thinking, "stream": stream,
                                     "thinking": sent["thinking"], "effort": effort})
        facts(gateway, "disabled-budget-zero", rows=rows)

    def test_signed_replay_binding_and_payload_filter_opt_out(self):
        for filtered in (False, True):
            def document(*args):
                doc, aliases, info = recognition.native_document(*args)
                doc["openai-compatibility"] = []  # discard the old fixture sentinel before refinalizing
                if filtered:
                    doc["payload"] = {"filter": [{"models": [{"name": "claude-opus-5-5", "protocol": "claude"}],
                                                  "params": ["thinking.block_binding"]}]}
                return render.finalize_document(doc), aliases, info

            captured = []
            def respond(route, body, raw, headers):
                captured.append((body, headers))
                return direct_reply(route, body, raw, headers)

            gateway = harness.GatewayHarness(document_factory=document,
                upstream_factory=lambda path: harness.FakeUpstream(path, respond))
            try:
                history = [{"role": "user", "content": "earlier prefix"}, {"role": "assistant", "content": [
                    {"type": "thinking", "thinking": "fixture reasoning", "signature": SIGNATURE},
                    {"type": "text", "text": "prior response"}]}, {"role": "user", "content": [
                        {"type": "text", "text": "reshaped prefix replay", "cache_control": {"type": "ephemeral"}}]}]
                reply = post(gateway, request_body(thinking={"type": "adaptive"}, messages=history,
                                                   system="changed fixture prefix"))
                self.assertEqual(reply.status, 200)
                sent, headers = captured[-1]
                self.assertEqual(sent["messages"], history)
                binding = sent.get("thinking", {}).get("block_binding")
                self.assertEqual(binding, None if filtered else {"prefix_mismatch_behavior": "drop_block"})
                if not filtered:
                    self.assertIn(BINDING_BETA, headers.get("anthropic-beta", "").split(","))
                facts(gateway, "binding-filter", filtered=filtered, binding=binding,
                      binding_beta=BINDING_BETA in headers.get("anthropic-beta", "").split(","))
            finally:
                gateway.close()


class RegistryTrafficTests(unittest.TestCase):
    def test_static_wins_and_seeded_exclusions_remain_additive(self):
        static, extra = "claude-opus-5-5", "claude-gwtest-v8-overlay"
        for globally_disabled in (False, True):
            def document(*args):
                doc, aliases, info = recognition.native_document(*args)
                doc.update({"claude-api-key": [], "openai-compatibility": [], "oauth-model-alias": {},
                            "oauth-excluded-models": {"claude": ["*"] if globally_disabled else []},
                            render.OVERLAY_KEY: {"claude": [
                                {"name": static, "max-context-length": 4096, "max-completion-tokens": 64,
                                 "thinking": {"levels": ["low"]}},
                                {"name": extra, "max-context-length": 8192, "max-completion-tokens": 128,
                                 "thinking": {"levels": ["low"]}}]}})
                state.atomic_write(Path(doc["auth-dir"]) / "claude-v8-seed.json", json.dumps({
                    "type": "claude", "email": "v8-seed@example.invalid", "access_token": recognition.D11_OAUTH_TOKEN,
                    "expired": "2099-01-01T00:00:00Z", "excluded_models": ["claude-haiku-5-5"]}).encode())
                return render.finalize_document(doc), aliases, info

            gateway = harness.GatewayHarness(document_factory=document)
            try:
                reply = gateway.get("/v1/models")
                self.assertEqual(reply.status, 200)
                rows = {row["id"]: row for row in reply.json()["data"]}
                if globally_disabled:
                    self.assertNotIn(static, rows)
                    self.assertNotIn(extra, rows)
                else:
                    self.assertIn(static, rows)
                    self.assertIn(extra, rows)
                    self.assertEqual(rows[static]["max_input_tokens"], 1_000_000)
                    self.assertEqual(rows[extra]["max_input_tokens"], 8192)
                self.assertNotIn("claude-haiku-5-5", rows)
                self.assertEqual(gateway.upstream.hits, [])
                facts(gateway, "static-overlay-exclusions", globally_disabled=globally_disabled,
                      static_present=static in rows, overlay_present=extra in rows,
                      provider_requests=0, credential_exclusion_preserved=True)
            finally:
                gateway.close()


class CodexGoldenTests(unittest.TestCase):
    def test_actual_native_configuration_update_capability(self):
        harness.run_codex_diagnostic("TestGatewayV8NativeConfigurationUpdateBoundary")

    def test_web_search_response_golden(self):
        search = {"id": "ws_fixture", "type": "web_search_call", "status": "completed", "action": {
            "type": "search", "query": "fixture query", "sources": [{"url": "https://example.invalid/result", "title": "Fixture"}]}}
        message = {"id": "msg_fixture", "type": "message", "role": "assistant", "status": "completed",
                   "content": [{"type": "output_text", "text": "FIXTURE-SEARCH", "annotations": []}]}
        def respond(_route, body, _raw, _headers):
            response = {"id": "resp_fixture", "object": "response", "status": "completed", "model": body["model"],
                        "output": [search, message], "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
            frames = [("response.created", {"response": {**response, "status": "in_progress", "output": []}}),
                      ("response.output_item.added", {"output_index": 0, "item": search}),
                      ("response.output_item.done", {"output_index": 0, "item": search}),
                      ("response.output_item.done", {"output_index": 1, "item": message}),
                      ("response.completed", {"response": response})]
            return 200, "text/event-stream", b"".join(harness.sse_frame(kind, {"type": kind, **data}) for kind, data in frames)

        gateway = harness.GatewayHarness(upstream_factory=lambda path: harness.FakeUpstream(path, respond))
        self.addCleanup(gateway.close)
        expected = [
            {"type": "server_tool_use", "id": "ws_fixture", "name": "web_search", "input": {"query": "fixture query"}},
            {"type": "web_search_tool_result", "tool_use_id": "ws_fixture", "content": [
                {"type": "web_search_result", "title": "Fixture", "url": "https://example.invalid/result", "page_age": None}]},
            {"type": "text", "text": "FIXTURE-SEARCH"}]
        for stream in (False, True):
            reply = gateway.request("gwtest-codex-reasoning-effort-high", stream=stream,
                body={"tools": [{"type": "web_search_20250305", "name": "web_search"}]})
            self.assertEqual(reply.status, 200)
            self.assertEqual(len(reply.hits), 1, "WebSearch upstream request count")
            blocks = reconstructed_blocks(reply) if stream else reply.json()["content"]
            for block in blocks:
                if "partial_json" in block:
                    block["input"] = json.loads(block.pop("partial_json"))
            self.assertEqual(blocks, expected)
            facts(gateway, "codex-web-search-response-golden", stream=stream, content=blocks,
                  provider_requests=len(reply.hits))

    def test_mcp_integer_bounds_and_web_search_request(self):
        gateway = harness.GatewayHarness()
        self.addCleanup(gateway.close)
        schema = {"type": "object", "properties": {"bound": {"type": "integer", "minimum": -(2**63),
                                                            "maximum": 2**63 - 1}}, "required": ["bound"]}
        for stream in (False, True):
            reply = gateway.request("gwtest-codex-reasoning-effort-high", stream=stream, body={"tools": [
                {"name": "mcp__fixture__bounded", "input_schema": schema},
                {"type": "web_search_20250305", "name": "web_search", "max_uses": 2}]})
            self.assertEqual(reply.status, 200)
            self.assertEqual(len(reply.hits), 1)
            tools = reply.hits[0].body["tools"]
            function = next(tool for tool in tools if tool.get("name") == "mcp__fixture__bounded")
            self.assertEqual(function["parameters"]["properties"]["bound"], schema["properties"]["bound"])
            self.assertIs(type(function["parameters"]["properties"]["bound"]["maximum"]), int)
            self.assertEqual([tool for tool in tools if tool["type"] == "web_search"], [{"type": "web_search"}])
            facts(gateway, "codex-tool-request-golden", stream=stream, bounds=schema["properties"]["bound"],
                  tool_types=[tool["type"] for tool in tools])


class HTTP1Proxy:
    """Fixture-only CONNECT interception; offers HTTP/1.1 and no h2 ALPN."""
    def __init__(self, path, cert, key, delay):
        self.hits, self.connects, self.negotiated = [], [], []
        self.stop = threading.Event()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        context.set_alpn_protocols(["http/1.1"])
        owner = self

        class Inner(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                owner.hits.append({"route": self.path, "protocol": self.request_version})
                status, data, kind = message_reply(body)
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        class Outer(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_CONNECT(self):
                owner.connects.append(self.path)
                if self.path != "api.anthropic.com:443":
                    self.send_error(403)
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.flush()
                self.close_connection = True
                if owner.stop.wait(delay):
                    return
                try:
                    with context.wrap_socket(self.connection, server_side=True) as connection:
                        owner.negotiated.append(connection.selected_alpn_protocol())
                        Inner(connection, self.client_address, self.server)
                except (OSError, ssl.SSLError):
                    pass  # expected for the bounded silent-handshake control

        self.server = probe.UnixHTTPServer(str(path), Outer)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def proxy_gateway(root, delay):
    openssl = shutil.which("openssl")
    if openssl is None:
        raise RuntimeError("proxy gateway fixture requires openssl on the test runner PATH")
    openssl = str(Path(openssl).absolute())
    cert, key = root / "ca.pem", root / "key.pem"
    subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=api.anthropic.com", "-addext", "subjectAltName=DNS:api.anthropic.com",
                    "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True,
                   timeout=60, env={"PATH": "/usr/bin:/bin"})
    trust = {}
    def document(bundle, egress, gateway, home):
        doc, aliases, info = recognition.native_document(bundle, egress, gateway, home)
        doc.update({"claude-api-key": [], "openai-compatibility": [], "proxy-url": f"http://127.0.0.1:{egress}",
                    "request-retry": 0, **recognition.shipped_claude_pool()})
        # Assert the shipped setting; do not silently inject it into this test.
        if doc.get("tls-handshake-timeout-seconds") != 20:
            raise AssertionError("TLS20 disposition requires the rendered 20-second policy")
        state.atomic_write(Path(doc["auth-dir"]) / "claude-v8.json", json.dumps({"type": "claude",
            "email": "v8@example.invalid", "access_token": recognition.D11_OAUTH_TOKEN,
            "scope": "user:inference", "expired": "2099-01-01T00:00:00Z"}).encode())
        trust["path"] = home / ".config/charmer/v8-ca.pem"
        state.atomic_write(trust["path"], cert.read_bytes())
        return render.finalize_document(doc), aliases, info

    original = probe.start_isolated_process
    def start(argv, **kwargs):
        if "path" in trust:
            kwargs["env"] = {**kwargs["env"], "SSL_CERT_FILE": str(trust["path"])}
        return original(argv, **kwargs)
    with mock.patch.object(probe, "start_isolated_process", start):
        return harness.GatewayHarness(document_factory=document,
            upstream_factory=lambda path: HTTP1Proxy(path, cert, key, delay))


class ProxyGatewayFixtureTests(unittest.TestCase):
    def test_resolves_openssl_once_with_exact_isolated_subprocess_call(self):
        root = Path("/fixture/proxy")
        sentinels = {"ANTHROPIC_API_KEY": "fixture-ambient-credential",
                     "OPENAI_API_KEY": "fixture-ambient-credential",
                     "HTTP_PROXY": "http://fixture-proxy.example.invalid:8080",
                     "HTTPS_PROXY": "http://fixture-proxy.example.invalid:8080",
                     "https_proxy": "http://fixture-proxy.example.invalid:8080",
                     "HOME": "/fixture/ambient/home",
                     "OPENSSL_CONF": "/fixture/ambient/openssl.cnf"}
        for openssl in ("/nix/store/fixture-openssl/bin/openssl", "relative/bin/openssl"):
            expected_openssl = str(Path.cwd() / openssl)
            with self.subTest(openssl=openssl), \
                    mock.patch.dict("os.environ", {"PATH": str(Path(openssl).parent), **sentinels}, clear=True), \
                    mock.patch.object(shutil, "which", return_value=openssl) as which, \
                    mock.patch.object(subprocess, "run") as run, \
                    mock.patch.object(harness, "GatewayHarness") as gateway_factory:
                gateway = proxy_gateway(root, 12.0)
                which.assert_called_once_with("openssl")
                run.assert_called_once_with(
                    [expected_openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                     "-subj", "/CN=api.anthropic.com", "-addext", "subjectAltName=DNS:api.anthropic.com",
                     "-keyout", str(root / "key.pem"), "-out", str(root / "ca.pem")],
                    check=True, capture_output=True, timeout=60, env={"PATH": "/usr/bin:/bin"})
                self.assertTrue(Path(run.call_args.args[0][0]).is_absolute())
                child_env = run.call_args.kwargs["env"]
                for name, value in sentinels.items():
                    self.assertNotIn(name, child_env)
                    self.assertNotIn(value, child_env.values())
                gateway_factory.assert_called_once()
                self.assertIs(gateway, gateway_factory.return_value)

    def test_missing_openssl_fails_before_subprocess_and_gateway_harness(self):
        with mock.patch.object(shutil, "which", return_value=None) as which, \
                mock.patch.object(subprocess, "run") as run, \
                mock.patch.object(harness, "GatewayHarness") as gateway_factory:
            with self.assertRaisesRegex(RuntimeError, "requires openssl on the test runner PATH"):
                proxy_gateway(Path("/fixture/proxy"), 12.0)
            which.assert_called_once_with("openssl")
            run.assert_not_called()
            gateway_factory.assert_not_called()


class ProxyTimeoutTests(unittest.TestCase):
    def test_http1_healthy_slow_and_bounded_silent_handshake(self):
        # The legacy uTLS path had no establishment bound (SDKConfig <= 0);
        # it did not enforce net/http's nominal ten-second handshake timeout.
        # Twelve seconds exercises a meaningful healthy-slow window below20,
        # not an invented claim that the old uTLS path failed after ten seconds.
        for delay, success in ((2.0, True), (12.0, True), (30.0, False)):
            with tempfile.TemporaryDirectory(prefix="gwtest-v8-tls-") as temporary:
                gateway = proxy_gateway(Path(temporary), delay)
                try:
                    offset = gateway.log_path.stat().st_size
                    start = time.monotonic()
                    reply = post(gateway, request_body(), timeout=35)
                    elapsed = time.monotonic() - start
                    log = gateway.log_path.read_bytes()[offset:]
                    facts(gateway, "tls20-proxy", delay=delay, status=reply.status, elapsed=round(elapsed, 3),
                          connects=len(gateway.upstream.connects), protocols=gateway.upstream.negotiated,
                          log_lines=len(log.splitlines()), log_bytes=len(log), requests=1)
                    self.assertEqual(gateway.upstream.connects, ["api.anthropic.com:443"])
                    if success:
                        self.assertEqual(reply.status, 200)
                        self.assertGreaterEqual(elapsed, delay)
                        self.assertLess(elapsed, 20)
                        self.assertEqual(gateway.upstream.negotiated, ["http/1.1"])
                        self.assertEqual([hit["protocol"] for hit in gateway.upstream.hits], ["HTTP/1.1"])
                    else:
                        self.assertGreaterEqual(reply.status, 400)
                        self.assertGreaterEqual(elapsed, 19)
                        self.assertLess(elapsed, 28)
                        self.assertEqual(gateway.upstream.hits, [])
                finally:
                    gateway.close()


def restart_oauth_gateway(gateway):
    """Restart only this harness process, retaining its fixture OAuth file."""
    old_pid = gateway.process.pid
    probe.stop_isolated_process(gateway.process)
    # The stopped ingress bridge leaves its fixture socket inode behind. Use a
    # fresh endpoint instead of bypassing the harness's existing-socket guard.
    gateway.socket = gateway.run / ("restart-" + uuid.uuid4().hex + ".sock")
    spec = json.loads(harness.SERVICE_SPEC.read_text())
    view = probe.HomeView(gateway.home, tuple(gateway.home / p for p in spec["bind_rw"]),
                          tuple(gateway.home / p for p in spec["bind_ro"]))
    with gateway.log_path.open("ab") as log:
        gateway.process = probe.start_isolated_process(
            [gateway.binary, "--config", str(gateway.config), "--local-model"],
            env={"HOME": str(gateway.home), "PATH": "/usr/bin:/bin",
                 "SSL_CERT_FILE": str(gateway.home / ".config/charmer/probe-d11-ca.pem")},
            cwd=gateway.home / spec["working_directory"], home_view=view,
            bridges=[probe.PortBridge(gateway.egress, gateway.run / "upstream.sock", "egress"),
                     probe.PortBridge(gateway.port, gateway.socket, "ingress")], stdout=log, stderr=subprocess.STDOUT)
    gateway.await_ready()
    return old_pid != gateway.process.pid


class TrafficRelay(recognition._GatewayRelay):
    """Original-byte relay with only bounded completion metadata retained."""
    def _respond(self, document, path, context=None):
        status, response = super()._respond(document, path, context)
        if isinstance(response, probe.SseResponse):
            self.rows[-1]["reply_thinking"] = any(kind == "content_block_start"
                and value.get("content_block", {}).get("type") == "thinking" for kind, value in response.events)
            self.rows[-1]["reply_errors"] = sum(kind == "error" for kind, _value in response.events)
            self.rows[-1]["reply_stops"] = sum(kind == "message_stop" for kind, _value in response.events)
            self.rows[-1]["reply_event_order"] = [kind for kind, _value in response.events]
            self.rows[-1]["reply_initial_fields"] = [initial_fields(value["message"])
                for kind, value in response.events if kind == "message_start"]
            self.rows[-1]["reply_stop_reasons"] = [value.get("delta", {}).get("stop_reason")
                for kind, value in response.events if kind == "message_delta"]
        else:
            self.rows[-1]["reply_thinking"] = any(block.get("type") == "thinking"
                for block in response.get("content", []) if isinstance(block, dict))
        return status, response

    def _observe(self, path, headers, hits, status, raw, document):
        self.rows.append({"status": status, "upstream_hits": len(hits), "model": document.get("model"),
                          "route": path, "message_count": len(document.get("messages", [])),
                          "tool_result_count": sum(block.get("type") == "tool_result"
                              for message in document.get("messages", []) if isinstance(message.get("content"), list)
                              for block in message["content"] if isinstance(block, dict)),
                          "assistant_types": sorted({block.get("type") for message in document.get("messages", [])
                              if message.get("role") == "assistant" and isinstance(message.get("content"), list)
                              for block in message["content"] if isinstance(block, dict)})})


def mixed_document(*args):
    doc, aliases, info = harness.build_document(*args)
    doc["openai-compatibility"] = [entry for entry in doc["openai-compatibility"]
                                   if entry["name"] != render.SENTINEL_NAME]
    section = copy.deepcopy(doc["claude-api-key"][0])
    section["models"] = [{"name": "claude-opus-5-5", "alias": "claude-opus-5-5", "force-mapping": True}]
    section.pop("headers", None)
    doc["claude-api-key"].append(section)
    return render.finalize_document(doc), aliases, info


class NativeReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gate = real_binary_gate(SHIPPED_ROOT / "catalog/native-contract.json", what="v8 native replay")
        if gate.boundary:
            raise unittest.SkipTest(gate.boundary)
        cls.trusted, cls.version = gate.trusted, gate.version

    def test_four_turn_signed_replay_restart_and_reshaped_prefix(self):
        root = Path(tempfile.mkdtemp(prefix="gwtest-v8-native-"))
        self.addCleanup(shutil.rmtree, root)
        environ = {"HOME": str(root / "ambient/home"), "PATH": "/usr/bin:/bin"}
        fixture = probe.build_fixture(root / "client", environ=environ)
        probe.seed_fixture_project_trust(fixture, fixture.project_dir)
        turn = [0]
        cache_counts = (0, 64, 128, 0)
        def respond(route, body):
            if route.endswith("/count_tokens"):
                return 200, b'{"input_tokens":1}', "application/json"
            return message_reply(body, cache=cache_counts[turn[0]], thinking=True)

        gateway = recognition.start_oauth_gateway(root, respond)
        self.addCleanup(gateway.close)
        session_id = str(uuid.uuid4())
        runs = []
        previous_tools = None
        with recognition._D11Relay(gateway) as relay:
            for index in range(4):
                turn[0] = index
                if index == 2:
                    self.assertTrue(restart_oauth_gateway(gateway))
                before = len(gateway.upstream.hits)
                offset = gateway.log_path.stat().st_size
                # From 2.1.310 --tools on a --resume turn no longer narrows the offered list (execution stays
                # restricted, measured); --disallowedTools still reshapes the native tool prefix.
                result = probe.run_native(("-p", "fixture turn " + str(index), "--model", "claude-opus-5-5[1m]",
                    "--output-format", "json", "--permission-mode", "default",
                    "--session-id" if index == 0 else "--resume", session_id,
                    *(("--disallowedTools", "Write") if index == 3 else ())),
                    trusted=self.trusted, fixture=fixture, provider=relay, allow_real=True,
                    environ=environ, timeout=120)
                self.assertEqual(result.returncode, 0)
                self.assertFalse(result.timed_out)
                self.assertTrue(result.daemon and result.daemon.unchanged)
                hits = gateway.upstream.hits[before:]
                self.assertEqual(len(hits), 1)
                headers, raw, _route = hits[0]
                body = json.loads(raw)
                binding = body.get("thinking", {}).get("block_binding")
                self.assertEqual(binding, {"prefix_mismatch_behavior": "drop_block"} if index else None)
                self.assertEqual(BINDING_BETA in headers.get("anthropic-beta", "").split(","), index > 0)
                observed_delta = relay.d11_rows[-1]["delta"]
                self.assertEqual(recognition.d11_disposition("main", observed_delta), "confirmed")
                self.assertEqual(recognition.D11_THINKING_BINDING in observed_delta, index > 0)
                tools = [tool["name"] for tool in body.get("tools", [])]
                if index == 3:
                    self.assertIn("Write", previous_tools)
                    self.assertEqual(len(tools), len(set(tools)))
                    self.assertEqual(set(tools), set(previous_tools) - {"Write"}, "native tool-prefix reshape was not observed")
                previous_tools = tools
                returned_cache = json.loads(result.stdout)["usage"]["cache_read_input_tokens"]
                self.assertEqual(returned_cache, cache_counts[index])
                log = gateway.log_path.read_bytes()[offset:]
                runs.append({"turn": index + 1, "upstream_hits": len(hits), "binding": binding,
                             "fixture_cache_read": cache_counts[index], "client_cache_read": returned_cache,
                             "tool_count": len(tools), "prefix_reshaped": index == 3, "restart_before": index == 2,
                             "log_lines": len(log.splitlines()), "log_bytes": len(log)})
            self.assertEqual(relay.completion_errors(), [])
        facts(gateway, "native-replay-restart", client_version=self.version, client_sha256=self.trusted.sha256,
              runs=runs, cache_scope="synthetic transport only; no provider cache claim")

    def test_opus_resume_from_lan_reasoning_history(self):
        with tempfile.TemporaryDirectory(prefix="gwtest-v8-resume-") as temporary:
            root = Path(temporary)
            environ = {"HOME": str(root / "ambient"), "PATH": "/usr/bin:/bin"}
            fixture = probe.build_fixture(root / "client", environ=environ)
            probe.seed_fixture_project_trust(fixture, fixture.project_dir)
            gateway = harness.GatewayHarness(document_factory=mixed_document)
            try:
                session_id = str(uuid.uuid4())
                with TrafficRelay(gateway) as relay:
                    for index, model in enumerate(("gwtest-compat-plain", "claude-opus-5-5")):
                        result = probe.run_native(("-p", "fixture gwtest-mode=reasoning", "--model", model,
                            "--output-format", "json", "--permission-mode", "default",
                            "--session-id" if index == 0 else "--resume", session_id),
                            trusted=self.trusted, fixture=fixture, provider=relay, allow_real=True,
                            environ=environ, timeout=90)
                        self.assertEqual(result.returncode, 0)
                        self.assertFalse(result.timed_out)
                        self.assertTrue(result.daemon and result.daemon.unchanged)
                    self.assertEqual(relay.completion_errors(), [])
                    self.assertEqual(len(relay.rows), 2)
                    self.assertTrue(relay.rows[0]["reply_thinking"], "LAN fixture never delivered its reasoning")
                    self.assertGreater(relay.rows[1]["message_count"], relay.rows[0]["message_count"])
                    self.assertEqual(relay.rows[1]["model"], "claude-opus-5-5")
                    sent = gateway.upstream.hits[-1].body
                    assistant = [block for message in sent["messages"] if message["role"] == "assistant"
                                 for block in message["content"] if isinstance(block, dict)]
                    self.assertTrue(any(block.get("text") == "FAKE_OK" for block in assistant))
                    self.assertFalse(any(block.get("type") in {"thinking", "redacted_thinking"} for block in assistant))
                    facts(gateway, "opus-other-family-resume", client_version=self.version,
                          rows=relay.rows, outgoing_assistant_types=[block["type"] for block in assistant])
            finally:
                gateway.close()

    def test_d216_known_negative_late_terminal_after_completed_text_or_tool(self):
        # Revalidate the known-negative expectation with conformant starts.
        # Late client false-success is not recovery or proof of harmlessness;
        # retain the expected observation, never weaken it to force a pass.
        # Changes (including improvement) require explicit reconciliation.
        # Buffered complete-SSE reconstruction establishes ordered content and
        # bounded execution counts, NOT eager timing or general exactly-once
        # behavior. No previous-version baseline has been established.
        self.assertEqual(self.version, "2.1.310", "requalify late-terminal evidence for a changed client")
        for tool in (False, True):
            paired_usage, skipped = {}, {}
            for cut in (False, True):
                with self.subTest(tool=tool, cut=cut), tempfile.TemporaryDirectory(prefix="gwtest-v8-impact-") as temporary:
                    root = Path(temporary)
                    environ = {"HOME": str(root / "ambient"), "PATH": "/usr/bin:/bin"}
                    fixture = probe.build_fixture(root / "client", environ=environ)
                    probe.seed_fixture_project_trust(fixture, fixture.project_dir)
                    counter = fixture.root / "tool-counter.log"
                    if tool:
                        server = state.ensure_private_dir(fixture.root / "bin") / "counter.py"
                        state.atomic_write(server, MCP_COUNTER_SERVER.encode())
                        config_path = fixture.claude_config_dir / ".claude.json"
                        config = json.loads(state.read_private(config_path))
                        config["mcpServers"] = {"v8counter": {"type": "stdio", "command": "/usr/bin/python3",
                            "args": ["-I", "-S", str(server), "v8counter", str(counter)]}}
                        state.atomic_write(config_path, json.dumps(config).encode())
                    settings = probe.write_fixture_settings(fixture, "scope/watchdog.json", {"env": {
                        "CLAUDE_CODE_RETRY_WATCHDOG": "1", "CLAUDE_CODE_RETRY_WATCHDOG_MAX_WAIT_MS": "10000"}})
                    upstream_rows = []
                    def respond(_route, body, _raw, _headers):
                        first = not upstream_rows
                        content = [{"type": "text", "text": "FIXTURE-FINAL"}]
                        stop = "end_turn"
                        if tool and first:
                            offered = [item["name"] for item in body.get("tools", [])
                                       if item.get("name") == "mcp__v8counter__probe"]
                            # From 2.1.304 the client defers MCP tools behind ToolSearch with a custom base URL;
                            # the fixture calls the deferred tool by name, which the pin accepts (measured on 2.1.310).
                            if not offered and any(item.get("name") == "ToolSearch" for item in body.get("tools", [])):
                                offered = ["mcp__v8counter__probe"]
                            if offered:
                                content = [{"type": "tool_use", "id": "toolu_v8_counter", "name": offered[0], "input": {}}]
                                stop = "tool_use"
                            else:
                                content = [{"type": "text", "text": "FIXTURE-TOOL-NOT-OFFERED"}]
                        payload = probe._message_payload(content, model=body["model"], stop_reason=stop,
                            message_id="msg_v8_impact", input_tokens=7, cache_creation_input_tokens=11,
                            cache_read_input_tokens=13)
                        terminal_payload = copy.deepcopy(payload)
                        events = text_tool_events(payload)
                        self.assertEqual(payload, terminal_payload, "fixture construction changed the terminal payload")
                        start = initial_fields(events[0][1]["message"])
                        self.assertEqual(start, {"stop_reason": None, "stop_sequence": None, "output_tokens": 0})
                        if cut and first:
                            self.assertEqual(events[-1][0], "message_stop")
                            events.pop()
                        upstream_rows.append({"attempt": len(upstream_rows) + 1, "stop_reason": stop, "initial_fields": start,
                            "event_order": [kind for kind, _ in events], "content_types": [item["type"] for item in content],
                            "text": [item["text"] for item in content if item["type"] == "text"],
                            "usage": payload["usage"], "stream": bool(body.get("stream"))})
                        if body.get("stream"):
                            return 200, "text/event-stream", b"".join(harness.sse_frame(kind, value) for kind, value in events)
                        return 200, "application/json", json.dumps(payload).encode()

                    try:
                        gateway = harness.GatewayHarness(upstream_factory=lambda path: harness.FakeUpstream(path, respond))
                    except unittest.SkipTest as exc:  # no candidate gateway: the pair below skips too
                        skipped[cut] = str(exc)
                        raise
                    try:
                        with TrafficRelay(gateway) as relay:
                            result = probe.run_native(("-p", "fixture late-terminal impact", "--model", "gwtest-cc-output-config-high",
                                "--settings", str(settings), "--output-format", "json", "--permission-mode", "default",
                                *(("--allowedTools", "mcp__v8counter__probe") if tool else ())),
                                trusted=self.trusted, fixture=fixture, provider=relay, allow_real=True, environ=environ, timeout=45)
                            outcome = json.loads(result.stdout)
                            calls = (counter.read_text().splitlines().count("call v8counter") if counter.exists() else 0)
                            facts(gateway, "corrected-late-terminal" if cut else "corrected-healthy-control",
                                client_version=self.version, client_sha256=self.trusted.sha256,
                                tool=tool, cut=cut, returncode=result.returncode,
                                timed_out=result.timed_out, is_error=outcome.get("is_error"), final_text=outcome.get("result"),
                                client_usage=outcome.get("usage"), tool_executions=calls, upstream=upstream_rows,
                                downstream=relay.rows, ordinary_tool_continuations=sum(row["tool_result_count"] > 0 for row in relay.rows),
                                relay="buffered complete SSE reconstruction; no eager timing claim",
                                disposition="known-negative expectation: late client false-success without recovery" if cut
                                            else "healthy positive control")
                            paired_usage[cut] = outcome["usage"]
                            self.assertFalse(result.timed_out)
                            self.assertTrue(result.daemon and result.daemon.unchanged)
                            self.assertEqual(relay.completion_errors(), [])
                            self.assertEqual(gateway.version, "8.0.33", "requalify late-terminal evidence for a changed gateway")
                            expected_start = {"stop_reason": None, "stop_sequence": None, "output_tokens": 0}
                            self.assertTrue(all(row["initial_fields"] == expected_start for row in upstream_rows))
                            self.assertTrue(all(row.get("reply_initial_fields") == [expected_start] for row in relay.rows))
                            self.assertEqual(upstream_rows[0]["stop_reason"], "tool_use" if tool else "end_turn")
                            order = ["message_start", "content_block_start", "content_block_delta", "content_block_stop", "message_delta"]
                            self.assertEqual(upstream_rows[0]["event_order"], order + ([] if cut else ["message_stop"]))
                            self.assertEqual(relay.rows[0]["reply_event_order"], order + ["error" if cut else "message_stop"])
                            self.assertEqual(relay.rows[0]["reply_stop_reasons"], ["tool_use" if tool else "end_turn"])
                            self.assertEqual(relay.rows[0]["reply_errors"], 1 if cut else 0)
                            self.assertEqual(relay.rows[0]["reply_stops"], 0 if cut else 1)
                            self.assertEqual(result.returncode, 0)
                            self.assertFalse(outcome.get("is_error"))
                            self.assertEqual(outcome.get("result"), "FIXTURE-FINAL")
                            self.assertEqual(calls, int(tool))
                            self.assertEqual(len(relay.rows), 2 if tool else 1, "unexpected recovery/retry or continuation")
                            self.assertEqual(len(upstream_rows), len(relay.rows))
                            self.assertTrue(all(row["status"] == 200 and row["upstream_hits"] == 1 for row in relay.rows))
                            self.assertEqual([row["tool_result_count"] for row in relay.rows], [0, 1] if tool else [0])
                            if tool:
                                self.assertEqual(relay.rows[1]["assistant_types"], ["tool_use"])
                                self.assertEqual(relay.rows[1]["reply_event_order"], order + ["message_stop"])
                                self.assertEqual(relay.rows[1]["reply_stop_reasons"], ["end_turn"])
                                self.assertEqual(relay.rows[1]["reply_errors"], 0)
                            turns = 2 if tool else 1
                            for key, value in {"input_tokens": 7, "output_tokens": 1, "cache_creation_input_tokens": 11,
                                               "cache_read_input_tokens": 13}.items():
                                self.assertEqual(outcome["usage"][key], value * turns)
                    finally:
                        gateway.close()
            with self.subTest(tool=tool, paired_usage=True):
                if skipped:  # the pair skips only when a run it pairs skipped; a run that failed fails it
                    self.skipTest(next(iter(skipped.values())))
                self.assertEqual(set(paired_usage), {False, True}, "missing paired client outcomes")
                self.assertEqual(paired_usage[False], paired_usage[True], "healthy/late client usage changed")

    def test_early_cut_streams_recover_with_watchdog_enabled(self):
        # The later cut after an end_turn delta is the separate late-terminal negative,
        # never counted as recovery here.
        for family, alias in (("claude-before-delta", "gwtest-cc-output-config-high"),
                              ("codex", "gwtest-codex-reasoning-effort-high"),
                              ("lan", "gwtest-compat-plain")):
            with self.subTest(family=family), tempfile.TemporaryDirectory(prefix="gwtest-v8-cut-") as temporary:
                count = [0]
                upstream_rows = []
                def respond(route, body, raw, _headers):
                    count[0] += 1
                    status, kind, data = harness.fake_reply(route, body, raw)
                    if family.startswith("claude") and body.get("stream"):
                        # Keep the shared responder unchanged. This control
                        # owns a conformant start and leaves its terminal usage
                        # and completed content untouched before making the cut.
                        frames = harness.Reply(status, {}, data).events()
                        self.assertEqual(frames[0]["type"], "message_start")
                        frames[0]["message"] = initial_message(frames[0]["message"])
                        data = b"".join(harness.sse_frame(frame["type"], frame) for frame in frames)
                    if count[0] == 1:
                        if family.startswith("claude"):
                            terminal = b"message_delta" if family == "claude-before-delta" else b"message_stop"
                            data = data.split(b"event: " + terminal, 1)[0]
                        elif family == "codex":
                            data = b""  # clean close before the first provider payload
                        else:
                            chunks = [json.loads(frame[6:]) for frame in data.split(b"\n\n")
                                      if frame.startswith(b"data: {")]
                            data = b"".join(b"data: " + json.dumps(chunk).encode() + b"\n\n"
                                            for chunk in chunks if chunk["choices"][0]["finish_reason"] is None)
                    frames = [json.loads(line[6:]) for line in data.splitlines() if line.startswith(b"data: {")]
                    upstream_rows.append({"attempt": count[0], "stream": bool(body.get("stream")),
                        "bytes": len(data), "event_types": [frame.get("type", "chat-chunk") for frame in frames],
                        "initial_fields": [initial_fields(frame["message"]) for frame in frames
                                           if frame.get("type") == "message_start"],
                        "stop_reasons": [frame["delta"].get("stop_reason") for frame in frames
                                         if frame.get("type") == "message_delta"],
                        "finish_reasons": [choice.get("finish_reason") for frame in frames
                                           for choice in frame.get("choices", [])]})
                    return status, kind, data

                gateway = harness.GatewayHarness(upstream_factory=lambda path: harness.FakeUpstream(path, respond))
                try:
                    root = Path(temporary)
                    environ = {"HOME": str(root / "ambient"), "PATH": "/usr/bin:/bin"}
                    fixture = probe.build_fixture(root / "client", environ=environ)
                    probe.seed_fixture_project_trust(fixture, fixture.project_dir)
                    settings = probe.write_fixture_settings(fixture, "scope/watchdog.json", {"env": {
                        "CLAUDE_CODE_RETRY_WATCHDOG": "1", "CLAUDE_CODE_RETRY_WATCHDOG_MAX_WAIT_MS": "10000",
                        "CLAUDE_CODE_RETRY_WATCHDOG_MAX_USAGE_LIMIT_WAIT_MS": "10000"}})
                    with TrafficRelay(gateway) as relay:
                        result = probe.run_native(("-p", "fixture cut-stream", "--model", alias,
                            "--settings", str(settings), "--output-format", "stream-json", "--verbose",
                            "--permission-mode", "default"), trusted=self.trusted, fixture=fixture,
                            provider=relay, allow_real=True, environ=environ, timeout=45)
                        events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
                        terminal = [event for event in events if event.get("type") == "result"]
                        retries = [{key: event[key] for key in ("attempt", "max_retries", "retry_delay_ms", "status_code")
                                    if isinstance(event.get(key), int)} for event in events
                                   if event.get("type") == "system" and event.get("subtype") == "api_retry"]
                        facts(gateway, "cut-stream-watchdog", family=family, upstream_requests=count[0],
                              client_requests=len(relay.rows), retries=retries, returncode=result.returncode,
                              timed_out=result.timed_out, final_errors=[event.get("is_error") for event in terminal],
                              gateway_replies=relay.rows, upstream_replies=upstream_rows)
                        self.assertEqual(result.returncode, 0)
                        self.assertFalse(result.timed_out)
                        self.assertTrue(result.daemon and result.daemon.unchanged)
                        self.assertEqual(relay.completion_errors(), [])
                        if family.startswith("claude"):
                            expected_start = [{"stop_reason": None, "stop_sequence": None, "output_tokens": 0}]
                            self.assertTrue(all(row["initial_fields"] == expected_start for row in upstream_rows))
                            self.assertTrue(all(row.get("reply_initial_fields") == expected_start for row in relay.rows))
                        self.assertEqual(len(terminal), 1)
                        self.assertFalse(terminal[0].get("is_error"))
                        self.assertGreaterEqual(count[0], 2, "cut stream was silently accepted without recovery")
                        self.assertLessEqual(count[0], 4, "unexpected retry amplification")
                finally:
                    gateway.close()
