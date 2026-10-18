"""Malicious links and tarfile's link fallback, on the running interpreter.

Two places unpack archives: the release build (the pinned
python-build-standalone runtimes, ``tools/_build/python_runtime.py``) and the
updater (a signed bundle, ``charmer.self_update``). Both check every member
before they extract, then extract with tarfile's ``data`` filter. The
bundles run CPython 3.15, whose tarfile also re-applies the filter when it
falls back from a hard link to a copy of its target; the suite runs these
fixtures on every interpreter it runs on (CI: 3.11, 3.14 and the bundled
3.15). Each malicious archive is refused with the tool's own error or, for
the updater, left inside its staging directory; nothing is ever written
outside, and the only filter used is ``data``, which never returns None
(the path where a filter's None skipped link fallback handling).
"""

from __future__ import annotations

import io
import os
import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from charmer import self_update
from _release import PYTHON_RUNTIME, load_tool, requires_release_tree

TOP = "charmer-9.9.9-linux-x86_64"


def archive(path: Path, top: str, entries: list[tuple[str, str, object]]) -> Path:
    """``(name below top, kind, payload)``, kind file/dir/symlink/hardlink;
    a hard link's payload names another entry below top."""

    with tarfile.open(path, "w:gz") as tar:
        root = tarfile.TarInfo(top)
        root.type, root.mode = tarfile.DIRTYPE, 0o755
        tar.addfile(root)
        for name, kind, payload in entries:
            info = tarfile.TarInfo(f"{top}/{name}")
            if kind == "file":
                data = payload if isinstance(payload, bytes) else b""
                info.size, info.mode = len(data), 0o644
                tar.addfile(info, io.BytesIO(data))
                continue
            if kind == "dir":
                info.type, info.mode = tarfile.DIRTYPE, 0o755
            elif kind == "symlink":
                info.type, info.linkname = tarfile.SYMTYPE, str(payload)
            else:
                info.type, info.linkname = tarfile.LNKTYPE, f"{top}/{payload}"
            tar.addfile(info)
    return path


# Each link is lexically inside the archive's top directory; followed as
# the extracted tree resolves them, some leave it.
MALICIOUS = {
    "symlink chain": [("d", "symlink", "."), ("d/e", "symlink", ".."), ("d/e/f", "symlink", ".."),
                      ("d/e/f/evil", "file", b"x")],
    "link through a later link": [("x", "symlink", "d/.."), ("d", "symlink", "."), ("x/escape", "file", b"x")],
    "member below a symlink": [("up", "symlink", "."), ("up/file", "file", b"x")],
    "hard link fallback to a relative symlink": [("a", "dir", None), ("a/s", "symlink", "../b"),
                                                  ("h", "hardlink", "a/s"), ("h/evil", "file", b"x")],
    "hard link to a directory": [("sub", "dir", None), ("h", "hardlink", "sub")],
    "hard link to a later file": [("h", "hardlink", "later"), ("later", "file", b"x")],
    "self-referencing link": [("loop", "symlink", "loop")],
}
# A link that stays inside (the runtimes' terminfo tree has many like it).
BENIGN = [("bin", "dir", None), ("bin/python3", "file", b"#!/bin/sh\n"), ("share", "dir", None),
          ("share/a", "dir", None), ("share/a/adm3a", "file", b"terminfo"), ("share/1", "dir", None),
          ("share/1/1178", "symlink", "../a/adm3a"), ("share/a/copy", "hardlink", "share/a/adm3a")]


def _outside(root: Path, keep: Path) -> list[str]:
    """Entries under ``root`` other than ``keep`` and what lies below it."""

    return sorted(str(path.relative_to(root)) for path in root.rglob("*")
                  if path != keep and keep not in path.parents)


