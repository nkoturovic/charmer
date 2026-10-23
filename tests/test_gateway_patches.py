"""Discriminating patch rows, against disposable network namespaces only."""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from charmer import probe, render, state, strict_json
from _catalog import SHIPPED_ROOT, uses_shipped_catalog
import _gateway_harness as harness

MODULE = "tests.test_gateway_patches"


def tearDownModule():
    harness.EVIDENCE.finalize_module(MODULE)


def record(row, **fields):
    harness.EVIDENCE.record(MODULE, "patches", {"row": row, **fields})


def gateway_binary():
    binary, reason = harness.find_gateway_binary()
    if binary is None:
        harness.boundary("BOUNDARY: " + reason)
    reason = probe.network_isolation_available()
    if reason:
        harness.boundary("BOUNDARY: " + reason)
    return binary


class LoopbackOAuthListenerTests(unittest.TestCase):
    def check_login(self, provider, row, method="B"):
        binary = gateway_binary()
        with tempfile.TemporaryDirectory(prefix="gwtest-login-") as directory:
            root = Path(directory).resolve()
            auth = root / "auth"
            state.ensure_private_dir(auth)
            port, _ = harness.ports()
            config = root / "config.yaml"
            state.atomic_write(config, (f'host: 127.0.0.1\nauth-dir: "{auth}"\n'
                'discovery: {enabled: false}\nremote-management: {disable-control-panel: true}\n').encode())
            argv = [binary, "--config", str(config), "--" + provider + "-login", "--no-browser",
                    "--oauth-callback-port", str(port)]
            if method == "A":
                argv = [os.path.realpath(sys.executable), "-I", "-S", "-c",
                        harness.listener_probe_source(), str(port), *argv]
            log_path = root / "login.log"
            with log_path.open("wb") as log:
                process = probe.start_isolated_process(argv, cwd=root,
                    env={"HOME": str(root), "PATH": "/usr/bin:/bin"}, stdout=log, stderr=subprocess.STDOUT)
                try:
                    if method == "A":
                        process.wait(timeout=15)
                        self.assertEqual(process.returncode, 0, "isolated listener probe failed")
                        match = re.search(r"^LISTEN=(.+)$", log_path.read_text(), re.M)
                        self.assertIsNotNone(match, "listener probe omitted its result")
                        rows = [tuple(row) for row in json.loads(match.group(1))]
                    else:
                        deadline = time.monotonic() + 10
                        while time.monotonic() < deadline:
                            self.assertIsNone(process.poll(), "OAuth login exited before callback listen")
                            try:
                                rows = harness.listener_rows(process.pid, binary)
                                if any(p == port for _, p in rows):
                                    break
                            except AssertionError as exc:
                                if "descendant not found" not in str(exc):
                                    raise
                            time.sleep(0.02)
                        else:
                            self.fail("OAuth callback listener deadline exceeded")
                    self.assertTrue(any(p == port for _, p in rows))
                    self.assertTrue(all(harness.loopback_listener(addr) for addr, _ in rows), rows)
                    record(row, method=method, addresses=[addr for addr, _ in rows], ports=[p for _, p in rows])
                finally:
                    probe.stop_isolated_process(process)

    def test_claude_login_callback_listens_on_loopback_only(self):
        self.check_login("claude", "LO1")

    def test_codex_login_callback_listens_on_loopback_only(self):
        self.check_login("codex", "LO2")

    def test_antigravity_login_callback_listens_on_loopback_only(self):
        # An installed gateway may predate this patch: never fall back to it.
        if not os.environ.get(harness.BINARY_ENV):
            harness.boundary("BOUNDARY: LO4 needs " + harness.BINARY_ENV + " set to the manifest build")
        self.check_login("antigravity", "LO4")

    def test_fallback_listener_probe_inside_namespace(self):
        self.check_login("claude", "LO1-A", "A")


class GatewayListenerTests(unittest.TestCase):
    def test_tcp_and_udp_listeners_are_private(self):
        gateway = harness.main_harness()
        rows = harness.listener_rows(gateway.process.pid, gateway.binary)
        self.assertEqual(rows, sorted([("127.0.0.1", gateway.port), ("127.0.0.1", gateway.egress)]))
        udp = harness.listener_rows(gateway.process.pid, gateway.binary, udp=True)
        self.assertTrue(all(harness.loopback_listener(addr) for addr, _ in udp), udp)
        record("LO3", tcp_count=len(rows), udp_count=len(udp))


class ManagementInertWithoutKeyTests(unittest.TestCase):
    def test_management_is_inert_without_key(self):
        gateway = harness.main_harness()
        reply = harness.unix_request(gateway.socket, "GET", "/v0/management/auth-files",
            headers={**gateway.headers(), "X-Management-Key": "dummy-gwtest-mgmt"})
        self.assertEqual(reply.status, 404)
        record("M0", status=reply.status)


class ManagementAllowlistTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gateway = harness.GatewayHarness(management=True)
        cls.addClassCleanup(cls.gateway.close)

    def request(self, method, path, *, key=True, **headers):
        # Do not exercise bad keys on allowlisted routes (five-strike ban).
        headers = {"Host": f"127.0.0.1:{self.gateway.port}", **headers}
        if key:
            headers["X-Management-Key"] = "dummy-gwtest-mgmt"
        return harness.unix_request(self.gateway.socket, method, path, headers=headers)

    def test_management_positive_control(self):
        reply = self.request("GET", "/v0/management/auth-files")
        self.assertEqual(reply.status, 200)
        record("M1", status=reply.status)

    def test_refused_route_is_404_with_valid_key(self):
        reply = self.request("GET", "/v0/management/config.yaml")
        self.assertEqual((reply.status, len(reply.raw)), (404, 0))
        record("M2", status=reply.status, body_length=len(reply.raw))

    def test_oauth_callback_is_404(self):
        reply = self.request("POST", "/v0/management/oauth-callback", key=False)
        self.assertEqual(reply.status, 404)
        record("M3", status=reply.status)

    def test_browser_origin_is_404_on_allowlisted_route(self):
        reply = self.request("GET", "/v0/management/auth-files", Origin="http://gwtest.invalid")
        self.assertEqual(reply.status, 404)
        record("M4", status=reply.status)

    def test_v8_management_is_404_with_valid_key(self):
        # The v8 namespace is refused whole, its read twin of the allowlist too.
        statuses = {}
        for method, path in (("GET", "/v8/management/credentials"), ("GET", "/v8/management/config"),
                             ("GET", "/v8/management/oauth/callback"), ("POST", "/v8/management/requests/api-call"),
                             # Trailing-slash spellings: the same bare 404, never Gin's redirect.
                             ("GET", "/v8/management/credentials/"),
                             ("GET", "/v8/management/server/latest-version/")):
            reply = self.request(method, path)
            self.assertEqual((reply.status, len(reply.raw)), (404, 0), method + " " + path)
            self.assertNotIn("location", reply.headers, method + " " + path)
            self.assertNotIn("access-control-allow-origin", reply.headers, method + " " + path)
            statuses[method + " " + path] = reply.status
        record("M5", refused=len(statuses))

    def test_refused_api_surfaces_are_404(self):
        # Model detail, the speech routes and every other 8.x surface charmer
        # does not serve (realtime, live, videos, images, alpha search,
        # interactions, the top-level OAuth redirect pages) are refused with
        # the API key too.
        headers = {"Authorization": "Bearer " + harness.FIXTURE_GATEWAY_TOKEN}
        statuses = {}
        for method, path in (("GET", "/v1/models/gwtest-detail"), ("POST", "/v1/audio/speech"), ("POST", "/v1/tts"),
                             ("GET", "/v1/realtime"), ("POST", "/v1/realtime/client_secrets"), ("GET", "/v1/live"),
                             ("POST", "/v1/videos"), ("POST", "/openai/v1/videos"),
                             ("POST", "/v1/images/generations"), ("POST", "/v1/alpha/search"),
                             ("POST", "/backend-api/codex/alpha/search"), ("POST", "/v1beta/interactions"),
                             ("GET", "/callback"), ("GET", "/codex/callback"), ("GET", "/anthropic/callback")):
            reply = self.request(method, path, key=False, **headers)
            self.assertEqual((reply.status, len(reply.raw)), (404, 0), method + " " + path)
            self.assertNotIn("access-control-allow-origin", reply.headers, method + " " + path)
            statuses[method + " " + path] = reply.status
        listing = self.request("GET", "/v1/models", key=False, **headers)
        self.assertEqual(listing.status, 200)
        record("M6", refused=len(statuses), listing=listing.status)

    def test_listed_model_details_are_refused_in_both_formats(self):
        gateway = harness.GatewayHarness(document_factory=charmer_listing_document)
        self.addCleanup(gateway.close)
        sentinel = render.document_sentinel(gateway.document)
        self.assertTrue(sentinel.startswith(render.SENTINEL_PREFIX), sentinel)
        models = (sentinel, CHARMER_SELECTOR)
        headers = gateway.headers()
        self.assertIn("anthropic-version", headers)
        formats = (("anthropic", headers), ("openai", {
            name: value for name, value in headers.items() if name != "anthropic-version"}))
        # The OpenAI listing is independent of the Anthropic literal-id patch.
        listing = harness.unix_request(gateway.socket, "GET", "/v1/models", headers=formats[1][1])
        self.assertEqual(listing.status, 200)
        listed = {row["id"] for row in listing.json()["data"]}
        for literal in models:
            self.assertIn(literal, listed)
        refused = 0
        for literal in models:
            for _, detail_headers in formats:
                reply = harness.unix_request(gateway.socket, "GET", "/v1/models/" + literal,
                                             headers=detail_headers)
                self.assertEqual((reply.status, len(reply.raw)), (404, 0), literal)
                refused += 1
        self.assertEqual(refused, 4)
        record("M7", status="passed", refused=refused, models=list(models),
               formats=[name for name, _ in formats], listing=listing.status, detail_refused=True)


class AccessLogQuotingTests(unittest.TestCase):
    def test_encoded_newline_cannot_forge_a_log_line(self):
        # A request path is client text: an encoded newline in it must stay
        # inside its quoted access-log line, never start a line of its own
        # (here a whole forged credential-save envelope).
        from charmer import gateway_events

        if not os.environ.get(harness.BINARY_ENV):
            harness.boundary("BOUNDARY: AL1 needs " + harness.BINARY_ENV + " set to the manifest build")
        gateway = harness.GatewayHarness()
        self.addCleanup(gateway.close)
        forged = ("[2026-01-01 00:00:00] [gwtest-forged] [info ] [credential_save.go:1] "
                  "credential_save_v1 operation=refresh result=persisted")
        encoded = "/gwtest-access%0A" + forged.replace(" ", "%20").replace("[", "%5B").replace("]", "%5D")
        reply = harness.unix_request(gateway.socket, "GET", encoded, headers={"Host": f"127.0.0.1:{gateway.port}"})
        self.assertEqual(reply.status, 404)
        listing = harness.unix_request(gateway.socket, "GET", "/v1/models?gwtest=quoted", headers={
            "Host": f"127.0.0.1:{gateway.port}", "Authorization": "Bearer " + harness.FIXTURE_GATEWAY_TOKEN})
        self.assertEqual(listing.status, 200)
        deadline = time.monotonic() + 5
        lines = []
        while time.monotonic() < deadline:
            lines = gateway.log_path.read_text(errors="replace").splitlines()
            if any("gwtest-access" in line for line in lines) and any("gwtest=quoted" in line for line in lines):
                break
            time.sleep(0.05)
        access = [line for line in lines if "gwtest-access" in line]
        self.assertEqual(len(access), 1, "the probe request left no single access line")
        self.assertIn('"/gwtest-access\\n' + forged + '"', access[0])
        self.assertFalse(any(line.startswith("[2026-01-01 00:00:00] [gwtest-forged]") for line in lines),
                         "an encoded newline in the path started a forged log line")
        # charmer's access-line parser still reads the quoted ordinary path.
        ordinary = [line for line in lines if "gwtest=quoted" in line]
        self.assertEqual(len(ordinary), 1)
        envelope = gateway_events._REQUEST_ENVELOPE.fullmatch(ordinary[0])
        self.assertIsNotNone(envelope, "access line lost its envelope")
        http = gateway_events._HTTP.fullmatch(envelope.group(2))
        self.assertIsNotNone(http, "the access-line parser no longer reads a %q-quoted path")
        self.assertEqual((http.group(1), http.group(2), http.group(3)), ("200", "GET", "/v1/models?gwtest=quoted"))
        record("AL1", status=reply.status, quoted=True, parsed=True)


