"""Providers declared on a private network (``network``): validation,
the gate, digests, declarations, approval, show, the guarded listing path,
doctor and readiness.

Every name and address is a documentation or fixture value; no test
resolves a name or connects anywhere but a loopback fake it started.
"""

from __future__ import annotations

import copy
import unittest
from typing import Any

from _catalog import FIXTURE_ROOT
from charmer import catalog, operator, strict_json

KEYED_ID = "box"
LAN_ID = "lanbox"
SECRET = "BOX_API_KEY"
KEY = "custom-box-chat"
LAN_KEY = "custom-lanbox-chat"
# A dummy stored credential with no token shape that is also a valid line key.
STORED_LINE_KEY = "custom-dummy-key-for-a-private-box"


def network_docs(*, keyed: bool = True, network: bool = True) -> dict:
    """The frozen fixture docs with the two trusted gates set as asked."""

    docs = copy.deepcopy(catalog.load_catalog(FIXTURE_ROOT).docs)
    docs["gateway"][catalog.AUDITS_KEY] = {catalog.KEYED_COMPAT_AUDIT: keyed,
                                           operator.PRIVATE_NETWORK_AUDIT: network}
    return docs


def line(key: str = KEY, wire: str = "box-chat-1") -> dict:
    return {key: {"wire_model": wire, "display": "Box chat", "efforts": ["high"], "default_effort": "high",
                  "context": {"declared_tokens": 32768, "source": "operator"}}}


def keyed_declaration(base_url: str = "http://100.88.12.34:8010/v1", network: str | None = "tailnet",
                      **provider: Any) -> dict:
    block = {"display": "Box", "kind": operator.KEYED_KIND, "base_url": base_url,
             "auth": {"kind": "bearer", "secret_ref": f"env:{SECRET}"}, "independence_family": "boxfam",
             **provider}
    if network is not None:
        block["network"] = network
    return {"version": 1, "provider": block, "lines": line()}


def lan_declaration(base_url: str = "http://box.example.test:8010/v1", network: str | None = "tailnet") -> dict:
    block = {"display": "LAN box", "kind": operator.LAN_KIND, "base_url": base_url, "auth": {"kind": "none"},
             "independence_family": "boxfam"}
    if network is not None:
        block["network"] = network
    return {"version": 1, "provider": block, "lines": line(LAN_KEY, "lanbox-chat-1")}


def raw(document: dict) -> bytes:
    return strict_json.pretty_file_bytes(document)


def layer_of(files: dict[str, dict], *, docs: dict | None = None, **kwargs: Any) -> operator.OperatorLayer:
    return operator.validate_layer(docs or network_docs(), {name: raw(doc) for name, doc in files.items()},
                                   schemas=operator.load_schemas(FIXTURE_ROOT), **kwargs)


def texts(layer: operator.OperatorLayer) -> list[str]:
    return [problem.text() for problem in layer.problems]

# The keyless LAN kind without ``network``: the rule as it is, byte for byte.
LAN_TABLE = (
    ("http://127.0.0.1:18010/v1", ("http://127.0.0.1:18010", "http://127.0.0.1:18010/v1")),
    ("http://box.lan:8010/v1", ("http://box.lan:8010", "http://box.lan:8010/v1")),
    ("http://box.local:8010/v1/", ("http://box.local:8010", "http://box.local:8010/v1")),
    ("http://box.home.arpa/v1", ("http://box.home.arpa", "http://box.home.arpa/v1")),
    ("http://box.internal:80/v1", ("http://box.internal", "http://box.internal/v1")),
    ("http://box:8000", ("http://box:8000", "http://box:8000")),
    ("http://localhost:8000/v1", ("http://localhost:8000", "http://localhost:8000/v1")),
    ("http://xn--bcher-kva.lan/v1", ("http://xn--bcher-kva.lan", "http://xn--bcher-kva.lan/v1")),
    ("https://box.lan/v1", ("https://box.lan", "https://box.lan/v1")),
    ("http://192.168.1.10:8000/v1", ("http://192.168.1.10:8000", "http://192.168.1.10:8000/v1")),
    ("http://10.1.2.3/v1", ("http://10.1.2.3", "http://10.1.2.3/v1")),
    ("http://198.18.0.1/v1", ("http://198.18.0.1", "http://198.18.0.1/v1")),
    ("http://[fd12:3456:789a::1]:8000/v1", ("http://[fd12:3456:789a::1]:8000", "http://[fd12:3456:789a::1]:8000/v1")),
    # The standard library calls documentation ranges private: kept as it is.
    ("http://203.0.113.9/v1", ("http://203.0.113.9", "http://203.0.113.9/v1")),
    ("http://box.example.com:8010/v1", "the keyless LAN kind needs a local-network host"),
    ("http://100.88.12.34:8010/v1", "the keyless LAN kind needs a local-network host"),
    ("http://8.8.8.8/v1", "the keyless LAN kind needs a local-network host"),
    ("http://box.lan.:8010/v1", "host is not a plain lower-case DNS name"),
    ("http://2130706433/v1", "ambiguous numeric host spelling"),
    ("http://0x7f.0.0.1/v1", "ambiguous numeric host spelling"),
    ("http://0177.0.0.1/v1", "ambiguous numeric host spelling"),
    ("http://127.1/v1", "ambiguous numeric host spelling"),
    ("http://bücher.lan/v1", "contains characters that are not allowed in a URL"),
    ("http://100.100.100.200/v1", "metadata and link-local addresses are forbidden"),
    ("http://169.254.169.254/v1", "metadata and link-local addresses are forbidden"),
    ("http://[fe80::1%25eth0]/v1", "metadata and link-local addresses are forbidden"),
    ("http://0.0.0.0/v1", "unspecified, broadcast and multicast addresses are forbidden"),
    ("http://224.0.0.1/v1", "unspecified, broadcast and multicast addresses are forbidden"),
    ("http://metadata.google.internal/v1", "metadata hosts are forbidden"),
    ("http://127.0.0.1:8317/v1", "the charmer gateway port is not a provider"),
    ("http://localhost:8316/v1", "the charmer gateway port is not a provider"),
    ("http://user@box.lan/v1", "userinfo is not allowed in a provider URL"),
    ("http://box.lan/v1?x=1", "a provider URL has no query or fragment"),
    ("http://box.lan:0/v1", "port 0 is not allowed"),
    ("ftp://box.lan/v1", "scheme ftp is not allowed"),
)


class LanRuleWithoutNetworkTests(unittest.TestCase):
    def test_lan_kind_without_network_is_unchanged(self) -> None:
        for url, expected in LAN_TABLE:
            with self.subTest(url=url):
                if isinstance(expected, str):
                    with self.assertRaises(ValueError) as caught:
                        operator.normalize_endpoint(url, keyed=False, lan=True)
                    self.assertEqual(str(caught.exception), expected)
                else:
                    self.assertEqual(operator.normalize_endpoint(url, keyed=False, lan=True), expected)


PORTS = frozenset({8316, 8317})
NETWORK_TABLE = (
    # (url, keyed, lan, network) -> (origin, url) or the refusal
    (("http://100.88.12.34:8010/v1", True, False, "tailnet"),
     ("http://100.88.12.34:8010", "http://100.88.12.34:8010/v1")),
    (("https://100.88.12.34/v1/", True, False, "tailnet"), ("https://100.88.12.34", "https://100.88.12.34/v1")),
    (("http://box.example.test:8010/v1", True, False, "tailnet"),
     ("http://box.example.test:8010", "http://box.example.test:8010/v1")),
    (("http://[fd7a:115c:a1e0:5ee::7]:8010/v1", True, False, "tailnet"),
     ("http://[fd7a:115c:a1e0:5ee::7]:8010", "http://[fd7a:115c:a1e0:5ee::7]:8010/v1")),
    (("http://192.168.1.10:8000/v1", True, False, "lan"), ("http://192.168.1.10:8000", "http://192.168.1.10:8000/v1")),
    (("http://127.0.0.1:18010/v1", True, False, "loopback"), ("http://127.0.0.1:18010", "http://127.0.0.1:18010/v1")),
    (("http://localhost:18010/v1", True, False, "loopback"), ("http://localhost:18010", "http://localhost:18010/v1")),
    # The keyless LAN kind: any name with a network (the suffix rule is for the field-less form).
    (("http://box.example.test:8010/v1", False, True, "tailnet"),
     ("http://box.example.test:8010", "http://box.example.test:8010/v1")),
    (("http://box.example.com/v1", False, True, "lan"), ("http://box.example.com", "http://box.example.com/v1")),
    (("http://192.168.1.10:8000/v1", True, False, "tailnet"), "192.168.1.10 is on the lan network, not tailnet"),
    (("http://100.88.12.34/v1", False, True, "lan"), "100.88.12.34 is on the tailnet network, not lan"),
    (("http://127.0.0.1:18010/v1", True, False, "tailnet"), "127.0.0.1 is on the loopback network, not tailnet"),
    (("http://203.0.113.9/v1", True, False, "lan"), "203.0.113.9 is not on a private network"),
    (("http://198.18.0.1/v1", False, True, "lan"), "198.18.0.1 is not on a private network"),
    (("http://100.100.100.100/v1", True, False, "tailnet"),
     "100.100.100.100 is not a provider address (100.100.100.0/24: reserved by the overlay for its own "
     "services; 100.100.100.100 is its resolver)"),
    (("http://[fd7a:115c:a1e0::53]/v1", True, False, "tailnet"),
     "fd7a:115c:a1e0::53 is not a provider address (fd7a:115c:a1e0::53: the overlay's resolver)"),
    (("http://[::ffff:100.88.12.34]/v1", True, False, "tailnet"), "write the IPv4 address, not its IPv6 form"),
    (("http://[::ffff:7f00:1]:18010/v1", True, False, "loopback"), "write the IPv4 address, not its IPv6 form"),
    (("http://[::100.88.12.34]/v1", True, False, "tailnet"), "write the IPv4 address, not its IPv6 form"),
    (("http://[fd7a:115c:a1e0:5ee::7%25tun0]/v1", True, False, "tailnet"), "an address with a zone is not allowed"),
    (("http://localhost:18010/v1", True, False, "tailnet"), "localhost is on the loopback network, not tailnet"),
    (("http://box.example.test:8317/v1", True, False, "loopback"), "the charmer gateway port is not a provider"),
    (("http://127.0.0.1:8317/v1", True, False, "tailnet"), "the charmer gateway port is not a provider"),
    # Precedence: never, then the gateway port, then outside the network.
    (("http://100.100.0.7:8317/v1", True, False, "loopback"),
     "100.100.0.7 is not a provider address (100.100.0.0/24: reserved by the overlay for its own services)"),
    (("http://192.168.1.10:8317/v1", True, False, "loopback"), "the charmer gateway port is not a provider"),
    (("http://169.254.169.254/v1", True, False, "lan"), "metadata and link-local addresses are forbidden"),
    (("http://box.lan.:8010/v1", True, False, "lan"), "host is not a plain lower-case DNS name"),
    (("http://127.1/v1", True, False, "loopback"), "ambiguous numeric host spelling"),
    (("ftp://box.example.test/v1", False, True, "lan"), "scheme ftp is not allowed"),
    (("ftp://box.example.test/v1", True, False, "lan"), "a secret is sent only over https"),
    (("http://box.example.test/v1", True, False, "wan"), "network must be one of loopback, lan, tailnet"),
)


class NetworkEndpointTests(unittest.TestCase):
    def test_network_rules(self) -> None:
        for (url, keyed, lan, network), expected in NETWORK_TABLE:
            with self.subTest(url=url, network=network):
                call = lambda: operator.normalize_endpoint(url, keyed=keyed, lan=lan, network=network,
                                                           gateway_ports=PORTS)
                if isinstance(expected, str):
                    with self.assertRaises(ValueError) as caught:
                        call()
                    self.assertEqual(str(caught.exception), expected)
                else:
                    self.assertEqual(call(), expected)

    def test_keyed_http_refusal_names_the_option_only_where_it_applies(self) -> None:
        with self.assertRaises(ValueError) as caught:
            operator.normalize_endpoint("http://box.example.test/v1", keyed=True, lan=False, network_hint=True)
        self.assertEqual(str(caught.exception), "a secret is sent only over https, or over http to a declared "
                                                "private network (--network loopback, lan or tailnet)")
        with self.assertRaises(ValueError) as caught:
            operator.normalize_endpoint("http://box.example.test/v1", keyed=True, lan=False)
        self.assertEqual(str(caught.exception), "a secret is sent only over https")


