"""Settings and policy reads reject special files without waiting for a writer."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from charmer import hooks, managed


_READER = """
import json
import sys
from pathlib import Path
from charmer import hooks, managed

reader, path = sys.argv[1], Path(sys.argv[2])
print("entered reader", flush=True)
if reader == "strict":
    result = managed.read_settings(path)
elif reader == "client":
    result = managed.read_settings_as_client(path)
elif reader == "policy":
    result = managed._read_policy_file(path)
elif reader == "source":
    source = managed.policy_source(path.parent)
    result = {"settings": source.settings,
              "unreadable": [[p.name, reason] for p, reason in source.unreadable]}
elif reader == "findings":
    result = [[item.kind, item.blocks] for item in managed.policy_findings(path.parent)]
elif reader == "hook":
    result = hooks.saved_user_model({"CLAUDE_CONFIG_DIR": str(path.parent)})
else:
    raise AssertionError(reader)
print(json.dumps(result))
"""


class SettingsReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="settings-readers-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "settings.json"
        self.document = {"model": "fixture-model", "permissions": {"defaultMode": "plan"}}

    def read_child(self, reader: str, path: Path):
        # Keep a regression in open() from hanging the suite. The runner's
        # private HOME and guarded PYTHONPATH are inherited; cwd is unrelated
        # to the checkout so the reader cannot depend on repository discovery.
        try:
            result = subprocess.run(
                [sys.executable, "-B", "-c", _READER, reader, str(path)],
                cwd=self.root, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=3,
            )
        except subprocess.TimeoutExpired as exc:
            self.fail(f"{reader} settings reader did not return within 3 seconds: {exc.stdout!r}")
        self.assertEqual(result.returncode, 0, result.stderr)
        entered, output = result.stdout.splitlines()
        self.assertEqual(entered, "entered reader")
        return json.loads(output)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs POSIX FIFOs")
    def test_fifo_settings_returns_promptly(self) -> None:
        os.mkfifo(self.path)
        for reader, expected in (("strict", {}), ("client", {}), ("hook", None)):
            with self.subTest(reader=reader):
                self.assertEqual(self.read_child(reader, self.path), expected)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs POSIX FIFOs")
    def test_fifo_policy_is_unreadable_not_absent_and_returns_promptly(self) -> None:
        path = self.root / "managed-settings.json"
        os.mkfifo(path)
        self.assertEqual(self.read_child("policy", path), [None, "is not a regular file"])
        self.assertEqual(self.read_child("source", path), {
            "settings": {}, "unreadable": [[path.name, "is not a regular file"]],
        })
        unreadable_root = self.root / "unreadable"
        unreadable_root.mkdir()
        (unreadable_root / path.name).write_bytes(b"{not-json")
        ordinary_unreadable = [[item.kind, item.blocks] for item in managed.policy_findings(unreadable_root)]
        self.assertEqual(ordinary_unreadable, [["unreadable", False]])
        self.assertEqual(self.read_child("findings", path), ordinary_unreadable)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs POSIX FIFOs")
    def test_symlink_to_fifo_is_also_unusable_without_waiting(self) -> None:
        target = self.root / "fifo-target"
        os.mkfifo(target)
        self.path.symlink_to(target)
        self.assertEqual(self.read_child("client", self.path), {})
        self.assertEqual(self.read_child("policy", self.path), [None, "is not a regular file"])

    def test_directory_is_not_a_settings_or_policy_file(self) -> None:
        self.path.mkdir()
        self.assertEqual(managed.read_settings(self.path), {})
        self.assertEqual(managed.read_settings_as_client(self.path), {})
        self.assertEqual(managed._read_policy_file(self.path), (None, "is not a regular file"))
        self.assertIsNone(hooks.saved_user_model({"CLAUDE_CONFIG_DIR": str(self.root)}))

    def test_regular_file_and_symlink_remain_readable(self) -> None:
        target = self.root / "regular-target"
        target.write_text(json.dumps(self.document), encoding="utf-8")
        self.path.symlink_to(target)
        for path in (target, self.path):
            with self.subTest(symlink=path == self.path):
                self.assertEqual(managed.read_settings(path), self.document)
                self.assertEqual(managed.read_settings_as_client(path), self.document)
                self.assertEqual(managed._read_policy_file(path), (self.document, None))
        self.assertEqual(hooks.saved_user_model({"CLAUDE_CONFIG_DIR": str(self.root)}),
                         (self.path, self.document["model"]))

    def test_exact_size_limit_is_read_but_one_extra_byte_is_rejected(self) -> None:
        limit = managed._SETTINGS_CAP
        raw = json.dumps(self.document).encode("utf-8")
        self.path.write_bytes(raw + b" " * (limit - len(raw)))
        self.assertEqual(managed.read_settings(self.path), self.document)
        self.assertEqual(managed.read_settings_as_client(self.path), self.document)
        self.assertEqual(managed._read_policy_file(self.path), (self.document, None))
        self.assertEqual(hooks.saved_user_model({"CLAUDE_CONFIG_DIR": str(self.root)}),
                         (self.path, self.document["model"]))
        with self.path.open("ab") as stream:
            stream.write(b" ")
        self.assertEqual(managed.read_settings(self.path), {})
        self.assertEqual(managed.read_settings_as_client(self.path), {})
        self.assertEqual(managed._read_policy_file(self.path), (None, "is larger than 1 MiB"))
        self.assertIsNone(hooks.saved_user_model({"CLAUDE_CONFIG_DIR": str(self.root)}))

    def test_oversized_file_read_is_bounded(self) -> None:
        limit = managed._SETTINGS_CAP
        self.path.write_bytes(b" " * (2 * limit))
        with mock.patch.object(hooks.os, "read", wraps=os.read) as read:
            raw = hooks.read_settings_bytes(self.path, limit)
        self.assertEqual(len(raw), limit + 1)
        read.assert_called_once_with(mock.ANY, limit + 1)

    def test_short_reads_do_not_truncate_the_settings_document(self) -> None:
        self.path.write_text(json.dumps(self.document), encoding="utf-8")
        real_read = os.read

        def short_read(descriptor: int, count: int) -> bytes:
            return real_read(descriptor, min(count, 7))

        with mock.patch.object(hooks.os, "read", side_effect=short_read):
            self.assertEqual(managed.read_settings(self.path), self.document)
            self.assertEqual(managed.read_settings_as_client(self.path), self.document)
            self.assertEqual(managed._read_policy_file(self.path), (self.document, None))
            self.assertEqual(hooks.saved_user_model({"CLAUDE_CONFIG_DIR": str(self.root)}),
                             (self.path, self.document["model"]))

    def test_missing_policy_is_still_absent(self) -> None:
        self.assertEqual(managed.read_settings(self.path), {})
        self.assertEqual(managed.read_settings_as_client(self.path), {})
        self.assertEqual(managed._read_policy_file(self.path), (None, None))
        self.assertIsNone(hooks.saved_user_model({"CLAUDE_CONFIG_DIR": str(self.root)}))
