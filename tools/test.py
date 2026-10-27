#!/usr/bin/env python3
"""Run the charmer test suite from a checkout, without Nix.

  python3 tools/test.py                      the whole suite
  python3 tools/test.py tests.test_trust     named modules (also test_trust, tests/test_trust.py)
  python3 tools/test.py --tier fast -k fork  quicker iteration, name filter
  python3 tools/test.py --list               what would run on this platform

What it guarantees on every platform (Python 3.11 or newer):

- The live-gateway tripwire is armed in this process (importing the
  ``tests`` package does that) and in Python children inheriting PYTHONPATH
  and loading ``sitecustomize`` (not ``-I``/``-E``/``-S`` children or
  non-Python binaries). Fixture network namespaces isolate other network
  traffic. The guard refuses ports 8317/8316 on every host and covered
  listener/service operations; any hit fails the run. Children never read
  endpoint metadata; the private-HOME runner does not capture the caller's
  configured custom port.
- The suite sees a private temporary HOME and TMPDIR, and no XDG or
  ``CLAUDE_CONFIG_DIR`` overrides, so it never reads or writes your own
  charmer or Claude Code state (``--home inherit`` opts out).
- Platform markers: a test module may declare ``PLATFORMS = ("linux",)``
  (any of linux, darwin, windows). It is read without importing the module,
  and on other platforms the module is listed as not run, never imported.
- An empty selection fails; ``--require PATTERN`` makes matching tests
  mandatory (a skip fails the run) and ``--max-skip-ratio`` bounds skips.
- The keyed host lanes need an explicit candidate gateway. Without
  ``CHARMER_TEST_CLI_PROXY_API``, both keyed modules are reported as not
  runnable before test imports (full or fast tier), and the run fails.
  ``CHARMER_TEST_KEYED_LANE=unit`` explicitly selects no keyed proof.
- The real-client lanes need the pinned Claude Code, which the private HOME
  does not have: ``--claude PATH`` verifies a file against the packaged pin
  and puts a copy in the private HOME's charmer location (the
  product's own acquisition). A lane that needs it (the keyed client lane
  once ``CHARMER_TEST_CLI_PROXY_API`` names a gateway) without it is
  reported as not runnable, with the reason, and the run fails.
- Two control clients are named by path, never copied: the previous
  release's pin (``CHARMER_TEST_PREVIOUS_CLAUDE``, the downgrade-resume
  control) and an older fallback control
  (``CHARMER_TEST_FALLBACK_CONTROL_CLAUDE``). Each named one must be an
  absolute path to that exact build (the sha256 recorded in
  ``tests/_client_roles.py``), checked before any test runs; a wrong one is a
  usage error. Without one, its control is reported unavailable or skipped.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLATFORM_NAMES = ("linux", "darwin", "windows")
MIN_PYTHON = (3, 11)
CLEARED_ENV = ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR",
               "CLAUDE_CONFIG_DIR", "CHARMER_ASSETS", "CHARMER_MANAGED_ID", "CLAUDECODE",
               "CLAUDE_PID", "DBUS_SESSION_BUS_ADDRESS", "PYTHONHOME", "PYTHONSTARTUP")
# Every charmer runtime variable is cleared too, except the runner's own:
# its lane selections and the private identifier input (a path the hygiene
# and history gates read; without it they run their generic layer alone).
RUNTIME_PREFIX, TEST_PREFIX = "CHARMER_", "CHARMER_TEST_"
KEPT_RUNTIME = ("CHARMER_PRIVATE_IDENTIFIERS",)
EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2
# Test modules that need the pinned Claude Code once the named variable is
# set (it selects their real-client lane).
PINNED_CLIENT_LANES = {"tests.test_keyed_compat_client": "CHARMER_TEST_CLI_PROXY_API",
                       "tests.test_openai_key_route_client": "CHARMER_TEST_CLI_PROXY_API"}
KEYED_MODULES = ("tests.test_gateway_keyed_compat", "tests.test_keyed_compat_client")


class UsageError(Exception):
    pass


def current_platform() -> str:
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform in ("win32", "cygwin"):
        return "windows"
    return sys.platform


def module_platforms(path: Path) -> tuple[str, ...] | None:
    """The module's ``PLATFORMS`` marker, read with ``ast`` (never imported)."""

    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PLATFORMS" for t in node.targets):
            try:
                value = ast.literal_eval(node.value)
            except ValueError as exc:
                raise UsageError(f"{path.name}: PLATFORMS must be a literal tuple of names") from exc
            if (not isinstance(value, (tuple, list)) or not value
                    or any(name not in PLATFORM_NAMES for name in value)):
                raise UsageError(f"{path.name}: PLATFORMS must name some of {', '.join(PLATFORM_NAMES)}")
            return tuple(value)
    return None