class NetworkDeclarationTests(unittest.TestCase):
    def test_keyed_tailnet_declaration_resolves_with_the_network(self) -> None:
        layer = layer_of({KEYED_ID: keyed_declaration()})
        self.assertEqual(texts(layer), [])
        provider = layer.providers[KEYED_ID]
        self.assertEqual(provider.network, "tailnet")
        self.assertEqual(provider.entry["transport"], {
            "kind": "direct-openai", "base_url": "http://100.88.12.34:8010/v1",
            "auth": {"kind": "bearer", "secret_ref": f"env:{SECRET}"}, "network": "tailnet"})
        self.assertIn(KEY, layer.lines)
        self.assertEqual(layer.route_status[KEYED_ID], "unapproved")

    def test_keyed_http_without_network_names_the_option(self) -> None:
        layer = layer_of({KEYED_ID: keyed_declaration(network=None)})
        self.assertNotIn(KEYED_ID, layer.providers)
        self.assertIn("providers.d/box.json: provider.base_url: base_url refused: a secret is sent only over https, "
                      "or over http to a declared private network (--network loopback, lan or tailnet)", texts(layer))

    def test_anthropic_kind_takes_no_network(self) -> None:
        document = keyed_declaration(base_url="https://api.example.test/anthropic", kind="anthropic-compatible")
        layer = layer_of({KEYED_ID: document})
        self.assertNotIn(KEYED_ID, layer.providers)
        self.assertEqual(texts(layer), ["providers.d/box.json: provider.network: network applies to the "
                                        "OpenAI-compatible kinds"])

    def test_lan_kind_with_network_takes_any_name_and_renders_the_normalized_base(self) -> None:
        layer = layer_of({LAN_ID: lan_declaration("http://Box.example.test:8010/v1/")})
        self.assertEqual(texts(layer), [])
        provider = layer.providers[LAN_ID]
        self.assertEqual(provider.entry["transport"]["base_url"], "http://box.example.test:8010/v1")
        self.assertEqual(provider.entry["transport"]["network"], "tailnet")
        self.assertEqual(layer.route_status[LAN_ID], "keyless")
        # Without the field the LAN kind keeps its rule and its authored base.
        without = layer_of({LAN_ID: lan_declaration("http://box.lan:8010/v1/", network=None)})
        self.assertEqual(without.providers[LAN_ID].entry["transport"],
                         {"kind": "direct-openai", "base_url": "http://box.lan:8010/v1/", "auth": {"kind": "none"}})
        refused = layer_of({LAN_ID: lan_declaration(network=None)})
        self.assertIn("providers.d/lanbox.json: provider.base_url: base_url refused: the keyless LAN kind needs a "
                      "local-network host", texts(refused))

    def test_a_literal_outside_the_network_is_refused_in_the_layer(self) -> None:
        layer = layer_of({LAN_ID: lan_declaration("http://192.168.1.10:8010/v1", network="tailnet")})
        self.assertEqual(texts(layer), ["providers.d/lanbox.json: provider.base_url: base_url refused: "
                                        "192.168.1.10 is on the lan network, not tailnet"])

    def test_listing_url_follows_the_network(self) -> None:
        document = keyed_declaration(listing={"url": "http://100.88.12.34:8010/v1/models", "shape": "openai",
                                              "auth": "provider"})
        layer = layer_of({KEYED_ID: document})
        self.assertEqual(texts(layer), [])
        self.assertEqual(layer.providers[KEYED_ID].listing_origin, "http://100.88.12.34:8010")

    def test_schema_refuses_an_unknown_network(self) -> None:
        layer = layer_of({KEYED_ID: keyed_declaration(network="wan")})
        self.assertNotIn(KEYED_ID, layer.providers)
        self.assertTrue(any(problem.code == "schema" and problem.json_path == "$.provider.network"
                            for problem in layer.problems), texts(layer))


class NetworkGateTests(unittest.TestCase):
    def test_closed_gate_refuses_by_metadata_before_any_store_access(self) -> None:
        docs = network_docs(network=False)

        def store() -> frozenset[str]:
            raise AssertionError("the secret store was read")

        for name, document in ((KEYED_ID, keyed_declaration()), (LAN_ID, lan_declaration())):
            with self.subTest(provider=name):
                files = {name: raw(document)}
                self.assertFalse(operator.store_scan_needed(docs, files))
                layer = operator.validate_layer(docs, files, schemas=operator.load_schemas(FIXTURE_ROOT),
                                                secret_values=operator._scan_values(store, docs, files))
                self.assertEqual(layer.providers, {})
                self.assertEqual([(p.code, p.json_path) for p in layer.problems], [("network-gated", "$.provider.network")])
                self.assertEqual(texts(layer), [
                    f"providers.d/{name}.json: provider.network: private-network routes are unavailable in this "
                    f"build — before going back to a release without them: charmer providers rm {name} (it names "
                    "any of its lines to remove first); add the provider and its lines again after returning"])
                self.assertEqual(operator.keyed_preflight(docs, name, files[name]).code, "network-gated")

    def test_closed_gate_remedy_carries_no_authored_text(self) -> None:
        # The metadata-only path never scans stored values, so a line key
        # holding a stored credential (no token shape) must not reach it.
        docs = network_docs(network=False)
        document = keyed_declaration()
        document["lines"] = line(STORED_LINE_KEY)
        problem = operator.keyed_preflight(docs, KEYED_ID, raw(document))
        self.assertEqual(problem.code, "network-gated")
        self.assertNotIn(STORED_LINE_KEY, problem.text())
        self.assertNotIn("dummy", repr(problem))

    def test_keyed_gate_is_checked_first(self) -> None:
        docs = network_docs(keyed=False, network=False)
        problem = operator.keyed_preflight(docs, KEYED_ID, raw(keyed_declaration()))
        self.assertEqual(problem.code, "kind-gated")

    def test_open_gates_and_declarations_without_network_are_untouched(self) -> None:
        docs = network_docs(network=False)
        self.assertIsNone(operator.keyed_preflight(docs, LAN_ID, raw(lan_declaration("http://box.lan/v1", None))))
        self.assertIsNone(operator.keyed_preflight(network_docs(), KEYED_ID, raw(keyed_declaration())))
        self.assertFalse(operator.private_network_audited(network_docs(network=False)["gateway"]))
        self.assertTrue(operator.private_network_audited(network_docs()["gateway"]))
        for value in ("true", 1, None, {}):
            gateway = {catalog.AUDITS_KEY: {operator.PRIVATE_NETWORK_AUDIT: value}}
            self.assertFalse(operator.private_network_audited(gateway))


class NetworkDigestTests(unittest.TestCase):
    def test_routes_without_network_keep_their_digest(self) -> None:
        # The body every earlier release hashed: no network key.
        expected = operator._digest("operator-route", {
            "secret_ref": f"env:{SECRET}", "auth": "bearer", "header": None, "origin": "https://api.example.test",
            "url": None, "listing_origin": None, "listing_auth": None})
        layer = layer_of({KEYED_ID: keyed_declaration("https://api.example.test/v1", network=None)})
        self.assertEqual(layer.providers[KEYED_ID].route_digest, expected)

    def test_the_network_is_part_of_the_route_and_a_change_needs_a_new_approval(self) -> None:
        https = {name: layer_of({KEYED_ID: keyed_declaration("https://100.88.12.34/v1", network=name)})
                 .providers[KEYED_ID] for name in (None, "tailnet")}
        self.assertNotEqual(https[None].route_digest, https["tailnet"].route_digest)
        named = keyed_declaration("http://box.example.test:8010/v1", network="tailnet")
        approved = layer_of({KEYED_ID: named}).providers[KEYED_ID]
        ledger = operator.parse_ledger(strict_json.pretty_file_bytes({
            **_empty_ledger(), "routes": {KEYED_ID: operator.route_record(approved, at="2026-11-11T00:00:00Z")}}),
            operator.load_schemas(FIXTURE_ROOT).ledger)
        self.assertEqual(layer_of({KEYED_ID: named}, ledger=ledger).route_status[KEYED_ID], "approved")
        # The same origin on another network is a retarget.
        moved = keyed_declaration("http://box.example.test:8010/v1", network="lan")
        self.assertEqual(layer_of({KEYED_ID: moved}).providers[KEYED_ID].origin, approved.origin)
        self.assertEqual(layer_of({KEYED_ID: moved}, ledger=ledger).route_status[KEYED_ID], "changed")
        record = operator.route_record(approved, at="2026-11-11T00:00:00Z")
        self.assertNotIn("network", record)


def netclass_describe(network: str) -> str:
    from charmer import netclass

    return netclass.describe(network)


def open_private_network_gate(assets) -> None:
    """Open ``audits.private_network`` in a private copy of an asset root
    (its gateway schema accepts the key first, as the gateway schema of this
    release does)."""

    import json

    schema_path = assets / "schemas" / "gateway.schema.json"
    schema = json.loads(schema_path.read_text())
    schema["properties"]["audits"]["properties"].setdefault(operator.PRIVATE_NETWORK_AUDIT, {"type": "boolean"})
    schema_path.write_bytes(strict_json.pretty_file_bytes(schema))
    gateway_path = assets / "catalog" / "gateway.json"
    gateway = json.loads(gateway_path.read_text())
    gateway.setdefault(catalog.AUDITS_KEY, {})[operator.PRIVATE_NETWORK_AUDIT] = True
    gateway_path.write_bytes(strict_json.pretty_file_bytes(gateway))


def _getaddrinfo(*addresses: str):
    """A resolver answering ``addresses`` in order (getaddrinfo's shape)."""

    import socket

    def resolve(_host: str, port: int):
        return [(socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 (address, port, 0, 0) if ":" in address else (address, port)) for address in addresses]

    return resolve


class NetworkCommandCase(unittest.TestCase):
    """The fixture CLI harness with both trusted gates open (in memory) and
    the shipped presets; a resolver seam that records every lookup."""

    def setUp(self) -> None:
        import shutil
        import tempfile
        from pathlib import Path
        from unittest import mock

        import test_cli
        import test_openai_compat_keyed as keyed
        from _catalog import SHIPPED_ROOT

        tmp = Path(tempfile.mkdtemp(prefix="nettest-assets-")).resolve()
        self.addCleanup(shutil.rmtree, tmp, True)
        assets = keyed.audited_root(tmp)
        shutil.copytree(SHIPPED_ROOT / "presets", assets / "presets")
        open_private_network_gate(assets)
        self.harness = test_cli.OperatorCommandCase("setUp")
        with mock.patch.object(test_cli, "CATALOG_ROOT", assets):
            self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)
        self.runtime = self.harness.runtime
        self.runtime.catalog.docs["gateway"][catalog.AUDITS_KEY] = {
            catalog.KEYED_COMPAT_AUDIT: True, operator.PRIVATE_NETWORK_AUDIT: True}
        self.answers: tuple[str, ...] = ("100.88.12.34",)
        self.lookups: list[str] = []
        self.runtime.host_resolver = self._resolve

    def _resolve(self, host: str, port: int):
        self.lookups.append(host)
        if self.answers == ():
            import socket

            raise socket.gaierror("fixture: no such name")
        return _getaddrinfo(*self.answers)(host, port)

    def command(self, argv: list[str], text: str = "", *, tty: bool = True, **extra: Any) -> tuple[int, str, str]:
        """Parse ``argv`` like the CLI, add the arguments the parser does not
        carry yet (``network``), and run the providers command."""

        import contextlib
        import io
        from unittest import mock

        from charmer import cli
        import charmer.cli.consent as consent
        from charmer.cli.commands import providers as providers_cmd

        args = cli.build_parser().parse_args(argv)
        for name, value in extra.items():
            setattr(args, name, value)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(consent, "stdio_ttys", return_value=tty), contextlib.redirect_stderr(err):
            code = providers_cmd._providers_command(self.runtime, args, input_stream=io.StringIO(text),
                                                    output_stream=out, interactive=tty)
        return code, out.getvalue(), err.getvalue()

    def declaration(self, provider_id: str = KEYED_ID) -> dict:
        return strict_json.loads((operator.providers_dir(self.harness.env) / f"{provider_id}.json").read_bytes())

    def declare_with_line(self, network: str = "tailnet", base: str = "http://box.example.test:8010/v1") -> None:
        """The keyless LAN kind on ``network`` with one line (LAN_KEY)."""

        argv = ["providers", "add", LAN_ID, "--kind", "openai-compatible-lan", "--base-url", base, "--auth", "none",
                "--family", "boxfam"]
        code, _out, err = self.command(argv, network=network)
        self.assertEqual(code, 0, err)
        path = operator.providers_dir(self.harness.env) / f"{LAN_ID}.json"
        document = strict_json.loads(path.read_bytes())
        document["lines"] = line(LAN_KEY, "lanbox-chat-1")
        path.write_bytes(strict_json.pretty_file_bytes(document))

    def state_bytes(self) -> dict:
        return self.harness.state_bytes()


