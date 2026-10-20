"""Qualification-only candidate network profile for an owned disposable macOS VM.

Run with system/test Python: --out NEW_ABSOLUTE_DIRECTORY --qualify-only.
This never runs a product, bundle, gateway, client or caller-supplied command.
The stock release workflow is unchanged. Neither import nor --help has effects.

SBPL grammar and enforcement are UNVERIFIED until the Darwin controls execute.
Even CONTROLS_PASSED means only the recorded paired controls and policy shape,
not universal Internet denial, a product journey, macOS 13 or runtime acceptance.
This is not filesystem isolation. Mach lookups are denied as a candidate barrier
against system DNS/proxy brokers; an ordinary resolver error cannot prove that
barrier. Cached resolution cannot prove DNS egress either, so it is incomplete.
No profile relaxation, unconfined retry, privileged mutation or candidate runner
is provided. Unavailable IPv6, DNS or other prerequisites remain missing coverage.
Allowed and excluded owned wildcard twins both need nonce-authenticated baselines;
public controls cannot substitute for their IPv4/IPv6 denial observations.
Helper-file, control-script and profile byte hashes are separate, checked with
reopened directory/ancestor identities before launches and final classification.
These are point-in-time checks, not atomic exec guarantees or Git/workflow identities.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import ipaddress
import json
import os
import platform
import re
import secrets
import socket
import stat
import subprocess
import sys
from pathlib import Path

SANDBOX = "/usr/bin/sandbox-exec"
IFCONFIG = "/sbin/ifconfig"
HOST = "example.com"
DNS_CONTROLS = ("1.1.1.1", "2606:4700:4700::1111")  # Non-provider DNS, no HTTP/auth.
RESERVED_PORTS = {8316, 8317}
LOCAL_IDS = ("loopback4", "loopback6", "wildcard4", "wildcard6")
DENIED_IDS = ("denied4", "denied6")
FIXTURE_IDS = LOCAL_IDS + DENIED_IDS
REMOTE_IDS = ("tcp4", "tcp6", "dns4", "dns6", "resolver")
CONTROL_IDS = FIXTURE_IDS + REMOTE_IDS
GUARD_MARKERS = ("CLAUDECODE", "CHARMER_MANAGED_ID", "CHARMER_SESSION_ID", "CHARMER_LAUNCH_EPOCH",
                 "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CM_ISOLATION_GUARD_LOG")
ROOTS = {"HOME": "home", "XDG_CONFIG_HOME": "config", "XDG_DATA_HOME": "data",
         "XDG_STATE_HOME": "state", "XDG_CACHE_HOME": "cache", "XDG_RUNTIME_DIR": "run", "TMPDIR": "tmp"}
OUTCOMES = {"ok", "denied", "timeout", "unreachable", "dns_error", "missing", "identity_error", "error"}

# Only this literal is written/executed, in the newly owned evidence directory.
# All observations are closed records: no exception strings, stream logs or env.
CONTROL_SOURCE = r'''import errno, ipaddress, json, os, socket, struct, subprocess, sys
P = json.loads(sys.argv[2])
ROLE = sys.argv[1]
IDS = ("loopback4", "loopback6", "wildcard4", "wildcard6", "denied4", "denied6", "tcp4", "tcp6", "dns4", "dns6", "resolver")
identity = (os.path.realpath(sys.executable) == P["python"] and sys.flags.isolated == 1
            and sys.flags.no_site == 1 and sys.dont_write_bytecode
            and os.path.abspath(__file__) == P["out"] + "/controls.py")
result = dict(role=ROLE, nonce=P["nonce"], exec_identity=bool(identity), cleanup=True,
              rows=[dict(id=n, outcome="missing", errno=None, port=None) for n in IDS])
def addresses():
    return sorted({str(ipaddress.ip_address(a[4][0])) for a in
                   socket.getaddrinfo("example.com", 443, socket.AF_UNSPEC, socket.SOCK_STREAM)
                   if ipaddress.ip_address(a[4][0]).is_global})[:16]
def exact(s, count):
    data = b""
    while len(data) < count:
        part = s.recv(count - len(data))
        if not part:
            break
        data += part
    return data

def local(name, row=None):
    address, wanted = P["addresses"][name], P["ports"][name]
    if address is None:
        return "missing", None
    family = socket.AF_INET6 if name.endswith("6") else socket.AF_INET
    # A wildcard bind is performed INSIDE the profile, not inherited from the host.
    bind = ("::" if family == socket.AF_INET6 else "0.0.0.0") if name.startswith(("wildcard", "denied")) else address
    used = {port for key, port in P["ports"].items() if key != name and port}
    for _ in range(8):  # Resample only forbidden/previously selected ephemeral ports, never a failed control.
        with socket.socket(family, socket.SOCK_STREAM) as listener:
            listener.settimeout(2)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            listener.bind((bind, wanted))
            port = listener.getsockname()[1]
            if port in (8316, 8317) or (wanted == 0 and port in used):
                continue
            listener.listen(1)
            with socket.socket(family, socket.SOCK_STREAM) as client:
                client.settimeout(2)
                try:
                    client.connect((address, port))
                except OSError as e:
                    if name.startswith("denied") and e.errno in (errno.EPERM, errno.EACCES):
                        if row is not None:
                            row["errno"] = e.errno
                        return "denied", port  # Only connect denial after our own bind/listen is a negative control.
                    raise
                challenge = (P["nonce"] + "/" + name).encode("ascii")
                client.sendall(challenge)
                peer, _ = listener.accept()
                with peer:
                    peer.settimeout(2)
                    if exact(peer, len(challenge)) != challenge:
                        return "identity_error", port
                    peer.sendall(challenge)
                    return ("ok" if exact(client, len(challenge)) == challenge else "identity_error"), port
    return "missing", None

def remote(name):
    if name == "resolver":
        families = {ipaddress.ip_address(a).version for a in addresses()}
        return "ok" if families == {4, 6} else "missing"
    family = socket.AF_INET6 if name.endswith("6") else socket.AF_INET
    address = P["addresses"]["tcp6" if name.endswith("6") else "tcp4"]
    if address is None:
        return "missing"
    dns = name.startswith("dns")
    with socket.socket(family, socket.SOCK_DGRAM if dns else socket.SOCK_STREAM) as s:
        s.settimeout(2)
        if not dns:
            s.connect((address, 443))  # TCP handshake only; never HTTP/TLS/model traffic.
            return "ok"
        target = "2606:4700:4700::1111" if name.endswith("6") else "1.1.1.1"
        label = P["nonce"] + ".example.com"
        question = b"".join(bytes([len(p)]) + p.encode("ascii") for p in label.split(".")) + b"\0\0\1\0\1"
        query = bytes.fromhex(P["nonce"][:4]) + struct.pack("!5H", 256, 1, 0, 0, 0) + question
        s.connect((target, 53))
        s.send(query)
        reply = s.recv(512)
        valid = (len(reply) >= 12 + len(question) and reply[:2] == query[:2]
                 and reply[2] & 128 and reply[4:6] == b"\0\1" and reply[12:12+len(question)] == question)
        return "ok" if valid else "identity_error"  # NXDOMAIN is still a real DNS response.

if identity and ROLE == "resolve":
    result.pop("rows")
    try:
        result["addresses"] = addresses()
    except OSError:
        result["addresses"] = []
elif identity and ROLE == "descendant":
    # No second sandbox invocation: this exec'd descendant must INHERIT the boundary.
    child = subprocess.Popen([P["python"], "-I", "-S", "-B", __file__, "exec", sys.argv[2]],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             close_fds=True, env=dict(os.environ), cwd=P["out"])
    try:
        output, _ = child.communicate(timeout=55)
        if child.returncode == 0 and len(output) <= 8192:
            observed = json.loads(output)
            if observed["role"] == "exec" and observed["nonce"] == P["nonce"]:
                result = observed
                result["role"] = ROLE
    except (subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        pass  # Missing child evidence is incomplete even if cleanup succeeds.
    finally:
        if child.poll() is None:
            child.kill()
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            result["cleanup"] = False
elif identity and ROLE in ("baseline", "confined", "exec"):
    for row in result["rows"]:
        try:
            if row["id"] in P["ports"]:
                row["outcome"], row["port"] = local(row["id"], row)
                if ROLE == "baseline" and row["outcome"] == "ok":
                    P["ports"][row["id"]] = row["port"]
            else:
                row["outcome"] = remote(row["id"])
        except OSError as e:
            row["errno"] = e.errno
            row["outcome"] = ("dns_error" if isinstance(e, socket.gaierror) else
                              "denied" if e.errno in (errno.EPERM, errno.EACCES) else
                              "timeout" if isinstance(e, TimeoutError) else "unreachable")
        except (ValueError, OverflowError):
            row["outcome"] = "error"
print(json.dumps(result, sort_keys=True))
'''


class Refusal(Exception):
    """A fixed category, never an arbitrary exception message."""


def absolute_path(value: str) -> Path:
    if not re.fullmatch(r"/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+", value) or any(
            part in (".", "..") for part in value.split("/")):
        raise Refusal("unsupported-path")
    return Path(value)


def numeric_address(value: str, family: int, *, outside: bool = False) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise Refusal("invalid-address") from None
    if str(address) != value or "%" in value or address.version != family or address.is_unspecified \
            or (isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None) \
            or address.is_multicast or address.is_link_local or (address.is_reserved and not address.is_loopback) \
            or (outside and not address.is_global):
        raise Refusal("invalid-address")
    return value


def prerequisite_reason(system: str, version: tuple, uid: int, environ: dict, capabilities: bool) -> str | None:
    if system != "Darwin":
        return "unsupported-platform"
    if version < (3, 11) or uid == 0 or not capabilities:
        return "unsupported-prerequisite"
    if any(name in environ for name in GUARD_MARKERS):
        return "session-or-harness-refused"
    return None


def interpreter_path(value: str) -> bool:
    return re.fullmatch(
        r"/(?:usr/bin/python3|Library/Developer/CommandLineTools/usr/bin/python3|"
        r"Library/Frameworks/Python\.framework/Versions/3\.\d+/(?:bin/python3(?:\.\d+)?|"
        r"Resources/Python\.app/Contents/MacOS/Python))", value) is not None


def trusted_executable(value: str, *, interpreter: bool = False) -> dict:
    path = Path(value).resolve(strict=True)
    allowed = interpreter_path(str(path)) if interpreter else value in (SANDBOX, IFCONFIG) and str(path) == value
    if not allowed:
        raise Refusal("untrusted-executable-path")
    for component in (*reversed(path.parents), path):
        info = component.lstat()
        kind = stat.S_ISREG(info.st_mode) if component == path else stat.S_ISDIR(info.st_mode)
        if not kind or info.st_uid != 0 or info.st_mode & (0o022 | stat.S_ISUID | stat.S_ISGID):
            raise Refusal("untrusted-executable-metadata")
    if not os.access(path, os.R_OK | os.X_OK):
        raise Refusal("inaccessible-executable")
    with path.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    return {"path": str(path), "sha256": digest, "size": info.st_size,
            "uid": info.st_uid, "mode": stat.S_IMODE(info.st_mode)}


def fresh_output(value: str) -> tuple[Path, int]:
    path = absolute_path(value)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open("/", flags)
    try:
        for part in path.parts[1:-1]:
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            info = os.fstat(fd)
            sticky = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
            if info.st_uid not in (0, os.geteuid()) or (info.st_mode & 0o022 and not sticky):
                raise Refusal("unsafe-output-parent")
        os.mkdir(path.name, 0o700, dir_fd=fd)  # EEXIST refuses even an empty directory/symlink.
        owned = os.open(path.name, flags, dir_fd=fd)
        info = os.fstat(owned)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            os.close(owned)
            raise Refusal("unsafe-output-directory")
        return path, owned
    finally:
        os.close(fd)


def new_file(fd: int, name: str, content: str) -> None:
    if name not in ("controls.py", "network.sb", "qualification.json"):
        raise Refusal("unsupported-evidence-name")
    handle = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.fsync(fd)


def file_sha256(fd: int, name: str) -> str:
    """Read one bound source/profile relative to its held directory, never a link/FIFO."""
    if name not in (Path(__file__).name, "controls.py", "network.sb"):
        raise Refusal("input-drift")
    handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(handle, "rb") as stream:
        if not stat.S_ISREG(os.fstat(handle).st_mode):
            raise Refusal("input-drift")
        return hashlib.file_digest(stream, "sha256").hexdigest()


def directory_chain(path: Path, held_fd: int) -> tuple:
    """Reopen from / without following links, including every ancestor's identity."""
    if not path.is_absolute() or ".." in path.parts:
        raise Refusal("input-drift")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open("/", flags)
    chain = []
    try:
        for part in (None, *path.parts[1:]):
            if part is not None:
                next_fd = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            info = os.fstat(fd)
            chain.append((info.st_dev, info.st_ino))
        held = os.fstat(held_fd)
        if not stat.S_ISDIR(held.st_mode) or chain[-1] != (held.st_dev, held.st_ino):
            raise Refusal("input-drift")
        return tuple(chain)
    finally:
        os.close(fd)