def _module_name(raw: str) -> str:
    text = raw.replace("\\", "/")
    if text.endswith(".py"):
        text = text[:-3]
    text = text.replace("/", ".")
    if not text.startswith("tests."):
        text = "tests." + text
    if not re.fullmatch(r"tests\.test_\w+", text):
        raise UsageError(f"not a test module: {raw}")
    return text


def select_modules(tests_dir: Path, names: list[str], platform_name: str) -> tuple[list[str], list[tuple[str, tuple[str, ...]]]]:
    if names:
        modules = sorted(dict.fromkeys(_module_name(name) for name in names))
    else:
        modules = [f"tests.{path.stem}" for path in sorted(tests_dir.glob("test_*.py"))]
    selected: list[str] = []
    excluded: list[tuple[str, tuple[str, ...]]] = []
    for module in modules:
        path = tests_dir / f"{module.split('.', 1)[1]}.py"
        if not path.is_file():
            raise UsageError(f"no such test module: {module}")
        platforms = module_platforms(path)
        if platforms is not None and platform_name not in platforms:
            excluded.append((module, platforms))
        else:
            selected.append(module)
    return selected, excluded


def guard_source(tests_dir: Path) -> str:
    """Bootstrap the same stdlib-only tripwire before child test imports.

    Embed its source so a child needs neither the tests package nor product
    imports. Register the canonical module name: a later ``import tests``
    shares its hit record and idempotent installation.
    """

    path = tests_dir / "_tripwire.py"
    source = path.read_text(encoding="utf-8")
    return ("import sys, types\n"
            "if '_tripwire' not in sys.modules:\n"
            "    guard = types.ModuleType('_tripwire')\n"
            f"    exec(compile({source!r}, {str(path)!r}, 'exec'), guard.__dict__)\n"
            "    sys.modules['_tripwire'] = guard\n"
            "sys.modules['_tripwire'].install(read_configured=False)\n")


def legacy_environment(repo: Path) -> tuple[str, ...]:
    """The variables the pre-rename commands exported (``charmer.legacy.ENVIRONMENT``),
    read with ``ast`` (never imported): a shell started inside an old session
    must not leak them into a run."""

    path = repo / "src" / "charmer" / "legacy.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "ENVIRONMENT" for t in node.targets):
            return tuple(ast.literal_eval(node.value))
    raise UsageError(f"{path} defines no ENVIRONMENT")


def control_clients(repo: Path) -> dict[str, tuple[str, str]]:
    """The control clients' variables and their recorded ``(version,
    sha256)`` (``CONTROL_CLIENTS`` in ``tests/_client_roles.py``), read with
    ``ast`` (never imported: the runner has not isolated its environment yet)."""

    path = repo / "tests" / "_client_roles.py"
    if not path.is_file():
        return {}  # a checkout without real-client lanes names no control
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "CONTROL_CLIENTS" for t in node.targets):
            value = ast.literal_eval(node.value)
            if (isinstance(value, dict) and value
                    and all(isinstance(name, str) and name.startswith(TEST_PREFIX)
                            and isinstance(identity, tuple) and len(identity) == 2 for name, identity in value.items())):
                return value
            raise UsageError(f"{path}: CONTROL_CLIENTS must map {TEST_PREFIX}* names to (version, sha256)")
    raise UsageError(f"{path} defines no CONTROL_CLIENTS")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as reader:
        while chunk := reader.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def check_control_clients(repo: Path, environ: dict[str, str] | os._Environ) -> list[str]:
    """Check every control client the environment names (an absolute path to
    a regular file at its recorded sha256); return what each one is."""

    lines = []
    for variable, (version, sha256) in control_clients(repo).items():
        named = environ.get(variable)
        if not named:
            continue
        if not os.path.isabs(named):
            raise UsageError(f"{variable} must be an absolute path (Claude Code {version}); it is {named!r}")
        path = Path(os.path.realpath(named))
        if not path.is_file():
            raise UsageError(f"{variable} names {named}, which is not a regular file (Claude Code {version})")
        if _file_sha256(path) != sha256:
            raise UsageError(f"{variable} names {named}, which is not the Claude Code {version} build the "
                             f"tests record (sha256 {sha256[:12]}…)")
        lines.append(f"control client {variable}: Claude Code {version} ({named})")
    return lines