# Most end-to-end command rows use HTTPS on a private network; keyed plain
# HTTP has its own end-to-end row (KeyedPlainHttpTests).
ADD_KEYED =["providers", "add", KEYED_ID, "--kind", "openai-compatible", "--base-url",
             "https://box.example.test:8010/v1", "--auth", "bearer", "--secret-ref", f"env:{SECRET}",
             "--family", "boxfam"]
SCOPE_LINE = ("The key is sent only to this origin, and only to addresses in that network (checked at every "
              "connection). Approve this route? [y/N] ")
APPROVAL_TAIL = ("  network: tailnet (100.64.0.0/10, fd7a:115c:a1e0::/48) — HTTPS\n"
                 "  now resolves to: 100.88.12.34 (tailnet)\n" + SCOPE_LINE)
PLAIN_HTTP_APPROVAL = (
    "approve the credential route of provider box:\n"
    "  origin: http://box.example.test:8010\n"
    f"  auth: bearer from {SECRET} (value not shown; not set yet)\n"
    "  listing: none\n"
    "  network: tailnet (100.64.0.0/10, fd7a:115c:a1e0::/48) — plain HTTP: the key is encrypted only while the "
    "connection runs through your tailnet; on a carrier-grade NAT network these addresses are not yours\n"
    "  now resolves to: 100.88.12.34 (tailnet)\n" + SCOPE_LINE)


class NetworkCommandTests(NetworkCommandCase):
    def test_plain_http_approval_text(self) -> None:
        from charmer.cli.commands import providers as providers_cmd

        provider = layer_of({KEYED_ID: keyed_declaration("http://box.example.test:8010/v1")}).providers[KEYED_ID]
        lines = providers_cmd.network_approval(self.runtime, provider, "providers approve box", "nothing approved")
        self.assertEqual(providers_cmd._approval_text(provider, False, lines), PLAIN_HTTP_APPROVAL)
        # A route without a network keeps its text byte for byte.
        plain = layer_of({KEYED_ID: keyed_declaration("https://api.example.test/v1", network=None)}).providers[KEYED_ID]
        self.assertEqual(providers_cmd.network_approval(self.runtime, plain, "v", "o"), ())
        self.assertEqual(providers_cmd._approval_text(plain, True), (
            "approve the credential route of provider box:\n  origin: https://api.example.test\n"
            f"  auth: bearer from {SECRET} (value not shown; present)\n  listing: none\n"
            "The key is sent only to this origin. Approve this route? [y/N] "))

    def test_add_keyed_tailnet_approves_with_the_network_and_its_resolution(self) -> None:
        code, out, err = self.command(ADD_KEYED, "y\n", network="tailnet")
        self.assertEqual(code, 0, err)
        self.assertIn(APPROVAL_TAIL, err)
        self.assertEqual(self.lookups, ["box.example.test"])
        self.assertEqual(self.declaration()["provider"]["network"], "tailnet")
        snapshot = self.runtime.operator_snapshot()
        provider = snapshot.layer.providers[KEYED_ID]
        self.assertEqual(snapshot.layer.route_status[KEYED_ID], "approved")
        self.assertEqual(snapshot.ledger.routes[KEYED_ID]["rd"], provider.route_digest)
        self.assertIn(f"approved route {KEYED_ID}: bearer env:{SECRET} → https://box.example.test:8010", out)

    def test_an_answer_outside_the_network_refuses_and_writes_nothing(self) -> None:
        self.answers = ("100.88.12.34", "203.0.113.9")
        before = self.state_bytes()
        code, out, err = self.command(ADD_KEYED, "y\n", network="tailnet")
        self.assertEqual(code, 1, out + err)
        self.assertIn("providers add box: box.example.test resolves to 203.0.113.9 (public), outside network "
                      "tailnet — nothing written", err)
        self.assertNotIn("Approve this route", err)
        self.assertEqual(self.state_bytes(), before)

    def test_no_answer_is_said_and_the_approval_proceeds(self) -> None:
        self.answers = ()
        code, out, err = self.command(ADD_KEYED, "y\n", network="tailnet")
        self.assertEqual(code, 0, err)
        self.assertIn("  now resolves to: nothing — box.example.test does not resolve from here\n", err)

    def test_the_guard_comes_before_any_lookup(self) -> None:
        before = self.state_bytes()
        code, out, err = self.command(ADD_KEYED, "y\n", tty=False, network="tailnet")
        self.assertNotEqual(code, 0)
        self.assertIn("stdin/stdout is not a terminal", err)
        self.assertEqual(self.lookups, [])
        self.assertEqual(self.state_bytes(), before)

    def test_the_guard_refuses_inside_a_session_marker_for_every_network_path(self) -> None:
        from unittest import mock

        from charmer.cli import consent

        before = self.state_bytes()
        paths = ((ADD_KEYED, {"network": "tailnet"}),
                 (["providers", "add", "--preset", "vllm", "--as", "vbox", "--base-url", "http://box.example.test:8000/v1",
                   "--secret-file", "/nonexistent"], {"network": "tailnet"}),
                 (["providers", "approve", KEYED_ID], {}))
        for marker in consent.SESSION_MARKERS:
            for argv, extra in paths:
                with self.subTest(marker=marker, argv=argv[:3]), \
                        mock.patch.dict(self.runtime.environ, {marker: "1"}):
                    code, _out, err = self.command(argv, "y\n", **extra)
                    self.assertNotEqual(code, 0)
                    self.assertIn(f"{marker} set", err)
        self.assertEqual(self.lookups, [])
        self.assertEqual(self.state_bytes(), before)

    def test_approve_names_the_network_and_refuses_an_outside_answer(self) -> None:
        code, _out, err = self.command([*ADD_KEYED, "--declare-only"], network="lan")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.lookups, [])
        self.answers = ("100.88.12.34",)
        code, out, err = self.command(["providers", "approve", KEYED_ID], "y\n")
        self.assertEqual(code, 1, out + err)
        self.assertIn("providers approve box: box.example.test resolves to 100.88.12.34 (tailnet), outside network "
                      "lan — nothing approved", err)
        self.answers = ("192.168.1.10",)
        code, out, err = self.command(["providers", "approve", KEYED_ID], "y\n")
        self.assertEqual(code, 0, err)
        self.assertIn("  network: lan (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, fc00::/7) — HTTPS\n"
                      "  now resolves to: 192.168.1.10 (lan)\n" + SCOPE_LINE, err)

    def test_plain_http_risk_sentences(self) -> None:
        from charmer.cli.commands import providers as providers_cmd

        for network, base, sentence in (
                ("lan", "http://192.168.1.10:8000/v1", "plain HTTP: anyone on this network can read the key and "
                                                       "your prompts"),
                ("loopback", "http://127.0.0.1:18010/v1", "plain HTTP on this computer: any local program can "
                                                          "listen on that port")):
            with self.subTest(network=network):
                provider = layer_of({KEYED_ID: keyed_declaration(base, network=network)}).providers[KEYED_ID]
                lines = providers_cmd.network_approval(self.runtime, provider, "v", "o")
                self.assertEqual(lines[0], f"  network: {netclass_describe(network)} — {sentence}")
        self.assertEqual(self.lookups, [])  # literals: no lookup

    def test_loopback_literal_is_classified_without_a_lookup(self) -> None:
        argv = [*ADD_KEYED]
        argv[argv.index("https://box.example.test:8010/v1")] = "https://127.0.0.1:18010/v1"
        code, _out, err = self.command(argv, "y\n", network="loopback")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.lookups, [])
        self.assertIn("  network: loopback (127.0.0.0/8, ::1) — HTTPS\n  now resolves to: 127.0.0.1 (loopback)\n",
                      err)

    def test_closed_gate_and_wrong_kind_refuse_before_any_store_access(self) -> None:
        from unittest import mock

        from charmer.cli.commands import providers as providers_cmd

        before = self.state_bytes()
        self.runtime.catalog.docs["gateway"][catalog.AUDITS_KEY][operator.PRIVATE_NETWORK_AUDIT] = False
        with mock.patch.object(providers_cmd, "operator_context", side_effect=AssertionError("context")):
            code, _out, err = self.command(ADD_KEYED, "y\n", network="tailnet")
            self.assertEqual(code, 1)
            self.assertIn("providers add box: private-network routes are unavailable in this build", err)
            self.runtime.catalog.docs["gateway"][catalog.AUDITS_KEY][operator.PRIVATE_NETWORK_AUDIT] = True
            argv = [*ADD_KEYED]
            argv[argv.index("openai-compatible")] = "anthropic-compatible"
            code, _out, err = self.command(argv, "y\n", network="tailnet")
            self.assertEqual(code, 1)
            self.assertIn("providers add box: network applies to the OpenAI-compatible kinds", err)
            code, _out, err = self.command(ADD_KEYED, "y\n", network="wan")
            self.assertEqual(code, 2)
            self.assertIn("--network takes loopback, lan, tailnet", err)
        self.assertEqual(self.lookups, [])
        self.assertEqual(self.state_bytes(), before)

    def test_closed_gate_never_shows_a_stored_credential_held_in_a_line_key(self) -> None:
        from charmer import state

        state.atomic_write(self.harness.secret_file, f"{SECRET}={STORED_LINE_KEY}\n".encode())
        self.runtime.catalog.docs["gateway"][catalog.AUDITS_KEY][operator.PRIVATE_NETWORK_AUDIT] = False
        document = keyed_declaration()
        document["lines"] = line(STORED_LINE_KEY)
        directory = operator.providers_dir(self.harness.env)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{KEYED_ID}.json").write_bytes(raw(document))
        for argv, shown in ((["providers", "show", KEYED_ID, "--resolved"], operator.NETWORK_GATED),
                            (["providers", "show", KEYED_ID], operator.NETWORK_GATED),
                            (["providers", "validate"], operator.NETWORK_GATED),
                            (["providers", "list"], "box\toperator provider · not loaded · 1 problem(s)"),
                            (["providers", "list", "--json"], '"id": "box"')):
            with self.subTest(argv=argv[1:]):
                _code, out, err = self.command(argv)
                self.assertIn(shown, out + err)
                self.assertNotIn(STORED_LINE_KEY, out + err)
                self.assertNotIn("dummy-key", out + err)

    def test_keyless_lan_kind_on_the_tailnet_declares_without_approval(self) -> None:
        argv = ["providers", "add", LAN_ID, "--kind", "openai-compatible-lan", "--base-url",
                "http://box.example.test:8010/v1", "--auth", "none", "--family", "boxfam"]
        code, out, err = self.command(argv, network="tailnet")
        self.assertEqual(code, 0, err)
        self.assertIn("declared provider lanbox — keyless route, private network (tailnet)", out)
        self.assertEqual(self.declaration(LAN_ID)["provider"]["network"], "tailnet")
        self.assertEqual(self.lookups, [])

    def test_show_and_resolved_name_the_credential_transport(self) -> None:
        code, _out, err = self.command(ADD_KEYED, "y\n", network="tailnet")
        self.assertEqual(code, 0, err)
        code, out, err = self.command(["providers", "show", KEYED_ID])
        self.assertEqual(code, 0, err)
        self.assertIn(f"provider {KEYED_ID}: openai-compatible · https://box.example.test:8010 · auth bearer "
                      f"env:{SECRET} · bearer over HTTPS, private network (tailnet) · route approved", out)
        code, out, err = self.command(["providers", "show", KEYED_ID, "--resolved"])
        self.assertEqual(code, 0, err)
        document = strict_json.loads(out.encode())
        self.assertEqual(document["credential_transport"], "bearer over HTTPS, private network (tailnet)")
        self.assertEqual(document["provider"]["transport"]["network"], "tailnet")
        self.assertNotIn("api_key", out)

    def test_credential_transport_forms(self) -> None:
        from charmer.cli.commands import providers as providers_cmd

        cases = ((keyed_declaration(), KEYED_ID, "bearer over plain HTTP, private network (tailnet)"),
                 (keyed_declaration("https://100.88.12.34/v1"), KEYED_ID, "bearer over HTTPS, private network (tailnet)"),
                 (lan_declaration(), LAN_ID, "plain HTTP, private network (tailnet)"),
                 (lan_declaration("https://box.example.test/v1"), LAN_ID, "HTTPS, private network (tailnet)"),
                 (keyed_declaration("https://api.example.test/v1", network=None), KEYED_ID, None))
        for document, name, expected in cases:
            with self.subTest(expected=expected):
                provider = layer_of({name: document}).providers[name]
                self.assertEqual(providers_cmd.credential_transport(provider), expected)

    def test_setup_layer_plans_carry_the_network_lines_and_refusals(self) -> None:
        from charmer.setup import model, providers as setup_providers

        values = {"id": KEYED_ID, "kind": "openai-compatible", "url": "https://box.example.test:8010/v1",
                  "auth": "bearer", "family": "boxfam", "network": "tailnet"}
        plan = setup_providers.plan_add_endpoint(self.runtime, values)
        self.assertIn("network: tailnet (100.64.0.0/10, fd7a:115c:a1e0::/48) — HTTPS", plan.lines)
        self.assertIn("now resolves to: 100.88.12.34 (tailnet)", plan.lines)
        self.assertEqual(strict_json.loads(plan.document_bytes)["provider"]["network"], "tailnet")
        self.answers = ("203.0.113.9",)
        with self.assertRaises(model.Refused) as caught:
            setup_providers.plan_add_endpoint(self.runtime, values)
        self.assertEqual(str(caught.exception), "providers add box: box.example.test resolves to 203.0.113.9 "
                                                "(public), outside network tailnet — nothing written")
        with self.assertRaises(model.Refused) as caught:
            setup_providers.plan_add_endpoint(self.runtime, {**values, "kind": "anthropic-compatible"})
        self.assertEqual(str(caught.exception), "network applies to the OpenAI-compatible kinds")
        preset = setup_providers.plan_add_preset(self.runtime, "vllm", "vbox", "http://box.example.test:8000/v1",
                                                 network="tailnet")
        self.assertEqual(preset.lines, ("vbox: OpenAI-compatible server at http://box.example.test:8000/v1 (no key), "
                                        "private network (tailnet): the gateway connects only to addresses in that "
                                        "network. Nothing is sent now.",))
        self.assertEqual(strict_json.loads(preset.document_bytes)["provider"]["network"], "tailnet")
        self.answers = ("100.88.12.34",)
        code, _out, err = self.command([*ADD_KEYED, "--declare-only"], network="tailnet")
        self.assertEqual(code, 0, err)
        approve = setup_providers.plan_approve(self.runtime, KEYED_ID)
        self.assertIn("now resolves to: 100.88.12.34 (tailnet)", approve.lines)
        self.answers = ("192.168.1.10",)
        with self.assertRaises(model.Refused):
            setup_providers.plan_approve(self.runtime, KEYED_ID)

    def test_template_and_preset_carry_the_network(self) -> None:
        code, out, err = self.command(["providers", "template", "--kind", "openai-compatible"], network="tailnet")
        self.assertEqual(code, 0, err)
        template = strict_json.loads(out.encode())
        self.assertEqual(template["provider"]["network"], "tailnet")
        self.assertEqual(template["provider"]["base_url"], "http://box.example.invalid:8000/v1")
        self.assertFalse(operator.file_problems(layer_of({"tmpl": template}), "tmpl"))
        code, out, err = self.command(["providers", "template", "--kind", "openai-compatible"])
        self.assertNotIn("network", out)
        code, out, err = self.command(["providers", "add", "--preset", "vllm", "--as", "vbox", "--base-url",
                                       "http://box.example.test:8000/v1"], network="tailnet")
        self.assertEqual(code, 0, err)
        provider = self.declaration("vbox")["provider"]
        self.assertEqual(provider["network"], "tailnet")
        self.assertEqual(provider["listing"]["url"], "http://box.example.test:8000/v1/models")
        self.assertIn("keyless route, private network (tailnet)", out)


