"""Mode-parameterised atomic writes outside private state directories."""

from __future__ import annotations

import errno
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from charmer import state
from charmer.platform import posix_fs


class AtomicReplaceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="cm-replace-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.root.chmod(0o755)
        self.target = self.root / "output"
        self.target.write_bytes(b"before")

    def test_explicit_mode_and_same_directory_replace(self) -> None:
        real_replace = os.replace
        for mode in (0o600, 0o644, 0o755, 0):
            with self.subTest(mode=mode), mock.patch.object(os, "replace", wraps=real_replace) as replace:
                posix_fs.atomic_replace(self.target, b"after", mode=mode)
                source, target = replace.call_args.args
                self.assertEqual(Path(source).parent, self.root)
                self.assertEqual(target, self.target)
                self.assertEqual(replace.call_count, 1)
                self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), mode)
                self.target.chmod(0o600)
                self.assertEqual(self.target.read_bytes(), b"after")
                self.assertEqual(set(self.root.iterdir()), {self.target})
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o755)

    def test_preexisting_temp_is_neither_overwritten_nor_removed(self) -> None:
        occupied = self.root / ".output.occupied.tmp"
        occupied.write_bytes(b"other writer")
        with mock.patch.object(tempfile, "_get_candidate_names", return_value=iter(["occupied", "fresh"])):
            posix_fs.atomic_replace(self.target, b"after", mode=0o644)
        self.assertEqual(occupied.read_bytes(), b"other writer")
        self.assertEqual(self.target.read_bytes(), b"after")
        self.assertEqual(set(self.root.iterdir()), {self.target, occupied})

    def test_temp_creation_failure_does_not_remove_another_file(self) -> None:
        occupied = self.root / ".output.occupied.tmp"
        occupied.write_bytes(b"other writer")
        with mock.patch.object(tempfile, "mkstemp", side_effect=FileExistsError(errno.EEXIST, "fixture")):
            with self.assertRaises(FileExistsError):
                posix_fs.atomic_replace(self.target, b"after", mode=0o644)
        self.assertEqual(occupied.read_bytes(), b"other writer")
        self.assertEqual(self.target.read_bytes(), b"before")

    def test_symlink_destination_refused_including_dangling(self) -> None:
        self.target.unlink()
        for name in ("referent", "absent"):
            referent = self.root / name
            if name == "referent":
                referent.write_bytes(b"untouched")
            self.target.symlink_to(referent)
            with self.subTest(name=name), self.assertRaises(OSError) as caught:
                posix_fs.atomic_replace(self.target, b"after", mode=0o644)
            self.assertEqual(caught.exception.errno, errno.ELOOP)
            self.assertTrue(self.target.is_symlink())
            if name == "referent":
                self.assertEqual(referent.read_bytes(), b"untouched")
            else:
                self.assertFalse(referent.exists())
            self.target.unlink()

    def test_symlink_created_during_write_is_refused(self) -> None:
        referent = self.root / "referent"
        referent.write_bytes(b"untouched")
        real_fsync = os.fsync

        def sync(fd):
            real_fsync(fd)
            self.target.unlink()
            self.target.symlink_to(referent)

        with mock.patch.object(os, "fsync", side_effect=sync):
            with self.assertRaises(OSError) as caught:
                posix_fs.atomic_replace(self.target, b"after", mode=0o644)
        self.assertEqual(caught.exception.errno, errno.ELOOP)
        self.assertTrue(self.target.is_symlink())
        self.assertEqual(referent.read_bytes(), b"untouched")
        self.assertEqual(set(self.root.iterdir()), {self.target, referent})

    def test_failure_before_replace_cleans_temp_and_preserves_target(self) -> None:
        for operation in ("fchmod", "fdopen", "fsync", "replace"):
            with self.subTest(operation=operation), \
                    mock.patch.object(os, operation, side_effect=OSError(errno.EIO, "fixture")):
                with self.assertRaises(OSError) as caught:
                    posix_fs.atomic_replace(self.target, b"after", mode=0o644)
            self.assertNotIsInstance(caught.exception, posix_fs.CommittedReplaceError)
            self.assertEqual(self.target.read_bytes(), b"before")
            self.assertEqual(set(self.root.iterdir()), {self.target})

    def test_descriptor_closed_when_setup_fails(self) -> None:
        real_mkstemp = tempfile.mkstemp
        opened = []

        def create(**kwargs):
            descriptor, name = real_mkstemp(**kwargs)
            opened.append(descriptor)
            return descriptor, name

        for operation in ("fchmod", "fdopen"):
            with self.subTest(operation=operation), mock.patch.object(tempfile, "mkstemp", side_effect=create), \
                    mock.patch.object(os, operation, side_effect=OSError(errno.EIO, "fixture")):
                with self.assertRaises(OSError):
                    posix_fs.atomic_replace(self.target, b"after", mode=0o644)
            with self.assertRaises(OSError):
                os.fstat(opened[-1])

    def test_post_replace_failure_is_committed_with_errno_and_target(self) -> None:
        with mock.patch.object(posix_fs, "fsync_directory", side_effect=OSError(errno.ENOSPC, "fixture")):
            with self.assertRaisesRegex(posix_fs.CommittedReplaceError, "written.*durability unconfirmed") as caught:
                posix_fs.atomic_replace(self.target, b"after", mode=0o755)
        self.assertEqual(caught.exception.errno, errno.ENOSPC)
        self.assertEqual(caught.exception.filename, str(self.target))
        self.assertEqual(self.target.read_bytes(), b"after")
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o755)
        self.assertEqual(set(self.root.iterdir()), {self.target})
        self.assertIn("written but may not be durable", state.filesystem_failure(caught.exception)[0])

    def test_nondurable_write_skips_both_syncs(self) -> None:
        with mock.patch.object(os, "fsync") as fsync, \
                mock.patch.object(posix_fs, "fsync_directory") as directory:
            posix_fs.atomic_replace(self.target, b"after", mode=0o644, durable=False)
        fsync.assert_not_called()
        directory.assert_not_called()
        self.assertEqual(self.target.read_bytes(), b"after")

    def test_state_committed_error_shares_the_commit_marker(self) -> None:
        self.assertIsInstance(state.CommittedStateError(errno.EIO, "fixture"), posix_fs.CommittedReplaceError)
        self.assertTrue(issubclass(state.CommittedStateError, state.StateError))


if __name__ == "__main__":
    unittest.main()
