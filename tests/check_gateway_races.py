#!/usr/bin/env python3
"""Gateway concurrency diagnostics. Observation only.

Builds tests/gateway-race.nix (a SEPARATE -race diagnostic derivation, never
the shipped gateway) from git-tracked files, then runs labelled scenarios:

- one service-level scenario per race pair under real-shaped traffic (the
  production builder path, rendered fixture config, fake upstream replies,
  accepted reloads, independent auth-file updates), plus unit controls;
- the shared Claude credential metadata pair (b108) separately labelled:
  direct-executor (the retained upstream tests and a setup-token overlay);
  manager/HTTP with synthetic file-backed Claude setup-token credentials (the
  planned observation: the credential document stays in Metadata through
  normal cloning and persistence); and, as an additional explicitly labelled
  case, manager/HTTP with config-synthesized credentials whose empty
  Metadata clones as nil;
- a rejected-reload control proving that a reload workload the gateway did
  not accept yields an inconclusive observation, never a bounded negative.

The admitted series carries the Claude metadata-locking patch, so the series
build is the only one observed.

A bounded negative needs every required scenario of a pair to have executed
its own workload (scenario-specific prerequisites, e.g. accepted
post-readiness reloads with concurrent traffic) and reached BOTH sides of the
pair's target path in that same workload (never a union of scenarios): for reload scenarios, execution counts above the same
build's startup-only baseline, so startup execution cannot stand in for the
reload phase. Otherwise the observation is recorded as incomplete
(inconclusive). A genuine detector reproduction is always retained.

D217's explicit --matrix-runs 100 uses 100 independent invocations per cell,
with every required root executed once in each invocation. Inner workloads
stay fixed (accepted reloads: 20 writes; rejected reloads: one write; both
B108 HTTP shapes: 8 credentials x 16 requests). Normal nonmatrix bounds stay
compatible. Incomplete attempts stop the cell and are retained, never retried
away; an incomplete matrix cannot publish a valid observation report.

Every executable runs through probe.start_isolated_process (bwrap
--unshare-net); the fake upstream is a unix socket. No provider calls, no live
state, no activation. A negative bounded run means "not reproduced within the
bound", never "race absent". Not part of the main check or the ordinary
harness budget; durations are reported. Evidence: <out>/races.json (0700/0600).
"""
from __future__ import annotations

import argparse
import copy
import datetime
import http.client
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import uuid

# Run as a script, the tests directory is sys.path[0]; imported, it is on
# PYTHONPATH. Either way _layout is the one root source.
from _layout import NIX_DIR, REPO_ROOT, SERVICE_SPEC  # noqa: E402

sys.path[:0] = [str(REPO_ROOT), str(REPO_ROOT / "src"), str(REPO_ROOT / "tests")]
from _catalog import FIXTURE_GATEWAY_TOKEN, FIXTURE_ROOT
from charmer import catalog, probe, render, state
import _gateway_harness as harness

SCHEMA = "gwtest-races-v1"
PINNED_GO_MODULES = harness.PINNED_GO_MODULES
NEGATIVE = "not reproduced within the bound"
INCOMPLETE = "workload incomplete (inconclusive)"
FILE_AUTH_PREFIX = "gwtest-b108-file-"
FILE_ROUTE = "gwtest-b108-file"
PAIRS = {
    "pair1": "ReconcileRegistryModelStates vs Auth.Clone",
    "pair2": "UpdateClientsContext vs handlers",
    "pair3": "SetPluginHost vs interceptorHost",
    "b108": "shared Claude credential metadata",
}
# Target-path execution: every group needs at least one executed target.
PAIR_TARGETS = {
    "pair1": (("reconcile_write",), ("clone_after_unlock",)),
    "pair2": (("config_write",), ("unified_models", "heartbeat")),
    "pair3": (("set_plugin_host",), ("interceptor_host",)),
    "b108": (("setup_token",), ("executor_prepare",)),
}
RETAINED_B108 = ("TestClaudeExecutorPrepareRequestAuthIsRaceFreeOnSharedCredential",
                 "TestClaudeExecutorSharedCredentialMetadataMixedAccess",
                 "TestClaudeExecutorSharedCredentialMetadataReadersUseOneLock")
RETAINED_PLUGIN_HOST = ("TestGatewayPluginHostConcurrentModelInterception",
                        "TestGatewayPluginHostConcurrentExecutorLookup",
                        "TestGatewayPluginHostTypedNilAndReentrantCallback")
BINARIES = ("executor", "auth", "api", "service", "handlers")
ALL_PAIRS = ("pair1", "pair2", "pair3")
B108_KINDS = ("direct-executor", "manager-http", "manager-http-config-empty-metadata")
# id -> (label, pairs observed, pairs whose bounded negative REQUIRES it, builds)
SCENARIOS = {
    "unit-pair1-reconcile-clone": ("unit", ("pair1",), ("pair1",), ("series",)),
    "unit-pairs23-reload-handlers": ("unit", ("pair2", "pair3"), ("pair2", "pair3"), ("series",)),
    "unit-plugin-host-retained": ("unit", ("pair3",), ("pair3",), ("series",)),
    "direct-b108-setup-token": ("direct-executor", ("b108",), ("b108",), ("series",)),
    "direct-b108-retained-upstream": ("direct-executor", ("b108",), ("b108",), ("series",)),
    # Startup overlap is an additional reader for pairs 2/3 (reproductions
    # count); their negative needs the post-readiness reload workload.
    "service-startup-overlap": ("service", ALL_PAIRS, ("pair1",), ("series",)),
    "service-reload-traffic": ("service", ALL_PAIRS, ALL_PAIRS, ("series",)),
    "control-rejected-reload": ("control", ALL_PAIRS, ALL_PAIRS, ("series",)),
    "manager-http-b108-file-auth": ("manager-http", ("b108",), ("b108",), ("series",)),
    "manager-http-b108-config-empty-metadata": ("manager-http-config-empty-metadata", ("b108",), ("b108",),
                                                ("series",)),
}
MATRIX_RUNS = 100
MATRIX_UNIT_ROOTS = {
    "unit-pair1-reconcile-clone": ("TestGatewayPair1ReconcileCloneUnit",),
    "unit-pairs23-reload-handlers": ("TestGatewayReloadHandlersUnit",),
    "unit-plugin-host-retained": RETAINED_PLUGIN_HOST,
    "direct-b108-setup-token": ("TestGatewayB108DirectSetupToken",),
    "direct-b108-retained-upstream": RETAINED_B108,
}
TARGET_LABELS = frozenset(("reconcile", "reconcile_write", "update_internal", "clone_after_unlock",
                          "update_clients", "config_write", "set_plugin_host", "interceptor_host",
                          "unified_models", "heartbeat", "setup_token", "executor_prepare",
                          "profile_fetch", "manager_prepare"))
GORACE = "halt_on_error=0 atexit_sleep_ms=0 history_size=2"
SECRETS = (FIXTURE_GATEWAY_TOKEN, "dummy-gwtest", "sk-ant-oat", "gwtesthint-")
RACE_START = "WARNING: DATA RACE"
KEEP_LOGS: Path | None = None
FRAMES_KEPT = 16
ACCESS = re.compile(r"^(Read|Write|Previous read|Previous write|Atomic read|Atomic write|"
                    r"Previous atomic read|Previous atomic write) at 0x[0-9a-f]+ by .+:$")
FRAME = re.compile(r"^  (\S.*)\(\)$")


# ---------------------------------------------------------------- parsing

def parse_races(text: str) -> list[dict]:
    """Race-detector reports as function frames only (no paths or values)."""
    reports = []
    for block in text.split(RACE_START)[1:]:
        block = block.split("\n==================", 1)[0]
        accesses, current = [], None
        for line in block.splitlines():
            if ACCESS.match(line):
                current = {"access": line.split(" at 0x", 1)[0].lower(), "frames": []}
                accesses.append(current)
            elif current is not None and FRAME.match(line):
                current["frames"].append(FRAME.match(line).group(1))
            elif current is not None and not line.strip():
                current = None
            elif line.startswith("Goroutine "):
                break
        if len(accesses) >= 2:
            reports.append({"accesses": [a["access"] for a in accesses[:2]],
                            "frames": [a["frames"] for a in accesses[:2]]})
        else:
            reports.append({"accesses": [], "frames": [], "unparsed": True})
    return reports


# The accessed memory is a Claude credential's Metadata map when an access's
# own top frames are the claude metadata helpers or the executor's unlocked
# setup-token reads (deeper executor frames alone say nothing about the map).
CLAUDE_METADATA = re.compile(r"CLIProxyAPI/v8/internal/auth/claude\.|executor\.(isClaudeSetupToken|claudeCreds|getCloakConfigFromAuth)$")
TOP_FRAMES = 3
HANDLER_SIDE = re.compile(r"CLIProxyAPI/v8/(internal/api|sdk/api/handlers)[./]")


def classify(report: dict) -> str:
    """Stack-signature classes. pair1-3/b108 name the race pairs; the extra
    classes keep related detector findings visible instead of hiding them."""
    frames = report.get("frames") or []
    if len(frames) != 2:
        return "unparsed"
    a, b = ("\n".join(side) for side in frames)

    def pair(x, y):
        return (x in a and y in b) or (x in b and y in a)
    if pair("ReconcileRegistryModelStates", "(*Auth).Clone"):
        return "pair1"
    if pair("SetPluginHost", "interceptorHost"):
        return "pair3"
    if ("(*Server).UpdateClientsContext" in a and HANDLER_SIDE.search(b)
            or "(*Server).UpdateClientsContext" in b and HANDLER_SIDE.search(a)):
        return "pair2"
    # b108: either access is made directly by a Claude metadata helper (or the
    # executor's unlocked flag readers) on the shared credential map.
    if any(CLAUDE_METADATA.search(frame) for side in frames for frame in side[:TOP_FRAMES]):
        return "b108"
    # Same unlocked post-publication Clone reader as pair1, other writers.
    if "(*Auth).Clone" in a + b and "sdk/cliproxy/auth.(*Manager)." in a + b:
        return "auth-clone-other-writer"
    return "other"


def fatal_messages(lines: list[str]) -> list[tuple[int, str]]:
    """(line index, message) per "fatal error:"; concurrent service logging can
    split the runtime's message onto one of the next lines."""
    found = []
    for index, line in enumerate(lines):
        if not line.startswith("fatal error: "):
            continue
        message = line[len("fatal error: "):]
        if not message.startswith("concurrent map"):
            message = next((later for later in lines[index + 1:index + 6] if later.startswith("concurrent map")),
                           message[:120])
        found.append((index, message))
    return found


def parse_fatal(text: str) -> list[dict]:
    """Go runtime concurrency crashes (e.g. concurrent map read and map
    write): the message and the crashing goroutine's function frames."""
    crashes, lines = [], text.splitlines()
    for index, message in fatal_messages(lines):
        if not message.startswith("concurrent map"):
            continue
        frames = []
        for start in range(index + 1, len(lines)):
            if re.match(r"^goroutine \d+ .*\[running\]:$", lines[start]):
                for frame in lines[start + 1:]:
                    if not frame.strip():
                        break
                    if not frame.startswith(("\t", " ")) and not frame.startswith("created by "):
                        frames.append(frame.rsplit("(", 1)[0] if frame.endswith(")") else frame)
                break
        crash = {"message": message, "frames": frames[:FRAMES_KEPT]}
        # The crashing access is on credential Metadata (the maps.fatal frame first).
        crash["classification"] = "b108" if any(
            CLAUDE_METADATA.search(frame) or re.search(r"auth\.(authMetadataString|accessTokenForFingerprint)$", frame)
            for frame in frames[:TOP_FRAMES + 1]) else "other"
        crashes.append(crash)
    return crashes


def test_outcomes(text: str) -> dict:
    """go test -v results; a race-only failure is an executed scenario.

    With -v, a test's t.Log/t.Error lines (4-space "file.go:N: msg") stream
    between its "=== RUN"/"=== CONT" and "--- FAIL" lines; any trailing ones
    directly after "--- FAIL" count too. Detector frames never match.
    """
    message = re.compile(r"^ {4,}(\S+\.go:\d+: .*)$")
    passed = sorted(set(re.findall(r"^\s*--- PASS: (\S+)", text, re.M)))
    failed, race_only, opened = [], [], {}
    lines = text.splitlines()
    for index, line in enumerate(lines):
        run = re.match(r"^=== (?:RUN|CONT)\s+(\S+)", line)
        if run:
            opened[run.group(1)] = index
            continue
        match = re.match(r"^\s*--- FAIL: (\S+)", line)
        if not match:
            continue
        name = match.group(1)
        window = lines[opened.get(name, index) + 1:index]
        messages = [m.group(1) for m in map(message.match, window) if m]
        for following in lines[index + 1:]:
            found = message.match(following)
            if not found:
                break
            messages.append(found.group(1))
        if messages and all("race detected during execution of test" in m for m in messages):
            race_only.append(name)
        else:
            failed.append(name)
    return {"passed": passed, "race_only_failures": sorted(set(race_only)),
            "skipped": sorted(set(re.findall(r"^\s*--- SKIP: (\S+)", text, re.M))),
            "other_failures": sorted(set(failed)),
            "runtime_fatal": sorted({message for _, message in fatal_messages(lines)}),
            "panics": len(re.findall(r"^panic: ", text, re.M))}


def root_test_counts(text: str) -> dict:
    """Count root attempts/completions, never deduplicate repeated -test.count runs."""
    counts = {}
    for line in text.splitlines():
        started = re.fullmatch(r"=== RUN\s+(Test\w+)\s*", line)
        ended = re.match(r"^--- (PASS|FAIL|SKIP): (Test\w+) \(", line)
        if started or ended:
            name = started.group(1) if started else ended.group(2)
            row = counts.setdefault(name, {"attempted": 0, "passed": 0, "failed": 0, "skipped": 0})
            row["attempted" if started else {"PASS": "passed", "FAIL": "failed", "SKIP": "skipped"}[ended.group(1)]] += 1
    return counts


def load_targets(diagnostics: Path) -> dict:
    targets = {}
    for line in (diagnostics / "share/targets.tsv").read_text().splitlines():
        label, file, first, last = line.split("\t")
        targets[label] = (file, int(first), int(last))
    return targets


