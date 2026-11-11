"""Gateway-in-the-loop rows for the surfaces charmer decides on the 8.0.67 pin.

The upstream surfaces new since 8.0.45, each decided rather than inherited,
and refusals as the client now receives them, against disposable network
namespaces, fake upstreams and fixture credentials only. Every row parses the
rendered YAML (the shipped catalog's real render, the running gateway's own
config file, or both); none matches raw YAML text.

Rows (table ``surfaces``):

- KV1  the key-value store is off: no database file, a control creates one.
- MV1  ``POST /v8/management/credentials/verify`` is refused before the key
       check, CORS and the body.
- PL1  plugins and the plugin host are off: a plugin file is never loaded.
- OA1  per-credential connection pools absent.
- OA2  native client entrypoint overrides absent.
- OA3  visible Codex commentary off (hidden commentary stays the default).
- OA4  session-affinity rebinding not rendered (upstream's default applies).
- OA5  sensitive-word replacement absent.
- OA6  server-side Claude fallback off (upstream fa11a573 made it opt-in).
- RT1  compression, passthrough, commercial mode, no-egress, retry and
       cooling as rendered today.
- SB1  the model-substitution warning still parses exactly.
- RF1  a stream refusal, RF2 a non-stream refusal (HTTP 400 since 8.0.56,
       03a8743c), RF3 a mid-stream policy block (400-classified, c3938b72):
       the exact client-visible shapes. The refusal fixture entry points
       (``refusal_gateway``, ``refusal_request``, ``expected_refusal_reply``,
       ``refusal_upstream``, ``REFUSAL_CASES``) are stable for client-side
       rows that drive the same refusals.
"""
import copy
import datetime
import functools
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from charmer import catalog, gateway_events, probe, proxy, render, served_plan, state
from charmer.cli import gateway_facts
from charmer.platform import file_log
from _catalog import SHIPPED_ROOT, uses_shipped_catalog
import _gateway_harness as harness

MODULE = "tests.test_gateway_surfaces"
TABLE = "surfaces"

CLAUDE_KEY_ALIAS = "gwtest-cc-output-config-high"
CODEX_ALIAS = "gwtest-codex-reasoning-effort-high"
COMPAT_ALIAS = "gwtest-compat-plain"
MANAGEMENT_KEY = "dummy-gwtest-mgmt"
CLIENT_ENCODINGS = "gzip, deflate, br, zstd"  # what the managed client sends
# Every per-credential section the gateway reads (charmer renders three).
CREDENTIAL_SECTIONS = ("claude-api-key", "codex-api-key", "openai-compatibility", "gemini-api-key",
                       "interactions-api-key", "vertex-api-key", "xai-api-key", "meta-api-key")


def setUpModule():
    # Close the shared gateway fixture with this module, before tools/test.py
    # removes its private workspace; atexit would run after that removal.
    unittest.addModuleCleanup(harness.close_main_harness)


def tearDownModule():
    harness.EVIDENCE.finalize_module(MODULE)


def record(case, row, **fields):
    """Evidence for ``row``, only once ``case`` (the running test) has passed
    every check, its subtests included."""
    if case._outcome.success:
        harness.EVIDENCE.record(MODULE, TABLE, {"row": row, **fields})


def require_selected_build(row):
    # An installed gateway may predate the pin: never fall back to it.
    if not os.environ.get(harness.BINARY_ENV):
        harness.boundary("BOUNDARY: " + row + " needs " + harness.BINARY_ENV + " set to the manifest build")


# ---------------------------------------------------------------- documents
def shipped_static():
    """The shipped catalog's ``cliproxy_static`` block (a fresh copy)."""
    return json.loads((SHIPPED_ROOT / "catalog/gateway.json").read_text())["gateway"]["cliproxy_static"]


@functools.lru_cache(maxsize=1)
def _shipped_render_yaml():
    bundle = catalog.load_catalog(SHIPPED_ROOT)
    return render.render_config(
        bundle.docs["gateway"], bundle.docs["providers"]["providers"], bundle.lines,
        home=Path("/home/gwtest-surfaces"), gateway_token=harness.FIXTURE_GATEWAY_TOKEN,
        resolve_secret=lambda _name: "dummy-gwtest-surfaces", continuity={},
    ).yaml


def shipped_rendered():
    """The shipped catalog's real render (every keyed provider credentialed),
    parsed back from the YAML the renderer writes."""
    return served_plan.parse_restricted_yaml(_shipped_render_yaml())


def rendered(gateway):
    """The config file the running gateway loaded, parsed."""
    return served_plan.parse_restricted_yaml(gateway.config.read_text())


def keys_anywhere(value):
    """Every mapping key at any depth of a parsed document."""
    found = set()
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            found.update(item)
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return found


def credential_entries(document):
    """Every per-credential mapping: each section and its api-key entries."""
    for key in CREDENTIAL_SECTIONS:
        for section in document.get(key) or ():
            yield key, section
            for entry in section.get("api-key-entries") or ():
                yield key + ".api-key-entries", entry


def documents(gateway):
    """(label, parsed document) for the shipped render and the running config."""
    return (("shipped", shipped_rendered()), ("running", rendered(gateway)))


def revised(document, **changes):
    from test_gateway_v8_traffic import revised as revise
    return revise(document, **changes)


def without(document, key):
    """A finalized copy of ``document`` with top-level ``key`` absent."""
    document = copy.deepcopy(document)
    document.pop(key)
    return revised(document)


def reload_document(gateway, document):
    from test_gateway_v8_traffic import reload_document as reload
    reload(gateway, document)