def verify_inputs(helper_fd: int, out_fd: int, helper_hash: str, profile_hash: str | None,
                  out: Path, chains: tuple) -> None:
    expected = [(helper_fd, Path(__file__).name, helper_hash),
                (out_fd, "controls.py", hashlib.sha256(CONTROL_SOURCE.encode()).hexdigest())]
    if profile_hash is not None:
        expected.append((out_fd, "network.sb", profile_hash))
    try:
        current = (directory_chain(Path(__file__).absolute().parent, helper_fd), directory_chain(out, out_fd))
        if current != chains:
            raise Refusal("input-drift")
        for fd, name, digest in expected:
            if file_sha256(fd, name) != digest:
                raise Refusal("input-drift")
    except OSError:
        raise Refusal("input-drift") from None


def child_environment(out: Path, environ: dict) -> dict[str, str]:
    absolute_path(str(out))
    return {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C", "LC_ALL": "C",
            **{key: str(out / leaf) for key, leaf in ROOTS.items()},
            **{key: environ[key] for key in GUARD_MARKERS if key in environ}}


def interface_addresses(text: str) -> dict[str, str | None]:
    found: dict[int, set[str]] = {4: set(), 6: set()}
    active = False
    for line in text.splitlines():
        if line and not line[0].isspace():
            flags = re.search(r"flags=\d+<([^>]+)>", line)
            active = bool(flags and "UP" in flags[1].split(",") and "LOOPBACK" not in flags[1].split(","))
        match = re.match(r"\s+inet(6)? ([^\s]+)", line) if active else None
        if match:
            value = match[2]
            address = ipaddress.ip_address(value.split("%", 1)[0])
            if address.is_link_local or address.is_loopback:
                continue  # Scoped link-local support is not invented; its coverage stays missing.
            family = 6 if match[1] else 4
            found[family].add(numeric_address(value, family))
    return {f"wildcard{family}": min(values) if values else None for family, values in found.items()}


