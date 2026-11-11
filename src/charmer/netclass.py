"""Address classes for providers declared on a private network.

Pure: no DNS, no sockets, no files. One ordered table decides the class of an
IP address; the first matching rule wins and its text is what a refusal
shows, so an address that is refused can always be traced to its rule.

The declarable networks are ``loopback``, ``lan`` and ``tailnet``. Two more
classes are refused for every network: ``never`` (addresses that are not a
provider's under any declaration) and ``public`` (everything else, including
NAT64, 6to4, Teredo, benchmarking and documentation ranges: the standard
library's ``is_private`` is not used, it calls several of those private).

Before classification a zone (``%eth0``) is dropped and an IPv4-mapped IPv6
address (``::ffff:0:0/96``) is read as its IPv4 address, as a connection's
remote address is read when it is checked. Endpoint validation refuses both
spellings before it gets here. An IPv4-compatible address (the deprecated
``::/96`` form, other than ``::1``) is never unmapped: it is ``never``.

The gateway's dial guard classifies with the same rules: both
implementations are tested against ``tests/fixtures/netclass-corpus.json``.

**The overlay's reserved addresses** are not ``tailnet`` addresses. They come
from the overlay vendors' published pages, read on 2026-11-11:

* https://tailscale.com/docs/reference/reserved-ip-addresses (marked "Last
  validated Jan 12, 2026"): ``100.100.0.0/24``, ``100.100.100.0/24`` and
  ``100.115.92.0/23`` are for Tailscale's internal use and never assigned to
  a device; ``100.100.100.100`` (in ``100.100.100.0/24``) and
  ``fd7a:115c:a1e0::53`` are the device-local resolver and management
  service; ``100.101.102.103`` is set aside for the ``tshello`` service.
* https://tailscale.com/kb/1015/100.x-addresses (Jan 12, 2026): devices get
  addresses from ``100.64.0.0/10``.
* https://tailscale.com/kb/1201/4via6-subnets (Oct 5, 2026): ``fd7a:115c:a1e0:b1a::/64``
  carries 4via6 traffic, which a subnet router delivers to an IPv4 LAN host.
* Headscale v0.29.5 (``config-example.yaml``: default prefixes
  ``100.64.0.0/10`` and ``fd7a:115c:a1e0::/48``; ``hscontrol/db/ip.go``
  ``isTailscaleReservedIP``): its allocator skips only ``100.115.92.0/23``,
  ``100.100.100.100`` and ``fd7a:115c:a1e0::53``. A Headscale node can
  therefore hold an address in another excluded range; such an address is
  refused here, and the refusal names the rule that matched.

**Cloud metadata services on unique local IPv6 addresses** are inside
``fc00::/7`` and would otherwise classify ``lan``; they are ``never``. Read
on 2026-11-12:

* https://docs.cloud.google.com/compute/docs/metadata/querying-metadata:
  Google Cloud's metadata server is ``fd20:ce::254`` on IPv6-only instances.
* cloud-init ``cloudinit/sources/DataSourceOracle.py`` (main branch),
  ``IPV6_METADATA_ROOT``: Oracle Cloud's instance metadata at
  ``fd00:c1::a9fe:a9fe``.
* https://techdocs.akamai.com/cloud-computing/docs/access-the-metadata-service-api
  (updated 2026-03-01): Linode's Metadata service on ``169.254.169.254`` and
  ``fd00:a9fe:a9fe::1``.
* cloud-init ``cloudinit/sources/DataSourceScaleway.py`` (main branch),
  ``DS_BASE_URLS``: Scaleway's metadata at ``169.254.42.42`` and
  ``fd00:42::42``.
* https://docs.aws.amazon.com/eks/latest/userguide/pod-id-agent-setup.html:
  the EKS Pod Identity Agent listens on ``169.254.170.23`` and
  ``fd00:ec2::23``.

Their IPv4 addresses are link-local, already ``never``.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any, Iterable, Union

LOOPBACK = "loopback"
LAN = "lan"
TAILNET = "tailnet"
NEVER = "never"
PUBLIC = "public"
# The classes a provider may declare (``network``), and every class.
NETWORKS = (LOOPBACK, LAN, TAILNET)
CLASSES = (*NETWORKS, NEVER, PUBLIC)

# Refusal reasons; the gateway's refusal log uses the same words.
OUTSIDE_CLASS = "outside-class"
NEVER_REASON = "never"
GATEWAY_PORT = "gateway-port"
LOCAL_NOT_TAILNET = "local-not-tailnet"

Address = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]
Network = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]

_MAPPED = ipaddress.ip_network("::ffff:0:0/96")


@dataclass(frozen=True)
class Rule:
    """One row of the table: a prefix, its class and why."""

    prefix: Network
    cls: str
    why: str

    def text(self) -> str:
        where = self.prefix.with_prefixlen if self.prefix.num_addresses > 1 else str(self.prefix.network_address)
        return f"{where}: {self.why}"


def _rule(prefix: str, cls: str, why: str) -> Rule:
    return Rule(ipaddress.ip_network(prefix), cls, why)


# Cloud instance metadata services (never a provider, whatever the network).
METADATA = (
    _rule("169.254.169.254/32", NEVER, "a cloud metadata address"),
    _rule("100.100.100.200/32", NEVER, "a cloud metadata address"),
    _rule("fd00:ec2::254/128", NEVER, "a cloud metadata address"),
)
# Cloud metadata services on unique local IPv6 addresses, inside fc00::/7
# (see the module text). The endpoint policy's literal list is ``METADATA``.
METADATA_ULA = (
    _rule("fd20:ce::254/128", NEVER, "a cloud metadata address"),
    _rule("fd00:c1::a9fe:a9fe/128", NEVER, "a cloud metadata address"),
    _rule("fd00:a9fe:a9fe::1/128", NEVER, "a cloud metadata address"),
    _rule("fd00:42::42/128", NEVER, "a cloud metadata address"),
    _rule("fd00:ec2::23/128", NEVER, "a cloud metadata address"),
)
# The overlay's own service and reserved addresses (see the module text).
OVERLAY_RESERVED = (
    _rule("100.100.0.0/24", NEVER, "reserved by the overlay for its own services"),
    _rule("100.100.100.0/24", NEVER,
          "reserved by the overlay for its own services; 100.100.100.100 is its resolver"),
    _rule("100.115.92.0/23", NEVER, "reserved by the overlay for its own services"),
    _rule("100.101.102.103/32", NEVER, "set aside by the overlay for its example service"),
    _rule("fd7a:115c:a1e0::53/128", NEVER, "the overlay's resolver"),
    _rule("fd7a:115c:a1e0:b1a::/64", NEVER,
          "the overlay's 4via6 range: a subnet router delivers it to a LAN host"),
)
# The first matching rule wins: specific exclusions come before the ranges
# that contain them (metadata before link-local and fc00::/7, the overlay's
# reserved addresses before the tailnet ranges, the tailnet ULA prefix before
# fc00::/7, ::1 before the IPv4-compatible ::/96).
RULES: tuple[Rule, ...] = (
    *METADATA,
    *METADATA_ULA,
    _rule("127.0.0.0/8", LOOPBACK, "loopback"),
    _rule("::1/128", LOOPBACK, "loopback"),
    _rule("0.0.0.0/8", NEVER, "this-network and unspecified addresses"),
    _rule("169.254.0.0/16", NEVER, "link-local"),
    _rule("224.0.0.0/4", NEVER, "multicast"),
    _rule("255.255.255.255/32", NEVER, "broadcast"),
    _rule("::/128", NEVER, "the unspecified address"),
    _rule("::/96", NEVER, "an IPv4-compatible IPv6 address (deprecated form)"),
    _rule("fe80::/10", NEVER, "link-local"),
    _rule("ff00::/8", NEVER, "multicast"),
    *OVERLAY_RESERVED,
    _rule("100.64.0.0/10", TAILNET, "the tailnet range (shared address space)"),
    _rule("fd7a:115c:a1e0::/48", TAILNET, "the tailnet range (unique local prefix)"),
    _rule("10.0.0.0/8", LAN, "a private LAN range"),
    _rule("172.16.0.0/12", LAN, "a private LAN range"),
    _rule("192.168.0.0/16", LAN, "a private LAN range"),
    _rule("fc00::/7", LAN, "a private LAN range (unique local)"),
)
PUBLIC_RULE_TEXT = "not in a private-network range"

# What each declarable network covers, for texts (its exclusions are rules).
RANGES: dict[str, tuple[str, ...]] = {
    LOOPBACK: ("127.0.0.0/8", "::1"),
    LAN: ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"),
    TAILNET: ("100.64.0.0/10", "fd7a:115c:a1e0::/48"),
}


@dataclass(frozen=True)
class Match:
    """The class of one address and the rule that gave it."""

    address: Address  # zone dropped, IPv4-mapped read as IPv4
    cls: str
    rule: str


@dataclass(frozen=True)
class Verdict:
    """Whether one address may be used for a declared network."""

    address: Address
    network: str
    found: str
    rule: str
    reason: str | None  # None, or one of the refusal reasons above

    @property
    def allowed(self) -> bool:
        return self.reason is None


def address_of(value: Any) -> Address:
    """The address ``value`` names, as it is classified.

    ``value`` is an address object, its text, or a socket address tuple
    (``(host, port, ...)``). Raises :class:`ValueError` for anything that
    is not an IP address.
    """

    if isinstance(value, tuple):
        if not value:
            raise ValueError("not an IP address")
        value = value[0]
    if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        address: Address = value
    elif isinstance(value, str):
        text = value.strip()
        if text.startswith("[") and text.endswith("]"):
            text = text[1:-1]
        if ":" in text and "%" in text:
            text = text.split("%", 1)[0]
        try:
            address = ipaddress.ip_address(text)
        except ValueError:
            raise ValueError("not an IP address") from None
    else:
        raise ValueError("not an IP address")
    if isinstance(address, ipaddress.IPv6Address):
        if address.scope_id is not None:
            address = ipaddress.IPv6Address(address.packed)
        if address in _MAPPED:
            return ipaddress.IPv4Address(address.packed[12:])
    return address


def match(value: Any) -> Match:
    """The class of ``value`` and the rule that matched (first wins)."""

    address = address_of(value)
    for rule in RULES:
        if rule.prefix.version == address.version and address in rule.prefix:
            return Match(address, rule.cls, rule.text())
    return Match(address, PUBLIC, PUBLIC_RULE_TEXT)


def classify(value: Any) -> str:
    """``loopback``, ``lan``, ``tailnet``, ``never`` or ``public``."""

    return match(value).cls


def allowed(value: Any, network: str, gateway_ports: Iterable[int] = (), *, port: int | None = None) -> Verdict:
    """Whether ``value`` may be used for a provider declared on ``network``.

    The port is the socket address's own when ``value`` is a socket address
    tuple, else ``port``. A loopback address on a gateway port is refused
    whatever the network.
    """

    if network not in NETWORKS:
        raise ValueError(f"unknown network {network!r}")
    if port is None and isinstance(value, tuple) and len(value) > 1 and isinstance(value[1], int):
        port = value[1]
    found = match(value)
    if found.cls == NEVER:
        reason: str | None = NEVER_REASON
    elif found.cls == LOOPBACK and port is not None and port in set(gateway_ports):
        reason = GATEWAY_PORT
    elif found.cls != network:
        reason = OUTSIDE_CLASS
    else:
        reason = None
    return Verdict(found.address, network, found.cls, found.rule, reason)


def local_allowed(value: Any, network: str) -> Verdict:
    """The local-address rule: a ``tailnet`` connection's own address must be
    a tailnet address too (otherwise its packets do not leave through the
    overlay). Other networks have no local-address rule."""

    if network not in NETWORKS:
        raise ValueError(f"unknown network {network!r}")
    found = match(value)
    reason = LOCAL_NOT_TAILNET if network == TAILNET and found.cls != TAILNET else None
    return Verdict(found.address, network, found.cls, found.rule, reason)


def answers(addresses: Iterable[Any]) -> tuple[tuple[str, str], ...]:
    """``(address, class)`` per distinct address in order; anything that is
    not an IP address is left out."""

    found: list[tuple[str, str]] = []
    for value in addresses:
        try:
            item = match(value)
        except ValueError:
            continue
        pair = (str(item.address), item.cls)
        if pair not in found:
            found.append(pair)
    return tuple(found)


def in_network(value: Any, network: str, gateway_ports: Iterable[int] = ()) -> bool:
    """:func:`allowed`, False for anything that is not an IP address."""

    try:
        return allowed(value, network, gateway_ports).allowed
    except ValueError:
        return False


def describe(network: str) -> str:
    """``tailnet (100.64.0.0/10, fd7a:115c:a1e0::/48)``."""

    return f"{network} ({', '.join(RANGES[network])})"


def refusal(verdict: Verdict) -> str:
    """The text of a refused verdict (value-free: an address and a rule)."""

    if verdict.reason == NEVER_REASON:
        return f"{verdict.address} is not a provider address ({verdict.rule})"
    if verdict.reason == GATEWAY_PORT:
        return "the charmer gateway port is not a provider"
    if verdict.reason == LOCAL_NOT_TAILNET:
        return f"the connection's local address {verdict.address} is not on the tailnet"
    if verdict.reason == OUTSIDE_CLASS:
        if verdict.found == PUBLIC:
            return f"{verdict.address} is not on a private network"
        return f"{verdict.address} is on the {verdict.found} network, not {verdict.network}"
    raise ValueError("the verdict allows the address")