def restart(gateway, document):
    """Stop this harness's gateway and start the same binary again on
    ``document`` (a fresh ingress socket; the stopped bridge leaves its
    fixture socket inode behind)."""
    probe.stop_isolated_process(gateway.process)
    state.atomic_write(gateway.config, render.emit_yaml(document).encode())
    gateway.document = document
    gateway.socket = gateway.run / ("restart-" + uuid.uuid4().hex[:12] + ".sock")
    spec = json.loads(harness.SERVICE_SPEC.read_text())
    view = probe.HomeView(gateway.home, tuple(gateway.home / p for p in spec["bind_rw"]),
                          tuple(gateway.home / p for p in spec["bind_ro"]))
    with gateway.log_path.open("ab") as log:
        gateway.process = probe.start_isolated_process(
            [gateway.binary, "--config", str(gateway.config), "--local-model"],
            env={"HOME": str(gateway.home), "PATH": "/usr/bin:/bin"},
            cwd=gateway.home / spec["working_directory"], home_view=view,
            bridges=[probe.PortBridge(gateway.egress, gateway.run / "upstream.sock", "egress"),
                     probe.PortBridge(gateway.port, gateway.socket, "ingress")],
            stdout=log, stderr=subprocess.STDOUT)
    gateway.await_ready()


def inventory(root):
    """Every non-directory entry below ``root``, as relative paths."""
    found = set()
    for directory, _dirs, files in os.walk(root):
        for name in files:
            found.add(str((Path(directory) / name).relative_to(root)))
    return found


def harness_written(gateway):
    """What the harness itself writes below its root: the config, the
    gateway's log and the two bridge sockets."""
    return {str(path.relative_to(gateway.root)) for path in (
        gateway.config, gateway.log_path, gateway.socket, gateway.run / "upstream.sock")}


def wait_for(predicate, *, deadline=5.0):
    ends = time.monotonic() + deadline
    while True:
        value = predicate()
        if value or time.monotonic() >= ends:
            return value
        time.sleep(0.05)


def ordinary_traffic(gateway):
    """One request per rendered route family, both modes; all answered."""
    count = 0
    for alias in (CLAUDE_KEY_ALIAS, CODEX_ALIAS, COMPAT_ALIAS):
        for stream in (False, True):
            reply = gateway.request(alias, stream=stream)
            if reply.status != 200 or len(reply.hits) != 1:
                raise AssertionError(gateway.diagnostic(f"ordinary traffic failed on {alias}"))
            count += 1
    return count


# ---------------------------------------------------------------- KV1
@uses_shipped_catalog
class KeyValueStoreTests(unittest.TestCase):
    """Upstream 8.0.55 (ec14dc47) opens a bbolt database at
    ``<auth-dir>/kv.db`` unless ``kv-store.disabled`` is true. charmer
    renders it disabled, so the gateway's caches stay in memory and the auth
    directory holds credentials only."""

    def test_kv_store_disabled_leaves_no_database(self):  # KV1
        require_selected_build("KV1")
        self.assertEqual(shipped_static()["kv-store"], {"disabled": True})
        self.assertEqual(shipped_rendered()["kv-store"], {"disabled": True})
        gateway = harness.GatewayHarness()
        self.addCleanup(gateway.close)
        document = rendered(gateway)
        self.assertEqual(document["kv-store"], {"disabled": True})
        auth = Path(document["auth-dir"])
        database = auth / "kv.db"
        written = harness_written(gateway)
        self.assertEqual(inventory(gateway.root), written, "the gateway wrote a file at startup")
        requests = ordinary_traffic(gateway)
        # A reload that keeps the store disabled (a second gateway key
        # changes the sentinel), then traffic again.
        reload_document(gateway, revised(gateway.document, **{
            "api-keys": [harness.FIXTURE_GATEWAY_TOKEN, "gwtest-second-" + uuid.uuid4().hex]}))
        self.assertEqual(rendered(gateway)["kv-store"], {"disabled": True})
        requests += ordinary_traffic(gateway)
        self.assertEqual(inventory(gateway.root), written, "traffic or a reload wrote a file")
        self.assertFalse(database.exists())
        # Control, reload path: the same gateway with the key absent opens
        # the database file at once.
        absent = without(gateway.document, "kv-store")
        reload_document(gateway, absent)
        self.assertNotIn("kv-store", rendered(gateway))
        self.assertTrue(wait_for(database.exists), "the key-absent reload created no database")
        self.assertEqual(inventory(gateway.root) - written, {str(database.relative_to(gateway.root))})
        # Control, startup path: the same gateway restarted with the key
        # absent creates it again before it serves.
        database.unlink()
        restart(gateway, absent)
        self.assertTrue(database.exists(), "the key-absent start created no database")
        record(self, "KV1", status="passed", rendered_disabled=True, requests=requests,
               reloads=1, new_files=0, control_reload_created=True, control_start_created=True)


# ---------------------------------------------------------------- MV1
def response_head(data):
    """(status, lower-cased headers) of the first HTTP response in ``data``."""
    head = data.split(b"\r\n\r\n", 1)[0].decode("latin-1")
    status, *lines = head.split("\r\n")
    return int(status.split()[1]), {name.strip().lower(): value.strip()
                                    for name, value in (line.split(":", 1) for line in lines if ":" in line)}


def cors_headers(headers):
    return sorted(name for name in headers if name.startswith("access-control-"))