class ManagementEnvOnlyTests(unittest.TestCase):
    def test_config_secret_cannot_enable_management(self):
        # The installed wrapper may select a different gateway build. Patch
        # evidence must select the manifest build, not infer it from version.
        if not os.environ.get(harness.BINARY_ENV):
            harness.boundary("BOUNDARY: ME1 needs " + harness.BINARY_ENV + " set to the manifest build")
        gateway = harness.GatewayHarness(config_secret=True)
        self.addCleanup(gateway.close)
        reply = harness.unix_request(gateway.socket, "GET", "/v0/management/auth-files",
            headers={"Host": f"127.0.0.1:{gateway.port}", "X-Management-Key": "dummy-gwtest-config-secret"})
        self.assertEqual(reply.status, 404)
        record("ME1", status=reply.status)


class StartupFileAuthTests(unittest.TestCase):
    def test_first_model_route_waits_for_file_auth(self):
        gateway = gateway_binary()  # ordinary main check retains the gateway BOUNDARY
        try:
            binary = harness.selected_diagnostic(gateway)
        except harness.DiagnosticMissing as exc:
            harness.boundary("BOUNDARY: set " + harness.STARTUP_PROBE_ENV + " to the matching diagnostic (" + str(exc) + ")")
        with tempfile.TemporaryDirectory(prefix="gwtest-startup-") as directory:
            root = Path(directory).resolve()
            log_path = root / "probe.log"
            with log_path.open("wb") as log:
                process = probe.start_isolated_process(
                    [str(binary), "-test.v", "-test.run=^TestGatewayStartupFileAuth$", "-test.timeout=25s"],
                    cwd=root, env={"HOME": str(root), "TMPDIR": str(root), "PATH": "/usr/bin:/bin"},
                    stdout=log, stderr=subprocess.STDOUT)
                try:
                    process.wait(timeout=30)
                finally:
                    probe.stop_isolated_process(process)
            # Never print auth/config/log payloads, even on a failed mutant.
            output = log_path.read_text()
            self.assertEqual(process.returncode, 0, "isolated startup file-auth probe failed")
            self.assertIn("--- PASS: TestGatewayStartupFileAuth", output)
            record("S1", status="passed", diagnostic=str(binary), synthetic_file_auth=True)


class PatchOmissionProbeTests(unittest.TestCase):
    """Independent API probes, built against the selected variant.

    P1 and every admitted patch row require an explicitly selected gateway
    build before creating a harness.
    Race probes are runtime discriminators, not strings/build-presence checks.
    """
    def check_probe(self, package, name, row):
        if not os.environ.get(harness.BINARY_ENV):
            harness.boundary("BOUNDARY: " + row + " needs " + harness.BINARY_ENV + " set to the manifest build")
        gateway = gateway_binary()
        # A store output or a content-bound build of the selected gateway;
        # anything else fails (the selected build needs its own diagnostic).
        binary = harness.selected_diagnostic(gateway, None if package == "service"
                                             else "gwtest-" + package + "-probe")
        with tempfile.TemporaryDirectory(prefix="gwtest-omission-") as directory:
            root = Path(directory).resolve()
            with (root / "probe.log").open("wb") as log:
                process = probe.start_isolated_process(
                    [str(binary), "-test.v", "-test.run=^" + name + "$", "-test.timeout=20s"],
                    cwd=root, env={"HOME": str(root), "TMPDIR": str(root), "PATH": "/usr/bin:/bin"},
                    stdout=log, stderr=subprocess.STDOUT)
                try:
                    process.wait(timeout=25)
                finally:
                    probe.stop_isolated_process(process)
            self.assertEqual(process.returncode, 0, "isolated " + row + " omission probe failed")
            self.assertIn("--- PASS: " + name, (root / "probe.log").read_text())
        record(row, status="passed", diagnostic=str(binary), test=name)

    def test_compat_retention(self):
        self.check_probe("executor", "TestOmissionCompatRetention", "P1")

    def test_credential_receipt(self):
        self.check_probe("service", "TestOmissionCredentialReceipt", "CS1")

    def test_credentialed_redirect(self):
        self.check_probe("executor", "TestOmissionCredentialedRedirect", "CR1")

    def test_keyed_safety(self):
        self.check_probe("executor", "TestOmissionKeyedSafety", "KS1")

    def test_auth_snapshot(self):
        self.check_probe("service", "TestOmissionAuthSnapshot", "AS1")

    def test_config_snapshot(self):
        self.check_probe("handlers", "TestOmissionConfigSnapshot", "SC1")

    def test_metadata_lock(self):
        self.check_probe("executor", "TestOmissionMetadataLock", "ML1")

    def test_overlay(self):
        self.check_probe("service", "TestOmissionOverlay", "OM1")

    def test_refresh_shutdown(self):
        self.check_probe("service", "TestOmissionRefreshShutdown", "RS1")

    def test_codex_refresh_lead(self):
        self.check_probe("service", "TestOmissionCodexRefreshLead", "RL1")



class CodexIdentityTests(unittest.TestCase):
    def test_codex_identity(self):
        binary = harness.run_codex_diagnostic("TestOmissionCodexIdentity")
        record("CI1", status="passed", diagnostic=binary,
               test="TestOmissionCodexIdentity")

    def test_final_header_precedence_matrix(self):
        # Six explicit, nonempty tests on the diagnostic of the selected build;
        # the final-header inventory includes 8.x's X-Codex-Routing-Hint.
        names = ("HTTPFinalHeaders", "WebsocketFinalHeaders", "VersionDefault",
                 "CloakingPrecedence", "RoutingHintFinal", "StaticOverrideException")
        passed = {}
        for name in names:
            with self.subTest(name=name):
                passed[name] = harness.run_codex_diagnostic("TestCM055CodexIdentity" + name, package="executor")
        # A failed (or boundary-skipped) subTest does not stop the loop: the
        # evidence row is recorded only after all six diagnostics succeeded.
        if len(passed) != len(names):
            if passed:
                self.fail("final-header matrix incomplete: " + ", ".join(n for n in names if n not in passed))
            return
        self.assertEqual(len(set(passed.values())), 1, "the six diagnostics ran on different builds")
        record("CI2", status="passed", diagnostic=passed[names[0]], tests=len(names),
               static_exception="gpt-5.6-luna")