LISTING_BODY = b'{"object": "list", "data": [{"id": "box-chat-1", "object": "model"}]}'


class _Fake:
    """A loopback HTTP fake this test starts: counts requests and the
    Authorization headers it saw; ``redirect`` answers 307 elsewhere."""

    def __init__(self, test: unittest.TestCase, *, redirect: str | None = None, proxy: bool = False):
        import http.server
        import threading

        fake = self
        self.requests: list[str] = []
        self.authorization: list[str | None] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 (http.server API)
                fake.requests.append(self.path)
                fake.authorization.append(self.headers.get("Authorization"))
                if redirect is not None:
                    self.send_response(307)
                    self.send_header("Location", redirect)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(LISTING_BODY)))
                self.end_headers()
                self.wfile.write(LISTING_BODY)

            def log_message(self, *_args: Any) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        test.addCleanup(thread.join, 5)
        test.addCleanup(self.server.server_close)
        test.addCleanup(self.server.shutdown)


class _Seen:
    """A socket that reports another remote or local address (what a
    socket behind a NAT or a missing overlay would report)."""

    def __init__(self, sock: Any, *, peer: tuple | None = None, local: tuple | None = None):
        self._sock, self._peer, self._local = sock, peer, local

    def getpeername(self) -> tuple:
        return self._peer or self._sock.getpeername()

    def getsockname(self) -> tuple:
        return self._local or self._sock.getsockname()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._sock, name)


class ListingGuardTests(unittest.TestCase):
    """``proxy.bounded_get`` with a declared network: the address filter,
    the socket re-check, the local-address rule, no proxy, no redirect."""

    PORTS = (8316, 8317)

    def setUp(self) -> None:
        self.fake = _Fake(self)
        self.dials: list[tuple] = []

    def url(self, port: int | None = None) -> str:
        return f"http://box.example.test:{port or self.fake.port}/v1/models"

    def connector(self, *, peer: tuple | None = None, local: tuple | None = None):
        import socket

        def connect(info: tuple, timeout: float) -> Any:
            self.dials.append(info[4])
            sock = socket.create_connection(("127.0.0.1", self.fake.port), timeout=timeout)
            return _Seen(sock, peer=peer, local=local) if (peer or local) else sock

        return connect

    def get(self, network: str | None, *answers: str, connector: Any = None, url: str | None = None) -> bytes:
        from charmer import proxy

        return proxy.bounded_get(url or self.url(), {"Authorization": "Bearer nettest-dummy-key"}, deadline=5.0,
                                 network=network, gateway_ports=self.PORTS, resolver=_getaddrinfo(*answers),
                                 connector=connector)

    def assertRefused(self, call: Any) -> None:
        from charmer import proxy

        with self.assertRaises(proxy.ListingFailure) as caught:
            call()
        self.assertEqual(caught.exception.kind, "network")
        self.assertEqual(self.fake.requests, [])

    def test_in_network_answer_is_dialled_by_address(self) -> None:
        self.assertEqual(self.get("loopback", "127.0.0.1"), LISTING_BODY)
        self.assertEqual(self.fake.authorization, ["Bearer nettest-dummy-key"])

    def test_rebinding_to_an_outside_answer_sends_nothing(self) -> None:
        self.assertRefused(lambda: self.get("loopback", "203.0.113.9", connector=self.connector()))
        self.assertEqual(self.dials, [])
        self.assertRefused(lambda: self.get("tailnet", "192.168.1.10", connector=self.connector()))
        self.assertEqual(self.dials, [])

    def test_mixed_answers_dial_only_the_allowed_ones(self) -> None:
        self.assertEqual(self.get("loopback", "203.0.113.9", "169.254.169.254", "127.0.0.1",
                                  connector=self.connector()), LISTING_BODY)
        self.assertEqual([address[0] for address in self.dials], ["127.0.0.1"])

    def test_the_socket_is_checked_again_before_a_byte_is_written(self) -> None:
        self.assertRefused(lambda: self.get("loopback", "127.0.0.1",
                                            connector=self.connector(peer=("203.0.113.9", 80))))
        self.assertEqual(len(self.dials), 1)

    def test_tailnet_needs_a_tailnet_local_address(self) -> None:
        answer = "100.88.12.34"
        peer = (answer, self.fake.port)
        self.assertRefused(lambda: self.get("tailnet", answer,
                                            connector=self.connector(peer=peer, local=("192.168.1.10", 50000))))
        self.assertRefused(lambda: self.get("tailnet", answer,
                                            connector=self.connector(peer=peer, local=("100.100.0.7", 50000))))
        self.assertEqual(self.get("tailnet", answer, connector=self.connector(peer=peer,
                                                                             local=("100.88.12.35", 50000))),
                         LISTING_BODY)
        # Other networks have no local-address rule.
        self.assertEqual(self.get("loopback", "127.0.0.1", connector=self.connector(local=("192.168.1.10", 50000))),
                         LISTING_BODY)

    def test_gateway_ports_are_refused_without_a_dial(self) -> None:
        for port in self.PORTS:
            with self.subTest(port=port):
                self.assertRefused(lambda: self.get("loopback", "127.0.0.1", connector=self.connector(),
                                                    url=self.url(port)))
        self.assertEqual(self.dials, [])

    def test_no_proxy_from_the_environment(self) -> None:
        import os
        from unittest import mock

        proxy_server = _Fake(self)
        environ = {key: value for key, value in os.environ.items() if key.lower() not in ("no_proxy", "all_proxy")}
        environ.update(HTTP_PROXY=f"http://127.0.0.1:{proxy_server.port}", http_proxy=f"http://127.0.0.1:{proxy_server.port}")
        from charmer import proxy

        with mock.patch.dict(os.environ, environ, clear=True):
            self.assertEqual(self.get("loopback", "127.0.0.1"), LISTING_BODY)
            self.assertEqual(proxy_server.requests, [])
            self.assertEqual(len(self.fake.requests), 1)
            # The control: without a network the same request honours the proxy.
            proxy.bounded_get(f"http://127.0.0.1:{self.fake.port}/v1/models", {}, deadline=5.0)
        self.assertEqual(len(proxy_server.requests), 1)
        self.assertEqual(len(self.fake.requests), 1)

    def test_redirects_are_refused(self) -> None:
        from charmer import proxy

        target = _Fake(self)
        for location in (f"http://127.0.0.1:{target.port}/v1/models", f"https://127.0.0.1:{target.port}/v1/models",
                         "/v1/other"):
            with self.subTest(location=location):
                redirecting = _Fake(self, redirect=location)
                with self.assertRaises(proxy.ListingFailure) as caught:
                    proxy.bounded_get(f"http://box.example.test:{redirecting.port}/v1/models",
                                      {"Authorization": "Bearer nettest-dummy-key"}, deadline=5.0, network="loopback",
                                      gateway_ports=self.PORTS, resolver=_getaddrinfo("127.0.0.1"))
                self.assertEqual(caught.exception.kind, "redirect")
                self.assertEqual(len(redirecting.requests), 1)
        self.assertEqual(target.requests, [])

    def test_refusal_text_is_value_free(self) -> None:
        from charmer import proxy

        self.assertEqual(str(proxy.ListingFailure("network", detail="tailnet")),
                         "address refused: outside the provider's private network (tailnet)")


