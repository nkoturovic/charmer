"""Providers declared on a private network, through the candidate gateway.

The dial guard of ``cli-proxy-api-private-network-dial.patch`` against the
real binary, rows T1-T6. Every upstream is a fake on a filesystem socket that
the gateway reaches through a loopback bridge inside its own network
namespace; nothing is resolved and no packet leaves the namespace. The config
comes from validated declarations through the renderer and is read back with
the restricted YAML parser; a row that needs a declaration the launcher
refuses (the tailnet class at a loopback address) edits the parsed document,
never the YAML text. The tailnet local-address rule needs a tailnet address
on the host: the patch's Go rows prove it with an injected local address.

Host-only: the TLS upstream of the redirect row needs openssl.
"""
from __future__ import annotations

import copy
import json
import os
import random
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import _gateway_harness as harness
import test_openai_compat_keyed as keyed
from _layout import REPO_ROOT
from charmer import catalog, operator, probe, render, served_plan, state

MODULE = "tests.test_gateway_private_network"
PATCH = REPO_ROOT / "gateway/patches/cli-proxy-api-private-network-dial.patch"
CORPUS = REPO_ROOT / "tests/fixtures/netclass-corpus.json"
CORPUS_COPY = "internal/privatenet/testdata/netclass-corpus.json"

SECRET = "PNBOX_API_KEY"
# The provider key: never in a gateway log line (T6). Its prefix is one the
# harness diagnostics redact.
KEY_VALUE = "keyedtest-key-pn-sentinel-4c1d"
REFUSED = "upstream address refused: outside the provider's private network (tailnet)"
REDIRECT_REFUSED = "upstream redirect refused"
# The watcher's line when a load (and its credentials' refresh) has finished.
LOAD_COMPLETE = "full client load complete"

# Line keys (the served aliases) and the path of each provider on its fake.
LOOP, LAN, TAIL = "custom-pnbox-chat", "custom-pnlan-chat", "custom-pntail-chat"
SAME, KSAME, PORT, HTTPS, TLS = ("custom-pnsame-chat", "custom-pnksame-chat", "custom-pnport-chat",
                                 "custom-pnhttps-chat", "custom-pntls-chat")
PLAIN = "custom-pnplain-chat"
FOLLOWED = "/followed/"


def lan_declaration(base_url: str, key: str, network: str | None = "loopback") -> dict:
    provider = {"display": "Private box", "kind": operator.LAN_KIND, "base_url": base_url,
                "auth": {"kind": "none"}, "independence_family": "pnfam"}
    if network is not None:
        provider["network"] = network
    return {"version": 1, "provider": provider, "lines": {key: {
        "wire_model": "pn-wire", "display": "Private box chat", "efforts": ["high"], "default_effort": "high",
        "context": {"declared_tokens": 32768, "source": "operator"}}}}


def keyed_declaration(base_url: str, key: str) -> dict:
    return keyed.keyed_document(base_url=base_url, secret=SECRET, key=key, wire="pn-wire", efforts=("high",),
                                default="high", display="Private keyed box", network="loopback")


def network_docs() -> dict:
    docs = keyed.fixture_docs(audited=True)
    docs["gateway"][catalog.AUDITS_KEY][operator.PRIVATE_NETWORK_AUDIT] = True
    return docs


def compat(document: dict) -> dict[str, dict]:
    return {section["name"]: section for section in document["openai-compatibility"]}


def refinalize(document: dict) -> dict:
    """``document`` with its reload sentinel recomputed."""
    bare = copy.deepcopy(document)
    bare["openai-compatibility"] = [s for s in bare["openai-compatibility"] if not render.is_reserved_name(s["name"])]
    return render.finalize_document(bare)