class CredentialVerifyRouteTests(unittest.TestCase):
    """8.0.48 (514d09f7) registers ``POST /v8/management/credentials/verify``.
    The read-only allowlist refuses it like every other v8 management route:
    a bare 404 before the management key is read (so no attempt counts
    toward the five-strike ban), before CORS (no Access-Control-* headers,
    preflight included) and before the body (an unread body closes the
    connection, as TestManagementAllowlist_RefusedUnreadBodyClosesConnection
    pins in Go)."""

    PATH = "/v8/management/credentials/verify"

    @classmethod
    def setUpClass(cls):
        require_selected_build("MV1")
        cls.gateway = harness.GatewayHarness(management=True)
        cls.addClassCleanup(cls.gateway.close)

    def request(self, method, path, *, body=b'{"name":"gwtest-credential.json"}', **headers):
        headers = {"Host": f"127.0.0.1:{self.gateway.port}", "Content-Type": "application/json", **headers}
        return harness.unix_request(self.gateway.socket, method, path, headers=headers, body=body)

    def assert_refused(self, reply, label):
        self.assertEqual((reply.status, reply.raw), (404, b""), label)
        self.assertEqual(cors_headers(reply.headers), [], label)
        self.assertNotIn("location", reply.headers, label)
        self.refused += 1

    def allowlisted(self, key=MANAGEMENT_KEY):
        return harness.unix_request(self.gateway.socket, "GET", "/v0/management/auth-files", headers={
            "Host": f"127.0.0.1:{self.gateway.port}", "X-Management-Key": key})

    def test_credentials_verify_is_refused_before_key_cors_and_body(self):  # MV1
        self.refused = 0
        cases = {"no key": {}, "valid key": {"X-Management-Key": MANAGEMENT_KEY},
                 "valid bearer": {"Authorization": "Bearer " + MANAGEMENT_KEY},
                 "browser origin": {"X-Management-Key": MANAGEMENT_KEY, "Origin": "http://gwtest.invalid"}}
        for label, headers in cases.items():
            reply = self.request("POST", self.PATH, **headers)
            self.assert_refused(reply, label)
            # The refusal never reads the body, so the connection is closed.
            self.assertEqual(reply.headers.get("connection"), "close", label)
        # Trailing-slash spelling: the same bare 404, never Gin's redirect.
        self.assert_refused(self.request("POST", self.PATH + "/", **{"X-Management-Key": MANAGEMENT_KEY}),
                            "trailing slash")
        # A CORS preflight is refused too, with no CORS header; the inference
        # route's preflight is the control that CORS itself is live.
        preflight = {"Origin": "http://gwtest.invalid", "Access-Control-Request-Method": "POST",
                     "Access-Control-Request-Headers": "X-Management-Key, Content-Type"}
        self.assert_refused(self.request("OPTIONS", self.PATH, body=None, **preflight), "preflight")
        control = self.request("OPTIONS", "/v1/messages", body=None, **preflight)
        self.assertEqual(control.status, 204)
        self.assertIn("access-control-allow-origin", control.headers)
        # Five (and a sixth) bad keys never reach the attempt counter: the
        # valid key is still served on an allowlisted route afterwards.
        for attempt in range(6):
            self.assert_refused(self.request("POST", self.PATH, **{"X-Management-Key": f"gwtest-bad-key-{attempt}"}),
                                f"bad key {attempt}")
        self.assertEqual(self.allowlisted().status, 200, "the refused route counted toward the ban")
        # A large body announced and never sent: the gateway answers and
        # closes at once, without waiting for the body.
        connection = probe.unix_connect(self.gateway.socket, timeout=5)
        try:
            started = time.monotonic()
            connection.sendall((f"POST {self.PATH} HTTP/1.1\r\nHost: 127.0.0.1:{self.gateway.port}\r\n"
                                f"X-Management-Key: {MANAGEMENT_KEY}\r\nContent-Type: application/json\r\n"
                                f"Content-Length: {1 << 20}\r\n\r\n").encode() + b'{"partial":')
            received, closed = b"", False
            while (remaining := 5.0 - (time.monotonic() - started)) > 0:
                connection.settimeout(remaining)
                try:
                    chunk = connection.recv(65536)
                except (socket.timeout, TimeoutError):
                    break
                if not chunk:
                    closed = True
                    break
                received += chunk
            elapsed = time.monotonic() - started
        finally:
            connection.close()
        status, headers = response_head(received)
        self.assertEqual((status, headers.get("connection"), headers.get("content-length")), (404, "close", "0"))
        self.assertEqual(cors_headers(headers), [])
        self.assertTrue(closed, "the connection stayed open behind an unread body")
        self.assertLess(elapsed, 5.0)
        # Control: a refused request without a body keeps its connection,
        # which then serves the next request.
        connection = probe.unix_connect(self.gateway.socket, timeout=5)
        try:
            connection.sendall((f"GET /v8/management/credentials HTTP/1.1\r\nHost: 127.0.0.1:{self.gateway.port}\r\n"
                                f"X-Management-Key: {MANAGEMENT_KEY}\r\n\r\n").encode())
            first = b""
            while b"\r\n\r\n" not in first:
                chunk = connection.recv(65536)
                self.assertTrue(chunk, "the gateway closed a refused request without a body")
                first += chunk
            status, headers = response_head(first)
            self.assertEqual((status, headers.get("connection"), headers.get("content-length")), (404, None, "0"))
            connection.sendall(f"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1:{self.gateway.port}\r\n\r\n".encode())
            second = b""
            while b"\r\n\r\n" not in second:
                chunk = connection.recv(65536)
                self.assertTrue(chunk, "the kept connection did not serve the next request")
                second += chunk
            self.assertEqual(response_head(second)[0], 200)
        finally:
            connection.close()
        self.assertEqual(self.allowlisted().status, 200)
        # Discriminator, last (it bans this class's gateway): five bad keys on
        # an allowlisted route do count, and the valid key is then refused.
        bad = [self.allowlisted(f"gwtest-bad-key-{attempt}").status for attempt in range(5)]
        self.assertEqual(bad, [401] * 5)
        banned = self.allowlisted()
        self.assertEqual(banned.status, 403)
        self.assertIn(b"IP banned", banned.raw)
        record(self, "MV1", status="passed", refused=self.refused, bad_keys=6, counted=False, cors_headers=0,
               preflight=404, unread_body_closed=True, close_seconds=round(elapsed, 3),
               keepalive_control=True, ban_control=banned.status)


# ---------------------------------------------------------------- PL1
PLUGIN_ID = "gwtestplugin"
PLUGIN_ROUTES = (("GET", "/v0/management/plugins"), ("GET", "/v8/management/plugins"),
                 ("GET", f"/v0/management/plugins/{PLUGIN_ID}/config"),
                 ("PATCH", f"/v0/management/plugins/{PLUGIN_ID}/enabled"),
                 ("GET", "/v0/management/plugin-store"), ("POST", f"/v0/management/plugin-store/{PLUGIN_ID}/install"),
                 ("GET", f"/v0/management/{PLUGIN_ID}/status"), ("GET", f"/v0/resource/plugins/{PLUGIN_ID}/index.html"))