class DiscoverNetworkTests(NetworkCommandCase):
    """``charmer discover`` on a provider declared on a private network: the
    policy is in the plan, its consent and its stale check, and reaches the
    transport explicitly."""

    def setUp(self) -> None:
        super().setUp()
        self.sent: list[dict] = []
        self.runtime.listing_transport = self._transport

    def _transport(self, url: str, headers: dict, **kwargs: Any) -> bytes:
        self.sent.append({"url": url, "headers": dict(headers), **kwargs})
        return LISTING_BODY

    def listing(self, provider_id: str, confirm: Any):
        from unittest import mock

        import charmer.cli.consent as consent
        from charmer.cli.commands import discover

        with mock.patch.object(consent, "stdio_ttys", return_value=True):
            return discover.listing(self.runtime, provider_id, confirm=confirm, notice=lambda _text: None)

    def declare_lan(self, network: str = "loopback", base: str = "http://box.example.test:18010/v1") -> None:
        argv = ["providers", "add", LAN_ID, "--kind", "openai-compatible-lan", "--base-url", base, "--auth", "none",
                "--family", "boxfam"]
        code, _out, err = self.command(argv, network=network)
        self.assertEqual(code, 0, err)

    def test_the_network_travels_from_the_plan_to_the_transport(self) -> None:
        self.declare_lan("tailnet")
        shown: list[str] = []
        observed = self.listing(LAN_ID, lambda text: shown.append(text) or True)
        self.assertIsNotNone(observed)
        call, _result = observed
        self.assertEqual(call.network, "tailnet")
        self.assertIn("     network: tailnet — a direct connection (no proxy), only to addresses in that network; "
                      "this computer's address must be a tailnet address too", shown[0])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["network"], "tailnet")
        self.assertEqual(self.sent[0]["url"], "http://box.example.test:18010/v1/models")
        self.assertIn(8317, self.sent[0]["gateway_ports"])

    def test_keyed_listing_carries_the_key_only_with_the_policy(self) -> None:
        code, _out, err = self.command(ADD_KEYED, "y\n", network="tailnet")
        self.assertEqual(code, 0, err)
        with self.harness.secret_file.open("a") as stream:
            stream.write(f"{SECRET}=nettest-dummy-key\n")
        observed = self.listing(KEYED_ID, lambda _text: True)
        self.assertIsNotNone(observed)
        self.assertEqual([(sent["network"], sent["headers"].get("Authorization")) for sent in self.sent],
                         [("tailnet", "Bearer nettest-dummy-key")])

    def test_a_policy_change_between_confirmation_and_send_sends_nothing(self) -> None:
        from charmer.cli.commands import providers as providers_cmd

        self.declare_lan("tailnet")

        def confirm(_text: str) -> bool:
            path = operator.providers_dir(self.harness.env) / f"{LAN_ID}.json"
            document = strict_json.loads(path.read_bytes())
            document["provider"]["network"] = "lan"
            path.write_bytes(strict_json.pretty_file_bytes(document))
            return True

        with self.assertRaises(providers_cmd.OperatorCommandError) as caught:
            self.listing(LAN_ID, confirm)
        self.assertIn("configuration changed while awaiting confirmation — nothing sent", str(caught.exception))
        self.assertEqual(self.sent, [])

    def test_providers_without_a_network_keep_their_call(self) -> None:
        self.declare_lan(network=None, base="http://box.lan:18010/v1")
        observed = self.listing(LAN_ID, lambda _text: True)
        self.assertIsNotNone(observed)
        self.assertIsNone(observed[0].network)
        self.assertEqual(self.sent, [{"url": "http://box.lan:18010/v1/models", "headers": self.sent[0]["headers"],
                                      "deadline": 20.0, "max_bytes": 4 * 1024 * 1024}])


class DoctorNetworkTests(NetworkCommandCase):
    """Doctor, launch readiness and the reload check for providers declared
    on a private network: a TCP connect only, the resolution named."""

    def observe(self, *answers: tuple[str, str], state: str = "reachable", reason: str | None = None):
        from charmer.cli import gateway_facts

        probes: list[str] = []

        def probe(url: str) -> Any:
            probes.append(url)
            return gateway_facts.LanReachability(state, "box.example.test", reason, tuple(answers))

        self.runtime.lan_probe = probe
        return probes

    def test_doctor_names_the_transport_and_the_resolution(self) -> None:
        from charmer.cli import doctor

        self.declare_with_line()
        probes = self.observe(("100.88.12.34", "tailnet"))
        attention: list[str] = []
        info = doctor._doctor_lan_report(self.runtime, attention, {})
        self.assertEqual(info, ["provider lanbox: plain HTTP, private network (tailnet); box.example.test resolves to "
                                "100.88.12.34 (tailnet)"])
        self.assertEqual(attention, [])
        self.assertEqual(probes, ["http://box.example.test:8010/v1"])

    def test_doctor_warns_about_an_answer_outside_the_network(self) -> None:
        from charmer.cli import doctor

        self.declare_with_line()
        self.observe(("100.88.12.34", "tailnet"), ("203.0.113.9", "public"))
        attention: list[str] = []
        doctor._doctor_lan_report(self.runtime, attention, {})
        self.assertEqual(attention, ["provider lanbox: box.example.test resolves to 203.0.113.9 (public), outside its "
                                     "network (tailnet): the gateway refuses those connections"])

    def test_only_outside_answers_make_the_provider_unreachable(self) -> None:
        from charmer.cli import doctor

        self.declare_with_line()
        self.observe(("203.0.113.9", "public"), state="unreachable", reason="outside")
        attention: list[str] = []
        info = doctor._doctor_lan_report(self.runtime, attention, {})
        self.assertIn("provider lanbox: not reachable from this network: box.example.test resolves only to addresses "
                      "outside its network — connect to the host's network, or bind another model (network-scoped; "
                      "its lines are unusable from here)", info)

    def test_probe_connects_only_to_addresses_in_the_network(self) -> None:
        from charmer.cli import gateway_facts

        tried: list[tuple] = []
        closed: list[bool] = []
        local = ["100.101.0.9"]

        class Sock:
            def __init__(self, peer: tuple) -> None:
                self.peer = peer

            def getpeername(self) -> tuple:
                return self.peer

            def getsockname(self) -> tuple:
                return (local[0], 40000)

            def close(self) -> None:
                closed.append(True)

        def connect(entry: tuple, _share: float) -> Any:
            tried.append(entry[4])
            return Sock(entry[4])

        found = gateway_facts.probe_lan("http://box.example.test:8010/v1", network="tailnet", gateway_ports=(8317,),
                                        resolve=_getaddrinfo("203.0.113.9", "100.88.12.34"), connect=connect)
        self.assertEqual((found.state, found.answers), ("reachable", (("203.0.113.9", "public"),
                                                                      ("100.88.12.34", "tailnet"))))
        self.assertEqual([address[0] for address in tried], ["100.88.12.34"])
        tried.clear()
        outside = gateway_facts.probe_lan("http://box.example.test:8010/v1", network="lan",
                                          resolve=_getaddrinfo("100.88.12.34"), connect=connect)
        self.assertEqual((outside.state, outside.reason), ("unreachable", "outside"))
        self.assertEqual(tried, [])

        class Plain:
            def close(self) -> None:
                closed.append(True)

        # Without a network the probe is the LAN kind's, unchanged.
        plain = gateway_facts.probe_lan("http://box.lan:8010/v1", resolve=_getaddrinfo("203.0.113.9"),
                                        connect=lambda _entry, _share: Plain())
        self.assertEqual((plain.state, plain.answers), ("reachable", ()))

    def test_probe_applies_the_connect_time_rules_of_the_gateway(self) -> None:
        from charmer.cli import gateway_facts

        closed: list[str] = []

        class Sock:
            def __init__(self, peer: tuple, local: str) -> None:
                self.peer, self.local = peer, local

            def getpeername(self) -> tuple:
                return self.peer

            def getsockname(self) -> tuple:
                return (self.local, 40000)

            def close(self) -> None:
                closed.append(self.peer[0])

        url = "http://box.example.test:8010/v1"
        # The tailnet local-address rule: this computer's address must be a tailnet address too.
        off = gateway_facts.probe_lan(url, network="tailnet", resolve=_getaddrinfo("100.88.12.34"),
                                      connect=lambda entry, _share: Sock(entry[4], "192.168.1.5"))
        self.assertEqual((off.state, off.reason), ("unreachable", "local"))
        self.assertEqual(off.text(), "not reachable from this network: box.example.test is reached only from a "
                                     "local address outside the tailnet")
        self.assertEqual(closed, ["100.88.12.34"])
        # The connected peer is checked again (not only the resolved answer).
        moved = gateway_facts.probe_lan(url, network="tailnet", resolve=_getaddrinfo("100.88.12.34"),
                                        connect=lambda entry, _share: Sock(("203.0.113.9", 8010), "100.101.0.9"))
        self.assertEqual((moved.state, moved.reason), ("unreachable", "outside"))
        # A socket that cannot say where it is connected is not reachable.
        class Mute:
            def close(self) -> None:
                pass

        mute = gateway_facts.probe_lan(url, network="lan", resolve=_getaddrinfo("192.168.1.10"),
                                       connect=lambda _entry, _share: Mute())
        self.assertEqual((mute.state, mute.reason), ("unreachable", "unreachable"))
        # The local rule is the tailnet's only.
        lan = gateway_facts.probe_lan(url, network="lan", resolve=_getaddrinfo("192.168.1.10"),
                                      connect=lambda entry, _share: Sock(entry[4], "192.168.1.5"))
        self.assertEqual(lan.state, "reachable")

    def test_verify_reload_requires_the_private_selectors_of_the_render_it_verifies(self) -> None:
        from types import SimpleNamespace
        from unittest import mock

        from charmer import proxy
        from charmer.cli import runtime as runtime_mod

        seen: list[tuple[str, Any]] = []

        def wait(_gateway: Any, _token: str, sentinel: str, **kwargs: Any) -> Any:
            seen.append((sentinel, kwargs.get("private_aliases")))
            return proxy.ReloadResult("reloaded", "fixture")

        self.runtime._remember_render(SimpleNamespace(sentinel="charmer-render-0123abcd",
                                                      private_aliases=("custom-box-chat",)))
        with mock.patch.object(proxy, "await_sentinel", side_effect=wait), \
                mock.patch.object(proxy, "published_private_network_selectors",
                                  side_effect=lambda _home, sentinel: frozenset({f"published-for-{sentinel}"})):
            # The instance's verify_reload is the harness's instant fake.
            runtime_mod.Runtime.verify_reload(self.runtime, "charmer-render-0123abcd")
            runtime_mod.Runtime.verify_reload(self.runtime, "charmer-render-ffffffff")
        self.assertEqual(seen, [("charmer-render-0123abcd", ("custom-box-chat",)),
                                ("charmer-render-ffffffff", frozenset({"published-for-charmer-render-ffffffff"}))])

    def test_observations_are_shared_by_url_and_network_policy(self) -> None:
        from types import SimpleNamespace

        from charmer.cli import doctor, gateway_facts

        base = "http://box.lan:8010/v1"
        self.declare_with_line(base=base)
        second = lan_declaration(base, network=None)
        second["lines"] = line("custom-lanbox-two", "lanbox-chat-2")
        (operator.providers_dir(self.harness.env) / "lanbox-two.json").write_bytes(raw(second))
        probes = self.observe(("100.88.12.34", "tailnet"))
        shared: dict = {}
        doctor._doctor_lan_report(self.runtime, [], shared)
        # The same URL under another network policy is another observation.
        self.assertEqual(probes, [base, base])
        self.assertEqual(set(shared), {(base, None), (base, "tailnet")})
        # Readiness reuses only an observation of the same URL and policy.
        probes.clear()
        stale = gateway_facts.LanReachability("unreachable", "box.lan", "refused")
        shared = {(base, None): stale}
        lineup = SimpleNamespace(lead=SimpleNamespace(binding=SimpleNamespace(provider=LAN_ID)), agents={})
        observed = self.runtime.readiness_observations([lineup], served=False, lan_observed=shared)
        self.assertEqual(probes, [base])
        self.assertEqual(observed.lan[LAN_ID][0], "reachable")
        self.assertEqual(shared[(base, "tailnet")].state, "reachable")
        probes.clear()
        shared = {(base, "tailnet"): stale}
        observed = self.runtime.readiness_observations([lineup], served=False, lan_observed=shared)
        self.assertEqual(probes, [])
        self.assertEqual(observed.lan[LAN_ID][0], "unreachable")

    def test_readiness_observes_private_network_providers(self) -> None:
        from charmer import readiness

        keyed = {"transport": {"kind": "direct-openai", "base_url": "http://box.example.test:8010/v1",
                               "auth": {"kind": "bearer", "secret_ref": "env:BOX_API_KEY"}, "network": "tailnet"}}
        self.assertTrue(readiness.probes_reachability(keyed))
        self.assertTrue(readiness.probes_reachability(
            {"transport": {"kind": "direct-openai", "auth": {"kind": "none"}, "base_url": "http://box.lan"}}))
        self.assertFalse(readiness.probes_reachability(
            {"transport": {"kind": "direct-openai", "auth": {"kind": "bearer"}, "base_url": "https://x.example"}}))
        self.assertFalse(readiness.probes_reachability({"transport": {"kind": "oauth-pool", "pool": "claude"}}))

    def test_an_unreachable_private_network_slot_is_not_ready(self) -> None:
        from types import SimpleNamespace

        from charmer import readiness

        provider = {"transport": {"kind": "direct-openai", "base_url": "http://box.example.test:8010/v1",
                                  "auth": {"kind": "bearer", "secret_ref": "env:BOX_API_KEY"}, "network": "tailnet"}}
        binding = SimpleNamespace(provider=KEYED_ID, selector=KEY, key=KEY)
        text = "not reachable from this network: box.example.test resolves only to addresses outside its network"
        row = readiness.slot_readiness("lead", binding, {KEYED_ID: provider},
                                       readiness.Observations(served=frozenset({KEY}),
                                                              lan={KEYED_ID: ("unreachable", text)}))
        self.assertEqual(row.state, readiness.BLOCKED)
        self.assertEqual((row.first.code, row.first.text), ("lan", text))
        reachable = readiness.slot_readiness("lead", binding, {KEYED_ID: provider},
                                             readiness.Observations(served=frozenset({KEY}),
                                                                    lan={KEYED_ID: ("reachable", "")}))
        self.assertEqual(reachable.state, readiness.READY)

    def test_the_runtime_knows_when_a_private_network_is_declared(self) -> None:
        self.assertFalse(self.runtime.declares_private_network())
        self.declare_with_line()
        self.assertTrue(self.runtime.declares_private_network())

    def test_provider_host_resolution_uses_the_seam_and_never_a_lookup_in_a_fixture(self) -> None:
        from unittest import mock

        found = self.runtime.resolve_provider_host("https://box.example.test:8010/v1")
        self.assertEqual((found.state, found.answers), ("resolved", (("100.88.12.34", "tailnet"),)))
        self.assertEqual(self.lookups, ["box.example.test"])
        literal = self.runtime.resolve_provider_host("http://[::ffff:192.168.1.10]:8000/v1")
        self.assertEqual((literal.state, literal.text()), ("resolved", "192.168.1.10 (lan)"))
        self.assertEqual(self.lookups, ["box.example.test"])
        self.answers = ()
        missing = self.runtime.resolve_provider_host("https://box.example.test:8010/v1")
        self.assertEqual((missing.state, missing.text()),
                         ("none", "nothing — box.example.test does not resolve from here"))
        self.runtime.host_resolver = None
        with mock.patch.object(self.runtime, "lan_probe", lambda *_args, **_kwargs: None):
            unchecked = self.runtime.resolve_provider_host("https://box.example.test:8010/v1")
            self.assertEqual((unchecked.state, unchecked.text()), ("unknown", "not checked"))
            self.assertEqual(self.runtime.resolve_provider_host("http://100.88.12.34:8000/v1").answers,
                             (("100.88.12.34", "tailnet"),))
        self.assertEqual(self.lookups, ["box.example.test", "box.example.test"])

    def test_restart_pending_names_private_network_providers(self) -> None:
        from pathlib import Path
        from types import SimpleNamespace
        from unittest import mock

        from charmer import endpoint as endpoint_mod
        from charmer.cli import gateway_facts

        class Drift(str):
            restart_pending = True

        class Optional(str):
            restart_pending = False

        hold = SimpleNamespace(held=False, coverage_only=False, source="journal", evaluated="",
                               journald_acknowledged=False)

        def report(drift: str, declared: bool) -> list[str]:
            gateway = SimpleNamespace(
                unit="charmer-gateway.service", config=None, backend=endpoint_mod.SYSTEMD,
                base_url="http://127.0.0.1:18317", remedy_backend=None, recent_starts=lambda: [],
                persistence_hold=lambda: hold, binary_drift=lambda _seen: drift,
                observe=lambda: SimpleNamespace(lock=True, stamp=None, state="ours", detail="fixture"))
            runtime = SimpleNamespace(endpoint_error=None, gateway=lambda: gateway, gateway_seams=object(),
                                      environ={}, home=Path("/nonexistent"), health_get=None,
                                      served_models_callback=None, declares_private_network=lambda: declared)
            with mock.patch.object(endpoint_mod, "set_up", return_value=True):
                attention, _info = gateway_facts.gateway_runtime_report(runtime)
            return [line for line in attention if "private-network" in line]

        lines = report(Drift("gateway restart pending: fixture"), True)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("private-network providers are served only after the gateway restarts: `"),
                        lines)
        self.assertEqual(report(Drift("gateway restart pending: fixture"), False), [])
        self.assertEqual(report(Optional("gateway same binary on another path; restart optional"), True), [])

    def test_qualify_route_text_names_the_network(self) -> None:
        from charmer import qualify

        provider = layer_of({KEYED_ID: keyed_declaration()}).providers[KEYED_ID]
        self.assertEqual(qualify.route_text(KEYED_ID, provider.entry, t2=provider), (
            f"http://100.88.12.34:8010; auth bearer from {SECRET} (value not shown); private network (tailnet)",
            "100.88.12.34"))
        plain = layer_of({KEYED_ID: keyed_declaration("https://api.example.test/v1", network=None)}).providers[KEYED_ID]
        self.assertEqual(qualify.route_text(KEYED_ID, plain.entry, t2=plain)[0],
                         f"https://api.example.test; auth bearer from {SECRET} (value not shown)")