def render_document(*, egress: int, gateway: int, tls: int, other: int, home: Path) -> dict:
    """The rendered document, read back with the restricted parser, plus the
    one entry the launcher refuses: ``pntail``, the tailnet class at a
    loopback address (a copy of the keyed loopback entry)."""
    docs = network_docs()
    docs["gateway"]["gateway"]["base_url"] = f"http://127.0.0.1:{gateway}"
    docs["providers"]["providers"] = {}
    docs["models-v2"]["models"] = {}
    docs["models"] = docs["models-v2"]
    docs["retired"]["models"] = {}
    http = f"http://127.0.0.1:{egress}"
    files = {
        "pnbox": keyed_declaration(f"{http}/pnbox/v1", LOOP),
        "pnksame": keyed_declaration(f"{http}/pnksame/v1", KSAME),
        "pnlan": lan_declaration(f"{http}/pnlan/v1", LAN),
        "pnsame": lan_declaration(f"{http}/pnsame/v1", SAME),
        "pnport": lan_declaration(f"{http}/pnport/v1", PORT),
        "pnhttps": lan_declaration(f"{http}/pnhttps/v1", HTTPS),
        "pntls": lan_declaration(f"https://127.0.0.1:{tls}/pntls/v1", TLS),
        "pnplain": lan_declaration(f"{http}/pnplain/v1", PLAIN, network=None),
    }
    plan, _ledger = keyed.keyed_plan(files, docs=docs)
    if plan.layer.problems:
        raise AssertionError("fixture declarations failed validation: "
                             + "; ".join(problem.text() for problem in plan.layer.problems))
    document, _available, unavailable, _info = render.build_config_document(
        plan.docs["gateway"], plan.docs["providers"]["providers"], plan.docs["models-v2"]["models"], home=home,
        gateway_token=harness.FIXTURE_GATEWAY_TOKEN, resolve_secret={SECRET: KEY_VALUE}.get, continuity={},
        captures=plan.captures, provider_headers=plan.headers, oauth_overlay=plan.overlay)
    if unavailable:
        raise AssertionError("unexpected unavailable rendered provider")
    parsed = served_plan.parse_restricted_yaml(render.emit_yaml(document))
    sections = compat(parsed)
    tail = copy.deepcopy(sections["pnbox"])
    tail["name"] = "pntail"
    tail[render.PRIVATE_NETWORK_KEY] = {"base-url": f"{http}/pntail/v1", "class": "tailnet"}
    for model in tail["models"]:
        model["alias"] = TAIL
    parsed["openai-compatibility"].insert(0, tail)
    return refinalize(parsed)


class AcceptCounter:
    """Counts the connections a fake upstream accepts."""

    def __init__(self, server):
        self.count = 0
        self.lock = threading.Lock()
        accept = server.get_request

        def get_request():
            connection, address = accept()
            with self.lock:
                self.count += 1
            return connection, address

        server.get_request = get_request


class CountingUpstream(harness.FakeUpstream):
    def __init__(self, path, respond=None, context=None):
        super().__init__(path, respond=respond)
        self.accepts = AcceptCounter(self.server)
        if context is not None:
            counted = self.server.get_request

            def get_request():
                connection, address = counted()
                try:
                    return context.wrap_socket(connection, server_side=True), address
                except BaseException:
                    connection.close()
                    raise

            self.server.get_request = get_request


class ProxySink:
    """A proxy that accepts, counts and closes: whatever reaches it fails."""

    def __init__(self, path: Path):
        self.count = 0
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        os.chmod(path, 0o600)
        self.server.listen(16)
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while not self.closed.is_set():
            try:
                connection, _ = self.server.accept()
            except OSError:
                return
            self.count += 1
            connection.close()

    def close(self):
        self.closed.set()
        try:
            self.server.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.server.close()
        self.thread.join(timeout=2)


def prerequisites() -> str:
    """A required proof fails on a missing prerequisite, never skips."""
    if not os.environ.get(harness.BINARY_ENV):
        harness.boundary("BOUNDARY: the private-network rows need " + harness.BINARY_ENV + " set to the candidate build")
    openssl = shutil.which("openssl")
    if openssl is None:
        raise AssertionError("TLS certificate tool unavailable")
    return openssl


def extra_ports(count: int, taken: tuple[int, ...]) -> list[int]:
    choices = [port for port in range(61000, 65536) if port not in taken]
    return random.SystemRandom().sample(choices, count)


def tearDownModule():
    harness.EVIDENCE.finalize_module(MODULE)


def record(row, **fields):
    harness.EVIDENCE.record(MODULE, "private-network", {"row": row, "status": "passed", **fields})


