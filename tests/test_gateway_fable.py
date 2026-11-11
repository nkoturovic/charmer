"""Claude Fable 5.2 through the candidate gateway (SPEC 080 §1 G item 6).

Offline and host-only: the candidate CLIProxyAPI build runs in its loopback
namespace against fake upstreams only. The Claude OAuth pool reaches a fixture
TLS endpoint for api.anthropic.com through the reviewed ``proxy-url`` seam
(``test_gateway_hint_client.start_oauth_gateway``, a fixture OAuth file, the
shipped Claude-pool render); the direct-key and keyed Claude-compatible routes
reach the harness's fake upstream. No provider is called.

Rows (one test each): FA1 static registry, reload and a conflicting overlay;
FA2 the 2.1.321+ native fingerprint and beta order; FA3 preserved-thinking
binding and per-turn controls through the cache-retention strip; FA4
``x-app: cli-bg`` treated as native; FA5 Kimi and OpenRouter get no
first-party-only field or header; FA6 the literal model id end to end. FA7
(the Fable lists of the hint-client and v8-traffic modules) and FA8 (the
registry snapshot row) live in those modules.

Request shapes are fixtures modelled on the 2.1.321 captures upstream records
in ``claude_executor_cli_2321_test.go`` (b4e7dbd2). A fake upstream cannot show
that Anthropic accepts them, and the signature fixture proves no real binding.
"""
from __future__ import annotations

import contextlib
import copy
import json
import os
import shutil
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from charmer import catalog, operator, render
from _catalog import SHIPPED_ROOT, uses_shipped_catalog
import _gateway_harness as harness
import test_gateway_hint_client as recognition
import test_gateway_patches as patches
import test_gateway_v8_traffic as traffic

MODULE = "tests.test_gateway_fable"
WIRE = "claude-fable-5-2"
# The Fable 5.2 set (19a0a854, b4e7dbd2, 6db7d4b8) first ships in 8.0.63.
FIRST_GATEWAY = (8, 0, 63)
BASELINE = "2.1.321"
SIGNATURE = traffic.SIGNATURE
BINDING_BETA = traffic.BINDING_BETA
OAUTH_BETA = "oauth-2025-04-20"
ADVISOR_BETA = "advisor-tool-2026-03-01"
# Native Claude Code 2.1.321 main-thread Anthropic-Beta on Fable 5.2 (OAuth,
# auto mode), as b4e7dbd2 records it (claudeCode2321MainBetas).
CLI_2321_MAIN_BETAS = (
    "claude-code-20250219,oauth-2025-04-20,interleaved-thinking-2025-05-14,"
    "context-management-2025-06-27,prompt-caching-scope-2026-01-05,per-turn-control-2026-07-01,"
    "mid-conversation-tool-changes-2026-07-01,inline-tools-2026-09-15,advisor-tool-2026-03-01,"
    "thinking-token-count-2026-05-13,advanced-tool-use-2025-11-20,mid-conversation-system-clear-at-2026-08-21,"
    "effort-2025-11-24,dangerous-tool-use-2026-09-03,thinking-binding-controls-2026-08-01,"
    "thinking-display-updates-2026-08-18,afk-mode-2026-01-31,extended-cache-ttl-2025-04-11,"
    "cache-diagnosis-2026-04-07,message-threads-2026-08-12")
# The gateway's measured count_tokens profile (claudeCountTokensBetas).
CLI_COUNT_BETAS = ("claude-code-20250219,interleaved-thinking-2025-05-14,context-management-2025-06-27,"
                   "token-counting-2024-11-01")
# What b4e7dbd2 sends for a caller it does not recognise as native.
BASELINE_FINGERPRINT = {"user-agent": f"claude-cli/{BASELINE} (external, cli)", "x-app": "cli",
                        "x-stainless-package-version": "0.128.0", "x-stainless-runtime-version": "v26.12.1"}
# Headers the gateway owns on every upstream hop (transport and route auth).
TRANSPORT_HEADERS = frozenset({"accept", "accept-encoding", "anthropic-version", "connection", "content-length",
                               "content-type", "host", "x-api-key", "authorization"})
# Body fields only the first-party (cloaked) path injects.
FIRST_PARTY_FIELDS = ("context_management", "diagnostics")


def setUpModule():
    if not os.environ.get(harness.BINARY_ENV):
        harness.boundary("BOUNDARY: the Fable 5.2 rows need " + harness.BINARY_ENV + " set to the candidate build")


def tearDownModule():
    harness.EVIDENCE.finalize_module(MODULE)