def runtime_environment(environ: dict[str, str] | os._Environ) -> tuple[str, ...]:
    """charmer's own runtime variables in ``environ`` (``CHARMER_*``): a shell
    inside a session, or an operator's, must not leak them into a run. The
    runner's lane selections (``CHARMER_TEST_*``) and the private identifier
    input (:data:`KEPT_RUNTIME`) stay."""

    return tuple(sorted(name for name in environ
                        if name.startswith(RUNTIME_PREFIX) and not name.startswith(TEST_PREFIX)
                        and name not in KEPT_RUNTIME))


def isolated_environment(work: Path, *, repo: Path, home: str = "temp", tier: str = "full",
                         environ: dict[str, str] | os._Environ | None = None) -> dict[str, str]:
    """Build the runner/probe environment without mutating the caller's.

    Only an explicit ``home='inherit'`` opts out of the private HOME. Probes
    always create one here and pass it to the runner with that opt-out so
    the operator-state probe's seeded HOME is the one its tests inspect.
    """

    env = dict(os.environ if environ is None else environ)

    guard_dir = work / "guard"
    guard_dir.mkdir()
    (guard_dir / "sitecustomize.py").write_text(guard_source(repo / "tests"), encoding="utf-8")
    guard_log = work / "guard.log"
    for name in (*CLEARED_ENV, *legacy_environment(repo), *runtime_environment(env),
                 *(name for name in env if name.startswith(("XDG_", "CLAUDE_CODE_", "ANTHROPIC_")))):
        env.pop(name, None)
    tmp = work / "tmp"
    tmp.mkdir(mode=0o700)
    for name in ("TMPDIR", "TMP", "TEMP"):
        env[name] = str(tmp)
    if home == "temp":
        private_home = work / "home"
        private_home.mkdir(mode=0o700)
        env["HOME"] = str(private_home)
        if current_platform() == "windows":
            env["USERPROFILE"] = str(private_home)
    env["CM_ISOLATION_GUARD_LOG"] = str(guard_log)
    env["CM_TEST_TIER"] = tier
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = os.pathsep.join([str(guard_dir), str(repo / "src"), str(repo / "tests")])
    return env