def coverage(profile: Path, targets: dict) -> dict:
    """label -> executed, from an atomic -coverprofile (count > 0 overlaps)."""
    hit = {label: False for label in targets}
    if not profile.is_file():
        return {label: None for label in targets}
    prefix = "github.com/router-for-me/CLIProxyAPI/v8/"
    for line in profile.read_text().splitlines()[1:]:
        match = re.fullmatch(r"(\S+):(\d+)\.\d+,(\d+)\.\d+ \d+ (\d+)", line)
        if not match or int(match.group(4)) == 0 or not match.group(1).startswith(prefix):
            continue
        file, first, last = match.group(1)[len(prefix):], int(match.group(2)), int(match.group(3))
        for label, (target_file, start, end) in targets.items():
            if file == target_file and first <= end and last >= start:
                hit[label] = True
    return hit


def coverage_counts(profile: Path, targets: dict) -> dict:
    """label -> highest execution count of an overlapping block (atomic mode)."""
    counts = {label: 0 for label in targets}
    if not profile.is_file():
        return {}
    prefix = "github.com/router-for-me/CLIProxyAPI/v8/"
    for line in profile.read_text().splitlines()[1:]:
        match = re.fullmatch(r"(\S+):(\d+)\.\d+,(\d+)\.\d+ \d+ (\d+)", line)
        if not match or not match.group(1).startswith(prefix):
            continue
        file, first, last = match.group(1)[len(prefix):], int(match.group(2)), int(match.group(3))
        for label, (target_file, start, end) in targets.items():
            if file == target_file and first <= end and last >= start:
                counts[label] = max(counts[label], int(match.group(4)))
    return counts


# Reload-phase execution. A test binary cannot reset its coverage counters
# (runtime/coverage.ClearCounters refuses: the testing package owns the counter
# mode), so a reload process's writer counts are compared with the highest
# count of the same build's startup-only processes: only executions above that
# baseline happened after readiness. Readers count as phase-executed only with
# the phase's own served traffic (the driver sends none before readiness).
PHASE_WRITERS = ("update_clients", "config_write", "set_plugin_host", "reconcile", "reconcile_write",
                 "update_internal", "clone_after_unlock")
PHASE_READERS = ("unified_models", "heartbeat", "interceptor_host")


def startup_baseline(runs: list[dict]) -> dict:
    return {label: max(run.get("target_counts", {}).get(label, 0) for run in runs)
            for label in PHASE_WRITERS} if runs and all(run.get("target_counts") for run in runs) else {}


def phase_path(runs: list[dict], baseline: dict) -> dict:
    def above(run, label):
        count, floor = run.get("target_counts", {}).get(label), baseline.get(label)
        return count is not None and floor is not None and count > floor
    path = {label: bool(runs) and all(above(run, label) for run in runs) for label in PHASE_WRITERS}
    path.update({label: bool(runs) and all(run["target_path"].get(label) is True and served(run, "") > 0
                                           for run in runs) for label in PHASE_READERS})
    return path


def merge_coverage(runs: list[dict]) -> dict:
    merged = {}
    for run in runs:
        for label, value in run.get("target_path", {}).items():
            if value is True or merged.get(label) is True:
                merged[label] = True
            elif value is False or merged.get(label) is False:
                merged[label] = False
            else:
                merged.setdefault(label, None)
    return merged


def target_executed(pair: str, target_path: dict) -> bool:
    return all(any(target_path.get(label) is True for label in group) for group in PAIR_TARGETS[pair])


def pair_verdict(pair: str, scenarios: list[dict]) -> dict:
    """Any scenario's detector report or crash reproduces. A bounded negative
    needs every scenario REQUIRED for the pair to have executed and met its
    own workload prerequisites, and the target path in their coverage."""
    reports = sum(scenario["classified"].get(pair, 0) for scenario in scenarios)
    crashes = sum(scenario.get("runtime_fatal", {}).get(pair, 0) for scenario in scenarios)
    required = [s for s in scenarios if pair in s.get("required_for", s.get("pairs", ()))]
    # Each required scenario must reach BOTH sides of the pair itself (a
    # reload scenario in its own phase, never its startup coverage). A pair
    # assembled by OR-ing disjoint scenarios' coverage is no target path.
    executed = bool(required) and all(
        target_executed(pair, s["phase_path"] if "phase_path" in s else s["target_path"]) for s in required)
    complete = bool(required) and all(s.get("executed") is True and s.get("prerequisites_met") is True
                                      for s in required)
    if not scenarios:
        verdict = "not run"
    elif reports or crashes:
        verdict = "reproduced"
    elif not complete:
        verdict = INCOMPLETE
    elif executed:
        verdict = NEGATIVE
    else:
        verdict = "target path not executed (inconclusive)"
    return {"verdict": verdict, "detector_reports": reports, "runtime_fatal_crashes": crashes,
            "target_path_executed": executed, "workload_complete": complete,
            "required_scenarios": [s["id"] for s in required],
            "scenarios": [scenario["id"] for scenario in scenarios]}


# ------------------------------------------------------------- evidence

def scrub(value, where="evidence"):
    if isinstance(value, dict):
        for key, item in value.items():
            scrub(key, where)
            scrub(item, where)
    elif isinstance(value, list):
        for item in value:
            scrub(item, where)
    elif isinstance(value, str):
        if any(secret in value for secret in SECRETS):
            raise ValueError("credential-shaped value refused in " + where)
    elif value is not None and type(value) not in (int, float, bool):
        raise TypeError("race evidence accepts JSON metadata only")


def validate_report(document: dict) -> None:
    if document.get("schema") != SCHEMA or document.get("observation_only") is not True:
        raise AssertionError("incorrect race evidence identity")
    if document.get("negative_meaning") != NEGATIVE:
        raise AssertionError("a negative bounded run must never read as race absent")
    matrix_runs = document.get("matrix_runs")
    if matrix_runs is not None and (type(matrix_runs) is not int or matrix_runs != MATRIX_RUNS):
        raise AssertionError("matrix mode requires exactly 100 runs")
    if matrix_runs is not None:
        targets = document.get("targets", {}).get("series", {})
        if set(targets) != TARGET_LABELS:
            raise AssertionError("matrix requires all fourteen target extents")
        for extent in targets.values():
            if (not isinstance(extent, (list, tuple)) or len(extent) != 3 or not isinstance(extent[0], str)
                    or type(extent[1]) is not int or type(extent[2]) is not int or not 0 < extent[1] <= extent[2]):
                raise AssertionError("invalid matrix target extent")
    omitted = document.get("omitted", [])
    validate_omissions(omitted)
    expected_patches = [name for name in harness.PATCH_ROWS if name not in omitted]
    builds = document.get("builds", [])
    if not builds or builds[0].get("label") != "series":
        raise AssertionError("missing series build identity")
    for build in builds:
        for name in ("diagnostics", "gateway", "source", "go_modules"):
            if not str(build.get(name, "")).startswith("/nix/store/"):
                raise AssertionError("missing race build identity: " + name)
        # Check the derivation's consumed $goModules and go-modules inputs.
        if build.get("go_modules") != PINNED_GO_MODULES or build.get("go_modules_pinned") is not True \
                or not harness.vendor_pinned(build.get("go_modules_consumed") or {}, PINNED_GO_MODULES):
            raise AssertionError("diagnostic derivation left the pinned vendor modules (F17)")
        if build.get("patches") != expected_patches:
            raise AssertionError("race series does not match the patch inventory")
    labels = [build["label"] for build in builds]
    if labels != ["series"]:
        raise AssertionError("unexpected race build labels")
    scenarios, seen = document.get("scenarios", []), set()
    seen_invocations = set()
    for scenario in scenarios:
        row = SCENARIOS.get(scenario.get("id"))
        if scenario.get("build") not in labels or row is None or scenario.get("label") != row[0] \
                or scenario.get("build") not in row[3]:
            raise AssertionError("unlabelled race scenario: " + str(scenario.get("id")))
        if (scenario["build"], scenario["id"]) in seen:
            raise AssertionError("duplicate race scenario: " + scenario["id"])
        seen.add((scenario["build"], scenario["id"]))
        if list(scenario.get("pairs", ())) != list(row[1]) or list(scenario.get("required_for", ())) != list(row[2]):
            raise AssertionError("race scenario pairs differ from the inventory: " + scenario["id"])
        if not scenario.get("runs") or scenario.get("executed") is not True:
            raise AssertionError("race scenario did not execute: " + str(scenario.get("id")))
        if row[0] in ("service", "control") and "reload" in scenario["id"] and (
                not isinstance(scenario.get("phase_path"), dict) or not scenario.get("startup_baseline")):
            raise AssertionError("reload scenario without reload-phase execution: " + scenario["id"])
        if not isinstance(scenario.get("prerequisites"), dict) or not scenario["prerequisites"] \
                or scenario.get("prerequisites_met") is not all(scenario["prerequisites"].values()):
            raise AssertionError("race scenario prerequisites missing: " + scenario["id"])
        if matrix_runs is not None:
            validate_matrix_cell(scenario)
            for run in scenario["runs"]:
                if run["invocation_id"] in seen_invocations:
                    raise AssertionError("matrix invocation receipt reused across cells")
                seen_invocations.add(run["invocation_id"])
    # The required inventory per build.
    for label in labels:
        for sid, row in SCENARIOS.items():
            if label in row[3] and (label, sid) not in seen:
                raise AssertionError(f"required race scenario missing for {label}: {sid}")
    verdicts = document.get("verdicts", {})
    if set(verdicts) != set(labels):
        raise AssertionError("race verdicts do not match the builds")
    for label in labels:
        ids = {sid for build, sid in seen if build == label}
        required = [(pair, kind) for pair in ALL_PAIRS for kind in ("service", "unit")] if label == "series" else []
        required += [("b108", kind) for kind in B108_KINDS]
        for pair, kind in required:
            verdict = verdicts[label].get(pair, {}).get(kind, {})
            if verdict.get("verdict") in (None, "not run"):
                raise AssertionError(f"missing {label} {kind} verdict for {pair}")
            refs = verdict.get("scenarios") or []
            if not refs or not set(refs) <= ids or not set(verdict.get("required_scenarios") or ["?"]) <= ids:
                raise AssertionError(f"{label} {pair} {kind} verdict names scenarios that did not run")
        if verdicts[label] != verdicts_for(label, scenarios):
            raise AssertionError("stored race verdicts differ from their scenarios: " + label)
    controls = document.get("controls")
    if controls != control_verdicts(scenarios):
        raise AssertionError("rejected-reload control missing or inconsistent")
    for pair, verdict in controls["rejected_reload"].items():
        if verdict["verdict"] == NEGATIVE:
            raise AssertionError("a rejected reload produced a bounded negative for " + pair)
    scrub(document)


def publish(directory: Path, document: dict) -> Path:
    validate_report(document)  # normal, failure-propagating code; never atexit
    state.ensure_private_dir(directory)
    path = directory / "races.json"
    state.atomic_write(path, (json.dumps(document, indent=2, sort_keys=True) + "\n").encode())
    return path


# ---------------------------------------------------------------- builds

def validate_omissions(omitted):
    from check_gateway_patch_revert import omission_closure
    manifest = list(harness.PATCH_ROWS)
    if len(omitted) != len(set(omitted)) or not set(omitted) <= set(manifest):
        raise ValueError("unknown or duplicate omitted patch")
    for patch in omitted:
        if not set(omission_closure(patch, manifest, harness.PATCH_DEPENDENCIES)) <= set(omitted):
            raise ValueError("omissions are not dependency closed")


def build_expression(worktree: Path, omitted=()) -> str:
    from check_gateway_patch_revert import DIAGNOSTIC_INSTALL_PHASE, DIAGNOSTIC_PATCH_PHASE
    validate_omissions(omitted)
    return '''let
      f = builtins.getFlake %s;
      pkgs = f.inputs.nixpkgs.legacyPackages.x86_64-linux;
      original = import "${f}/nix/gateway.nix" { inherit pkgs; };
      omitted = builtins.fromJSON %s;
      keep = p: !(builtins.elem (builtins.baseNameOf (toString p)) omitted);
      # The diagnostics depend on this gateway's output, so a partial series
      # must install: record only, never the shipped-target inspect. Its
      # remaining patches may sit at the omissions' line offsets.
      gateway = if omitted == [ ] then original else original.overrideAttrs (old: {
        patches = builtins.filter keep old.patches;
        doCheck = false;
        %s
        %s
        passthru = old.passthru // { gatewayPatches = builtins.filter keep original.gatewayPatches; };
      });
      diag = import "${f}/tests/gateway-race.nix" { cliProxyApi = gateway; };
    in
      # Never a new fixed-output go-modules derivation, judged by the vendor
      # derivation the build references (not the attribute it set).
      assert import "${f}/tests/vendor-inputs.nix" diag == [ gateway.goModules.drvPath ];
      assert gateway.goModules.outPath == %s;
      diag
    ''' % (json.dumps(str(worktree)), json.dumps(json.dumps(list(omitted))), DIAGNOSTIC_INSTALL_PHASE,
           DIAGNOSTIC_PATCH_PHASE, json.dumps(PINNED_GO_MODULES))


def build(worktree: Path, label: str, omitted=()) -> dict:
    started = time.monotonic()
    expression = build_expression(worktree, omitted)
    evaluated = subprocess.run(["nix", "eval", "--offline", "--impure", "--json", "--expr",
        "let d = " + expression + "; in { drv = d.drvPath; goModules = d.goModules.outPath; }"],
        cwd=worktree, capture_output=True, text=True, timeout=600)
    if evaluated.returncode:
        raise RuntimeError("race derivation evaluation failed:\n" + evaluated.stderr[-4000:])
    identity = json.loads(evaluated.stdout)
    built = subprocess.run(["nix", "build", "--offline", "--no-link", "--print-out-paths", "--impure",
                            "--max-jobs", "1", "--expr", expression],
                           cwd=worktree, capture_output=True, text=True, timeout=3600)
    if built.returncode:
        failed = sorted(set(re.findall(r"/nix/store/[^\s'\"]+\.drv", built.stderr)))
        raise RuntimeError(f"race diagnostics build failed ({label}); derivations: {failed}\n"
                           + built.stderr[-4000:])
    paths = [line for line in built.stdout.splitlines() if line.startswith("/nix/store/")]
    if len(paths) != 1:
        raise RuntimeError("Nix did not return exactly one race diagnostics outPath")
    return describe(Path(paths[0]), label, drv=identity["drv"], seconds=time.monotonic() - started)