@requires_release_tree
class BuildExtractionTests(unittest.TestCase):
    """The release build's runtime extraction refuses each malicious
    archive before writing anything."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tool = load_tool(PYTHON_RUNTIME, "python_runtime")

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="charmer-tar-build-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_malicious_links_are_refused_before_extraction(self) -> None:
        for label, entries in MALICIOUS.items():
            with self.subTest(label):
                case = self.tmp / label.replace(" ", "-")
                case.mkdir()
                source = archive(case / "runtime.tar.gz", "python", [("bin", "dir", None),
                                                                     ("bin/python3", "file", b"x"), *entries])
                with self.assertRaises(self.tool.BuildError):
                    self.tool.extract(source, case / "dest")
                self.assertEqual(sorted(p.name for p in case.iterdir()), ["dest", "runtime.tar.gz"])
                self.assertEqual(list((case / "dest").iterdir()), [])

    def test_links_inside_extract(self) -> None:
        root = self.tool.extract(archive(self.tmp / "runtime.tar.gz", "python", BENIGN), self.tmp / "dest")
        self.assertEqual((root / "share/1/1178").read_bytes(), b"terminfo")
        self.assertEqual((root / "share/a/copy").read_bytes(), b"terminfo")

    def test_only_the_data_filter(self) -> None:
        seen = []
        original = tarfile.TarFile.extractall

        def record(tar, *args, **kwargs):
            seen.append(kwargs.get("filter"))
            return original(tar, *args, **kwargs)

        with mock.patch.object(tarfile.TarFile, "extractall", record):
            self.tool.extract(archive(self.tmp / "runtime.tar.gz", "python", BENIGN), self.tmp / "dest")
        self.assertEqual(seen, ["data"] if hasattr(tarfile, "data_filter") else [None])


class UpdateExtractionTests(unittest.TestCase):
    """The updater's bundle extraction: a malicious archive raises an
    UpdateError or stays inside the staging directory; never a raw tarfile
    error, never a write outside."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="charmer-tar-update-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def unpack(self, label: str, entries: list[tuple[str, str, object]]) -> tuple[Path, Path | None, Exception | None]:
        case = self.tmp / label.replace(" ", "-")
        (case / "work").mkdir(parents=True)
        source = archive(case / "bundle.tar.gz", TOP, entries)
        staging = case / "work" / "staging"
        try:
            return case, self_update._unpack(source, staging, TOP), None
        except self_update.UpdateError as exc:
            return case, None, exc

    def test_malicious_links_never_write_outside_staging(self) -> None:
        for label, entries in MALICIOUS.items():
            with self.subTest(label):
                case, unpacked, _error = self.unpack(label, entries)
                staging = case / "work" / "staging"
                self.assertEqual(_outside(case / "work", staging), [])
                self.assertEqual(sorted(p.name for p in case.iterdir()), ["bundle.tar.gz", "work"])
                if unpacked is not None:
                    self.assertEqual(sorted(p.name for p in staging.iterdir()), [TOP])
                    for path in unpacked.rglob("*"):
                        self.assertTrue(staging in Path(os.path.realpath(path)).parents
                                        or Path(os.path.realpath(path)) == staging, path)

    def test_the_lexical_checks_refuse_what_they_can_see(self) -> None:
        for label in ("hard link to a later file",):
            with self.subTest(label):
                _case, unpacked, error = self.unpack(label, MALICIOUS[label])
                self.assertIsNone(unpacked)
                self.assertIn("names no earlier entry", str(error))

    def test_links_inside_extract(self) -> None:
        _case, unpacked, error = self.unpack("benign", BENIGN)
        self.assertIsNone(error)
        self.assertEqual((unpacked / "share/1/1178").read_bytes(), b"terminfo")
        self.assertEqual((unpacked / "share/a/copy").read_bytes(), b"terminfo")

    def test_only_the_data_filter(self) -> None:
        seen = []
        original = tarfile.TarFile.extractall

        def record(tar, *args, **kwargs):
            seen.append(kwargs.get("filter"))
            return original(tar, *args, **kwargs)

        with mock.patch.object(tarfile.TarFile, "extractall", record):
            self.unpack("filter", BENIGN)
        self.assertEqual(seen, ["data"] if hasattr(tarfile, "data_filter") else [None])


if __name__ == "__main__":
    unittest.main()