def validate_plan(plan: dict) -> None:
    if set(plan) != {"nonce", "python", "out", "addresses", "ports"} or not re.fullmatch(
            r"[0-9a-f]{32}", plan["nonce"]):
        raise Refusal("invalid-control-plan")
    absolute_path(plan["python"])
    absolute_path(plan["out"])
    if not interpreter_path(plan["python"]):
        raise Refusal("untrusted-executable-path")
    if set(plan["addresses"]) != set(FIXTURE_IDS + ("tcp4", "tcp6")) or set(plan["ports"]) != set(FIXTURE_IDS):
        raise Refusal("invalid-control-plan")
    for name, value in plan["addresses"].items():
        if value is not None:
            numeric_address(value, int(name[-1]), outside=name.startswith("tcp"))
            if name.startswith(("wildcard", "denied")) and ipaddress.ip_address(value).is_loopback:
                raise Refusal("invalid-wildcard-address")
    if (plan["addresses"]["loopback4"], plan["addresses"]["loopback6"]) != ("127.0.0.1", "::1"):
        raise Refusal("invalid-loopback-address")
    for family in (4, 6):
        if plan["addresses"][f"denied{family}"] != plan["addresses"][f"wildcard{family}"]:
            raise Refusal("invalid-denied-twin-address")
    selected = set()
    for port in plan["ports"].values():
        if type(port) is not int or not 0 <= port <= 65535 or port in RESERVED_PORTS or (port and port in selected):
            raise Refusal("invalid-fixture-port")
        if port:
            selected.add(port)
    local = {plan["addresses"][name] for name in FIXTURE_IDS} - {None}
    remote = {plan["addresses"]["tcp4"], plan["addresses"]["tcp6"], *DNS_CONTROLS} - {None}
    if local & remote:
        raise Refusal("outside-control-allowlisted")