def prepare_environment(work: Path, *, repo: Path, home: str, tier: str) -> Path:
    """Install the shared environment before importing any test module."""

    env = isolated_environment(work, repo=repo, home=home, tier=tier)
    os.environ.clear()
    os.environ.update(env)
    tempfile.tempdir = None  # re-read TMPDIR
    for entry in (str(repo / "tests"), str(repo / "src"), str(repo)):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    return Path(env["CM_ISOLATION_GUARD_LOG"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tools/test.py", description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("modules", nargs="*", help="test modules (default: every tests/test_*.py)")
    parser.add_argument("--tier", choices=("full", "fast"), default="full",
                        help="fast skips the real pinned-client classes (iteration only)")
    parser.add_argument("--home", choices=("temp", "inherit"), default="temp",
                        help="temp (default) gives the suite a private HOME")
    parser.add_argument("--platform", default=None, help=argparse.SUPPRESS)
    parser.add_argument("-k", dest="patterns", action="append", default=[], help="only tests whose name contains this")
    parser.add_argument("--require", action="append", default=[], metavar="PATTERN",
                        help="this regex must match selected test ids, and matching tests must not skip")
    parser.add_argument("--max-skip-ratio", type=float, default=None, help="fail when skipped/ran exceeds this")
    parser.add_argument("--failfast", action="store_true")
    parser.add_argument("--list", action="store_true", help="print the selection and exit")
    parser.add_argument("--keep", action="store_true", help="keep the private HOME/TMPDIR for inspection")
    parser.add_argument("--claude", type=Path, metavar="PATH",
                        help="the pinned Claude Code (verified, then copied into the private HOME) "
                             "for the real-client lanes")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    if sys.version_info < MIN_PYTHON:
        print(f"tools/test.py needs Python {'.'.join(map(str, MIN_PYTHON))} or newer", file=sys.stderr)
        return EXIT_USAGE
    platform_name = args.platform or current_platform()
    try:
        args.require = [re.compile(pattern) for pattern in args.require]
    except re.error as exc:
        print(f"tools/test.py: invalid --require pattern: {exc}", file=sys.stderr)
        return EXIT_USAGE
    try:
        selected, excluded = select_modules(REPO / "tests", args.modules, platform_name)
    except UsageError as exc:
        print(f"tools/test.py: {exc}", file=sys.stderr)
        return EXIT_USAGE
    for module, platforms in excluded:
        print(f"not run on {platform_name}: {module} (PLATFORMS = {', '.join(platforms)})")
    if args.list:
        print("\n".join(selected))
        return EXIT_OK
    if not selected:
        print("tools/test.py: nothing selected on this platform", file=sys.stderr)
        return EXIT_USAGE
    try:
        control_lines = check_control_clients(REPO, os.environ)
    except UsageError as exc:
        print(f"tools/test.py: {exc}", file=sys.stderr)
        return EXIT_USAGE
    for line in control_lines:
        print(line, flush=True)
    not_runnable = []
    if (os.environ.get("CHARMER_TEST_KEYED_LANE", "host") == "host"
            and not os.environ.get("CHARMER_TEST_CLI_PROXY_API")):
        for module in KEYED_MODULES:
            if module in selected:
                selected.remove(module)
                not_runnable.append(f"{module} needs an explicit candidate gateway for the keyed host lane; "
                                    "set CHARMER_TEST_CLI_PROXY_API=PATH to a candidate build. "
                                    "For unit-only iteration, CHARMER_TEST_KEYED_LANE=unit selects no keyed proof")
    if (args.tier == "fast" and os.environ.get("CHARMER_TEST_CLI_PROXY_API")
            and os.environ.get("CHARMER_TEST_KEYED_LANE", "host") == "host"
            and "tests.test_keyed_compat_client" in selected):
        selected.remove("tests.test_keyed_compat_client")
        not_runnable.append("tests.test_keyed_compat_client with a candidate gateway needs --tier full or "
                            "CHARMER_TEST_KEYED_LANE=unit (no keyed proof)")
    if args.claude is None and args.home == "temp" and args.tier != "fast":
        for module, variable in PINNED_CLIENT_LANES.items():
            if (module in selected and os.environ.get(variable)
                    and not (module == "tests.test_keyed_compat_client"
                             and os.environ.get("CHARMER_TEST_KEYED_LANE") == "unit")):
                selected.remove(module)
                not_runnable.append(f"{module} needs the pinned Claude Code ({variable} selects its real-client "
                                    "lane) and the private HOME has none; pass --claude PATH (a file matching the "
                                    "packaged pin)")
    for reason in not_runnable:
        print(f"not runnable: {reason}", flush=True)
    args.not_runnable = not_runnable
    if not selected:
        print("tools/test.py: nothing runnable is selected", file=sys.stderr)
        return EXIT_FAILED
    work = Path(tempfile.mkdtemp(prefix="charmer-tests-"))
    os.chmod(work, 0o700)
    try:
        return _run(args, selected, work)
    except UsageError as exc:
        print(f"tools/test.py: {exc}", file=sys.stderr)
        return EXIT_USAGE
    finally:
        if args.keep:
            print(f"kept {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


def place_pinned_client(repo: Path, source: Path) -> str:
    """Verify ``source`` against the packaged pin and copy it into the
    (private) HOME's owned location, as ``setup --step claude --claude-from``
    does; returns what was placed."""

    from charmer import acquire, errors, strict_json

    contract = strict_json.load(repo / "src" / "charmer" / "data" / "catalog" / "native-contract.json")
    try:
        outcome = acquire.acquire(contract, {"HOME": os.environ["HOME"], "PATH": ""}, claude_from=source.resolve())
    except (OSError, errors.CharmerError) as exc:
        raise UsageError(f"--claude {source}: {exc}") from exc
    return f"Claude Code {outcome.version} ({outcome.platform}) placed in the private HOME"


def selected_test_ids(suite: unittest.TestSuite) -> list[str]:
    """Capture the inventory before unittest consumes the nested suites."""

    ids = []
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            ids.extend(selected_test_ids(test))
        else:
            ids.append(test.id())
    return ids


def _run(args: argparse.Namespace, selected: list[str], work: Path) -> int:
    guard_log = prepare_environment(work, repo=REPO, home=args.home, tier=args.tier)
    if args.claude is not None:
        print(place_pinned_client(REPO, args.claude))
    os.chdir(REPO)
    import tests  # noqa: F401  (arms the in-process tripwire)

    tripwire = sys.modules.get("_tripwire")
    if tripwire is None:
        raise UsageError("importing the tests package did not arm the tripwire")
    loader = unittest.TestLoader()
    if args.patterns:
        loader.testNamePatterns = [f"*{pattern}*" for pattern in args.patterns]
    suite = loader.loadTestsFromNames(selected)
    test_ids = selected_test_ids(suite)
    if not test_ids:
        print("tools/test.py: the selection holds no tests", file=sys.stderr)
        return EXIT_USAGE
    unmatched = [pattern.pattern for pattern in args.require
                 if not any(pattern.search(test_id) for test_id in test_ids)]
    if unmatched:
        for pattern in unmatched:
            print(f"FAILED: --require {pattern!r} matched no selected tests", file=sys.stderr)
        return EXIT_FAILED
    keyed_ids = [test_id for test_id in test_ids if test_id.startswith("tests.test_keyed_compat_client.")]
    if keyed_ids and (args.patterns or any(_module_name(name) == "tests.test_keyed_compat_client"
                                          for name in args.modules)
                      or any(pattern.search(test_id) for pattern in args.require for test_id in keyed_ids)):
        os.environ["CHARMER_TEST_KEYED_REQUIRED"] = "1"
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2 if args.verbose else 1, failfast=args.failfast).run(suite)
    elapsed = time.monotonic() - started
    problems: list[str] = [f"not runnable: {reason}" for reason in getattr(args, "not_runnable", [])]
    if not result.wasSuccessful():
        problems.append(f"{len(result.failures)} failures, {len(result.errors)} errors")
    hits = list(getattr(tripwire, "HITS", []))
    if hits:
        problems.append(f"the in-process tripwire refused {len(hits)} live-gateway connects")
    if guard_log.exists() and guard_log.read_text(encoding="utf-8", errors="replace").strip():
        problems.append(f"a child process tried the live gateway (see {guard_log}; rerun with --keep)")
    skipped = [(test.id(), reason) for test, reason in result.skipped]
    skipped_ids = {test_id for test_id, _ in skipped}
    for test_id, _ in skipped:
        fixture = re.fullmatch(r"(?:setUpClass|setUpModule) \(([^)]+)\)", test_id)
        if fixture is not None:
            prefix = fixture.group(1) + "."
            skipped_ids.update(selected_id for selected_id in test_ids if selected_id.startswith(prefix))
    for pattern in args.require:
        missed = sorted(test_id for test_id in skipped_ids if pattern.search(test_id))
        if missed:
            problems.append(f"required tests were skipped ({pattern.pattern}): {', '.join(missed[:5])}")
    if args.max_skip_ratio is not None and result.testsRun and len(skipped) > args.max_skip_ratio * result.testsRun:
        problems.append(f"{len(skipped)} of {result.testsRun} tests skipped (more than {args.max_skip_ratio:.0%})")
    reasons: dict[str, int] = {}
    for _test_id, reason in skipped:
        key = reason.split(":", 1)[0] if reason.startswith("BOUNDARY") else reason[:60]
        reasons[key] = reasons.get(key, 0) + 1
    print(f"\nran {result.testsRun} tests in {elapsed:.1f}s on {current_platform()} "
          f"(tier {args.tier}, HOME {args.home}); skipped {len(skipped)}")
    for reason, count in sorted(reasons.items(), key=lambda item: -item[1]):
        print(f"  skipped {count}: {reason}")
    for problem in problems:
        # A test may leave sys.stderr replaced; runner failures must remain visible.
        print(f"FAILED: {problem}", file=sys.__stderr__)
    return EXIT_FAILED if problems else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