def describe(diagnostics: Path, label: str, *, drv=None, seconds=None) -> dict:
    share = diagnostics / "share"
    for name in BINARIES:
        binary = diagnostics / "bin" / ("gwtest-race-" + name)
        if not binary.is_file() or binary.is_symlink() or binary.stat().st_mode & 0o022:
            raise RuntimeError("race diagnostics binary missing or writable: " + name)
    go_modules = (share / "go-modules").read_text().strip()
    # The share file echoes an attribute; what counts is what the derivation consumed.
    consumed = harness.consumed_vendor(drv or diagnostics)
    if not harness.vendor_pinned(consumed, PINNED_GO_MODULES):
        raise RuntimeError("race diagnostics do not consume the pinned vendor modules")
    return {"label": label, "diagnostics": str(diagnostics), "drv": drv, "go_modules_consumed": consumed,
            "gateway": (share / "gateway-outpath").read_text().strip(),
            "source": (share / "gateway-source").read_text().strip(),
            "go_modules": go_modules, "go_modules_pinned": go_modules == PINNED_GO_MODULES,
            "patches": json.loads((share / "gateway-patches.json").read_text()),
            "build_seconds": None if seconds is None else round(seconds, 3)}


# -------------------------------------------------------------- running

def isolated_run(binary: Path, args: list[str], root: Path, *, env=None, timeout=240, targets=None,
                 driver=None, cwd=None, home_view=None, bridges=()) -> dict:
    """One diagnostic process in bwrap --unshare-net, observed as metadata."""
    run_dir = root / "run"
    state.ensure_private_dir(run_dir)
    log_path, profile = root / "probe.log", run_dir / "cover.out"
    started = time.monotonic()
    process, observed = None, {}
    invocation_id, process_error, cleanup_error = uuid.uuid4().hex, None, None
    try:
        with log_path.open("wb") as log:
            process = probe.start_isolated_process(
                [str(binary), "-test.v", f"-test.timeout={timeout}s", "-test.coverprofile=" + str(profile), *args],
                cwd=cwd or root, env={"HOME": str(root), "TMPDIR": str(root), "PATH": "/usr/bin:/bin",
                                      "GORACE": GORACE, **(env or {})},
                home_view=home_view, bridges=list(bridges), stdout=log, stderr=subprocess.STDOUT)
            if driver is not None:
                try:
                    observed = driver(process, log_path) or {}
                except (AssertionError, OSError, ValueError, RuntimeError) as error:
                    # Kept only as a type: a crashed service explains it, else
                    # the scenario is recorded as not executed.
                    observed = {"driver_error": type(error).__name__}
            process.wait(timeout=timeout + 30)
    except (OSError, ValueError, probe.ProbeError, subprocess.SubprocessError) as error:
        process_error = type(error).__name__
    finally:
        if process is not None:
            try:
                probe.stop_isolated_process(process)
            except (OSError, RuntimeError, probe.ProbeError, subprocess.SubprocessError) as error:
                cleanup_error = type(error).__name__
    text = log_path.read_text(errors="replace") if log_path.is_file() else ""
    if KEEP_LOGS:
        # Raw local diagnostics for a human, never evidence (may hold fixture values).
        state.ensure_private_dir(KEEP_LOGS)
        state.atomic_write(KEEP_LOGS / f"{time.time_ns()}-{binary.name}.log", text.encode())
    reports = parse_races(text)
    results = []
    for line in re.findall(r"^GWTEST_RACE_RESULT=(.+)$", text, re.M):
        try:
            result = json.loads(line)
            if not isinstance(result, dict):
                raise ValueError("result is not an object")
            results.append(result)
        except ValueError as error:
            process_error = process_error or type(error).__name__
    return {"exit_status": process.returncode if process is not None else None,
            "invocation_id": invocation_id, "process_started": process is not None,
            "process_pid": process.pid if process is not None else None, "process_error": process_error,
            "cleanup_error": cleanup_error,
            "seconds": round(time.monotonic() - started, 3),
            "tests": test_outcomes(text), "root_tests": root_test_counts(text), "results": results, "detector": signatures(reports),
            "test_pass_counts": {name: len(re.findall(r"^--- PASS: " + re.escape(name) + r" \(", text, re.M))
                                 for name in RETAINED_PLUGIN_HOST},
            "runtime_fatal": parse_fatal(text),
            "target_path": coverage(profile, targets) if targets else {},
            "target_counts": coverage_counts(profile, targets) if targets else {}, **observed}


def signatures(reports: list[dict]) -> list[dict]:
    """Distinct detector reports (classification, accesses, kept frames) with counts."""
    unique = {}
    for report in reports:
        row = {"classification": classify(report), "accesses": report.get("accesses", []),
               "frames": [side[:FRAMES_KEPT] for side in report.get("frames", [])]}
        key = json.dumps(row, sort_keys=True)
        unique.setdefault(key, {**row, "count": 0})["count"] += 1
    return list(unique.values())


def run_executed(run: dict, expected: tuple[str, ...], *, result_line: bool) -> bool:
    """Completed (pass, or race-only failure), or ended by a Go runtime
    concurrency crash, which is itself an observation. Anything else is not."""
    tests = run["tests"]
    if run.get("process_error") or run.get("cleanup_error") or tests.get("skipped"):
        return False
    if run.get("runtime_fatal"):
        return (run["exit_status"] == 2 and not tests["other_failures"]
                and all(crash["message"].startswith("concurrent map") for crash in run["runtime_fatal"]))
    done = set(tests["passed"]) | set(tests["race_only_failures"])
    return (not run.get("driver_error") and not tests["other_failures"] and not tests["runtime_fatal"]
            and not tests["panics"] and set(expected) <= done and (not result_line or len(run["results"]) >= 1)
            and run["exit_status"] in (0, 1) and (run["exit_status"] == 0) == (not tests["race_only_failures"]))


def scenario(sid, build, runs, bound, expected, *, prerequisites: dict, result_line=True, **extra):
    label, pairs, required_for, _builds = SCENARIOS[sid]
    detector = [report for run in runs for report in run["detector"]]
    classified = {}
    for report in detector:
        classified[report["classification"]] = classified.get(report["classification"], 0) + report["count"]
    crashes = {}
    for run in runs:
        for crash in run.get("runtime_fatal", []):
            crashes[crash["classification"]] = crashes.get(crash["classification"], 0) + 1
    prerequisites = {name: bool(value) for name, value in prerequisites.items()}
    return {"id": sid, "build": build, "label": label, "pairs": list(pairs), "required_for": list(required_for),
            "bound": bound, "runtime_fatal": crashes,
            "runs_ended_by_runtime_fatal": sum(bool(run.get("runtime_fatal")) for run in runs),
            "runs": runs, "executed": all(run_executed(run, expected, result_line=result_line) for run in runs),
            "prerequisites": prerequisites, "prerequisites_met": bool(prerequisites) and all(prerequisites.values()),
            "classified": classified, "target_path": merge_coverage(runs),
            "seconds": round(sum(run["seconds"] for run in runs), 3), **extra}


def result_of(run: dict) -> dict:
    return run["results"][-1] if run.get("results") else {}


def served(run: dict, prefix: str) -> int:
    return sum(count for key, count in run.get("request_outcomes", {}).items()
               if key.startswith(prefix) and key.endswith(" 200"))


def service_prerequisites(kind: str, runs: list[dict], baseline: dict | None = None) -> dict:
    """Scenario-specific workload prerequisites, from driver/Go observations."""
    every = lambda check: bool(runs) and all(check(run) for run in runs)  # noqa: E731
    if kind == "startup":
        return {"traffic_before_listener": every(lambda r: sum(
                    n for k, n in r.get("request_outcomes", {}).items() if k.startswith("pre-listen ")) > 0),
                "model_lists_served": every(lambda r: served(r, "GET /v1/models") > 0),
                "messages_served": every(lambda r: served(r, "POST /v1/messages") > 0)}
    if kind == "reload":
        # Accepted post-readiness reloads, each reaching UpdateClientsContext
        # above the startup-only baseline, with concurrent traffic.
        floor = (baseline or {}).get("config_write")
        return {"reloads_all_accepted": every(lambda r: r.get("config_writes", 0) > 0 and r.get("reloads_not_observed") == 0
                                              and r.get("accepted_reloads") == r.get("config_writes")),
                "reload_phase_client_updates": floor is not None and every(
                    lambda r: r.get("accepted_reloads", 0) > 0
                    and r.get("target_counts", {}).get("config_write", 0) - floor >= r["accepted_reloads"]),
                "concurrent_model_lists": every(lambda r: served(r, "GET /v1/models") > 0),
                "concurrent_messages": every(lambda r: served(r, "POST /v1/messages") > 0)}
    if kind in ("file", "config"):
        checks = {"credentials_armed": every(lambda r: r.get("credentials", 0) > 0 and r.get("armed") == r["credentials"]
                                             and result_of(r).get("b108_armed") == r["credentials"]),
                  "every_credential_completed": every(lambda r: r.get("credentials_completed") == r.get("credentials")),
                  "manager_prepare_executed": merge_coverage(runs).get("manager_prepare") is True}
        if kind == "file":
            checks["file_metadata_prepared"] = every(lambda r: result_of(r).get("file_prepared") == r.get("credentials"))
            checks["file_upstream_local_only"] = every(lambda r: result_of(r).get("file_upstream_refused") == 0
                                                       and result_of(r).get("file_upstream_answered", 0) >= r.get("credentials", 1)
                                                       and r.get("upstream_hits", {}).get(FILE_ROUTE, 0) > 0)
        return checks
    raise ValueError(kind)


def matrix_roots(sid):
    if sid not in SCENARIOS:
        raise AssertionError("unknown matrix cell")
    return MATRIX_UNIT_ROOTS.get(sid, ("TestGatewayRaceService",))


def matrix_workload(sid, run, baseline):
    """D217's inner work, checked for EACH independent invocation."""
    result = result_of(run)
    if sid == "unit-pair1-reconcile-clone":
        return result.get("rounds") == result.get("reconciles") == result.get("updates") == 2000
    if sid == "unit-pairs23-reload-handlers":
        return result.get("rounds") == result.get("applied") == 300 and result.get("requests", 0) > 0
    if sid == "direct-b108-setup-token":
        return (result.get("rounds") == 200 and result.get("goroutines") == 32 and result.get("prepared", 0) > 0
                and result.get("errors") == result.get("profile_fetches") == 0)
    if sid in ("unit-plugin-host-retained", "direct-b108-retained-upstream"):
        return run_executed(run, matrix_roots(sid), result_line=False)
    if sid == "service-startup-overlap":
        return all(service_prerequisites("startup", [run]).values())
    if sid == "service-reload-traffic":
        return (run.get("config_writes") == run.get("accepted_reloads") == run.get("auth_updates") == 20
                and run.get("rejected_reload_control") is False
                and all(service_prerequisites("reload", [run], baseline).values()))
    if sid == "control-rejected-reload":
        return (run.get("config_writes") == run.get("auth_updates") == run.get("reloads_not_observed") == 1
                and run.get("accepted_reloads") == 0 and run.get("rejected_reload_control") is True
                and served(run, "GET /v1/models") > 0 and served(run, "POST /v1/messages") > 0)
    mode = "file" if sid == "manager-http-b108-file-auth" else "config"
    return (run.get("credential_shape") == mode and run.get("credentials") == run.get("armed") == 8
            and run.get("credentials_completed") == 8 and run.get("concurrent_per_credential") == 16
            and served(run, "POST /v1/messages b108 json") == 8 * 16
            and sum(run.get("request_outcomes", {}).values()) == 8 * 16
            and all(service_prerequisites(mode, [run]).values()))


def matrix_target_window(sid, run, baseline):
    # Record the control's observed targets too. Its failed acceptance
    # prerequisite still prevents a race-absence/negative verdict.
    path = phase_path([run], baseline) if sid in ("service-reload-traffic", "control-rejected-reload") else run.get("target_path", {})
    return all(target_executed(pair, path) for pair in SCENARIOS[sid][2])


def matrix_accounting(sid, runs, baseline=None):
    roots = matrix_roots(sid)
    total = {name: {"attempted": 0, "passed": 0, "failed": 0, "skipped": 0} for name in roots}
    completed, targets, workloads, other_failures, race_failures = 0, 0, 0, 0, 0
    identities = []
    for run in runs:
        ident = run.get("invocation_id")
        pid = run.get("process_pid")
        if run.get("process_started") is True and isinstance(ident, str) and re.fullmatch(r"[0-9a-f]{32}", ident) \
                and type(pid) is int and pid > 0:
            identities.append(ident)  # PID reuse is legitimate; receipt reuse is not.
        observed = run.get("root_tests", {})
        root_complete = set(observed) == set(roots)
        for name in roots:
            row = observed.get(name, {})
            values = {key: row.get(key, 0) for key in total[name]}
            if any(type(value) is not int or value < 0 for value in values.values()):
                raise AssertionError("invalid matrix root count")
            for key, value in values.items():
                total[name][key] += value
            root_complete &= (values["attempted"] == values["passed"] + values["failed"] == 1
                              and values["skipped"] == 0)
            if name in run.get("tests", {}).get("race_only_failures", []):
                race_failures += values["failed"]
            else:
                other_failures += values["failed"]
        result_line = sid not in ("unit-plugin-host-retained", "direct-b108-retained-upstream")
        root_complete &= len(run.get("results", [])) == (1 if result_line else 0)
        completed += bool(root_complete and run_executed(run, roots, result_line=result_line))
        targets += bool(matrix_target_window(sid, run, baseline or {}))
        workloads += bool(matrix_workload(sid, run, baseline or {}))
    return {"requested_processes": MATRIX_RUNS, "attempted_processes": len(runs),
            "started_processes": len(identities), "distinct_invocations": len(set(identities)),
            "completed_processes": completed, "required_roots": total,
            "workload_windows": workloads, "target_reaching_windows": targets,
            "race_only_root_failures": race_failures, "other_root_failures": other_failures,
            "runtime_fatal_processes": sum(bool(run.get("runtime_fatal")) for run in runs),
            "process_errors": sum(bool(run.get("process_error") or run.get("driver_error") or run.get("cleanup_error")) for run in runs),
            "processes_with_skips": sum(bool(run.get("tests", {}).get("skipped")) for run in runs)}


