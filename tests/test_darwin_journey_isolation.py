"""Pure Linux/macOS checks of a candidate qualifier, NOT Darwin enforcement evidence.

All native subprocesses, sockets, platform/trust observations and output writes
are mocked. No product, native sandbox or Internet control executes in this lane.
"""

from __future__ import annotations

import ast
import contextlib
import copy
import errno
import hashlib
import importlib.util
import io
import json
import stat
import subprocess
import types
import unittest
from pathlib import Path
from unittest import mock

from _layout import REPO_ROOT
from _release import load_tool

PLATFORMS = ("linux", "darwin")

SCRIPT = REPO_ROOT / ".github" / "scripts" / "darwin_journey_isolation.py"
q = load_tool(SCRIPT, "darwin_journey_isolation")
OUT = Path("/private/tmp/qualification")
PYTHON = "/usr/bin/python3"
NONCE = "0" * 32
INTERFACES = """lo0: flags=8049<UP,LOOPBACK,RUNNING> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
\tinet6 ::1 prefixlen 128
en0: flags=8863<UP,BROADCAST,RUNNING> mtu 1500
\tinet 10.20.30.4 netmask 0xffffff00 broadcast 10.20.30.255
\tinet6 fe80::1%en0 prefixlen 64
\tinet6 fd12::2 prefixlen 64
"""


def plan() -> dict:
    return {"nonce": NONCE, "python": PYTHON, "out": str(OUT),
            "addresses": {"loopback4": "127.0.0.1", "loopback6": "::1", "wildcard4": "10.20.30.4",
                          "wildcard6": "fd12::2", "denied4": "10.20.30.4", "denied6": "fd12::2",
                          "tcp4": "93.184.216.34", "tcp6": "2606:4700:4700::1001"},
            "ports": dict(zip(q.FIXTURE_IDS, range(40000, 40006)))}


def record(role: str) -> dict:
    rows = [{"id": name, "outcome": "ok", "errno": None,
             "port": plan()["ports"].get(name)} for name in q.CONTROL_IDS]
    if role in ("confined", "descendant"):
        for row in rows:
            if row["id"] not in q.LOCAL_IDS:
                row.update(outcome="denied", errno=errno.EPERM)
    return {"role": role, "nonce": NONCE, "exec_identity": True, "cleanup": True, "rows": rows}


def capture(data: dict | bytes) -> dict:
    return {"exit": 0, "capture": "complete", "reaped": True,
            "data": data if isinstance(data, bytes) else json.dumps(data).encode()}


def paired_captures() -> list[dict]:
    resolved = {"role": "resolve", "nonce": NONCE, "exec_identity": True, "cleanup": True,
                "addresses": [plan()["addresses"]["tcp4"], plan()["addresses"]["tcp6"]]}
    return [capture(INTERFACES.encode()), capture(resolved), capture(record("baseline")),
            capture(record("confined")), capture(record("descendant"))]


def socket_mock() -> mock.MagicMock:
    sock = mock.MagicMock()
    sock.__enter__.return_value = sock
    return sock


def literal_functions() -> dict:
    """Load only function definitions; no native script entry code executes."""
    tree = ast.parse(q.CONTROL_SOURCE)
    namespace = {"P": plan(), "socket": mock.Mock(), "errno": errno,
                 "ipaddress": q.ipaddress, "struct": __import__("struct")}
    functions = ast.Module(body=[node for node in tree.body if isinstance(node, ast.FunctionDef)], type_ignores=[])
    exec(compile(functions, "<qualification-functions>", "exec"), namespace)
    return namespace