def record(row, tally, **fields):
    """Evidence for a row whose every case passed (a failed subTest lets the
    method continue, so the tally, not reaching this line, decides)."""
    if tally and all(tally):
        harness.EVIDENCE.record(MODULE, "fable", {"row": row, "status": "passed", "cases": len(tally), **fields})


@contextlib.contextmanager
def case(test, tally, **params):
    """A subTest that counts as passed only when its whole body completed."""
    tally.append(False)
    with test.subTest(**params):
        yield
        tally[-1] = True


def facts(gateway, row, **values):
    """Bounded metadata only, like the v8 traffic module's facts."""
    print("gateway fable 5.2: " + json.dumps({"case": row,
          "gateway": harness.binary_identity(gateway.binary, gateway.version), **values}, sort_keys=True))


def betas(value):
    return [beta for beta in (value or "").split(",") if beta]


def without(value, *names):
    return ",".join(beta for beta in betas(value) if beta not in names)


def fable_docs():
    """The shipped catalog with SPEC §1 M item 1 applied in memory: the
    ``fable`` line on wire claude-fable-5-2 (selector with ``[1m]``, no
    registry overlay) and its passthrough route. A no-op once M has moved
    the shipped line; nothing else is edited."""
    docs = copy.deepcopy(catalog.load_catalog(SHIPPED_ROOT).docs)
    anthropic = docs["providers"]["providers"]["anthropic"]
    if not any(route["name"] == WIRE for route in anthropic["passthrough_routes"]):
        anthropic["passthrough_routes"].append({"fork": True, "name": WIRE})
    for key in ("models", "models-v2"):
        line = docs[key]["models"]["fable"]
        line.update(wire_model=WIRE, selector=WIRE + "[1m]", generation="5.2", registry_overlay=None)
    return docs


def fable_pool(docs):
    """The product render's Claude OAuth pool sections for ``docs``: its
    served alias table and the registry overlay (as shipped_claude_pool)."""
    plan = operator.render_plan(docs, operator.empty_layer(), None)
    gateway_doc = copy.deepcopy(docs["gateway"])
    gateway_doc["gateway"]["base_url"] = "http://127.0.0.1:1"
    document, _available, _unavailable, _info = render.build_config_document(
        gateway_doc, plan.docs["providers"]["providers"], plan.docs["models-v2"]["models"],
        home=Path("/nonexistent/gwtest-fable"), gateway_token=harness.FIXTURE_GATEWAY_TOKEN,
        resolve_secret=lambda _name: "dummy-gwtest-fable", continuity={}, oauth_overlay=plan.overlay,
        overlay_static=None)
    pool = {"oauth-model-alias": {"claude": copy.deepcopy(document["oauth-model-alias"].get("claude", []))}}
    overlay = (document.get(render.OVERLAY_KEY) or {}).get("claude")
    if overlay:
        pool[render.OVERLAY_KEY] = {"claude": copy.deepcopy(overlay)}
    return pool


def overlay_names(document):
    return {entry["name"] for entry in (document.get(render.OVERLAY_KEY) or {}).get("claude", [])}


def oauth_reply(route, body):
    if route.endswith("/count_tokens"):
        return 200, b'{"input_tokens":1}', "application/json"
    return recognition.native_reply(body, [{"type": "text", "text": "FABLE-OK"}], "end_turn")


def oauth_gateway(test, pool):
    """The candidate gateway on the Claude OAuth pool path with ``pool`` as
    its rendered Claude-pool sections."""
    if shutil.which("openssl", path="/usr/bin:/bin") is None:
        harness.boundary("BOUNDARY: openssl is needed for the fixture TLS upstream")
    root = Path(tempfile.mkdtemp(prefix="gwtest-fable-"))
    test.addCleanup(shutil.rmtree, root, True)
    with mock.patch.object(recognition, "shipped_claude_pool", return_value=pool):
        gateway = recognition.start_oauth_gateway(root, oauth_reply)
    test.addCleanup(gateway.close)
    check_version(test, gateway)
    return gateway


def check_version(test, gateway):
    test.assertGreaterEqual(tuple(map(int, gateway.version.split("."))), FIRST_GATEWAY,
                            "Fable 5.2 support first ships in CLIProxyAPI 8.0.63")


def user_id():
    return json.dumps({"device_id": "c" * 64, "account_uuid": "", "session_id": str(uuid.uuid4())})


