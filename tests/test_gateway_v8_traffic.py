"""Traffic through the actual gateway and disposable upstreams only.

Cache counts are fixture values: this proves transport/replay, not provider cache
accuracy or a live qualification. Native runs use the pinned client and netns
harness; request bodies stay in memory and only bounded facts are printed.
"""
from __future__ import annotations

import base64
import copy
import gzip
import hashlib
import http.client
import json
import os
import shutil
import socket
import ssl
import struct
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
import zlib
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest import mock

from charmer import probe, render, state
from _catalog import SHIPPED_ROOT
from _tier import real_binary_gate
import _gateway_harness as harness
import test_gateway_hint_client as recognition
import test_openai_key_route as route
from test_gateway_contracts import reconstructed_blocks
from test_client_mcp_policy import _SERVER as MCP_COUNTER_SERVER

try:
    from compression import zstd
except ImportError:  # Python before 3.14: the zstd encodings are not exercised
    zstd = None

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
        self.assertEqual(self.version, "2.1.312", "requalify late-terminal evidence for a changed client")
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
                            # Re-measured on 8.0.45 with clients 2.1.311 and 2.1.312: the same known negative.
                            self.assertEqual(gateway.version, "8.0.45", "requalify late-terminal evidence for a changed gateway")
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


# ------------------------------------------------------- 8.0.44 re-pin rows
# Rendered compression, the Responses WebSocket key retirement, the commentary
# replay marker, the capacity 429, the caller-scoped MCP alias and discovery
# (gateway/upstream-review.json records each disposition).

CODEX_ALIAS = "gwtest-codex-reasoning-effort-high"
CLAUDE_KEY_ALIAS = "gwtest-cc-output-config-high"
CLIENT_ENCODINGS = "gzip, deflate, br, zstd"  # what the managed client sends
COMMENTARY_MARKER = "cpa:commentary:v1"
UPSTREAM_ENCODINGS = ("identity", "gzip", "deflate") + (("zstd",) if zstd else ())


def revised(document, **changes):
    """A finalized document with ``changes`` and a fresh render sentinel."""
    document = copy.deepcopy(document)
    if document["openai-compatibility"][-1]["name"] != render.SENTINEL_NAME:
        raise AssertionError("document does not end with its render sentinel")
    document["openai-compatibility"].pop()
    document.update(changes)
    return render.finalize_document(document)


def reload_document(gateway, document, *, key=harness.FIXTURE_GATEWAY_TOKEN, deadline=15.0):
    """Write ``document`` as the running config (the watcher's hot reload) and
    wait until the gateway serves its sentinel to a holder of ``key``."""
    state.atomic_write(gateway.config, render.emit_yaml(document).encode())
    sentinel = render.document_sentinel(document)
    headers = {"Authorization": "Bearer " + key, "Host": f"127.0.0.1:{gateway.port}", "User-Agent": "gwtest-harness/1"}
    ends = time.monotonic() + deadline
    while time.monotonic() < ends:
        try:
            reply = harness.unix_request(gateway.socket, "GET", "/v1/models", headers=headers, timeout=1)
            if reply.status == 200 and sentinel in {row["id"] for row in reply.json().get("data", [])}:
                gateway.document = document
                return
        except (OSError, ValueError, http.client.HTTPException):
            pass
        time.sleep(0.05)
    raise AssertionError(gateway.diagnostic("the reloaded config was not served"))


def compression_unset(*args):
    """The fixture render with the compression settings left at upstream's
    defaults: present but empty, so the harness adds nothing and the gateway
    sees every enabled flag unset (response and WebSocket compression on)."""
    document, aliases, info = harness.build_document(*args)
    return revised(document, **{"response-compression": {}, "codex": {}}), aliases, info


def encoded(encoding, data):
    if encoding == "gzip":
        return gzip.compress(data)
    if encoding == "deflate":
        return zlib.compress(data)
    if encoding == "zstd":
        return zstd.compress(data)
    return data


class StreamDecoder:
    """Incremental decoder for a downstream Content-Encoding."""
    def __init__(self, encoding):
        self.encoding = encoding or "identity"
        if self.encoding == "gzip":
            self.inner = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif self.encoding == "deflate":
            self.inner = zlib.decompressobj()
        elif self.encoding == "zstd":
            if zstd is None:
                raise unittest.SkipTest("zstd decoding needs Python 3.14")
            self.inner = zstd.ZstdDecompressor()
        elif self.encoding == "identity":
            self.inner = None
        else:
            raise AssertionError("unexpected downstream encoding " + self.encoding)

    def feed(self, data):
        return self.inner.decompress(data) if self.inner is not None else data


