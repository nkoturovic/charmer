"""The name census: the product's earlier name appears nowhere in the
repository outside an explicit allowlist, and every allowlisted place is
exactly as listed.

charmer was released under an earlier name up to 1.1.0. History keeps that
name: the released release notes, the fixtures that prove compatibility with
what earlier releases wrote, recorded wire names and recorded evidence, and
the guide to moving an installation of the earlier name. Everything current
names charmer. The census searches for every form of the name
(``charmer.legacy.NAME_PATTERN``: any case, any separator, split across a
line, and the half-renamed forms), and each exemption is exact: a whole file
pinned byte for byte, a span with the number of times it occurs, a block
pinned byte for byte, a page with the number of times it names the earlier
product, a released section of the release notes pinned byte for byte, the
exact lines of 2.0.0's record of the rename, and the structural keys of the
retired map. This module spells the old name only through
``charmer.legacy``.
"""

from __future__ import annotations

import hashlib
import json
import re
import unittest
from typing import Mapping

from _layout import REPO_ROOT, repository_files
from charmer import legacy

OLD = legacy.NAME
PATTERN = re.compile(legacy.NAME_PATTERN.encode())

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
    "tests/fixtures/legacy/v1.1.0-tree.json": (
        "ee00c7327a65aa9f82c7f19d787470972278fc929eff4e8b0574c8df25475f8f",
        "the installation 1.1.0 wrote, generated from the v1.1.0 tag (migrate from-legacy input)"),
}

# Exact spans of a file (path -> [(span, occurrences, reason)]): each span
# occurs exactly that many times, and every old spelling in the file lies
# inside one of them.
SPANS = {
    "src/charmer/data/catalog/native-contract.json": [
        (f"{OLD}-* ids are unknown", 1, "U17: a client check that was run, recorded as observed"),
        (f"deepwork-{OLD}-durable-session-config-phase0-2.1.312", 1, "the evidence id of a run lifecycle probe"),
    ],
    "tests/fixtures/assets/catalog/native-contract.json": [
        (f"while {OLD}-* Anthropic ids are unknown", 1, "the fixture mirror of the recorded U17 note"),
        (f"deepwork-{OLD}-durable-session-config-phase0-2.1.281", 1, "the fixture mirror of a recorded evidence id"),
    ],
    "tests/_log_secrets.py": [
        (f"[selector.go:40] model={OLD}-qwen38-max-xhigh provider=qwen", 1,
         "a gateway log line an earlier release wrote: the redactor keeps it"),
        (f'"{OLD}-proxy: gateway instance 0123456789abcdef starting"', 1,
         "the earlier gateway start line: the redactor keeps it"),
        (f"API server started on 127.0.0.1:18329 ({OLD}-render-0123abcd)", 1,
         "the earlier render sentinel in a log line: the redactor keeps it"),
    ],
}

# Exact blocks (path -> [(first line, last line, sha256, reason)]): the
# block occurs once and keeps its bytes; every old spelling inside it is allowed.
BLOCKS = {
    "tests/test_catalog.py": [
        ("FROZEN_2X_SELECTORS = {\n", "\n}\n",
         "2f1880872c44b7cfffb3634eabc1b63a25f66e8368ebecf076903a74cc4bf4b0",
         "the catalog-32 selectors recorded sessions name: kept resolvable, never rewritten"),
    ],
    "tests/test_scope_v2.py": [
        ("    PREVIOUS = (\n", "\n    )\n",
         "0a4f98c3911a3492ce95a171be48680f5c9eef70b82c4a73784fc12ef2ada769",
         "the /cm skill bytes earlier releases compiled, an independent copy of charmer.legacy's"),
    ],
}

# Current pages about the earlier name (path -> (occurrences, reason)):
# documentation, so not pinned byte for byte, but every occurrence is counted.
PAGES = {
    "docs/guides/legacy-migration.md": (
        54, "the guide to moving an installation of the earlier name onto charmer"),
}