# A dummy key value (no token shape): the leak tests look for it everywhere.
SENTINEL_KEY = "boxco-dummy-sentinel-key-value"
PLAIN_ID = "plainbox"
PLAIN_KEY = "custom-plainbox-chat"


def network_plan(files: dict[str, dict], *, docs: dict | None = None,
                 aliases: dict[str, dict] | None = None) -> operator.RenderPlan:
    """Validated declarations with approved keyed routes through the real render plan."""

    import test_operator as op_tests

    docs = docs or network_docs()
    open_docs = network_docs()
    first = layer_of(files, docs=open_docs)
    routes = {pid: op_tests._route(provider) for pid, provider in first.providers.items()
              if provider.auth_kind != "none"}
    ledger = op_tests._ledger(routes=routes, aliases=aliases or {})
    return operator.render_plan(docs, layer_of(files, docs=docs, ledger=ledger), ledger)


def render_network(plan: operator.RenderPlan) -> Any:
    from pathlib import Path

    from charmer import render

    return render.render_config(
        plan.docs["gateway"], plan.docs["providers"]["providers"], plan.docs["models-v2"]["models"],
        home=Path("/fixture/home"), gateway_tokens=("t" * 64,),
        resolve_secret=lambda name: SENTINEL_KEY if name == SECRET else f"dummy-{name.lower().replace('_', '-')}",
        continuity={}, captures=plan.captures, provider_headers=plan.headers, oauth_overlay=plan.overlay,
        quiet_providers=plan.quiet_providers)


def network_files() -> dict[str, dict]:
    plain = lan_declaration("http://box.lan:8000/v1", network=None)
    plain["lines"] = line(PLAIN_KEY, "plainbox-chat-1")
    return {KEYED_ID: keyed_declaration("http://box.example.test:8010/v1", network="lan",
                                        headers={"X-Client": "charmer"}),
            LAN_ID: lan_declaration("http://box.example.test:8010/v1", network="tailnet"), PLAIN_ID: plain}


def compat_by_name(result: Any) -> dict[str, dict]:
    from charmer import served_plan

    document = served_plan.parse_restricted_yaml(result.yaml)
    return {section["name"]: section for section in document["openai-compatibility"]}


class NetworkRenderTests(unittest.TestCase):
    """The gateway's config shape: the ``private-network`` block instead of ``base-url``."""

    def test_a_network_provider_renders_the_block_instead_of_the_base_url(self) -> None:
        result = render_network(network_plan(network_files()))
        sections = compat_by_name(result)
        keyed, lan, plain = sections[KEYED_ID], sections[LAN_ID], sections[PLAIN_ID]
        self.assertEqual(list(keyed), ["name", "private-network", "api-key-entries", "headers", "models"])
        self.assertEqual(keyed["private-network"], {"base-url": "http://box.example.test:8010/v1", "class": "lan"})
        self.assertEqual(keyed["api-key-entries"], [{"api-key": SENTINEL_KEY}])
        self.assertEqual(list(lan), ["name", "private-network", "models"])
        self.assertEqual(lan["private-network"], {"base-url": "http://box.example.test:8010/v1", "class": "tailnet"})
        # Without a network, the 2.4.0 shape.
        self.assertEqual(list(plain), ["name", "base-url", "models"])
        self.assertEqual(plain["base-url"], "http://box.lan:8000/v1")
        private = {model["alias"] for name in (KEYED_ID, LAN_ID) for model in sections[name]["models"]}
        self.assertTrue(private)
        self.assertEqual(result.private_aliases, tuple(sorted(private)))
        self.assertFalse({model["alias"] for model in plain["models"]} & set(result.private_aliases))
        self.assertLessEqual(set(result.private_aliases), result.served)
        # The models are rendered exactly as without a network.
        bare = {KEYED_ID: keyed_declaration("https://box.example.test:8010/v1", network=None,
                                            headers={"X-Client": "charmer"}),
                LAN_ID: lan_declaration("http://box.lan:8010/v1", network=None), PLAIN_ID: network_files()[PLAIN_ID]}
        before = compat_by_name(render_network(network_plan(bare)))
        for name in (KEYED_ID, LAN_ID):
            self.assertEqual(sections[name]["models"], before[name]["models"])
        for field in ("api-key-entries", "headers"):
            self.assertEqual(sections[KEYED_ID][field], before[KEYED_ID][field])

    def test_the_render_resolves_no_name(self) -> None:
        import socket
        from unittest import mock

        plan = network_plan(network_files())
        refuse = mock.Mock(side_effect=AssertionError("the render resolved a name"))
        with mock.patch.object(socket, "getaddrinfo", refuse), mock.patch.object(socket, "gethostbyname", refuse), \
                mock.patch.object(socket, "create_connection", refuse):
            render_network(plan)
        refuse.assert_not_called()

    def test_the_served_plan_puts_the_class_into_the_route_identity(self) -> None:
        from charmer import served_plan

        def routes(files: dict[str, dict]) -> dict:
            return served_plan.routes_from_document(
                served_plan.parse_restricted_yaml(render_network(network_plan(files)).yaml), strict=True)

        tailnet = routes(network_files())
        self.assertEqual(tailnet[LAN_KEY].route, "lanbox http://box.example.test:8010/v1 private network tailnet")
        self.assertEqual(tailnet[KEY].route, "box http://box.example.test:8010/v1 private network lan")
        self.assertEqual(tailnet[KEY].auth_header, "bearer")
        self.assertEqual(tailnet[PLAIN_KEY].route, "plainbox http://box.lan:8000/v1")
        files = network_files()
        files[LAN_ID] = lan_declaration("http://box.example.test:8010/v1", network="lan")
        lan = routes(files)
        self.assertEqual(lan[LAN_KEY].changed_parts(tailnet[LAN_KEY]), ("route",))
        self.assertEqual(lan[PLAIN_KEY], tailnet[PLAIN_KEY])
        self.assertEqual(served_plan.PRIVATE_NETWORK_KEY, __import__("charmer.render").render.PRIVATE_NETWORK_KEY)