def timed_messages_stream(gateway, body, *, accept=CLIENT_ENCODINGS, timeout=30):
    """POST /v1/messages and read the SSE reply as it arrives: the status, the
    headers and (seconds since the request, event name) per decoded event."""
    connection = probe.UnixHTTPConnection(gateway.socket, timeout=timeout)
    try:
        started = time.monotonic()
        connection.request("POST", "/v1/messages", body=json.dumps(body).encode(),
                           headers={**gateway.headers(), "Accept-Encoding": accept})
        response = connection.getresponse()
        headers = {key.lower(): value for key, value in response.getheaders()}
        decoder = StreamDecoder(headers.get("content-encoding"))
        text, arrivals = "", []
        while True:
            data = response.read1(65536)
            if not data:
                break
            at = time.monotonic() - started
            text += decoder.feed(data).decode()
            while "\n\n" in text:
                frame, text = text.split("\n\n", 1)
                kind = next((line[7:] for line in frame.splitlines() if line.startswith("event: ")), None)
                if kind:
                    arrivals.append((round(at, 3), kind))
        return response.status, headers, arrivals
    finally:
        connection.close()


PING_GAP, PINGS = 0.6, 4


def held_stream_reply(_route, body, _raw, _headers):
    """A Claude stream that holds its content behind ``PINGS`` ping events,
    one per ``PING_GAP`` seconds (the keepalive shape the client's watchdog
    relies on), written and flushed one event at a time."""
    payload = probe._message_payload([{"type": "text", "text": "FIXTURE-HELD"}], model=body["model"],
                                     stop_reason="end_turn", message_id="msg_v8_held")
    events = text_tool_events(payload)
    chunks = [(0.0, harness.sse_frame(*events[0]))]
    chunks += [(PING_GAP, harness.sse_frame("ping", {"type": "ping"})) for _ in range(PINGS)]
    chunks.append((PING_GAP, b"".join(harness.sse_frame(kind, value) for kind, value in events[1:])))
    return 200, "text/event-stream", harness.ChunkedBody(tuple(chunks))


