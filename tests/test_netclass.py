"""Address classes of private-network providers (pure; no DNS, no sockets).

The corpus ``tests/fixtures/netclass-corpus.json`` is shared with the gateway's
dial-guard tests: the expected classes there are written by hand, never
derived from the module, and a disagreement is a defect in one of the two
implementations, never a corpus edit.
"""

from __future__ import annotations

import ast
import ipaddress
import json
import unittest

from _layout import FIXTURES_ROOT, SRC_DIR
from charmer import netclass

CORPUS = FIXTURES_ROOT / "netclass-corpus.json"


def corpus() -> dict:
    return json.loads(CORPUS.read_text(encoding="utf-8"))


class CorpusTests(unittest.TestCase):
    def test_corpus_format(self) -> None:
        document = corpus()
        self.assertEqual(set(document), {"version", "cases"})
        self.assertEqual(document["version"], 1)
        cases = document["cases"]
        self.assertGreaterEqual(len(cases), 40)
        for case in cases:
            with self.subTest(case=case):
                self.assertEqual(set(case), {"address", "class"})
                self.assertIsInstance(case["address"], str)
                self.assertIn(case["class"], netclass.CLASSES)
        addresses = [case["address"] for case in cases]
        self.assertEqual(len(addresses), len(set(addresses)))
        self.assertEqual({case["class"] for case in cases}, set(netclass.CLASSES))

    def test_every_case_classifies_as_written(self) -> None:
        for case in corpus()["cases"]:
            with self.subTest(address=case["address"]):
                self.assertEqual(netclass.classify(case["address"]), case["class"])

    def test_every_rule_has_its_boundaries_and_neighbours(self) -> None:
        covered = {netclass.address_of(case["address"]) for case in corpus()["cases"]}
        for rule in netclass.RULES:
            first, last = rule.prefix.network_address, rule.prefix.broadcast_address
            wanted = [first, last]
            width = 32 if rule.prefix.version == 4 else 128
            if int(first) > 0:
                wanted.append(first - 1)
            if int(last) < 2 ** width - 1:
                wanted.append(last + 1)
            for address in wanted:
                with self.subTest(rule=rule.text(), address=str(address)):
                    self.assertIn(address, covered)

    def test_every_rule_decides_some_case(self) -> None:
        decided = {netclass.match(case["address"]).rule for case in corpus()["cases"]}
        for rule in netclass.RULES:
            with self.subTest(rule=rule.text()):
                self.assertIn(rule.text(), decided)
        self.assertIn(netclass.PUBLIC_RULE_TEXT, decided)

    def test_corpus_disagrees_with_is_private_where_it_must(self) -> None:
        # The standard library's private flag is not a security class.
        for address in ("198.18.0.1", "2001:db8::1", "192.0.2.1"):
            with self.subTest(address=address):
                self.assertTrue(ipaddress.ip_address(address).is_private)
                self.assertEqual(netclass.classify(address), netclass.PUBLIC)
        self.assertFalse(ipaddress.ip_address("100.88.12.34").is_private)
        self.assertEqual(netclass.classify("100.88.12.34"), netclass.TAILNET)