class GateClosedControlTests(unittest.TestCase):
    """A closed private-network gate: nothing of the provider is rendered,
    served, captured or fenced."""

    def test_nothing_is_rendered_or_captured_for_a_gated_provider(self) -> None:
        files = network_files()
        first = layer_of(files)
        capture = {"provider": LAN_ID, "rd": first.providers[LAN_ID].route_digest, "wire": "lanbox-old-1",
                   "proxy_contract": None, "display": "LAN box old", "context_tokens": 32768,
                   "source": "operator:custom-lanbox-old", "since_catalog": 36, "overlay": None}
        aliases = {"custom-lanbox-old": capture}
        opened = network_plan(files, aliases=aliases)
        self.assertIn("custom-lanbox-old", opened.captures)
        open_result = render_network(opened)
        self.assertIn("custom-lanbox-old", open_result.served)
        closed = network_plan(files, docs=network_docs(network=False), aliases=aliases)
        self.assertEqual(set(closed.captures), set())
        result = render_network(closed)
        names = set(compat_by_name(result))
        self.assertEqual(names & {KEYED_ID, LAN_ID}, set())
        self.assertIn(PLAIN_ID, names)
        gated = {alias for alias in open_result.served if alias not in result.served}
        self.assertTrue({"custom-lanbox-old"} <= gated)
        self.assertEqual(result.private_aliases, ())
        self.assertNotIn(SENTINEL_KEY, result.yaml)


class GateClosedFenceTests(NetworkCommandCase):
    def fence_models(self) -> set[str]:
        """The fence of an ad-hoc lineup whose configured workflow default
        names the provider's line (the one unbound operator selector a fence
        gains)."""

        import dataclasses

        from charmer import profile, scope

        lcat = self.runtime.lineup_catalog()
        eff = dataclasses.replace(self.runtime.current_effective(),
                                  workflow_default_binding={"model": LAN_KEY, "effort": "high"})
        lead = next(key for key, entry in lcat.lines.items()
                    if "lead" in entry.get("capabilities", ()) and entry["provider"] != LAN_ID)
        lineup = profile.resolve(profile.ad_hoc_direct(lead), lcat, effective=eff, ad_hoc=True)
        return set(scope.compile_fence(lineup, lcat, eff).available_models)

    def test_a_gated_provider_never_reaches_a_compiled_fence(self) -> None:
        self.declare_with_line()
        entry = self.runtime.lineup_catalog().lines[LAN_KEY]
        selectors = {selector for _level, selector, _contract in catalog.line_selectors(dict(entry))}
        self.assertTrue(selectors & self.fence_models())
        self.runtime.catalog.docs["gateway"][catalog.AUDITS_KEY][operator.PRIVATE_NETWORK_AUDIT] = False
        self.assertNotIn(LAN_KEY, self.runtime.lineup_catalog().lines)
        self.assertFalse(selectors & self.fence_models())
        self.assertFalse(selectors & set(self.runtime.operator_render_plan().docs["models-v2"]["models"]))


class ReloadCheckTests(unittest.TestCase):
    """A gateway without private-network support loads the render, sentinel
    included, and drops those entries: the reload check says so."""

    def wait(self, served: set[str], private: tuple[str, ...]):
        from charmer import proxy

        now = [0.0]

        def clock() -> float:
            return now[0]

        def sleep(seconds: float) -> None:
            now[0] += seconds

        gateway = {"gateway": {"base_url": "http://127.0.0.1:18317"}}
        return proxy.await_sentinel(gateway, "fixture-token", "charmer-render-0123abcd",
                                    models_get=lambda _base, _token: (200, set(served)), clock=clock, sleep=sleep,
                                    private_aliases=private)

    def test_unserved_private_selectors_need_a_restart(self) -> None:
        result = self.wait({"charmer-render-0123abcd", "custom-other"}, ("custom-box-chat",))
        self.assertEqual(result.status, "restart_required")
        self.assertTrue(result.message.startswith("gateway reloaded without the private-network selector(s) "
                                                  "custom-box-chat: the running gateway predates them — restart "
                                                  "required: "), result.message)

    def test_served_or_absent_private_selectors_reload(self) -> None:
        self.assertEqual(self.wait({"charmer-render-0123abcd", "custom-box-chat"}, ("custom-box-chat",)).status,
                         "reloaded")
        self.assertEqual(self.wait({"charmer-render-0123abcd"}, ()).status, "reloaded")

    def test_private_selectors_of_a_document(self) -> None:
        from charmer import proxy

        document = {"openai-compatibility": [
            {"name": "box", "private-network": {"base-url": "http://box.example.test:8010/v1", "class": "tailnet"},
             "models": [{"name": "box-chat-1", "alias": "custom-box-chat"}]},
            {"name": "lan", "base-url": "http://box.lan/v1", "models": [{"name": "w", "alias": "custom-lan"}]}]}
        self.assertEqual(proxy.private_network_selectors(document), frozenset({"custom-box-chat"}))
        self.assertEqual(proxy.private_network_selectors({}), frozenset())

    def test_every_reload_wait_passes_the_render_private_selectors(self) -> None:
        import ast
        import inspect

        from charmer import proxy

        calls = [node for node in ast.walk(ast.parse(inspect.getsource(proxy)))
                 if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "await_sentinel"]
        self.assertEqual(len(calls), 5)
        for node in calls:
            keywords = {keyword.arg: keyword.value for keyword in node.keywords}
            mode = keywords.get("mode")
            if isinstance(mode, ast.Constant) and mode.value == "start":
                continue
            with self.subTest(line=node.lineno):
                self.assertIn("private_aliases", keywords)
                self.assertEqual(ast.unparse(keywords["private_aliases"]), "result.private_aliases")

    def test_published_selectors_belong_to_the_render_with_that_sentinel(self) -> None:
        import shutil
        import tempfile
        from pathlib import Path

        from charmer import proxy, render, state

        home = Path(tempfile.mkdtemp(prefix="nettest-home-")).resolve()
        self.addCleanup(shutil.rmtree, home, True)
        document = {"openai-compatibility": [
            {"name": "box", "base-url": "http://box.example.test:8010/v1",
             "private-network": {"base-url": "http://box.example.test:8010/v1", "class": "tailnet"},
             "models": [{"name": "box-chat-1", "alias": "custom-box-chat"}]},
            {"name": "charmer-render", "base-url": "http://127.0.0.1:9/v1",
             "models": [{"name": "sentinel", "alias": "charmer-render-0123abcd"}]}]}
        state.ensure_private_dir(proxy.config_dir(home))
        state.atomic_write(proxy.config_dir(home) / "config.yaml", render.emit_yaml(document).encode())
        self.assertEqual(render.rendered_private_network_selectors(document), ("custom-box-chat",))
        self.assertEqual(proxy.published_private_network_selectors(home, "charmer-render-0123abcd"),
                         frozenset({"custom-box-chat"}))
        self.assertEqual(proxy.published_private_network_selectors(home, "charmer-render-ffffffff"), frozenset())
        self.assertEqual(proxy.published_private_network_selectors(home / "missing", "charmer-render-0123abcd"),
                         frozenset())


def _proxy_control_case() -> type:
    import test_proxy

    return test_proxy.ProxyControlTestCase


class ReloadEntryPointTests(_proxy_control_case()):
    """``charmer-proxy init --reload-check`` (the unit's ExecReload) needs
    the render's private-network selectors served: exit and stamp."""

    def test_reload_check_requires_the_private_selectors_of_its_render(self) -> None:
        import dataclasses
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from unittest import mock

        from charmer import proxy, sessions

        self.set_up_gateway()
        self._write_secret()
        self._init()
        original = proxy._publish_prepared_render

        def publish(*args: Any, **kwargs: Any):
            target, result, report = original(*args, **kwargs)
            return target, dataclasses.replace(result, private_aliases=("custom-box-chat",)), report

        for served, status in ((False, "restart_required"), (True, "reloaded")):
            now = [0.0]

            def models_get(base: str, token: str, served: bool = served) -> tuple[int, set[str]]:
                code, ids = self._models_get(base, token)
                return code, ids | ({"custom-box-chat"} if served else set())

            def sleep(seconds: float) -> None:
                now[0] += seconds

            with self.subTest(served=served), \
                    mock.patch.object(proxy, "_publish_prepared_render", side_effect=publish), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
                code = proxy.cmd_init(["--reload-check"], environ=self.environ, models_get=models_get,
                                      clock=lambda: now[0], sleep=sleep)
                self.assertEqual(code, proxy.RELOAD_CHECK_EXIT[status])
                stamp = proxy.service.read_reload_stamp(proxy.gateway_workdir(sessions.state_root(self.environ)))
                self.assertEqual(stamp["status"], status)
                if not served:
                    self.assertIn("gateway reloaded without the private-network selector(s) custom-box-chat",
                                  err.getvalue())


class CatalogNetworkGateTests(unittest.TestCase):
    """``catalog.keyed_compat_problem`` (T1 validation, T2 and the render
    precondition) takes the network: a keyed plain-HTTP base on a declared
    network passes; a closed private-network gate refuses any network entry."""

    def entry(self, document: dict, *, docs: dict | None = None) -> dict:
        return layer_of({KEYED_ID: document}, docs=docs or network_docs()).providers[KEYED_ID].entry

    def test_keyed_plain_http_on_a_network_passes_and_a_closed_gate_refuses(self) -> None:
        provider = self.entry(keyed_declaration("http://box.example.test:8010/v1", network="tailnet"))
        self.assertEqual(provider["transport"]["network"], "tailnet")
        self.assertIsNone(catalog.keyed_compat_problem(provider, network_docs()["gateway"]))
        self.assertEqual(catalog.keyed_compat_problem(provider, network_docs(network=False)["gateway"]),
                         catalog.PRIVATE_NETWORK_CLOSED)
        # The keyed gate first.
        self.assertEqual(catalog.keyed_compat_problem(provider, network_docs(keyed=False, network=False)["gateway"]),
                         catalog.KEYED_AUDIT_CLOSED)
        # The base URL is checked with the network, on every path.
        for base, text in (("http://203.0.113.9:8010/v1", "203.0.113.9 is not on a private network"),
                           ("http://192.168.1.10:8010/v1", "192.168.1.10 is on the lan network, not tailnet")):
            outside = copy.deepcopy(provider)
            outside["transport"]["base_url"] = base
            self.assertEqual(catalog.keyed_compat_problem(outside, network_docs()["gateway"]),
                             f"a keyed openai-compatible base URL is refused ({text})")
        bare = copy.deepcopy(provider)
        del bare["transport"]["network"]
        self.assertEqual(catalog.keyed_compat_problem(bare, network_docs()["gateway"]),
                         "a keyed openai-compatible base URL is refused (a secret is sent only over https)")

    def test_the_keyless_lan_kind_with_a_network_gets_the_same_gate(self) -> None:
        provider = layer_of({LAN_ID: lan_declaration()}).providers[LAN_ID].entry
        self.assertIsNone(catalog.keyed_compat_problem(provider, network_docs()["gateway"]))
        self.assertEqual(catalog.keyed_compat_problem(provider, network_docs(network=False)["gateway"]),
                         catalog.PRIVATE_NETWORK_CLOSED)
        plain = layer_of({LAN_ID: lan_declaration("http://box.lan:8010/v1", network=None)}).providers[LAN_ID].entry
        self.assertIsNone(catalog.keyed_compat_problem(plain, network_docs(keyed=False, network=False)["gateway"]))

    def test_one_accessor(self) -> None:
        self.assertIs(operator.private_network_audited, catalog.private_network_audited)
        self.assertEqual((operator.PRIVATE_NETWORK_AUDIT, operator.NETWORK_GATED),
                         (catalog.PRIVATE_NETWORK_AUDIT, catalog.PRIVATE_NETWORK_CLOSED))