def plugin_document(enabled):
    """The fixture render with a plugin file in the plugin directory the
    gateway resolves for ``dir: ""`` (``plugins`` below its working
    directory); ``enabled`` also turns the host and that plugin on (the
    discriminator)."""
    def factory(bundle, egress, port, home):
        document, aliases, info = harness.build_document(bundle, egress, port, home)
        plugins = home / json.loads(harness.SERVICE_SPEC.read_text())["working_directory"] / "plugins"
        state.ensure_private_dir(plugins)
        # Not a shared object (and not executable): a load attempt fails.
        state.atomic_write(plugins / (PLUGIN_ID + ".so"), b"gwtest plugin fixture, not a shared object\n")
        if enabled:
            document = revised(document, plugins={"enabled": True, "dir": "", "configs": {PLUGIN_ID: {"enabled": True}}})
        return document, aliases, info
    return factory


@uses_shipped_catalog
class PluginHostTests(unittest.TestCase):
    """Plugins stay off as rendered: a plugin file in the gateway's plugin
    directory is never loaded (no plugin host line at all) and every plugin
    management and resource route is a bare 404 with the valid key. The
    control turns the host and the plugin on: the host then tries to load
    the file (this build has no dynamic loader, so it logs the failure),
    and the routes stay refused by the allowlist."""

    def test_plugins_off_and_a_plugin_file_is_never_loaded(self):  # PL1
        require_selected_build("PL1")
        off = {"enabled": False, "dir": "", "configs": {}}
        self.assertEqual(shipped_static()["plugins"], off)
        self.assertEqual(shipped_rendered()["plugins"], off)
        rows = {}
        # Off as rendered first; the control (host and plugin on) second.
        for enabled in (False, True):
            gateway = harness.GatewayHarness(management=True, document_factory=plugin_document(enabled))
            try:
                if not enabled:
                    self.assertEqual(rendered(gateway)["plugins"], off)

                def plugin_lines():
                    return [line for line in gateway.log_path.read_text(errors="replace").splitlines()
                            if "plugin" in line.lower()]
                if enabled:
                    lines = wait_for(plugin_lines)
                    self.assertTrue(any(f"pluginhost: failed to load plugin {PLUGIN_ID}" in line for line in lines),
                                    "the discriminator saw no plugin load")
                else:
                    time.sleep(1.0)  # past the plugin host's start on a gateway that runs one
                    lines = plugin_lines()
                    self.assertEqual(lines, [], "the plugin host logged with plugins off")
                host = {"Host": f"127.0.0.1:{gateway.port}", "X-Management-Key": MANAGEMENT_KEY}
                refused = 0
                for method, path in PLUGIN_ROUTES:
                    reply = harness.unix_request(gateway.socket, method, path, headers=host,
                                                 body=b"{}" if method in ("POST", "PATCH") else None)
                    self.assertEqual((reply.status, reply.raw), (404, b""), f"{method} {path} (enabled={enabled})")
                    refused += 1
                # Management itself is live: the refusals are the guard's.
                self.assertEqual(harness.unix_request(gateway.socket, "GET", "/v0/management/auth-files",
                                                      headers=host).status, 200)
                rows[enabled] = {"plugin_log_lines": len(lines), "refused": refused}
            finally:
                gateway.close()
        record(self, "PL1", status="passed", rendered_enabled=False, plugin_log_lines=rows[False]["plugin_log_lines"],
               routes_refused=rows[False]["refused"], control_plugin_log_lines=rows[True]["plugin_log_lines"],
               control_routes_refused=rows[True]["refused"])


# ---------------------------------------------------------------- OA1-OA6
def commentary_upstream(path, body, raw, _headers=None):
    """A Codex Responses stream carrying a commentary item, then the answer."""
    from test_gateway_v8_traffic import codex_commentary_reply
    if path.endswith("/responses"):
        return codex_commentary_reply(body.get("model", "gwtest-wire"), "GWTEST-COMMENTARY", "GWTEST-FINAL")
    return harness.fake_reply(path, body, raw)


