"""The byte-stable bundle-content-v1 digest, shared by build and verification.

Readers supply (path parts, kind, mode, value) records. A file's value is its
SHA-256, a link's is its literal target, and a directory's is empty. This
module only orders and encodes records; it neither reads nor follows paths.
"""

from __future__ import annotations

import hashlib
from typing import Iterable

CONTENT_FORMAT = "bundle-content-v1"
Entry = tuple[tuple[str, ...], str, int, str]


def content_digest(entries: Iterable[Entry], *, exclude: Iterable[str] = ()) -> tuple[str, int]:
    """Return (sha256, regular-file count) using the release archive's modes."""

    skipped = set(exclude)
    total = hashlib.sha256(CONTENT_FORMAT.encode() + b"\n")
    files = 0
    for parts, kind, mode, value in sorted(entries, key=lambda entry: entry[0]):
        relative = "/".join(parts)
        if relative in skipped:
            continue
        if kind == "file":
            files += 1
            line = f"F {0o755 if mode & 0o111 else 0o644:04o} {value} {relative}\n"
        elif kind == "link":
            line = f"L {value} {relative}\n"
        elif kind == "dir":
            line = f"D {relative}\n"
        else:
            raise ValueError("not a bundle entry kind")
        total.update(line.encode())
    return total.hexdigest(), files