class SafetyTests(unittest.TestCase):
    def test_import_and_help_do_not_spawn_connect_or_create_output(self) -> None:
        with mock.patch.object(q.subprocess, "Popen") as popen, \
                mock.patch.object(q.socket, "socket") as sock, \
                mock.patch.object(q.socket, "getaddrinfo") as dns, \
                mock.patch.object(q.os, "mkdir") as mkdir, \
                mock.patch.object(q.platform, "system") as system:
            spec = importlib.util.spec_from_file_location("_qualification_import_check", SCRIPT)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
                module.main(["--help"])
            self.assertEqual(raised.exception.code, 0)
            for interface in (popen, sock, dns, mkdir, system):
                interface.assert_not_called()

    def test_explicit_flag_and_no_command_or_fallback_surface(self) -> None:
        for argv in (["--out", str(OUT)], ["--out", str(OUT), "--qualify-on"],
                     ["--out", str(OUT), "--qualify-only", "--allow-unconfined"],
                     ["--out", str(OUT), "--qualify-only", "--", "/bin/sh"]):
            with self.subTest(argv=argv), mock.patch.object(q, "fresh_output") as fresh, \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                q.main(argv)
            self.assertEqual(raised.exception.code, 2)
            fresh.assert_not_called()

    def test_environment_is_allowlisted_and_preserves_guards_even_when_empty(self) -> None:
        ambient = {"PATH": "/untrusted/bin", "HTTP_PROXY": "fixture", "https_proxy": "fixture",
                   "GITHUB_TOKEN": "dummy", "ANTHROPIC_API_KEY": "dummy", "SSH_AUTH_SOCK": "fixture",
                   "PYTHONPATH": "fixture", "DYLD_INSERT_LIBRARIES": "fixture", "HOME": "/home/user"}
        ambient.update(dict.fromkeys(q.GUARD_MARKERS, ""))
        env = q.child_environment(OUT, ambient)
        self.assertEqual(set(env), set(q.ROOTS) | set(q.GUARD_MARKERS) | {"PATH", "LANG", "LC_ALL"})
        self.assertEqual(env["PATH"], "/usr/bin:/bin:/usr/sbin:/sbin")
        for key, leaf in q.ROOTS.items():
            self.assertEqual(env[key], str(OUT / leaf))
        for key in q.GUARD_MARKERS:
            self.assertEqual(env[key], "")
            self.assertEqual(q.prerequisite_reason("Darwin", (3, 14), 1000, {key: ""}, True),
                             "session-or-harness-refused")

    def test_launch_epoch_alone_is_preserved_and_refused(self) -> None:
        ambient = {"CHARMER_LAUNCH_EPOCH": "7"}
        self.assertEqual(q.child_environment(OUT, ambient)["CHARMER_LAUNCH_EPOCH"], "7")
        self.assertEqual(q.prerequisite_reason("Darwin", (3, 14), 1000, ambient, True),
                         "session-or-harness-refused")

    def test_prerequisites_fail_closed(self) -> None:
        for system, version, uid, caps in (("Linux", (3, 14), 1000, True), ("Darwin", (3, 10), 1000, True),
                                           ("Darwin", (3, 14), 0, True), ("Darwin", (3, 14), 1000, False)):
            self.assertIsNotNone(q.prerequisite_reason(system, version, uid, {}, caps))
        self.assertIsNone(q.prerequisite_reason("Darwin", (3, 14), 1000, {}, True))

    def test_paths_and_numeric_addresses_reject_injection_and_ambiguous_forms(self) -> None:
        for value in ("relative", "/", "/private/../new", "/private/./new", "/private//new", "/private/new\n",
                      '/private/"new', "/private/new space", "/private/new\\path"):
            with self.subTest(value=value), self.assertRaises(q.Refusal):
                q.absolute_path(value)
        for value in ("localhost", "127.0.0.1:443", "127.0.0.1/8", "::1%lo0", "[::1]", "0.0.0.0",
                      "ff02::1", "fe80::1", '127.0.0.1\") (allow network*)', "FD12::2", "::ffff:127.0.0.1",
                      "::ffff:93.184.216.34", "::ffff:5db8:d822", "::ffff:7f00:1"):
            with self.subTest(value=value), self.assertRaises(q.Refusal):
                q.numeric_address(value, 6 if ":" in value else 4)
        self.assertEqual(q.numeric_address("::1", 6), "::1")
        self.assertEqual(q.numeric_address("127.0.0.1", 4), "127.0.0.1")
        with self.assertRaises(q.Refusal):
            q.numeric_address("10.20.30.4", 4, outside=True)

    def test_profile_is_fixed_narrow_and_does_not_force_loopback_bind(self) -> None:
        text = q.profile_text(plan())
        self.assertTrue(text.startswith("(version 1)\n(allow default)\n(deny network*)\n(deny mach-lookup)\n"))
        self.assertIn("(allow network-bind)\n(allow network-inbound)\n", text)
        for endpoint in ("127.0.0.1:40000", "[::1]:40001", "10.20.30.4:40002", "[fd12::2]:40003"):
            self.assertIn(f'(remote tcp "{endpoint}")', text)
        for value in (q.HOST, *q.DNS_CONTROLS, plan()["addresses"]["tcp4"], "localhost", "*:443", "local ip",
                      "10.20.30.4:40004", "[fd12::2]:40005"):
            self.assertNotIn(value, text)
        for key, value in (("wildcard4", q.DNS_CONTROLS[0]), ("wildcard4", "127.0.0.2"),
                           ("tcp4", "10.20.30.4"), ("loopback6", "fd12::3"),
                           ("denied4", "93.184.216.34"), ("denied6", "::1")):
            altered = plan()
            altered["addresses"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(q.Refusal):
                q.profile_text(altered)
        for port in (8316, 8317, -1, 65536, True, "40000"):
            altered = plan()
            altered["ports"]["wildcard4"] = port
            with self.subTest(port=port), self.assertRaises(q.Refusal):
                q.profile_text(altered)

        altered = plan()
        altered["ports"]["denied4"] = altered["ports"]["wildcard4"]
        with self.assertRaises(q.Refusal):
            q.profile_text(altered)

    def test_source_and_argv_are_not_a_general_execution_proxy(self) -> None:
        native = ast.parse(q.CONTROL_SOURCE)
        calls = [node for node in ast.walk(native) if isinstance(node, ast.Call)]
        popen = [node for node in calls if isinstance(node.func, ast.Attribute) and node.func.attr == "Popen"]
        self.assertEqual(len(popen), 1)
        child_argv = ast.unparse(popen[0].args[0])
        self.assertIn("P['python']", child_argv)
        self.assertIn("__file__", child_argv)
        self.assertIn("'exec'", child_argv)
        self.assertNotIn("sandbox-exec", child_argv)  # Inheritance, not a freshly applied child profile.
        self.assertIn("child.wait(timeout=3)", q.CONTROL_SOURCE)
        self.assertNotIn("shell=", q.CONTROL_SOURCE)
        self.assertNotIn("os.system", q.CONTROL_SOURCE)
        for role in ("resolve", "baseline", "confined", "descendant"):
            argv = q.probe_argv(PYTHON, OUT, role, plan())
            prefix = [q.SANDBOX, "-f", str(OUT / "network.sb")] if role in ("confined", "descendant") else []
            self.assertEqual(argv[:-1], prefix + [PYTHON, "-I", "-S", "-B", str(OUT / "controls.py"), role])
        with self.assertRaises(q.Refusal):
            q.probe_argv(PYTHON, OUT, "/bin/sh", plan())
        argv = q.probe_argv(PYTHON, OUT, "confined", plan())
        argv[4] = "-c"
        with mock.patch.object(q.subprocess, "Popen") as spawn, self.assertRaises(q.Refusal):
            q.collect(argv, OUT, {})
        spawn.assert_not_called()


class OwnershipTests(unittest.TestCase):
    def test_trust_requires_fixed_root_owned_nonwritable_executable_and_ancestors(self) -> None:
        path = Path(PYTHON)
        payload = b"fixture executable identity"
        def metadata(component):
            return types.SimpleNamespace(st_mode=(stat.S_IFREG if component == path else stat.S_IFDIR) | 0o755,
                                         st_uid=0, st_size=len(payload))
        with mock.patch.object(Path, "resolve", return_value=path), \
                mock.patch.object(Path, "lstat", autospec=True, side_effect=metadata), \
                mock.patch.object(Path, "open", return_value=io.BytesIO(payload)), mock.patch.object(q.os, "access", return_value=True):
            identity = q.trusted_executable(PYTHON, interpreter=True)
        self.assertEqual(identity["sha256"], hashlib.sha256(payload).hexdigest())
        for mode, uid in ((stat.S_IFREG | 0o775, 0), (stat.S_IFREG | 0o755, 1000),
                          (stat.S_IFLNK | 0o777, 0), (stat.S_IFREG | 0o4755, 0)):
            def unsafe(component):
                return types.SimpleNamespace(st_mode=mode, st_uid=uid, st_size=1) if component == path else metadata(component)
            with self.subTest(mode=mode, uid=uid), mock.patch.object(Path, "resolve", return_value=path), \
                    mock.patch.object(Path, "lstat", autospec=True, side_effect=unsafe), self.assertRaises(q.Refusal):
                q.trusted_executable(PYTHON, interpreter=True)
        with mock.patch.object(Path, "resolve", return_value=path), \
                mock.patch.object(Path, "lstat", return_value=types.SimpleNamespace(st_mode=stat.S_IFDIR | 0o777, st_uid=0)), \
                self.assertRaises(q.Refusal):
            q.trusted_executable(PYTHON, interpreter=True)
        for value in ("/private/tmp/python3", "/private/bundle/runtime/python/bin/python3", "/nix/store/fixture/bin/python3"):
            self.assertFalse(q.interpreter_path(value))
        with mock.patch.object(Path, "resolve", return_value=Path("/private/tmp/sandbox-exec")), self.assertRaises(q.Refusal):
            q.trusted_executable(q.SANDBOX)
        with mock.patch.object(Path, "resolve", return_value=path), \
                mock.patch.object(Path, "lstat", autospec=True, side_effect=metadata), \
                mock.patch.object(q.os, "access", return_value=False), self.assertRaises(q.Refusal):
            q.trusted_executable(PYTHON, interpreter=True)

    def test_fresh_directory_uses_nofollow_descriptors_and_refuses_existing_paths(self) -> None:
        def metadata(fd):
            return types.SimpleNamespace(st_uid=1000 if fd == 13 else 0,
                                         st_mode=stat.S_IFDIR | (0o700 if fd == 13 else 0o1777))
        with mock.patch.object(q.os, "open", side_effect=[10, 11, 12, 13]) as opened, \
                mock.patch.object(q.os, "close") as closed, mock.patch.object(q.os, "fstat", side_effect=metadata), \
                mock.patch.object(q.os, "geteuid", return_value=1000), mock.patch.object(q.os, "mkdir") as mkdir:
            self.assertEqual(q.fresh_output(str(OUT)), (OUT, 13))
            mkdir.assert_called_once_with("qualification", 0o700, dir_fd=12)
            self.assertTrue(all(call.args[1] & q.os.O_NOFOLLOW for call in opened.call_args_list))
            self.assertNotIn(mock.call(13), closed.call_args_list)
        with mock.patch.object(q.os, "open", side_effect=[10, 11, 12]), mock.patch.object(q.os, "close"), \
                mock.patch.object(q.os, "fstat", side_effect=metadata), mock.patch.object(q.os, "geteuid", return_value=1000), \
                mock.patch.object(q.os, "mkdir", side_effect=FileExistsError), self.assertRaises(FileExistsError):
            q.fresh_output(str(OUT))
        with mock.patch.object(q.os, "open", side_effect=[10, OSError(errno.ELOOP, "fixture")]), \
                mock.patch.object(q.os, "close"), mock.patch.object(q.os, "mkdir") as mkdir, self.assertRaises(OSError):
            q.fresh_output(str(OUT))
        mkdir.assert_not_called()

    def test_evidence_is_exclusive_private_and_never_truncated(self) -> None:
        stream = mock.MagicMock()
        stream.__enter__.return_value = stream
        stream.fileno.return_value = 44
        with mock.patch.object(q.os, "open", return_value=44) as opened, \
                mock.patch.object(q.os, "fdopen", return_value=stream), mock.patch.object(q.os, "fsync") as sync:
            q.new_file(12, "network.sb", "fixture profile")
        flags = opened.call_args.args[1]
        self.assertTrue(flags & q.os.O_EXCL and flags & q.os.O_NOFOLLOW)
        self.assertFalse(flags & q.os.O_TRUNC)
        self.assertEqual(opened.call_args.args[2], 0o600)
        self.assertEqual(opened.call_args.kwargs, {"dir_fd": 12})
        self.assertEqual(sync.call_args_list, [mock.call(44), mock.call(12)])
        with self.assertRaises(q.Refusal):
            q.new_file(12, "../existing", "fixture")

    def test_directory_paths_reopen_every_component_and_bind_to_held_fds(self) -> None:
        inodes = {20: 1, 21: 2, 22: 3, 23: 4, 12: 4}
        def metadata(fd):
            return types.SimpleNamespace(st_dev=1, st_ino=inodes[fd], st_mode=stat.S_IFDIR | 0o700)
        def reopen():
            return mock.patch.object(q.os, "open", side_effect=[20, 21, 22, 23])
        with reopen() as opened, mock.patch.object(q.os, "fstat", side_effect=metadata), \
                mock.patch.object(q.os, "close") as closed:
            original = q.directory_chain(OUT, 12)
        self.assertEqual(original, ((1, 1), (1, 2), (1, 3), (1, 4)))
        self.assertTrue(all(call.args[1] & q.os.O_NOFOLLOW and call.args[1] & q.os.O_DIRECTORY
                            for call in opened.call_args_list))
        self.assertEqual(opened.call_args_list[-1].kwargs, {"dir_fd": 22})
        self.assertNotIn(mock.call(12), closed.call_args_list)
        inodes[23] = 40  # Held fd 12 still names the original output directory.
        with reopen(), mock.patch.object(q.os, "fstat", side_effect=metadata), \
                mock.patch.object(q.os, "close"), self.assertRaisesRegex(q.Refusal, "^input-drift$"):
            q.directory_chain(OUT, 12)
        inodes[23], inodes[22] = 4, 30  # Old leaf reattached beneath a replacement ancestor.
        with reopen(), mock.patch.object(q.os, "fstat", side_effect=metadata), mock.patch.object(q.os, "close"):
            changed = q.directory_chain(OUT, 12)
        self.assertEqual(changed[-1], original[-1])
        self.assertNotEqual(changed, original)
        with mock.patch.object(q.os, "open", side_effect=[20, 21, OSError(errno.ELOOP, "fixture")]), \
                mock.patch.object(q.os, "fstat", side_effect=metadata), mock.patch.object(q.os, "close"), \
                self.assertRaises(OSError):
            q.directory_chain(OUT, 12)

    def test_bound_source_reads_are_fd_relative_nofollow_and_regular_only(self) -> None:
        payload = b"fixture bound source"
        with mock.patch.object(q.os, "open", return_value=44) as opened, \
                mock.patch.object(q.os, "fdopen", return_value=io.BytesIO(payload)), \
                mock.patch.object(q.os, "fstat", return_value=types.SimpleNamespace(st_mode=stat.S_IFREG | 0o600)):
            self.assertEqual(q.file_sha256(12, "controls.py"), hashlib.sha256(payload).hexdigest())
        self.assertEqual(opened.call_args.args[0], "controls.py")
        self.assertTrue(opened.call_args.args[1] & q.os.O_NOFOLLOW)
        self.assertTrue(opened.call_args.args[1] & q.os.O_NONBLOCK)
        self.assertEqual(opened.call_args.kwargs, {"dir_fd": 12})
        with mock.patch.object(q.os, "open", return_value=44), \
                mock.patch.object(q.os, "fdopen", return_value=io.BytesIO(payload)), \
                mock.patch.object(q.os, "fstat", return_value=types.SimpleNamespace(st_mode=stat.S_IFIFO | 0o600)), \
                self.assertRaisesRegex(q.Refusal, "^input-drift$"):
            q.file_sha256(12, "controls.py")
        with mock.patch.object(q, "file_sha256", side_effect=OSError(errno.ELOOP, "not retained")), \
                mock.patch.object(q, "directory_chain", return_value=()), \
                self.assertRaisesRegex(q.Refusal, "^input-drift$"):
            q.verify_inputs(13, 12, "0" * 64, None, OUT, ((), ()))

    def test_owned_child_cleanup_is_bounded_and_never_signals_a_foreign_pid(self) -> None:
        child = mock.Mock()
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired("fixed-probe", 3), 0]
        self.assertTrue(q.reap_owned(child))
        child.terminate.assert_called_once_with()
        child.kill.assert_called_once_with()
        self.assertEqual(child.wait.call_args_list, [mock.call(timeout=3), mock.call(timeout=3)])
        child.wait.side_effect = subprocess.TimeoutExpired("fixed-probe", 3)
        self.assertFalse(q.reap_owned(child))
        child.reset_mock()
        child.poll.return_value = 0
        child.wait.side_effect = None
        self.assertTrue(q.reap_owned(child))
        child.kill.assert_not_called()
        child.terminate.assert_not_called()
        argv = q.probe_argv(PYTHON, OUT, "confined", plan())
        child.communicate.side_effect = subprocess.TimeoutExpired("fixed-probe", 65)
        with mock.patch.object(q.subprocess, "Popen", return_value=child), \
                mock.patch.object(q, "reap_owned", return_value=False) as reaper:
            result = q.collect(argv, OUT, {})
        reaper.assert_called_once_with(child)
        self.assertFalse(result["reaped"])
        self.assertEqual(result["capture"], "timeout")