@uses_shipped_catalog
class OptInSurfaceTests(unittest.TestCase):
    """Upstream surfaces that charmer leaves absent or off, one row each.
    A row with a client-visible behaviour also runs it, with a control that
    turns the surface on; the others are rendered-document rows."""

    def test_connection_pools_absent(self):  # OA1
        # 8.0.50 (bf834311 and others): a connection-pool block on a
        # credential is the opt-in. Measured on 8.0.67, the rendered Claude
        # key route reuses one upstream connection for four requests with or
        # without a block, so no client-visible behaviour discriminates.
        gateway = harness.main_harness()
        self.assertNotIn("connection-pool", keys_anywhere(shipped_static()))
        entries = 0
        for label, document in documents(gateway):
            with self.subTest(document=label):
                self.assertNotIn("connection-pool", keys_anywhere(document))
                self.assertNotIn("antigravity", document)
                for section, entry in credential_entries(document):
                    self.assertNotIn("connection-pool", entry, section)
                    entries += 1
        self.assertGreater(entries, 0)
        record(self, "OA1", status="passed", credential_entries=entries, pool_keys=0, behaviour="none-discriminable")

    def test_native_entrypoint_overrides_absent(self):  # OA2
        # 8.0.53 (6d4102f3): claude-header-defaults.native-entrypoints
        # extends the built-in native Claude Code entrypoint allowlist. Native
        # recognition needs the pinned client's exact request shape (the hint
        # client lanes drive it), so this is a rendered-document row.
        gateway = harness.main_harness()
        for label, document in documents(gateway):
            with self.subTest(document=label):
                self.assertNotIn("native-entrypoints", keys_anywhere(document))
                self.assertNotIn("claude-header-defaults", document)
        self.assertNotIn("native-entrypoints", keys_anywhere(shipped_static()))
        record(self, "OA2", status="passed", native_entrypoints=0, header_defaults=False)

    def test_visible_codex_commentary_off(self):  # OA3
        # 8.0.49 (cc4b9949): client.claude.show-codex-commentary mirrors Codex
        # commentary into visible text. Off, the client receives it only as
        # the marked thinking block the gateway restores on replay.
        require_selected_build("OA3")
        from test_gateway_contracts import reconstructed_blocks
        from test_gateway_v8_traffic import COMMENTARY_MARKER
        gateway = harness.GatewayHarness(upstream_factory=lambda path: harness.FakeUpstream(path, commentary_upstream))
        self.addCleanup(gateway.close)
        for label, document in documents(gateway):
            with self.subTest(document=label):
                self.assertNotIn("client", document)
                self.assertNotIn("show-codex-commentary", keys_anywhere(document))
        thinking = {"type": "thinking", "thinking": "GWTEST-COMMENTARY", "signature": COMMENTARY_MARKER}
        final = {"type": "text", "text": "GWTEST-FINAL"}
        expected = {False: [thinking, final], True: [thinking, {"type": "text", "text": "GWTEST-COMMENTARY"}, final]}
        for visible in (False, True):
            if visible:
                # The control: the same gateway with the opt-in on.
                reload_document(gateway, revised(gateway.document, client={"claude": {"show-codex-commentary": True}}))
            for stream in (False, True):
                with self.subTest(visible=visible, stream=stream):
                    reply = gateway.request(CODEX_ALIAS, stream=stream)
                    self.assertEqual(reply.status, 200, reply.raw[:300])
                    self.assertEqual(len(reply.hits), 1)
                    blocks = reconstructed_blocks(reply) if stream else reply.json()["content"]
                    self.assertEqual(blocks, expected[visible])
        record(self, "OA3", status="passed", rendered=False, hidden_blocks=["thinking", "text"],
               control_blocks=["thinking", "text", "text"], modes=2)

    def test_session_affinity_rebind_not_rendered(self):  # OA4
        # 8.0.61: routing.session-affinity-rebind-on (any | quota | never)
        # stays unrendered, so upstream's default, any, applies unchanged.
        # Rebinding needs a second credential behind one bound session and a
        # failing first one; a rendered-document row.
        gateway = harness.main_harness()
        routing = {"strategy": "fill-first", "session-affinity": True, "session-affinity-ttl": "24h"}
        self.assertEqual(shipped_static()["routing"], routing)
        for label, document in documents(gateway):
            with self.subTest(document=label):
                self.assertEqual(document["routing"], routing)
                self.assertNotIn("session-affinity-rebind-on", keys_anywhere(document))
        record(self, "OA4", status="passed", rebind_on="unrendered", effective="any", strategy="fill-first")

    def test_sensitive_word_replacement_absent(self):  # OA5
        # 8.0.65: sensitive-words / sensitive-words-replace (a credential's
        # cloak block, the provider-wide devin and antigravity blocks). The
        # replacement only runs on cloaked requests from words a config or
        # credential names; a rendered-document row.
        gateway = harness.main_harness()
        sections = 0
        for label, document in documents(gateway):
            with self.subTest(document=label):
                keys = keys_anywhere(document)
                self.assertFalse({"sensitive-words", "sensitive-words-replace"} & keys)
                self.assertFalse({"devin", "antigravity"} & set(document))
                for section in document.get("claude-api-key") or ():
                    self.assertNotIn("cloak", section)
                    sections += 1
        self.assertGreater(sections, 0)
        record(self, "OA5", status="passed", sensitive_word_keys=0, cloak_blocks=0, claude_sections=sections)

    def test_server_side_claude_fallback_off(self):  # OA6
        # 8.0.66 (fa11a573): claude.server-side-fallback is opt-in, default
        # false. On the Claude OAuth route a cloaked request is sent without
        # injected fallbacks; with the opt-in on (the control) the gateway
        # injects them.
        require_selected_build("OA6")
        if shutil.which("openssl", path="/usr/bin:/bin") is None:
            harness.boundary("BOUNDARY: openssl is needed for the fixture TLS upstream")
        import test_gateway_hint_client as recognition
        from test_gateway_v8_traffic import post, request_body
        sent = []

        def respond(route_path, body):
            if route_path.endswith("/count_tokens"):
                return 200, b'{"input_tokens":1}', "application/json"
            sent.append(body)
            payload = probe._message_payload([{"type": "text", "text": "FALLBACK-PROBE"}], model=body["model"],
                                             stop_reason="end_turn", message_id="msg_gwtest_fallback")
            return 200, json.dumps(payload).encode(), "application/json"

        shipped = shipped_rendered()
        self.assertNotIn("server-side-fallback", keys_anywhere(shipped))
        self.assertNotIn("claude", shipped)
        root = Path(tempfile.mkdtemp(prefix="gwtest-surfaces-fallback-"))
        self.addCleanup(shutil.rmtree, root, True)
        gateway = recognition.start_oauth_gateway(root, respond)
        self.addCleanup(gateway.close)
        document = rendered(gateway)
        self.assertNotIn("server-side-fallback", keys_anywhere(document))
        self.assertNotIn("claude", document)
        injected = {"claude-opus-5-5": [{"model": "claude-opus-4-8"}], "claude-fable-5-1": [{"model": "claude-opus-5"}]}
        aliases = {entry["name"]: entry["alias"] for entry in gateway.document["oauth-model-alias"]["claude"]
                   if entry["name"] in injected}
        self.assertEqual(set(aliases), set(injected), "the shipped Claude pool lost a fallback-eligible model")
        observed = {}
        for enabled in (False, True):
            if enabled:
                reload_document(gateway, revised(gateway.document, claude={"server-side-fallback": True}))
            for model, alias in sorted(aliases.items()):
                with self.subTest(enabled=enabled, model=model):
                    before = len(sent)
                    reply = post(gateway, request_body(alias, messages=[{"role": "user", "content": "gwtest fallback"}]))
                    self.assertEqual(reply.status, 200, reply.raw[:300])
                    bodies = sent[before:]
                    self.assertEqual([body["model"] for body in bodies], [model])
                    if enabled:
                        self.assertEqual(bodies[0].get("fallbacks"), injected[model])
                    else:
                        self.assertNotIn("fallbacks", bodies[0])
                    observed[(enabled, model)] = "fallbacks" in bodies[0]
        record(self, "OA6", status="passed", rendered=False, models=sorted(aliases),
               injected_off=[observed[(False, model)] for model in sorted(aliases)],
               injected_control=[observed[(True, model)] for model in sorted(aliases)])


# ---------------------------------------------------------------- RT1
def unavailable_upstream(path, body, raw, _headers=None):
    """A transient 503 for ``gwtest-mode=unavailable``, otherwise the fake."""
    if b"gwtest-mode=unavailable" in raw and path.endswith("/v1/messages"):
        return 503, "application/json", json.dumps({"type": "error", "error": {
            "type": "overloaded_error", "message": "fixture unavailable"}}).encode()
    return harness.fake_reply(path, body, raw)