def profile_text(plan: dict) -> str:
    validate_plan(plan)
    rules = []
    for name in LOCAL_IDS:  # The owned denied twins are deliberately never outbound exceptions.
        address, port = plan["addresses"][name], plan["ports"][name]
        if address is not None and port:
            endpoint = f"[{address}]:{port}" if ":" in address else f"{address}:{port}"
            rules.append(f'  (remote tcp "{endpoint}")')
    if not rules:
        raise Refusal("no-local-control")
    # Candidate grammar: no names, caller SBPL, DNS wildcards or outbound network* exception.
    return ("(version 1)\n(allow default)\n(deny network*)\n(deny mach-lookup)\n"
            "(allow network-bind)\n(allow network-inbound)\n(allow network-outbound\n"
            + "\n".join(rules) + ")\n")


def probe_argv(python: str, out: Path, role: str, plan: dict) -> list[str]:
    validate_plan(plan)
    if role not in ("resolve", "baseline", "confined", "descendant") or python != plan["python"] \
            or str(out) != plan["out"]:
        raise Refusal("invalid-probe-argv")
    argv = [python, "-I", "-S", "-B", str(out / "controls.py"), role,
            json.dumps(plan, sort_keys=True, separators=(",", ":"))]
    return [SANDBOX, "-f", str(out / "network.sb"), *argv] if role in ("confined", "descendant") else argv


