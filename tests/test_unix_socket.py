"""Unix socket fixtures work below a deeply nested temporary directory."""

import os
import socket
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _unix_socket


@unittest.skipUnless(hasattr(socket, "AF_UNIX"), "needs Unix sockets")
class UnixSocketTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="unix-socket-")
        self.addCleanup(temporary.cleanup)
        parent = Path(temporary.name) / ("nested-" * 20)
        parent.mkdir()
        self.path = parent / "fixture.sock"
        self.assertGreater(len(os.fsencode(self.path)), 107)

    def test_long_path_creates_socket_without_changing_cwd(self) -> None:
        cwd = Path.cwd()
        with socket.socket(socket.AF_UNIX) as sock:
            if sys.platform == "linux":
                with mock.patch.object(os, "chdir", side_effect=AssertionError("changed cwd")):
                    _unix_socket.bind_unix_socket(sock, self.path)
            else:
                _unix_socket.bind_unix_socket(sock, self.path)
            self.assertTrue(stat.S_ISSOCK(self.path.stat().st_mode))
        self.assertEqual(Path.cwd(), cwd)

    @unittest.skipUnless(sys.platform == "linux", "needs Linux directory descriptors")
    def test_long_path_in_write_and_search_only_directory(self) -> None:
        self.path.parent.chmod(0o300)
        self.addCleanup(self.path.parent.chmod, 0o700)
        with socket.socket(socket.AF_UNIX) as sock:
            _unix_socket.bind_unix_socket(sock, self.path)
            self.assertTrue(stat.S_ISSOCK(self.path.stat().st_mode))

    def test_short_address_binds_directly(self) -> None:
        sock = mock.Mock(spec=socket.socket)
        _unix_socket.bind_unix_socket(sock, Path("fixture.sock"))
        sock.bind.assert_called_once_with("fixture.sock")

    def test_relative_fallback_restores_cwd_after_success_and_failure(self) -> None:
        cwd = Path.cwd()
        with mock.patch.object(_unix_socket.sys, "platform", "darwin"):
            with socket.socket(socket.AF_UNIX) as sock:
                _unix_socket.bind_unix_socket(sock, self.path)
            self.assertTrue(stat.S_ISSOCK(self.path.stat().st_mode))
            self.assertEqual(Path.cwd(), cwd)
            with socket.socket(socket.AF_UNIX) as sock, self.assertRaises(OSError):
                _unix_socket.bind_unix_socket(sock, self.path)
            self.assertEqual(Path.cwd(), cwd)

    def test_relative_fallback_refuses_to_change_cwd_with_threads(self) -> None:
        with mock.patch.object(_unix_socket.sys, "platform", "darwin"), \
                mock.patch.object(_unix_socket.threading, "active_count", return_value=2), \
                mock.patch.object(os, "chdir") as chdir:
            with socket.socket(socket.AF_UNIX) as sock, self.assertRaisesRegex(unittest.SkipTest, "fixture threads"):
                _unix_socket.bind_unix_socket(sock, self.path)
            chdir.assert_not_called()