def native_headers(gateway, *, version=BASELINE, extensions=(), x_app="cli", beta=None, count=False):
    """A managed native client's headers: it authenticates to the gateway
    with the gateway key, so it never declares the OAuth beta itself."""
    if beta is None:
        beta = without(CLI_COUNT_BETAS if count else CLI_2321_MAIN_BETAS, OAUTH_BETA, ADVISOR_BETA)
    headers = {"User-Agent": f"claude-cli/{version} (" + ", ".join(("external", "cli", *extensions)) + ")",
               "X-App": x_app, "Anthropic-Beta": beta, "Anthropic-Version": "2023-06-01",
               "Anthropic-Dangerous-Direct-Browser-Access": "true", "Content-Type": "application/json",
               "Accept": "application/json", "Accept-Encoding": "gzip, deflate, br, zstd",
               "X-Stainless-Lang": "js", "X-Stainless-Runtime": "node", "X-Stainless-Runtime-Version": "v26.12.1",
               "X-Stainless-Package-Version": "0.128.0", "X-Stainless-Os": "Linux", "X-Stainless-Arch": "x64",
               "X-Stainless-Retry-Count": "0", "X-Claude-Code-Session-Id": str(uuid.uuid4()),
               "Authorization": "Bearer " + harness.FIXTURE_GATEWAY_TOKEN, "Host": f"127.0.0.1:{gateway.port}"}
    if not count:
        headers["X-Stainless-Timeout"] = "600"
    return headers


def history():
    """A signed-thinking replay with a per-turn effort directive (role=system
    output_config.effort) and a per-turn timing control (6db7d4b8 shapes)."""
    return [{"role": "user", "content": "earlier fable turn"},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "fixture reasoning", "signature": SIGNATURE},
                {"type": "text", "text": "prior response"}]},
            {"role": "system", "content": [{"type": "text", "text": "per-turn directive"}],
             "output_config": {"effort": "low"}},
            {"role": "user", "content": "current fable turn",
             "output_config": {"timing": {"now": "2026-11-11T00:00:00Z"}}}]


def native_body(*, stream=False, model=WIRE, effort="high", thinking="default", advisor=False, retention=False):
    tools = [{"name": "Read", "description": "Read a file", "input_schema": {"type": "object", "properties": {}}}]
    if advisor:
        tools.append({"type": "advisor_20260301", "name": "advisor", "model": "claude-opus-5-5", "defer_loading": True})
    body = {"model": model, "max_tokens": 128000, "stream": stream, "metadata": {"user_id": user_id()},
            "system": [{"type": "text", "text": f"x-anthropic-billing-header: cc_version={BASELINE}.20d; "
                                                "cc_entrypoint=cli; cch=00000;"},
                       {"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude."}],
            "tools": tools, "output_config": {"effort": effort}, "messages": history()}
    if thinking == "default":
        body["thinking"] = {"type": "adaptive", "display": "updates"}
    elif thinking is not None:
        body["thinking"] = copy.deepcopy(thinking)
    if retention:
        body["prompt_cache_retention"] = "24h"
    return body


def count_body(thinking=None):
    body = {"model": WIRE, "messages": history(), "tools": native_body()["tools"][:1], "prompt_cache_retention": "24h"}
    if thinking is not None:
        body["thinking"] = copy.deepcopy(thinking)
    return body


def oauth_send(test, gateway, body, headers, *, count=False):
    """One request on the OAuth pool: the reply, the upstream headers and the
    parsed upstream body (exactly one upstream request per client request)."""
    before = len(gateway.upstream.hits)
    path = "/v1/messages/count_tokens?beta=true" if count else "/v1/messages?beta=true"
    reply = harness.unix_request(gateway.socket, "POST", path, headers=headers, body=json.dumps(body).encode(),
                                 timeout=45)
    hits = gateway.upstream.hits[before:]
    test.assertEqual(reply.status, 200, reply.raw[:300])
    test.assertEqual(len(hits), 1)
    upstream_headers, raw, route = hits[0]
    test.assertEqual(route, "/v1/messages/count_tokens" if count else "/v1/messages")
    return reply, upstream_headers, json.loads(raw)


def reply_model(reply):
    if "text/event-stream" in reply.headers.get("content-type", ""):
        return next(event["message"]["model"] for event in reply.events() if event.get("type") == "message_start")
    return reply.json()["model"]


def per_turn(messages):
    """The per-turn controls of a message list: (role, output_config) per message."""
    return [(message.get("role"), message.get("output_config")) for message in messages]


def signed_blocks(messages):
    return [block for message in messages if message.get("role") == "assistant"
            and isinstance(message.get("content"), list) for block in message["content"]
            if isinstance(block, dict) and block.get("type") == "thinking"]


def anthropic_listing(gateway):
    reply = gateway.get("/v1/models")
    if reply.status != 200:
        raise AssertionError(f"Anthropic model listing answered {reply.status}")
    return {row["id"]: row for row in reply.json()["data"]}