def reap_owned(child: subprocess.Popen) -> bool:
    """Only our unreaped Popen handle; never a PID scan, killpg or foreign listener."""
    try:
        if child.poll() is None:
            child.terminate()
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=3)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def collect(argv: list[str], out: Path, environ: dict, *, interfaces: bool = False) -> dict:
    """Private capture of fixed tool argv only; raw streams never become evidence."""
    if interfaces:
        if argv != [IFCONFIG, "-a"]:
            raise Refusal("invalid-interface-argv")
    else:
        try:
            plan = json.loads(argv[-1])
            expected = probe_argv(plan["python"], out, argv[-2], plan)
        except (ValueError, TypeError, KeyError, IndexError):
            raise Refusal("invalid-probe-argv") from None
        if argv != expected:
            raise Refusal("invalid-probe-argv")
    child = subprocess.Popen(argv, cwd=out, env=environ, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True)
    try:
        output, _ = child.communicate(timeout=5 if interfaces else 65)
        limit = 65536 if interfaces else 8192
        return {"exit": child.returncode, "capture": "complete" if len(output) <= limit else "oversize",
                "reaped": True, "data": output if len(output) <= limit else b""}
    except subprocess.TimeoutExpired:
        return {"exit": None, "capture": "timeout", "reaped": reap_owned(child), "data": b""}
    except BaseException:
        reap_owned(child)
        raise