class CodexApiKeySafetyTests(unittest.TestCase):
    """The OpenAI API-key route as rendered (a codex API key with cloaking
    off) against a fake Responses upstream that quotes the key it received:
    the client gets fixed local failure text, and the request carries no
    codex client Version default and no session header."""

    def test_key_route_failures_and_identity(self):
        # An installed gateway may predate this patch: never fall back to it.
        if not os.environ.get(harness.BINARY_ENV):
            harness.boundary("BOUNDARY: CK1 needs " + harness.BINARY_ENV + " set to the manifest build")
        import test_openai_key_route as route
        gateway = harness.GatewayHarness(
            document_factory=route.gateway_document,
            upstream_factory=lambda path: harness.FakeUpstream(path, respond=route.reflecting_upstream))
        try:
            alias = sorted(gateway.info["reviewed"])[0]
            secret = route.DUMMY.encode()
            cases = 0
            for case, text in (("reflect-status-failure", b"upstream rejected the credential"),
                               ("reflect-stream-failure", b"upstream server error")):
                for stream in (False, True):
                    with self.subTest(case=case, stream=stream):
                        reply = gateway.request(alias, stream=stream,
                                                body={"messages": [{"role": "user", "content": case}]})
                        self.assertEqual(len(reply.hits), 1, "the failure never reached the fake upstream")
                        self.assertNotIn(secret, reply.raw)
                        self.assertIn(text, reply.raw)
                        if not stream:
                            self.assertGreaterEqual(reply.status, 400)
                        cases += 1
            reply = gateway.request(alias)
            self.assertEqual(reply.status, 200, reply.raw[:300])
            sent = set(reply.hits[0].header_names)
            self.assertFalse({"version", "session-id", "session_id", "originator", "chatgpt-account-id"} & sent)
            self.assertEqual(cases, 4)
            record("CK1", status="passed", failure_cases=cases, fixed_text=True, version_header=False,
                   session_header=False)
        finally:
            gateway.close()


CHARMER_SELECTOR = "charmer-gwtest-listed"
GPT_MULTI_SELECTOR = "gpt-multi-gwtest-listed"


def cloaked(model_id):
    """CLIProxyAPI's Anthropic-listing name for an id it cloaks
    (EnsureClaudeModelIDPrefix): ``claude-fable-5-dd-`` plus the id reversed."""
    return "claude-fable-5-dd-" + model_id[::-1]


def charmer_listing_document(bundle, egress, gateway, home):
    """The main document plus a ``charmer-`` and a ``gpt-multi-`` alias of the
    plain compat model, listed next to the ``charmer-render-`` sentinel."""
    document, aliases, info = harness.build_document(bundle, egress, gateway, home, "main")
    section = next(section for section in document["openai-compatibility"]
                   if urlsplit(section["base-url"]).path == "/gwtest-compat")
    plain = next(model for model in section["models"] if model["alias"] == "gwtest-compat-plain")
    for alias in (CHARMER_SELECTOR, GPT_MULTI_SELECTOR):
        section["models"].append({**plain, "alias": alias})
        aliases[alias] = aliases["gwtest-compat-plain"]
    return document, aliases, info


class CharmerModelListingTests(unittest.TestCase):
    """With Anthropic-Version the gateway answers ``GET /v1/models`` in the
    Anthropic format: ``charmer-`` selectors and the ``charmer-render-``
    sentinel are listed literally (as 1.1.0 listed its own selectors under
    their earlier prefix), and every other alias outside ``claude-``, ``gpt-multi-``
    included, keeps the upstream cloaking. Without the header the OpenAI
    listing is unchanged: every alias as configured."""

    @classmethod
    def setUpClass(cls):
        # An installed gateway may predate this patch: never fall back to it.
        if not os.environ.get(harness.BINARY_ENV):
            harness.boundary("BOUNDARY: CL1 needs " + harness.BINARY_ENV + " set to the manifest build")
        cls.gateway = harness.GatewayHarness(document_factory=charmer_listing_document)
        cls.addClassCleanup(cls.gateway.close)

    def listing(self, headers):
        reply = harness.unix_request(self.gateway.socket, "GET", "/v1/models", headers=headers)
        self.assertEqual(reply.status, 200)
        return {row["id"] for row in reply.json()["data"]}

    def test_both_listing_formats(self):
        sentinel = render.document_sentinel(self.gateway.document)
        self.assertTrue(sentinel.startswith(render.SENTINEL_PREFIX), sentinel)
        headers = self.gateway.headers()
        self.assertIn("anthropic-version", headers)
        anthropic = self.listing(headers)
        openai = self.listing({name: value for name, value in headers.items() if name != "anthropic-version"})
        for literal in (sentinel, CHARMER_SELECTOR):
            self.assertIn(literal, anthropic)
            self.assertNotIn(cloaked(literal), anthropic)
        for other in (GPT_MULTI_SELECTOR, "gwtest-compat-plain"):
            self.assertIn(cloaked(other), anthropic)
            self.assertNotIn(other, anthropic)
        for alias in (sentinel, CHARMER_SELECTOR, GPT_MULTI_SELECTOR, "gwtest-compat-plain"):
            self.assertIn(alias, openai)
            self.assertNotIn(cloaked(alias), openai)
        record("CL1", status="passed", anthropic_literal=[sentinel, CHARMER_SELECTOR],
               anthropic_cloaked=[GPT_MULTI_SELECTOR, "gwtest-compat-plain"], openai_literal=True)


