"""Locale-independent command output and explicit text I/O encodings."""

from __future__ import annotations

import ast
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _catalog import FIXTURE_ROOT
from _layout import REPO_ROOT, SRC_DIR
from charmer import entrypoints
from charmer.platform import mounts


class StandardStreamTests(unittest.TestCase):
    def streams(self, stdout_encoding: str, stderr_encoding: str):
        streams = tuple(io.TextIOWrapper(io.BytesIO(), encoding=encoding, errors="strict")
                        for encoding in (stdout_encoding, stderr_encoding))
        for stream in streams:
            self.addCleanup(stream.close)
        return streams

    def test_all_three_programs_replace_unencodable_output_before_dispatch(self) -> None:
        def program(argv):
            print("● café")
            print("● café", file=sys.stderr)
            return 17

        for name in entrypoints.NAMES:
            with self.subTest(name=name):
                stdout, stderr = self.streams("latin-1", "latin-1")
                with mock.patch.object(sys, "stdout", stdout), mock.patch.object(sys, "stderr", stderr), \
                        mock.patch.object(sys, "flags", SimpleNamespace(utf8_mode=0)), \
                        mock.patch.dict(entrypoints.PROGRAMS, {name: program}):
                    result = entrypoints.run(name, [])
                self.assertEqual(result, 17)
                for stream in (stdout, stderr):
                    stream.flush()
                    self.assertEqual(stream.encoding, "latin-1")
                    self.assertEqual(stream.errors, "replace")
                    self.assertEqual(stream.buffer.getvalue(), b"? caf\xe9\n")

    def test_only_utf8_streams_keep_their_error_policy_regardless_of_utf8_mode(self) -> None:
        for encodings, utf8_mode, expected in (
            (("utf-8", "utf-8"), 0, ("strict", "strict")),
            (("utf-8", "latin-1"), 0, ("strict", "replace")),
            (("latin-1", "utf-8"), 0, ("replace", "strict")),
            (("latin-1", "ascii"), 1, ("replace", "replace")),
            (("shift_jis", "utf-8"), 0, ("replace", "strict")),
            (("utf_8", "UTF8"), 1, ("strict", "strict")),
        ):
            with self.subTest(encodings=encodings, utf8_mode=utf8_mode):
                stdout, stderr = self.streams(*encodings)
                with mock.patch.object(sys, "stdout", stdout), mock.patch.object(sys, "stderr", stderr), \
                        mock.patch.object(sys, "flags", SimpleNamespace(utf8_mode=utf8_mode)), \
                        mock.patch.dict(entrypoints.PROGRAMS, {"charmer": lambda argv: 0}):
                    self.assertEqual(entrypoints.run("charmer", []), 0)
                self.assertEqual((stdout.encoding, stderr.encoding), encodings)
                self.assertEqual((stdout.errors, stderr.errors), expected)

    def test_streams_without_reconfigure_are_left_alone(self) -> None:
        for stream in (io.StringIO(), SimpleNamespace(encoding="latin-1"), None):
            with self.subTest(stream=type(stream).__name__), \
                    mock.patch.object(sys, "stdout", stream), mock.patch.object(sys, "stderr", stream), \
                    mock.patch.object(sys, "flags", SimpleNamespace(utf8_mode=0)), \
                    mock.patch.dict(entrypoints.PROGRAMS, {"charmer": lambda argv: 0}):
                self.assertEqual(entrypoints.run("charmer", []), 0)


class CommandLocaleTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="charmer-locale-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()

    def command(self, args: list[str], encoding: str, *, bypass_run: bool = False, utf8_mode: str = "0"):
        env = {**os.environ, "HOME": str(self.home), "TERM": "dumb",
               "XDG_CONFIG_HOME": str(self.root / "config"), "XDG_STATE_HOME": str(self.root / "state"),
               "XDG_DATA_HOME": str(self.root / "data"), "CHARMER_ASSETS": str(FIXTURE_ROOT),
               "LC_ALL": "C", "PYTHONCOERCECLOCALE": "0", "PYTHONUTF8": utf8_mode,
               "PYTHONIOENCODING": encoding,
               # The source first; the runner's own entries (its guard) stay.
               "PYTHONPATH": os.pathsep.join([str(SRC_DIR), *filter(None, os.environ.get("PYTHONPATH", "").split(os.pathsep))])}
        if bypass_run:
            # The pre-stream-policy dispatch, to pin the unchanged UTF-8 bytes.
            argv = ["-c", "import sys; from charmer import entrypoints; "
                    "raise SystemExit(entrypoints.PROGRAMS[sys.argv[1]](sys.argv[2:]))"]
        else:
            argv = ["-m", "charmer.entrypoints"]
        return subprocess.run([sys.executable, *argv, "charmer", *args], cwd=self.root,
                              env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=30)

    def test_the_child_keeps_the_runner_path(self) -> None:
        # The runner's PYTHONPATH carries its connection guard; the child
        # gets the source first and keeps every inherited entry.
        inherited = os.pathsep.join(["/runner-guard", "", "/runner-tests"])
        with mock.patch.dict(os.environ, {"PYTHONPATH": inherited}), \
                mock.patch.object(subprocess, "run") as run:
            self.command(["--help"], "utf-8")
        self.assertEqual(run.call_args.kwargs["env"]["PYTHONPATH"].split(os.pathsep),
                         [str(SRC_DIR), "/runner-guard", "/runner-tests"])

    def test_help_and_sessions_list_survive_latin1(self) -> None:
        for args in (["--help"], ["sessions", "list"]):
            with self.subTest(args=args):
                utf8 = self.command(args, "utf-8")
                latin1 = self.command(args, "latin-1")
                self.assertEqual(utf8.returncode, 0, utf8.stderr)
                self.assertEqual(latin1.returncode, 0, latin1.stderr)
                self.assertNotIn(b"Traceback", latin1.stderr)
                self.assertEqual(latin1.stdout, utf8.stdout.decode("utf-8").encode("latin-1", errors="replace"))
                if args == ["sessions", "list"]:
                    self.assertIn("●".encode("utf-8"), utf8.stdout)

    def test_ascii_help_survives_a_stream_override_in_utf8_mode(self) -> None:
        utf8 = self.command(["--help"], "utf-8", utf8_mode="1")
        ascii_output = self.command(["--help"], "ascii", utf8_mode="1")
        self.assertEqual(utf8.returncode, 0, utf8.stderr)
        self.assertEqual(ascii_output.returncode, 0, ascii_output.stderr)
        self.assertNotIn(b"Traceback", ascii_output.stderr)
        self.assertEqual(ascii_output.stdout, utf8.stdout.decode("utf-8").encode("ascii", errors="replace"))

    def test_sessions_list_survives_shift_jis_which_encodes_only_some_markers(self) -> None:
        utf8 = self.command(["sessions", "list"], "utf-8")
        shift_jis = self.command(["sessions", "list"], "shift_jis")
        self.assertEqual(utf8.returncode, 0, utf8.stderr)
        self.assertEqual(shift_jis.returncode, 0, shift_jis.stderr)
        self.assertNotIn(b"Traceback", shift_jis.stderr)
        self.assertEqual(shift_jis.stdout, utf8.stdout.decode("utf-8").encode("shift_jis", errors="replace"))

    def test_utf8_output_is_byte_identical_to_unconfigured_dispatch(self) -> None:
        for args in (["--help"], ["sessions", "list"]):
            with self.subTest(args=args):
                before = self.command(args, "utf-8", bypass_run=True)
                after = self.command(args, "utf-8")
                self.assertEqual(before.returncode, 0, before.stderr)
                self.assertEqual(after.returncode, 0, after.stderr)
                self.assertEqual((after.stdout, after.stderr), (before.stdout, before.stderr))


class TextEncodingTests(unittest.TestCase):
    def test_mount_points_keep_utf8_and_undecodable_filename_bytes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="charmer-mount-encoding-") as tmp:
            table = Path(tmp) / "mountinfo"
            table.write_bytes(b"1 0 0:1 / /mnt/caf\xc3\xa9 rw - ext4 /dev/a rw\n"
                              b"2 0 0:2 / /mnt/raw-\xff rw - nfs server:/ rw\n")
            self.assertEqual(mounts.linux_mounts(table), [("/mnt/café", "ext4"), ("/mnt/raw-\udcff", "nfs")])

    def test_scoped_text_io_always_names_its_encoding(self) -> None:
        paths = (
            "src/charmer/platform/mounts.py", "src/charmer/upgrade.py", "src/charmer/setup/firstrun.py",
            "src/charmer/platform/linux_process.py", "src/charmer/release_manifest.py",
            "src/charmer/cli/commands/migration.py", "tools/release.py", "tools/history_scan.py",
        )
        for relative in paths:
            tree = ast.parse((REPO_ROOT / relative).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                keywords = {kw.arg: kw.value for kw in node.keywords}
                method = getattr(node.func, "attr", "")
                text = keywords.get("text")
                text_mode = isinstance(text, ast.Constant) and text.value is True
                file_text = method in ("read_text", "write_text") or (
                    method == "fdopen" and len(node.args) > 1 and isinstance(node.args[1], ast.Constant)
                    and "b" not in node.args[1].value)
                if text_mode or file_text:
                    with self.subTest(path=relative, line=node.lineno):
                        self.assertIn("encoding", keywords, f"{relative}:{node.lineno} uses the locale's default encoding")