class TableTests(unittest.TestCase):
    def test_overlay_reserved_addresses_are_pinned(self) -> None:
        self.assertEqual([rule.prefix.with_prefixlen for rule in netclass.OVERLAY_RESERVED], [
            "100.100.0.0/24", "100.100.100.0/24", "100.115.92.0/23", "100.101.102.103/32",
            "fd7a:115c:a1e0::53/128", "fd7a:115c:a1e0:b1a::/64",
        ])
        tailnet = [ipaddress.ip_network(prefix) for prefix in netclass.RANGES[netclass.TAILNET]]
        for rule in netclass.OVERLAY_RESERVED:
            with self.subTest(rule=rule.text()):
                self.assertEqual(rule.cls, netclass.NEVER)
                self.assertTrue(any(rule.prefix.version == net.version and rule.prefix.subnet_of(net)
                                    for net in tailnet))

    def test_cloud_metadata_on_unique_local_addresses_is_never(self) -> None:
        self.assertEqual([rule.prefix.with_prefixlen for rule in netclass.METADATA_ULA], [
            "fd20:ce::254/128", "fd00:c1::a9fe:a9fe/128", "fd00:a9fe:a9fe::1/128", "fd00:42::42/128",
            "fd00:ec2::23/128",
        ])
        unique_local = ipaddress.ip_network("fc00::/7")
        for rule in netclass.METADATA_ULA:
            with self.subTest(rule=rule.text()):
                self.assertTrue(rule.prefix.subnet_of(unique_local))
                address = rule.prefix.network_address
                self.assertEqual(netclass.classify(address), netclass.NEVER)
                self.assertEqual(netclass.allowed(address, "lan", port=80).reason, netclass.NEVER_REASON)

    def test_metadata_addresses_match_the_endpoint_policy(self) -> None:
        from charmer import operator

        self.assertEqual({rule.prefix.network_address for rule in netclass.METADATA},
                         set(operator.METADATA_ADDRESSES))
        for address in operator.METADATA_ADDRESSES:
            self.assertEqual(netclass.classify(address), netclass.NEVER)

    def test_ranges_and_describe(self) -> None:
        self.assertEqual(netclass.NETWORKS, ("loopback", "lan", "tailnet"))
        self.assertEqual(netclass.describe("tailnet"), "tailnet (100.64.0.0/10, fd7a:115c:a1e0::/48)")
        self.assertEqual(netclass.describe("lan"), "lan (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, fc00::/7)")
        self.assertEqual(netclass.describe("loopback"), "loopback (127.0.0.0/8, ::1)")
        for network, prefixes in netclass.RANGES.items():
            for prefix in prefixes:
                with self.subTest(network=network, prefix=prefix):
                    self.assertEqual(netclass.classify(ipaddress.ip_network(prefix).network_address), network)

    def test_module_is_pure(self) -> None:
        tree = ast.parse((SRC_DIR / "charmer" / "netclass.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0] if node.level == 0 else "." + (node.module or ""))
        self.assertEqual(imported, {"__future__", "ipaddress", "dataclasses", "typing"})


class AddressTests(unittest.TestCase):
    def test_spellings_are_read_as_classified(self) -> None:
        v4 = ipaddress.IPv4Address
        for value, expected in (
            ("::ffff:100.88.12.34", v4("100.88.12.34")),
            ("::ffff:7f00:1", v4("127.0.0.1")),
            ("[fd7a:115c:a1e0::53]", ipaddress.IPv6Address("fd7a:115c:a1e0::53")),
            ("fe80::1%eth0", ipaddress.IPv6Address("fe80::1")),
            (ipaddress.IPv6Address("fe80::1%eth0"), ipaddress.IPv6Address("fe80::1")),
            (("192.168.1.10", 8010), v4("192.168.1.10")),
            (("fd12:3456:789a::1%eth0", 8010, 0, 2), ipaddress.IPv6Address("fd12:3456:789a::1")),
            ("::100.64.0.10", ipaddress.IPv6Address("::100.64.0.10")),
        ):
            with self.subTest(value=value):
                found = netclass.address_of(value)
                self.assertEqual(found, expected)
                if isinstance(found, ipaddress.IPv6Address):
                    self.assertIsNone(found.scope_id)

    def test_names_and_ambiguous_spellings_are_not_addresses(self) -> None:
        for value in ("localhost", "host.example", "127.1", "010.0.0.1", "0x7f.0.0.1", "2130706433",
                      "", " ", "1.2.3.4%eth0", "1.2.3.4:80", (), None, 2130706433, b"127.0.0.1"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "^not an IP address$"):
                netclass.match(value)


class AllowedTests(unittest.TestCase):
    PORTS = frozenset({8316, 8317, 18317})

    def test_each_network_admits_only_its_class(self) -> None:
        samples = {"loopback": "127.0.0.1", "lan": "192.168.1.10", "tailnet": "100.88.12.34",
                   "never": "100.100.100.100", "public": "203.0.113.9"}
        for network in netclass.NETWORKS:
            for found, address in samples.items():
                with self.subTest(network=network, address=address):
                    verdict = netclass.allowed(address, network, self.PORTS, port=8010)
                    self.assertEqual(verdict.found, found)
                    self.assertEqual(verdict.allowed, found == network)
                    expected = (None if found == network else "never" if found == "never" else "outside-class")
                    self.assertEqual(verdict.reason, expected)

    def test_gateway_ports_are_refused_on_loopback_for_every_network(self) -> None:
        for network in netclass.NETWORKS:
            for port in sorted(self.PORTS):
                with self.subTest(network=network, port=port):
                    self.assertEqual(netclass.allowed("127.0.0.1", network, self.PORTS, port=port).reason,
                                     "gateway-port")
                    self.assertEqual(netclass.allowed(("::1", port, 0, 0), network, self.PORTS).reason,
                                     "gateway-port")
        self.assertTrue(netclass.allowed(("127.0.0.1", 8010), "loopback", self.PORTS).allowed)
        self.assertTrue(netclass.allowed("100.88.12.34", "tailnet", self.PORTS, port=8317).allowed)
        # A never-class address is refused as such, whatever its port.
        self.assertEqual(netclass.allowed("169.254.169.254", "lan", self.PORTS, port=8317).reason, "never")

    def test_mapped_and_zoned_answers_are_judged_as_their_address(self) -> None:
        self.assertTrue(netclass.allowed("::ffff:100.88.12.34", "tailnet").allowed)
        self.assertEqual(netclass.allowed("::ffff:127.0.0.1", "tailnet").reason, "outside-class")
        self.assertEqual(netclass.allowed("::ffff:127.0.0.1", "loopback", self.PORTS, port=8317).reason,
                         "gateway-port")
        self.assertTrue(netclass.allowed("fd12:3456:789a::1%eth0", "lan").allowed)
        self.assertEqual(netclass.allowed("::100.88.12.34", "tailnet").reason, "never")

    def test_local_address_rule(self) -> None:
        self.assertTrue(netclass.local_allowed("100.88.12.34", "tailnet").allowed)
        self.assertTrue(netclass.local_allowed(("fd7a:115c:a1e0:5ee:2c1:9a00:7b:10", 50000, 0, 0), "tailnet").allowed)
        for local in ("192.168.1.10", "203.0.113.9", "127.0.0.1", "100.100.0.7"):
            with self.subTest(local=local):
                verdict = netclass.local_allowed(local, "tailnet")
                self.assertEqual(verdict.reason, "local-not-tailnet")
                self.assertEqual(netclass.refusal(verdict),
                                 f"the connection's local address {local} is not on the tailnet")
        for network in ("loopback", "lan"):
            self.assertTrue(netclass.local_allowed("203.0.113.9", network).allowed)

    def test_unknown_network_is_refused(self) -> None:
        for network in ("never", "public", "Tailnet", "", None):
            with self.subTest(network=network), self.assertRaises(ValueError):
                netclass.allowed("100.88.12.34", network)
            with self.subTest(network=network), self.assertRaises(ValueError):
                netclass.local_allowed("100.88.12.34", network)

    def test_refusal_texts_name_the_rule(self) -> None:
        cases = (
            (("203.0.113.9", "tailnet"), "203.0.113.9 is not on a private network"),
            (("192.168.1.10", "tailnet"), "192.168.1.10 is on the lan network, not tailnet"),
            (("100.88.12.34", "lan"), "100.88.12.34 is on the tailnet network, not lan"),
            (("::ffff:127.0.0.1", "lan"), "127.0.0.1 is on the loopback network, not lan"),
            (("100.100.100.7", "tailnet"), "100.100.100.7 is not a provider address (100.100.100.0/24: "
                                           "reserved by the overlay for its own services; 100.100.100.100 "
                                           "is its resolver)"),
            (("100.101.102.103", "tailnet"), "100.101.102.103 is not a provider address (100.101.102.103: "
                                             "set aside by the overlay for its example service)"),
            (("fd7a:115c:a1e0:b1a::7", "tailnet"), "fd7a:115c:a1e0:b1a::7 is not a provider address "
                                                   "(fd7a:115c:a1e0:b1a::/64: the overlay's 4via6 range: a subnet "
                                                   "router delivers it to a LAN host)"),
            (("fd00:ec2::254", "lan"), "fd00:ec2::254 is not a provider address (fd00:ec2::254: a cloud "
                                       "metadata address)"),
            (("::10.1.2.3", "lan"), "::a01:203 is not a provider address (::/96: an IPv4-compatible IPv6 "
                                    "address (deprecated form))"),
        )
        for (address, network), text in cases:
            with self.subTest(address=address):
                self.assertEqual(netclass.refusal(netclass.allowed(address, network)), text)
        self.assertEqual(netclass.refusal(netclass.allowed("127.0.0.1", "loopback", {8317}, port=8317)),
                         "the charmer gateway port is not a provider")
        with self.assertRaises(ValueError):
            netclass.refusal(netclass.allowed("100.88.12.34", "tailnet"))

    def test_answers_classify_distinct_addresses_and_skip_names(self) -> None:
        self.assertEqual(
            netclass.answers(("100.88.12.34", "box.example.invalid", "::ffff:192.168.1.10", "100.88.12.34",
                              "fe80::1%eth0", "203.0.113.9")),
            (("100.88.12.34", "tailnet"), ("192.168.1.10", "lan"), ("fe80::1", "never"), ("203.0.113.9", "public")),
        )
        self.assertEqual(netclass.answers(()), ())

    def test_in_network_is_allowed_and_false_otherwise(self) -> None:
        self.assertTrue(netclass.in_network("100.88.12.34", "tailnet"))
        self.assertTrue(netclass.in_network("::ffff:192.168.1.10", "lan"))
        self.assertFalse(netclass.in_network("192.168.1.10", "tailnet"))
        self.assertFalse(netclass.in_network("100.100.100.100", "tailnet"))
        self.assertFalse(netclass.in_network("box.example.invalid", "lan"))
        # An unknown network fails closed.
        self.assertFalse(netclass.in_network("100.88.12.34", "public"))


if __name__ == "__main__":
    unittest.main()