def validate_matrix_cell(scenario):
    sid, runs = scenario["id"], scenario["runs"]
    measured = matrix_accounting(sid, runs, scenario.get("startup_baseline"))
    bound = scenario.get("bound", {})
    if bound.get("processes") != MATRIX_RUNS:
        raise AssertionError("matrix requires 100 independent processes per cell")
    if sid in ("service-reload-traffic", "control-rejected-reload"):
        if bound.get("config_writes") != (20 if sid == "service-reload-traffic" else 1):
            raise AssertionError("matrix reload inner bound changed")
    if sid in ("manager-http-b108-file-auth", "manager-http-b108-config-empty-metadata"):
        if bound.get("credentials") != 8 or bound.get("concurrent_per_credential") != 16:
            raise AssertionError("matrix B108 inner bound changed")
    if scenario.get("matrix") != measured:
        raise AssertionError("stored matrix counts differ from invocation receipts: " + sid)
    classified, crashes = {}, {}
    for run in runs:
        for report in run.get("detector", []):
            key = report["classification"]
            classified[key] = classified.get(key, 0) + report["count"]
        for crash in run.get("runtime_fatal", []):
            key = crash["classification"]
            crashes[key] = crashes.get(key, 0) + 1
    if scenario.get("classified") != classified or scenario.get("runtime_fatal") != crashes:
        raise AssertionError("matrix discarded an observed race or runtime failure: " + sid)
    for key in ("attempted_processes", "started_processes", "distinct_invocations", "completed_processes", "workload_windows"):
        if measured[key] != MATRIX_RUNS:
            raise AssertionError(f"matrix {sid}: {key}={measured[key]}, want {MATRIX_RUNS}")
    for name, counts in measured["required_roots"].items():
        if counts["attempted"] != MATRIX_RUNS or counts["passed"] + counts["failed"] != MATRIX_RUNS or counts["skipped"]:
            raise AssertionError("matrix missing root executions: " + name)
    if sid != "control-rejected-reload" and measured["target_reaching_windows"] != MATRIX_RUNS:
        raise AssertionError("matrix required target window missing: " + sid)
    if sid == "control-rejected-reload" and scenario.get("prerequisites_met") is not False:
        raise AssertionError("matrix rejected reload must remain inconclusive")


def matrix_invocations(sid, invoke, *, baseline=None, on_attempt=None):
    """Exactly the requested attempts unless a recorded incomplete attempt stops the cell.

    A race-only completion remains an observation and does not trigger a retry.
    An early stop is explicitly underfilled and cannot pass matrix validation.
    """
    runs = []
    for index in range(MATRIX_RUNS):
        try:
            run = invoke()
        except (AssertionError, OSError, ValueError, RuntimeError, probe.ProbeError, subprocess.SubprocessError) as error:
            run = {"invocation_id": uuid.uuid4().hex, "process_started": False, "process_pid": None,
                   "process_error": type(error).__name__, "exit_status": None, "seconds": 0,
                   "tests": test_outcomes(""), "root_tests": {}, "results": [], "detector": [],
                   "runtime_fatal": [], "target_path": {}, "target_counts": {}}
        runs.append(run)
        if on_attempt is not None:
            on_attempt(sid, index + 1, run)
        measured = matrix_accounting(sid, [run], baseline)
        complete = (measured["started_processes"] == measured["completed_processes"] == measured["workload_windows"] == 1
                    and (sid == "control-rejected-reload" or measured["target_reaching_windows"] == 1))
        print(f"matrix {sid}: attempt={index + 1}/{MATRIX_RUNS} completed={measured['completed_processes']} "
              f"workload_windows={measured['workload_windows']} target_windows={measured['target_reaching_windows']}", flush=True)
        if not complete:
            print(f"matrix {sid}: incomplete attempt retained; stopping, not retrying or counting unattempted work", flush=True)
            break
    return runs


def matrix_complete(cell):
    try:
        validate_matrix_cell(cell)
    except AssertionError:
        return False
    return True


def unit_scenarios(build_label: str, diagnostics: Path, targets: dict, *, matrix_runs=None, on_attempt=None) -> list[dict]:
    out = []

    def one(name, test, rounds, extra_args=()):
        with tempfile.TemporaryDirectory(prefix="gwtest-race-unit-") as directory:
            root = Path(directory).resolve()
            return isolated_run(diagnostics / "bin" / ("gwtest-race-" + name),
                                [f"-test.run=^({test})$", *extra_args], root,
                                env={"GWTEST_ROUNDS": str(rounds)}, targets=targets, timeout=120)
    if matrix_runs is not None:
        if matrix_runs != MATRIX_RUNS:
            raise ValueError("matrix runs must be 100")
        for sid, binary, rounds, inner in (
                ("unit-pair1-reconcile-clone", "auth", 2000, {"rounds": 2000}),
                ("unit-pairs23-reload-handlers", "api", 300, {"rounds": 300, "request_workers": 4}),
                ("unit-plugin-host-retained", "handlers", 0, {"count": 100, "count_per_process": 1, "tests": list(RETAINED_PLUGIN_HOST)}),
                ("direct-b108-setup-token", "executor", 200, {"rounds": 200, "goroutines": 32}),
                ("direct-b108-retained-upstream", "executor", 0, {"count": 100, "count_per_process": 1, "tests": list(RETAINED_B108)})):
            roots = matrix_roots(sid)
            runs = matrix_invocations(sid, lambda: one(binary, "|".join(roots), rounds, ("-test.count=1",)),
                                      on_attempt=on_attempt)
            cell = scenario(sid, build_label, runs, {**inner, "processes": MATRIX_RUNS}, roots,
                result_line=sid not in ("unit-plugin-host-retained", "direct-b108-retained-upstream"),
                prerequisites={"each_inner_workload_complete": bool(runs) and all(matrix_workload(sid, run, {}) for run in runs)})
            cell["matrix"] = matrix_accounting(sid, runs)
            out.append(cell)
            if not matrix_complete(cell):
                break
        return out
    run = one("auth", "TestGatewayPair1ReconcileCloneUnit", 2000)
    done = result_of(run)
    out.append(scenario("unit-pair1-reconcile-clone", build_label, [run],
                        {"rounds": 2000, "processes": 1}, ("TestGatewayPair1ReconcileCloneUnit",),
                        prerequisites={"all_rounds_applied": done.get("reconciles") == done.get("updates") == 2000}))
    run = one("api", "TestGatewayReloadHandlersUnit", 300)
    done = result_of(run)
    out.append(scenario("unit-pairs23-reload-handlers", build_label, [run],
                        {"rounds": 300, "request_workers": 4, "processes": 1}, ("TestGatewayReloadHandlersUnit",),
                        prerequisites={"all_reloads_applied": done.get("applied") == 300,
                                       "concurrent_requests": done.get("requests", 0) > 0}))
    run = one("handlers", "|".join(RETAINED_PLUGIN_HOST), 0, ("-test.count=100",))
    out.append(scenario("unit-plugin-host-retained", build_label, [run],
                        {"count": 100, "tests": list(RETAINED_PLUGIN_HOST), "processes": 1}, RETAINED_PLUGIN_HOST,
                        result_line=False, prerequisites={"all_tests_completed_100_times": all(
                            run.get("test_pass_counts", {}).get(name) == 100 for name in RETAINED_PLUGIN_HOST)}))
    run = one("executor", "TestGatewayB108DirectSetupToken", 200)
    done = result_of(run)
    out.append(scenario("direct-b108-setup-token", build_label, [run],
                        {"rounds": 200, "goroutines": 32, "processes": 1}, ("TestGatewayB108DirectSetupToken",),
                        prerequisites={"prepared_offline": done.get("prepared", 0) > 0 and done.get("errors") == 0
                                       and done.get("profile_fetches") == 0}))
    run = one("executor", "|".join(RETAINED_B108), 0, ("-test.count=20",))
    ran = set(run["tests"]["passed"]) | set(run["tests"]["race_only_failures"])
    out.append(scenario("direct-b108-retained-upstream", build_label, [run],
                        {"count": 20, "tests": list(RETAINED_B108), "processes": 1}, RETAINED_B108,
                        result_line=False, prerequisites={"retained_tests_completed": set(RETAINED_B108) <= ran}))
    return out


# ----------------------------------------------------- service scenarios

def revised(document, **changes):
    document = copy.deepcopy(document)
    document["openai-compatibility"].pop()  # only the final render sentinel
    document.update(changes)
    return render.finalize_document(document)


def with_b108_credentials(document, egress: int, count: int):
    """Config-synthesized claude-api-key credentials with synthetic OAuth-shaped
    keys: the ADDITIONAL empty-metadata case (config synthesis leaves Metadata
    nil, and Auth.Clone keeps a nil map nil), never the file-auth observation."""
    document = copy.deepcopy(document)
    document["openai-compatibility"].pop()
    template = copy.deepcopy(document["claude-api-key"][0])
    aliases = []
    for index in range(count):
        entry = copy.deepcopy(template)
        alias = f"gwtest-b108-{index}"
        # A fake OAuth-shaped key; the Go race tests match its prefix.
        entry.update({"api-key": f"sk-ant-oat-gwtest-b108-{index}-{uuid.uuid4().hex[:8]}",
                      "base-url": f"http://127.0.0.1:{egress}/gwtest-b108"})
        entry["models"] = [{**copy.deepcopy(template["models"][0]), "name": f"gwtest-wire-b108-{index}", "alias": alias}]
        entry.pop("headers", None)
        document["claude-api-key"].append(entry)
        aliases.append(alias)
    return render.finalize_document(document), aliases


def file_auth(note: str) -> bytes:
    # No refresh token and no real identity, as in the startup probe.
    return json.dumps({"type": "claude", "email": "gwtest@example.invalid", "access_token": "dummy-gwtest-access",
                       "expired": "2099-01-01T00:00:00Z", "note": note}).encode()


def file_setup_token_auth(index: int) -> bytes:
    """A synthetic file-backed Claude setup-token credential: OAuth-shaped
    access token, inference-only scope (no user:profile, so preparation takes
    the offline setup-token branch), no refresh token, far expiry. The prefix
    gives the credential its own routable model ids, read at run time."""
    return json.dumps({"type": "claude", "email": f"{FILE_AUTH_PREFIX}{index}@example.invalid",
                       # A fake OAuth-shaped token; the Go race tests match its prefix.
                       "access_token": f"sk-ant-oat-gwtest-file-{index}-{uuid.uuid4().hex[:8]}",
                       "scope": "user:inference", "expired": "2099-01-01T00:00:00Z",
                       "prefix": file_prefix(index)}).encode()


def file_prefix(index: int) -> str:
    return f"gwtestfb{index}"


class ServiceFixture:
    """Rendered fixture home, fake upstream and bridges for the service overlay."""

    def __init__(self, root: Path, *, b108=0, b108_mode=None):
        spec = json.loads((SERVICE_SPEC).read_text())
        self.home, self.control = root / "home", root / "run"
        for path in (self.home, self.control, *(self.home / p for p in spec["private_dirs"]),
                     self.home / ".local/share/charmer-release"):
            state.ensure_private_dir(path)
        self.egress, self.port = harness.ports()
        document, self.aliases, _ = harness.build_document(catalog.load_catalog(FIXTURE_ROOT),
                                                           self.egress, self.port, self.home)
        self.b108, self.b108_mode, self.file_prefixes = [], b108_mode, []
        if b108 and b108_mode == "config":
            document, self.b108 = with_b108_credentials(document, self.egress, b108)
        self.document = document
        self.config = self.home / ".config/charmer/config.yaml"
        state.atomic_write(self.config, render.emit_yaml(document).encode())
        self.auth_file = Path(document["auth-dir"]) / "gwtest-race-claude.json"
        state.atomic_write(self.auth_file, file_auth("gwtest-0"))
        if b108 and b108_mode == "file":
            for index in range(b108):
                state.atomic_write(Path(document["auth-dir"]) / f"{FILE_AUTH_PREFIX}{index}.json",
                                   file_setup_token_auth(index))
                self.file_prefixes.append(file_prefix(index))
        elif b108 and b108_mode != "config":
            raise ValueError("b108 credentials need b108_mode file or config")
        self.socket = self.control / "gateway.sock"
        self.upstream = harness.FakeUpstream(self.control / "upstream.sock")
        self.view = probe.HomeView(self.home, tuple(self.home / p for p in spec["bind_rw"]),
                                   tuple(self.home / p for p in spec["bind_ro"]))
        self.cwd = self.home / spec["working_directory"]
        self.bridges = [probe.PortBridge(self.egress, self.control / "upstream.sock", "egress"),
                        probe.PortBridge(self.port, self.socket, "ingress")]
        family = {}
        for alias, info in sorted(self.aliases.items()):
            if info.origin == "catalog":
                family.setdefault(info.family, alias)
        self.traffic_aliases = [family[name] for name in ("claude", "compat", "codex")]

    def headers(self):
        return {"Authorization": "Bearer " + FIXTURE_GATEWAY_TOKEN, "anthropic-version": "2023-06-01",
                "Content-Type": "application/json", "Host": f"127.0.0.1:{self.port}",
                "User-Agent": "gwtest-races/1"}

    def get(self, target, timeout=10):
        return harness.unix_request(self.socket, "GET", target, headers=self.headers(), timeout=timeout)

    def message(self, alias, stream):
        body = {"model": alias, "max_tokens": 64, "stream": stream,
                "messages": [{"role": "user", "content": "gwtest-case-" + uuid.uuid4().hex[:12]}]}
        return harness.unix_request(self.socket, "POST", "/v1/messages", headers=self.headers(),
                                    body=json.dumps(body).encode(), timeout=30)

    def sentinel_ids(self):
        # The product's served read: the OpenAI listing never cloaks the sentinel.
        headers = {name: value for name, value in self.headers().items() if name != "anthropic-version"}
        reply = harness.unix_request(self.socket, "GET", "/v1/models", headers=headers, timeout=10)
        return reply.status, {row["id"] for row in reply.json().get("data", [])} if reply.status == 200 else set()

    def ready(self, process, log_path, deadline=90):
        end = time.monotonic() + deadline
        sentinel = render.document_sentinel(self.document)
        while time.monotonic() < end:
            if process.poll() is not None:
                raise AssertionError(f"race service exited before readiness: rc={process.returncode}")
            try:
                if (self.get("/healthz", timeout=2).status == 200
                        and harness.WATCHER_READY in log_path.read_text(errors="replace")
                        and sentinel in self.sentinel_ids()[1]):
                    return
            except (OSError, ValueError, http.client.HTTPException):
                pass
            time.sleep(0.05)
        raise AssertionError("race service readiness deadline exceeded")

    def finish(self):
        state.atomic_write(self.control / "done", b"done\n")

    def close(self):
        self.upstream.close()