@uses_shipped_catalog
class RenderedTodayTests(unittest.TestCase):
    """Settings rendered as they were before the 8.0.67 pin, from the shipped
    render and the running config, with their behaviour: plain downstream
    bytes and no WebSocket compression, local-model mode (no updater egress),
    passthrough off, and one upstream request per transient failure with the
    credential never cooled. Retry and cooling each have a control (a reload
    that turns them on) on the same gateway; the compression control is
    RenderedCompressionTrafficTests (test_gateway_v8_traffic)."""

    STATIC = {
        "response-compression": {"enabled": False},
        "codex": {"websocket-compression": False, "upstream-request-compression": False},
        "passthrough-headers": False,
        "commercial-mode": True,
        "request-retry": 0,
        "max-retry-credentials": 1,
        "max-retry-interval": 0,
        "disable-cooling": True,
        "save-cooldown-status": False,
    }

    def test_compression_passthrough_commercial_egress_retry_and_cooling(self):  # RT1
        require_selected_build("RT1")
        from test_gateway_v8_traffic import ResponsesSocket
        static = shipped_static()
        self.assertEqual({key: static[key] for key in self.STATIC}, self.STATIC)
        gateway = harness.GatewayHarness(upstream_factory=lambda path: harness.FakeUpstream(path, unavailable_upstream))
        self.addCleanup(gateway.close)
        entries = 0
        for label, document in documents(gateway):
            with self.subTest(document=label):
                self.assertEqual({key: document.get(key) for key in self.STATIC}, self.STATIC)
                # Cooling stays off for every credential: no per-credential
                # override re-enables it, and no key route retries.
                for section, entry in credential_entries(document):
                    self.assertIs(entry.get("disable-cooling", True), True, section)
                    self.assertEqual(entry.get("request-retry", 0), 0, section)
                    entries += 1
        # No-egress: the service runs the gateway in local-model mode, which
        # starts no version or catalog updater.
        self.assertIn("--local-model", proxy.EXEC_ARGV_FLAGS)
        log = gateway.log_path.read_text(errors="replace")
        self.assertIn("local mode: background version and asset updaters are not started", log)
        self.assertNotIn("periodic antigravity version refresh started", log)
        # Compression off: plain bytes for a client that offers every
        # encoding, and no WebSocket permessage-deflate.
        encodings = set()
        for alias in (CLAUDE_KEY_ALIAS, CODEX_ALIAS):
            for stream in (False, True):
                reply = gateway.request(alias, stream=stream, headers={"Accept-Encoding": CLIENT_ENCODINGS})
                self.assertEqual(reply.status, 200)
                encodings.add(reply.headers.get("content-encoding", "identity"))
        self.assertEqual(encodings, {"identity"})
        connection = ResponsesSocket(gateway, harness.FIXTURE_GATEWAY_TOKEN)
        try:
            self.assertEqual(connection.status, 101)
            self.assertNotIn("sec-websocket-extensions", connection.headers)
        finally:
            connection.close()
        # Retry 0, one credential per round, cooling disabled: a transient
        # upstream 503 is one upstream request, and the next request reaches
        # the same credential's upstream at once. Passthrough off: the fake
        # upstream's marker header never reaches the client (the harness
        # checks it on every request).
        def transient_hits():
            reply = gateway.request(CLAUDE_KEY_ALIAS, mode="unavailable")
            self.assertGreaterEqual(reply.status, 500)
            self.assertNotIn(harness.MARKER, reply.headers)
            return reply.status, len(reply.hits)

        statuses = []
        for _attempt in range(2):
            status, hits = transient_hits()
            self.assertEqual(hits, 1, "a retry or a cooled credential")
            statuses.append(status)
        self.assertEqual(gateway.request(CLAUDE_KEY_ALIAS).status, 200)
        # Controls on the same gateway. Two retry rounds: the transient 503
        # is sent three times.
        reload_document(gateway, revised(gateway.document, **{"request-retry": 2}))
        retried = [transient_hits()[1] for _attempt in range(2)]
        self.assertEqual(retried, [3, 3])
        # Cooling on (no retry): the 503 cools the only credential, and the
        # next requests never reach the upstream.
        reload_document(gateway, revised(gateway.document, **{"request-retry": 0, "disable-cooling": False}))
        cooled = [transient_hits()[1] for _attempt in range(2)]
        self.assertEqual(cooled, [1, 0])
        self.assertEqual(len(gateway.request(CLAUDE_KEY_ALIAS).hits), 0)
        record(self, "RT1", status="passed", credential_entries=entries, downstream_encodings=sorted(encodings),
               websocket_extension=False, local_model=True, transient_statuses=statuses,
               upstream_requests_per_transient=1, control_retry_requests=retried, control_cooling_requests=cooled)


# ---------------------------------------------------------------- SB1
SERVED_OTHER = "gwtest-served-other"  # harness.fake_reply's substitute model
SERVED_FALLBACK = "gwtest-served-fallback"


def substitution_upstream(path, body, raw, _headers=None):
    """``gwtest-mode=transition``: a Claude reply that starts on the requested
    model and moves to another through upstream's serving-model transition
    shapes (fa11a573): a ``fallback`` content block naming ``to.model`` and
    ``usage.iterations`` ending in a ``fallback_message``. Every other mode
    is the shared fake (``substitute`` serves another model outright)."""
    if not path.endswith("/v1/messages") or b"gwtest-mode=transition" not in raw:
        return harness.fake_reply(path, body, raw)
    model = body.get("model")
    iterations = [{"type": "message", "model": model}, {"type": "fallback_message", "model": SERVED_FALLBACK}]
    message = {"id": "msg_gwtest_transition", "type": "message", "role": "assistant", "model": model,
               "content": [], "stop_reason": None, "stop_sequence": None,
               "usage": {"input_tokens": 7, "output_tokens": 0}}
    if not body.get("stream"):
        return 200, "application/json", json.dumps({
            **message, "content": [{"type": "text", "text": "FAKE_OK"}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 7, "output_tokens": 3, "iterations": iterations}}).encode()
    frames = [("message_start", {"type": "message_start", "message": message}),
              ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {
                  "type": "fallback", "from": {"model": model}, "to": {"model": SERVED_FALLBACK}}}),
              ("content_block_stop", {"type": "content_block_stop", "index": 0}),
              ("content_block_start", {"type": "content_block_start", "index": 1,
                                       "content_block": {"type": "text", "text": ""}}),
              ("content_block_delta", {"type": "content_block_delta", "index": 1,
                                       "delta": {"type": "text_delta", "text": "FAKE_OK"}}),
              ("content_block_stop", {"type": "content_block_stop", "index": 1}),
              ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                 "usage": {"output_tokens": 3, "iterations": iterations}}),
              ("message_stop", {"type": "message_stop"})]
    return 200, "text/event-stream", b"".join(harness.sse_frame(kind, value) for kind, value in frames)