class CorpusCopyTests(unittest.TestCase):
    def test_the_gateway_classifies_with_the_same_corpus(self):
        """The patch's Go table reads a byte-identical copy of the corpus."""
        lines, inside = [], False
        for line in PATCH.read_text(encoding="utf-8").splitlines():
            if line.startswith("diff --git "):
                inside = line.endswith(" b/" + CORPUS_COPY)
                continue
            if inside and line.startswith("+") and not line.startswith("+++ "):
                lines.append(line[1:])
        self.assertTrue(lines, "the patch carries no corpus copy")
        self.assertEqual("\n".join(lines) + "\n", CORPUS.read_text(encoding="utf-8"))


class _PrivateNetworkFixture(unittest.TestCase):
    """One candidate gateway with the rendered private-network document, the
    fake upstreams (plain, TLS, another port) and a proxy sink every proxy
    setting points at. ``INERT`` adds that many inert providers of that many
    models each (keyless, on the discard port), which lengthens a reload."""

    INERT = (0, 0)

    @classmethod
    def setUpClass(cls):
        openssl = prerequisites()
        cls.root = Path(tempfile.mkdtemp(prefix="gwtest-pn-")).resolve()
        state.ensure_private_dir(cls.root)
        cls.gateway = None
        cls.fakes = []
        try:
            cert, key = cls.root / "ca.pem", cls.root / "key.pem"
            made = subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                                   "-subj", "/CN=gwtest-pn.invalid", "-addext", "subjectAltName=IP:127.0.0.1",
                                   "-keyout", str(key), "-out", str(cert)], capture_output=True, timeout=30)
            if made.returncode:
                raise AssertionError("TLS certificate generation failed")
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(str(cert), str(key))
            holder = {}

            def document(_bundle, egress, port, home):
                cls.tls_port, cls.other_port, cls.proxy_port = extra_ports(3, (egress, port))
                cls.egress, cls.port = egress, port
                cls.document = render_document(egress=egress, gateway=port, tls=cls.tls_port,
                                               other=cls.other_port, home=home)
                providers, models = cls.INERT
                cls.document["openai-compatibility"][:0] = [
                    {"name": f"pninert{index:03d}", "base-url": "http://127.0.0.1:9/v1",
                     "models": [{"name": f"pninert-wire-{model:02d}", "alias": f"custom-pninert-{index:03d}-{model:02d}"}
                                for model in range(models)]}
                    for index in range(providers)]
                proxy = f"http://127.0.0.1:{cls.proxy_port}"
                # T3: every proxy source the config and environment offer.
                cls.document["proxy-url"] = proxy
                for section in cls.document["openai-compatibility"]:
                    for entry in section.get("api-key-entries") or ():
                        entry["proxy-url"] = proxy
                cls.document = refinalize(cls.document)
                trust = home / ".config/charmer/gwtest-pn-ca.pem"
                state.atomic_write(trust, cert.read_bytes())
                holder["trust"], holder["proxy"] = str(trust), proxy
                cls.other = CountingUpstream(cls.root / "other.sock")
                cls.tls = CountingUpstream(cls.root / "tls.sock", respond=cls.respond_tls, context=context)
                cls.proxy = ProxySink(cls.root / "proxy.sock")
                cls.fakes += [cls.other, cls.tls, cls.proxy]
                return cls.document, {}, {}

            original = probe.start_isolated_process

            def start(argv, **kwargs):
                if "proxy" not in holder:
                    # The binary's version check, before the document exists.
                    return original(argv, **kwargs)
                proxy = holder["proxy"]
                kwargs["env"] = {**kwargs["env"], "SSL_CERT_FILE": holder["trust"], "HTTP_PROXY": proxy,
                                 "HTTPS_PROXY": proxy, "ALL_PROXY": proxy, "http_proxy": proxy,
                                 "https_proxy": proxy, "all_proxy": proxy}
                kwargs["bridges"] = [*kwargs["bridges"],
                                     probe.PortBridge(cls.tls_port, cls.root / "tls.sock", "egress"),
                                     probe.PortBridge(cls.other_port, cls.root / "other.sock", "egress"),
                                     probe.PortBridge(cls.proxy_port, cls.root / "proxy.sock", "egress")]
                return original(argv, **kwargs)

            def upstream(path):
                fake = CountingUpstream(path, respond=cls.respond_main)
                cls.main = fake
                return fake

            with mock.patch.object(probe, "start_isolated_process", start):
                cls.gateway = harness.GatewayHarness(document_factory=document, upstream_factory=upstream)
            # The published document (the harness adds the shipped compression).
            cls.document = cls.gateway.document
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        if cls.gateway is not None:
            cls.gateway.close()
            cls.gateway = None
        for fake in cls.fakes:
            fake.close()
        cls.fakes = []
        shutil.rmtree(cls.root, ignore_errors=True)

    @classmethod
    def respond_main(cls, path, body, raw, _headers):
        locations = {
            "/pnsame/": "/followed/same/chat/completions",
            "/pnksame/": "/followed/ksame/chat/completions",
            "/pnport/": f"http://127.0.0.1:{cls.other_port}/followed/port/chat/completions",
            "/pnhttps/": f"https://127.0.0.1:{cls.tls_port}/followed/https/chat/completions",
        }
        for prefix, location in locations.items():
            if path.startswith(prefix):
                return 307, "application/json", b"{}", {"Location": location}
        return harness.fake_reply(path, body, raw)

    @classmethod
    def respond_tls(cls, path, body, raw, _headers):
        if path.startswith("/pntls/"):
            return 307, "application/json", b"{}", {
                "Location": f"http://127.0.0.1:{cls.egress}/followed/tls/chat/completions"}
        return harness.fake_reply(path, body, raw)

    def log(self) -> str:
        return self.gateway.log_path.read_text(errors="replace")

    def followed(self) -> list:
        return [hit.path for fake in (self.main, self.other, self.tls) for hit in fake.hits
                if hit.path.startswith(FOLLOWED)]

    def loads(self) -> int:
        return self.log().count(LOAD_COMPLETE)

    def settle(self, loads: int) -> None:
        """Wait until the watcher has finished the load after ``loads``
        (the credentials of the published document follow)."""
        deadline = time.monotonic() + 30
        while self.loads() <= loads:
            if time.monotonic() > deadline:
                raise AssertionError(self.gateway.diagnostic("reload did not finish"))
            time.sleep(0.05)
        time.sleep(0.5)

    def reload(self, document: dict, *, pause: float = 0.05, settle: bool = True) -> None:
        """Publish ``document`` and wait until the gateway serves its sentinel
        and, with ``settle``, until its credentials follow."""
        loads = self.loads()
        state.atomic_write(self.gateway.config, render.emit_yaml(document).encode())
        sentinel = render.document_sentinel(document)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            reply = self.gateway.served_listing()
            if reply.status == 200 and sentinel in {row["id"] for row in reply.json()["data"]}:
                type(self).document = document
                if settle:
                    self.settle(loads)
                return
            if pause:
                time.sleep(pause)
        raise AssertionError(self.gateway.diagnostic("reload sentinel not served"))

    def assertRefused(self, reply, text):
        self.assertEqual(reply.status, 502, self.gateway.diagnostic(f"status {reply.status}"))
        self.assertIn(text, reply.raw.decode(errors="replace"))
        self.assertEqual(reply.hits, [])