@uses_shipped_catalog
class ContentChunkRouteInventoryTests(unittest.TestCase):
    def test_shipped_protocol_shapes_have_controls(self):
        # Read declarations only: never the operator's configured providers.
        providers = json.loads((SHIPPED_ROOT / "catalog/providers.json").read_text())["providers"]
        self.assertEqual({name: value["adapter"] for name, value in providers.items()}, {
            "anthropic": "cliproxy-oauth-claude-v1", "openai": harness.CODEX,
            **{name: harness.CLAUDE for name in ("openrouter", "deepseek", "kimi", "qwen", "meta")},
        })
        presets = {path.stem: json.loads(path.read_text())["provider"]
                   for path in (SHIPPED_ROOT / "presets").glob("*.json")}
        self.assertEqual({name for name, value in presets.items() if value["kind"] == "openai-compatible"},
                         {"cerebras", "gemini", "groq", "mistral", "xai"})
        self.assertEqual({name for name, value in presets.items() if value["kind"] == "openai-compatible-lan"},
                         {"lan-openai-compatible", "llama-cpp", "lm-studio", "ollama", "vllm"})
        self.assertEqual({name for name, value in presets.items() if value["kind"] == "anthropic-compatible"},
                         {"model-studio", "moonshot", "novita", "vercel", "zai"})
        self.assertEqual(presets["mistral"]["auth"]["kind"], "bearer")
        self.assertEqual(presets["zai"]["kind"], "anthropic-compatible")
        self.assertEqual(set(harness.CONTENT_CHUNK_SHAPES),
                         {"openai-codex", "openrouter", "deepseek", "kimi", "qwen", "meta", "local-llm", "keyed"})

    def test_goldens_are_independent_omission_controls(self):
        import check_gateway_patch_revert as tool
        patch = "cli-proxy-api-openai-content-chunks.patch"
        self.assertEqual(len(harness.PATCH_ROWS[patch]), 2)
        self.assertEqual(len(harness.PATCH_CONTROL_ROWS[patch]), 2)
        for control in harness.PATCH_CONTROL_ROWS[patch]:
            self.assertIn(control, tool.test_ids())
            self.assertNotIn(control, harness.PATCH_ROWS[patch])
        for route in ("keyless", "keyed"):
            for case in harness.CONTENT_CHUNK_LEGACY_MESSAGES:
                for stream in (False, True):
                    self.assertTrue(harness.CONTENT_CHUNK_HTTP_GOLDENS[f"{route}:{case}:{stream}"])


class _ContentChunkFixture:
    respond = staticmethod(harness.content_chunk_reply)

    @classmethod
    def setUpClass(cls):
        if not os.environ.get(harness.BINARY_ENV):
            harness.boundary("BOUNDARY: chat conversion proof needs an explicitly selected manifest build")
        from test_gateway_keyed_compat import KeyedTLS
        import test_openai_compat_keyed as keyed
        cls.keyless = harness.GatewayHarness(upstream_factory=lambda path: harness.FakeUpstream(
            path, respond=cls.respond))
        cls.addClassCleanup(cls.keyless.close)
        cls.keyed = KeyedTLS(respond=cls.respond)
        cls.addClassCleanup(cls.keyed.close)
        cls.routes = (("keyless", cls.keyless, "gwtest-compat-plain"),
                      ("keyed", cls.keyed.gateway, keyed.KEYED_KEY))


class MistralContentChunkTests(_ContentChunkFixture, unittest.TestCase):
    def check_chunks(self, stream, row):
        from test_gateway_contracts import reconstructed_blocks
        for route, gateway, alias in self.routes:
            with self.subTest(route=route):
                reply = gateway.request(alias, stream=stream, mode="mistral")
                self.assertEqual(reply.status, 200)
                self.assertEqual(len(reply.hits), 1)
                self.assertTrue(reply.hits[0].path.endswith("/chat/completions"))
                blocks = reconstructed_blocks(reply) if stream else reply.json()["content"]
                self.assertEqual(blocks, [{"type": "thinking", "thinking": "first second"},
                                          {"type": "text", "text": "answer"}])
                if stream:
                    events = reply.events()
                    self.assertEqual(sum(e["type"] == "message_stop" for e in events), 1)
                    self.assertEqual(sum(e["type"] == "content_block_stop" for e in events), 2)
                    delta = next(e for e in events if e["type"] == "message_delta")
                    self.assertEqual(delta["delta"]["stop_reason"], "end_turn")
                    self.assertEqual(delta["usage"], {"input_tokens": 11, "output_tokens": 4})
                else:
                    self.assertEqual(reply.json()["stop_reason"], "end_turn")
                    self.assertEqual(reply.json()["usage"], {"input_tokens": 11, "output_tokens": 4})
        if self._outcome.success:
            record(row, status="passed", routes=2, unsigned_thinking=True, repeated_closed_ignored=True)

    def test_streaming_content_chunks(self):
        self.check_chunks(True, "MC1")

    def test_non_streaming_content_chunks(self):
        self.check_chunks(False, "MC2")


class ContentChunkGoldenTests(_ContentChunkFixture, unittest.TestCase):
    def check_golden(self, gateway, alias, route, case, stream):
        reply = harness.content_chunk_request(gateway, alias, stream=stream, mode=case)
        self.assertEqual(reply.status, 200)
        self.assertEqual(len(reply.hits), 1)
        self.assertEqual(reply.raw, harness.CONTENT_CHUNK_HTTP_GOLDENS[f"{route}:{case}:{stream}"].encode(),
                         f"legacy bytes changed: {route}/{case}/{stream}")

    def test_legacy_chat_bytes(self):
        fields = ("string", "reasoning_content", "reasoning", "reasoning_details")
        cases = 0
        for route, gateway, alias in self.routes:
            # Every compatible shape is tested against every reasoning spelling,
            # not inferred from a provider name or a successful text-only turn.
            for shape in harness.CONTENT_CHUNK_SHAPES:
                for case in fields:
                    with self.subTest(route=route, shape=shape, field=case):
                        self.check_golden(gateway, alias, route, case, True)
                        cases += 1
            for case in harness.CONTENT_CHUNK_LEGACY_MESSAGES:
                for stream in (False, True):
                    with self.subTest(route=route, case=case, stream=stream):
                        self.check_golden(gateway, alias, route, case, stream)
                        cases += 1
        if self._outcome.success:
            record("MG1", status="passed", cases=cases, byte_identical=True,
                   shapes=list(harness.CONTENT_CHUNK_SHAPES), unknown_chunks="legacy-per-path")

    def test_native_route_bytes(self):
        for alias in ("gwtest-cc-think-a", "gwtest-ccb-x", "gwtest-codex-e-none"):
            for stream in (False, True):
                with self.subTest(alias=alias, stream=stream):
                    reply = harness.content_chunk_request(self.keyless, alias, stream=stream)
                    self.assertEqual(reply.status, 200)
                    self.assertEqual(len(reply.hits), 1)
                    self.assertTrue(reply.hits[0].path.endswith(
                        "/responses" if "codex" in alias else "/v1/messages"))
                    self.assertEqual(reply.raw, harness.CONTENT_CHUNK_HTTP_GOLDENS[
                        f"native:{alias}:{stream}"].encode())
        if self._outcome.success:
            record("MG2", status="passed", cases=6, messages_and_responses_untouched=True)