# The release notes: the sections of the releases made under the earlier
# name, verbatim (heading prefix -> sha256), and the exact lines of 2.0.0's
# record of the rename, each once inside its section; nothing else names it.
CHANGELOG = "CHANGELOG.md"
RELEASED_SECTIONS = {
    "## [1.1.0] ": "7eaaec2feeef11facbfaff43ce440a3489ff7f70f277cb6915919baf17c9dc77",
    "## [1.0.0] ": "b468eeedc9584a28073ff6fc9603d2f6ee73b48810ab5e0bb61bddc69309793d",
}
RENAME_SECTION = "## [2.0.0] "
RENAME_LINES = (
    f"Renamed from {OLD} to charmer. The program, its commands, files,",
    f"beside an installed {OLD} and never updates it in place;",
    f"- `charmer migrate from-legacy` moves an installation of {OLD} 1.1.0",
    f"  onto charmer in one journaled transaction: it stops the {OLD}",
    f"  {OLD} from any step, and `--purge` removes the retained",
    f"  {OLD} installation tree and caches once the move is complete. It",
    f"- The installer and `charmer setup` notice a {OLD} installation and",
    f"  import` accepts an operator export {OLD} wrote.",
    f"  `{OLD}`, `{OLD}-proxy` and `{OLD}-dev`); the installer",
    f"  (import package `charmer`, formerly `{OLD.replace('-', '_')}`). charmer is not",
    f"  `{OLD}`. The hook shims are `charmer-hook`, `charmer-hook-3` and",
    f"  `charmer-gateway-failure.service` (formerly `{OLD}-gateway…`), still",
    f"- Every `{legacy.ENVIRONMENT_PREFIX}*` environment variable is now `CHARMER_*` with the",
    f"- Catalog 39: the 31 gateway selectors `{OLD}-<model>-<effort>` are",
    f"  an installed 1.0.0: charmer does not install {OLD} releases.",
)

# The retired maps (path -> (retired selector keys under the old prefix,
# renamed keys, the sha256 of their projection)): the old selectors are the
# real wire names of retired lines (``retired.*.selectors`` keys) and the old
# names of renamed live selectors (``renamed`` keys), each exactly once as a
# key; nothing else names them. The projection (``retired_projection``: every
# (retired entry, old selector key) pair and every renamed key, sorted) is
# pinned, so a substituted name fails even when the counts stay.
RETIRED_FILES = {
    "src/charmer/data/catalog/retired.json": (
        16, 31, "786075f01694cbb0257c6d07f719644634754691b6318466f14e132dba1b56d2"),
    "tests/fixtures/assets/catalog/retired.json": (
        0, 8, "ef904ee14713ca226799d929d363dad6364325e210c41b09328f8d7c7d6e7e65"),
}


def occurrences(data: bytes) -> list[tuple[int, int]]:
    """Every (start, end) of a form of the old name in ``data``."""

    return [match.span() for match in PATTERN.finditer(data)]


def line_of(data: bytes, offset: int) -> int:
    return data.count(b"\n", 0, offset) + 1


def sections(data: bytes) -> dict[bytes, tuple[int, int]]:
    """Each ``## `` heading line -> (start, end) of its section."""

    found: dict[bytes, tuple[int, int]] = {}
    heading, start, offset = None, 0, 0
    for line in data.split(b"\n"):
        if line.startswith(b"## "):
            if heading is not None:
                found[heading] = (start, offset)
            heading, start = line, offset
        offset += len(line) + 1
    if heading is not None:
        found[heading] = (start, len(data))
    return found


def section_of(data: bytes, prefix: str) -> tuple[int, int] | None:
    matches = [span for heading, span in sections(data).items() if heading.startswith(prefix.encode())]
    return matches[0] if len(matches) == 1 else None


def changelog_spans(data: bytes) -> tuple[list[tuple[int, int]], list[str]]:
    spans: list[tuple[int, int]] = []
    problems: list[str] = []
    for prefix, digest in RELEASED_SECTIONS.items():
        span = section_of(data, prefix)
        if span is None:
            problems.append(f"{CHANGELOG}: section {prefix!r} is not there once")
            continue
        if hashlib.sha256(data[span[0]:span[1]]).hexdigest() != digest:
            problems.append(f"{CHANGELOG}: the released section {prefix!r} changed (history keeps its bytes)")
        spans.append(span)
    rename = section_of(data, RENAME_SECTION)
    if rename is None:
        return spans, [*problems, f"{CHANGELOG}: section {RENAME_SECTION!r} is not there once"]
    body = data[rename[0]:rename[1]]
    for line in RENAME_LINES:
        token = (line + "\n").encode()
        count = body.count(b"\n" + token)
        if count != 1:
            problems.append(f"{CHANGELOG}: rename line {line!r} occurs {count} times in 2.0.0 (expected once)")
            continue
        start = rename[0] + body.find(b"\n" + token) + 1
        spans.append((start, start + len(token)))
    return spans, problems


def retired_projection(document: Mapping) -> dict:
    """Every (retired entry, old selector key) pair and every renamed key, sorted."""

    return {"retired": sorted([entry, selector] for entry, value in document["retired"].items()
                              for selector in value["selectors"] if PATTERN.search(selector.encode())),
            "renamed": sorted(document.get("renamed", {}))}