class Traffic:
    """Continuous authenticated traffic; outcomes are counted, never stored."""

    def __init__(self, fixture: ServiceFixture, *, workers=6, early=False):
        self.fixture, self.stop, self.outcomes = fixture, threading.Event(), {}
        self.lock, self.threads, self.early = threading.Lock(), [], early
        plan = ["models", "healthz"] + [(alias, stream) for alias in fixture.traffic_aliases for stream in (False, True)]
        for index in range(workers):
            thread = threading.Thread(target=self.loop, args=(plan[index % len(plan):] + plan[:index % len(plan)],), daemon=True)
            self.threads.append(thread)

    def count(self, key):
        with self.lock:
            self.outcomes[key] = self.outcomes.get(key, 0) + 1

    def loop(self, plan):
        index = 0
        while not self.stop.is_set():
            item = plan[index % len(plan)]
            index += 1
            try:
                if item == "models":
                    reply = self.fixture.get("/v1/models")
                    self.count(f"GET /v1/models {reply.status}")
                elif item == "healthz":
                    reply = self.fixture.get("/healthz")
                    self.count(f"GET /healthz {reply.status}")
                else:
                    alias, stream = item
                    family = self.fixture.aliases[alias].family
                    reply = self.fixture.message(alias, stream)
                    self.count(f"POST /v1/messages {family} {'stream' if stream else 'json'} {reply.status}")
            except (OSError, ValueError, http.client.HTTPException) as error:
                self.count(("pre-listen " if self.early else "") + "transport " + type(error).__name__)
                time.sleep(0.01)

    def __enter__(self):
        for thread in self.threads:
            thread.start()
        return self

    def __exit__(self, *_exc):
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=35)


def upstream_summary(fixture: ServiceFixture) -> dict:
    hits = {}
    for hit in list(fixture.upstream.hits):
        hits[hit.route] = hits.get(hit.route, 0) + 1
    return {"upstream_hits": hits, "upstream_unexpected_paths": fixture.upstream.unexpected}


def service_run(diagnostics: Path, targets: dict, scenario_id: str, drive, *, b108=0, b108_mode=None) -> dict:
    with tempfile.TemporaryDirectory(prefix="gwtest-race-svc-") as directory:
        root = Path(directory).resolve()
        fixture = ServiceFixture(root, b108=b108, b108_mode=b108_mode)
        env = {"WRITABLE_PATH": str(root), "GWTEST_CONFIG": str(fixture.config),
               "GWTEST_CONTROL": str(fixture.control), "GWTEST_SCENARIO": scenario_id}
        if b108_mode:
            env["GWTEST_B108_MODE"] = b108_mode
        if b108_mode == "file":
            # Loopback inside the unshared namespace: the egress bridge to the fake upstream.
            env["GWTEST_FILE_UPSTREAM"] = f"http://127.0.0.1:{fixture.egress}/{FILE_ROUTE}"
        run = None
        try:
            def driver(process, log_path):
                try:
                    return drive(fixture, process, log_path)
                finally:
                    fixture.finish()  # a failed driver never leaves the service to its timeout
            run = isolated_run(diagnostics / "bin/gwtest-race-service", ["-test.run=^TestGatewayRaceService$"], root,
                               env=env,
                               targets=targets, timeout=280, driver=driver, cwd=fixture.cwd,
                               home_view=fixture.view, bridges=fixture.bridges)
            run.update(upstream_summary(fixture))
            return run
        finally:
            try:
                fixture.close()
            except (OSError, RuntimeError) as error:
                if run is None:
                    raise
                run["cleanup_error"] = type(error).__name__


def drive_startup(fixture, process, log_path):
    # Overlap startup: traffic begins before the listener exists and runs
    # through readiness (the ungated health reader and the first model lists).
    started = time.monotonic()
    with Traffic(fixture, workers=4, early=True) as traffic:
        fixture.ready(process, log_path)
        ready_seconds = time.monotonic() - started
        time.sleep(1.5)
    return {"ready_seconds": round(ready_seconds, 3), "request_outcomes": traffic.outcomes,
            "accepted_reloads": 0, "auth_updates": 0}


def wait_for(path: Path, process, deadline=15) -> dict:
    """A control answer the Go side writes completely (write, then rename)."""
    end = time.monotonic() + deadline
    while time.monotonic() < end and process.poll() is None:
        try:
            return json.loads(path.read_text())
        except FileNotFoundError:
            time.sleep(0.02)
    raise AssertionError("race service did not answer " + path.name)


def drive_reload(reloads, *, reject=False):
    """Post-readiness reloads under traffic (traffic starts after readiness).
    reject=True is the control: each reload drops every client key, which the
    gateway refuses."""
    def drive(fixture, process, log_path):
        fixture.ready(process, log_path)
        accepted, missed, auth_updates, document = 0, 0, 0, fixture.document
        with Traffic(fixture, workers=6) as traffic:
            for index in range(1, reloads + 1):
                # An independent auth-file update, then a genuinely changed render.
                state.atomic_write(fixture.auth_file, file_auth(f"gwtest-{index}"))
                auth_updates += 1
                changes = {"request-retry": 1 + index % 3, "max-retry-interval": 10 + index}
                if reject:
                    changes["api-keys"] = []
                document = revised(document, **changes)
                state.atomic_write(fixture.config, render.emit_yaml(document).encode())
                sentinel, end = render.document_sentinel(document), time.monotonic() + 15
                while time.monotonic() < end:
                    try:
                        if sentinel in fixture.sentinel_ids()[1]:
                            accepted += 1
                            break
                    except (OSError, ValueError, http.client.HTTPException):
                        pass
                    time.sleep(0.05)
                else:
                    missed += 1
        return {"request_outcomes": traffic.outcomes, "accepted_reloads": accepted,
                "reloads_not_observed": missed, "config_writes": reloads, "auth_updates": auth_updates,
                "rejected_reload_control": reject}
    return drive


def drive_b108(bursts):
    """Concurrent bursts per credential through the ordinary manager/HTTP path.
    File mode: each credential's own prefixed model id, read by the Go side
    from the model registry (never pinned, never stored as evidence)."""
    def drive(fixture, process, log_path):
        fixture.ready(process, log_path)
        state.atomic_write(fixture.control / "b108-arm", b"arm\n")
        answer = wait_for(fixture.control / "b108-armed", process)
        armed = int(answer["armed"])
        aliases = list(answer["aliases"]) if fixture.b108_mode == "file" else list(fixture.b108)
        outcomes, completed, lock = {}, {}, threading.Lock()
        for alias in aliases:
            barrier = threading.Barrier(bursts)

            def one(alias=alias, barrier=barrier):
                barrier.wait(timeout=10)
                try:
                    status = fixture.message(alias, False).status
                    key = f"POST /v1/messages b108 json {status}"
                except (OSError, ValueError, http.client.HTTPException) as error:
                    status, key = None, "transport " + type(error).__name__
                with lock:
                    outcomes[key] = outcomes.get(key, 0) + 1
                    if status == 200:
                        completed[alias] = completed.get(alias, 0) + 1
            threads = [threading.Thread(target=one, daemon=True) for _ in range(bursts)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=40)
        return {"request_outcomes": outcomes, "credentials": len(aliases), "armed": armed,
                "credentials_completed": len(completed), "credential_shape": fixture.b108_mode,
                "concurrent_per_credential": bursts, "accepted_reloads": 0}
    return drive


def service_scenarios(build_label, diagnostics, targets, *, startups=3, reloads=12, http_runs=3,
                      matrix_runs=None, on_attempt=None) -> list[dict]:
    out = []
    expected = ("TestGatewayRaceService",)
    if matrix_runs is not None:
        if matrix_runs != MATRIX_RUNS:
            raise ValueError("matrix runs must be 100")
        sid = "service-startup-overlap"
        runs = matrix_invocations(sid, lambda: service_run(diagnostics, targets, "service-startup", drive_startup),
                                  on_attempt=on_attempt)
        cell = scenario(sid, build_label, runs,
                        {"processes": MATRIX_RUNS, "traffic_workers": 4, "post_ready_seconds": 1.5}, expected,
                        prerequisites=service_prerequisites("startup", runs), coverage="process lifetime")
        cell["matrix"] = matrix_accounting(sid, runs)
        out.append(cell)
        if not matrix_complete(cell):
            return out
        baseline = startup_baseline(runs)
        for sid, count, reject in (("service-reload-traffic", 20, False), ("control-rejected-reload", 1, True)):
            runs = matrix_invocations(sid, lambda: service_run(diagnostics, targets, sid, drive_reload(count, reject=reject)),
                                      baseline=baseline, on_attempt=on_attempt)
            bound = {"processes": MATRIX_RUNS, "config_writes": count, "traffic_workers": 6, "reload_observation_seconds": 15}
            if reject:
                bound["reload"] = "drops every client key (refused by the gateway)"
            cell = scenario(sid, build_label, runs, bound, expected,
                            prerequisites=service_prerequisites("reload", runs, baseline),
                            coverage="reload phase: writer counts above the startup-only baseline",
                            startup_baseline=baseline, phase_path=phase_path(runs, baseline))
            cell["matrix"] = matrix_accounting(sid, runs, baseline)
            out.append(cell)
            if not matrix_complete(cell):
                return out
        for sid, mode in (("manager-http-b108-file-auth", "file"), ("manager-http-b108-config-empty-metadata", "config")):
            runs = matrix_invocations(sid, lambda: service_run(diagnostics, targets, sid, drive_b108(16), b108=8, b108_mode=mode),
                                      on_attempt=on_attempt)
            cell = scenario(sid, build_label, runs,
                            {"processes": MATRIX_RUNS, "credentials": 8, "concurrent_per_credential": 16, "credential_shape": mode},
                            expected, prerequisites=service_prerequisites(mode, runs), coverage="process lifetime")
            cell["matrix"] = matrix_accounting(sid, runs)
            out.append(cell)
            if not matrix_complete(cell):
                break
        return out
    runs = [service_run(diagnostics, targets, "service-startup", drive_startup) for _ in range(startups)]
    out.append(scenario("service-startup-overlap", build_label, runs,
                        {"processes": startups, "traffic_workers": 4, "post_ready_seconds": 1.5}, expected,
                        prerequisites=service_prerequisites("startup", runs), coverage="process lifetime"))
    baseline = startup_baseline(runs)
    for sid, count, reject in (("service-reload-traffic", reloads, False), ("control-rejected-reload", 1, True)):
        runs = [service_run(diagnostics, targets, sid, drive_reload(count, reject=reject))]
        bound = {"processes": 1, "config_writes": count, "traffic_workers": 6, "reload_observation_seconds": 15}
        if reject:
            bound["reload"] = "drops every client key (refused by the gateway)"
        out.append(scenario(sid, build_label, runs, bound, expected,
                            prerequisites=service_prerequisites("reload", runs, baseline),
                            coverage="reload phase: writer counts above the startup-only baseline",
                            startup_baseline=baseline, phase_path=phase_path(runs, baseline)))
    for sid, mode in (("manager-http-b108-file-auth", "file"), ("manager-http-b108-config-empty-metadata", "config")):
        runs = [service_run(diagnostics, targets, sid, drive_b108(16), b108=8, b108_mode=mode)
                for _ in range(http_runs)]
        out.append(scenario(sid, build_label, runs,
                            {"processes": http_runs, "credentials": 8, "concurrent_per_credential": 16,
                             "credential_shape": mode}, expected,
                            prerequisites=service_prerequisites(mode, runs), coverage="process lifetime"))
    return out


# ------------------------------------------------------------ assembly

def verdicts_for(label: str, scenarios: list[dict]) -> dict:
    mine = [s for s in scenarios if s["build"] == label]
    result = {}
    if label == "series":
        for pair in ALL_PAIRS:
            result[pair] = {kind: pair_verdict(pair, [s for s in mine if s["label"] == kind and pair in s["pairs"]])
                            for kind in ("service", "unit")}
    b108 = {kind: pair_verdict("b108", [s for s in mine if s["label"] == kind]) for kind in B108_KINDS}
    direct, http = b108["direct-executor"]["verdict"], b108["manager-http"]["verdict"]
    # The disposition answers the planned question (file-auth manager/HTTP);
    # the config-credential case stays a separately labelled observation.
    if http == "reproduced":
        disposition = "reproduced under manager/HTTP file-auth traffic"
    elif direct == "reproduced":
        disposition = "unit reproduction only" + ("" if http == NEGATIVE else
                                                  "; manager/HTTP file-auth observation: " + http)
    else:
        disposition = f"direct-executor: {direct}; manager/HTTP file-auth: {http}"
    result["b108"] = {**b108, "disposition": disposition,
                      "config_empty_metadata_note": "config-synthesized credentials (empty Metadata, cloned as "
                                                    "nil); an additional case, not the file-auth observation"}
    if label == "series" and any(s["id"] == "service-reload-traffic" for s in mine):
        shape = next(s for s in mine if s["id"] == "service-reload-traffic")["target_path"]
        # Production-shaped credentials: did request preparation run at all?
        result["b108"]["production_shape_preparation_executed"] = bool(
            shape.get("setup_token") or shape.get("executor_prepare"))
    return result


def control_verdicts(scenarios: list[dict]) -> dict:
    """The rejected-reload control: its reload workload never executes, so no
    pair may read as a bounded negative from it."""
    control = [s for s in scenarios if s["build"] == "series" and s["id"] == "control-rejected-reload"]
    if len(control) != 1 or control[0].get("prerequisites_met") is not False:
        raise AssertionError("rejected-reload control missing, or its reload was accepted")
    return {"rejected_reload": {pair: pair_verdict(pair, control) for pair in ALL_PAIRS}}


