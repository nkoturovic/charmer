"""The three client roles of the real-client lanes (``_client_roles``) and
the runner's check of the two named control clients.

The candidate is the packaged pin; the previous release's pin (N-1) is the
downgrade-resume control; an older fallback control keeps the behaviours the
gateway-fallback checks show the pin removed. Each control has its own
variable, is an absolute path and is accepted only at its recorded sha256.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from charmer import pin, probe, strict_json

import _client_roles
import test_client_gateway_fallbacks as fallbacks
import test_scope_probe_client as client_probe
from _layout import REPO_ROOT, RESOURCES_ROOT
from _release import TEST_RUNNER, load_tool, requires_release_tree

_PACKAGED = strict_json.load(RESOURCES_ROOT / "catalog" / "native-contract.json")


class ClientRoleTests(unittest.TestCase):
    def test_three_distinct_roles(self) -> None:
        candidate = pin.version(_PACKAGED)
        previous, control = _client_roles.PREVIOUS, _client_roles.FALLBACK_CONTROL
        self.assertEqual(previous[0], "2.1.311")
        self.assertEqual(previous[1], "86dc1fcc69498159eaf15f446613e847010f9036bfbbdffd4c49350be8489ada")
        self.assertEqual(control[0], "2.1.302")
        self.assertEqual(control[1], "60d7eb7f12fbb28a52533fe8be5884097403562aa02dc73991a021c7067e29a5")
        # Strictly older down the roles: candidate > N-1 > fallback control.
        self.assertGreater(pin.version_key(candidate), pin.version_key(previous[0]))
        self.assertGreater(pin.version_key(previous[0]), pin.version_key(control[0]))
        self.assertNotEqual(_client_roles.PREVIOUS_ENV, _client_roles.FALLBACK_CONTROL_ENV)
        self.assertEqual(set(_client_roles.CONTROL_CLIENTS),
                         {"CHARMER_TEST_PREVIOUS_CLAUDE", "CHARMER_TEST_FALLBACK_CONTROL_CLAUDE"})

    def test_the_downgrade_resume_control_is_the_previous_release_pin(self) -> None:
        self.assertEqual(client_probe._DG_PREVIOUS, _client_roles.PREVIOUS)
        self.assertEqual(client_probe._DG_PREVIOUS_ENV, "CHARMER_TEST_PREVIOUS_CLAUDE")

    def test_the_fallback_controls_keep_the_older_client(self) -> None:
        self.assertEqual(fallbacks._FALLBACK_CONTROL, _client_roles.FALLBACK_CONTROL)
        self.assertEqual(fallbacks.FALLBACK_CONTROL_ENV, "CHARMER_TEST_FALLBACK_CONTROL_CLAUDE")
        self.assertFalse(hasattr(fallbacks, "PREVIOUS_ENV"))


class _Files(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="charmer-client-roles-"))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.older = self.base / "claude-older"
        self.newer = self.base / "claude-newer"
        self.older.write_bytes(b"#!/bin/sh\necho older\n")
        self.newer.write_bytes(b"#!/bin/sh\necho newer\n")
        self.table = {
            "CHARMER_TEST_PREVIOUS_CLAUDE": ("1.0.1", hashlib.sha256(self.newer.read_bytes()).hexdigest()),
            "CHARMER_TEST_FALLBACK_CONTROL_CLAUDE": ("1.0.0", hashlib.sha256(self.older.read_bytes()).hexdigest()),
        }


class NamedControlTests(_Files):
    def test_only_the_exact_build_at_an_absolute_path(self) -> None:
        with mock.patch.dict(_client_roles.CONTROL_CLIENTS, self.table):
            previous, control = "CHARMER_TEST_PREVIOUS_CLAUDE", "CHARMER_TEST_FALLBACK_CONTROL_CLAUDE"
            self.assertIsNone(_client_roles.named_control(previous, {}))
            self.assertIsNone(_client_roles.named_control(previous, {previous: "claude-newer"}))
            self.assertIsNone(_client_roles.named_control(previous, {previous: str(self.base / "missing")}))
            # Each role takes only its own build: the inputs are not interchangeable.
            self.assertIsNone(_client_roles.named_control(previous, {previous: str(self.older)}))
            self.assertIsNone(_client_roles.named_control(control, {control: str(self.newer)}))
            found = _client_roles.named_control(previous, {previous: str(self.newer)})
            self.assertEqual(found, probe.TrustedExecutable(self.newer.resolve(), self.table[previous][1]))
            found = _client_roles.named_control(control, {control: str(self.older)})
            self.assertEqual(found, probe.TrustedExecutable(self.older.resolve(), self.table[control][1]))

    def test_the_downgrade_lookup_reads_only_the_previous_role(self) -> None:
        trusted = probe.TrustedExecutable(self.base / "pinned" / "claude", "0" * 64)
        table = dict(self.table)
        with mock.patch.dict(_client_roles.CONTROL_CLIENTS, table), \
                mock.patch.object(client_probe, "_DG_PREVIOUS", table["CHARMER_TEST_PREVIOUS_CLAUDE"]):
            with mock.patch.dict(os.environ, {"CHARMER_TEST_FALLBACK_CONTROL_CLAUDE": str(self.newer)}, clear=False):
                os.environ.pop("CHARMER_TEST_PREVIOUS_CLAUDE", None)
                self.assertIsNone(client_probe._previous_pin(trusted))
            with mock.patch.dict(os.environ, {"CHARMER_TEST_PREVIOUS_CLAUDE": str(self.newer)}, clear=False):
                self.assertEqual(client_probe._previous_pin(trusted).resolved_path, self.newer.resolve())
                same = probe.TrustedExecutable(self.newer.resolve(), "0" * 64)
                self.assertIsNone(client_probe._previous_pin(same))


@requires_release_tree
class RunnerControlTests(_Files):
    def setUp(self) -> None:
        super().setUp()
        self.runner = load_tool(TEST_RUNNER, "charmer_test_runner_roles")
        self.repo = self.base / "repo"
        (self.repo / "tests").mkdir(parents=True)
        (self.repo / "tests" / "_client_roles.py").write_text(
            textwrap.dedent(f"""\
                CONTROL_CLIENTS = {{
                    "CHARMER_TEST_PREVIOUS_CLAUDE": {self.table["CHARMER_TEST_PREVIOUS_CLAUDE"]!r},
                    "CHARMER_TEST_FALLBACK_CONTROL_CLAUDE": {self.table["CHARMER_TEST_FALLBACK_CONTROL_CLAUDE"]!r},
                }}
            """), encoding="utf-8")

    def test_the_runner_reads_the_recorded_roles(self) -> None:
        self.assertEqual(self.runner.control_clients(REPO_ROOT), _client_roles.CONTROL_CLIENTS)
        self.assertEqual(self.runner.control_clients(self.repo), self.table)
        self.assertEqual(self.runner.control_clients(self.base / "elsewhere"), {})

    def test_a_named_control_is_checked_before_any_test(self) -> None:
        check = self.runner.check_control_clients
        previous, control = "CHARMER_TEST_PREVIOUS_CLAUDE", "CHARMER_TEST_FALLBACK_CONTROL_CLAUDE"
        self.assertEqual(check(self.repo, {}), [])
        lines = check(self.repo, {previous: str(self.newer), control: str(self.older)})
        self.assertEqual(lines, [f"control client {previous}: Claude Code 1.0.1 ({self.newer})",
                                 f"control client {control}: Claude Code 1.0.0 ({self.older})"])
        for environ, needle in (
            ({previous: "claude-newer"}, "must be an absolute path"),
            ({previous: str(self.base / "missing")}, "not a regular file"),
            ({previous: str(self.older)}, "not the Claude Code 1.0.1 build"),
            ({control: str(self.newer)}, "not the Claude Code 1.0.0 build"),
        ):
            with self.subTest(environ=environ):
                with self.assertRaises(self.runner.UsageError) as caught:
                    check(self.repo, environ)
                self.assertIn(needle, str(caught.exception))

    def test_a_wrong_control_stops_the_run(self) -> None:
        roles = self.repo / "tests" / "_client_roles.py"
        with mock.patch.object(self.runner, "REPO", self.repo), \
                mock.patch.object(self.runner, "select_modules", return_value=(["tests.test_never"], [])), \
                mock.patch.dict(os.environ, {"CHARMER_TEST_PREVIOUS_CLAUDE": str(self.older)}, clear=False), \
                mock.patch("sys.stderr") as stderr:
            self.assertTrue(roles.is_file())
            self.assertEqual(self.runner.main(["tests.test_never"]), self.runner.EXIT_USAGE)
        written = "".join(str(call.args[0]) for call in stderr.write.call_args_list)
        self.assertIn("CHARMER_TEST_PREVIOUS_CLAUDE names", written)


if __name__ == "__main__":
    unittest.main()