def retired_spans(rel: str, data: bytes) -> tuple[list[tuple[int, int]], list[str]]:
    retired_keys, renamed_keys, pinned = RETIRED_FILES[rel]
    document = json.loads(data)
    keys = [selector for entry in document["retired"].values() for selector in entry["selectors"]
            if PATTERN.search(selector.encode())]
    renamed = list(document.get("renamed", {}))
    problems = []
    projection = hashlib.sha256(json.dumps(retired_projection(document), sort_keys=True,
                                           separators=(",", ":")).encode()).hexdigest()
    if projection != pinned:
        problems.append(f"{rel}: its old-name keys are not the pinned ones (projection sha256 {projection}, "
                        f"expected {pinned})")
    if len(keys) != retired_keys:
        problems.append(f"{rel}: {len(keys)} retired selector keys under the old prefix (expected {retired_keys})")
    if len(renamed) != renamed_keys:
        problems.append(f"{rel}: {len(renamed)} renamed selector keys (expected {renamed_keys})")
    structural: dict[str, int] = {}
    for key in keys + renamed:
        structural[key] = structural.get(key, 0) + 1
    spans = []
    for key, expected in sorted(structural.items()):
        token = json.dumps(key).encode() + b":"
        if data.count(token) != expected:
            problems.append(f"{rel}: {key!r} is a key {data.count(token)} times (expected {expected}: "
                            "only where the retired map keeps it)")
        start = data.find(token)
        while start >= 0:
            spans.append((start, start + len(token)))
            start = data.find(token, start + 1)
    return spans, problems


def allowed(rel: str, data: bytes) -> tuple[list[tuple[int, int]], list[str]]:
    """The allowlisted spans of ``rel`` and every way it left its listing."""

    spans: list[tuple[int, int]] = []
    problems: list[str] = []
    for span, expected, _reason in SPANS.get(rel, ()):
        token = span.encode()
        if data.count(token) != expected:
            problems.append(f"{rel}: span {span!r} occurs {data.count(token)} times (expected {expected})")
        start = data.find(token)
        while start >= 0:
            spans.append((start, start + len(token)))
            start = data.find(token, start + 1)
    for first, last, digest, _reason in BLOCKS.get(rel, ()):
        begin = data.find(first.encode())
        end = data.find(last.encode(), begin) if begin >= 0 else -1
        if data.count(first.encode()) != 1 or end < 0:
            problems.append(f"{rel}: block {first!r} is not there once")
            continue
        block = (begin, end + len(last))
        if hashlib.sha256(data[block[0]:block[1]]).hexdigest() != digest:
            problems.append(f"{rel}: block {first!r} changed (it keeps its bytes)")
        spans.append(block)
    if rel in PAGES:
        expected = PAGES[rel][0]
        if expected is not None and len(occurrences(data)) != expected:
            problems.append(f"{rel}: names the earlier product {len(occurrences(data))} times (expected {expected})")
        spans.append((0, len(data)))
    if rel == CHANGELOG:
        found, more = changelog_spans(data)
        spans += found
        problems += more
    if rel in RETIRED_FILES:
        found, more = retired_spans(rel, data)
        spans += found
        problems += more
    return spans, problems


def violations(files: Mapping[str, bytes]) -> list[str]:
    """Every way ``files`` (repository path -> bytes) leaves the census."""

    problems: list[str] = []
    for rel, (digest, _reason) in sorted(HISTORICAL_FILES.items()):
        if rel not in files:
            problems.append(f"{rel}: historical file is gone")
        elif hashlib.sha256(files[rel]).hexdigest() != digest:
            problems.append(f"{rel}: historical file changed (history keeps its bytes)")
        elif not occurrences(files[rel]):
            problems.append(f"{rel}: no longer needs its allowlist entry")
    listed = {*SPANS, *BLOCKS, *PAGES, CHANGELOG, *RETIRED_FILES}
    problems += [f"{rel}: allowlisted file is gone" for rel in sorted(listed - set(files))]
    for rel, data in sorted(files.items()):
        if occurrences(rel.encode()):
            problems.append(f"{rel}: the path names the earlier product")
        if rel == LEGACY_MODULE or rel in HISTORICAL_FILES:
            continue
        found = occurrences(data)
        if not found and rel not in listed:
            continue
        spans, more = allowed(rel, data)
        problems += more
        for start, end in found:
            if not any(low <= start and end <= high for low, high in spans):
                text = data[max(0, start - 30):end + 30].decode("utf-8", "replace").replace("\n", " ")
                problems.append(f"{rel}:{line_of(data, start)}: …{text}…")
        for low, high in spans:
            if not occurrences(data[low:high]) and rel != CHANGELOG:
                problems.append(f"{rel}: an allowlisted span without the earlier name at {line_of(data, low)}")
    return problems


