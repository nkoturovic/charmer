#!/usr/bin/env python3
"""Measure the gateway series against a full, existing upstream clone.

    python3 -I tools/upstream_delta.py --clone PATH --target v8.0.33

Standard library only, Python >= 3.11. No upstream code is executed. Only
--fetch permits network access (and updates the clone's refs); otherwise the
clone and checkout are read-only. Exports, rewritten patches and rejects live
in a temporary directory, removed on success and failure. --out is the only
persistent output. Rejects are data, not a failing measurement.

Watch facts are static hints, not a runtime route/egress audit: literal route
registrations (including literal groups and constant prefixes), URL literals,
and the Claude/Codex model catalogs. Dynamic Handle registrations count once.
Totals describe cumulative results, or isolated results with --mode isolated.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Loading the trusted sibling tool must not create __pycache__ in the checkout.
_SPEC = importlib.util.spec_from_file_location("_upstream_delta_build", Path(__file__).with_name("build.py"))
build = importlib.util.module_from_spec(_SPEC)
_bytecode_setting = sys.dont_write_bytecode
try:
    sys.dont_write_bytecode = True
    _SPEC.loader.exec_module(build)
finally:
    sys.dont_write_bytecode = _bytecode_setting
git_environment = build.git_environment
OFFSET_REPORT = build.OFFSET_REPORT
CONTEXT_REDUCED = build.CONTEXT_REDUCED

STABLE_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
OFFSET = re.compile(r"^Hunk #(\d+) succeeded at \d+(?: \(offset ([+-]?\d+) lines?\))?")
GO_TOKEN = re.compile(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|`[^`]*`|\'(?:\\.|[^\'\\])*\'', re.DOTALL)
GO_STRING = r'(?:"(?:\\.|[^"\\])*"|`[^`]*`)'
EXPRESSION = rf'(?:{GO_STRING}|\w+)(?:\s*\+\s*(?:{GO_STRING}|\w+))*'
ROUTE = re.compile(rf'([\w.]+)\.(GET|POST|PUT|PATCH|DELETE|Any|Handle)\(\s*({EXPRESSION})\s*,')
GROUP = re.compile(rf'(\w+)\s*:?=\s*([\w.]+)\.Group\(\s*({EXPRESSION})\s*\)')
CONSTANT = re.compile(rf'\bconst\s+(\w+)\s*=\s*({GO_STRING})')
URL = re.compile(r'https?://[^\s"`<>\\]+')


class DeltaError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def git(directory: Path, *args: str, network: bool = False, ok: tuple[int, ...] = (0,),
        input: bytes | None = None) -> subprocess.CompletedProcess:
    env = git_environment(directory)
    env.update(GIT_TERMINAL_PROMPT="0", GIT_NO_LAZY_FETCH="1", GIT_OPTIONAL_LOCKS="0")
    if not network:
        env["GIT_ALLOW_PROTOCOL"] = "file"
    command = ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false",
               "-c", "gc.auto=0", "-c", "maintenance.auto=false", "-C", str(directory), *args]
    result = subprocess.run(command, env=env, input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode not in ok:
        raise DeltaError(3, f"git {args[0]} failed: {result.stderr.decode('utf-8', 'replace').strip()}")
    return result


def text(directory: Path, *args: str) -> str:
    return git(directory, *args).stdout.decode("utf-8", "replace")


def resolve(clone: Path, ref: str, *, optional: bool = False) -> str | None:
    result = git(clone, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}", ok=(0, 128))
    if result.returncode:
        if optional:
            return None
        raise DeltaError(3, f"cannot resolve commit {ref!r} in clone")
    return result.stdout.decode().strip()


def validate_clone(clone: Path) -> None:
    if not clone.is_dir():
        raise DeltaError(3, "--clone must be an existing full local clone")
    git(clone, "rev-parse", "--git-dir")
    partial = git(clone, "config", "--get-regexp", r"^(extensions\.partialclone|remote\..*\.promisor)$", ok=(0, 1))
    if partial.returncode == 0:
        raise DeltaError(3, "partial clone refused: archive must never lazily fetch blobs")
    if text(clone, "rev-parse", "--is-shallow-repository").strip() != "false":
        raise DeltaError(3, "shallow clone refused: review and release history need a full clone")


def load_recipe(repo: Path) -> tuple[dict, dict, list[dict], list[dict], dict | None]:
    try:
        recipe = build.load_json(repo / "gateway" / "UPSTREAM.json")
        build.check_upstream(recipe)
        series = build.admitted_series(recipe)
        build._require(series == recipe["series"] and all(e["admitted"] is True for e in series),
                       "series must be the ordered admitted patches")
        review = build.load_json(repo / "gateway" / "upstream-review.json")
        build._require(review["format"] == 1, "unsupported review format")
        reviewed = review["upstream"]["reviewed_through"]
        build._require(isinstance(reviewed, dict) and set(reviewed) == {"version", "commit"},
                       "reviewed_through needs exactly a version and a commit")
        for point in (recipe["upstream"], reviewed):
            build._require(isinstance(point["version"], str) and bool(point["version"].strip()), "missing review/pin version")
            build._require(re.fullmatch(r"[0-9a-f]{40}", point["commit"]) is not None, "invalid review/pin commit")
        build._require(isinstance(recipe["upstream"]["module"], str) and bool(recipe["upstream"]["module"]),
                       "missing upstream module")
        for fix in review["fixes"]:
            build._require(bool(fix["commits"]), "empty reviewed commit list")
            for commit in fix["commits"]:
                build._require(re.fullmatch(r"[0-9a-f]{8,40}", commit) is not None, "invalid reviewed commit")
        candidates = recipe["candidates"]
        modules = recipe["vendor"].get("modules_patch")
        for directory, entries in (("patches", series + ([modules] if modules else [])), ("candidates", candidates)):
            for entry in entries:
                path = repo / "gateway" / directory / entry["basename"]
                build._require(path.resolve().parent == (repo / "gateway" / directory).resolve(),
                               "patch escapes its directory")
                build._require(build.sha256_file(path) == entry["sha256"], f"{entry['basename']}: sha256 mismatch")
        return recipe, review, series, candidates, modules
    except (build.BuildError, OSError, KeyError, TypeError, ValueError, AttributeError) as exc:
        raise DeltaError(4, str(exc)) from exc


def rewrite_patch(repo: Path, entry: dict, directory: str, work: Path, old: str, new: str) -> tuple[Path, int]:
    try:
        original = (repo / "gateway" / directory / entry["basename"]).read_text(encoding="utf-8")
        rewritten, count = (original, 0) if old == new else re.subn(re.escape(old) + r'(?=[/"])', lambda _: new, original)
        path = work / entry["basename"]
        path.write_text(rewritten, encoding="utf-8")
        return path, count
    except (OSError, UnicodeError) as exc:
        raise DeltaError(4, f"{entry['basename']}: {exc}") from exc


def parse_apply(output: str) -> tuple[list[dict], list[dict], int]:
    """Offsets are exactly the lines the build refuses, including zero offsets.

    Git reports offsets during 'Checking patch' and rejects in the later
    'Applying patch ... with N rejects' pass; those have independent file
    headers. Do not assign all rejects to the last file checked.
    """
    rejected, offsets, reduced = [], [], 0
    current = ""
    for line in output.splitlines():
        match = re.match(r"^Checking patch (.+)\.\.\.$", line)
        applied = re.match(r"^(?:Applied|Applying) patch (.+?) (?:cleanly|with (?:conflicts|\d+ rejects?))\.*$", line)
        if match:
            current = match.group(1)
        elif applied:
            current = applied.group(1)
        if CONTEXT_REDUCED.search(line):
            reduced += 1
        if OFFSET_REPORT.search(line):
            moved = OFFSET.match(line)
            if moved:
                offsets.append({"file": current, "hunk": int(moved[1]), "offset": int(moved[2] or 0)})
        failed = re.match(r"^Rejected hunk #(\d+)\.$", line)
        if failed:
            rejected.append({"file": current, "hunk": int(failed[1])})
    return rejected, offsets, reduced


def measure_patch(tree: Path, pristine: Path, entry: dict, patch: Path, position: int, rewrites: int) -> dict:
    result = git(tree, "apply", "--reject", "--verbose", "--unidiff-zero", str(patch), ok=(0, 1, 128))
    rejected, offsets, reduced = parse_apply((result.stdout + result.stderr).decode("utf-8", "replace"))
    # Files removed/renamed upstream may fail before git emits a Rejected hunk
    # line. 'clean' must still be false; rejected counts only reported hunks.
    for reject in tree.rglob("*.rej"):
        if reject.is_file() or reject.is_symlink():
            reject.unlink()
    already = False
    if result.returncode:
        reverse = git(pristine, "apply", "--check", "--reverse", "--unidiff-zero", str(patch), ok=(0, 1, 128))
        already = reverse.returncode == 0
    return {"position": position, "basename": entry["basename"], "sha256": entry["sha256"],
            "rewrites": rewrites, "rejected": len(rejected), "rejected_hunks": rejected,
            "offsets": len(offsets), "offset_hunks": offsets, "context_reduced": reduced,
            "clean": result.returncode == 0 and not offsets and not reduced, "already_upstream": already}


def requires(go_mod: str) -> set[str]:
    lines, block = set(), False
    for line in go_mod.splitlines():
        value = line.strip()
        if value.startswith("require ("):
            block = True
        elif block and value == ")":
            block = False
        elif block and value and not value.startswith("//"):
            lines.add(value)
        elif value.startswith("require "):
            lines.add(value[len("require "):])
    return lines


def stable_tags(clone: Path, target: str) -> list[str]:
    return sorted((tag for tag in text(clone, "tag", "--merged", target).splitlines() if STABLE_TAG.fullmatch(tag)),
                  key=lambda tag: (tuple(map(int, STABLE_TAG.fullmatch(tag).groups())), tag))


def non_test(path: str) -> bool:
    return (not path.endswith("_test.go") and not Path(path).name.startswith("test_")
            and not {"test", "tests", "testdata"}.intersection(Path(path).parts))


def review_gap(clone: Path, start: str, target: str, review: dict, tags: list[str]) -> dict:
    commits = text(clone, "rev-list", "--no-merges", "--reverse", f"{start}..{target}").splitlines()
    prefixes = sorted({commit for fix in review["fixes"] for commit in fix["commits"]})
    releases, previous = {}, None
    for tag in tags:
        revision = tag if previous is None else f"{previous}..{tag}"
        for commit in text(clone, "rev-list", "--no-merges", revision, "--").splitlines():
            releases.setdefault(commit, tag)
        previous = tag
    unreviewed = []
    for commit in commits:
        if any(commit.startswith(prefix) for prefix in prefixes):
            continue
        files = text(clone, "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "-z", commit, "--").split("\0")
        unreviewed.append({"commit": commit[:8], "release": releases.get(commit, "unreleased"),
                           "subject": text(clone, "show", "-s", "--format=%s", commit).strip(),
                           "files": sorted(path for path in files if path and non_test(path))})
    outside, unknown = [], []
    for prefix in prefixes:
        commit = resolve(clone, prefix, optional=True)
        if commit is None:
            unknown.append(prefix)
        elif git(clone, "merge-base", "--is-ancestor", commit, target, ok=(0, 1)).returncode:
            outside.append(prefix)
    return {"from": start, "to": target, "commits_total": len(commits), "reviewed_count": len(commits) - len(unreviewed),
            "unreviewed": unreviewed, "fixes_outside_range": outside, "fixes_unknown": unknown}


def without_comments(source: str) -> str:
    return GO_TOKEN.sub(lambda m: " " if m[0].startswith(("//", "/*")) else m[0], source)


def string_value(expression: str, constants: dict) -> str | None:
    parts = re.findall(GO_STRING + r"|\w+", expression)
    values = [part[1:-1] if part.startswith(('"', '`')) else constants.get(part) for part in parts]
    return "".join(values) if values and all(value is not None for value in values) else None


def routes(source: str) -> set[tuple[str, str]]:
    source = without_comments(source)
    constants = {match[1]: match[2][1:-1] for match in CONSTANT.finditer(source)}
    groups = {}
    for match in GROUP.finditer(source):
        value = string_value(match[3], constants)
        if value is not None:
            groups[match[1]] = groups.get(match[2], "") + value
    result = set()
    for match in ROUTE.finditer(source):
        receiver, method, expression = match.groups()
        if method == "Handle":
            # Handle(method, "path", handler), not Handle("path", handler).
            rest = re.match(rf'\s*({EXPRESSION})\s*,', source[match.end():])
            expression = rest[1] if rest else expression
        value = string_value(expression, constants)
        if value is not None and value.startswith("/"):
            result.add((method, groups.get(receiver, "") + value))
    return result


def watch_snapshot(clone: Path, commit: str) -> tuple[set, set, dict]:
    paths = text(clone, "ls-tree", "-r", "--name-only", "-z", commit).split("\0")
    route_set, urls, registry = set(), set(), {}
    # grep narrows the files read, without evaluating the clone's diff drivers.
    found = git(clone, "grep", "-I", "-l", "-z", "-e", r"https\?://", "-e", r"\.\(GET\|POST\|PUT\|PATCH\|DELETE\|Any\|Handle\)(",
                commit, "--", "*.go", ok=(0, 1))
    for name in found.stdout.decode("utf-8", "replace").split("\0"):
        if not name:
            continue
        path = name.split(":", 1)[1]
        if not non_test(path):
            continue
        source = text(clone, "show", f"{commit}:{path}")
        if Path(path).parent.as_posix() == "internal/api":
            route_set.update(routes(source))
        for token in GO_TOKEN.finditer(source):
            if token[0].startswith(('"', '`')):
                urls.update(URL.findall(token[0][1:-1]))
    for basename in ("models.json", "codex_client_models.json"):
        path = "internal/registry/models/" + basename
        if path not in paths:
            continue
        document = json.loads(text(clone, "show", f"{commit}:{path}"))
        sections = ({"codex_client_models": document.get("models", [])} if basename == "codex_client_models.json"
                    else {key: rows for key, rows in document.items() if key == "claude" or key.startswith("codex-")})
        for section, rows in sections.items():
            registry[section] = {row.get("id", row.get("slug")): row for row in rows}
    return route_set, urls, registry


def watch(clone: Path, pin: str, target: str) -> dict:
    before_routes, before_urls, before_registry = watch_snapshot(clone, pin)
    after_routes, after_urls, after_registry = watch_snapshot(clone, target)
    registry = {}
    for section in sorted(before_registry.keys() | after_registry.keys()):
        before, after = before_registry.get(section, {}), after_registry.get(section, {})
        changed = {}
        for identifier in sorted(before.keys() & after.keys()):
            if before[identifier] != after[identifier]:
                changed[identifier] = sorted(key for key in before[identifier].keys() | after[identifier].keys()
                                             if (key in before[identifier]) != (key in after[identifier])
                                             or before[identifier].get(key) != after[identifier].get(key))
        registry[section] = {"added": sorted(after.keys() - before.keys()), "removed": sorted(before.keys() - after.keys()),
                             "changed": changed}
    return {"routes_added": [{"method": method, "path": path} for method, path in sorted(after_routes - before_routes)],
            "routes_removed": [{"method": method, "path": path} for method, path in sorted(before_routes - after_routes)],
            "management_routes": {version: sum(path == f"/{version}/management" or path.startswith(f"/{version}/management/")
                                                for _, path in after_routes) for version in ("v0", "v8")},
            "url_literals_added": sorted(after_urls - before_urls), "registry": registry}


def measure(args: argparse.Namespace, work: Path) -> dict:
    clone, repo = args.clone.resolve(), args.repo.resolve()
    recipe, review, series, candidates, modules = load_recipe(repo)
    validate_clone(clone)
    if args.fetch:
        git(clone, "fetch", "--tags", "--prune", "origin", network=True)
        validate_clone(clone)
    target = resolve(clone, args.target)
    pin = resolve(clone, recipe["upstream"]["commit"])
    start = resolve(clone, review["upstream"]["reviewed_through"]["commit"])
    target_mod = text(clone, "show", f"{target}:go.mod")
    module = re.search(r'^module\s+([^\s]+)', target_mod, re.MULTILINE)
    if module is None:
        raise DeltaError(3, "target go.mod has no module line")
    old, new = recipe["upstream"]["module"], module[1].strip('"')
    archive = work / "target.tar"
    archive.write_bytes(git(clone, "archive", "--format=tar", target).stdout)
    pristine = work / "pristine"
    try:
        build.safe_extract(archive, pristine, strip=0)
    except build.BuildError as exc:
        raise DeltaError(3, str(exc)) from exc
    if (pristine / ".git").exists():
        raise DeltaError(3, "export contains .git")
    cherry_picks = []
    for ref in args.cherry_pick:
        commit = resolve(clone, ref)
        patch = git(clone, "format-patch", "-1", "--stdout", commit).stdout
        result = git(pristine, "apply", "--unidiff-zero", "-", input=patch, ok=(0, 1, 128))
        if result.returncode:
            raise DeltaError(5, f"cherry-pick {commit} conflicts: {result.stderr.decode('utf-8', 'replace').strip()}")
        cherry_picks.append({"commit": commit, "subject": text(clone, "show", "-s", "--format=%s", commit).strip()})
    rewritten = {}
    for directory, entries in (("patches", series + ([modules] if modules else [])), ("candidates", candidates)):
        for entry in entries:
            rewritten[entry["basename"]] = rewrite_patch(repo, entry, directory, work, old, new)
    module_status = None
    if modules:
        patch, _ = rewritten[modules["basename"]]
        check = git(pristine, "apply", "--check", "--verbose", "--unidiff-zero", str(patch), ok=(0, 1, 128))
        module_status = ("rejects" if check.returncode else
                         "offsets" if OFFSET_REPORT.search((check.stdout + check.stderr).decode()) else "clean")
    old_requires, new_requires = requires(text(clone, "show", f"{pin}:go.mod")), requires(target_mod)
    cumulative, isolated, candidate_results = [], [], []
    tree = work / "series"
    if args.mode in ("cumulative", "both"):
        shutil.copytree(pristine, tree, symlinks=True)
        for position, entry in enumerate(series, 1):
            patch, count = rewritten[entry["basename"]]
            cumulative.append(measure_patch(tree, pristine, entry, patch, position, count))
    for entries, results in ((series if args.mode in ("isolated", "both") else [], isolated),
                             (candidates if args.series == "all" else [], candidate_results)):
        for position, entry in enumerate(entries, 1):
            alone = work / "isolated"
            shutil.copytree(pristine, alone, symlinks=True)
            patch, count = rewritten[entry["basename"]]
            results.append(measure_patch(alone, pristine, entry, patch, position, count))
            shutil.rmtree(alone)
    measured = cumulative if args.mode != "isolated" else isolated
    tags = stable_tags(clone, target)
    exact_tags = sorted(text(clone, "tag", "--points-at", target).splitlines())
    return {"format": 1, "tool": "upstream_delta", "clone": {"path": str(clone), "partial": False, "fetched": args.fetch},
            "pinned": {"version": recipe["upstream"]["version"], "commit": pin},
            "reviewed_through": review["upstream"]["reviewed_through"],
            "target": {"ref": args.target, "commit": target, "tag": (args.target if args.target in exact_tags else
                       next((tag for tag in tags if tag in exact_tags), exact_tags[0] if exact_tags else None)),
                       "committer_date": text(clone, "show", "-s", "--format=%cI", target).strip(), "module": new},
            "cherry_picks": cherry_picks,
            "module_rewrite": {"from": old, "to": new, "replacements": {name: count for name, (_, count) in rewritten.items()}},
            "modules_patch": {"basename": modules["basename"] if modules else None, "applies": module_status,
                              "go_sum_identical": git(clone, "diff", "--quiet", "--no-ext-diff", "--no-textconv", pin, target,
                                                      "--", "go.sum", ok=(0, 1)).returncode == 0,
                              "go_mod_requires_changed": {"added": sorted(new_requires - old_requires),
                                                          "removed": sorted(old_requires - new_requires)}},
            "series": cumulative, "isolated": isolated, "candidates": candidate_results,
            "totals": {"rejected": sum(row["rejected"] for row in measured), "offsets": sum(row["offsets"] for row in measured),
                       "patches_with_rejects": sum(bool(row["rejected"]) for row in measured),
                       "patches_with_offsets": sum(bool(row["offsets"]) for row in measured),
                       "clean_positions": [row["position"] for row in measured if row["clean"]]},
            "review_gap": review_gap(clone, start, target, review, tags), "watch": watch(clone, pin, target)}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    result.add_argument("--clone", type=Path, required=True, help="full local upstream clone (never cloned by this tool)")
    result.add_argument("--target", required=True, help="tag, remote branch or commit")
    result.add_argument("--cherry-pick", action="append", default=[], metavar="SHA")
    result.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    result.add_argument("--series", choices=("admitted", "all"), default="admitted")
    result.add_argument("--mode", choices=("cumulative", "isolated", "both"), default="cumulative")
    result.add_argument("--fetch", action="store_true", help="allow git fetch --tags --prune origin before measuring")
    result.add_argument("--out", default="-", metavar="FILE|-", help="JSON output (default stdout)")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.out != "-":
            output = Path(args.out).resolve()
            if any(output.is_relative_to(root.resolve()) for root in (args.clone, args.repo)):
                raise DeltaError(2, "--out must be outside the clone and recipe checkout")
        with tempfile.TemporaryDirectory(prefix="upstream-delta-") as directory:
            document = measure(args, Path(directory))
            encoded = json.dumps(document, sort_keys=True, indent=1) + "\n"
            if args.out == "-":
                sys.stdout.write(encoded)
            else:
                Path(args.out).write_text(encoded, encoding="utf-8")
        return 0
    except DeltaError as exc:
        print(f"upstream_delta: {exc}", file=sys.stderr)
        return exc.code
    except (OSError, ValueError) as exc:
        print(f"upstream_delta: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