class CapturingUpstream(harness.FakeUpstream):
    """The harness's fake upstream that also keeps each request's full
    header map (the harness keeps an allowlist), in memory only."""

    def __init__(self, path):
        self.captured = []

        def respond(route, body, raw, headers):
            self.captured.append({"route": route, "body": body, "headers": dict(headers)})
            return traffic.direct_reply(route, body, raw, headers)

        super().__init__(path, respond)


def key_send(test, gateway, alias, body, *, stream=False, headers=None, count=False):
    """One request through ``gateway.request`` with the captured upstream
    request (None for a locally answered count_tokens)."""
    before = len(gateway.upstream.captured)
    reply = gateway.request(alias, stream=stream, body=body, headers=headers,
                            path="/v1/messages/count_tokens" if count else "/v1/messages")
    test.assertEqual(reply.status, 200, reply.raw[:300])
    captured = gateway.upstream.captured[before:]
    test.assertEqual(len(captured), len(reply.hits))
    if not count:
        test.assertEqual(len(captured), 1)
    return reply, (captured[0] if captured else None)


@uses_shipped_catalog
class StaticRegistryTests(unittest.TestCase):
    def test_fa1_served_from_the_static_registry_reload_keeps_it_and_static_wins(self):
        # FA1: Fable 5.2 is served from the pinned static registry with no
        # operator overlay (registry alone, and the product's Claude-pool
        # render with the Fable 5.2 line); an ordinary reload keeps it; a
        # conflicting overlay entry loses to the static one, while an extra
        # overlay model in the same reload proves the overlay was applied.
        meta = catalog.load_pinned_registry(SHIPPED_ROOT / "registry").meta["claude"][WIRE]
        static = {"display_name": meta.display_name, "max_input_tokens": meta.context_length,
                  "max_tokens": meta.max_completion_tokens,
                  "created_at": datetime.fromtimestamp(meta.created, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        self.assertEqual((static["max_input_tokens"], static["max_tokens"]), (1_000_000, 128_000))
        docs = fable_docs()
        self.assertIsNone(docs["models"]["models"]["fable"]["registry_overlay"])
        extra = "claude-gwtest-fable-overlay"
        rows, tally = [], []
        for label, pool in (("registry-only", {}), ("rendered-pool", fable_pool(docs))):
            with case(self, tally, pool=label):
                self.assertNotIn(WIRE, overlay_names(pool), "the precondition: no overlay names the wire")
                gateway = oauth_gateway(self, pool)

                def served(stage):
                    listed = anthropic_listing(gateway)
                    self.assertIn(WIRE, listed, stage)
                    self.assertEqual({key: listed[WIRE].get(key) for key in static}, static, stage)
                    _reply, _headers, sent = oauth_send(self, gateway, native_body(), native_headers(gateway))
                    self.assertEqual(sent["model"], WIRE, stage)
                    rows.append({"pool": label, "stage": stage, "listed": True, "routed": True})
                    return listed

                served("initial")
                self.assertNotIn(WIRE, overlay_names(gateway.document))
                traffic.reload_document(gateway, traffic.revised(gateway.document))
                served("reload")
                existing = (gateway.document.get(render.OVERLAY_KEY) or {}).get("claude", [])
                conflict = traffic.revised(gateway.document, **{render.OVERLAY_KEY: {"claude": [
                    *copy.deepcopy(existing),
                    {"name": WIRE, "display-name": "gwtest conflicting overlay", "max-context-length": 4096,
                     "max-completion-tokens": 64, "thinking": {"levels": ["low"]}},
                    {"name": extra, "display-name": "gwtest overlay control", "max-context-length": 8192,
                     "max-completion-tokens": 128, "thinking": {"levels": ["low"]}}]}})
                traffic.reload_document(gateway, conflict)
                listed = served("conflicting-overlay")
                self.assertIn(extra, listed, "the overlay reload was not applied")
                self.assertEqual(listed[extra]["max_input_tokens"], 8192)
                restore = copy.deepcopy(conflict)
                restore.pop(render.OVERLAY_KEY)
                if existing:
                    restore[render.OVERLAY_KEY] = {"claude": copy.deepcopy(existing)}
                traffic.reload_document(gateway, traffic.revised(restore))
                served("overlay-removed")
                facts(gateway, "FA1-static-registry", pool=label, static=static, overlay_control=extra)
        record("FA1", tally, pools=["registry-only", "rendered-pool"],
               stages=sorted({row["stage"] for row in rows}))


@uses_shipped_catalog
class NativeFingerprintTests(unittest.TestCase):
    maxDiff = None

    def test_fa2_native_2_1_321_fingerprint_and_beta_order(self):
        # FA2: a confirmed native 2.1.321-or-later client on Fable 5.2 keeps
        # its own fingerprint (user agent with any extension fields, 542a525f
        # and 59304330; Stainless versions; X-App) and its beta order; the
        # gateway only restores the OAuth credential beta at the captured
        # position 2. The capture shape with the advisor tool must reach the
        # upstream exactly as b4e7dbd2 records it (whether the managed client
        # offers that tool through the gateway is the client unit's
        # measurement, not assumed here). Control: a client below the 2.1.321
        # baseline is not native and receives the 2.1.321 baseline fingerprint
        # with the assembled 2.1.321 order.
        gateway = oauth_gateway(self, fable_pool(fable_docs()))
        rows, tally = [], []
        cases = (
            ("2.1.321", (), False),
            ("2.1.330", ("request-phase/background", "agent-sdk/0.2.133", "workload/cron"), False),
            ("2.1.321", (), True),
        )
        for version, extensions, advisor in cases:
            client = without(CLI_2321_MAIN_BETAS, OAUTH_BETA) if advisor else None
            expected = CLI_2321_MAIN_BETAS if advisor else without(CLI_2321_MAIN_BETAS, ADVISOR_BETA)
            for stream in (False, True):
                with case(self, tally, version=version, extensions=extensions, advisor=advisor, stream=stream):
                    headers = native_headers(gateway, version=version, extensions=extensions, beta=client)
                    _reply, sent_headers, sent = oauth_send(self, gateway, native_body(stream=stream, advisor=advisor),
                                                            headers)
                    for name in ("User-Agent", "X-App", "X-Stainless-Package-Version",
                                 "X-Stainless-Runtime-Version", "X-Stainless-Lang", "X-Stainless-Runtime"):
                        self.assertEqual(sent_headers.get(name.lower()), headers[name], name)
                    rows.append({"version": version, "advisor": advisor, "stream": stream,
                                 "betas_match": sent_headers.get("anthropic-beta") == expected})
                    self.assertEqual(betas(sent_headers.get("anthropic-beta")), betas(expected))
                    self.assertEqual(sent["messages"], history(), "native passthrough rewrote the messages")
        for stream in (False, True):
            with case(self, tally, control="2.1.320", stream=stream):
                headers = native_headers(gateway, version="2.1.320",
                                         beta=without(CLI_2321_MAIN_BETAS, OAUTH_BETA))
                _reply, sent_headers, _sent = oauth_send(self, gateway, native_body(stream=stream, advisor=True),
                                                         headers)
                self.assertEqual({name: sent_headers.get(name) for name in BASELINE_FINGERPRINT}, BASELINE_FINGERPRINT)
                order = betas(sent_headers.get("anthropic-beta"))
                self.assertEqual(order[:2], ["claude-code-20250219", OAUTH_BETA])
                for before, after in ((ADVISOR_BETA, "thinking-token-count-2026-05-13"),
                                      ("effort-2025-11-24", "dangerous-tool-use-2026-09-03")):
                    self.assertLess(order.index(before), order.index(after), (before, after))
                self.assertNotIn("mid-conversation-system-2026-04-07", order)
        facts(gateway, "FA2-native-fingerprint", rows=rows)
        record("FA2", tally, native_versions=["2.1.321", "2.1.330"], control="2.1.320")

    def test_fa4_background_requests_are_native(self):
        # FA4: X-App cli-bg (6876a2eb) is a native Claude Code client on
        # execute, stream and count_tokens: its user agent and X-App reach the
        # upstream, its betas pass through (plus the OAuth credential beta),
        # and the body is not cloaked. Control: an unknown X-App is cloaked
        # with the 2.1.321 baseline fingerprint.
        gateway = oauth_gateway(self, fable_pool(fable_docs()))
        version = "2.1.330"  # above the baseline, so a cloak would visibly replace it
        rows, tally = [], []
        for path in ("execute", "stream", "count_tokens"):
            count = path == "count_tokens"
            for x_app in ("cli-bg", "cli-desktop"):
                with case(self, tally, path=path, x_app=x_app):
                    headers = native_headers(gateway, version=version, x_app=x_app, count=count)
                    body = count_body() if count else native_body(stream=path == "stream")
                    _reply, sent_headers, sent = oauth_send(self, gateway, body, headers, count=count)
                    native = x_app == "cli-bg"
                    rows.append({"path": path, "x_app": x_app, "upstream_x_app": sent_headers.get("x-app"),
                                 "user_agent_kept": sent_headers.get("user-agent") == headers["User-Agent"]})
                    if native:
                        self.assertEqual(sent_headers.get("x-app"), "cli-bg")
                        self.assertEqual(sent_headers.get("user-agent"), headers["User-Agent"])
                        order = betas(headers["Anthropic-Beta"])
                        expected = [order[0], OAUTH_BETA, *order[1:]]
                        # The one permitted body change: the 6db7d4b8 default
                        # binding on the signed replay, whose beta travels with it.
                        self.assertEqual(sent["thinking"]["block_binding"], {"prefix_mismatch_behavior": "drop_block"})
                        if BINDING_BETA not in order:
                            expected.append(BINDING_BETA)
                        self.assertEqual(betas(sent_headers.get("anthropic-beta")), expected)
                        self.assertEqual(sent["messages"], history())
                        for field in FIRST_PARTY_FIELDS:
                            self.assertNotIn(field, sent)
                    else:
                        self.assertEqual({name: sent_headers.get(name) for name in BASELINE_FINGERPRINT},
                                         BASELINE_FINGERPRINT)
        facts(gateway, "FA4-cli-bg", rows=rows)
        record("FA4", tally, paths=["execute", "stream", "count_tokens"], control="cli-desktop")


@uses_shipped_catalog
class RetentionStripTests(unittest.TestCase):
    THINKING = {"omitted": None, "adaptive": {"type": "adaptive"},
                "caller-binding": {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "error"}}}

    def expected_thinking(self, name):
        if name == "caller-binding":
            return self.THINKING[name]
        # 6db7d4b8: a signed replay on Fable 5.2 defaults to drop_block
        # (an omitted thinking field is adaptive on preserved-thinking models).
        return {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}

    def test_fa3_binding_and_per_turn_controls_pass_the_retention_strip(self):
        # FA3: on a third-party Claude-format base (the direct-key route, where
        # cli-proxy-api-non-claude-cache-retention.patch strips) only the top-level
        # prompt_cache_retention goes; the preserved-thinking binding (caller's
        # or the 6db7d4b8 default), its beta, the signed block and the per-turn
        # controls pass unchanged on execute and stream; count_tokens there is
        # answered locally (nothing reaches an upstream). On the official
        # Anthropic base (the OAuth pool) the strip is a no-op by contract and
        # the same fields pass on execute, stream and count_tokens.
        key = harness.GatewayHarness(document_factory=recognition.native_document, upstream_factory=CapturingUpstream)
        self.addCleanup(key.close)
        check_version(self, key)
        rows, tally = [], []
        for name, thinking in self.THINKING.items():
            for stream in (False, True):
                with case(self, tally, route="direct-key", thinking=name, stream=stream):
                    body = {"messages": history(), "output_config": {"effort": "max"}, "prompt_cache_retention": "24h"}
                    if thinking is not None:
                        body["thinking"] = copy.deepcopy(thinking)
                    _reply, captured = key_send(self, key, WIRE, body, stream=stream)
                    sent = captured["body"]
                    self.assertNotIn("prompt_cache_retention", sent)
                    self.assertEqual(sent["model"], WIRE)
                    self.assertEqual(sent.get("thinking"), self.expected_thinking(name))
                    self.assertIn(BINDING_BETA, betas(captured["headers"].get("anthropic-beta")))
                    self.assertEqual(sent["output_config"], {"effort": "max"})
                    self.assertEqual(per_turn(sent["messages"])[:len(history())], per_turn(history()))
                    self.assertEqual(signed_blocks(sent["messages"]), signed_blocks(history()))
                    rows.append({"route": "direct-key", "thinking": name, "stream": stream, "retention_stripped": True})
        with case(self, tally, route="direct-key", path="count_tokens"):
            body = {"messages": [message for message in history() if message["role"] != "system"],
                    "thinking": {"type": "adaptive"}, "prompt_cache_retention": "24h"}
            reply, captured = key_send(self, key, WIRE, body, count=True)
            self.assertIsNone(captured, "a third-party count_tokens reached an upstream")
            self.assertIsInstance(reply.json().get("input_tokens"), int)
        gateway = oauth_gateway(self, fable_pool(fable_docs()))
        for name, thinking in self.THINKING.items():
            for path in ("execute", "stream", "count_tokens"):
                with case(self, tally, route="oauth", thinking=name, path=path):
                    count = path == "count_tokens"
                    body = count_body(thinking) if count else native_body(stream=path == "stream", thinking=thinking,
                                                                          retention=True)
                    _reply, sent_headers, sent = oauth_send(self, gateway, body, native_headers(gateway, count=count),
                                                            count=count)
                    expected = self.expected_thinking(name)
                    self.assertEqual({key: value for key, value in sent["thinking"].items() if key != "display"},
                                     expected)
                    self.assertIn(BINDING_BETA, betas(sent_headers.get("anthropic-beta")))
                    self.assertEqual(sent["messages"], history())
                    self.assertEqual(sent.get("prompt_cache_retention"), "24h", "the official base is preserved")
                    rows.append({"route": "oauth", "thinking": name, "path": path, "retention_stripped": False})
        facts(gateway, "FA3-retention-strip", rows=rows)
        record("FA3", tally, routes=["direct-key", "oauth"], paths=["execute", "stream", "count_tokens"])


def keyed_document(bundle, egress, port, home):
    """The product render of the fixture's own Kimi and OpenRouter
    providers (Claude format, x-api-key), redirected to the fake upstream:
    Kimi and OpenRouter Opus with their fixture lines, and OpenRouter serving
    Fable 5.2 under its OpenRouter id with charmer's gateway-effort selectors."""
    providers = {}
    for provider_id in ("kimi", "openrouter"):
        provider = copy.deepcopy(bundle.providers[provider_id])
        provider["transport"]["base_url"] = f"http://127.0.0.1:{egress}/gwtest-{provider_id}"
        provider["transport"]["auth"]["secret_ref"] = "env:GWTEST_" + provider_id.upper()
        providers[provider_id] = provider
    lines = {key: copy.deepcopy(bundle.lines[key]) for key in ("kimi-k3", "or-opus")}
    fable = copy.deepcopy(bundle.lines["or-opus"])
    fable.update(wire_model="anthropic/claude-fable-5.2", display="Fable 5.2 · OpenRouter", generation="5.2")
    for level, spec in fable["efforts"].items():
        spec["selector"] = f"charmer-or-fable-{level}[1m]"
    lines["or-fable"] = fable
    gateway_doc = harness.shipped_gateway_doc(bundle.docs["gateway"])
    gateway_doc["gateway"]["base_url"] = f"http://127.0.0.1:{port}"
    document, available, unavailable, info = render.build_config_document(
        gateway_doc, providers, lines, home=home, gateway_token=harness.FIXTURE_GATEWAY_TOKEN,
        resolve_secret=lambda name: "dummy-gwtest-" + name.removeprefix("GWTEST_").lower(), continuity={})
    if unavailable or set(available) != set(providers):
        raise AssertionError("a keyed fixture provider was omitted by the real render")
    return document, {}, info


def ttl_values(value):
    if isinstance(value, dict):
        own = [value["cache_control"].get("ttl")] if isinstance(value.get("cache_control"), dict) else []
        return own + [ttl for item in value.values() for ttl in ttl_values(item)]
    if isinstance(value, list):
        return [ttl for item in value for ttl in ttl_values(item)]
    return []


class KeyedProviderTests(unittest.TestCase):
    def test_fa5_kimi_and_openrouter_get_no_first_party_fields_or_headers(self):
        # FA5: Claude-format traffic to Kimi and to OpenRouter (Opus 5.5, and
        # Fable 5.2 under its OpenRouter id, anthropic/claude-fable-5.2) gets
        # none of the first-party-only additions: no header the client did not
        # send beyond transport and route auth, its own Anthropic-Beta (none
        # added), no preserved-thinking binding, no thinking display, no
        # context_management/diagnostics, no metadata, system or 1h cache TTL
        # it did not send; count_tokens never leaves the gateway.
        gateway = harness.GatewayHarness(document_factory=keyed_document, upstream_factory=CapturingUpstream)
        self.addCleanup(gateway.close)
        check_version(self, gateway)
        routes = {}
        for section in gateway.document["claude-api-key"]:
            for model in section["models"]:
                routes[model["alias"]] = model["name"]
        aliases = {"kimi": "charmer-kimi-k3", "openrouter-opus": "charmer-or-opus-high",
                   "openrouter-fable": "charmer-or-fable-high"}
        self.assertEqual(routes.get(aliases["openrouter-fable"]), "anthropic/claude-fable-5.2")
        self.assertEqual(routes.get(aliases["openrouter-opus"]), "anthropic/claude-opus-5.5")
        self.assertIn(aliases["kimi"], routes)
        rows, tally = [], []
        for provider, alias in aliases.items():
            for client in ("plain", "native"):
                for stream in (False, True):
                    with case(self, tally, provider=provider, client=client, stream=stream):
                        body = {"thinking": {"type": "adaptive"}, "messages": history(), "prompt_cache_retention": "24h"}
                        headers = {}
                        if client == "native":
                            headers = {name: value for name, value in native_headers(gateway).items()
                                       if name not in ("Authorization", "Host", "Content-Type", "Anthropic-Version")}
                            native = native_body()
                            body.update(metadata=native["metadata"], system=native["system"])
                        _reply, captured = key_send(self, gateway, alias, body, stream=stream, headers=headers)
                        sent, sent_headers = captured["body"], {k.lower(): v for k, v in captured["headers"].items()}
                        client_headers = {k.lower(): v for k, v in {**gateway.headers(), **headers}.items()}
                        added = sorted(set(sent_headers) - set(client_headers) - TRANSPORT_HEADERS)
                        found = {"added_headers": added,
                                 "beta_added": betas(sent_headers.get("anthropic-beta"))
                                 != betas(client_headers.get("anthropic-beta")),
                                 "binding": "block_binding" in (sent.get("thinking") or {}),
                                 "display": "display" in (sent.get("thinking") or {}),
                                 "first_party_fields": [field for field in FIRST_PARTY_FIELDS if field in sent],
                                 "metadata_changed": sent.get("metadata") != body.get("metadata"),
                                 "system_changed": sent.get("system") != body.get("system"),
                                 "ttl": sorted({ttl for ttl in ttl_values(sent) if ttl})}
                        rows.append({"provider": provider, "client": client, "stream": stream, **{
                            key: value for key, value in found.items() if value}})
                        self.assertEqual(found, {"added_headers": [], "beta_added": False, "binding": False,
                                                 "display": False, "first_party_fields": [], "metadata_changed": False,
                                                 "system_changed": False, "ttl": []})
            with case(self, tally, provider=provider, path="count_tokens"):
                body = {"messages": [message for message in history() if message["role"] != "system"]}
                _reply, captured = key_send(self, gateway, alias, body, count=True)
                self.assertIsNone(captured, "a keyed count_tokens reached an upstream")
        facts(gateway, "FA5-keyed-first-party", rows=rows)
        record("FA5", tally, providers=sorted(aliases), clients=["native", "plain"])


@uses_shipped_catalog
class LiteralModelIdTests(unittest.TestCase):
    def test_fa6_literal_model_id_survives_for_the_selector_and_every_effort(self):
        # FA6: the Fable line is client-effort, so its only gateway aliases are
        # the selectors catalog.line_selectors yields (one: the canonical
        # claude-fable-5-2[1m], whose [1m] the client strips); its efforts
        # travel in output_config.effort. Each alias is listed literally in
        # both listing formats (never the cloaked claude-fable-5-dd- form),
        # and for every effort it reaches the upstream as the literal wire
        # with the effort unchanged and returns to the client under its own id,
        # on execute, stream and count_tokens.
        docs = fable_docs()
        line = docs["models"]["models"]["fable"]
        selectors = catalog.line_selectors(line)
        self.assertEqual([selector for _effort, selector, _contract in selectors], [WIRE + "[1m]"])
        self.assertEqual(line["efforts"], ["low", "medium", "high", "xhigh", "max"])
        pool = fable_pool(docs)
        table = {entry["alias"]: entry["name"] for entry in pool["oauth-model-alias"]["claude"]}
        gateway = oauth_gateway(self, pool)
        anthropic = set(anthropic_listing(gateway))
        openai = {row["id"] for row in gateway.served_listing().json()["data"]}
        rows, tally = [], []
        for _effort, selector, _contract in selectors:
            alias = selector.removesuffix("[1m]")
            self.assertEqual(table.get(alias), WIRE)
            for listing in (anthropic, openai):
                self.assertIn(alias, listing)
                self.assertNotIn(patches.cloaked(alias), listing)
            for effort in line["efforts"]:
                for stream in (False, True):
                    with case(self, tally, alias=alias, effort=effort, stream=stream):
                        reply, _headers, sent = oauth_send(self, gateway, native_body(stream=stream, model=alias,
                                                                                      effort=effort),
                                                           native_headers(gateway))
                        self.assertEqual(sent["model"], WIRE)
                        self.assertEqual(sent["output_config"], {"effort": effort})
                        self.assertEqual(reply_model(reply), alias)
                        rows.append({"alias": alias, "effort": effort, "stream": stream})
            with case(self, tally, alias=alias, path="count_tokens"):
                body = count_body()
                body["model"] = alias
                _reply, _headers, sent = oauth_send(self, gateway, body, native_headers(gateway, count=True), count=True)
                self.assertEqual(sent["model"], WIRE)
        facts(gateway, "FA6-literal-model-id", rows=rows)
        record("FA6", tally, aliases=[selector.removesuffix("[1m]") for _e, selector, _c in selectors],
               efforts=list(line["efforts"]))