def repository() -> dict[str, bytes]:
    return {path.relative_to(REPO_ROOT).as_posix(): path.read_bytes() for path in repository_files()}


class NameCensusTests(unittest.TestCase):
    def test_the_repository_keeps_the_census(self) -> None:
        self.assertEqual(violations(repository()), [], "the earlier name outside the census allowlist")

    def test_every_allowlist_entry_gives_its_reason(self) -> None:
        for rel, entries in [*SPANS.items(), *BLOCKS.items()]:
            for entry in entries:
                self.assertTrue(entry[-1], rel)
        for rel, (_digest, reason) in [*HISTORICAL_FILES.items(), *PAGES.items()]:
            self.assertTrue(reason, rel)

    def test_only_the_legacy_module_spells_the_old_name_in_the_package(self) -> None:
        package = {rel for rel, data in repository().items()
                   if rel.startswith("src/") and not rel.startswith("src/charmer/data/") and occurrences(data)}
        self.assertEqual(package, {LEGACY_MODULE})

    def test_the_pattern_finds_every_form_and_nothing_current(self) -> None:
        # Built from the old name (this module never spells it): its two
        # words, and the half-renamed forms a careless replace leaves.
        first, second = OLD.split("-")
        forms = [*legacy.SPELLINGS, OLD.upper(), OLD.title(), OLD.replace("-", " "), OLD.replace("-", " ").title(),
                 OLD.replace("-", "").upper(), legacy.LEGACY_NIX_ATTRIBUTES[0], OLD.upper().replace("_", "-"),
                 OLD.replace("-", "-\n"), OLD.replace("-", "-\n    # "), OLD.replace("-", "\n// "),
                 OLD.replace("-", " \n  > "), f"charmer-{second}", f"charmer_{second}".title(),
                 f"{first}-charmer", f"{first} charmer".upper()]
        for form in forms:
            with self.subTest(form=form):
                self.assertEqual(len(occurrences(f"x {form} y".encode())), 1)
        for current in ("charmer", "claude", "Claude Code", "CLAUDE_CODE_X", "claude-opus-5-5[1m]", "gpt-multi-sol",
                        "CHARMER_MANAGED_ID", "CharmerError", "cm-lead", "multi-provider Claude", "Claude, multi",
                        "~/.claude", "CLAUDE_CONFIG_DIR", "charmer-hook-3"):
            with self.subTest(current=current):
                self.assertEqual(occurrences(current.encode()), [])