def observation(capture: dict, role: str, nonce: str) -> dict | None:
    if capture["exit"] != 0 or capture["capture"] != "complete" or not capture["reaped"] \
            or len(capture["data"]) > 8192:
        return None
    try:
        result = json.loads(capture["data"])
        key = "addresses" if role == "resolve" else "rows"
        if set(result) != {"role", "nonce", "exec_identity", "cleanup", key} or result["role"] != role \
                or result["nonce"] != nonce or result["exec_identity"] is not True or result["cleanup"] is not True:
            return None
        if role == "resolve":
            if not isinstance(result[key], list) or len(result[key]) > 16:
                return None
            for value in result[key]:
                numeric_address(value, ipaddress.ip_address(value).version, outside=True)
        else:
            if [row["id"] for row in result[key]] != list(CONTROL_IDS):
                return None
            for row in result[key]:
                if set(row) != {"id", "outcome", "errno", "port"} or row["outcome"] not in OUTCOMES \
                        or (row["errno"] is not None and type(row["errno"]) is not int) \
                        or (row["port"] is not None and (type(row["port"]) is not int
                            or not 1 <= row["port"] <= 65535 or row["port"] in RESERVED_PORTS)) \
                        or (row["id"] in FIXTURE_IDS and row["outcome"] == "ok" and row["port"] is None) \
                        or (row["id"] in REMOTE_IDS and row["port"] is not None):
                    return None
        return result
    except (ValueError, TypeError, KeyError, Refusal):
        return None