def tool_integrity_deltas(mode):
    """Independent wire stimuli; indexes are absent, not null/zero substitutes."""
    def call(identity=None, name=None, arguments="", index=None):
        value = {"function": {"arguments": arguments}}
        if identity is not None:
            value.update(id=identity, type="function")
        if name is not None:
            value["function"]["name"] = name
        if index is not None:
            value["index"] = index
        return {"tool_calls": [value]}

    a = lambda index=None: call("call_gwtest_a", "gwtest_tool_a", '{"k":"a"}', index)
    b = lambda index=None: call("call_gwtest_b", "gwtest_tool_b", '{"k":"b"}', index)
    if mode == "omitted":
        return [a(), b()]
    if mode == "mixed_forward":
        # Array position 1 and explicit index 1 name different calls.
        return [{"tool_calls": [a(1)["tool_calls"][0], b()["tool_calls"][0]]}]
    if mode == "mixed_reverse":
        return [a(), b(0)]
    if mode == "stable_id":
        # The explicit continuation may not rewrite the announced A identity.
        # Its attempted B rewrite must not make the later omitted B join A.
        return [call("call_gwtest_a", "gwtest_tool_a", '{"k":', 0),
                call("call_gwtest_b", arguments='"a"}', index=0), b()]
    indexes = {"explicit_continue": (0, 0), "omitted_continue": (None, None),
               "omit_to_explicit": (None, 0), "explicit_to_omit": (0, None)}
    first, second = indexes[mode]
    return [call("call_gwtest_a", "gwtest_tool_a", '{"k":', first),
            call(arguments='"a"}', index=second)]