class SubstitutionEvidenceTests(unittest.TestCase):
    """charmer's journal facts count silent upstream model swaps from the
    gateway's warning (``SUBSTITUTION_FORMAT``, the pin of
    internal/runtime/executor/helps/usage_helpers.go). Read the way the
    product reads an instance log (``file_log``) and parsed by
    ``gateway_events.collect``, each request's line yields exactly the
    requested and served models; a transition yields exactly one parse,
    naming the model the gateway reports as serving."""

    def substitutions(self, gateway, offset):
        def access_logged():
            text = gateway.log_path.read_bytes()[offset:].decode(errors="replace")
            return '"/v1/messages"' in text
        self.assertTrue(wait_for(access_logged, deadline=3.0), "the request was never logged")
        log = file_log.InstanceLog(gateway.log_path, datetime.datetime.now(datetime.timezone.utc),
                                   "0123456789abcdef", gateway.log_path.stat().st_size)
        window, _end = file_log.read_window(log, start=offset)
        events = gateway_events.collect(window, providers={"claude", "codex"})
        return [(route.requested, route.served) for route in events.routes if route.requested or route.served]

    def test_substitution_warning_parses_exactly(self):  # SB1
        require_selected_build("SB1")
        self.assertEqual(gateway_facts.SUBSTITUTION_FORMAT,
                         "%s executor: upstream served model %q for requested model %q (auth_index=%s)")
        gateway = harness.GatewayHarness(upstream_factory=lambda path: harness.FakeUpstream(path, substitution_upstream))
        self.addCleanup(gateway.close)
        wire = gateway.aliases[CLAUDE_KEY_ALIAS].wire
        expected = {"substitute": [(wire, SERVED_OTHER)], "transition": [(wire, SERVED_FALLBACK)], "text": []}
        parsed = 0
        for mode, wanted in expected.items():
            for stream in (False, True):
                with self.subTest(mode=mode, stream=stream):
                    offset = gateway.log_path.stat().st_size
                    reply = gateway.request(CLAUDE_KEY_ALIAS, stream=stream, mode=mode)
                    self.assertEqual(reply.status, 200)
                    self.assertEqual(len(reply.hits), 1)
                    self.assertEqual(self.substitutions(gateway, offset), wanted)
                    parsed += len(wanted)
        record(self, "SB1", status="passed", parsed=parsed, requests=6, substitute_served=SERVED_OTHER,
               transition_served=SERVED_FALLBACK, extra_parses=0)


# ---------------------------------------------------------------- RF1-RF3
REFUSAL_EXPLANATION = ("This request triggered restrictions on violative cyber content and was blocked under "
                       "Anthropic's Usage Policy. To learn more, see "
                       "https://platform.claude.com/docs/en/build-with-claude/refusals-and-fallback.")
REFUSAL_STOP_DETAILS = {"type": "refusal", "category": "cyber", "explanation": REFUSAL_EXPLANATION}
# What the gateway's classifier (internal/clienterror ClaudePolicyRefusalMessage)
# makes of REFUSAL_STOP_DETAILS: the client's error message.
REFUSAL_MESSAGE = "claude refusal (cyber): " + REFUSAL_EXPLANATION
STREAM_CUT_MESSAGE = "upstream stream closed before message_stop"
# Each case is selected by ``gwtest-mode=<case>`` in the request text.
# stream: a streamed reply; content: a text block before the refusal; details:
# the refusal carries stop_details (a policy refusal); stop: the stream ends
# with message_stop (otherwise the upstream closes right after the refusal).
REFUSAL_CASES = {
    "stream_refusal": {"stream": True, "content": False, "details": True, "stop": True},
    "stream_refusal_cut": {"stream": True, "content": False, "details": True, "stop": False},
    "nonstream_refusal": {"stream": False, "details": True},
    "nonstream_plain_refusal": {"stream": False, "details": False},
    "midstream_block": {"stream": True, "content": True, "details": True, "stop": False},
    "midstream_block_stopped": {"stream": True, "content": True, "details": True, "stop": True},
    "midstream_plain_cut": {"stream": True, "content": True, "details": False, "stop": False},
}


def _refusal_case(raw):
    cases = re.findall(rb"gwtest-mode=([a-z0-9_]+)", raw)
    case = cases[-1].decode() if cases else None
    return case if case in REFUSAL_CASES else None