def run_all(args) -> int:
    worktree = REPO_ROOT
    validate_omissions(args.omit_patch)
    began = time.monotonic()
    reason = probe.network_isolation_available()
    if reason:
        print("BLOCKED: " + reason, file=sys.stderr)
        return 2
    head = subprocess.check_output(["git", "-C", str(worktree), "rev-parse", "HEAD"], text=True).strip()
    status = subprocess.check_output(["git", "-C", str(worktree), "status", "--porcelain",
                                      "--untracked-files=all"], text=True)
    document = {"schema": SCHEMA, "observation_only": True, "negative_meaning": NEGATIVE,
                "product_gate": False, "pairs": PAIRS, "worktree_head": head, "dirty": bool(status),
                "status_porcelain": status.splitlines(), "omitted": args.omit_patch,
                "sandbox": {"isolation": "bwrap --unshare-net (probe.start_isolated_process)",
                            "fake_upstream": "unix socket via egress PortBridge", "provider_calls": 0},
                "started": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                "builds": [], "scenarios": []}
    matrix_runs = getattr(args, "matrix_runs", None)
    if matrix_runs is not None:
        document["matrix_runs"] = matrix_runs
    label, given = "series", args.series_diagnostics
    print(f"race diagnostics: {label}: " + ("reusing " + str(given) if given else "building"), flush=True)
    identity = describe(Path(given).resolve(), label) if given else build(worktree, label, args.omit_patch)
    if identity["patches"] != [p for p in harness.PATCH_ROWS if p not in args.omit_patch]:
        raise AssertionError("diagnostics do not match the selected omission series")
    document["builds"].append(identity)
    diagnostics = Path(identity["diagnostics"])
    targets = load_targets(diagnostics)
    document.setdefault("targets", {})[label] = {k: list(v) for k, v in targets.items()}
    on_attempt = None
    if matrix_runs is not None:
        if set(targets) != TARGET_LABELS:
            raise AssertionError("matrix requires all fourteen target extents before execution")
        state.ensure_private_dir(args.out)
        attempts = args.out / "matrix-attempts.jsonl"
        if attempts.exists() or (args.out / "races.json").exists() or (args.out / "races-incomplete.json").exists():
            raise ValueError("matrix evidence directory must be fresh")
        state.atomic_write(attempts, b"")
        def on_attempt(sid, index, run):
            row = {"cell": sid, "attempt": index, "run": run}
            scrub(row)
            # Preserve actual attempts before the next process starts, even
            # when an incomplete cell cannot produce a valid final report.
            with attempts.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, sort_keys=True) + "\n")

    def append_cells(items):
        for item in items:
            print(f"  {item['id']}: executed={item['executed']} prerequisites={item['prerequisites']} "
                  f"classified={item['classified']} "
                  f"seconds={item['seconds']} target_path={item['target_path']}", flush=True)
            for run in item["runs"]:
                print("    run: " + json.dumps({k: v for k, v in run.items() if k not in (
                    "detector", "target_path")}, sort_keys=True), flush=True)
            document["scenarios"].append(item)

    append_cells(unit_scenarios(label, diagnostics, targets, matrix_runs=matrix_runs, on_attempt=on_attempt))
    if matrix_runs is None or (len(document["scenarios"]) == len(MATRIX_UNIT_ROOTS)
                               and all(matrix_complete(cell) for cell in document["scenarios"])):
        append_cells(service_scenarios(label, diagnostics, targets, startups=args.startups, reloads=args.reloads,
                                       http_runs=args.http_runs, matrix_runs=matrix_runs, on_attempt=on_attempt))
    document["duration_seconds"] = round(time.monotonic() - began, 3)
    try:
        document["verdicts"] = {build["label"]: verdicts_for(build["label"], document["scenarios"])
                                for build in document["builds"]}
        document["controls"] = control_verdicts(document["scenarios"])
        path = publish(args.out, document)
    except (AssertionError, ValueError, TypeError, KeyError) as error:
        if matrix_runs is None:
            raise
        partial = {"schema": "gwtest-races-incomplete-v1", "product_gate": False,
                   "validation_error": str(error), "observations": document}
        scrub(partial)
        state.atomic_write(args.out / "races-incomplete.json", (json.dumps(partial, indent=2, sort_keys=True) + "\n").encode())
        print("matrix incomplete: retained attempts and observations; no acceptance report published", file=sys.stderr)
        return 1
    print(json.dumps(document["verdicts"], indent=2, sort_keys=True))
    print(f"race diagnostics wall time: {document['duration_seconds']}s (observation only) -> {path}")
    return 0


# ------------------------------------------------------------ self-test

SAMPLE = """=== RUN   TestGatewayRaceService
==================
WARNING: DATA RACE
Write at 0x00c000 by goroutine 7:
  github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/auth.(*Manager).ReconcileRegistryModelStates()
      /build/source/sdk/cliproxy/auth/conductor_selection.go:340 +0x1
  github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy.(*Service).completeModelRegistrationForAuthWithCache()
      /build/source/sdk/cliproxy/service_auth.go:536 +0x1

Previous read at 0x00c000 by goroutine 9:
  github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/auth.(*Auth).Clone()
      /build/source/sdk/cliproxy/auth/types.go:304 +0x1

Goroutine 7 (running) created at:
  main.main()
==================
    testing.go:1712: race detected during execution of test
--- FAIL: TestGatewayRaceService (1.00s)
"""