class ControlTests(unittest.TestCase):
    def test_read_only_interface_selection_does_not_invent_scoped_ipv6_coverage(self) -> None:
        self.assertEqual(q.interface_addresses(INTERFACES), {"wildcard4": "10.20.30.4", "wildcard6": "fd12::2"})
        self.assertIsNone(q.interface_addresses(INTERFACES.replace("\tinet6 fd12::2 prefixlen 64\n", ""))["wildcard6"])
        self.assertEqual(q.interface_addresses("en0: flags=0<BROADCAST>\n\tinet 10.20.30.4\n"),
                         {"wildcard4": None, "wildcard6": None})
        with self.assertRaises(ValueError):
            q.interface_addresses("en0: flags=1<UP>\n\tinet not-an-address\n")

    def test_native_fixture_uses_actual_nonloopback_wildcard_bind_and_nonce(self) -> None:
        ns = literal_functions()
        listener, client, peer = (socket_mock() for _ in range(3))
        listener.getsockname.return_value = ("0.0.0.0", 40002)
        listener.accept.return_value = (peer, ("10.20.30.4", 40002))
        challenge = (NONCE + "/wildcard4").encode()
        peer.recv.return_value = client.recv.return_value = challenge
        ns["socket"].socket.side_effect = [listener, client]
        self.assertEqual(ns["local"]("wildcard4"), ("ok", 40002))
        listener.bind.assert_called_once_with(("0.0.0.0", 40002))
        client.connect.assert_called_once_with(("10.20.30.4", 40002))
        peer.sendall.assert_called_once_with(challenge)
        peer.recv.return_value = b"foreign"
        client.recv.return_value = b"foreign"
        ns["socket"].socket.side_effect = [listener, client]
        self.assertEqual(ns["local"]("wildcard4")[0], "identity_error")

    def test_owned_denied_twins_authenticate_baselines_and_deny_only_at_connect(self) -> None:
        for family, bind in ((4, "0.0.0.0"), (6, "::")):
            name = f"denied{family}"
            ns = literal_functions()
            port, address = ns["P"]["ports"][name], ns["P"]["addresses"][name]
            listener, client, peer = (socket_mock() for _ in range(3))
            listener.getsockname.return_value = (bind, port)
            listener.accept.return_value = (peer, (address, port))
            challenge = (NONCE + "/" + name).encode()
            peer.recv.return_value = client.recv.return_value = challenge
            ns["socket"].socket.side_effect = [listener, client]
            self.assertEqual(ns["local"](name), ("ok", port))
            listener.bind.assert_called_once_with((bind, port))
            client.connect.assert_called_once_with((address, port))
            peer.sendall.assert_called_once_with(challenge)
            for error in (errno.EPERM, errno.EACCES):
                ns["socket"].socket.side_effect = [listener, client]
                client.connect.side_effect = OSError(error, "fixture denial")
                row = {"errno": None}
                self.assertEqual(ns["local"](name, row), ("denied", port))
                self.assertEqual(row["errno"], error)
            ns["socket"].socket.side_effect = [listener]
            listener.bind.side_effect = OSError(errno.EPERM, "not connect denial")
            row = {"errno": None}
            with self.assertRaises(OSError):
                ns["local"](name, row)
            self.assertIsNone(row["errno"])

    def test_ipv6_wildcard_and_udp_dns_controls_use_fixed_numeric_targets(self) -> None:
        ns = literal_functions()
        listener, client, peer = (socket_mock() for _ in range(3))
        listener.getsockname.return_value = ("::", 40003, 0, 0)
        listener.accept.return_value = (peer, ("fd12::2", 40003, 0, 0))
        peer.recv.return_value = client.recv.return_value = (NONCE + "/wildcard6").encode()
        ns["socket"].socket.side_effect = [listener, client]
        self.assertEqual(ns["local"]("wildcard6"), ("ok", 40003))
        listener.bind.assert_called_once_with(("::", 40003))
        listener.setsockopt.assert_any_call(ns["socket"].IPPROTO_IPV6, ns["socket"].IPV6_V6ONLY, 1)
        client.connect.assert_called_once_with(("fd12::2", 40003))
        dns = socket_mock()
        ns["socket"].socket.side_effect = [dns]
        def response(count):
            query = dns.send.call_args.args[0]
            return query[:2] + b"\x81\x83" + query[4:]  # Non-provider NXDOMAIN with the nonce question.
        dns.recv.side_effect = response
        self.assertEqual(ns["remote"]("dns6"), "ok")
        dns.connect.assert_called_once_with((q.DNS_CONTROLS[1], 53))
        self.assertIn(NONCE.encode(), dns.send.call_args.args[0])

    def test_reserved_ephemeral_ports_are_resampled_with_a_bound(self) -> None:
        ns = literal_functions()
        ns["P"]["ports"]["loopback4"] = 0
        blocked = socket_mock()
        blocked.getsockname.return_value = ("127.0.0.1", 8317)
        ns["socket"].socket.return_value = blocked
        self.assertEqual(ns["local"]("loopback4"), ("missing", None))
        self.assertEqual(ns["socket"].socket.call_count, 8)
        blocked.listen.assert_not_called()
        blocked.connect.assert_not_called()
        self.assertEqual(blocked.__exit__.call_count, 8)

    def test_denied_twin_resamples_an_allowed_ephemeral_port_collision(self) -> None:
        ns = literal_functions()
        ns["P"]["ports"]["denied4"] = 0
        collision, listener, client, peer = (socket_mock() for _ in range(4))
        collision.getsockname.return_value = ("0.0.0.0", 40002)
        listener.getsockname.return_value = ("0.0.0.0", 40004)
        listener.accept.return_value = (peer, ("10.20.30.4", 40004))
        peer.recv.return_value = client.recv.return_value = (NONCE + "/denied4").encode()
        ns["socket"].socket.side_effect = [collision, listener, client]
        self.assertEqual(ns["local"]("denied4"), ("ok", 40004))
        collision.listen.assert_not_called()
        collision.__exit__.assert_called_once()
        client.connect.assert_called_once_with(("10.20.30.4", 40004))

    def test_only_complete_paired_controls_can_pass(self) -> None:
        before, after, child = (record(role) for role in ("baseline", "confined", "descendant"))
        self.assertEqual(q.classify(before, after, child), "CONTROLS_PASSED")  # Synthetic observations only.
        for name in q.CONTROL_IDS:
            missing = copy.deepcopy(before)
            missing["rows"][q.CONTROL_IDS.index(name)]["outcome"] = "missing"
            self.assertEqual(q.classify(missing, after, child), "INCOMPLETE", name)
        for category in ("timeout", "unreachable", "dns_error", "identity_error"):
            altered = copy.deepcopy(after)
            altered["rows"][q.CONTROL_IDS.index("tcp4")].update(outcome=category, errno=errno.ECONNREFUSED)
            self.assertEqual(q.classify(before, altered, child), "INCOMPLETE")
        for name in q.REMOTE_IDS[:-1]:
            escaped = copy.deepcopy(child)
            escaped["rows"][q.CONTROL_IDS.index(name)].update(outcome="ok", errno=None)
            self.assertEqual(q.classify(before, after, escaped), "FAILED")
        masked = copy.deepcopy(after)
        masked["rows"][2].update(outcome="denied", errno=errno.EPERM)
        self.assertEqual(q.classify(before, masked, child), "FAILED")
        for key in ("cleanup", "exec_identity"):
            altered = copy.deepcopy(child)
            altered[key] = False
            self.assertEqual(q.classify(before, after, altered), "INCOMPLETE")
        for category in ("ok", "dns_error", "timeout"):
            resolver = copy.deepcopy(after)
            resolver["rows"][-1].update(outcome=category, errno=None)
            self.assertEqual(q.classify(before, resolver, child), "INCOMPLETE")
        self.assertEqual(q.classify(before, None, child), "INCOMPLETE")

    def test_public_denials_cannot_substitute_for_excluded_owned_twins(self) -> None:
        before = record("baseline")
        for name in q.DENIED_IDS:
            index = q.CONTROL_IDS.index(name)
            for role in ("confined", "descendant"):
                after, child = record("confined"), record("descendant")
                target = after if role == "confined" else child
                target["rows"][index].update(outcome="ok", errno=None)
                self.assertEqual(q.classify(before, after, child), "FAILED", (name, role))
                for outcome, error, port in (("timeout", None, plan()["ports"][name]),
                                              ("denied", errno.EPERM, None),  # Bind/socket/send failure, not connect.
                                              ("denied", errno.ECONNREFUSED, plan()["ports"][name])):
                    target["rows"][index].update(outcome=outcome, errno=error, port=port)
                    self.assertEqual(q.classify(before, after, child), "INCOMPLETE", (name, role, outcome))

    def test_observation_rejects_wrong_identity_extra_data_ports_and_incomplete_cleanup(self) -> None:
        valid = record("confined")
        self.assertEqual(q.observation(capture(valid), "confined", NONCE), valid)
        mutations = []
        for key, value in (("nonce", "1" * 32), ("exec_identity", False), ("cleanup", False), ("unexpected", "fixture")):
            altered = copy.deepcopy(valid)
            altered[key] = value
            mutations.append(altered)
        for port in (None, 8316, 8317, True):
            altered = copy.deepcopy(valid)
            altered["rows"][0]["port"] = port
            mutations.append(altered)
        altered = copy.deepcopy(valid)
        altered["rows"].reverse()
        mutations.append(altered)
        for altered in mutations:
            self.assertIsNone(q.observation(capture(altered), "confined", NONCE))
        self.assertIsNone(q.observation(capture(b"x" * 8193), "confined", NONCE))