def tool_integrity_reply(path, body, raw, _headers=None):
    """Chat responses only, with literal calls, terminal reasons and usage."""
    if not path.endswith("/chat/completions"):
        return 404, "application/json", b'{"error":"unexpected tool fixture path"}'
    modes = re.findall(rb"gwtest-mode=([a-z0-9_]+)", raw)
    mode = modes[-1].decode() if modes else "omitted"
    base = {"id": "chatcmpl_tool_fixture", "model": body["model"], "created": 1}
    usage = {"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15}
    if not body.get("stream"):
        _, encoding, finish = mode.split("_", 2)
        call = {"id": "call_gwtest_a", "type": "function",
                "function": {"name": "gwtest_tool_a", "arguments": '{"k":"a"}'}}
        message = {"role": "assistant", "content": None, "tool_calls": [call]}
        if encoding == "content":
            message = {"role": "assistant", "content": [{"type": "tool_calls", "tool_calls": [call]}]}
        elif encoding == "text":
            message = {"role": "assistant", "content": "answer"}
        elif encoding != "top":
            raise AssertionError("unknown tool fixture encoding")
        choice = {"index": 0, "message": message}
        if finish != "missing":
            choice["finish_reason"] = {"stop": "stop", "empty": "", "null": None,
                "length": "length", "filter": "content_filter", "tools": "tool_calls"}[finish]
        return 200, "application/json", json.dumps({**base, "object": "chat.completion",
            "choices": [choice], "usage": usage}).encode()
    chunks = [{**base, "object": "chat.completion.chunk", "choices": [
        {"index": 0, "delta": delta, "finish_reason": None}]}
        for delta in [{"role": "assistant"}, *tool_integrity_deltas(mode)]]
    chunks.extend([{**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
                   {**base, "choices": [], "usage": usage}])
    return 200, "text/event-stream", b"".join(
        b"data: " + json.dumps(chunk).encode() + b"\n\n" for chunk in chunks) + b"data: [DONE]\n\n"


def tool_integrity_sse_events(raw):
    """Validate the frozen gateway framing, not the full permissive SSE grammar.

    Each frame has exactly `event: <type>` then one `data: <JSON object>`
    record, followed by an empty terminating line. Comments, id/retry records,
    multiline data and extra empty frames are outside this fixture contract.
    LF and CRLF (also mixed) are accepted; bare CR is never normalized away.
    Escaped CR inside JSON remains payload, parsed by the existing strict loader.
    """
    events, records = [], []
    offset = 0
    while offset < len(raw):
        end = raw.find(b"\n", offset)
        if end < 0:
            raise AssertionError("tool SSE: unterminated line/frame")
        line, offset = raw[offset:end], end + 1
        if line.endswith(b"\r"):
            line = line[:-1]  # consume only the CR belonging to this CRLF
        if b"\r" in line:
            raise AssertionError("tool SSE: bare CR in frame")
        if line:
            records.append(line)
            continue
        if (len(records) != 2 or not records[0].startswith(b"event: ")
                or not records[1].startswith(b"data: ")):
            raise AssertionError("tool SSE: expected one event and one data record per frame")
        label = records[0][7:]
        if not re.fullmatch(rb"[a-z][a-z0-9_]*", label):
            raise AssertionError("tool SSE: invalid event label")
        try:
            event = strict_json.loads(records[1][6:])
        except strict_json.StrictJSONError as error:
            raise AssertionError("tool SSE: invalid JSON payload") from error
        if not isinstance(event, dict) or event.get("type") != label.decode("ascii"):
            raise AssertionError("tool SSE: event label differs from JSON type")
        events.append(event)
        records = []
    if records or not events:
        raise AssertionError("tool SSE: empty stream or incomplete frame boundary")
    return events


class ToolCallIntegrityTests(_ContentChunkFixture, unittest.TestCase):
    """Actual keyed TLS and rendered keyless Messages-to-chat conversions."""
    respond = staticmethod(tool_integrity_reply)
    A = {"type": "tool_use", "id": "call_gwtest_a", "name": "gwtest_tool_a", "input": {"k": "a"}}
    B = {"type": "tool_use", "id": "call_gwtest_b", "name": "gwtest_tool_b", "input": {"k": "b"}}

    def check_reply(self, gateway, alias, mode, *, stream, expected, reason):
        from test_gateway_contracts import reconstructed_blocks

        before = len(gateway.upstream.hits)
        tools = [{"name": name, "input_schema": {"type": "object", "properties": {"k": {"type": "string"}}}}
                 for name in ("gwtest_tool_a", "gwtest_tool_b")]
        reply = gateway.request(alias, stream=stream, mode=mode, body={"tools": tools})
        self.assertEqual(reply.status, 200)
        self.assertEqual(len(reply.hits), 1)
        self.assertEqual(len(gateway.upstream.hits), before + 1, "extra or unattributed fake-upstream request")
        self.assertEqual(reply.hits, gateway.upstream.hits[before:])
        self.assertEqual(reply.hits[0].method, "POST")
        self.assertTrue(reply.hits[0].path.endswith("/chat/completions"))
        self.assertEqual(gateway.upstream.unexpected, 0)
        if stream:
            events = tool_integrity_sse_events(reply.raw)
            self.assertEqual(reply.events(), events, "normalized events differ from validated raw frames")
            self.assertEqual(events[0]["type"], "message_start")
            self.assertIsNone(events[0]["message"]["stop_reason"])
            self.assertEqual(events[0]["message"]["usage"]["output_tokens"], 0)
            self.assertEqual([e["type"] for e in events].count("message_start"), 1)
            self.assertEqual([e["type"] for e in events].count("message_stop"), 1)
            self.assertEqual([e["type"] for e in events[-2:]], ["message_delta", "message_stop"])
            terminal, = [e for e in events if e["type"] == "message_delta"]
            self.assertEqual(terminal["delta"]["stop_reason"], reason)
            self.assertEqual(terminal["usage"], {"input_tokens": 11, "output_tokens": 4})
            opened, indexes, stops = None, [], []
            for event in events:
                if event["type"] == "content_block_start":
                    self.assertIsNone(opened, "overlapping converted blocks")
                    opened = event["index"]
                    indexes.append(opened)
                elif event["type"] == "content_block_delta":
                    self.assertIsNotNone(opened)
                    self.assertEqual(event["index"], opened)
                elif event["type"] == "content_block_stop":
                    self.assertIsNotNone(opened)
                    self.assertEqual(event["index"], opened)
                    stops.append(opened)
                    opened = None
            self.assertIsNone(opened)
            self.assertEqual(indexes, list(range(len(expected))))
            self.assertEqual(stops, indexes)
            blocks = reconstructed_blocks(SimpleNamespace(events=lambda: events))
            for block in blocks:
                if block["type"] == "tool_use":
                    block["input"] = json.loads(block.pop("partial_json"))
        else:
            message = reply.json()
            self.assertEqual(message["stop_reason"], reason)
            self.assertEqual(message["usage"], {"input_tokens": 11, "output_tokens": 4})
            blocks = message["content"]
        self.assertEqual(blocks, expected)

    def test_distinct_omitted_index_calls(self):
        for route, gateway, alias in self.routes:
            for mode in ("omitted", "mixed_forward", "mixed_reverse"):
                with self.subTest(route=route, encoding=mode):
                    self.check_reply(gateway, alias, mode, stream=True, expected=[self.A, self.B], reason="tool_use")
        if self._outcome.success:
            record("TI1", status="passed", routes=2, cases=6, upstream_hits=6, distinct_calls=2)

    def test_announced_tool_identity_is_stable(self):
        for route, gateway, alias in self.routes:
            with self.subTest(route=route):
                self.check_reply(gateway, alias, "stable_id", stream=True, expected=[self.A, self.B], reason="tool_use")
        if self._outcome.success:
            record("TI2", status="passed", routes=2, cases=2, upstream_hits=2, announced_identity_stable=True)

    def test_nonstream_converted_tools_override_ordinary_or_absent_finish(self):
        for route, gateway, alias in self.routes:
            for encoding in ("top", "content"):
                for finish in ("stop", "missing", "empty", "null"):
                    with self.subTest(route=route, encoding=encoding, finish=finish):
                        self.check_reply(gateway, alias, f"terminal_{encoding}_{finish}", stream=False,
                                         expected=[self.A], reason="tool_use")
        if self._outcome.success:
            record("TI3", status="passed", routes=2, encodings=2, cases=16, upstream_hits=16)

    def test_explicit_and_mixed_continuations_remain_one_call(self):
        for route, gateway, alias in self.routes:
            for mode in ("explicit_continue", "omitted_continue", "omit_to_explicit", "explicit_to_omit"):
                with self.subTest(route=route, encoding=mode):
                    self.check_reply(gateway, alias, mode, stream=True, expected=[self.A], reason="tool_use")
        if self._outcome.success:
            record("TIC1", status="passed", routes=2, cases=8, upstream_hits=8, continuation_preserved=True)

    def test_text_only_and_explicit_terminal_precedence_are_unchanged(self):
        cases = [(encoding, finish, reason, [self.A]) for encoding in ("top", "content")
                 for finish, reason in (("length", "max_tokens"), ("filter", "end_turn"), ("tools", "tool_use"))]
        cases += [("text", finish, reason, [{"type": "text", "text": "answer"}]) for finish, reason in (
            ("stop", "end_turn"), ("missing", "end_turn"), ("empty", "end_turn"), ("null", "end_turn"),
            ("length", "max_tokens"), ("filter", "end_turn"))]
        for route, gateway, alias in self.routes:
            for encoding, finish, reason, expected in cases:
                with self.subTest(route=route, encoding=encoding, finish=finish):
                    self.check_reply(gateway, alias, f"terminal_{encoding}_{finish}", stream=False,
                                     expected=expected, reason=reason)
        if self._outcome.success:
            record("TIC2", status="passed", routes=2, cases=24, upstream_hits=24, explicit_precedence=True)


class ToolCallSSEFramingTests(unittest.TestCase):
    """Pure wire controls: no gateway creation, process or serving evidence."""

    def setUp(self):
        self.events = [
            {"type": "message_start", "message": {"id": "msg_fixture", "type": "message", "role": "assistant",
                "model": "fixture", "content": [], "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": 11, "output_tokens": 0}}},
            {"type": "content_block_start", "index": 0, "content_block": {
                "type": "tool_use", "id": "call_gwtest_a", "name": "gwtest_tool_a", "input": {}}},
            {"type": "content_block_delta", "index": 0, "delta": {
                "type": "input_json_delta", "partial_json": '{"k":"a"}'}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                "usage": {"input_tokens": 11, "output_tokens": 4}},
            {"type": "message_stop"},
        ]
        self.frames = [harness.sse_frame(event["type"], event) for event in self.events]
        self.raw = b"".join(self.frames)
        self.helper = ToolCallIntegrityTests("test_distinct_omitted_index_calls")

    def gateway_for(self, raw):
        hit = SimpleNamespace(method="POST", path="/fixture/chat/completions")
        upstream = SimpleNamespace(hits=[], unexpected=0)
        reply = harness.Reply(200, {"content-type": "text/event-stream"}, raw, hits=[hit])
        def request(*_args, **_kwargs):
            upstream.hits.append(hit)
            return reply
        return SimpleNamespace(upstream=upstream, request=request), reply

    def check_serving_helper(self, gateway):
        self.helper.check_reply(gateway, "fixture", "omitted", stream=True,
                                expected=[ToolCallIntegrityTests.A], reason="tool_use")

    def test_lf_crlf_and_mixed_line_endings_preserve_complete_frames(self):
        for raw in (self.raw, self.raw.replace(b"\n", b"\r\n"),
                    self.raw.replace(b"\ndata: ", b"\r\ndata: ")):
            with self.subTest(raw_prefix=raw[:40]):
                self.assertEqual(tool_integrity_sse_events(raw), self.events)
                self.assertEqual(harness.Reply(200, {}, raw).events(), self.events)

    def test_escaped_cr_stays_json_payload_in_lf_and_crlf_frames(self):
        event = {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "before\rafter"}}
        raw = harness.sse_frame(event["type"], event)
        self.assertIn(b"\\r", raw)
        for escaped in (raw, raw.replace(b"\\r", b"\\u000d")):
            for framed in (escaped, escaped.replace(b"\n", b"\r\n")):
                with self.subTest(raw=framed):
                    self.assertEqual(tool_integrity_sse_events(framed), [event])

    def test_missing_merged_premature_and_incomplete_boundaries_are_rejected(self):
        cases = {
            "empty": b"", "merged": self.raw.replace(b"\n\n", b"\n", 1),
            "all_merged": self.raw.replace(b"\n\n", b"\n"),
            "premature": self.raw.replace(b"\ndata: ", b"\n\ndata: ", 1),
            "missing_last_blank": self.raw[:-1], "unterminated_data": self.raw[:-2],
            "unfinished_suffix": self.raw + b"event: message_stop\n",
            "leading_blank": b"\n" + self.raw, "extra_blank": self.raw + b"\n",
            "whitespace_delimiter": self.raw.replace(b"\n\n", b"\n \n", 1),
            "bare_cr_delimiters": self.raw.replace(b"\n", b"\r"),
            "bare_cr_boundary": self.raw.replace(b"\n\n", b"\r\r", 1),
            "cr_cr_lf": self.raw.replace(b"\n", b"\r\r\n"),
            "crlf_missing_last_blank": self.raw.replace(b"\n", b"\r\n")[:-2],
        }
        for name, raw in cases.items():
            with self.subTest(case=name), self.assertRaisesRegex(AssertionError, "tool SSE:"):
                tool_integrity_sse_events(raw)

    def test_each_frame_requires_exactly_one_matching_event_then_one_data_record(self):
        event, data = self.frames[-1][:-2].split(b"\n")
        cases = {
            "missing_event": [data], "missing_data": [event],
            "duplicate_event": [event, event, data], "duplicate_data": [event, data, data],
            "reversed": [data, event], "wrong_label": [b"event: message_delta", data],
            "empty_label": [b"event: ", data], "padded_label": [event + b" ", data],
            "empty_data": [event, b"data: "],
            "missing_event_space": [event.replace(b"event: ", b"event:"), data],
            "missing_data_space": [event, data.replace(b"data: ", b"data:")],
            "comment": [b": keepalive", event, data], "id": [b"id: 1", event, data],
            "retry": [b"retry: 100", event, data],
            "multiline_data": [event, b'data: {"type":', b'data: "message_stop"}'],
        }
        for name, records in cases.items():
            with self.subTest(case=name), self.assertRaisesRegex(AssertionError, "tool SSE:"):
                tool_integrity_sse_events(b"\n".join(records) + b"\n\n")

    def test_malformed_or_ambiguous_json_payloads_are_rejected(self):
        payloads = (b"{", b'{"type":"message_stop"}{}', b'{"type":"message_stop","type":"message_stop"}',
                    b'{"type":"message_stop","n":NaN}', b'{"type":"message_stop","n":1e999}',
                    b'{"type":"message_stop","text":"before\rafter"}', b'{"type":"message_stop","text":"\xff"}',
                    b'{"not_type":"message_stop"}', b'{"type":7}', b"[]", b'"message_stop"', b"null", b"[DONE]")
        for payload in payloads:
            with self.subTest(payload=payload), self.assertRaisesRegex(AssertionError, "tool SSE:"):
                tool_integrity_sse_events(b"event: message_stop\ndata: " + payload + b"\n\n")

    def test_serving_helper_rejects_raw_faults_before_any_normalized_consumer(self):
        from unittest import mock
        import test_gateway_contracts as contracts

        for raw in (self.raw.replace(b"\n\n", b"\n", 1),
                    self.raw.replace(b"event: message_start", b"event: message_delta", 1)):
            gateway, reply = self.gateway_for(raw)
            reply.events = mock.Mock(side_effect=AssertionError("normalized too soon"))
            with self.subTest(raw_prefix=raw[:40]), mock.patch.object(contracts, "reconstructed_blocks") as reconstruct:
                with self.assertRaisesRegex(AssertionError, "tool SSE:"):
                    self.check_serving_helper(gateway)
                reply.events.assert_not_called()
                reconstruct.assert_not_called()

    def test_serving_helper_rejects_a_different_normalized_interpretation(self):
        from unittest import mock
        import test_gateway_contracts as contracts

        gateway, reply = self.gateway_for(self.raw)
        reply.events = mock.Mock(return_value=list(reversed(self.events)))
        with mock.patch.object(contracts, "reconstructed_blocks") as reconstruct:
            with self.assertRaisesRegex(AssertionError, "normalized events differ"):
                self.check_serving_helper(gateway)
            reconstruct.assert_not_called()

    def test_serving_helper_reconstructs_only_the_validated_events(self):
        from unittest import mock

        for raw in (self.raw, self.raw.replace(b"\n", b"\r\n")):
            gateway, reply = self.gateway_for(raw)
            reply.events = mock.Mock(wraps=reply.events)
            with self.subTest(raw_prefix=raw[:40]):
                self.check_serving_helper(gateway)
                # Reconstruction receives validated events, not a second parse
                # through Reply.events(), whose boundaries/labels are lossy.
                reply.events.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