class SelfTests(unittest.TestCase):
    def test_diagnostic_ports_exclude_ephemeral_source_range(self):
        with mock.patch.object(harness.random, "SystemRandom") as random:
            random.return_value.sample.return_value = [61000, 65535]
            self.assertEqual(harness.ports(), (61000, 65535))
            random.return_value.sample.assert_called_once_with(range(61000, 65536), 2)
        for _ in range(100):
            ports = harness.ports()
            self.assertEqual(len(set(ports)), 2)
            self.assertTrue(all(61000 <= port <= 65535 for port in ports))

    def test_parser_and_classifier(self):
        reports = parse_races(SAMPLE)
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["accesses"], ["write", "previous read"])
        self.assertEqual(classify(reports[0]), "pair1")
        self.assertNotIn("/build/", json.dumps(reports))
        outcome = test_outcomes(SAMPLE)
        self.assertEqual(outcome["race_only_failures"], ["TestGatewayRaceService"])
        self.assertEqual(outcome["other_failures"], [])

    def test_incomplete_process_keeps_race_observations(self):
        for bad_result, stop_error in ((None, None), ("{", None), ("[]", None), (None, RuntimeError("fixture cleanup"))):
            with self.subTest(result=bad_result, cleanup=bool(stop_error)), tempfile.TemporaryDirectory() as directory:
                process = mock.Mock(pid=123, returncode=1)

                def start(_argv, **kwargs):
                    kwargs["stdout"].write((SAMPLE + "GWTEST_RACE_RESULT=" + (bad_result or "{}") + "\n").encode())
                    kwargs["stdout"].flush()
                    return process

                with mock.patch.object(probe, "start_isolated_process", side_effect=start), \
                        mock.patch.object(probe, "stop_isolated_process", side_effect=stop_error), \
                        mock.patch(__name__ + ".KEEP_LOGS", None):
                    run = isolated_run(Path("/fixture/probe"), [], Path(directory))
                self.assertEqual(run["detector"][0]["classification"], "pair1")
                self.assertEqual(run["detector"][0]["count"], 1)
                self.assertEqual(run["root_tests"]["TestGatewayRaceService"]["failed"], 1)
                self.assertEqual(run["tests"]["race_only_failures"], ["TestGatewayRaceService"])
                self.assertEqual(run_executed(run, ("TestGatewayRaceService",), result_line=True),
                                 bad_result is None and stop_error is None)
                self.assertEqual(bool(run["cleanup_error"]), stop_error is not None)
                self.assertEqual(bool(run["process_error"]), bad_result is not None)

    def test_other_failure_is_not_an_executed_scenario(self):
        text = "=== RUN   TestX\n    x_test.go:9: service stopped\n--- FAIL: TestX (0.1s)\n"
        run = {"tests": test_outcomes(text), "results": [{}], "exit_status": 1}
        self.assertFalse(run_executed(run, ("TestX",), result_line=True))
        run = {"tests": test_outcomes(SAMPLE.replace("TestGatewayRaceService", "TestX")), "results": [{}], "exit_status": 1}
        self.assertTrue(run_executed(run, ("TestX",), result_line=True))
        run["exit_status"] = 0
        self.assertFalse(run_executed(run, ("TestX",), result_line=True))

    def test_classifier_separates_pairs(self):
        def report(a, b):
            return {"frames": [a, b]}
        self.assertEqual(classify(report(["x.(*BaseAPIHandler).SetPluginHost", "api.(*Server).UpdateClientsContext"],
                                         ["x.(*BaseAPIHandler).interceptorHost", "gin.(*Engine).ServeHTTP"])), "pair3")
        api = "github.com/router-for-me/CLIProxyAPI/v8/internal/api."
        self.assertEqual(classify(report([api + "(*Server).UpdateClientsContext"],
                                         [api + "NewServer.(*Server).homeHeartbeatMiddleware.func6"])), "pair2")
        self.assertEqual(classify(report([api + "effectiveSDKConfig", api + "(*Server).UpdateClientsContext"],
                                         ["github.com/router-for-me/CLIProxyAPI/v8/sdk/api/handlers.PassthroughHeadersEnabled"])), "pair2")
        auth = "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/auth."
        self.assertEqual(classify(report(["runtime.mapassign_faststr", "github.com/router-for-me/CLIProxyAPI/v8/internal/auth/claude.StoreMetadataString"],
                                         ["runtime.mapaccess1_faststr", auth + "authMetadataString"])), "b108")
        self.assertEqual(classify(report([auth + "(*Manager).MarkResult"],
                                         [auth + "(*Auth).Clone", auth + "(*Manager).updateInternal"])), "auth-clone-other-writer")
        # A deeper executor frame is not a metadata access (observed signature).
        self.assertEqual(classify(report(
            [auth + "resetModelState", auth + "(*Manager).MarkResult", auth + "(*Manager).executeMixedOnce",
             "github.com/router-for-me/CLIProxyAPI/v8/internal/runtime/executor.(*ClaudeExecutor).Execute"],
            [auth + "(*ModelState).Clone", auth + "(*Auth).Clone", auth + "(*Manager).updateInternal"])),
            "auth-clone-other-writer")
        self.assertEqual(classify(report(["runtime.mapassign_faststr", "github.com/router-for-me/CLIProxyAPI/v8/internal/auth/claude.ensureDeviceIDPoolLocked"],
                                         ["runtime.mapiternext", auth + "(*Auth).Clone", auth + "cloneAuthSlice"])), "b108")
        self.assertEqual(classify(report(["runtime.mapaccess1_faststr", "github.com/router-for-me/CLIProxyAPI/v8/internal/runtime/executor.isClaudeSetupToken"],
                                         ["github.com/router-for-me/CLIProxyAPI/v8/internal/auth/claude.StoreMetadataString"])), "b108")
        self.assertEqual(classify(report(["a.f"], ["b.g"])), "other")

    def test_runtime_fatal_is_an_observed_crash(self):
        text = ("=== RUN   TestGatewayRaceService\nfatal error: time=x level=info msg=interleaved\n"
                "concurrent map read and map write\n\n"
                "goroutine 3 [running]:\ninternal/runtime/maps.fatal({0x1?, 0x2?})\n\t/x.go:1 +0x1\n"
                "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/auth.authMetadataString(0xc0, {0x1, 0x9})\n"
                "\t/y.go:2 +0x2\n\ngoroutine 4 [select]:\n")
        crashes = parse_fatal(text)
        self.assertEqual(crashes[0]["classification"], "b108")
        self.assertEqual(crashes[0]["frames"][1], "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/auth.authMetadataString")
        run = {"tests": test_outcomes(text), "results": [], "exit_status": 2, "runtime_fatal": crashes}
        self.assertTrue(run_executed(run, ("TestGatewayRaceService",), result_line=True))
        run["exit_status"] = 1
        self.assertFalse(run_executed(run, ("TestGatewayRaceService",), result_line=True))
        missing = {"target_path": {}, "classified": {}, "runtime_fatal": {"b108": 1}, "id": "s"}
        self.assertEqual(pair_verdict("b108", [missing])["verdict"], "reproduced")

    def test_negative_never_reads_as_absent(self):
        executed = {"target_path": {"reconcile_write": True, "clone_after_unlock": True}, "classified": {}, "id": "s",
                    "pairs": ["pair1"], "executed": True, "prerequisites_met": True}
        self.assertEqual(pair_verdict("pair1", [executed])["verdict"], NEGATIVE)
        missing = {**executed, "target_path": {"reconcile_write": True, "clone_after_unlock": False}}
        self.assertIn("inconclusive", pair_verdict("pair1", [missing])["verdict"])
        incomplete = {**executed, "prerequisites_met": False}
        self.assertEqual(pair_verdict("pair1", [incomplete])["verdict"], INCOMPLETE)
        hit = {**incomplete, "classified": {"pair1": 2}}  # a genuine reproduction is retained
        self.assertEqual(pair_verdict("pair1", [hit])["verdict"], "reproduced")
        # A supplementary scenario (not required for the pair) cannot supply the negative.
        startup = {**executed, "id": "startup", "required_for": ["pair1"],
                   "target_path": {"config_write": True, "unified_models": True}}
        verdict = pair_verdict("pair2", [startup, {**incomplete, "id": "reload", "pairs": list(ALL_PAIRS), "target_path": {}}])
        self.assertEqual(verdict["verdict"], INCOMPLETE)
        self.assertEqual(verdict["required_scenarios"], ["reload"])

    def test_target_pair_is_never_assembled_across_scenarios(self):
        # Startup reaches only the writer, the reload phase only the reader.
        # Their union covers pair1, but neither scenario executed it.
        base = {"classified": {}, "runtime_fatal": {}, "pairs": list(ALL_PAIRS), "executed": True,
                "prerequisites_met": True}
        startup = {**base, "id": "service-startup-overlap", "required_for": ["pair1"],
                   "target_path": {"reconcile_write": True, "clone_after_unlock": False}}
        reload = {**base, "id": "service-reload-traffic", "required_for": list(ALL_PAIRS),
                  "target_path": {"reconcile_write": True, "clone_after_unlock": True},
                  "phase_path": {"reconcile_write": False, "clone_after_unlock": True}}
        self.assertTrue(target_executed("pair1", merge_coverage([startup, {"target_path": reload["phase_path"]}])))
        verdict = pair_verdict("pair1", [startup, reload])
        self.assertFalse(verdict["target_path_executed"])
        self.assertIn("target path not executed", verdict["verdict"])
        # Startup coverage cannot mask a reload phase that missed its writer.
        startup["target_path"]["clone_after_unlock"] = True
        verdict = pair_verdict("pair1", [startup, reload])
        self.assertFalse(verdict["target_path_executed"])
        self.assertNotEqual(verdict["verdict"], NEGATIVE)
        # Both scenarios executing the pair themselves: the bounded negative.
        reload["phase_path"]["reconcile_write"] = True
        self.assertEqual(pair_verdict("pair1", [startup, reload])["verdict"], NEGATIVE)
        # A genuine reproduction is kept however incomplete the coverage.
        startup["target_path"] = {}
        self.assertEqual(pair_verdict("pair1", [startup, {**reload, "classified": {"pair1": 1}}])["verdict"],
                         "reproduced")

    BASELINE = {label: 2 for label in PHASE_WRITERS}

    @classmethod
    def reload_run(cls, *, accepted, missed, grown=None):
        # The reviewer's rejected-reload shape: startup coverage true, no accepted reload.
        grown = accepted if grown is None else grown
        targets = {label: True for label in PHASE_WRITERS + PHASE_READERS}
        return {"exit_status": 0, "seconds": 15.7, "detector": [], "runtime_fatal": [], "target_path": targets,
                "target_counts": {label: cls.BASELINE[label] + grown for label in PHASE_WRITERS},
                "tests": {"passed": ["TestGatewayRaceService"], "race_only_failures": [], "other_failures": [],
                          "runtime_fatal": [], "panics": 0},
                "results": [{"scenario": "service-reload", "b108_armed": -1}],
                "request_outcomes": {"GET /v1/models 200": 40, "POST /v1/messages claude json 200": 40},
                "accepted_reloads": accepted, "reloads_not_observed": missed, "config_writes": accepted + missed}

    def reload_scenario(self, sid, run):
        return scenario(sid, "series", [run], {}, ("TestGatewayRaceService",),
                        prerequisites=service_prerequisites("reload", [run], self.BASELINE),
                        startup_baseline=self.BASELINE, phase_path=phase_path([run], self.BASELINE))

    def test_rejected_reload_control_is_inconclusive(self):
        rejected = self.reload_scenario("control-rejected-reload", self.reload_run(accepted=0, missed=1))
        self.assertTrue(rejected["executed"])
        self.assertTrue(merge_coverage(rejected["runs"])["config_write"])  # startup coverage alone
        self.assertFalse(rejected["phase_path"]["config_write"])
        self.assertFalse(rejected["prerequisites"]["reloads_all_accepted"])
        for pair in ALL_PAIRS:
            self.assertEqual(pair_verdict(pair, [rejected])["verdict"], INCOMPLETE)
        self.assertEqual(control_verdicts([rejected])["rejected_reload"]["pair2"]["verdict"], INCOMPLETE)
        # Sentinels observed but no execution above the startup baseline: not reload-phase evidence.
        stale = self.reload_scenario("service-reload-traffic", self.reload_run(accepted=12, missed=0, grown=0))
        self.assertFalse(stale["prerequisites"]["reload_phase_client_updates"])
        self.assertEqual(pair_verdict("pair3", [stale])["verdict"], INCOMPLETE)
        accepted = self.reload_scenario("service-reload-traffic", self.reload_run(accepted=12, missed=0))
        self.assertTrue(accepted["prerequisites_met"])
        self.assertEqual(pair_verdict("pair2", [accepted])["verdict"], NEGATIVE)
        # Accepted reloads that never reached SetPluginHost: pair3 cannot be negative.
        partial = copy.deepcopy(accepted)
        partial["phase_path"]["set_plugin_host"] = False
        self.assertIn("target path not executed", pair_verdict("pair3", [partial])["verdict"])
        self.assertEqual(phase_path([self.reload_run(accepted=1, missed=0)], {})["config_write"], False)
        with self.assertRaises(AssertionError):  # an accepted "control" is no control
            control_verdicts([{**accepted, "id": "control-rejected-reload", "label": "control"}])

    def test_coverage_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "cover.out"
            profile.write_text("mode: atomic\n"
                               "github.com/router-for-me/CLIProxyAPI/v8/a/b.go:10.1,12.2 1 3\n"
                               "github.com/router-for-me/CLIProxyAPI/v8/a/b.go:20.1,22.2 1 0\n")
            hit = coverage(profile, {"x": ("a/b.go", 11, 11), "y": ("a/b.go", 21, 21), "z": ("a/c.go", 1, 99)})
            self.assertEqual(hit, {"x": True, "y": False, "z": False})
            self.assertEqual(coverage(Path(directory) / "absent", {"x": ("a/b.go", 1, 2)}), {"x": None})
            self.assertEqual(coverage_counts(profile, {"x": ("a/b.go", 11, 11), "y": ("a/b.go", 21, 21)}),
                             {"x": 3, "y": 0})
            self.assertEqual(coverage_counts(Path(directory) / "absent", {"x": ("a/b.go", 1, 2)}), {})

    @staticmethod
    def fake_scenario(sid, build, *, met=True):
        label, pairs, required_for, _ = SCENARIOS[sid]
        covered = {name: True for group in PAIR_TARGETS.values() for names in group for name in names}
        row = {"id": sid, "build": build, "label": label, "pairs": list(pairs), "required_for": list(required_for),
               "runs": [{}], "executed": True, "prerequisites": {"workload": met}, "prerequisites_met": met,
               "classified": {}, "runtime_fatal": {}, "target_path": covered}
        if "reload" in sid:
            row.update(phase_path=dict(covered), startup_baseline={"config_write": 1})
        return row

    def document(self):
        builds = [{"label": "series", "diagnostics": "/nix/store/d", "gateway": "/nix/store/g",
                   "source": "/nix/store/s", "go_modules": PINNED_GO_MODULES, "go_modules_pinned": True,
                   "go_modules_consumed": {"env_go_modules": PINNED_GO_MODULES,
                                           "go_modules_input_outputs": [PINNED_GO_MODULES]},
                   "patches": list(harness.PATCH_ROWS)}]
        scenarios = [self.fake_scenario(sid, build["label"], met=sid != "control-rejected-reload")
                     for build in builds for sid, row in SCENARIOS.items() if build["label"] in row[3]]
        return {"schema": SCHEMA, "observation_only": True, "negative_meaning": NEGATIVE, "builds": builds,
                "scenarios": scenarios,
                "verdicts": {build["label"]: verdicts_for(build["label"], scenarios) for build in builds},
                "controls": control_verdicts(scenarios)}

    def test_report_validation(self):
        validate_report(self.document())
        self.assertEqual(self.document()["verdicts"]["series"]["b108"]["manager-http"]["verdict"], NEGATIVE)

        def drop(document, predicate):
            document["scenarios"] = [s for s in document["scenarios"] if not predicate(s)]

        def reverdict(document):  # recompute, so only the inventory/reference checks can catch it
            document["verdicts"] = {b["label"]: verdicts_for(b["label"], document["scenarios"])
                                    for b in document["builds"]}
        for mutate in (lambda d: d.update(negative_meaning="race absent"),
                       lambda d: d["builds"][0].update(go_modules="/nix/store/other-go-modules"),
                       # the vendor pin judged on the consumed inputs, not the echoed attribute
                       lambda d: d["builds"][0]["go_modules_consumed"].update(
                           go_modules_input_outputs=[PINNED_GO_MODULES, "/nix/store/x-gwtest-race-go-modules"]),
                       lambda d: d["builds"][0].pop("go_modules_consumed"),
                       lambda d: d["builds"][0].update(patches=[]),
                       lambda d: d["scenarios"][0].update(executed=False),
                       lambda d: d["scenarios"][0].update(label="unlabelled"),
                       lambda d: d["scenarios"][0].update(prerequisites={}),
                       lambda d: next(s for s in d["scenarios"] if s["id"] == "service-reload-traffic").pop("phase_path"),
                       lambda d: d["verdicts"]["series"]["b108"].pop("manager-http"),
                       lambda d: d.update(scenarios=[]),
                       # one b108 label missing, verdicts recomputed ("not run")
                       lambda d: (drop(d, lambda s: s["label"] == "manager-http"), reverdict(d)),
                       lambda d: d["verdicts"]["series"]["b108"].pop("manager-http-config-empty-metadata"),
                       # a second, unlabelled build
                       lambda d: d["builds"].append({**d["builds"][0], "label": "series+extra"}),
                       lambda d: d["builds"].pop(),
                       # a stored verdict that names a scenario which never ran
                       lambda d: d["verdicts"]["series"]["pair2"]["service"].update(scenarios=["ghost"]),
                       # a stored verdict that disagrees with its scenarios
                       lambda d: d["verdicts"]["series"]["pair1"]["unit"].update(verdict="reproduced"),
                       lambda d: drop(d, lambda s: s["id"] == "control-rejected-reload"),
                       lambda d: d.pop("controls"),
                       lambda d: d.update(note="sk-ant-oat-gwtest-x"),
                       lambda d: d.update(note=FIXTURE_GATEWAY_TOKEN)):
            document = self.document()
            mutate(document)
            with self.assertRaises((AssertionError, ValueError, KeyError)):
                validate_report(document)

    def test_publication_is_private_and_failure_propagating(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "evidence"
            path = publish(out, self.document())
            self.assertEqual(out.stat().st_mode & 0o777, 0o700)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            broken = self.document()
            broken["scenarios"][0]["runs"] = []
            with self.assertRaises(AssertionError):
                publish(Path(directory) / "second", broken)
            self.assertFalse((Path(directory) / "second").exists())

    def test_b108_prerequisites_need_preparation_and_completion(self):
        def run(**result):
            return {"credentials": 8, "armed": 8, "credentials_completed": 8, "upstream_hits": {FILE_ROUTE: 128},
                    "results": [{"b108_armed": 8, "file_prepared": 8, "file_upstream_answered": 128,
                                 "file_upstream_refused": 0, **result}],
                    "target_path": {"manager_prepare": True}}
        self.assertTrue(all(service_prerequisites("file", [run()]).values()))
        self.assertFalse(all(service_prerequisites("file", [run(file_prepared=0)]).values()))
        self.assertFalse(all(service_prerequisites("file", [run(file_upstream_refused=1)]).values()))
        self.assertFalse(all(service_prerequisites("file", [{**run(), "credentials_completed": 7}]).values()))
        self.assertFalse(all(service_prerequisites("config", [{**run(), "target_path": {}}]).values()))
        self.assertTrue(all(service_prerequisites("config", [run()]).values()))


    def test_omissions_are_explicit_and_dependency_closed(self):
        from check_gateway_patch_revert import (DIAGNOSTIC_INSTALL_PHASE, DIAGNOSTIC_PATCH_PHASE,
                                                SHIPPED_INSTALL_STEP)
        patch = "cli-proxy-api-auth-snapshot-locking.patch"
        expression = build_expression(Path("/w"), [patch])
        self.assertIn(patch, expression)
        self.assertIn("patches = builtins.filter keep old.patches", expression)
        self.assertIn("gatewayPatches = builtins.filter keep original.gatewayPatches", expression)
        # The diagnostics depend on the omitted gateway's output, which must
        # install without the shipped-target inspect (it refuses a partial
        # series); the full-series control stays the shipped derivation.
        self.assertIn(DIAGNOSTIC_INSTALL_PHASE, expression)
        # Only inside the omission branch: the full series applies offset-free.
        self.assertIn(DIAGNOSTIC_PATCH_PHASE, expression.split("original.overrideAttrs", 1)[1])
        self.assertIn("gateway = if omitted == [ ] then original else original.overrideAttrs", expression)
        self.assertIn("cliProxyApi = gateway;", expression)
        self.assertIn(SHIPPED_INSTALL_STEP + " --target", (NIX_DIR / "gateway.nix").read_text())
        for omitted in (["unknown.patch"], [patch, patch], ["cli-proxy-api-watcher-parentdir.patch"]):
            with self.assertRaises(ValueError):
                build_expression(Path("/w"), omitted)
        document = self.document()
        document["omitted"] = [patch]
        with self.assertRaises(AssertionError):
            validate_report(document)
        for build in document["builds"]:
            build["patches"].remove(patch)
        validate_report(document)

    def test_other_fatal_counts_survive_scenario_summary(self):
        run = {"detector": [], "runtime_fatal": [{"classification": "other", "message": "concurrent map writes"}],
               "tests": {"other_failures": []}, "exit_status": 2, "seconds": 1}
        summary = scenario("unit-pair1-reconcile-clone", "series", [run], {}, (), prerequisites={"ran": True})
        self.assertEqual(summary["runtime_fatal"], {"other": 1})
        self.assertEqual(summary["runs_ended_by_runtime_fatal"], 1)

    def matrix_document(self):
        document = self.document()
        document["matrix_runs"] = MATRIX_RUNS
        document["targets"] = {"series": {name: ["fixture.go", 1, 2] for name in TARGET_LABELS}}
        for cell_index, cell in enumerate(document["scenarios"], 1):
            sid = cell["id"]
            roots = matrix_roots(sid)
            cell["bound"] = {"processes": 100}
            if "reload" in sid:
                cell["startup_baseline"] = {name: 1 for name in PHASE_WRITERS}
                cell["bound"]["config_writes"] = 1 if sid.startswith("control-") else 20
            if sid.startswith("manager-http-"):
                cell["bound"].update(credentials=8, concurrent_per_credential=16)
            runs = []
            for index in range(100):
                run = {"invocation_id": f"{cell_index:02x}{index:030x}", "process_pid": 123,
                       "process_started": True, "exit_status": 0, "seconds": 0,
                       "tests": {"passed": list(roots), "race_only_failures": [], "other_failures": [], "runtime_fatal": [], "panics": 0},
                       "root_tests": {name: {"attempted": 1, "passed": 1, "failed": 0, "skipped": 0} for name in roots},
                       "results": [] if sid in ("unit-plugin-host-retained", "direct-b108-retained-upstream") else [{}],
                       "runtime_fatal": [], "detector": [],
                       "target_path": {name: True for name in TARGET_LABELS},
                       "target_counts": {name: 21 for name in TARGET_LABELS}}
                if sid == "unit-pair1-reconcile-clone":
                    run["results"] = [{"rounds": 2000, "updates": 2000, "reconciles": 2000}]
                elif sid == "unit-pairs23-reload-handlers":
                    run["results"] = [{"rounds": 300, "applied": 300, "requests": 10}]
                elif sid == "direct-b108-setup-token":
                    run["results"] = [{"rounds": 200, "goroutines": 32, "prepared": 6400, "errors": 0, "profile_fetches": 0}]
                elif sid == "service-startup-overlap":
                    run["request_outcomes"] = {"pre-listen transport": 1, "GET /v1/models 200": 1, "POST /v1/messages 200": 1}
                    run["target_counts"] = {name: 1 for name in TARGET_LABELS}
                elif "reload" in sid:
                    rejected = sid.startswith("control-")
                    run.update(config_writes=1 if rejected else 20, auth_updates=1 if rejected else 20,
                               accepted_reloads=0 if rejected else 20, reloads_not_observed=1 if rejected else 0,
                               rejected_reload_control=rejected,
                               request_outcomes={"GET /v1/models 200": 10, "POST /v1/messages 200": 10})
                    if rejected:
                        run["target_counts"] = {name: 1 for name in TARGET_LABELS}
                elif sid.startswith("manager-http-"):
                    mode = "file" if sid.endswith("file-auth") else "config"
                    run.update(credential_shape=mode, credentials=8, armed=8, credentials_completed=8,
                               concurrent_per_credential=16, request_outcomes={"POST /v1/messages b108 json 200": 128},
                               upstream_hits={FILE_ROUTE: 128}, results=[{"b108_armed": 8, "file_prepared": 8,
                                   "file_upstream_answered": 128, "file_upstream_refused": 0}])
                runs.append(run)
            cell["runs"] = runs
            cell["seconds"] = 0
            if "reload" in sid:
                cell["phase_path"] = phase_path(runs, cell["startup_baseline"])
            cell["matrix"] = matrix_accounting(sid, runs, cell.get("startup_baseline"))
        document["verdicts"] = {"series": verdicts_for("series", document["scenarios"])}
        document["controls"] = control_verdicts(document["scenarios"])
        return document

    def test_matrix_counts_preserve_each_root_and_allow_pid_reuse(self):
        counts = root_test_counts("=== RUN   TestA\n=== RUN   TestA/sub\n    --- PASS: TestA/sub (0.0s)\n"
                                  "--- PASS: TestA (0.0s)\n=== RUN   TestA\n--- FAIL: TestA (0.0s)\n"
                                  "=== RUN   TestB\n--- SKIP: TestB (0.0s)\n")
        self.assertEqual(counts, {"TestA": {"attempted": 2, "passed": 1, "failed": 1, "skipped": 0},
                                  "TestB": {"attempted": 1, "passed": 0, "failed": 0, "skipped": 1}})
        document = self.matrix_document()
        validate_report(document)  # every PID is deliberately equal, every invocation distinct
        for cell in document["scenarios"]:
            self.assertEqual(cell["matrix"]["completed_processes"], 100)
            self.assertEqual(cell["matrix"]["distinct_invocations"], 100)
        self.assertTrue(all(value["verdict"] == INCOMPLETE for value in document["controls"]["rejected_reload"].values()))

    def test_matrix_rejects_underfilled_roots_processes_workloads_and_targets(self):
        def cell(doc, sid):
            return next(row for row in doc["scenarios"] if row["id"] == sid)
        def first(doc, sid):
            return cell(doc, sid)["runs"][0]
        mutations = {
            "99 processes": lambda d: cell(d, "service-startup-overlap")["runs"].pop(),
            "missing root": lambda d: first(d, "unit-plugin-host-retained")["root_tests"].pop(RETAINED_PLUGIN_HOST[0]),
            "99 completions": lambda d: first(d, "direct-b108-retained-upstream")["root_tests"][RETAINED_B108[0]].update(passed=0),
            "duplicated receipt": lambda d: cell(d, "service-startup-overlap")["runs"].__setitem__(1, copy.deepcopy(first(d, "service-startup-overlap"))),
            "one process 100 writes": lambda d: cell(d, "service-reload-traffic").update(runs=[{**first(d, "service-reload-traffic"), "config_writes": 100, "accepted_reloads": 100}]),
            "missing target window": lambda d: first(d, "unit-pair1-reconcile-clone")["target_path"].update(clone_after_unlock=False),
            "short unit workload": lambda d: first(d, "unit-pairs23-reload-handlers")["results"][0].update(applied=299),
            "duplicate workload receipt": lambda d: first(d, "unit-pair1-reconcile-clone")["results"].append(
                copy.deepcopy(first(d, "unit-pair1-reconcile-clone")["results"][0])),
            "127 HTTP requests": lambda d: first(d, "manager-http-b108-file-auth")["request_outcomes"].update({"POST /v1/messages b108 json 200": 127}),
            "wrong config burst": lambda d: first(d, "manager-http-b108-config-empty-metadata").update(concurrent_per_credential=15),
            "accepted control": lambda d: first(d, "control-rejected-reload").update(accepted_reloads=1, reloads_not_observed=0),
            "missing extent": lambda d: d["targets"]["series"].pop("profile_fetch"),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                document = self.matrix_document()
                mutate(document)
                # Recompute stored counters, so rejection cannot depend only
                # on stale aggregate metadata hiding the underlying failure.
                for row in document["scenarios"]:
                    row["matrix"] = matrix_accounting(row["id"], row["runs"], row.get("startup_baseline"))
                with self.assertRaises(AssertionError):
                    validate_report(document)

    def test_matrix_drivers_make_independent_invocations_and_keep_inner_work(self):
        templates = {cell["id"]: cell["runs"] for cell in self.matrix_document()["scenarios"]}
        indices, attempts = {}, []
        def take(sid):
            index = indices.get(sid, 0)
            indices[sid] = index + 1
            return copy.deepcopy(templates[sid][index])
        def unit(binary, argv, root, **kwargs):
            sid = next(sid for sid, roots in MATRIX_UNIT_ROOTS.items()
                       if argv[0] == "-test.run=^(" + "|".join(roots) + ")$")
            self.assertEqual(argv[1:], ["-test.count=1"])
            rounds = {"unit-pair1-reconcile-clone": "2000", "unit-pairs23-reload-handlers": "300",
                      "direct-b108-setup-token": "200"}.get(sid, "0")
            self.assertEqual(kwargs["env"]["GWTEST_ROUNDS"], rounds)
            return take(sid)
        def service(_diagnostics, _targets, sid, _drive, **kwargs):
            sid = "service-startup-overlap" if sid == "service-startup" else sid
            if sid.startswith("manager-http-"):
                self.assertEqual(kwargs, {"b108": 8, "b108_mode": "file" if sid.endswith("file-auth") else "config"})
            return take(sid)
        record = lambda sid, index, run: attempts.append((sid, index, run["invocation_id"]))
        with mock.patch(__name__ + ".isolated_run", side_effect=unit) as units, \
                mock.patch(__name__ + ".service_run", side_effect=service) as services, \
                mock.patch(__name__ + ".drive_reload", wraps=drive_reload) as reload_driver, \
                mock.patch(__name__ + ".drive_b108", wraps=drive_b108) as http_driver, mock.patch("sys.stdout"):
            cells = unit_scenarios("series", Path("/fixture"), {}, matrix_runs=100, on_attempt=record)
            cells += service_scenarios("series", Path("/fixture"), {}, matrix_runs=100, on_attempt=record)
        self.assertEqual(units.call_count, 500)
        self.assertEqual(services.call_count, 500)
        self.assertEqual(indices, {sid: 100 for sid in SCENARIOS})
        self.assertEqual(len(attempts), 1000)
        self.assertEqual(len({row[2] for row in attempts}), 1000)
        self.assertEqual(reload_driver.call_args_list, [mock.call(20, reject=False)] * 100 + [mock.call(1, reject=True)] * 100)
        self.assertEqual(http_driver.call_args_list, [mock.call(16)] * 200)
        for cell in cells:
            validate_matrix_cell(cell)

    def test_matrix_keeps_races_but_stops_incomplete_attempts_without_retry(self):
        template = self.matrix_document()["scenarios"][0]
        runs = copy.deepcopy(template["runs"])
        root = matrix_roots(template["id"])[0]
        runs[0]["tests"].update(passed=[], race_only_failures=[root])
        runs[0]["root_tests"][root].update(passed=0, failed=1)
        runs[0].update(exit_status=1, detector=[{"classification": "pair1", "count": 1, "frames": []}])
        with mock.patch("sys.stdout"):
            invoke = mock.Mock(side_effect=runs)
            observed = matrix_invocations(template["id"], invoke)
        self.assertEqual(invoke.call_count, 100)
        cell = scenario(template["id"], "series", observed, {"processes": 100}, (root,), prerequisites={"workload": True})
        cell["matrix"] = matrix_accounting(cell["id"], observed)
        validate_matrix_cell(cell)
        self.assertEqual(cell["matrix"]["race_only_root_failures"], 1)
        self.assertEqual(pair_verdict("pair1", [cell])["verdict"], "reproduced")
        hidden = copy.deepcopy(cell)
        hidden["classified"] = {}
        with self.assertRaisesRegex(AssertionError, "discarded"):
            validate_matrix_cell(hidden)
        fatal = copy.deepcopy(runs[0])
        fatal["root_tests"][root].update(failed=0)
        fatal.update(exit_status=2, runtime_fatal=[{"classification": "pair1", "message": "concurrent map writes"}])
        attempts = []
        with mock.patch("sys.stdout"):
            invoke = mock.Mock(return_value=fatal)
            observed = matrix_invocations(template["id"], invoke, on_attempt=lambda sid, index, run: attempts.append(run))
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(len(observed), 1)
        self.assertEqual(attempts[0]["runtime_fatal"], fatal["runtime_fatal"])
        self.assertEqual(matrix_accounting(template["id"], observed)["completed_processes"], 0)

    def test_incomplete_matrix_retains_attempt_receipts_and_never_publishes_acceptance(self):
        document = self.matrix_document()
        cell = document["scenarios"][0]
        cell["runs"] = cell["runs"][:1]
        root = matrix_roots(cell["id"])[0]
        cell["runs"][0]["root_tests"][root]["passed"] = 0
        cell["matrix"] = matrix_accounting(cell["id"], cell["runs"])
        def units(*_args, on_attempt=None, **_kwargs):
            on_attempt(cell["id"], 1, cell["runs"][0])
            return [cell]
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "matrix"
            args = parse_args(["--matrix-runs", "100", "--series-diagnostics", "/nix/store/fixture", "--out", str(out)])
            with mock.patch.object(probe, "network_isolation_available", return_value=None), \
                    mock.patch.object(subprocess, "check_output", side_effect=["fixture-head\n", ""]), \
                    mock.patch(__name__ + ".describe", return_value=document["builds"][0]), \
                    mock.patch(__name__ + ".load_targets", return_value=document["targets"]["series"]), \
                    mock.patch(__name__ + ".unit_scenarios", side_effect=units), \
                    mock.patch(__name__ + ".service_scenarios") as services, mock.patch("sys.stdout"), mock.patch("sys.stderr"):
                self.assertEqual(run_all(args), 1)
            services.assert_not_called()
            self.assertFalse((out / "races.json").exists())
            rows = [json.loads(line) for line in (out / "matrix-attempts.jsonl").read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["run"]["root_tests"][root]["passed"], 0)
            partial = json.loads((out / "races-incomplete.json").read_text())
            self.assertEqual(partial["schema"], "gwtest-races-incomplete-v1")
            self.assertFalse(partial["product_gate"])
            self.assertEqual(partial["observations"]["scenarios"][0]["matrix"]["requested_processes"], 100)

    def test_matrix_mode_is_explicit_and_does_not_silently_override_bounds(self):
        normal = parse_args([])
        self.assertEqual((normal.matrix_runs, normal.startups, normal.reloads, normal.http_runs), (None, 10, 20, 3))
        matrix = parse_args(["--matrix-runs", "100"])
        self.assertEqual((matrix.matrix_runs, matrix.startups, matrix.reloads, matrix.http_runs), (100, 100, 20, 100))
        for argv in (["--matrix-runs", "99"], ["--matrix-runs", "100", "--reloads", "100"], ["--matrix-runs100"]):
            with self.subTest(argv=argv), mock.patch("sys.stderr"), self.assertRaises(SystemExit) as error:
                parse_args(argv)
            self.assertEqual(error.exception.code, 2)

    def test_expression_pins_vendor_modules(self):
        expression = build_expression(Path("/w"))
        self.assertIn(PINNED_GO_MODULES, expression)
        self.assertIn("gateway-race.nix", expression)
        self.assertIn("vendor-inputs.nix\" diag == [ gateway.goModules.drvPath ]", expression)
        self.assertNotIn("diag.goModules", expression)  # never the attribute it set
        self.assertNotIn("gateway-check.nix", expression)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=Path.home() / ".cache/charmer-gateway-races" /
                        datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    parser.add_argument("--omit-patch", action="append", default=[], choices=list(harness.PATCH_ROWS),
                        help="diagnostic-only omission; must be dependency closed")
    parser.add_argument("--series-diagnostics", help="reuse a built gateway-race.nix output (series)")
    parser.add_argument("--matrix-runs", type=int, choices=(MATRIX_RUNS,),
                        help="D217: 100 independent invocations per cell; fixed inner workloads")
    parser.add_argument("--startups", type=int)
    parser.add_argument("--reloads", type=int)
    parser.add_argument("--http-runs", type=int)
    parser.add_argument("--self-test", action="store_true", help="pure parser/verdict/evidence tests only")
    parser.add_argument("--keep-logs", type=Path, help="private directory for raw local logs (not evidence)")
    args = parser.parse_args(argv)
    if args.matrix_runs is not None:
        if any(getattr(args, name) is not None for name in ("startups", "reloads", "http_runs")):
            parser.error("--matrix-runs fixes every bound; do not combine it with --startups/--reloads/--http-runs")
        args.startups, args.reloads, args.http_runs = MATRIX_RUNS, 20, MATRIX_RUNS
    else:
        for name, default in (("startups", 10), ("reloads", 20), ("http_runs", 3)):
            if getattr(args, name) is None:
                setattr(args, name, default)
    return args


def main(argv=None):
    args = parse_args(argv)
    global KEEP_LOGS
    KEEP_LOGS = args.keep_logs
    if args.self_test:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(SelfTests)
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
    return run_all(args)


if __name__ == "__main__":
    raise SystemExit(main())