class ResponsesSocket:
    """A minimal RFC 6455 client on the harness's ingress socket; it offers
    permessage-deflate so the gateway's negotiation is observable."""

    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, gateway, key, *, timeout=20):
        self.sock = probe.unix_connect(gateway.socket, timeout=timeout)
        nonce = base64.b64encode(os.urandom(16)).decode()
        request = ["GET /v1/responses HTTP/1.1", f"Host: 127.0.0.1:{gateway.port}", "Authorization: Bearer " + key,
                   "Connection: Upgrade", "Upgrade: websocket", "Sec-WebSocket-Version: 13",
                   "Sec-WebSocket-Key: " + nonce, "Sec-WebSocket-Extensions: permessage-deflate; client_max_window_bits",
                   "User-Agent: gwtest-harness/1"]
        self.sock.sendall(("\r\n".join(request) + "\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise AssertionError("the gateway closed the WebSocket upgrade")
            head += chunk
        head, self.buffer = head.split(b"\r\n\r\n", 1)
        status, *lines = head.decode("latin-1").split("\r\n")
        self.status = int(status.split()[1])
        self.headers = {name.strip().lower(): value.strip()
                        for name, value in (line.split(":", 1) for line in lines if ":" in line)}
        accept = base64.b64encode(hashlib.sha1((nonce + self.GUID).encode()).digest()).decode()
        if self.status == 101 and self.headers.get("sec-websocket-accept") != accept:
            raise AssertionError("invalid WebSocket upgrade")

    def _read(self, size):
        while len(self.buffer) < size:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("the gateway closed the WebSocket")
            self.buffer += chunk
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data

    def _send(self, opcode, payload):
        mask, size = os.urandom(4), len(payload)
        header = bytes([0x80 | opcode])
        if size < 126:
            header += bytes([0x80 | size])
        elif size < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", size)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", size)
        self.sock.sendall(header + mask + bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload)))

    def receive(self):
        """The next data message as JSON (pings answered); None on close."""
        message = b""
        while True:
            first, second = self._read(2)
            if first & 0x70:
                raise AssertionError("a reserved (compression) bit is set on a server frame")
            if second & 0x80:
                raise AssertionError("a server frame is masked")
            size = second & 0x7F
            if size == 126:
                size = struct.unpack("!H", self._read(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self._read(8))[0]
            payload, opcode = self._read(size), first & 0x0F
            if opcode == 0x9:
                self._send(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x8:
                return None
            message += payload
            if first & 0x80:
                return json.loads(message)

    def turn(self, alias, text):
        """One response.create turn: its events up to the terminal one."""
        self._send(0x1, json.dumps({"type": "response.create", "model": alias, "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}]}).encode())
        events = []
        while True:
            event = self.receive()
            if event is None:
                return events + [{"type": "close"}]
            events.append(event)
            if event.get("type") in ("response.completed", "response.failed", "response.incomplete", "error"):
                return events

    def close(self):
        try:
            self._send(0x8, struct.pack("!H", 1000))
        except OSError:
            pass
        self.sock.close()


class RenderedCompressionTrafficTests(unittest.TestCase):
    """The rendered compression settings (gateway.json cliproxy_static), against
    a control that leaves them at upstream's defaults."""

    def test_codex_responses_decode_every_upstream_encoding_and_stay_plain_downstream(self):
        seen = []
        def respond(route_path, body, raw, headers):
            seen.append({"accept_encoding": headers.get("accept-encoding"),
                         "content_encoding": headers.get("content-encoding")})
            status, kind, data = harness.fake_reply(route_path, body, raw)
            marker = next((name for name in UPSTREAM_ENCODINGS if f"gwtest-encoding-{name}".encode() in raw), "identity")
            if marker == "identity":
                return status, kind, data
            return status, kind, encoded(marker, data), {"Content-Encoding": marker}

        gateway = harness.GatewayHarness(upstream_factory=lambda path: harness.FakeUpstream(path, respond))
        self.addCleanup(gateway.close)
        self.assertEqual(gateway.document["response-compression"], {"enabled": False})
        self.assertEqual(gateway.document["codex"], {"websocket-compression": False, "upstream-request-compression": False})
        rows = []
        for encoding in UPSTREAM_ENCODINGS:
            for stream in (False, True):
                with self.subTest(encoding=encoding, stream=stream):
                    reply = gateway.request(CODEX_ALIAS, stream=stream, headers={"Accept-Encoding": CLIENT_ENCODINGS},
                                            body={"system": f"gwtest-encoding-{encoding}"})
                    self.assertEqual(reply.status, 200, reply.raw[:300])
                    self.assertEqual(len(reply.hits), 1)
                    self.assertNotIn("content-encoding", reply.headers)
                    text = ("".join(block.get("text", "") for block in reconstructed_blocks(reply)) if stream
                            else "".join(block.get("text", "") for block in reply.json()["content"]))
                    self.assertEqual(text, "FAKE_OK")
                    rows.append({"upstream_encoding": encoding, "stream": stream, "status": reply.status,
                                 "downstream_encoding": reply.headers.get("content-encoding", "identity")})
        # Every Codex request advertises the shared decoder's encodings and is
        # itself sent plain (upstream-request-compression false).
        self.assertEqual({row["accept_encoding"] for row in seen}, {"gzip, deflate, br, zstd"})
        self.assertEqual({row["content_encoding"] for row in seen}, {None})
        facts(gateway, "codex-upstream-encodings", rows=rows, upstream_request_headers=seen[0],
              brotli="not exercised (no stdlib encoder)")

    def test_held_stream_timing_and_websocket_negotiation_against_upstream_defaults(self):
        rows = {}
        for rendered in (True, False):
            with self.subTest(rendered=rendered):
                gateway = harness.GatewayHarness(document_factory=None if rendered else compression_unset,
                                                 upstream_factory=lambda path: harness.FakeUpstream(path, held_stream_reply))
                try:
                    status, headers, arrivals = timed_messages_stream(
                        gateway, request_body(CLAUDE_KEY_ALIAS, stream=True, max_tokens=64))
                    self.assertEqual(status, 200)
                    kinds = [kind for _at, kind in arrivals]
                    self.assertEqual(kinds.count("ping"), PINGS)
                    self.assertEqual(kinds[0], "message_start")
                    self.assertEqual(kinds[-1], "message_stop")
                    pings = [at for at, kind in arrivals if kind == "ping"]
                    connection = ResponsesSocket(gateway, harness.FIXTURE_GATEWAY_TOKEN)
                    try:
                        self.assertEqual(connection.status, 101)
                        extension = connection.headers.get("sec-websocket-extensions")
                    finally:
                        connection.close()
                    rows[rendered] = {"downstream_encoding": headers.get("content-encoding", "identity"),
                                      "arrivals": arrivals, "websocket_extension": extension}
                    if rendered:
                        # Off as rendered: plain bytes, every ping delivered when the
                        # upstream wrote it (the stream is never held back).
                        self.assertNotIn("content-encoding", headers)
                        self.assertIsNone(extension)
                        self.assertLess(arrivals[0][0], PING_GAP)
                        for index, at in enumerate(pings, start=1):
                            self.assertAlmostEqual(at - arrivals[0][0], index * PING_GAP, delta=PING_GAP / 2)
                    else:
                        # The discriminator: unset, upstream compresses the client's
                        # stream and negotiates WebSocket compression.
                        self.assertIn(headers.get("content-encoding"), {"zstd", "gzip"})
                        self.assertIsNotNone(extension)
                        self.assertIn("permessage-deflate", extension)
                    facts(gateway, "held-stream-compression", rendered=rendered, **rows[rendered])
                finally:
                    gateway.close()


class ResponsesWebsocketKeyRetirementTests(unittest.TestCase):
    def test_a_retired_key_is_refused_on_the_socket_it_opened(self):
        gateway = harness.GatewayHarness()
        self.addCleanup(gateway.close)
        old, new = harness.FIXTURE_GATEWAY_TOKEN, "gwtest-rotated-" + uuid.uuid4().hex
        self.assertEqual(gateway.document["api-keys"], [old])
        rows = []
        connection = ResponsesSocket(gateway, old)
        self.addCleanup(connection.close)
        self.assertEqual(connection.status, 101)
        self.assertNotIn("sec-websocket-extensions", connection.headers)

        def turn(name, socket_=connection):
            before = len(gateway.upstream.hits)
            events = socket_.turn(CODEX_ALIAS, f"gwtest-ws-{name}")
            terminal = events[-1]
            rows.append({"turn": name, "terminal": terminal.get("type"), "status": terminal.get("status"),
                         "events": len(events), "upstream_requests": len(gateway.upstream.hits) - before})
            return terminal

        self.assertEqual(turn("before-rotation")["type"], "response.completed")
        # Rotation as the product renders it: (previous, current), then current alone.
        reload_document(gateway, revised(gateway.document, **{"api-keys": [old, new]}), key=new)
        self.assertEqual(turn("overlap")["type"], "response.completed")
        reload_document(gateway, revised(gateway.document, **{"api-keys": [new]}), key=new)
        retired_http = harness.unix_request(gateway.socket, "GET", "/v1/models", headers={
            "Authorization": "Bearer " + old, "Host": f"127.0.0.1:{gateway.port}"}).status
        self.assertEqual(retired_http, 401)
        refused = turn("after-retirement")
        self.assertEqual(refused["type"], "error")
        self.assertEqual(refused["status"], 401)
        self.assertEqual(rows[-1]["upstream_requests"], 0)
        fresh = ResponsesSocket(gateway, new)
        try:
            self.assertEqual(fresh.status, 101)
            self.assertEqual(turn("new-socket-current-key", fresh)["type"], "response.completed")
        finally:
            fresh.close()
        self.assertEqual([row["upstream_requests"] for row in rows], [1, 1, 0, 1])
        facts(gateway, "responses-websocket-key-retirement", rows=rows, retired_key_http_status=retired_http,
              same_socket=True, websocket_extension=None)


def codex_commentary_reply(model, commentary, final):
    """A Codex Responses stream: an optional commentary message, then the final answer."""
    items = [{"id": "msg_final", "type": "message", "role": "assistant", "status": "completed",
              "phase": "final_answer", "content": [{"type": "output_text", "text": final, "annotations": []}]}]
    if commentary:
        items.insert(0, {"id": "msg_commentary", "type": "message", "role": "assistant", "status": "completed",
                         "phase": "commentary", "content": [{"type": "output_text", "text": commentary, "annotations": []}]})
    response = {"id": "resp_commentary", "object": "response", "model": model, "status": "completed",
                "output": items, "usage": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}}
    frames = [("response.created", {"response": {**response, "status": "in_progress", "output": []}})]
    for index, item in enumerate(items):
        part, text = item["content"][0], item["content"][0]["text"]
        frames += [("response.output_item.added", {"output_index": index, "item": {
                        **{key: value for key, value in item.items() if key != "content"}, "status": "in_progress", "content": []}}),
                   ("response.content_part.added", {"output_index": index, "item_id": item["id"], "content_index": 0,
                                                    "part": {"type": "output_text", "text": "", "annotations": []}}),
                   ("response.output_text.delta", {"output_index": index, "item_id": item["id"], "content_index": 0,
                                                   "delta": text}),
                   ("response.output_text.done", {"output_index": index, "item_id": item["id"], "content_index": 0,
                                                  "text": text}),
                   ("response.content_part.done", {"output_index": index, "item_id": item["id"], "content_index": 0,
                                                   "part": part}),
                   ("response.output_item.done", {"output_index": index, "item": item})]
    frames.append(("response.completed", {"response": response}))
    return 200, "text/event-stream", b"".join(harness.sse_frame(kind, {"type": kind, **data}) for kind, data in frames)


def commentary_inputs(body):
    """The replayed assistant messages a Codex request carries: (phase, text)."""
    return [(item.get("phase"), "".join(part.get("text", "") for part in item.get("content", []) if isinstance(part, dict)))
            for item in body.get("input", []) if isinstance(item, dict)
            and item.get("type") == "message" and item.get("role") == "assistant"]


def assistant_blocks(body):
    return [block for message in body.get("messages", []) if message.get("role") == "assistant"
            and isinstance(message.get("content"), list) for block in message["content"] if isinstance(block, dict)]


class CommentaryReplayTests(unittest.TestCase):
    """531721bf: Codex commentary reaches the client as thinking carrying the
    gateway's internal marker and is restored on replay; the marker itself
    never reaches an upstream."""

    @classmethod
    def setUpClass(cls):
        gate = real_binary_gate(SHIPPED_ROOT / "catalog/native-contract.json", what="v8 commentary replay")
        if gate.boundary:
            raise unittest.SkipTest(gate.boundary)
        cls.trusted, cls.version = gate.trusted, gate.version

    def journey(self, account):
        root = Path(tempfile.mkdtemp(prefix="gwtest-v8-commentary-"))
        self.addCleanup(shutil.rmtree, root, True)
        commentary = "COMMENTARY-" + uuid.uuid4().hex[:8]
        upstream = []

        def respond(path, body, raw, _headers):
            upstream.append({"path": path, "raw": raw, "body": body})
            if path.endswith("/responses"):
                first = not any(row["path"].endswith("/responses") for row in upstream[:-1])
                return codex_commentary_reply(body.get("model", "gwtest-wire"), commentary if first else "",
                                              "FINAL-FIRST" if first else "FINAL-REPLAY")
            if path.endswith("/v1/messages"):
                status, data, kind = message_reply(body)
                return status, kind, data
            return harness.fake_reply(path, body, raw)

        if account:
            gateway = route.account_gateway(root, respond=respond)
        else:
            gateway = harness.GatewayHarness(document_factory=route.gateway_document,
                                             upstream_factory=lambda path: harness.FakeUpstream(path, respond))
        self.addCleanup(gateway.close)
        codex_alias = sorted(gateway.info["reviewed"])[0]
        claude_alias = gateway.document["claude-api-key"][0]["models"][0]["alias"]
        environ = {"HOME": str(root / "ambient/home"), "PATH": "/usr/bin:/bin"}
        fixture = probe.build_fixture(root / "client", environ=environ)
        probe.seed_fixture_project_trust(fixture, fixture.project_dir)
        session = str(uuid.uuid4())
        runs = []
        with TrafficRelay(gateway) as relay:
            for index, model in enumerate((codex_alias, codex_alias, claude_alias)):
                result = probe.run_native(("-p", f"fixture commentary turn {index}", "--model", model,
                    "--output-format", "json", "--permission-mode", "default",
                    "--session-id" if index == 0 else "--resume", session),
                    trusted=self.trusted, fixture=fixture, provider=relay, allow_real=True, environ=environ, timeout=120)
                outcome = json.loads(result.stdout or "{}")
                runs.append({"model": model, "returncode": result.returncode, "timed_out": result.timed_out,
                             "is_error": outcome.get("is_error"), "result": str(outcome.get("result"))[:80]})
                self.assertEqual(result.returncode, 0, runs[-1])
                self.assertFalse(result.timed_out)
                self.assertTrue(result.daemon and result.daemon.unchanged)
            self.assertEqual(relay.completion_errors(), [])
            rows = list(relay.rows)
        return gateway, commentary, upstream, runs, rows, claude_alias

    def check_journey(self, account):
        gateway, commentary, upstream, runs, relay_rows, claude_alias = self.journey(account)
        # The marker never reaches any upstream request, on any route.
        self.assertEqual([row["path"] for row in upstream if COMMENTARY_MARKER.encode() in row["raw"]], [])
        codex = [row for row in upstream if row["path"].endswith("/responses")]
        claude = [row for row in upstream if row["path"].endswith("/v1/messages") and row["body"].get("messages")]
        self.assertTrue(any(row["reply_thinking"] for row in relay_rows),
                        "the commentary did not reach the client as thinking")
        replayed = [commentary_inputs(row["body"]) for row in codex if commentary_inputs(row["body"])]
        # Same route: the commentary returns as an assistant commentary message.
        self.assertTrue(replayed and all(("commentary", commentary) in inputs for inputs in replayed), replayed)
        self.assertTrue(claude, "the cross-family turn did not reach the Claude route")
        blocks = assistant_blocks(claude[-1]["body"])
        # What the client itself replayed on the cross-family turn: the gateway
        # can only restore a block the client sends.
        sent = sorted({kind for row in relay_rows if row["model"] == claude_alias for kind in row["assistant_types"]})
        cross = {"client_assistant_types": sent,
                 "text": commentary in [block.get("text") for block in blocks if block.get("type") == "text"],
                 "thinking": [block.get("type") for block in blocks if block.get("type") in ("thinking", "redacted_thinking")]}
        # Cross-family: never thinking with the marker; plain assistant text
        # whenever the client replays the marked block.
        self.assertEqual(cross["thinking"], [])
        self.assertEqual(cross["text"], "thinking" in sent, (cross, relay_rows))
        facts(gateway, "commentary-replay", route="account" if account else "key", client_version=self.version,
              client_sha256=self.trusted.sha256, runs=runs, codex_requests=len(codex),
              codex_replays=[len(inputs) for inputs in replayed], commentary_restored=True,
              cross_family=cross, marker_upstream=False, upstream_requests=len(upstream))

    def test_key_route_commentary_survives_replay_and_crosses_family_as_text(self):
        self.check_journey(account=False)

    def test_account_route_commentary_survives_replay_and_crosses_family_as_text(self):
        self.check_journey(account=True)


class CommentaryMarkerControlTests(unittest.TestCase):
    """Without the client: a genuine signature stays a signed thinking block,
    and a forged marker buys plain text only (the marker is forgeable by
    design and is no credential)."""

    def test_genuine_signature_kept_and_forged_marker_reduced_to_text(self):
        captured = []
        def respond(route_path, body, raw, headers):
            captured.append((route_path, raw, body))
            return direct_reply(route_path, body, raw, headers)

        gateway = harness.GatewayHarness(document_factory=recognition.native_document,
                                         upstream_factory=lambda path: harness.FakeUpstream(path, respond))
        self.addCleanup(gateway.close)
        rows = []
        cases = {
            "genuine": [{"type": "thinking", "thinking": "fixture reasoning", "signature": SIGNATURE},
                        {"type": "text", "text": "prior response"}],
            "forged": [{"type": "thinking", "thinking": "FORGED-COMMENTARY", "signature": COMMENTARY_MARKER},
                       {"type": "text", "text": "prior response"}],
            "forged-empty": [{"type": "thinking", "thinking": "", "signature": COMMENTARY_MARKER},
                             {"type": "text", "text": "prior response"}]}
        expected = {"genuine": cases["genuine"],
                    "forged": [{"type": "text", "text": "FORGED-COMMENTARY"}, {"type": "text", "text": "prior response"}],
                    "forged-empty": [{"type": "text", "text": "prior response"}]}
        for name, content in cases.items():
            with self.subTest(case=name):
                history = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": content},
                           {"role": "user", "content": "next"}]
                reply = post(gateway, request_body(thinking={"type": "adaptive"}, messages=history))
                self.assertEqual(reply.status, 200)
                _path, raw, sent = captured[-1]
                self.assertNotIn(COMMENTARY_MARKER.encode(), raw)
                self.assertEqual(sent["messages"][1]["content"], expected[name])
                rows.append({"case": name, "route": "claude",
                             "upstream_assistant": [block["type"] for block in sent["messages"][1]["content"]]})
        # On a Codex route (the fixture render's key route) the forged marker
        # becomes a commentary message, text only.
        codex = harness.GatewayHarness()
        self.addCleanup(codex.close)
        reply = codex.request(CODEX_ALIAS, body={"messages": [
            {"role": "user", "content": "earlier"}, {"role": "assistant", "content": cases["forged"]}]})
        self.assertEqual(reply.status, 200)
        self.assertEqual(len(reply.hits), 1)
        self.assertNotIn(COMMENTARY_MARKER.encode(), reply.hits[0].raw)
        inputs = commentary_inputs(reply.hits[0].body)
        self.assertIn(("commentary", "FORGED-COMMENTARY"), inputs)
        self.assertFalse([item for item in reply.hits[0].body.get("input", []) if item.get("type") == "reasoning"])
        rows.append({"case": "forged", "route": "codex", "upstream_assistant": inputs})
        facts(gateway, "commentary-marker-controls", rows=rows)


TURN_LIMIT = {"error": {"type": "invalid_request_error", "code": "turn_rate_limit_exceeded",
                        "message": "Codex is at capacity. Please try again later."}}


def capacity_reply(path, body, raw, _headers):
    if path.endswith("/responses"):
        return 429, "application/json", json.dumps(TURN_LIMIT).encode(), {"Retry-After": "300"}
    return harness.fake_reply(path, body, raw)


class CodexCapacityRejectionTests(unittest.TestCase):
    """#7017: the ChatGPT Codex capacity 429 with Retry-After under the
    rendered request-retry 0 and disable-cooling true. Measured on 8.0.45:
    the client receives the provider's Retry-After on both OpenAI routes
    (8.0.44 sent none); the disposition is in gateway/upstream-review.json."""

    def observe(self, gateway, alias, route_name):
        rows = []
        for attempt, stream in enumerate((False, True, False)):
            before = len(gateway.upstream.hits)
            reply = gateway.request(alias, stream=stream)
            hits = len(gateway.upstream.hits) - before
            error = {}
            if "text/event-stream" in reply.headers.get("content-type", ""):
                error = next((event.get("error", {}) for event in reply.events() if event.get("type") == "error"), {})
            elif reply.raw:
                document = reply.json()
                error = document.get("error", {}) if isinstance(document, dict) else {}
            rows.append({"route": route_name, "attempt": attempt + 1, "stream": stream, "status": reply.status,
                         "retry_after": reply.headers.get("retry-after"), "error_type": error.get("type"),
                         "upstream_requests": hits})
            # request-retry 0 and no cooldown: one upstream request per client
            # request, and the next request still reaches the upstream.
            self.assertEqual(hits, 1, rows[-1])
            self.assertGreaterEqual(reply.status, 400)
        return rows

    def test_capacity_rejection_reaches_the_client_once_per_request(self):
        gateway = harness.GatewayHarness(upstream_factory=lambda path: harness.FakeUpstream(path, capacity_reply))
        try:
            rows = self.observe(gateway, CODEX_ALIAS, "key")
        finally:
            gateway.close()
        root = Path(tempfile.mkdtemp(prefix="gwtest-v8-capacity-"))
        self.addCleanup(shutil.rmtree, root, True)
        account = route.account_gateway(root, respond=capacity_reply)
        self.addCleanup(account.close)
        rows += self.observe(account, sorted(account.info["reviewed"])[0], "account")
        facts(account, "codex-capacity-429", upstream_status=429, upstream_retry_after="300",
              upstream_error=TURN_LIMIT["error"]["code"], rows=rows)
        # The provider's delay reaches the client on both routes, streamed or
        # not (on the API-key route through patch 20's classification).
        self.assertEqual([row["retry_after"] for row in rows], ["300"] * len(rows))
        self.assertEqual({row["status"] for row in rows}, {429})


class CallerScopedAliasTests(unittest.TestCase):
    """e48d780f: on the Claude OAuth route a non-MCP client tool is sent as an
    MCP tool whose server component derives from the caller scope (a hash of
    the gateway key since 8.0.43, previously the key itself): stable per key,
    distinct per key, never the key, and restored in the reply."""

    def test_claude_oauth_tool_alias_is_caller_scoped(self):
        if shutil.which("openssl", path="/usr/bin:/bin") is None:
            raise unittest.SkipTest("BOUNDARY: openssl is needed for the fixture TLS upstream")
        sent = []
        def respond(route_path, body):
            if route_path.endswith("/count_tokens"):
                return 200, b'{"input_tokens":1}', "application/json"
            names = [tool["name"] for tool in body.get("tools", [])]
            sent.append(names)
            content = [{"type": "tool_use", "id": "toolu_v8_alias", "name": names[0], "input": {}}]
            payload = probe._message_payload(content, model=body["model"], stop_reason="tool_use",
                                             message_id="msg_v8_alias")
            return 200, json.dumps(payload).encode(), "application/json"

        root = Path(tempfile.mkdtemp(prefix="gwtest-v8-alias-"))
        self.addCleanup(shutil.rmtree, root, True)
        gateway = recognition.start_oauth_gateway(root, respond)
        self.addCleanup(gateway.close)
        model = gateway.document["oauth-model-alias"]["claude"][0]["alias"]
        second = "gwtest-second-caller-" + uuid.uuid4().hex
        reload_document(gateway, revised(gateway.document, **{"api-keys": [harness.FIXTURE_GATEWAY_TOKEN, second]}))
        body = request_body(model, tools=[{"name": "gwtest_lookup", "description": "fixture",
                                           "input_schema": {"type": "object", "properties": {}}}])
        replies = []
        for key in (harness.FIXTURE_GATEWAY_TOKEN, harness.FIXTURE_GATEWAY_TOKEN, second):
            reply = harness.unix_request(gateway.socket, "POST", "/v1/messages", timeout=45,
                                         headers={**gateway.headers(), "Authorization": "Bearer " + key},
                                         body=json.dumps(body).encode())
            self.assertEqual(reply.status, 200, reply.raw[:300])
            replies.append([block.get("name") for block in reply.json()["content"] if block.get("type") == "tool_use"])
        aliases = [names[0] for names in sent]
        self.assertEqual(len(aliases), 3)
        self.assertTrue(all(alias.startswith("mcp__") and alias.endswith("gwtest_lookup") for alias in aliases), aliases)
        self.assertEqual(aliases[0], aliases[1], "one caller's alias changed between requests")
        self.assertNotEqual(aliases[0], aliases[2], "two callers share an alias")
        for alias in aliases:
            self.assertNotIn(harness.FIXTURE_GATEWAY_TOKEN, alias)
            self.assertNotIn(second, alias)
        self.assertEqual(replies, [["gwtest_lookup"]] * 3)
        facts(gateway, "claude-oauth-caller-scoped-alias", stable_per_caller=True, distinct_callers=True,
              restored_in_reply=True, alias_shape="mcp__<caller-scoped server>__gwtest_lookup")


class DiscoveryOffTests(unittest.TestCase):
    """Discovery (8a982a52, 11e5f67b) stays off: the schema pins it, the render
    states it, and the running gateway neither logs the advertiser nor holds a
    UDP socket in its namespace; enabling it is the discriminator."""

    def test_rendered_off_and_the_advertiser_never_runs(self):
        schema = json.loads((SHIPPED_ROOT / "schemas/gateway.schema.json").read_text())
        static_schema = schema["properties"]["gateway"]["properties"]["cliproxy_static"]["properties"]["discovery"]
        self.assertEqual(static_schema["properties"]["enabled"], {"const": False})
        rows = {}
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                factory = None
                if enabled:
                    def factory(*args):
                        document, aliases, info = harness.build_document(*args)
                        return revised(document, discovery={"enabled": True}), aliases, info
                gateway = harness.GatewayHarness(document_factory=factory)
                try:
                    self.assertEqual(gateway.document["discovery"], {"enabled": enabled})
                    self.assertIn(f"discovery:\n  enabled: {str(enabled).lower()}\n", gateway.config.read_text())
                    time.sleep(1.0)  # past the advertiser's start on a gateway that runs one
                    log = gateway.log_path.read_text(errors="replace")
                    lines = [line for line in log.splitlines() if "discovery:" in line]
                    udp = harness.listener_rows(gateway.process.pid, gateway.binary, udp=True)
                    rows[enabled] = {"discovery_log_lines": len(lines), "udp_sockets": udp}
                    if enabled:
                        self.assertTrue(lines or udp, "the discriminator saw no advertiser")
                    else:
                        self.assertEqual(lines, [])
                        self.assertEqual(udp, [])
                    facts(gateway, "discovery", enabled=enabled, **rows[enabled])
                finally:
                    gateway.close()


def refusal_reply(status, headers):
    """A Claude refusal (429 or 529) carrying the given response headers."""
    kind = "rate_limit_error" if status == 429 else "overloaded_error"
    body = json.dumps({"type": "error", "error": {"type": kind, "message": "fixture refusal"}}).encode()
    return status, "application/json", body, headers


class ShouldRetryHeaderTests(unittest.TestCase):
    """Whether a Claude upstream's x-should-retry (and Retry-After) reaches
    the client through the gateway as rendered (passthrough-headers false),
    on the Claude key and Claude OAuth routes. The client ends a turn on
    x-should-retry: false (client check do-not-retry); this row records what
    the gateway lets through. Observation of the rendered behaviour."""

    HEADERS = {"x-should-retry": "false", "retry-after": "7"}

    def test_claude_refusal_headers_through_the_gateway(self):
        rows = []
        for status in (429, 529):
            def respond(route_path, body, raw, headers, status=status):
                if route_path.endswith("/v1/messages"):
                    return refusal_reply(status, self.HEADERS)
                return harness.fake_reply(route_path, body, raw)

            gateway = harness.GatewayHarness(upstream_factory=lambda path: harness.FakeUpstream(path, respond))
            try:
                for stream in (False, True):
                    before = len(gateway.upstream.hits)
                    reply = post(gateway, request_body(CLAUDE_KEY_ALIAS, stream=stream))
                    rows.append({"route": "claude-key", "upstream_status": status, "stream": stream,
                                 "status": reply.status, "x_should_retry": reply.headers.get("x-should-retry"),
                                 "retry_after": reply.headers.get("retry-after"),
                                 "upstream_requests": len(gateway.upstream.hits) - before})
            finally:
                gateway.close()

        def oauth_respond(route_path, body):
            if route_path.endswith("/count_tokens"):
                return 200, b'{"input_tokens":1}', "application/json"
            status = 529 if "gwtest-overloaded" in json.dumps(body) else 429
            _status, kind, data, headers = refusal_reply(status, self.HEADERS)
            return status, data, kind, headers

        root = Path(tempfile.mkdtemp(prefix="gwtest-v8-should-retry-"))
        self.addCleanup(shutil.rmtree, root, True)
        oauth = recognition.start_oauth_gateway(root, oauth_respond)
        self.addCleanup(oauth.close)
        model = oauth.document["oauth-model-alias"]["claude"][0]["alias"]
        for status in (429, 529):
            for stream in (False, True):
                before = len(oauth.upstream.hits)
                text = "gwtest-overloaded" if status == 529 else "gwtest-rate-limited"
                reply = post(oauth, {**request_body(model, stream=stream),
                                     "messages": [{"role": "user", "content": text}]})
                rows.append({"route": "claude-oauth", "upstream_status": status, "stream": stream,
                             "status": reply.status, "x_should_retry": reply.headers.get("x-should-retry"),
                             "retry_after": reply.headers.get("retry-after"),
                             "upstream_requests": len(oauth.upstream.hits) - before})
        facts(oauth, "claude-should-retry-header", upstream_headers=self.HEADERS, rows=rows)
        for row in rows:
            # One upstream request per client request (request-retry 0), and
            # the refusal's status reaches the client.
            self.assertEqual(row["upstream_requests"], 1, row)
            self.assertEqual(row["status"], row["upstream_status"], row)
            # Measured on 8.0.45: with passthrough-headers false x-should-retry
            # still never reaches the client, so its do-not-retry rule never
            # sees the provider's answer (a known limit). The gateway now
            # answers every refusal with a Retry-After of its own choosing
            # (8 to 36 s in two runs against the provider's 7 s, on both
            # routes and in both modes; 8.0.44 sent none). A change is
            # reconciled deliberately, never just accepted.
            self.assertIsNone(row["x_should_retry"], row)
            self.assertRegex(row["retry_after"] or "", r"^[1-9][0-9]*$", row)


class HeaderReadTimeoutTests(unittest.TestCase):
    """A connection that starts a request and stops before the end of its
    headers is closed by the gateway as rendered (the HTTP server's header read
    deadline), so an idle client cannot hold it open. One connection, the
    harness's own ports."""

    BOUND = 15.0

    def test_unfinished_request_head_is_closed(self):
        gateway = harness.GatewayHarness()
        self.addCleanup(gateway.close)
        connection = probe.unix_connect(gateway.socket, timeout=self.BOUND)
        self.addCleanup(connection.close)
        started = time.monotonic()
        connection.sendall(f"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1:{gateway.port}\r\n".encode())
        closed, reply = False, b""
        while (remaining := self.BOUND - (time.monotonic() - started)) > 0:
            connection.settimeout(remaining)
            try:
                chunk = connection.recv(4096)
            except (socket.timeout, TimeoutError):
                break
            if not chunk:
                closed = True
                break
            reply += chunk
        elapsed = round(time.monotonic() - started, 3)
        facts(gateway, "unfinished-request-head", bound_seconds=self.BOUND, closed=closed, seconds=elapsed,
              reply_bytes=len(reply))
        self.assertTrue(closed, f"the connection was still open after {self.BOUND} s")
        self.assertLess(elapsed, self.BOUND)
        self.assertEqual(gateway.get("/healthz").status, 200)