class PrivateNetworkGatewayTests(_PrivateNetworkFixture):
    def test_rendered_entries_carry_the_private_network_block(self):
        sections = compat(served_plan.parse_restricted_yaml(self.gateway.config.read_text()))
        for name in ("pnbox", "pnksame", "pnlan", "pnsame", "pnport", "pnhttps", "pntls"):
            with self.subTest(provider=name):
                block = sections[name][render.PRIVATE_NETWORK_KEY]
                self.assertNotIn("base-url", sections[name])
                self.assertEqual(sorted(block), ["base-url", "class"])
                self.assertEqual(block["class"], "loopback")
        self.assertEqual(sections["pnbox"][render.PRIVATE_NETWORK_KEY]["base-url"],
                         f"http://127.0.0.1:{self.egress}/pnbox/v1")
        self.assertNotIn(render.PRIVATE_NETWORK_KEY, sections["pnplain"])
        self.assertEqual(sections["pnplain"]["base-url"], f"http://127.0.0.1:{self.egress}/pnplain/v1")
        listing = {row["id"] for row in self.gateway.served_listing().json()["data"]}
        self.assertLessEqual({LOOP, LAN, TAIL, SAME, KSAME, PORT, HTTPS, TLS, PLAIN}, listing)

    def test_t1_keyed_loopback_is_served_with_the_bearer(self):
        reply = self.gateway.request(LOOP)
        self.assertEqual(reply.status, 200, self.gateway.diagnostic(f"status {reply.status}"))
        self.assertEqual([hit.path for hit in reply.hits], ["/pnbox/v1/chat/completions"])
        self.assertEqual(reply.hits[0].header_values.get("authorization"), "Bearer " + KEY_VALUE)
        keyless = self.gateway.request(LAN)
        self.assertEqual(keyless.status, 200, self.gateway.diagnostic(f"status {keyless.status}"))
        self.assertEqual([hit.path for hit in keyless.hits], ["/pnlan/v1/chat/completions"])
        self.assertNotIn("authorization", keyless.hits[0].header_values)
        record("T1", keyed=True, keyless=True)

    def test_t2_tailnet_class_at_loopback_is_refused_before_any_connection(self):
        before = self.main.accepts.count
        reply = self.gateway.request(TAIL)
        self.assertRefused(reply, REFUSED)
        self.assertEqual(self.main.accepts.count, before)
        self.assertNotIn("/pntail/", [hit.path[:8] for hit in self.main.hits])
        self.assertIn(f"private-network: refused provider=pntail class=tailnet remote=127.0.0.1 reason=outside-class",
                      self.log())
        record("T2", upstream_accepts=0)

    def test_t3_no_proxy_from_any_source(self):
        self.assertTrue(self.document.get("proxy-url"))
        self.assertTrue(compat(self.document)["pnbox"]["api-key-entries"][0].get("proxy-url"))
        before = self.proxy.count
        for alias in (LOOP, LAN):
            with self.subTest(alias=alias):
                self.assertEqual(self.gateway.request(alias).status, 200)
        self.assertEqual(self.proxy.count, before)
        # Control: the same settings route an entry without a network
        # through the proxy, which fails it.
        control = self.gateway.request(PLAIN)
        self.assertNotEqual(control.status, 200)
        self.assertEqual(control.hits, [])
        self.assertGreater(self.proxy.count, before)
        record("T3", proxy_accepts=0, control_proxied=True)

    def test_t4_no_redirect_is_followed(self):
        other = self.other.accepts.count
        for alias in (SAME, KSAME, PORT, HTTPS, TLS):
            with self.subTest(alias=alias):
                reply = self.gateway.request(alias)
                self.assertEqual(reply.status, 502, self.gateway.diagnostic(f"{alias}: status {reply.status}"))
                self.assertIn(REDIRECT_REFUSED, reply.raw.decode(errors="replace"))
        self.assertEqual(self.followed(), [])
        self.assertEqual(self.other.accepts.count, other)
        record("T4", cases=5)

    def test_t5_reload_changes_the_class_and_the_url(self):
        original = copy.deepcopy(self.document)
        try:
            tailnet = copy.deepcopy(original)
            compat(tailnet)["pnbox"][render.PRIVATE_NETWORK_KEY]["class"] = "tailnet"
            self.reload(refinalize(tailnet))
            before = self.main.accepts.count
            self.assertRefused(self.gateway.request(LOOP), REFUSED)
            self.assertEqual(self.main.accepts.count, before)
            moved = copy.deepcopy(original)
            compat(moved)["pnbox"][render.PRIVATE_NETWORK_KEY]["base-url"] = f"http://127.0.0.1:{self.egress}/pnmoved/v1"
            self.reload(refinalize(moved))
            reply = self.gateway.request(LOOP)
            self.assertEqual(reply.status, 200, self.gateway.diagnostic(f"status {reply.status}"))
            self.assertEqual([hit.path for hit in reply.hits], ["/pnmoved/v1/chat/completions"])
            base = f"http://127.0.0.1:{self.egress}"
            log = self.log()
            self.assertIn(f"private-network loopback {base}/pnbox/v1 -> tailnet {base}/pnbox/v1", log)
            self.assertIn(f"private-network tailnet {base}/pnbox/v1 -> loopback {base}/pnmoved/v1", log)
        finally:
            self.reload(original)
        record("T5", reloads=3)

    def test_t6_the_key_is_in_no_log_line(self):
        self.assertEqual(self.gateway.request(LOOP).status, 200)
        self.assertRefused(self.gateway.request(TAIL), REFUSED)
        original = copy.deepcopy(self.document)
        try:
            changed = copy.deepcopy(original)
            compat(changed)["pnbox"][render.PRIVATE_NETWORK_KEY]["class"] = "tailnet"
            self.reload(refinalize(changed))
            self.assertRefused(self.gateway.request(LOOP), REFUSED)
        finally:
            self.reload(original)
        log = self.log()
        self.assertIn("private-network: refused provider=", log)
        self.assertIn("private-network loopback", log)
        self.assertNotIn(KEY_VALUE, log)
        self.assertNotIn(KEY_VALUE[-12:], log)
        record("T6", success=True, refusal=True, reload=True)


