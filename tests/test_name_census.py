"""The name census: the product's earlier name appears nowhere in the
repository outside an explicit allowlist, and every allowlisted span is intact.

charmer was released under an earlier name up to 1.1.0
(``charmer.legacy.SPELLINGS`` holds every spelling of it). History keeps that
name: the released release notes, the fixtures that prove compatibility with
what earlier releases wrote, recorded wire names and recorded evidence.
Everything current names charmer. The allowlist is explicit: whole files
whose bytes are pinned, and exact spans of a file, each with the reason it
keeps the old name. This module spells the old name only through
``charmer.legacy``.
"""

from __future__ import annotations

import hashlib
import json
import unittest

from _layout import REPO_ROOT, repository_files
from charmer import legacy

OLD = legacy.NAME
SPELLINGS = tuple(spelling.encode() for spelling in legacy.SPELLINGS)

# The one module of the package that spells the old names (its constants
# carry their lifetimes). Not pinned: it grows with the migration.
LEGACY_MODULE = "src/charmer/legacy.py"

# Whole historical files, byte for byte (path -> sha256, reason).
HISTORICAL_FILES = {
    "tests/fixtures/permissive/catalog-1.0.0.json": (
        "78ecf9c48c65b233f66971e87940456a3ac52b1efd39e255e3df9045babe6df8",
        "a real scope compiled by 1.0.0 (permissive-gate compatibility input)"),
    "tests/fixtures/permissive/unknown-family-1.0.0.json": (
        "6a37d5e6d608221d4da364443a5a89609b77fc4086ee38c7af48b43a12eb5870",
        "a real scope compiled by 1.0.0 (permissive-gate compatibility input)"),
    "tests/fixtures/render/earlier-release-config.yaml": (
        "ec78c46ef06cf4dd94b68c29ed8c3beffea9d0243ca8fdebbf4975bb0876f5c2",
        "a gateway configuration an earlier release rendered (render compatibility input)"),
    "tests/fixtures/catalog32-models.json": (
        "8f3cab2c132dea9b46eecc7060f35a745ba1be28940e49d90cd9bee6b918192e",
        "the frozen catalog-32 models file the fixture catalog recipe reads"),
    "tests/fixtures/service/reference.service": (
        "e0fd6b823ce01ad478d71d47751fdb46e82ab2747bb95afcc6d31bcdae3871e9",
        "the historical reference gateway unit (reference-charmer.service is its translation)"),
    "tests/fixtures/service/reference-failure.service": (
        "a17132cfe5939908da65df1e74105abc969de9d5a8e6a5ef55a4213a3b6bb2ae",
        "the historical reference failure unit (reference-charmer-failure.service is its translation)"),
}

# Exact spans of a file (path -> [(span, reason)]): each span occurs, and
# every old spelling in the file lies inside one of them.
SPANS = {
    "src/charmer/data/catalog/native-contract.json": [
        (f"{OLD}-* ids are unknown", "U17: a client check that was run, recorded as observed"),
        (f"deepwork-{OLD}-durable-session-config-phase0-2.1.292", "the evidence id of a run lifecycle probe"),
    ],
    "tests/fixtures/assets/catalog/native-contract.json": [
        (f"while {OLD}-* Anthropic ids are unknown", "the fixture mirror of the recorded U17 note"),
        (f"deepwork-{OLD}-durable-session-config-phase0-2.1.281", "the fixture mirror of a recorded evidence id"),
    ],
    "tests/_log_secrets.py": [
        (f"[selector.go:40] model={OLD}-qwen38-max-xhigh provider=qwen",
         "a gateway log line an earlier release wrote: the redactor keeps it"),
        (f'"{OLD}-proxy: gateway instance 0123456789abcdef starting"',
         "the earlier gateway start line: the redactor keeps it"),
        (f"API server started on 127.0.0.1:18329 ({OLD}-render-0123abcd)",
         "the earlier render sentinel in a log line: the redactor keeps it"),
    ],
}

# Exact blocks (path -> [(first line, last line, reason)]): every old
# spelling of the block is allowed; each block occurs once.
BLOCKS = {
    "tests/test_catalog.py": [
        ("FROZEN_2X_SELECTORS = {\n", "\n}\n",
         "the catalog-32 selectors recorded sessions name: served through continuity, never renamed"),
    ],
    "tests/test_scope_v2.py": [
        ("    PREVIOUS = (\n", "\n    )\n",
         "the /cm skill bytes earlier releases compiled, an independent copy of charmer.legacy's"),
    ],
}

# The release notes' sections (``## `` headings) that may name the old
# product: the released ones, verbatim, and 2.0.0's record of the rename.
CHANGELOG = "CHANGELOG.md"
CHANGELOG_SECTIONS = ("## [2.0.0] ", "## [1.1.0] ", "## [1.0.0] ")

# The retired map keeps the old selectors as the real wire names of retired
# lines (its ``retired.*.selectors`` keys) and maps each renamed live
# selector from its old name (its ``renamed`` keys); nothing else in it.
RETIRED = "src/charmer/data/catalog/retired.json"
RETIRED_SELECTOR_KEYS = 16
RENAMED_KEYS = 31


def occurrences(data: bytes) -> list[tuple[int, int]]:
    """Every (start, end) of an old spelling in ``data``."""

    found = []
    for spelling in SPELLINGS:
        start = data.find(spelling)
        while start >= 0:
            found.append((start, start + len(spelling)))
            start = data.find(spelling, start + 1)
    return sorted(found)


def line_of(data: bytes, offset: int) -> int:
    return data.count(b"\n", 0, offset) + 1


