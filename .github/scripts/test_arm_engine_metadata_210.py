"""Pure fixtures for the public inline collector; no engine or host probing.

Only ci.yml is read. Loading this fixture file does not load/run the collector.
All collector OS, filesystem, process, clock and output boundaries are mocked.
"""

import ast
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import textwrap
import time
import types
import unittest
from unittest import mock


PRIVATE = "fixture-private-sentinel-not-for-output"
FIELDS = {"version", "native_arch", "rootless", "owned_store_match", "localness", "outcome"}
REQUIRED_FIELDS = (
    ("host", "os"), ("host", "arch"), ("host", "security", "rootless"),
    ("host", "serviceIsRemote"), ("host", "cgroupManager"), ("host", "eventLogger"),
    ("store", "graphDriverName"), ("store", "graphRoot"), ("store", "runRoot"),
    ("store", "volumePath"), ("store", "imageCopyTmpDir"), ("version", "Version"),
)


def public_workflow():
    return (Path(__file__).parent.parent / "workflows" / "ci.yml").read_text(encoding="utf-8")


def load_program(workflow=None):
    """Extract only the marked public module; reject import-time observations."""
    if workflow is None:
        workflow = public_workflow()
    lines = workflow.splitlines()
    begin = "          # BEGIN ARM_ENGINE_METADATA_PROGRAM"
    end = "          # END ARM_ENGINE_METADATA_PROGRAM"
    if lines.count(begin) != 1 or lines.count(end) != 1:
        raise AssertionError("collector markers")
    source = textwrap.dedent("\n".join(lines[lines.index(begin) + 1:lines.index(end)]))
    tree = ast.parse(source, filename="<public-arm-engine-metadata>")
    imports = {"json", "os", "re", "selectors", "signal", "stat", "subprocess", "sys", "time"}
    for node in tree.body:
        if isinstance(node, ast.Import):
            if any(alias.name not in imports or alias.asname is not None for alias in node.names):
                raise AssertionError("collector imports")
        elif isinstance(node, ast.Assign):
            ast.literal_eval(node.value)
            if any(not isinstance(target, ast.Name) for target in node.targets):
                raise AssertionError("collector assignment")
        elif isinstance(node, ast.FunctionDef):
            if node.decorator_list or node.returns is not None:
                raise AssertionError("collector definition effects")
            arguments = (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
            if any(argument.annotation is not None for argument in arguments):
                raise AssertionError("collector annotation effects")
            for default in (*node.args.defaults, *node.args.kw_defaults):
                if default is not None:
                    ast.literal_eval(default)
        elif isinstance(node, ast.If):
            guard = node.test
            if not (isinstance(guard, ast.Compare) and isinstance(guard.left, ast.Name)
                    and guard.left.id == "__name__" and len(guard.ops) == 1
                    and isinstance(guard.ops[0], ast.Eq) and len(guard.comparators) == 1
                    and isinstance(guard.comparators[0], ast.Constant)
                    and guard.comparators[0].value == "__main__" and not node.orelse):
                raise AssertionError("collector main guard")
        else:
            raise AssertionError("collector import-time statement")
    module = types.ModuleType("arm_engine_metadata_fixture")
    exec(compile(tree, "<public-arm-engine-metadata>", "exec"), module.__dict__)
    module._fixture_main_guard = tree.body[-1]
    return module


def publication_decision(workflow, child_status, control_failure=None):
    """Pure decision model bound to the exact small shell wrapper, never run Bash."""
    prefix = workflow.split("          # BEGIN ARM_ENGINE_METADATA_PROGRAM\n", 1)[0]
    prefix = prefix[prefix.rindex("      - name: Closed local rootless engine metadata"):]
    expected_prefix = (
        "      - name: Closed local rootless engine metadata only (VM teardown pending)\n"
        "        id: engine_metadata\n"
        "        shell: bash\n"
        "        run: |\n"
        "          umask 077\n"
        "          metadata_status=0\n"
        "          /usr/bin/python3 -I -S -B - <<'PY_ARM_ENGINE_METADATA' || metadata_status=$?\n"
    )
    suffix = workflow.split("          # END ARM_ENGINE_METADATA_PROGRAM\n", 1)[1]
    suffix = suffix.split("      - name: Retain only the safe metadata JSON if present\n", 1)[0]
    expected_suffix = (
        "          PY_ARM_ENGINE_METADATA\n"
        "          case \"$metadata_status\" in\n"
        "            0|10)\n"
        "              printf '%s\\n' 'metadata_complete=true' 2>/dev/null >>\"$GITHUB_OUTPUT\"\n"
        "              ;;\n"
        "          esac\n"
        "          exit \"$metadata_status\"\n"
    )
    gate = "        if: ${{ always() && steps.engine_metadata.outputs.metadata_complete == 'true' }}\n"
    if (prefix != expected_prefix or suffix != expected_suffix or gate not in workflow
            or workflow.count("metadata_complete=true") != 1
            or workflow.count("metadata_status=$?") != 1):
        raise AssertionError("publication wrapper changed")
    if child_status not in (0, 10):
        return child_status, False
    if control_failure == "before_output":
        return 1, False
    if control_failure == "after_output":
        return 1, True
    if control_failure is not None:
        raise AssertionError("publication fixture control failure")
    return child_status, True


def status_text(**overrides):
    fields = {
        "Uid": "1001 1001 1001 1001", "CapEff": "0000000000000000",
        "CapPrm": "0000000000000000", "CapInh": "0000000000000000",
        "CapAmb": "0000000000000000", "CapBnd": "ffffffffffffffff", "NoNewPrivs": "0",
    }
    fields.update(overrides)
    return "\n".join(key + ":\t" + value for key, value in fields.items() if value is not None)


def elf_header(bits=64, endian="little", machine=183, elf_type=3, ident_version=1, version=1, size=None):
    header = bytearray(64)
    header[:4] = b"\x7fELF"
    header[4:7] = bytes((2 if bits == 64 else 1, 1 if endian == "little" else 2, ident_version))
    header[16:18] = elf_type.to_bytes(2, endian)
    header[18:20] = machine.to_bytes(2, endian)
    header[20:24] = version.to_bytes(4, endian)
    offset, expected = (52, 64) if bits == 64 else (40, 52)
    header[offset:offset + 2] = (expected if size is None else size).to_bytes(2, endian)
    return bytes(header)


def metadata(mode, uid, inode=1):
    return types.SimpleNamespace(st_mode=mode, st_uid=uid, st_gid=uid, st_ino=inode,
                                 st_dev=7, st_nlink=1, st_size=32,
                                 st_mtime_ns=100, st_ctime_ns=100)


def change_field(document, path, value=None, delete=False):
    parent = document
    for key in path[:-1]:
        parent = parent[key]
    if delete:
        del parent[path[-1]]
    else:
        parent[path[-1]] = value


class FakeStream:
    def __init__(self, descriptor):
        self.descriptor = descriptor
        self.close = mock.Mock()

    def fileno(self):
        return self.descriptor


class FakeProcess:
    def __init__(self):
        self.stdout, self.stderr = FakeStream(101), FakeStream(102)
        self.poll = mock.Mock(return_value=0)
        for name in ("kill", "terminate", "wait", "communicate", "send_signal"):
            setattr(self, name, mock.Mock(side_effect=AssertionError(PRIVATE)))


class FakeSelector:
    def __init__(self):
        self.keys = {}
        self.close = mock.Mock()

    def register(self, stream, events, data):
        self.keys[stream.fileno()] = types.SimpleNamespace(
            fd=stream.fileno(), fileobj=stream, data=data, events=events)

    def unregister(self, stream):
        del self.keys[stream.fileno()]

    def get_map(self):
        return self.keys

    def select(self, unused_timeout):
        return [(key, selectors.EVENT_READ) for key in list(self.keys.values())]


class FakeMetadataFile:
    """One retained in-memory artifact file, including failed writes/closes."""
    def __init__(self, content=None, write_failure=None, close_failure=False):
        self.content = content
        self.write_failure = write_failure
        self.close_failure = close_failure
        self.close_attempts = 0
        self.closed = False
        self.open = mock.Mock(side_effect=self.open_file)
        self.fdopen = mock.Mock(return_value=self)
        self.write = mock.Mock(side_effect=self.write_payload)

    def open_file(self, unused_path, flags, unused_mode):
        if not flags & os.O_EXCL:
            raise AssertionError("exclusive metadata creation required")
        if self.content is not None:
            raise FileExistsError(PRIVATE)
        self.content = ""
        return 501

    def write_payload(self, payload):
        if self.write_failure is not None:
            partial = payload[:len(payload) // 2]
            self.content += partial
            if self.write_failure == "raise":
                raise OSError(PRIVATE)
            if self.write_failure == "short":
                return len(partial)
            raise AssertionError("publication fixture write failure")
        self.content += payload
        return len(payload)

    def __enter__(self):
        return self

    def __exit__(self, unused_type, unused_value, unused_traceback):
        self.close_attempts += 1
        if self.close_failure:
            raise OSError(PRIVATE)
        self.closed = True
        return False


class FixtureHost:
    """Synthetic observation boundary, never a wrapper around a real command."""
    def __init__(self, program):
        self.p = program
        self.uid = 1001
        self.base = "/fixture/runner-temp"
        self.paths = program.owned_paths(self.base)
        self.environment = {
            "GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted",
            "RUNNER_TEMP": self.base, "HOME": "/private/ambient-home",
            "PATH": "/private/ambient-bin", "XDG_RUNTIME_DIR": "/private/user-bus",
            "DBUS_SESSION_BUS_ADDRESS": PRIVATE, "CONTAINERS_CONF": PRIVATE,
            "CONTAINERS_STORAGE_CONF": PRIVATE, "REGISTRY_AUTH_FILE": PRIVATE,
            "DOCKER_CONFIG": PRIVATE, "CONTAINER_SSHKEY": PRIVATE,
            "TMPDIR": "/private/ambient-tmp", "LANG": PRIVATE, "UNRELATED": PRIVATE,
        }
        self.fs = {
            "/": metadata(stat.S_IFDIR | 0o755, 0, 1),
            "/usr": metadata(stat.S_IFDIR | 0o755, 0, 2),
            "/usr/bin": metadata(stat.S_IFDIR | 0o755, 0, 3),
            program.TOOL: metadata(stat.S_IFREG | 0o755, 0, 4),
            self.base: metadata(stat.S_IFDIR | 0o755, self.uid, 5),
        }
        self.created = []
        self.events = []
        self.process = FakeProcess()
        self.selector = FakeSelector()
        self.info = {
            "host": {"os": "linux", "arch": "arm64", "security": {"rootless": True},
                     "serviceIsRemote": False, "cgroupManager": "cgroupfs", "eventLogger": "none"},
            "store": {"graphDriverName": "vfs", "graphRoot": self.paths["graphroot"],
                      "runRoot": self.paths["runroot"], "volumePath": self.paths["graphroot"] + "/volumes",
                      "imageCopyTmpDir": self.paths["tmp"]},
            "version": {"Version": "4.9.3"},
            "unreported": {"registry": PRIVATE, "auth": PRIVATE, "error": PRIVATE},
        }
        self.chunks = {101: [self.payload(), b""], 102: [PRIVATE.encode("ascii"), b""]}
        self.bytes_read = 0
        self.lstat = mock.Mock(side_effect=self.stat_path)
        self.realpath = mock.Mock(side_effect=lambda path: path)
        self.mkdir = mock.Mock(side_effect=self.mkdir_path)
        self.launch = mock.Mock(side_effect=self.spawn)
        self.read = mock.Mock(side_effect=self.read_pipe)
        self.clock = mock.Mock(return_value=0.0)
        self.pidfd = mock.Mock(return_value=71)
        self.close = mock.Mock()
        self.status = mock.Mock(return_value=status_text())
        self.header = mock.Mock(return_value=elf_header())
        self.uname = mock.Mock(return_value=types.SimpleNamespace(machine="aarch64"))
        self.resuid = mock.Mock(return_value=(1001, 1001, 1001))
        self.sigchld = mock.Mock(return_value=signal.SIG_DFL)
        self.runtime = mock.Mock(return_value=True)
        self.set_blocking = mock.Mock()
        self.selector_factory = mock.Mock(return_value=self.selector)
        self.forbidden = []

    def payload(self):
        return json.dumps(self.info, separators=(",", ":")).encode("utf-8")

    def refresh_payload(self):
        self.chunks[101] = [self.payload(), b""]

    def stat_path(self, path):
        self.events.append(("lstat", path))
        if path not in self.fs:
            raise FileNotFoundError(PRIVATE)
        return self.fs[path]

    def mkdir_path(self, path, mode):
        self.events.append(("mkdir", path))
        if path in self.fs:
            raise FileExistsError(PRIVATE)
        self.fs[path] = metadata(stat.S_IFDIR | mode, self.uid, 100 + len(self.created))
        self.created.append(path)

    def spawn(self, *unused_args, **unused_kwargs):
        self.events.append(("spawn",))
        return self.process

    def read_pipe(self, descriptor, limit):
        chunk = self.chunks[descriptor].pop(0)
        if len(chunk) > limit:
            self.chunks[descriptor].insert(0, chunk[limit:])
            chunk = chunk[:limit]
        self.bytes_read += len(chunk)
        return chunk

    def assert_retained(self):
        for operation in self.forbidden:
            operation.assert_not_called()
        for name in ("kill", "terminate", "wait", "communicate", "send_signal"):
            getattr(self.process, name).assert_not_called()
        if not all(path in self.fs for path in self.created):
            raise AssertionError("owned tree was removed")

    @contextlib.contextmanager
    def mocked(self):
        with contextlib.ExitStack() as stack:
            bindings = (
                (self.p.os, "environ", self.environment),
                (self.p, "runtime_admitted", self.runtime),
                (self.p, "read_small_ascii", self.status),
                (self.p, "read_interpreter_header", self.header),
                (self.p.os, "getresuid", self.resuid),
                (self.p.os, "getuid", mock.Mock(return_value=self.uid)),
                (self.p.os, "uname", self.uname),
                (self.p.signal, "getsignal", self.sigchld),
                (self.p.os, "getpid", mock.Mock(return_value=123)),
                (self.p.os, "pidfd_open", self.pidfd),
                (self.p.os, "close", self.close),
                (self.p.os, "lstat", self.lstat),
                (self.p.os.path, "realpath", self.realpath),
                (self.p.os, "mkdir", self.mkdir),
                (self.p.subprocess, "Popen", self.launch),
                (self.p.os, "read", self.read),
                (self.p.os, "set_blocking", self.set_blocking),
                (self.p.time, "monotonic", self.clock),
                (self.p.selectors, "DefaultSelector", self.selector_factory),
            )
            for target, name, value in bindings:
                stack.enter_context(mock.patch.object(target, name, value))
            for target, names in (
                (self.p.os, ("open", "stat", "remove", "unlink", "rmdir", "kill", "killpg", "system")),
                (self.p.subprocess, ("run", "call", "check_call", "check_output")),
            ):
                for name in names:
                    operation = mock.Mock(side_effect=AssertionError(PRIVATE))
                    stack.enter_context(mock.patch.object(target, name, operation))
                    self.forbidden.append(operation)
            opened = stack.enter_context(mock.patch("builtins.open", side_effect=AssertionError(PRIVATE)))
            self.forbidden.append(opened)
            try:
                yield self
            finally:
                self.assert_retained()


class ArmEngineMetadataFixtures(unittest.TestCase):
    def setUp(self):
        self.p = load_program()

    def assert_safe(self, data):
        self.assertEqual(set(data), FIELDS)
        self.assertIn(data["outcome"], self.p.OUTCOMES)
        self.assertIn(data["native_arch"], (None, "arm64"))
        for key in ("rootless", "owned_store_match", "localness"):
            self.assertTrue(data[key] is None or type(data[key]) is bool)
        self.assertTrue(data["version"] is None or re.fullmatch(
            r"[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}", data["version"]))
        serialized = self.p.safe_json(data)
        self.assertNotIn(PRIVATE, serialized)
        self.assertNotIn("/fixture/", serialized)
        self.assertNotIn("/private/", serialized)

    def assert_schema_rejected(self, payload, paths):
        with self.assertRaises((ValueError, KeyError, TypeError, UnicodeError, RecursionError)):
            self.p.validate_info(payload, paths)

    def assert_observers_closed(self, host):
        host.process.stdout.close.assert_called_once_with()
        host.process.stderr.close.assert_called_once_with()
        host.selector.close.assert_called_once_with()

    def test_module_load_has_no_observation_side_effects(self):
        workflow = public_workflow()
        with mock.patch("builtins.open", side_effect=AssertionError(PRIVATE)) as opened, \
                mock.patch.object(os, "lstat", side_effect=AssertionError(PRIVATE)) as lstat_call, \
                mock.patch.object(os, "mkdir", side_effect=AssertionError(PRIVATE)) as mkdir, \
                mock.patch.object(os, "pidfd_open", side_effect=AssertionError(PRIVATE)) as pidfd, \
                mock.patch.object(subprocess, "Popen", side_effect=AssertionError(PRIVATE)) as launch:
            program = load_program(workflow)
        for operation in (opened, lstat_call, mkdir, pidfd, launch):
            operation.assert_not_called()
        self.assertTrue(callable(program.collect_metadata))

    def test_workflow_is_manual_pinned_and_metadata_only(self):
        workflow = public_workflow()
        self.assertIn("NEVER MERGE", workflow)
        self.assertRegex(workflow, r"(?m)^on:\n  workflow_dispatch:\n\npermissions:\n  contents: read$")
        self.assertNotRegex(workflow, r"(?m)^  (push|pull_request|schedule|inputs):")
        self.assertEqual(workflow.count("runs-on:"), 1)
        self.assertIn("runs-on: ubuntu-24.04-arm", workflow)
        self.assertIn("timeout-minutes: 10", workflow)
        self.assertIn("group: charmer-arm-engine-metadata-210\n  cancel-in-progress: false", workflow)
        self.assertIn("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1", workflow)
        self.assertIn("ref: 81a266801b11569e6e8873ddebed16eb1d162356", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn('git rev-parse --verify HEAD', workflow)
        self.assertIn('"$product_source_sha" != 81a266801b11569e6e8873ddebed16eb1d162356', workflow)
        self.assertIn('"$product_source_sha" "$GITHUB_SHA"', workflow)
        self.assertNotRegex(workflow, r"(?m)^\s*(export )?GITHUB_SHA=")
        self.assertIn("/usr/bin/python3 -I -S -B - <<'PY_ARM_ENGINE_METADATA'", workflow)
        self.assertIn("actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9", workflow)
        self.assertIn("if: ${{ always() && steps.engine_metadata.outputs.metadata_complete == 'true' }}", workflow)
        self.assertEqual(publication_decision(workflow, 0), (0, True))
        self.assertEqual(publication_decision(workflow, 10), (10, True))
        self.assertEqual(publication_decision(workflow, 20), (20, False))
        self.assertIn("path: ${{ runner.temp }}/arm-engine-metadata-210.json", workflow)
        self.assertIn("retention-days: 3\n          if-no-files-found: ignore", workflow)
        uses = re.findall(r"(?m)^\s*(?:- )?uses: (.+)$", workflow)
        self.assertEqual(len(uses), 2)
        for forbidden in ("sudo ", "apt-get", "setup-python@", "curl ", "unshare ",
                          "sysctl ", "binfmt", "tools/test.py", "tools/build.py", "TemporaryDirectory"):
            self.assertNotIn(forbidden, workflow)

    def test_valid_status_and_elf_predicates(self):
        parsed = self.p.parse_status(status_text())
        self.assertEqual(parsed["Uid"], [1001] * 4)
        self.assertEqual(parsed["CapEff"], 0)
        self.assertEqual(parsed["CapBnd"], 2**64 - 1)
        self.assertEqual(parsed["NoNewPrivs"], 0)
        for elf_type in (2, 3):
            self.assertEqual(self.p.parse_elf(elf_header(elf_type=elf_type)), {
                "class_bits": 64, "endianness": "little", "e_machine": 183})
        self.assertEqual(self.p.parse_elf(elf_header(bits=32, endian="big", machine=40)), {
            "class_bits": 32, "endianness": "big", "e_machine": 40})

    def test_status_invalid_or_duplicate_required_fields_refuse(self):
        for text in (None, b"Uid: 1001", "x" * 16385):
            self.assertTrue(all(value is None for value in self.p.parse_status(text).values()))
        cases = [status_text(Uid=value) for value in (
            "", "1001 1001 1001", "1001 1001 1001 1001 1001", "-1 1 1 1",
            "4294967296 4294967296 4294967296 4294967296", "x x x x")]
        for name in ("Uid", "CapEff", "CapPrm", "CapInh", "CapAmb"):
            cases.extend((status_text(**{name: None}), status_text() + "\n" + name + ": 0"))
        for value in ("0", "00000000000000000", "gggggggggggggggg", "-000000000000001"):
            cases.append(status_text(CapEff=value))
        for text in cases:
            with self.subTest(text=text), FixtureHost(self.p).mocked() as host:
                host.status.return_value = text
                self.assertIsNone(self.p.caller_uid())
                host.mkdir.assert_not_called()
                host.launch.assert_not_called()

    def test_elf_type_version_size_and_architecture_refuse(self):
        corrupt = []
        for offset, value in ((0, 0), (4, 0), (5, 0), (6, 0)):
            header = bytearray(elf_header())
            header[offset] = value
            corrupt.append(bytes(header))
        corrupt.extend((None, b"", elf_header()[:63], elf_header(elf_type=1),
                        elf_header(version=0), elf_header(ident_version=2), elf_header(size=52)))
        for header in corrupt:
            with self.subTest(header=header):
                self.assertTrue(all(value is None for value in self.p.parse_elf(header).values()))
        for header in (*corrupt, elf_header(bits=32), elf_header(endian="big"), elf_header(machine=62)):
            with self.subTest(header=header), FixtureHost(self.p).mocked() as host:
                host.header.return_value = header
                self.assertEqual(self.p.collect_metadata()["outcome"], "HELD_ADMISSION")
                host.mkdir.assert_not_called()
                host.launch.assert_not_called()

    def test_runtime_linux_isolated_no_site_no_bytecode_required(self):
        flags = {name: 1 for name in ("isolated", "ignore_environment", "no_site",
                                     "no_user_site", "dont_write_bytecode")}
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.object(sys, "flags", types.SimpleNamespace(**flags)), \
                mock.patch.object(sys, "dont_write_bytecode", True):
            self.assertTrue(self.p.runtime_admitted())
            for name in flags:
                with self.subTest(flag=name), mock.patch.object(sys.flags, name, 0):
                    self.assertFalse(self.p.runtime_admitted())
            with mock.patch.object(sys, "dont_write_bytecode", False):
                self.assertFalse(self.p.runtime_admitted())
            for platform in ("darwin", "win32"):
                with mock.patch.object(sys, "platform", platform):
                    self.assertFalse(self.p.runtime_admitted())

    def test_caller_uid_caps_signal_and_kernel_admissions(self):
        cases = (
            ("resuid", (0, 0, 0)), ("resuid", (1001, 1002, 1001)),
            ("resuid", (True, True, True)), ("status", status_text(Uid="1001 1001 1001 1002")),
            ("status", status_text(Uid="1002 1002 1002 1002")),
            *(("status", status_text(**{name: "0000000000000001"})) for name in
              ("CapEff", "CapPrm", "CapInh", "CapAmb")),
            ("sigchld", signal.SIG_IGN),
            *(("uname", types.SimpleNamespace(machine=machine)) for machine in
              ("arm64", "x86_64", "i686", PRIVATE)),
        )
        for boundary, value in cases:
            with self.subTest(boundary=boundary, value=value), FixtureHost(self.p).mocked() as host:
                getattr(host, boundary).return_value = value
                self.assertEqual(self.p.collect_metadata()["outcome"], "HELD_ADMISSION")
                host.mkdir.assert_not_called()
                host.launch.assert_not_called()

    def test_bounding_caps_and_nnp_are_not_admission_requirements(self):
        for bounding in (None, "ffffffffffffffff", "0000000000000000", "malformed"):
            for nnp in (None, "0", "1", "malformed"):
                with self.subTest(bounding=bounding, nnp=nnp), FixtureHost(self.p).mocked() as host:
                    host.status.return_value = status_text(CapBnd=bounding, NoNewPrivs=nnp)
                    self.assertEqual(self.p.collect_metadata()["outcome"], self.p.SUCCESS)

    def test_self_pidfd_open_close_and_failures(self):
        with FixtureHost(self.p).mocked() as host:
            self.assertTrue(self.p.self_pidfd_admitted())
            host.pidfd.assert_called_once_with(123, 0)
            host.close.assert_called_once_with(71)
        for error in (OSError(PRIVATE), NotImplementedError(PRIVATE), AttributeError(PRIVATE)):
            with self.subTest(error=type(error)), FixtureHost(self.p).mocked() as host:
                host.pidfd.side_effect = error
                self.assertEqual(self.p.collect_metadata()["outcome"], "HELD_ADMISSION")
                host.close.assert_not_called()
                host.mkdir.assert_not_called()
                host.launch.assert_not_called()
        with FixtureHost(self.p).mocked() as host:
            host.close.side_effect = OSError(PRIVATE)
            self.assertEqual(self.p.collect_metadata()["outcome"], "HELD_ADMISSION")
            host.close.assert_called_once_with(71)
            host.mkdir.assert_not_called()
            host.launch.assert_not_called()

    def test_empty_remote_marker_refuses_before_observers_or_effects(self):
        for marker in self.p.REMOTE_MARKERS:
            for value in ("", PRIVATE):
                with self.subTest(marker=marker, value=value), FixtureHost(self.p).mocked() as host:
                    host.environment[marker] = value
                    data = self.p.collect_metadata()
                    self.assertEqual(data["outcome"], "HELD_ADMISSION")
                    self.assert_safe(data)
                    for operation in (host.runtime, host.status, host.pidfd, host.lstat,
                                      host.mkdir, host.launch):
                        operation.assert_not_called()

    def test_hosted_execution_identity_admitted_before_any_effects(self):
        for marker, values in (("GITHUB_ACTIONS", (None, "", "false", "True", PRIVATE)),
                               ("RUNNER_ENVIRONMENT", (None, "", "self-hosted", "github-Hosted", PRIVATE))):
            for value in values:
                with self.subTest(marker=marker, value=value), FixtureHost(self.p).mocked() as host:
                    if value is None:
                        del host.environment[marker]
                    else:
                        host.environment[marker] = value
                    data = self.p.collect_metadata()
                    self.assertEqual(data["outcome"], "HELD_ADMISSION")
                    self.assert_safe(data)
                    for operation in (host.runtime, host.status, host.pidfd, host.lstat,
                                      host.mkdir, host.launch):
                        operation.assert_not_called()
                    with mock.patch.object(self.p, "write_metadata"), \
                            mock.patch.object(sys, "stdout", io.StringIO()) as output:
                        self.assertEqual(self.p.main(), 10)
                    self.assertEqual(json.loads(output.getvalue())["outcome"], "HELD_ADMISSION")

    def test_all_initial_admissions_precede_mkdir_and_spawn(self):
        for boundary in ("runtime", "status", "header", "uname", "resuid", "sigchld"):
            with self.subTest(boundary=boundary), FixtureHost(self.p).mocked() as host:
                getattr(host, boundary).side_effect = RuntimeError(PRIVATE)
                self.assertEqual(self.p.collect_metadata()["outcome"], "HELD_ADMISSION")
                host.mkdir.assert_not_called()
                host.launch.assert_not_called()
        with FixtureHost(self.p).mocked() as host:
            self.assertEqual(self.p.collect_metadata()["outcome"], self.p.SUCCESS)
            host.close.assert_called_once_with(71)
            self.assertLess(host.events.index(("lstat", self.p.TOOL)),
                            host.events.index(("mkdir", host.paths["tree"])))

    def test_guard_markers_preserved_verbatim_in_fixed_environment(self):
        with FixtureHost(self.p).mocked() as host:
            values = ("", PRIVATE, "session-fixture", "000123", "http://fixture.invalid:1")
            host.environment.update(zip(self.p.GUARD_MARKERS, values))
            before = dict(host.environment)
            data = self.p.collect_metadata()
            self.assertEqual(data["outcome"], self.p.SUCCESS)
            environment = host.launch.call_args.kwargs["env"]
            expected = {
                "HOME": host.paths["home"], "XDG_CONFIG_HOME": host.paths["config"],
                "XDG_DATA_HOME": host.paths["data"], "XDG_CACHE_HOME": host.paths["cache"],
                "XDG_RUNTIME_DIR": host.paths["runtime"], "TMPDIR": host.paths["tmp"],
                "PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C",
            }
            expected.update(zip(self.p.GUARD_MARKERS, values))
            self.assertEqual(environment, expected)
            self.assertEqual(host.environment, before)
            self.assert_safe(data)

    def test_absent_guard_markers_not_invented(self):
        with FixtureHost(self.p).mocked() as host:
            self.p.collect_metadata()
            environment = host.launch.call_args.kwargs["env"]
            self.assertFalse(set(self.p.GUARD_MARKERS) & set(environment))
            self.assertFalse(set(self.p.REMOTE_MARKERS) & set(environment))
            self.assertEqual(len(environment), 9)

    def test_tool_file_metadata_refusals(self):
        for mode, uid in ((stat.S_IFLNK | 0o755, 0), (stat.S_IFDIR | 0o755, 0),
                          (stat.S_IFREG | 0o755, 1001), (stat.S_IFREG | 0o777, 0),
                          (stat.S_IFREG | 0o4755, 0), (stat.S_IFREG | 0o555, 0)):
            with self.subTest(mode=mode, uid=uid), FixtureHost(self.p).mocked() as host:
                host.fs[self.p.TOOL] = metadata(mode, uid)
                self.assertEqual(self.p.collect_metadata()["outcome"], "HELD_TOOL_PATH")
                host.mkdir.assert_not_called()
                host.launch.assert_not_called()
        with FixtureHost(self.p).mocked() as host:
            del host.fs[self.p.TOOL]
            self.assertEqual(self.p.collect_metadata()["outcome"], "HELD_TOOL_PATH")
            host.mkdir.assert_not_called()
            host.launch.assert_not_called()

    def test_tool_ancestor_metadata_refusals(self):
        for path in ("/", "/usr", "/usr/bin"):
            for mode, uid in ((stat.S_IFLNK | 0o755, 0), (stat.S_IFREG | 0o755, 0),
                              (stat.S_IFDIR | 0o775, 0), (stat.S_IFDIR | 0o757, 0),
                              (stat.S_IFDIR | 0o755, 1001)):
                with self.subTest(path=path, mode=mode, uid=uid), FixtureHost(self.p).mocked() as host:
                    host.fs[path] = metadata(mode, uid)
                    self.assertEqual(self.p.collect_metadata()["outcome"], "HELD_TOOL_PATH")
                    host.mkdir.assert_not_called()
                    host.launch.assert_not_called()

    def test_tool_revalidation_refuses_changed_metadata(self):
        for path, attribute in ((self.p.TOOL, "st_ino"), (self.p.TOOL, "st_mode"),
                                (self.p.TOOL, "st_uid"), (self.p.TOOL, "st_size"),
                                (self.p.TOOL, "st_mtime_ns"), ("/usr/bin", "st_ctime_ns")):
            with self.subTest(path=path, attribute=attribute), FixtureHost(self.p).mocked() as host:
                def create(directory, mode):
                    host.mkdir_path(directory, mode)
                    if directory == host.paths["podman-tmp"]:
                        info = host.fs[path]
                        setattr(info, attribute, getattr(info, attribute) + 1)
                host.mkdir.side_effect = create
                self.assertEqual(self.p.collect_metadata()["outcome"], "HELD_TOOL_PATH")
                self.assertEqual(len(host.created), 10)
                host.launch.assert_not_called()

    def test_owned_runner_temp_refusals(self):
        for directory in (None, "", "relative", "/", "/fixture/../runner-temp", "/fixture/runner-temp/"):
            with self.subTest(directory=directory), FixtureHost(self.p).mocked() as host:
                if directory is None:
                    del host.environment["RUNNER_TEMP"]
                else:
                    host.environment["RUNNER_TEMP"] = directory
                self.assertEqual(self.p.collect_metadata()["outcome"], "HELD_OWNED_DIR")
                host.mkdir.assert_not_called()
                host.launch.assert_not_called()
        for mode, uid in ((stat.S_IFLNK | 0o700, 1001), (stat.S_IFREG | 0o700, 1001),
                          (stat.S_IFDIR | 0o700, 0), (stat.S_IFDIR | 0o777, 1001)):
            with self.subTest(mode=mode, uid=uid), FixtureHost(self.p).mocked() as host:
                host.fs[host.base] = metadata(mode, uid)
                self.assertEqual(self.p.collect_metadata()["outcome"], "HELD_OWNED_DIR")
                host.mkdir.assert_not_called()
                host.launch.assert_not_called()
        with FixtureHost(self.p).mocked() as host:
            host.realpath.side_effect = lambda path: "/fixture/other" if path == host.base else path
            self.assertEqual(self.p.collect_metadata()["outcome"], "HELD_OWNED_DIR")
            host.mkdir.assert_not_called()
            host.launch.assert_not_called()

    def test_owned_tree_is_fresh_private_and_complete(self):
        with FixtureHost(self.p).mocked() as host:
            data = self.p.collect_metadata()
            self.assertEqual(data["outcome"], self.p.SUCCESS)
            expected = [host.paths["tree"], *(host.paths[name] for name in self.p.DIRECTORY_NAMES)]
            self.assertEqual(host.created, expected)
            self.assertEqual(host.mkdir.call_args_list, [mock.call(path, 0o700) for path in expected])
            for path in expected:
                self.assertEqual(host.fs[path].st_uid, host.uid)
                self.assertEqual(stat.S_IMODE(host.fs[path].st_mode), 0o700)
            self.assert_safe(data)

    def test_owned_dir_validation_refusals(self):
        for mode, uid in ((stat.S_IFLNK | 0o700, 1001), (stat.S_IFREG | 0o700, 1001),
                          (stat.S_IFDIR | 0o700, 0), (stat.S_IFDIR | 0o755, 1001),
                          (stat.S_IFDIR | 0o1700, 1001)):
            with self.subTest(mode=mode, uid=uid), FixtureHost(self.p).mocked() as host:
                def create(path, requested_mode):
                    host.mkdir_path(path, requested_mode)
                    if path == host.paths["runtime"]:
                        host.fs[path] = metadata(mode, uid)
                host.mkdir.side_effect = create
                self.assertEqual(self.p.collect_metadata()["outcome"], "HELD_OWNED_DIR")
                self.assertEqual(len(host.created), 10)
                host.launch.assert_not_called()
        with FixtureHost(self.p).mocked() as host:
            host.realpath.side_effect = lambda path: "/elsewhere" if path == host.paths["home"] else path
            self.assertEqual(self.p.collect_metadata()["outcome"], "HELD_OWNED_DIR")
            host.launch.assert_not_called()
        with FixtureHost(self.p).mocked() as host:
            paths = self.p.create_owned_paths(host.base, host.uid)
            paths["graphroot"] = "/fixture/not-owned"
            with self.assertRaises(ValueError):
                self.p.validate_owned_paths(paths, host.uid)

    def test_existing_or_partial_tree_is_retained(self):
        with FixtureHost(self.p).mocked() as host:
            existing = metadata(stat.S_IFDIR | 0o700, host.uid, 99)
            host.fs[host.paths["tree"]] = existing
            self.assertEqual(self.p.collect_metadata()["outcome"], "HELD_OWNED_DIR")
            self.assertIs(host.fs[host.paths["tree"]], existing)
            self.assertEqual(host.created, [])
            host.launch.assert_not_called()
        with FixtureHost(self.p).mocked() as host:
            def fail_partway(path, mode):
                if path == host.paths["cache"]:
                    raise OSError(PRIVATE)
                host.mkdir_path(path, mode)
            host.mkdir.side_effect = fail_partway
            self.assertEqual(self.p.collect_metadata()["outcome"], "HELD_OWNED_DIR")
            self.assertEqual(len(host.created), 4)
            host.launch.assert_not_called()

    def test_exactly_one_fixed_engine_invocation(self):
        with FixtureHost(self.p).mocked() as host:
            self.assertEqual(self.p.collect_metadata()["outcome"], self.p.SUCCESS)
            environment = self.p.engine_environment(host.paths, host.environment)
            host.launch.assert_called_once_with(
                ["/usr/bin/podman", "--remote=false", "--log-level=error",
                 "--root=" + host.paths["graphroot"], "--runroot=" + host.paths["runroot"],
                 "--tmpdir=" + host.paths["podman-tmp"], "--storage-driver=vfs",
                 "--cgroup-manager=cgroupfs", "--events-backend=none", "info", "--format=json"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=environment, cwd=host.paths["home"], shell=False, close_fds=True, restore_signals=True)
            self.assertEqual(host.events[-5:], [("lstat", "/"), ("lstat", "/usr"),
                                                ("lstat", "/usr/bin"), ("lstat", self.p.TOOL), ("spawn",)])
            self.assert_observers_closed(host)
            self.assertEqual(host.set_blocking.call_args_list, [mock.call(101, False), mock.call(102, False)])

    def test_valid_info_and_no_version_floor(self):
        host = FixtureHost(self.p)
        for version in ("0.0.0", "1.0.0", "4.9.3", "123456.123456.123456"):
            with self.subTest(version=version):
                host.info["version"]["Version"] = version
                data = self.p.validate_info(host.payload(), host.paths)
                self.assertEqual(data, {"version": version, "rootless": True, "owned_store_match": True,
                                        "localness": True, "outcome": self.p.SUCCESS})

    def test_duplicate_keys_at_each_level_rejected(self):
        host = FixtureHost(self.p)
        text = host.payload().decode("utf-8")
        for key in ("host", "store", "version", "security", "os", "arch", "rootless",
                    "serviceIsRemote", "cgroupManager", "eventLogger", "graphDriverName",
                    "graphRoot", "runRoot", "volumePath", "imageCopyTmpDir", "Version"):
            with self.subTest(key=key):
                prefix = json.dumps(key) + ":"
                duplicate = text.replace(prefix, prefix + "null," + prefix, 1)
                self.assert_schema_rejected(duplicate.encode("utf-8"), host.paths)
        self.assert_schema_rejected((text[:-1] + ',"extra":{"x":0,"x":1}}').encode("utf-8"), host.paths)

    def test_missing_required_info_fields(self):
        host = FixtureHost(self.p)
        for path in (("host",), ("store",), ("version",), ("host", "security"), *REQUIRED_FIELDS):
            with self.subTest(path=path):
                info = copy.deepcopy(host.info)
                change_field(info, path, delete=True)
                self.assert_schema_rejected(json.dumps(info).encode("utf-8"), host.paths)

    def test_wrong_type_info_fields(self):
        host = FixtureHost(self.p)
        for path in REQUIRED_FIELDS:
            for value in (None, [], {}, 0, 1, True, "true"):
                if type(value) is type(self.value_at(host.info, path)):
                    continue
                with self.subTest(path=path, value=value):
                    info = copy.deepcopy(host.info)
                    change_field(info, path, value)
                    self.assert_schema_rejected(json.dumps(info).encode("utf-8"), host.paths)
        for path in (("host",), ("store",), ("version",), ("host", "security")):
            info = copy.deepcopy(host.info)
            change_field(info, path, [])
            self.assert_schema_rejected(json.dumps(info).encode("utf-8"), host.paths)

    @staticmethod
    def value_at(document, path):
        for key in path:
            document = document[key]
        return document

    def test_numeric_version_rejections(self):
        host = FixtureHost(self.p)
        for value in ("", "5", "5.0", "5.0.0.1", "v5.0.0", "5.0.0-dev", " 5.0.0", "5.0.0\n",
                      "1234567.0.0", "5.0." + "1" * 100, PRIVATE):
            with self.subTest(value=value):
                host.info["version"]["Version"] = value
                self.assert_schema_rejected(host.payload(), host.paths)

    def test_identity_localness_and_architecture_mismatches(self):
        cases = (
            (("host", "os"), "darwin"), (("host", "arch"), "aarch64"),
            (("host", "arch"), "x86_64"), (("host", "arch"), PRIVATE),
            (("host", "security", "rootless"), False), (("host", "serviceIsRemote"), True),
            (("host", "cgroupManager"), "systemd"), (("host", "eventLogger"), "journald"),
        )
        for path, value in cases:
            with self.subTest(path=path, value=value), FixtureHost(self.p).mocked() as host:
                change_field(host.info, path, value)
                host.refresh_payload()
                data = self.p.collect_metadata()
                self.assertEqual(data["outcome"], "HELD_ENGINE_IDENTITY_STORE_VM_TEARDOWN_PENDING")
                self.assertIs(data["owned_store_match"], True)
                if path == ("host", "serviceIsRemote"):
                    self.assertIs(data["localness"], False)
                if path == ("host", "security", "rootless"):
                    self.assertIs(data["rootless"], False)
                self.assert_safe(data)

    def test_owned_store_mismatches(self):
        for key in ("graphDriverName", "graphRoot", "runRoot", "volumePath", "imageCopyTmpDir"):
            with self.subTest(key=key), FixtureHost(self.p).mocked() as host:
                host.info["store"][key] += "/"
                host.refresh_payload()
                data = self.p.collect_metadata()
                self.assertEqual(data["outcome"], "HELD_ENGINE_IDENTITY_STORE_VM_TEARDOWN_PENDING")
                self.assertIs(data["owned_store_match"], False)
                self.assert_safe(data)
        with FixtureHost(self.p).mocked() as host:
            host.info["store"]["imageCopyTmpDir"] = host.paths["podman-tmp"]
            host.refresh_payload()
            self.assertEqual(self.p.collect_metadata()["outcome"],
                             "HELD_ENGINE_IDENTITY_STORE_VM_TEARDOWN_PENDING")

    def test_non_json_or_nonfinite_payload_refused(self):
        host = FixtureHost(self.p)
        for payload in (b"", b"[]", b"null", b"true", b"{}", b"\xff", PRIVATE.encode("ascii"),
                        b"{} trailing", b"x" * (self.p.MAX_OUTPUT + 1)):
            with self.subTest(payload=payload[:32]):
                self.assert_schema_rejected(payload, host.paths)
        for token in ("NaN", "Infinity", "-Infinity"):
            text = host.payload().decode("utf-8")
            self.assert_schema_rejected((text[:-1] + ',"extra":' + token + "}").encode("utf-8"), host.paths)

    def test_nonzero_retains_state_and_closes_observers(self):
        with FixtureHost(self.p).mocked() as host:
            host.process.poll.return_value = 125
            data = self.p.collect_metadata()
            self.assertEqual(data["outcome"], "HELD_ENGINE_NONZERO_VM_TEARDOWN_PENDING")
            self.assertEqual(host.launch.call_count, 1)
            self.assertEqual(len(host.created), 10)
            self.assert_observers_closed(host)
            self.assert_safe(data)

    def test_timeout_never_kills_waits_or_removes(self):
        with FixtureHost(self.p).mocked() as host:
            host.process.poll.return_value = None
            host.clock.side_effect = (0.0, self.p.ENGINE_SECONDS + 1.0)
            data = self.p.collect_metadata()
            self.assertEqual(data["outcome"], "HELD_ENGINE_TIMEOUT_VM_TEARDOWN_PENDING")
            self.assertEqual(host.launch.call_count, 1)
            self.assertIsNone(host.process.poll.return_value)
            self.assertEqual(len(host.created), 10)
            self.assert_observers_closed(host)
            self.assert_safe(data)

    def test_output_limit_bounds_both_streams_without_teardown(self):
        limit = self.p.MAX_OUTPUT
        for stdout, stderr in ((b"x" * (limit + 1), b""), (b"", b"x" * (limit + 1)),
                               (b"x" * (limit // 2), b"y" * (limit // 2 + 1))):
            with self.subTest(stdout=len(stdout), stderr=len(stderr)), FixtureHost(self.p).mocked() as host:
                host.process.poll.return_value = None
                host.chunks = {101: [stdout, b""], 102: [stderr, b""]}
                data = self.p.collect_metadata()
                self.assertEqual(data["outcome"], "HELD_ENGINE_OUTPUT_LIMIT_VM_TEARDOWN_PENDING")
                self.assertLessEqual(host.bytes_read, limit + 1)
                self.assertTrue(all(0 < call.args[1] <= 65536 for call in host.read.call_args_list))
                self.assertEqual(host.launch.call_count, 1)
                self.assert_observers_closed(host)
                self.assert_safe(data)

    def test_observer_read_or_registration_failure_closes_fds(self):
        for boundary in ("read", "set_blocking", "selector_factory", "register", "close"):
            with self.subTest(boundary=boundary), FixtureHost(self.p).mocked() as host:
                if boundary == "register":
                    host.selector.register = mock.Mock(side_effect=OSError(PRIVATE))
                elif boundary == "close":
                    host.process.stdout.close.side_effect = OSError(PRIVATE)
                else:
                    getattr(host, boundary).side_effect = OSError(PRIVATE)
                data = self.p.collect_metadata()
                self.assertEqual(data["outcome"], "HELD_ENGINE_UNAVAILABLE_VM_TEARDOWN_PENDING")
                host.process.stdout.close.assert_called_once_with()
                host.process.stderr.close.assert_called_once_with()
                if boundary != "selector_factory":
                    host.selector.close.assert_called_once_with()
                self.assert_safe(data)
        with FixtureHost(self.p).mocked() as host:
            read = host.read_pipe
            first = True
            def temporarily_blocked(descriptor, limit):
                nonlocal first
                if first:
                    first = False
                    raise BlockingIOError(PRIVATE)
                return read(descriptor, limit)
            host.read.side_effect = temporarily_blocked
            self.assertEqual(self.p.collect_metadata()["outcome"], self.p.SUCCESS)
            self.assert_observers_closed(host)

    def test_spawn_exception_does_not_disclose_private_sentinel(self):
        with FixtureHost(self.p).mocked() as host, \
                mock.patch.object(self.p, "write_metadata") as write, \
                mock.patch.object(sys, "stdout", io.StringIO()) as output:
            host.launch.side_effect = OSError(PRIVATE)
            self.assertEqual(self.p.main(), 10)
            self.assertEqual(host.launch.call_count, 1)
            data = json.loads(output.getvalue())
            self.assertEqual(data["outcome"], "HELD_ENGINE_UNAVAILABLE_VM_TEARDOWN_PENDING")
            self.assert_safe(data)
            write.assert_called_once_with(data)

    def test_schema_and_identity_results_have_only_six_safe_fields(self):
        for kind in ("schema", "identity", "valid"):
            with self.subTest(kind=kind), FixtureHost(self.p).mocked() as host:
                if kind == "schema":
                    host.chunks[101] = [(PRIVATE + " not-json").encode("ascii"), b""]
                elif kind == "identity":
                    host.info["store"]["graphRoot"] = "/private/" + PRIVATE
                    host.refresh_payload()
                data = self.p.collect_metadata()
                self.assert_safe(data)
                expected = {"schema": "HELD_ENGINE_SCHEMA_VM_TEARDOWN_PENDING",
                            "identity": "HELD_ENGINE_IDENTITY_STORE_VM_TEARDOWN_PENDING",
                            "valid": self.p.SUCCESS}[kind]
                self.assertEqual(data["outcome"], expected)

    def test_unexpected_exception_is_fixed_and_nondisclosing(self):
        with mock.patch.object(self.p, "collect_metadata", side_effect=RuntimeError(PRIVATE)), \
                mock.patch.object(self.p, "write_metadata") as write, \
                mock.patch.object(sys, "stdout", io.StringIO()) as output:
            self.assertEqual(self.p.main(), 20)
            data = json.loads(output.getvalue())
            self.assertEqual(data, self.p.result("HELD_INTERNAL_VM_TEARDOWN_PENDING"))
            self.assert_safe(data)
            write.assert_not_called()

    def test_safe_output_is_exclusive_fixed_and_six_fields(self):
        workflow = public_workflow()
        with FixtureHost(self.p).mocked() as host:
            sink = FakeMetadataFile()
            with mock.patch.object(os, "open", sink.open), \
                    mock.patch.object(os, "fdopen", sink.fdopen), \
                    mock.patch.object(sys, "stdout", io.StringIO()) as output:
                status = self.p.main()
            self.assertEqual(status, 0)
            self.assertEqual(publication_decision(workflow, status), (0, True))
            sink.open.assert_called_once_with(
                host.base + "/arm-engine-metadata-210.json",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            sink.fdopen.assert_called_once_with(501, "w", encoding="ascii")
            self.assertEqual(sink.close_attempts, 1)
            self.assertTrue(sink.closed)
            data = json.loads(sink.content)
            sink.write.assert_called_once_with(self.p.safe_json(data))
            self.assertEqual(sink.content, output.getvalue())
            self.assert_safe(data)

    def test_output_failure_is_fixed_and_retains_state(self):
        with FixtureHost(self.p).mocked() as host, \
                mock.patch.object(self.p, "write_metadata", side_effect=OSError(PRIVATE)), \
                mock.patch.object(sys, "stdout", io.StringIO()) as output:
            self.assertEqual(self.p.main(), 20)
            data = json.loads(output.getvalue())
            self.assertEqual(data["outcome"], "HELD_METADATA_OUTPUT_VM_TEARDOWN_PENDING")
            self.assertEqual(len(host.created), 10)
            self.assert_safe(data)
        with FixtureHost(self.p).mocked() as host:
            with mock.patch.object(os, "open", return_value=501), \
                    mock.patch.object(os, "fdopen", side_effect=OSError(PRIVATE)):
                with self.assertRaises(OSError):
                    self.p.write_metadata(self.p.result())
            host.close.assert_called_once_with(501)
        with FixtureHost(self.p).mocked() as host:
            with mock.patch.object(os, "open", side_effect=FileExistsError(PRIVATE)) as opened:
                with self.assertRaises(FileExistsError):
                    self.p.write_metadata(self.p.result())
            self.assertEqual(opened.call_count, 1)
            host.close.assert_not_called()

    def test_publication_collision_cannot_authorize_upload(self):
        workflow = public_workflow()
        stale_success = self.p.safe_json({
            "version": "4.9.3", "native_arch": "arm64", "rootless": True,
            "owned_store_match": True, "localness": True, "outcome": self.p.SUCCESS,
        })
        for existing in ("", PRIVATE, '{"partial":', stale_success):
            with self.subTest(existing=existing), FixtureHost(self.p).mocked() as host:
                sink = FakeMetadataFile(content=existing)
                with mock.patch.object(os, "open", sink.open), \
                        mock.patch.object(os, "fdopen", sink.fdopen), \
                        mock.patch.object(sys, "stdout", io.StringIO()) as output:
                    status = self.p.main()
                self.assertEqual(status, 20)
                self.assertEqual(publication_decision(workflow, status), (20, False))
                self.assertEqual(sink.content, existing)
                self.assertEqual(sink.close_attempts, 0)
                sink.open.assert_called_once()
                sink.fdopen.assert_not_called()
                sink.write.assert_not_called()
                data = json.loads(output.getvalue())
                self.assertEqual(data["outcome"], "HELD_METADATA_OUTPUT_VM_TEARDOWN_PENDING")
                self.assert_safe(data)
                self.assertEqual(len(host.created), 10)

    def test_publication_partial_write_cannot_authorize_upload(self):
        workflow = public_workflow()
        for failure in ("raise", "short"):
            with self.subTest(failure=failure), FixtureHost(self.p).mocked() as host:
                sink = FakeMetadataFile(write_failure=failure)
                with mock.patch.object(os, "open", sink.open), \
                        mock.patch.object(os, "fdopen", sink.fdopen), \
                        mock.patch.object(sys, "stdout", io.StringIO()) as output:
                    status = self.p.main()
                self.assertEqual(status, 20)
                self.assertEqual(publication_decision(workflow, status), (20, False))
                self.assertGreater(len(sink.content), 0)
                self.assertLess(len(sink.content), len(sink.write.call_args.args[0]))
                self.assertEqual(sink.close_attempts, 1)
                sink.write.assert_called_once()
                data = json.loads(output.getvalue())
                self.assertEqual(data["outcome"], "HELD_METADATA_OUTPUT_VM_TEARDOWN_PENDING")
                self.assert_safe(data)
                self.assertEqual(len(host.created), 10)

    def test_publication_close_error_cannot_authorize_upload(self):
        workflow = public_workflow()
        with FixtureHost(self.p).mocked() as host:
            sink = FakeMetadataFile(close_failure=True)
            with mock.patch.object(os, "open", sink.open), \
                    mock.patch.object(os, "fdopen", sink.fdopen), \
                    mock.patch.object(sys, "stdout", io.StringIO()) as output:
                status = self.p.main()
            self.assertEqual(status, 20)
            self.assertEqual(publication_decision(workflow, status), (20, False))
            self.assertEqual(sink.close_attempts, 1)
            self.assertFalse(sink.closed)
            retained = json.loads(sink.content)
            self.assertEqual(retained["outcome"], self.p.SUCCESS)
            self.assert_safe(retained)
            sink.write.assert_called_once_with(self.p.safe_json(retained))
            data = json.loads(output.getvalue())
            self.assertEqual(data["outcome"], "HELD_METADATA_OUTPUT_VM_TEARDOWN_PENDING")
            self.assert_safe(data)
            self.assertEqual(len(host.created), 10)

    def test_written_held_metadata_authorizes_upload_but_fails_job(self):
        workflow = public_workflow()
        with FixtureHost(self.p).mocked() as host:
            host.process.poll.return_value = 125
            sink = FakeMetadataFile()
            with mock.patch.object(os, "open", sink.open), \
                    mock.patch.object(os, "fdopen", sink.fdopen), \
                    mock.patch.object(sys, "stdout", io.StringIO()) as output:
                status = self.p.main()
            self.assertEqual(status, 10)
            self.assertEqual(publication_decision(workflow, status), (10, True))
            self.assertEqual(sink.close_attempts, 1)
            self.assertTrue(sink.closed)
            self.assertEqual(sink.content, output.getvalue())
            data = json.loads(sink.content)
            self.assertEqual(data["outcome"], "HELD_ENGINE_NONZERO_VM_TEARDOWN_PENDING")
            self.assert_safe(data)
            self.assertEqual(len(host.created), 10)
            self.assertEqual(host.launch.call_count, 1)

    def test_interpreter_and_early_failures_cannot_authorize_upload(self):
        workflow = public_workflow()
        for status in (1, 2, 20, 126, 127, 130, 137):
            with self.subTest(interpreter_status=status):
                self.assertEqual(publication_decision(workflow, status), (status, False))
        with FixtureHost(self.p).mocked() as host, \
                mock.patch.object(sys, "stdout", io.StringIO()) as output:
            namespace = dict(self.p.__dict__)
            namespace.update(__name__="__main__", main=mock.Mock(side_effect=RuntimeError(PRIVATE)),
                             write_metadata=mock.Mock())
            guard = ast.Module(body=[self.p._fixture_main_guard], type_ignores=[])
            with self.assertRaises(SystemExit) as stopped:
                exec(compile(guard, "<public-arm-engine-metadata-entrypoint>", "exec"), namespace)
            self.assertEqual(stopped.exception.code, 20)
            self.assertEqual(publication_decision(workflow, stopped.exception.code), (20, False))
            namespace["main"].assert_called_once_with()
            namespace["write_metadata"].assert_not_called()
            self.assertEqual(output.getvalue(), "")
            host.mkdir.assert_not_called()
            host.launch.assert_not_called()

    def test_control_channel_failure_keeps_finalized_metadata_unchanged(self):
        workflow = public_workflow()
        for held in (False, True):
            with self.subTest(held=held), FixtureHost(self.p).mocked() as host:
                if held:
                    host.process.poll.return_value = 125
                sink = FakeMetadataFile()
                with mock.patch.object(os, "open", sink.open), \
                        mock.patch.object(os, "fdopen", sink.fdopen), \
                        mock.patch.object(sys, "stdout", io.StringIO()) as output:
                    status = self.p.main()
                self.assertEqual(status, 10 if held else 0)
                finalized = sink.content
                self.assertTrue(sink.closed)
                self.assertEqual(sink.close_attempts, 1)
                for failure, authorized in (("before_output", False), ("after_output", True)):
                    self.assertEqual(publication_decision(workflow, status, failure), (1, authorized))
                    self.assertEqual(sink.content, finalized)
                self.assertEqual(sink.content, output.getvalue())
                self.assert_safe(json.loads(finalized))
                sink.write.assert_called_once_with(finalized)
                self.assertEqual(host.launch.call_count, 1)

    def test_all_outcomes_retain_state(self):
        for expected in self.p.OUTCOMES:
            with self.subTest(outcome=expected), FixtureHost(self.p).mocked() as host, \
                    mock.patch.object(self.p, "write_metadata") as write, \
                    mock.patch.object(sys, "stdout", io.StringIO()) as output:
                if expected == "HELD_ADMISSION":
                    host.environment["DOCKER_HOST"] = ""
                elif expected == "HELD_TOOL_PATH":
                    host.fs[self.p.TOOL].st_uid = host.uid
                elif expected == "HELD_OWNED_DIR":
                    def partial_tree(path, mode):
                        if path == host.paths["home"]:
                            raise OSError(PRIVATE)
                        host.mkdir_path(path, mode)
                    host.mkdir.side_effect = partial_tree
                elif expected == "HELD_ENGINE_UNAVAILABLE_VM_TEARDOWN_PENDING":
                    host.launch.side_effect = OSError(PRIVATE)
                elif expected == "HELD_ENGINE_NONZERO_VM_TEARDOWN_PENDING":
                    host.process.poll.return_value = 1
                elif expected == "HELD_ENGINE_TIMEOUT_VM_TEARDOWN_PENDING":
                    host.process.poll.return_value = None
                    host.clock.side_effect = (0.0, self.p.ENGINE_SECONDS + 1.0)
                elif expected == "HELD_ENGINE_OUTPUT_LIMIT_VM_TEARDOWN_PENDING":
                    host.process.poll.return_value = None
                    host.chunks[101] = [b"x" * (self.p.MAX_OUTPUT + 1), b""]
                elif expected == "HELD_ENGINE_SCHEMA_VM_TEARDOWN_PENDING":
                    host.chunks[101] = [PRIVATE.encode("ascii"), b""]
                elif expected == "HELD_ENGINE_IDENTITY_STORE_VM_TEARDOWN_PENDING":
                    host.info["host"]["serviceIsRemote"] = True
                    host.refresh_payload()
                elif expected == "HELD_METADATA_OUTPUT_VM_TEARDOWN_PENDING":
                    write.side_effect = OSError(PRIVATE)
                elif expected == "HELD_INTERNAL_VM_TEARDOWN_PENDING":
                    collect = self.p.collect_metadata
                    def fail_after_observation():
                        collect()
                        raise RuntimeError(PRIVATE)
                    with mock.patch.object(self.p, "collect_metadata", side_effect=fail_after_observation):
                        self.assertEqual(self.p.main(), 20)
                    data = json.loads(output.getvalue())
                    self.assertEqual(data["outcome"], expected)
                    self.assertEqual(len(host.created), 10)
                    self.assert_safe(data)
                    write.assert_not_called()
                    continue
                status = (0 if expected == self.p.SUCCESS else
                          20 if expected == "HELD_METADATA_OUTPUT_VM_TEARDOWN_PENDING" else 10)
                self.assertEqual(self.p.main(), status)
                data = json.loads(output.getvalue())
                self.assertEqual(data["outcome"], expected)
                self.assert_safe(data)
                if expected not in ("HELD_ADMISSION", "HELD_TOOL_PATH", "HELD_OWNED_DIR"):
                    self.assertEqual(len(host.created), 10)
                self.assertTrue(all(path in host.fs for path in host.created))


if __name__ == "__main__":
    unittest.main()