class PrivateNetworkReloadRaceTests(_PrivateNetworkFixture):
    """T7: a request sent as soon as a reload's sentinel is served is dialled
    under the reloaded declaration, never under the previous one. A reload
    publishes its routes before the synthesized credentials follow; 200 inert
    providers of 20 models each (within the supported provider count) keep
    that window open long enough to land in it. Every proxy setting points at
    the sink, so an ordinary dial never reaches the provider directly."""

    INERT = (200, 20)

    def immediately(self, change):
        """Publish the changed document, request ``LOOP`` as soon as its
        sentinel is served, then let the load finish. Returns the reply and
        whether it came back before the load finished."""
        changed = copy.deepcopy(self.document)
        change(changed)
        loads = self.loads()
        self.reload(refinalize(changed), pause=0, settle=False)
        reply = self.gateway.request(LOOP)
        in_window = self.loads() == loads
        self.settle(loads)
        return reply, in_window

    def test_t7_a_published_reload_is_dialled_under_its_own_declaration(self):
        original = copy.deepcopy(self.document)
        base = f"http://127.0.0.1:{self.egress}"
        windows = {}

        def network(document):
            return compat(document)["pnbox"][render.PRIVATE_NETWORK_KEY]

        try:
            # loopback -> tailnet: refused, nothing reaches the provider.
            reply, windows["to-tailnet"] = self.immediately(lambda d: network(d).update({"class": "tailnet"}))
            self.assertRefused(reply, REFUSED)
            # tailnet -> loopback: served under the reloaded class.
            reply, windows["to-loopback"] = self.immediately(lambda d: network(d).update({"class": "loopback"}))
            self.assertEqual(reply.status, 200, self.gateway.diagnostic(f"status {reply.status}"))
            self.assertEqual([hit.path for hit in reply.hits], ["/pnbox/v1/chat/completions"])
            # The base URL moves: the previous one is never dialled.
            reply, windows["url-moved"] = self.immediately(
                lambda d: network(d).update({"base-url": f"{base}/pnmoved/v1"}))
            self.assertNotIn("/pnbox/v1/chat/completions", [hit.path for hit in reply.hits])
            self.reload(original)

            # The private network is removed (an ordinary entry at the same
            # URL): the previous guard never dials it directly.
            def ordinary(document):
                section = compat(document)["pnbox"]
                section["base-url"] = section.pop(render.PRIVATE_NETWORK_KEY)["base-url"]

            reply, windows["network-removed"] = self.immediately(ordinary)
            self.assertEqual(reply.hits, [])
            self.reload(original)

            # The entry is removed: its credential is never dialled again.
            def removed(document):
                document["openai-compatibility"] = [s for s in document["openai-compatibility"] if s["name"] != "pnbox"]

            reply, windows["entry-removed"] = self.immediately(removed)
            self.assertEqual(reply.hits, [])
        finally:
            self.reload(original)
        record("T7", cases=len(windows), in_window=sum(windows.values()))