def refusal_upstream_frames(case, model):
    """The upstream's SSE events for a streamed ``case``: (event, data)."""
    spec = REFUSAL_CASES[case]
    if not spec["stream"]:
        raise ValueError("not a streamed refusal case")
    frames = [("message_start", {"type": "message_start", "message": {
        "id": "msg_gwtest_refusal", "type": "message", "role": "assistant", "model": model, "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 7, "output_tokens": 0}}})]
    if spec["content"]:
        frames += [("content_block_start", {"type": "content_block_start", "index": 0,
                                            "content_block": {"type": "text", "text": ""}}),
                   ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                            "delta": {"type": "text_delta", "text": "GWTEST-PARTIAL"}}),
                   ("content_block_stop", {"type": "content_block_stop", "index": 0})]
    delta = {"stop_reason": "refusal", "stop_sequence": None}
    if spec["details"]:
        delta["stop_details"] = copy.deepcopy(REFUSAL_STOP_DETAILS)
    frames.append(("message_delta", {"type": "message_delta", "delta": delta, "usage": {"output_tokens": 2}}))
    if spec["stop"]:
        frames.append(("message_stop", {"type": "message_stop"}))
    return frames


def refusal_upstream_message(case, model):
    """The upstream's JSON reply for a non-streamed ``case``."""
    spec = REFUSAL_CASES[case]
    if spec["stream"]:
        raise ValueError("a streamed refusal case")
    message = {"id": "msg_gwtest_refusal", "type": "message", "role": "assistant", "model": model, "content": [],
               "stop_reason": "refusal", "stop_sequence": None, "usage": {"input_tokens": 7, "output_tokens": 0}}
    if spec["details"]:
        message["stop_details"] = copy.deepcopy(REFUSAL_STOP_DETAILS)
    return message


def refusal_upstream(path, body, raw, _headers=None):
    """FakeUpstream responder: the Claude refusal ``REFUSAL_CASES`` names in
    the request (``gwtest-mode=<case>``), otherwise the shared fake."""
    case = _refusal_case(raw)
    if case is None or not path.endswith("/v1/messages"):
        return harness.fake_reply(path, body, raw)
    model = body.get("model")
    if REFUSAL_CASES[case]["stream"]:
        return 200, "text/event-stream", b"".join(
            harness.sse_frame(kind, data) for kind, data in refusal_upstream_frames(case, model))
    return 200, "application/json", json.dumps(refusal_upstream_message(case, model)).encode()


def _client_error(kind, message):
    return json.dumps({"type": "error", "error": {"type": kind, "message": message}}, separators=(",", ":")).encode()


def expected_refusal_reply(case, alias=CLAUDE_KEY_ALIAS):
    """What the client receives for ``case`` on the Claude Messages route,
    measured on 8.0.67: (status, content type, exact body bytes).

    - Streams start before the refusal is seen, so the status is 200 and the
      upstream's events pass through unchanged (the served model restored to
      the client's alias). A stream that ends with message_stop gets nothing
      more. A stream the upstream closes at the refusal gets one more event:
      ``error`` of type ``invalid_request_error`` (the 400 class) with the
      classifier's refusal message when stop_details made it a policy
      refusal, and ``api_error`` (502) "upstream stream closed before
      message_stop" for a plain refusal (the control).
    - A non-stream policy refusal is HTTP 400 with an ``invalid_request_error``
      body; a plain refusal (no stop_details) is passed through with 200."""
    spec = REFUSAL_CASES[case]
    if not spec["stream"]:
        if spec["details"]:
            return 400, "application/json", _client_error("invalid_request_error", REFUSAL_MESSAGE)
        return 200, "application/json", json.dumps(refusal_upstream_message(case, alias)).encode()
    body = b"".join(harness.sse_frame(kind, data) for kind, data in refusal_upstream_frames(case, alias))
    if not spec["stop"]:
        error = (_client_error("invalid_request_error", REFUSAL_MESSAGE) if spec["details"]
                 else _client_error("api_error", STREAM_CUT_MESSAGE))
        body += b"event: error\ndata: " + error + b"\n\n"
    return 200, "text/event-stream", body


def refusal_gateway():
    """A harness gateway whose fake upstream answers every refusal case."""
    return harness.GatewayHarness(upstream_factory=lambda path: harness.FakeUpstream(path, refusal_upstream))


def refusal_request(gateway, case, alias=CLAUDE_KEY_ALIAS):
    """One Claude Messages request for ``case`` through ``gateway``."""
    return gateway.request(alias, stream=REFUSAL_CASES[case]["stream"], mode=case)


class RefusalShapeTests(unittest.TestCase):
    """Refusals through the running gateway on the Claude key route, as the
    client receives them since 8.0.56 (03a8743c non-stream, c3938b72
    mid-stream). Each row asserts the exact status, content type and body
    bytes of ``expected_refusal_reply``."""

    @classmethod
    def setUpClass(cls):
        require_selected_build("RF1-RF3")
        cls.gateway = refusal_gateway()
        cls.addClassCleanup(cls.gateway.close)

    def check(self, case):
        reply = refusal_request(self.gateway, case)
        status, content_type, body = expected_refusal_reply(case)
        self.assertEqual(len(reply.hits), 1, case)
        self.assertEqual(reply.status, status, case)
        self.assertEqual(reply.headers.get("content-type", "").split(";")[0], content_type, case)
        self.assertEqual(reply.raw, body, case)
        # The client's retry rules see no provider retry hints here.
        self.assertNotIn("retry-after", reply.headers, case)
        self.assertNotIn("x-should-retry", reply.headers, case)
        events = ([json.loads(line[6:])["type"] for line in body.decode().splitlines() if line.startswith("data: ")]
                  if content_type == "text/event-stream" else [])
        return {"status": reply.status, "events": events}

    def test_stream_refusal_client_shape(self):  # RF1
        complete = self.check("stream_refusal")
        cut = self.check("stream_refusal_cut")
        self.assertEqual(complete["events"], ["message_start", "message_delta", "message_stop"])
        self.assertEqual(cut["events"], ["message_start", "message_delta", "error"])
        record(self, "RF1", status="passed", http_status=complete["status"], events=complete["events"],
               cut_events=cut["events"], cut_error="invalid_request_error")

    def test_non_stream_refusal_is_400(self):  # RF2
        refusal = self.check("nonstream_refusal")
        control = self.check("nonstream_plain_refusal")
        self.assertEqual((refusal["status"], control["status"]), (400, 200))
        record(self, "RF2", status="passed", http_status=refusal["status"], error="invalid_request_error",
               control_status=control["status"], since="8.0.56", commit="03a8743c")

    def test_mid_stream_policy_block_is_400(self):  # RF3
        block = self.check("midstream_block")
        stopped = self.check("midstream_block_stopped")
        control = self.check("midstream_plain_cut")
        self.assertEqual(block["events"][-1], "error")
        self.assertEqual(stopped["events"][-1], "message_stop")
        self.assertEqual(control["events"][-1], "error")
        record(self, "RF3", status="passed", http_status=block["status"], events=block["events"],
               error="invalid_request_error", stopped_events=stopped["events"], control_error="api_error",
               commit="c3938b72")


if __name__ == "__main__":
    unittest.main()
