"""The launcher across the interpreters it runs on: argparse messages and
text encoding.

Bundles run the pinned CPython (3.15); a source checkout or the Nix package
runs any Python 3.11 or newer. Two 3.15 changes reach the launcher without
a code change. argparse suggests a close match for a mistyped choice by
default, so a parser error is compared with that one clause optional, and
the clause is required where the interpreter suggests by default. UTF-8
mode is the default, so output is UTF-8 whatever the locale; under a
locale that is neither UTF-8 nor C (ISO-8859-1 here), the launcher's
read-only commands run in UTF-8 mode, with no implicit text encoding
(``-X warn_default_encoding`` shows none).
"""

from __future__ import annotations

import argparse
import inspect
import locale
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from _layout import REPO_ROOT
from _release import RUNTIMES_JSON, PYTHON_RUNTIME, load_tool, requires_release_tree

LATIN1 = "en_US.ISO-8859-1"
SUGGESTION = re.compile(r", maybe you meant '[^']+'\?")
ANSI = re.compile(r"\x1b\[")
CHOICES_ERROR = re.compile(r"\Ausage: charmer .*\ncharmer: error: argument COMMAND: invalid choice: '(?P<word>[^']+)'"
                           r" \(choose from (?:'[\w-]+', )+'[\w-]+'\)\n\Z", re.S)


def suggests_by_default() -> bool:
    parameter = inspect.signature(argparse.ArgumentParser).parameters.get("suggest_on_error")
    return parameter is not None and parameter.default is True


def latin1_available() -> bool:
    current = locale.setlocale(locale.LC_CTYPE)
    try:
        locale.setlocale(locale.LC_CTYPE, LATIN1)
    except locale.Error:
        return False
    finally:
        locale.setlocale(locale.LC_CTYPE, current)
    return True


class LauncherProcess(unittest.TestCase):
    def run_cli(self, *argv: str, python_options: tuple[str, ...] = (), **env: str) -> subprocess.CompletedProcess:
        home = Path(tempfile.mkdtemp(prefix="cm-compat-home-"))
        self.addCleanup(__import__("shutil").rmtree, home, True)
        environment = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                       "PYTHONPATH": str(REPO_ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1", "LC_ALL": "C.UTF-8",
                       **env}
        return subprocess.run([sys.executable, *python_options, "-B", "-m", "charmer.cli", *argv], env=environment,
                              cwd=home, stdin=subprocess.DEVNULL, capture_output=True, timeout=120)


class ParserMessageTests(LauncherProcess):
    def test_an_unknown_command_has_one_message_on_every_interpreter(self) -> None:
        for word, near in (("no-such-command", None), ("lineu", "lineup"), ("doctr", "doctor")):
            with self.subTest(word):
                result = self.run_cli(word)
                stderr = result.stderr.decode()
                self.assertEqual((result.returncode, result.stdout), (2, b""))
                found = SUGGESTION.search(stderr)
                if near is not None and suggests_by_default():
                    self.assertIsNotNone(found, stderr)
                    self.assertEqual(found.group(0), f", maybe you meant '{near}'?")
                elif near is None or not suggests_by_default():
                    self.assertIsNone(found, stderr)
                match = CHOICES_ERROR.match(SUGGESTION.sub("", stderr))
                self.assertIsNotNone(match, stderr)
                self.assertEqual(match.group("word"), word)

    def test_an_unknown_option_names_it(self) -> None:
        for argv, option in ((("doctor", "--jsno"), "--jsno"), (("--hlep",), "--hlep")):
            with self.subTest(argv):
                result = self.run_cli(*argv)
                self.assertEqual((result.returncode, result.stdout), (2, b""))
                self.assertTrue(result.stderr.decode().endswith(f"charmer: error: unrecognized arguments: {option}\n"))

    def test_redirected_messages_are_never_coloured(self) -> None:
        """Python 3.14 and later colour argparse output on a terminal; a pipe
        and NO_COLOR get none (NO_COLOR wins over FORCE_COLOR)."""

        for env in ({}, {"NO_COLOR": "1", "FORCE_COLOR": "1"}):
            for argv in (("lineu",), ("doctor", "--jsno"), ("lineup", "--help")):
                with self.subTest(env=env, argv=argv):
                    result = self.run_cli(*argv, **env)
                    self.assertIsNone(ANSI.search((result.stdout + result.stderr).decode()))


@requires_release_tree
class EncodingTests(LauncherProcess):
    READ_ONLY = (("--version",), ("--help",), ("lineup", "--help"), ("sessions", "list"), ("models", "list"),
                 ("profile", "list"), ("providers", "list"))

    def test_the_bundled_runtime_defaults_to_utf8_mode(self) -> None:
        runtimes = load_tool(PYTHON_RUNTIME, "python_runtime").load(RUNTIMES_JSON)
        self.assertGreaterEqual(tuple(int(part) for part in runtimes.minor.split(".")), (3, 15))

    def test_read_only_commands_under_a_non_utf8_locale(self) -> None:
        if not latin1_available():
            self.skipTest(f"BOUNDARY: the locale {LATIN1} is not installed here")
        # The bundled runtime's default; an older interpreter gets it explicitly.
        options = ("-X", "warn_default_encoding") + (() if sys.version_info >= (3, 15) else ("-X", "utf8"))
        for argv in self.READ_ONLY:
            with self.subTest(argv):
                result = self.run_cli(*argv, python_options=options, LC_ALL=LATIN1, LANG=LATIN1, NO_COLOR="1")
                stderr = result.stderr.decode("utf-8", "replace")
                self.assertEqual(result.returncode, 0, stderr)
                self.assertNotIn("EncodingWarning", stderr)
                result.stdout.decode("utf-8")  # UTF-8 output, whatever the locale
        sessions = self.run_cli("sessions", "list", python_options=options, LC_ALL=LATIN1, NO_COLOR="1")
        self.assertIn("● live".encode(), sessions.stdout)  # a marker ISO-8859-1 cannot encode


if __name__ == "__main__":
    unittest.main()