class ReportTests(unittest.TestCase):
    def run_mock_main(self, *, system="Linux", captures=None, trust_error=None, write_error=False,
                      drift=None, late=False, chain_drift=None):
        written = {}
        def read_chain(path, fd):
            self.assertEqual(path, SCRIPT.parent if fd == 13 else OUT)
            expected = ((1, 1), (1, 2), (1, fd))
            changed = collect.call_count >= 5 if late else "controls.py" in written
            if changed and ((fd == 12 and chain_drift in ("output", "ancestor", "symlink"))
                            or (fd == 13 and chain_drift == "helper")):
                if chain_drift == "symlink":
                    raise OSError(errno.ELOOP, "not retained")
                if chain_drift == "ancestor":
                    return (expected[0], (1, 99), expected[-1])  # Old leaf and held fd still valid.
                raise q.Refusal("input-drift")  # Reopened leaf differs from the still-valid held fd.
            return expected
        def read_hash(fd, name):
            self.assertEqual(fd, 13 if name == SCRIPT.name else 12)
            expected = (hashlib.sha256(b"fixture helper source").hexdigest() if name == SCRIPT.name else
                        hashlib.sha256(written[name].encode()).hexdigest())
            changed = collect.call_count >= 5 if late else "controls.py" in written
            return "f" * 64 if name == drift and changed else expected
        def write(fd, name, content):
            if write_error and name == "qualification.json":
                raise OSError("fixture evidence failure")
            written[name] = content
        def trust(value, **kwargs):
            if trust_error:
                raise trust_error
            return {"path": value if value in (q.SANDBOX, q.IFCONFIG) else PYTHON,
                    "sha256": "0" * 64, "size": 1, "uid": 0, "mode": 0o755}
        with mock.patch.object(q, "fresh_output", return_value=(OUT, 12)), \
                mock.patch.object(q, "directory_chain", side_effect=read_chain), \
                mock.patch.object(q, "file_sha256", side_effect=read_hash), mock.patch.object(q.os, "open", return_value=13), \
                mock.patch.object(q, "new_file", side_effect=write), mock.patch.object(q, "trusted_executable", side_effect=trust), \
                mock.patch.object(q.platform, "system", return_value=system), \
                mock.patch.object(q.platform, "release", return_value="fixture"), \
                mock.patch.object(q.platform, "machine", return_value="arm64"), \
                mock.patch.object(q.os, "geteuid", return_value=1000), mock.patch.object(q.os, "environ", {}), \
                mock.patch.object(q.os, "mkdir") as mkdir, mock.patch.object(q.os, "close"), \
                mock.patch.object(q.secrets, "token_hex", return_value=NONCE), \
                mock.patch.object(q, "collect", side_effect=captures) as collect, \
                mock.patch.object(q.subprocess, "Popen") as spawn, \
                mock.patch.object(q.socket, "socket") as sock, contextlib.redirect_stderr(io.StringIO()):
            status = q.main(["--out", str(OUT), "--qualify-only"])
            spawn.assert_not_called()
            sock.assert_not_called()
        return status, written, collect, mkdir

    def test_nondarwin_and_missing_tool_keep_fixed_reports_without_execution(self) -> None:
        for system, error, reason in (("Linux", None, "unsupported-platform"),
                                      ("Darwin", FileNotFoundError("do not retain raw exception text"),
                                       "prerequisite-or-observation-unavailable")):
            status, written, collect, mkdir = self.run_mock_main(system=system, trust_error=error)
            self.assertEqual(status, 1)
            report = json.loads(written["qualification.json"])
            self.assertEqual(report["status"], "INCOMPLETE")
            self.assertEqual(report["reason"], reason)
            self.assertFalse(report["internet_denial_proven"])
            self.assertEqual(set(written), {"qualification.json"})
            collect.assert_not_called()
            mkdir.assert_not_called()
            self.assertNotIn("raw exception", written["qualification.json"])

    def test_helper_control_and_profile_drift_stop_execution_or_prevent_a_pass(self) -> None:
        for name, before_execution_count in ((SCRIPT.name, 0), ("controls.py", 0), ("network.sb", 3)):
            for late in (False, True):
                with self.subTest(name=name, late=late):
                    status, written, collect, _ = self.run_mock_main(
                        system="Darwin", captures=paired_captures(), drift=name, late=late)
                    report = json.loads(written["qualification.json"])
                    self.assertEqual(status, 1)
                    self.assertEqual(report["status"], "INCOMPLETE")
                    self.assertEqual(report["reason"], "input-drift")
                    self.assertEqual(collect.call_count, 5 if late else before_execution_count)
                    self.assertEqual(report["helper_source_sha256"], hashlib.sha256(b"fixture helper source").hexdigest())
                    self.assertNotIn("fixture helper source", written["qualification.json"])

    def test_replaced_execution_paths_cannot_launch_or_pass_with_unchanged_held_files(self) -> None:
        for replacement in ("output", "ancestor", "symlink", "helper"):
            for late in (False, True):
                with self.subTest(replacement=replacement, late=late):
                    status, written, collect, _ = self.run_mock_main(
                        system="Darwin", captures=paired_captures(), chain_drift=replacement, late=late)
                    report = json.loads(written["qualification.json"])
                    self.assertEqual(status, 1)
                    self.assertEqual(report["status"], "INCOMPLETE")
                    self.assertEqual(report["reason"], "input-drift")
                    self.assertEqual(collect.call_count, 5 if late else 0)
                    self.assertEqual(written["controls.py"], q.CONTROL_SOURCE)
                    self.assertEqual(report["control_source_sha256"], hashlib.sha256(q.CONTROL_SOURCE.encode()).hexdigest())

    def test_report_binds_exact_source_profile_outcomes_and_child_cleanup(self) -> None:
        captures = paired_captures()
        status, written, collect, mkdir = self.run_mock_main(system="Darwin", captures=captures)
        self.assertEqual(status, 0)
        report = json.loads(written["qualification.json"])
        self.assertEqual(written["controls.py"], q.CONTROL_SOURCE)
        self.assertEqual(report["helper_source_file"], SCRIPT.name)
        self.assertEqual(report["helper_source_sha256"], hashlib.sha256(b"fixture helper source").hexdigest())
        self.assertEqual(report["control_source_sha256"], hashlib.sha256(written["controls.py"].encode()).hexdigest())
        self.assertNotIn("source_sha256", report)
        self.assertEqual(report["profile_sha256"], hashlib.sha256(written["network.sb"].encode()).hexdigest())
        self.assertEqual(report["status"], "CONTROLS_PASSED")
        self.assertEqual(report["cleanup"], "reaped")
        self.assertFalse(report["internet_denial_proven"])
        self.assertEqual(collect.call_count, 5)
        self.assertEqual(mkdir.call_count, len(q.ROOTS))
        for call in mkdir.call_args_list:
            self.assertEqual(call.args[1], 0o700)
        bad = copy.deepcopy(captures)
        bad[-1].update(exit=None, capture="timeout", reaped=False, data=b"")
        status, written, _, _ = self.run_mock_main(system="Darwin", captures=bad)
        self.assertEqual(status, 1)
        report = json.loads(written["qualification.json"])
        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertEqual(report["cleanup"], "unconfirmed")
        status, _, _, _ = self.run_mock_main(system="Darwin", captures=captures, write_error=True)
        self.assertEqual(status, 1)