class NameCensusMutationTests(unittest.TestCase):
    """Each exemption is exact: a change that slips the old name past it fails."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.files = repository()

    def mutated(self, rel: str, change) -> list[str]:
        files = dict(self.files)
        files[rel] = change(files[rel])
        return violations(files)

    def test_the_repository_starts_clean(self) -> None:
        self.assertEqual(violations(self.files), [])

    def test_an_old_name_sentence_in_the_rename_section(self) -> None:
        def add(data: bytes) -> bytes:
            start, _end = section_of(data, RENAME_SECTION)
            heading_end = data.index(b"\n", start) + 1
            return data[:heading_end] + f"\nThe {OLD} pages moved.\n".encode() + data[heading_end:]

        self.assertTrue(self.mutated(CHANGELOG, add))

    def test_an_allowlisted_rename_line_edited(self) -> None:
        line = RENAME_LINES[0].encode()
        self.assertTrue(self.mutated(CHANGELOG, lambda data: data.replace(line, line + f" ({OLD})".encode(), 1)))

    def test_a_released_section_edited(self) -> None:
        def edit(data: bytes) -> bytes:
            start, end = section_of(data, "## [1.0.0] ")
            return data[:end - 1] + b" " + data[end - 1:]

        self.assertTrue(self.mutated(CHANGELOG, edit))

    def test_a_fabricated_frozen_selector(self) -> None:
        first = BLOCKS["tests/test_catalog.py"][0][0].encode()
        fabricated = f'    "{legacy.LEGACY_SELECTOR_PREFIX}fabricated": ("x", "y", None),\n'.encode()
        self.assertTrue(self.mutated("tests/test_catalog.py", lambda data: data.replace(first, first + fabricated, 1)))

    def test_the_u17_note_elsewhere(self) -> None:
        rel = "src/charmer/data/catalog/native-contract.json"
        span = SPANS[rel][0][0].encode()
        self.assertTrue(self.mutated(rel, lambda data: data + b"\n" + span + b"\n"))

    def test_a_retired_key_in_an_unrelated_object(self) -> None:
        rel = "src/charmer/data/catalog/retired.json"
        document = json.loads(self.files[rel])
        key = next(selector for entry in document["retired"].values() for selector in entry["selectors"]
                   if selector.startswith(legacy.LEGACY_SELECTOR_PREFIX))
        token = json.dumps({"note": {key: 1}})[1:-1].encode() + b", "
        self.assertTrue(self.mutated(rel, lambda data: data.replace(b"{", b"{" + token, 1)))

    def test_an_edit_after_an_allowlisted_span(self) -> None:
        rel = "tests/_log_secrets.py"
        span = SPANS[rel][0][0].encode()
        self.assertTrue(self.mutated(rel, lambda data: data.replace(span, span + f" {OLD}".encode(), 1)))

    def test_another_form_in_a_current_file(self) -> None:
        for form in (OLD.replace("-", " ").title(), OLD.upper().replace("_", "-"), OLD.replace("-", "-\n# ")):
            with self.subTest(form=form):
                self.assertTrue(self.mutated("README.md", lambda data: data + f"\n{form}\n".encode()))

    # One bounded separator joins every pair: nothing, spaces or tabs around
    # a hyphen or underscore, or one line break (a comment marker allowed).
    # The words come from charmer.legacy, so this module never spells a form.
    FIRST, SECOND = OLD.split("-")
    NEW = "charmer"
    FORMS = (f"{NEW.title()}{SECOND.title()}", f"{FIRST.title()}{NEW.title()}", f"{FIRST}\t{SECOND}",
             f"{FIRST}  {SECOND}", f"{NEW}\t-\t{SECOND}", f"{FIRST.title()}_\n# {NEW.title()}",
             f"{NEW}\n// {SECOND}", f"{FIRST.upper()} -\n> {SECOND.upper()}")
    # Legitimate upstream and product names, and words of separate sentences.
    NOT_FORMS = (f"{FIRST}-code", f"{FIRST.title()} Code", f"{NEW}-gateway", f"{SECOND}-provider",
                 f"{FIRST}\n\n{SECOND}", f"{FIRST}     {SECOND}", f"{NEW}.\n{SECOND.title()}")

    def test_every_separator_form_is_found_by_the_census_and_r16(self) -> None:
        from _docs_vocab import former_name

        rule = former_name()
        for form in self.FORMS:
            with self.subTest(form=form):
                self.assertTrue(self.mutated("README.md", lambda data: data + f"\n{form}\n".encode()))
                self.assertIsNotNone(rule.search(form))
        for name in self.NOT_FORMS:
            with self.subTest(name=name):
                self.assertEqual(self.mutated("README.md", lambda data: data + f"\n{name}\n".encode()), [])
                self.assertIsNone(rule.search(name))

    def test_a_retired_key_substituted_at_the_same_count(self) -> None:
        rel = "src/charmer/data/catalog/retired.json"
        real = json.dumps(f"{legacy.LEGACY_SELECTOR_PREFIX}deepseek-pro-high[1m]").encode()
        fabricated = json.dumps(f"{legacy.LEGACY_SELECTOR_PREFIX}fabricated-never-released").encode()
        self.assertEqual(self.files[rel].count(real), 1)
        problems = self.mutated(rel, lambda data: data.replace(real, fabricated))
        self.assertTrue(any("not the pinned ones" in problem for problem in problems), problems)
        renamed = json.dumps(f"{legacy.LEGACY_SELECTOR_PREFIX}qwen-flash-low[1m]").encode()
        problems = self.mutated(rel, lambda data: data.replace(renamed, fabricated))
        self.assertTrue(any("not the pinned ones" in problem for problem in problems), problems)

    def test_a_historical_file_edited(self) -> None:
        rel = "tests/fixtures/legacy/v1.1.0-tree.json"
        self.assertTrue(self.mutated(rel, lambda data: data + b"\n"))

    def test_a_counted_page_names_it_once_more(self) -> None:
        for rel, (expected, _reason) in PAGES.items():
            if expected is None or rel not in self.files:
                continue
            with self.subTest(rel=rel):
                self.assertTrue(self.mutated(rel, lambda data: data + f"\n{OLD}\n".encode()))


if __name__ == "__main__":
    unittest.main()