def retired_spans(data: bytes) -> list[tuple[int, int]]:
    document = json.loads(data)
    keys = [selector for entry in document["retired"].values() for selector in entry["selectors"]
            if selector.startswith(legacy.SELECTOR_PREFIX)]
    renamed = list(document.get("renamed", {}))
    assert len(keys) == RETIRED_SELECTOR_KEYS, f"{len(keys)} retired selector keys under the old prefix"
    assert len(renamed) == RENAMED_KEYS, f"{len(renamed)} renamed selector keys"
    spans = []
    for key in sorted(set(keys + renamed)):
        token = json.dumps(key).encode() + b":"
        start = data.find(token)
        assert start >= 0, key
        while start >= 0:
            spans.append((start, start + len(token)))
            start = data.find(token, start + 1)
    return spans


def changelog_spans(data: bytes) -> list[tuple[int, int]]:
    spans, start, offset = [], None, 0
    for line in data.split(b"\n"):
        if line.startswith(b"## "):
            if start is not None:
                spans.append((start, offset))
            start = offset if line.decode().startswith(CHANGELOG_SECTIONS) else None
        offset += len(line) + 1
    if start is not None:
        spans.append((start, len(data)))
    return spans


def allowed_spans(rel: str, data: bytes) -> list[tuple[int, int]]:
    spans = []
    for span, _reason in SPANS.get(rel, ()):
        token = span.encode()
        start = data.find(token)
        assert start >= 0, f"{rel}: allowlisted span is gone: {span!r}"
        while start >= 0:
            spans.append((start, start + len(token)))
            start = data.find(token, start + 1)
    for first, last, _reason in BLOCKS.get(rel, ()):
        begin = data.find(first.encode())
        assert begin >= 0 and data.count(first.encode()) == 1, f"{rel}: block {first!r} is not there once"
        end = data.find(last.encode(), begin)
        assert end >= 0, f"{rel}: block {first!r} has no end"
        spans.append((begin, end + len(last)))
    if rel == CHANGELOG:
        spans += changelog_spans(data)
    if rel == RETIRED:
        spans += retired_spans(data)
    return spans


class NameCensusTests(unittest.TestCase):
    def files(self) -> dict[str, bytes]:
        return {path.relative_to(REPO_ROOT).as_posix(): path.read_bytes() for path in repository_files()}

    def test_no_old_name_outside_the_allowlist(self) -> None:
        stray = []
        for rel, data in sorted(self.files().items()):
            if rel == LEGACY_MODULE or rel in HISTORICAL_FILES:
                continue
            found = occurrences(data)
            if not found:
                continue
            spans = allowed_spans(rel, data)
            for start, end in found:
                if not any(low <= start and end <= high for low, high in spans):
                    text = data[max(0, start - 30):end + 30].decode("utf-8", "replace").replace("\n", " ")
                    stray.append(f"{rel}:{line_of(data, start)}: …{text}…")
        self.assertEqual(stray, [], "the earlier name outside the census allowlist")

    def test_no_path_names_the_old_product(self) -> None:
        self.assertEqual([rel for rel in self.files() if occurrences(rel.encode())], [])

    def test_historical_files_keep_their_bytes(self) -> None:
        files = self.files()
        for rel, (digest, reason) in sorted(HISTORICAL_FILES.items()):
            with self.subTest(rel=rel):
                self.assertTrue(reason)
                self.assertIn(rel, files)
                self.assertEqual(hashlib.sha256(files[rel]).hexdigest(), digest, f"{rel} is history: {reason}")
                self.assertTrue(occurrences(files[rel]), f"{rel} no longer needs its allowlist entry")

    def test_every_allowlisted_span_is_present_and_needed(self) -> None:
        files = self.files()
        for rel in sorted({*SPANS, *BLOCKS, RETIRED}):
            with self.subTest(rel=rel):
                self.assertIn(rel, files)
                spans = allowed_spans(rel, files[rel])
                for low, high in spans:
                    self.assertTrue(occurrences(files[rel][low:high]), f"{rel}: a span without the old name")
        # The release notes: every listed section is there, and 2.0.0's
        # records the rename (a released section need not name the product).
        sections = changelog_spans(files[CHANGELOG])
        self.assertEqual(len(sections), len(CHANGELOG_SECTIONS))
        first = files[CHANGELOG][sections[0][0]:sections[0][1]]
        self.assertTrue(first.startswith(CHANGELOG_SECTIONS[0].encode()))
        self.assertIn(f"Renamed from {OLD} to charmer".encode(), first)
        for rel, entries in [*SPANS.items(), *BLOCKS.items()]:
            for entry in entries:
                self.assertTrue(entry[-1], f"{rel}: every allowlist entry gives its reason")

    def test_only_the_legacy_module_spells_the_old_name_in_the_package(self) -> None:
        package = {rel for rel, data in self.files().items()
                   if rel.startswith("src/") and not rel.startswith("src/charmer/data/") and occurrences(data)}
        self.assertEqual(package, {LEGACY_MODULE})

    def test_the_census_is_not_vacuous(self) -> None:
        for spelling in legacy.SPELLINGS:
            self.assertEqual(len(occurrences(f"x {spelling} y".encode())), 1, spelling)
        self.assertEqual(occurrences(b"charmer, claude, Claude Code, CLAUDE_CODE_X, cm-lead"), [])
        notes = (b"# Changelog\n\n## [2.0.0] \xe2\x80\x94 x\n" + OLD.encode() + b"\n## [9.9.9] \xe2\x80\x94 y\n"
                 + OLD.encode() + b"\n")
        spans = changelog_spans(notes)
        found = occurrences(notes)
        self.assertEqual([any(low <= s and e <= high for low, high in spans) for s, e in found], [True, False])
