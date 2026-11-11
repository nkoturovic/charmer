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

        for name, document, keys in ((KEYED_ID, keyed_declaration(), [KEY]),
                                     (LAN_ID, lan_declaration(), [LAN_KEY])):
            with self.subTest(provider=name):
                files = {name: raw(document)}
                self.assertFalse(operator.store_scan_needed(docs, files))
                layer = operator.validate_layer(docs, files, schemas=operator.load_schemas(FIXTURE_ROOT),
                                                secret_values=operator._scan_values(store, docs, files))
                self.assertEqual(layer.providers, {})
                self.assertEqual([(p.code, p.json_path) for p in layer.problems], [("network-gated", "$.provider.network")])
                self.assertEqual(texts(layer), [
                    f"providers.d/{name}.json: provider.network: private-network routes are unavailable in this "
                    f"build — before going back to a release without them: charmer models rm {keys[0]}; "
                    f"charmer providers rm {name} (add it again after returning)"])
                self.assertEqual(operator.keyed_preflight(docs, name, files[name]).code, "network-gated")

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

    def state_bytes(self) -> dict:
        return self.harness.state_bytes()


# Keyed plain HTTP is rendered once the renderer's keyed check takes the
# network; the end-to-end command rows below use HTTPS on a private network,
# and the plain-HTTP approval text is checked on the resolved provider.
ADD_KEYED = ["providers", "add", KEYED_ID, "--kind", "openai-compatible", "--base-url",
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


def _empty_ledger() -> dict:
    from _layout import FIXTURES_ROOT
    import json

    return json.loads((FIXTURES_ROOT / "operator" / "ledger-empty.json").read_text())


if __name__ == "__main__":
    unittest.main()