def classify(baseline: dict | None, confined: dict | None, descendant: dict | None) -> str:
    incomplete = False
    for result in (baseline, confined, descendant):
        if result is None or result.get("cleanup") is not True or result.get("exec_identity") is not True:
            incomplete = True
    for index, name in enumerate(CONTROL_IDS):
        before = baseline["rows"][index] if baseline else None
        for result in (confined, descendant):
            after = result["rows"][index] if result else None
            if name in DENIED_IDS + REMOTE_IDS[:-1] and after and after["outcome"] == "ok":
                return "FAILED"  # An excluded owned endpoint or outside control answered, not an errno.
            if not before or before["outcome"] != "ok" or not after:
                incomplete = True
            elif name in LOCAL_IDS:
                if after["outcome"] == "denied":
                    return "FAILED"  # The profile masks a required positive control.
                if after["outcome"] != "ok" or after["port"] != before["port"]:
                    incomplete = True
            elif (name in DENIED_IDS and after["port"] != before["port"]) \
                    or after["outcome"] != "denied" or after["errno"] not in (errno.EACCES, errno.EPERM):
                incomplete = True  # Timeout, routing, cache and resolver errors are not policy denial.
    return "INCOMPLETE" if incomplete else "CONTROLS_PASSED"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--out", required=True)
    parser.add_argument("--qualify-only", required=True, action="store_true")
    args = parser.parse_args(argv)
    fd = helper_fd = None
    report = {"version": 1, "status": "INCOMPLETE", "reason": "not-started", "internet_denial_proven": False,
              "target": {key: re.sub(r"[^A-Za-z0-9_.-]", "_", value)[:80] for key, value in
                         (("os", platform.system()), ("release", platform.release()), ("arch", platform.machine()))},
              "tools": {}, "policy": {"grammar": "unverified-candidate", "remote": "deny-by-default",
              "exceptions": "exact-local-address-and-port", "bind_inbound": "unrestricted", "mach_lookup": "denied"},
              "helper_source_file": Path(__file__).name, "helper_source_sha256": None,
              "control_source_sha256": hashlib.sha256(CONTROL_SOURCE.encode()).hexdigest(), "profile_sha256": None,
              "outside_controls": {"tcp": {"host": HOST, "port": 443, "payload": "none"},
                                   "dns": {"addresses": list(DNS_CONTROLS), "port": 53, "protocol": "udp",
                                           "question": "random-nonce.example.com"},
                                   "resolver": "system-lookup; cached success and generic errors are inconclusive",
                                   "proxy": "no inherited proxy/auth/control environment; no proxy fallback"},
              "required_controls": list(CONTROL_IDS), "addresses": {}, "runs": {}, "cleanup": "not-started"}
    try:
        out, fd = fresh_output(args.out)
        reason = prerequisite_reason(platform.system(), sys.version_info[:2], os.geteuid(), os.environ,
                                     all(hasattr(os, key) for key in ("O_NOFOLLOW", "O_DIRECTORY"))
                                     and hasattr(socket, "IPV6_V6ONLY"))
        if reason:
            raise Refusal(reason)
        helper_fd = os.open(Path(__file__).absolute().parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        chains = (directory_chain(Path(__file__).absolute().parent, helper_fd), directory_chain(out, fd))
        report["helper_source_sha256"] = file_sha256(helper_fd, Path(__file__).name)
        for label, value in (("sandbox", SANDBOX), ("interfaces", IFCONFIG), ("python", sys.executable)):
            report["tools"][label] = trusted_executable(value, interpreter=label == "python")
        python = report["tools"]["python"]["path"]
        for leaf in ROOTS.values():
            os.mkdir(leaf, 0o700, dir_fd=fd)
        env = child_environment(out, os.environ)
        new_file(fd, "controls.py", CONTROL_SOURCE)
        plan = {"nonce": secrets.token_hex(16), "python": python, "out": str(out),
                "addresses": {"loopback4": "127.0.0.1", "loopback6": "::1", "wildcard4": None,
                              "wildcard6": None, "denied4": None, "denied6": None, "tcp4": None, "tcp6": None},
                "ports": dict.fromkeys(FIXTURE_IDS, 0)}
        stages = ("interfaces", "resolve", "baseline", "confined", "descendant")
        results = {}
        report["cleanup"] = "reaped"
        for stage in stages:
            verify_inputs(helper_fd, fd, report["helper_source_sha256"], report["profile_sha256"], out, chains)
            capture = collect([IFCONFIG, "-a"] if stage == "interfaces" else probe_argv(python, out, stage, plan),
                              out, env, interfaces=stage == "interfaces")
            report["runs"][stage] = {key: value for key, value in capture.items() if key != "data"}
            if not capture["reaped"] or (stage == "descendant" and observation(capture, stage, plan["nonce"]) is None):
                report["cleanup"] = "unconfirmed"
            if stage == "interfaces":
                if capture["exit"] != 0 or capture["capture"] != "complete":
                    raise Refusal("interface-data-unavailable")
                plan["addresses"].update(interface_addresses(capture["data"].decode("ascii")))
                for family in (4, 6):
                    plan["addresses"][f"denied{family}"] = plan["addresses"][f"wildcard{family}"]
                continue
            observed = observation(capture, stage, plan["nonce"])
            report["runs"][stage]["observation"] = observed
            if stage == "resolve":
                if observed is None:
                    raise Refusal("dns-baseline-unavailable")
                for family in (4, 6):
                    plan["addresses"][f"tcp{family}"] = next(
                        (value for value in observed["addresses"] if ipaddress.ip_address(value).version == family), None)
                validate_plan(plan)
                report["addresses"] = dict(plan["addresses"])
            else:
                results[stage] = observed
                if stage == "baseline":
                    if observed is None:
                        raise Refusal("baseline-unavailable")
                    for row in observed["rows"][:len(FIXTURE_IDS)]:
                        if row["outcome"] == "ok" and row["port"] is not None:
                            plan["ports"][row["id"]] = row["port"]
                    profile = profile_text(plan)
                    new_file(fd, "network.sb", profile)
                    report["profile_sha256"] = hashlib.sha256(profile.encode()).hexdigest()
        verify_inputs(helper_fd, fd, report["helper_source_sha256"], report["profile_sha256"], out, chains)
        report["status"] = classify(*(results.get(stage) for stage in stages[2:]))
        report["reason"] = "paired-controls-only" if report["status"] == "CONTROLS_PASSED" else "controls-not-qualified"
    except Refusal as exc:
        report["reason"] = str(exc)
    except (OSError, ValueError, subprocess.SubprocessError):
        report["reason"] = "prerequisite-or-observation-unavailable"
    except KeyboardInterrupt:
        report["reason"] = "interrupted"
        report["cleanup"] = "unconfirmed"
    finally:
        if fd is not None:
            try:
                new_file(fd, "qualification.json", json.dumps(report, sort_keys=True, indent=2) + "\n")
            except OSError:
                report["status"] = "INCOMPLETE"
                report["reason"] = "evidence-write-failed"
            finally:
                os.close(fd)
        if helper_fd is not None:
            os.close(helper_fd)
    print("darwin journey isolation: " + report["status"] + " (" + report["reason"] + ")", file=sys.stderr)
    return 0 if report["status"] == "CONTROLS_PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