class KeyedPlainHttpTests(NetworkCommandCase):
    """A keyed provider over plain HTTP on a declared network, end to end:
    declared, approved with the plain-HTTP sentence, rendered as the block."""

    def test_add_approve_and_render(self) -> None:
        from charmer import proxy, served_plan

        argv = [*ADD_KEYED]
        argv[argv.index("https://box.example.test:8010/v1")] = "http://box.example.test:8010/v1"
        code, out, err = self.command(argv, "y\n", network="tailnet")
        self.assertEqual(code, 0, err)
        self.assertIn(PLAIN_HTTP_APPROVAL, err)
        self.assertEqual(self.declaration()["provider"]["base_url"], "http://box.example.test:8010/v1")
        from charmer import state

        state.atomic_write(self.harness.secret_file, f"{SECRET}={LEAK_KEY}\n".encode())
        path = operator.providers_dir(self.harness.env) / f"{KEYED_ID}.json"
        document = strict_json.loads(path.read_bytes())
        document["lines"] = line()
        path.write_bytes(strict_json.pretty_file_bytes(document))
        code, out, err = self.harness.op(["providers", "apply"])
        self.assertEqual(code, 0, out + err)
        rendered = served_plan.parse_restricted_yaml((proxy.config_dir(self.runtime.home) / "config.yaml").read_text())
        section = next(item for item in rendered["openai-compatibility"] if item["name"] == KEYED_ID)
        self.assertEqual(section["private-network"], {"base-url": "http://box.example.test:8010/v1",
                                                      "class": "tailnet"})
        self.assertNotIn("base-url", section)
        self.assertEqual(section["api-key-entries"], [{"api-key": LEAK_KEY}])
        self.assertNotIn(LEAK_KEY, out + err)


LEAK_KEY = "boxco-dummy-leak-sentinel-value"


class NothingLeaksTests(NetworkCommandCase):
    """A key given for a private-network provider is in the
    secret store, the file it came from and the rendered config.yaml —
    nowhere else."""

    def test_the_key_is_only_in_the_store_its_source_file_and_the_rendered_config(self) -> None:
        import json
        import os
        import shutil
        import tempfile
        from pathlib import Path

        import charmer.cli.doctor as doctor_mod
        from charmer import proxy, state

        source_dir = Path(tempfile.mkdtemp(prefix="nettest-key-")).resolve()
        self.addCleanup(shutil.rmtree, source_dir, True)
        os.chmod(source_dir, 0o700)
        source = source_dir / "box.key"
        state.atomic_write(source, (LEAK_KEY + "\n").encode())
        outputs: dict[str, str] = {}
        code, out, err = self.command(ADD_KEYED, "y\n", network="tailnet")
        self.assertEqual(code, 0, err)
        outputs["providers add"] = out + err
        code, out, err = self.command(["providers", "set-key", KEYED_ID, "--secret-file", str(source)], "y\n")
        self.assertEqual(code, 0, err)
        outputs["providers set-key"] = out + err
        path = operator.providers_dir(self.harness.env) / f"{KEYED_ID}.json"
        document = strict_json.loads(path.read_bytes())
        document["lines"] = line()
        path.write_bytes(strict_json.pretty_file_bytes(document))
        for argv in (["providers", "apply"], ["providers", "show", KEYED_ID], ["providers", "show", KEYED_ID, "--resolved"],
                     ["providers", "list", "--json"], ["doctor"], ["doctor", "--json"], ["doctor", "-v"]):
            code, out, err = self.harness.op(argv)
            outputs[" ".join(argv)] = out + err
        config = proxy.config_dir(self.runtime.home) / "config.yaml"
        rendered = config.read_text()
        self.assertIn(LEAK_KEY, rendered)
        self.assertIn("private-network:", rendered)
        # A pending served change: the plan and its doctor fact.
        document["lines"] = {**line(), **line("custom-box-two", "box-chat-2")}
        path.write_bytes(strict_json.pretty_file_bytes(document))
        plan = doctor_mod.pending_served_plan(self.runtime)
        self.assertIsNotNone(plan)
        outputs["served plan"] = plan.text() + json.dumps(plan.as_document()) + (plan.doctor_fact() or "")
        for argv in (["plan"], ["plan", "--json"]):
            code, out, err = self.harness.op(argv)
            outputs[" ".join(argv)] = out + err
        for name, text in outputs.items():
            with self.subTest(output=name):
                self.assertNotIn(LEAK_KEY, text)
        holders = sorted(str(candidate) for base in (self.harness.root, source_dir)
                         for candidate in base.rglob("*")
                         if candidate.is_file() and not candidate.is_symlink()
                         and LEAK_KEY.encode() in candidate.read_bytes())
        self.assertEqual(holders, sorted({str(self.harness.secret_file), str(source), str(config)}))


class GatedLineKeyTests(NetworkCommandCase):
    """A declaration a closed gate refuses is never screened against stored
    values, so no output names its line keys — even admitted or bound ones,
    where the dependency checks still use them."""

    OTHER = "custom-box-other"

    def gated_state(self, code: str) -> str:
        """Declare (gate open), admit both lines and bind one in a profile and
        a named binding; then store a credential equal to one line key and
        close the gate ``code`` names. Returns the provider id."""

        import json

        from charmer import profile as profile_mod, state

        if code == "network-gated":
            pid, document = LAN_ID, lan_declaration()
        else:
            pid, document = KEYED_ID, keyed_declaration("https://box.example.test:8010/v1", network=None)
        document["lines"] = {**line(STORED_LINE_KEY, "box-chat-1"), **line(self.OTHER, "box-chat-2")}
        directory = operator.providers_dir(self.harness.env)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{pid}.json").write_bytes(raw(document))
        self.assertIn(STORED_LINE_KEY, self.runtime.lineup_catalog().lines)
        snapshot = self.runtime.operator_snapshot()
        schemas = operator.load_schemas(self.runtime.asset_root)
        ledger = operator.ledger_document(snapshot.ledger)
        for key in (STORED_LINE_KEY, self.OTHER):
            entry = snapshot.layer.lines[key]
            ledger["admissions"][key] = {
                "digest": entry.definition_digest, "at": "2026-11-11T00:00:00Z", "via": "admit",
                "diagnostic": {"wire": entry.core_entry["wire_model"],
                               "fields": snapshot.layer.metadata[key]["field_hashes"]}}
        # A capture of an earlier line of it (its alias carries the key).
        ledger["aliases"][f"{STORED_LINE_KEY}-old"] = {
            "provider": pid, "rd": snapshot.layer.providers[pid].route_digest, "wire": "box-old-1",
            "proxy_contract": None, "display": "Box old", "context_tokens": 32768,
            "source": f"operator:{STORED_LINE_KEY}", "since_catalog": 36, "overlay": None}
        operator.write_ledger(self.harness.env, ledger, schemas)
        self.runtime.settings_store.update(
            lambda doc: doc.__setitem__("admitted_lines", [STORED_LINE_KEY, self.OTHER]),
            catalog=self.runtime.lineup_catalog())
        lead = next(key for key, entry in self.runtime.lineup_catalog().lines.items()
                    if "lead" in entry.get("capabilities", ()) and entry["provider"] != pid)
        self.runtime.profiles.save({
            "version": 2, "name": "mine", "description": "", "lead": {"model": lead, "effort": "high"},
            "agents": {"cm-explorer": {"model": STORED_LINE_KEY, "effort": "high"}},
            "native_agents": {"explore": "native", "general_purpose": "on", "plan": "native"},
            "workflows": "native"})
        state.atomic_write(profile_mod.bindings_path(self.runtime.environ), json.dumps(
            {"version": 1, "bindings": {"fast": {"model": STORED_LINE_KEY, "effort": "high"}}}).encode())
        state.atomic_write(self.harness.secret_file, f"{SECRET}={STORED_LINE_KEY}\n".encode())
        audit = operator.PRIVATE_NETWORK_AUDIT if code == "network-gated" else catalog.KEYED_COMPAT_AUDIT
        self.runtime.catalog.docs["gateway"][catalog.AUDITS_KEY][audit] = False
        # The same gate on disk, for the paths that load the bundle themselves.
        gateway_path = self.runtime.asset_root / "catalog" / "gateway.json"
        gateway = json.loads(gateway_path.read_text())
        gateway[catalog.AUDITS_KEY][audit] = False
        gateway_path.write_bytes(strict_json.pretty_file_bytes(gateway))
        problems = self.runtime.operator_snapshot().layer.problems_by_file[operator.file_label(pid)]
        self.assertEqual([problem.code for problem in problems], [code])
        return pid

    def run_everything(self, pid: str, *argvs: list[str], store_free: bool = True) -> list[tuple[str, int, str]]:
        """Output paths that could name a gated line key."""

        import test_openai_compat_keyed as keyed

        spy = keyed.SpyStore({SECRET: STORED_LINE_KEY})
        results = []
        with keyed.spy_store(spy):
            for argv in argvs:
                code, out, err = self.harness.op(argv)
                results.append((" ".join(argv), code, out + err))
            # No stored-value scan and no read of the credential's name (doctor
            # still reads the shipped providers' keys for its render check).
            if store_free:
                self.assertEqual((spy.count("scan_values"), spy.touched(SECRET)), (0, 0), spy.calls)
        return results

    def assert_unnamed(self, results: list[tuple[str, int, str]]) -> None:
        for argv, _code, text in results:
            with self.subTest(argv=argv):
                self.assertNotIn(STORED_LINE_KEY, text)
                self.assertNotIn("dummy-key", text)

    def check_gate(self, code: str) -> None:
        from charmer import profile as profile_mod

        pid = self.gated_state(code)
        results = self.run_everything(
            pid, ["providers", "rm", pid, "--yes"], ["providers", "show", pid], ["providers", "show", pid, "--resolved"],
            ["providers", "validate"], ["providers", "list"], ["providers", "list", "--json"],
            ["models", "revoke", self.OTHER, "--yes"])
        self.assert_unnamed(results)
        # apply renders: it reads the stored keys of the providers it renders
        # (none of this one); it still names nothing of it.
        applied = self.run_everything(pid, ["providers", "apply"], store_free=False)
        self.assert_unnamed(applied)
        by = {argv: (status, text) for argv, status, text in results}
        status, text = by[f"providers rm {pid} --yes"]
        self.assertNotEqual(status, 0)
        file = operator.file_label(pid)
        # Still refused (the checks use the keys); fixed texts only.
        self.assertIn(f"revoke its 2 admitted line(s), not named while a closed gate refuses {file}: "
                      f"charmer models revoke KEY for each (the keys are in {file})", text)
        self.assertIn("charmer profile edit mine (it uses one of its lines)", text)
        self.assertIn("rebind named binding fast in the profile editor (it uses one of its lines)", text)
        self.assertTrue((operator.providers_dir(self.harness.env) / f"{pid}.json").exists())
        # Doctor, once the profile and the named binding are gone: a
        # profile's own text naming a model is not the declaration's (it
        # shows whatever the gate), so it is left out of this check.
        self.runtime.profiles.delete("mine")
        profile_mod.bindings_path(self.runtime.environ).unlink()
        doctor = self.run_everything(pid, ["doctor"], ["doctor", "--json"], ["doctor", "-v"])
        self.assert_unnamed(doctor)
        # The alias prune's preview (the planner; the fixture has no process table).
        from charmer import proxy

        environ = self.runtime.gateway_environ()
        planned = proxy._plan_alias_prune(self.runtime.home, self.runtime.session_store.root, None, environ=environ,
                                          env=environ, is_process_live=None, pinned=None)
        self.assertIn(f"kept: 1 captured alias(es) of {file} — not named while a closed gate refuses that "
                      "declaration", planned.lines)
        self.assertNotIn(STORED_LINE_KEY, "\n".join(planned.lines))
        self.assertIn(f"1 admitted line(s) of {file} are not named while a closed gate refuses that declaration",
                      doctor[2][2])
        self.assertIn(f"1 captured alias(es) of {file} are not named while a closed gate refuses that declaration",
                      doctor[2][2])

    def test_admitted_and_bound_lines_of_a_network_gated_declaration_are_never_named(self) -> None:
        self.check_gate("network-gated")

    def test_admitted_and_bound_lines_of_a_kind_gated_declaration_are_never_named(self) -> None:
        self.check_gate("kind-gated")

    def test_the_gated_blocker_texts_name_no_key(self) -> None:
        from charmer.setup import texts as setup_texts

        for kind in ("gated-admitted", "gated-bound-profile", "gated-bound-binding"):
            for text in setup_texts.BLOCKER[kind]:
                with self.subTest(kind=kind, text=text):
                    self.assertNotIn("{key}", text)


def _empty_ledger() -> dict:
    from _layout import FIXTURES_ROOT
    import json

    return json.loads((FIXTURES_ROOT / "operator" / "ledger-empty.json").read_text())


if __name__ == "__main__":
    unittest.main()
