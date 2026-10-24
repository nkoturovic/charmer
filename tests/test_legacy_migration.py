"""``charmer migrate from-legacy``: an installation of the earlier name moves
onto charmer once, journaled, and back.

Every test lays out a whole installation release 1.1.0 left behind
(``tests/_legacy_tree.py``: the files 1.1.0's own code wrote, from the
``v1.1.0`` tag, and everything else such an installation keeps) in a
temporary HOME, with charmer's fresh installation beside it. The service
manager is a fake over the unit directory; the session inventory, the
persistence hold, the shim writer, the gateway start, the verification and
the earlier launcher's repair are seams (``legacy_migration.Seams``). Nothing
runs a gateway, a client, the earlier release or ``systemctl``.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import _legacy_tree as lt
from _catalog import FIXTURE_GATEWAY_TOKEN, FIXTURE_ROOT
from _v4 import V4Case
from charmer import (assets, catalog, continuity, endpoint, gateway_events, gateway_hold, gateway_inhibition,
                     install_receipt, legacy, legacy_migration as lm, pin, scope, sessions, state, strict_json)

OLD_UNIT, OLD_FAILURE = legacy.LEGACY_UNIT, legacy.LEGACY_FAILURE_UNIT


class Crash(BaseException):
    """A kill between a step's effects and its journal entry (never caught)."""


class Harness:
    """One tree, its fake service manager and the seams over it."""

    def __init__(self, case: unittest.TestCase, *, home: str = "home", enabled: bool | None = None,
                 active: bool | None = None, runtime: bool = False, **build: object) -> None:
        self.case = case
        # Resolved: --purge reaches what it removes only through real
        # folders, from / down (a temporary folder behind a link refuses).
        self.base = Path(os.path.realpath(tempfile.mkdtemp(prefix="charmer-legacy-")))
        case.addCleanup(shutil.rmtree, self.base, True)
        for name in ("xdg_config", "xdg_state"):
            if callable(build.get(name)):
                build[name] = build[name](self.base / home)
        self.tree = lt.build(self.base / home, **build)
        self.layout = self.tree.layout
        self.environ = self.tree.environ
        on = bool(build.get("service", True)) if enabled is None else enabled
        self.systemd = lt.FakeSystemd(self.layout.unit_dir, enabled={OLD_UNIT: on},
                                      active={OLD_UNIT: on if active is None else active},
                                      runtime=(OLD_UNIT,) if runtime else ())
        self.live: list[str] = []
        self.holds: list[str] = []
        # charmer's own gateway: its hold, and whether its stop succeeds.
        self.charmer_holds: list[str] = []
        self.hold_seen: list[str] = []
        self.stop_ok = True
        self.stop_refusal = "the service manager did not stop charmer-gateway"
        self.verify_problems: list[str] = []
        self.calls: list[str] = []
        self.tokens: list[str] = []
        self.repair_converges = True
        self.repair_crash = False
        self.repaired: list[str] = []
        self.launch_items: dict = {}
        self.repair_result: tuple[bool, list[str]] = (True, ["Repair-all: 1 session(s) converged to record authority."])

    def _token(self, environ) -> None:
        # The token a writer gets is the charmer root's own record's.
        token = environ.get(gateway_inhibition.TOKEN_ENV)
        record = gateway_inhibition.read(self.layout.state)
        self.case.assertIsNotNone(record)
        self.case.assertEqual(token, record.token)
        self.case.assertFalse(os.path.lexists(self.layout.legacy_state))
        self.tokens.append(token)

    def shims(self, environ) -> None:
        self._token(environ)
        self.calls.append("shims")
        for name in lm.CHARMER_SHIMS:
            lt.write(self.layout.state / "bin" / name, "#!/bin/sh\n", 0o700)

    def gateway(self, environ):
        self._token(environ)
        self.calls.append("gateway")
        for name, path in self.layout.charmer_units():
            lt.write(path, lt.charmer_unit_text(name), 0o644)
        self.systemd(["systemctl", "--user", "enable", "--now", f"{lm.CHARMER_UNIT}.service"])
        # The unit's own preparation renders the configuration and its receipt.
        lt.write(self.layout.gateway / "config.yaml", f"auth-dir: {self.layout.data}/auth\n")
        lt.dump(self.layout.gateway / "prepared.json", {"state_root": str(self.layout.state)})
        return True, "ready"

    def verify(self, environ, since) -> list[str]:
        self.calls.append("verify")
        self.case.assertIsNone(gateway_inhibition.read(self.layout.state))
        return list(self.verify_problems)

    def liveness(self, root):
        if self.live == ["unknown"]:
            raise lm.LivenessUnknown("the process table is unreadable")
        return list(self.live)

    def repair(self, environ):
        """The earlier launcher's ``doctor --repair-all``: called once, when
        everything else is back and no guard of the run is held. Unless
        ``repair_converges`` is off, it does what the earlier repair does to
        a scope charmer compiled: the items the launch fence proves stay
        (``launch_items`` keeps what it found), everything else is compiled
        in the earlier form (here: the hooks and token helper name the
        earlier shims, the permissions the earlier folders, and no subagent
        status line is compiled)."""

        layout = self.layout
        self.calls.append("repair")
        self.case.assertNotIn(gateway_inhibition.TOKEN_ENV, environ)
        self.case.assertEqual(environ.get("HOME"), str(self.tree.home))
        self.case.assertFalse(os.path.lexists(layout.state))
        self.case.assertIsNone(gateway_inhibition.read(layout.legacy_state))
        self.case.assertEqual(lm.locate(layout).status, lm.REVERTING)
        for name in legacy.LEGACY_SHIMS:
            self.case.assertTrue(os.access(layout.legacy_state / "bin" / name, os.X_OK), name)
        # The earlier gateway runs again before its own launcher repairs.
        self.case.assertEqual(self.systemd.unit(OLD_UNIT), {"enabled": True, "runtime": False, "active": True})
        self.repaired = lm._charmer_scopes(layout)
        self.launch_items = {}
        for mid in self.repaired:
            live = layout.legacy_state / "scopes" / mid
            settings = json.loads((live / scope.SETTINGS_RELPATH).read_bytes())
            self.launch_items[mid] = ({key: settings[key] for key in sessions.LAUNCH_TIME_SETTINGS_KEYS
                                       if key in settings}, (live / scope.LEAD_SET_JSON).read_bytes())
        if self.repair_crash:
            raise Crash("repair")
        if self.repair_converges:
            replace = {f"{layout.state}/bin/{new}": f"{layout.legacy_state}/bin/{old}"
                       for new, old in zip(lm.CHARMER_SHIMS, legacy.LEGACY_SHIMS)}
            replace.update({f"~/{root}/charmer/": f"~/{root}/{legacy.LEGACY_DIR}/"
                            for root in (".config", ".local/share", ".local/state")})
            for mid in self.repaired:
                path = layout.legacy_state / "scopes" / mid / scope.SETTINGS_RELPATH
                settings = json.loads(path.read_bytes())
                fenced = {key: settings.pop(key) for key in sessions.LAUNCH_TIME_SETTINGS_KEYS if key in settings}
                settings.pop(scope.SUBAGENT_STATUS_LINE_KEY, None)  # the earlier release compiles none
                text = json.dumps(settings)
                for new, old in replace.items():
                    text = text.replace(new, old)
                state.atomic_write(path, strict_json.canonical_file_bytes({**json.loads(text), **fenced}))
        return self.repair_result

    def charmer_hold(self, layout) -> list[str]:
        """Evaluated before anything stops (the journal's status then kept)."""

        self.calls.append("charmer-hold")
        self.hold_seen.append(lm.locate(self.layout).status)
        return list(self.charmer_holds)

    def stop_gateway(self, environ) -> tuple[bool, str]:
        """charmer's own stop: under the run's token, its unit stopped."""

        record = gateway_inhibition.read(self.layout.state)
        self.case.assertIsNotNone(record)
        self.case.assertEqual(environ.get(gateway_inhibition.TOKEN_ENV), record.token)
        self.calls.append("stop")
        if not self.stop_ok:
            return False, self.stop_refusal
        if os.path.lexists(self.layout.unit_dir / f"{lm.CHARMER_UNIT}.service"):
            self.systemd(["systemctl", "--user", "stop", f"{lm.CHARMER_UNIT}.service"])
        return True, "gateway stopped"

    def seams(self) -> lm.Seams:
        return lm.Seams(installed=lambda _env: None, liveness=self.liveness, hold=lambda _layout: list(self.holds),
                        shims=self.shims, gateway=self.gateway, verify=self.verify, repair=self.repair,
                        charmer_hold=self.charmer_hold, stop_gateway=self.stop_gateway, runner=self.systemd)

    def migrate(self) -> lm.Report:
        return lm.migrate(self.environ, self.seams())

    def back(self) -> lm.Report:
        return lm.back(self.environ, self.seams())

    # -------------------------------------------------------- snapshots
    # The gateway's rendered pair: the next start of either release's
    # gateway renders both again through its own preparation (never edited,
    # never copied: the configuration embeds the gateway's keys).
    RENDERED = ("config.yaml", "prepared.json")

    @staticmethod
    def _guard_file(path: Path) -> bool:
        """An empty lock file a guard creates (the in-use, start, fence and
        migration locks), or the journal the move keeps as evidence."""

        if lm.JOURNAL_DIR in path.parts:
            return True
        return (path.is_file() and not path.is_symlink() and path.stat().st_size == 0
                and (path.name.endswith(".lock") or path.name == ".in-use"))

    def legacy_view(self) -> dict:
        """The earlier installation as it stands, guard files and the rendered pair aside."""

        def skip(path: Path) -> bool:
            return self._guard_file(path) or (path.name in self.RENDERED and path.parent == self.layout.legacy_gateway)

        roots = [*self.layout.legacy_roots(), self.layout.legacy_data, self.layout.unit_dir, self.layout.bin_dir,
                 self.tree.home / ".profile"]
        return lt.snapshot(*roots, skip=skip)

    def remember(self) -> dict:
        self._before = self.legacy_view()
        return self._before

    def charmer_view(self) -> dict:
        roots = [self.layout.state, self.layout.config, self.layout.gateway, self.layout.data, self.layout.unit_dir,
                 self.layout.bin_dir, *self.layout.legacy_roots(), self.layout.legacy_data]
        return lt.snapshot(*roots, skip=self._guard_file)

    def journal(self) -> lm.Journal:
        journal = lm.locate(self.layout)
        self.case.assertIsNotNone(journal)
        return journal


def moved(layout: lm.Layout, old: Path) -> Path:
    found = layout.mapped(old)
    assert found is not None, old
    return found


def renamed_map() -> dict:
    return catalog.load_catalog(assets.default_asset_root()).renamed


def forward(value, renamed):
    """``value`` with every earlier selector under its charmer name (what a
    resume under charmer records)."""

    if isinstance(value, str):
        return catalog.renamed_selector(renamed, value)
    if isinstance(value, dict):
        return {key: forward(item, renamed) for key, item in value.items()}
    if isinstance(value, list):
        return [forward(item, renamed) for item in value]
    return value


def without_digests(record: dict) -> dict:
    return {key: value for key, value in record.items() if key not in ("applied_hash", "launch_fence")}


def charmer_names(record, renamed) -> list[str]:
    return [value for value in lm._strings(record) if lm.earlier_selector(renamed, value) != value]


def resumed_under_charmer(harness: Harness) -> dict:
    """What a resume under charmer leaves after the move (stated, not run;
    ``ResumeAfterMigrationTests`` runs it), from the scope the earlier
    release compiled: the record under charmer's selector names, its
    ``applied_hash`` with them; the scope's launch-time items in charmer's
    form (selectors, ``CHARMER_`` lifecycle variables, the picker's
    ``charmer ·`` descriptions, ``lead-set.json``) with the record's launch
    fence proving them; its hooks naming charmer's shims. The fixture
    catalog renames the record's selectors, so the run reads that catalog.
    The record and the scope's two launch files before."""

    harness.environ["CHARMER_ASSETS"] = str(FIXTURE_ROOT)
    renamed = catalog.load_catalog(FIXTURE_ROOT).renamed
    layout, mid = harness.layout, harness.tree.mid
    path = layout.state / "sessions" / f"{mid}.json"
    live = layout.state / "scopes" / mid
    before = strict_json.loads(path.read_bytes())
    files = {name: (live / name).read_bytes() for name in (scope.SETTINGS_RELPATH, scope.LEAD_SET_JSON)}
    settings = forward(json.loads(files[scope.SETTINGS_RELPATH]), renamed)
    settings["availableModels"] = sorted(settings["availableModels"])
    settings["env"] = {(lm.CHARMER_ENV_PREFIX + key.removeprefix(legacy.ENVIRONMENT_PREFIX)
                        if key.startswith(legacy.ENVIRONMENT_PREFIX) else key): value
                       for key, value in settings["env"].items()}
    for option in settings["modelPicker"]["options"]:
        option["description"] = option["description"].replace(lm.EARLIER_PICKER_PREFIX, lm.PICKER_PREFIX)
    lead_set = strict_json.canonical_file_bytes(forward(json.loads(files[scope.LEAD_SET_JSON]), renamed))
    text = json.dumps(settings)
    for old, new in zip(legacy.LEGACY_SHIMS, lm.CHARMER_SHIMS):
        text = text.replace(f"{layout.legacy_state}/bin/{old}", f"{layout.state}/bin/{new}")
    settings = json.loads(text)
    record = forward(before, renamed)
    harness.case.assertNotEqual(record["applied"], before["applied"])
    harness.case.assertEqual(before["launch_fence"], sessions.launch_digest(json.loads(files[scope.SETTINGS_RELPATH]),
                                                                           files[scope.LEAD_SET_JSON]))
    record["applied_hash"] = strict_json.bundle_digest(record["applied"])
    record["launch_fence"] = sessions.launch_digest(settings, lead_set)
    state.atomic_write(path, strict_json.canonical_file_bytes(record))
    state.atomic_write(live / scope.SETTINGS_RELPATH, strict_json.canonical_file_bytes(settings))
    state.atomic_write(live / scope.LEAD_SET_JSON, lead_set)
    return {"record": before, **files}


class DryRunTests(unittest.TestCase):
    def test_the_tree_is_the_installation_1_1_0_left(self) -> None:
        harness = Harness(self)
        self.assertEqual(sorted(os.listdir(harness.layout.legacy_config)), sorted(lt.CONFIG_ENTRIES))
        self.assertEqual(len(lt.CONFIG_ENTRIES), 27)
        for name in (OLD_UNIT, OLD_FAILURE):
            self.assertEqual(lm._first_line(harness.tree.unit(name)), legacy.LEGACY_UNIT_HEADER)
        for name in legacy.LEGACY_LAUNCHERS:
            self.assertTrue(lm.launcher_owned(harness.tree.launcher(name)))
        self.assertEqual(sorted(os.listdir(harness.layout.legacy_state / "bin")), sorted(legacy.LEGACY_SHIMS))
        self.assertEqual((harness.layout.legacy_state / "channel").read_text(), "bundle\n")
        self.assertEqual((harness.layout.legacy_state / "state-version").read_text(), "4\n")
        receipt = install_receipt.read(harness.layout.legacy_install)
        self.assertEqual([item.marker for item in receipt.path_lines], [legacy.LEGACY_PATH_MARK])

    def test_the_dry_run_names_everything_and_writes_nothing(self) -> None:
        harness = Harness(self)
        lt.dump(harness.layout.legacy_config / "custom.json",
                {"version": 1, "models": {}, "providers": {"mine": {
                    "base_url": "https://models.example.invalid/v1", "auth_kind": "bearer",
                    "secret_env": "CHARMER_MINE"}}})
        everything = lt.snapshot(harness.tree.home)
        lines, ready = lm.dry_run(harness.environ, harness.seams())
        self.assertTrue(ready, "\n".join(lines))
        self.assertEqual(lt.snapshot(harness.tree.home), everything)
        text = "\n".join(lines)
        home = str(harness.tree.home)
        self.assertNotIn(home, text)  # names only, HOME-relative
        for expected in (f"stop and disable {OLD_UNIT}", f"stop and disable {OLD_FAILURE}",
                         f"install, enable and start {lm.CHARMER_UNIT}",
                         f"~/.config/{legacy.LEGACY_DIR} -> ~/.config/charmer",
                         f"~/.local/state/{legacy.LEGACY_DIR} -> ~/.local/state/charmer",
                         f"~/.local/share/{legacy.LEGACY_DIR}/auth -> ~/.local/share/charmer/auth",
                         f"~/.local/share/{legacy.LEGACY_DIR}/proxy.log -> ~/.local/share/charmer/proxy.log",
                         "continuity.json: state_root", "secret-file.json: path (its key file moves)",
                         f"endpoint.json: unit {lm.CHARMER_UNIT}",
                         "installer.json: takes over the earlier installer's PATH line(s) in ~/.profile",
                         *(f"remove bin/{name}" for name in legacy.LEGACY_SHIMS),
                         *(f"~/.local/bin/{name}" for name in legacy.LEGACY_LAUNCHERS),
                         f"~/.local/share/{legacy.LEGACY_DIR}/install",
                         "charmer's empty ~/.local/state/charmer is removed first",
                         ".env exists; it moves as it is and is never read", "CHARMER_MINE",
                         "interrupted write(s) left temporary files", "need revalidation (line.review.json)"):
            self.assertIn(expected, text)
        for kept in lm.KEPT_DATA:
            self.assertNotIn(f"{legacy.LEGACY_DIR}/{kept} ->", text)
        # Secrets are named, never shown.
        self.assertNotIn(lt.SECRET.decode().strip(), text)

    def test_a_reserved_name_in_a_secret_reference_is_named(self) -> None:
        from charmer import operator

        harness = Harness(self)
        # What 1.1.0's `providers add reserved-test --kind anthropic-compatible
        # --base-url ... --auth header --header x-api-key --secret-ref
        # env:CHARMER_TEST_KEY --family local --declare-only` writes.
        document = {"version": operator.DATA_VERSION, "provider": {
            "display": "reserved-test", "kind": "anthropic-compatible",
            "base_url": "https://reserved-test.invalid/anthropic",
            "auth": {"kind": "header", "secret_ref": "env:CHARMER_TEST_KEY", "header": "x-api-key"},
            "independence_family": "local"}, "lines": {}}
        lt.write(harness.layout.legacy_gateway / operator.PROVIDERS_DIRNAME / "reserved-test.json",
                 operator.document_bytes(document).decode(), 0o600)
        lines, ready = lm.dry_run(harness.environ, harness.seams())
        self.assertTrue(ready, "\n".join(lines))
        text = "\n".join(lines)
        consequence = ("providers.d/reserved-test.json: its secret CHARMER_TEST_KEY uses charmer's reserved "
                       "CHARMER_ prefix: the provider is ignored for routing, and it blocks gateway configuration "
                       "changes until you rename its secret or remove it (charmer providers rm reserved-test)")
        self.assertIn(consequence, text)
        self.assertNotIn("env:CHARMER_TEST_KEY", text)
        harness.migrate()
        remnants = lm.remnants(harness.environ)
        self.assertIn((consequence, "store its key under a name outside CHARMER_ (charmer providers edit "
                                    "reserved-test), or remove it: charmer providers rm reserved-test"), remnants)

    def test_the_receipt_line_shows_only_with_path_lines_to_take(self) -> None:
        harness = Harness(self)
        profile = harness.tree.home / ".profile"
        kept = [line for line in profile.read_text().splitlines() if line != harness.tree.files["path-line"]]
        self.assertLess(len(kept), len(profile.read_text().splitlines()))
        profile.write_text("".join(f"{line}\n" for line in kept))
        lines, ready = lm.dry_run(harness.environ, harness.seams())
        self.assertTrue(ready, "\n".join(lines))
        self.assertNotIn("PATH line", "\n".join(lines))
        harness.migrate()
        receipt = install_receipt.read(harness.layout.install)
        self.assertEqual(receipt.path_lines, ())
        self.assertNotIn(lm.RECEIPT_SAVED, harness.journal().document["rewritten"])

    def test_the_command_prints_it_and_exits_by_readiness(self) -> None:
        import charmer.cli.commands.migration as command

        harness = Harness(self)
        out = io.StringIO()
        args = SimpleNamespace(legacy_dry_run=True, json_output=False)
        self.assertEqual(command._migrate_from_legacy(args, out, environ=harness.environ, seams=harness.seams()), 0)
        self.assertIn(f"Ready: {lm.COMMAND}", out.getvalue())
        harness.live.append("22222222-2222-4222-8222-222222222222")
        out = io.StringIO()
        self.assertEqual(command._migrate_from_legacy(args, out, environ=harness.environ, seams=harness.seams()), 1)
        self.assertIn("NO  no managed session runs: 1 managed session(s) run now: 22222222", out.getvalue())


class PreconditionTests(unittest.TestCase):
    """Every precondition refuses with its fix, and a refusal writes nothing."""

    def refused(self, harness: Harness, needle: str, remedy: str | None = None) -> lm.MigrationError:
        before = lt.snapshot(harness.tree.home)
        with self.assertRaises(lm.MigrationError) as raised:
            harness.migrate()
        self.assertIn(needle, str(raised.exception) + "\n".join(raised.exception.lines))
        self.assertIn("nothing was changed", str(raised.exception))
        if remedy is not None:
            self.assertIn(remedy, str(raised.exception.remedy) + "\n".join(raised.exception.lines))
        self.assertEqual(lt.snapshot(harness.tree.home), before)
        self.assertEqual(harness.systemd.verbs(), [])
        return raised.exception

    def test_not_an_installed_release(self) -> None:
        harness = Harness(self)
        seams = harness.seams()
        seams.installed = lambda _env: "this charmer does not run from an installed release bundle"
        with self.assertRaises(lm.MigrationError) as raised:
            lm.migrate(harness.environ, seams)
        self.assertIn("installed release bundle", str(raised.exception))
        self.assertIn("sh install.sh --no-setup", raised.exception.remedy)

    def test_no_earlier_installation(self) -> None:
        harness = Harness(self)
        for root in harness.layout.legacy_roots():
            shutil.rmtree(root)
        self.refused(harness, f"no installation of {legacy.NAME} was found")

    def test_a_nix_installation(self) -> None:
        harness = Harness(self)
        lt.write(harness.layout.legacy_state / "channel", "nix\n")
        self.refused(harness, "it is a Nix installation", "remove the Nix installation through your Nix setup")
        harness = Harness(self)
        (harness.layout.legacy_state / "channel").unlink()
        os.symlink("/nix/store/x", lt.mkdirs(harness.layout.legacy_data / "nix") / "current")
        self.refused(harness, "it is a Nix installation")

    def test_no_installed_bundle_to_return_to(self) -> None:
        harness = Harness(self)
        (harness.layout.legacy_install / "current").unlink()
        self.refused(harness, "holds no installed release")

    def test_a_state_format_this_release_cannot_read(self) -> None:
        harness = Harness(self)
        lt.write(harness.layout.legacy_state / "state-version", "9\n")
        self.refused(harness, "its state cannot be read by this release")

    def test_charmer_already_has_roots_of_its_own(self) -> None:
        harness = Harness(self)
        lt.dump(harness.layout.state / "sessions" / "x.json", {})
        self.refused(harness, "two roots are never merged", "move charmer's own directories aside")
        harness = Harness(self)
        lt.write(harness.layout.data / "auth" / "x.json", "{}\n")
        self.refused(harness, "two roots are never merged")

    def test_a_move_across_file_systems(self) -> None:
        harness = Harness(self)
        real = os.stat
        destination = harness.layout.state  # charmer's empty skeleton is there

        def stat(path, *args, **kwargs):
            info = real(path, *args, **kwargs)
            if Path(path) == destination:
                values = list(info)
                values[2] = info.st_dev + 1
                return os.stat_result(values)
            return info

        with mock.patch.object(lm.os, "stat", stat):
            self.refused(harness, "would cross file systems, and nothing is copied")

    def test_a_unit_file_the_earlier_release_did_not_write(self) -> None:
        harness = Harness(self)
        lt.write(harness.tree.unit(OLD_UNIT), "[Unit]\nDescription=mine\n", 0o644)
        self.refused(harness, f"was not written by {legacy.NAME}'s gateway service install")

    def test_a_gateway_service_under_another_name(self) -> None:
        harness = Harness(self)
        lt.dump(harness.layout.legacy_gateway / "endpoint.json",
                {"version": 1, "host": "127.0.0.1", "port": 8317, "backend": "systemd", "unit": "my-gateway"})
        self.refused(harness, "installed as my-gateway", "--name my-gateway")

    def test_a_foreign_charmer_unit(self) -> None:
        harness = Harness(self)
        lt.write(harness.tree.unit(lm.CHARMER_UNIT), "[Unit]\n", 0o644)
        self.refused(harness, "exists and was not written by charmer")

    def test_a_gateway_serving_another_state_root(self) -> None:
        harness = Harness(self)
        lt.dump(harness.layout.legacy_gateway / "continuity.json", {**lt.read_json(harness.layout.legacy_gateway / "continuity.json"), "state_root": "/elsewhere"})
        self.refused(harness, "names another state root (/elsewhere)")

    def test_an_unfinished_install_of_either_release(self) -> None:
        harness = Harness(self)
        lt.dump(harness.layout.install / ".switch.json", {})
        self.refused(harness, "an install or update of charmer did not finish", "sh install.sh --repair")
        harness = Harness(self)
        lt.dump(harness.layout.legacy_install / ".switch.json", {})
        self.refused(harness, f"an install or update of {legacy.NAME} did not finish",
                     "let the retained 1.1.0 launcher recover it first")

    def test_an_unfinished_gateway_transaction(self) -> None:
        for record in (gateway_inhibition.RECORD, gateway_inhibition.LEGACY_RECORD):
            with self.subTest(record=record):
                harness = Harness(self)
                lt.dump(harness.layout.legacy_state / "gateway" / record, {"version": 1})
                self.refused(harness, "an unfinished gateway transaction is recorded")

    def test_a_live_or_unknown_session(self) -> None:
        harness = Harness(self)
        harness.live.append("33333333-3333-4333-8333-333333333333")
        self.refused(harness, "1 managed session(s) run now: 33333333", "close every managed session")
        harness = Harness(self)
        harness.live.append("unknown")
        self.refused(harness, "whether a managed session runs cannot be told")

    def test_a_persistence_hold(self) -> None:
        harness = Harness(self)
        harness.holds.append("instance 0123: a credential save failed")
        error = self.refused(harness, "may hold an unsaved credential", f"{legacy.NAME} gateway clear-hold")
        self.assertIn("a credential save failed", str(error))

    def test_nested_configuration_directories(self) -> None:
        harness = Harness(self, xdg_config=lambda home: home / ".config" / legacy.LEGACY_DIR / "xdg")
        self.refused(harness, "lie one inside the other")


class MigrationTests(unittest.TestCase):
    def test_the_move_keeps_every_byte_but_the_named_values(self) -> None:
        harness = Harness(self)
        layout = harness.layout
        before = lt.snapshot(*layout.legacy_roots(), layout.legacy_data)
        raw = {name: (layout.legacy_gateway / name).read_bytes() for name in lm.AUTHORITY_FILES}
        units = {name: harness.tree.unit(name).read_bytes() for name in (OLD_UNIT, OLD_FAILURE)}
        earlier_receipt = install_receipt.receipt_path(layout.legacy_install).read_bytes()
        report = harness.migrate()
        self.assertEqual(report.lines, [lm.step_text(step, harness.journal()) for step in lm.STEPS])
        self.assertIn(lm.GATEWAY_TEXT[True], report.lines)
        self.assertEqual(harness.calls, ["shims", "gateway", "verify"])
        journal = harness.journal()
        self.assertEqual((journal.status, journal.root, journal.done), (lm.DONE, layout.state, list(lm.STEPS)))
        for root in layout.legacy_roots():
            self.assertFalse(os.path.lexists(root))
        rewritten = {str(layout.legacy_gateway / name) for name in (*lm.AUTHORITY_FILES, *Harness.RENDERED)}
        shims = {str(layout.legacy_state / "bin" / name) for name in legacy.LEGACY_SHIMS}
        for path, entry in before.items():
            old = Path(path)
            if path in shims or old.is_relative_to(layout.legacy_install):
                continue
            new = moved(layout, old)
            with self.subTest(path=path):
                found = lt._entry(new)
                if path in rewritten:
                    self.assertEqual(found[0], "file")
                else:
                    self.assertEqual(found, entry)
        # The install tree stays, with its receipt as it was.
        self.assertEqual(install_receipt.receipt_path(layout.legacy_install).read_bytes(), earlier_receipt)
        for path, entry in before.items():
            if Path(path).is_relative_to(layout.legacy_install):
                self.assertEqual(lt._entry(Path(path)), entry)
        # One value each, every other byte the same.
        new = {name: (layout.gateway / name).read_bytes() for name in lm.AUTHORITY_FILES}
        changes = {
            "continuity.json": (str(layout.legacy_state), str(layout.state)),
            "secret-file.json": (f"~/.config/{legacy.LEGACY_DIR}/key-file", "~/.config/charmer/key-file"),
            "endpoint.json": (OLD_UNIT, lm.CHARMER_UNIT),
        }
        for name, (old, value) in changes.items():
            with self.subTest(name=name):
                self.assertEqual(new[name].replace(json.dumps(value).encode(), json.dumps(old).encode()), raw[name])
                self.assertNotEqual(new[name], raw[name])
        self.assertEqual(lt.read_json(layout.gateway / "endpoint.json"),
                         {"version": 1, "host": "127.0.0.1", "port": 8317, "backend": "systemd",
                          "unit": lm.CHARMER_UNIT})
        # Units: the earlier ones stopped, disabled, saved and removed.
        self.assertEqual(harness.systemd.verbs()[:2], [("disable", f"{OLD_UNIT}.service"),
                                                       ("disable", f"{OLD_FAILURE}.service")])
        self.assertIn(("enable", f"{lm.CHARMER_UNIT}.service"), harness.systemd.verbs())
        for name, data in units.items():
            self.assertFalse(os.path.lexists(harness.tree.unit(name)))
            self.assertEqual(journal.saved("units", f"{name}.service").read_bytes(), data)
        self.assertEqual(journal.document["units"][OLD_UNIT]["mode"], 0o644)
        self.assertEqual(journal.document["shutdown"][OLD_UNIT]["result"], "success")
        self.assertEqual(journal.document["exec_stamp"]["instance"], "0123456789abcdef")
        # The earlier launchers removed (charmer's kept); shims replaced.
        for name in legacy.LEGACY_LAUNCHERS:
            self.assertFalse(os.path.lexists(harness.tree.launcher(name)))
        self.assertTrue(os.access(harness.tree.launcher("charmer"), os.X_OK))
        self.assertEqual(sorted(os.listdir(layout.state / "bin")), sorted(lm.CHARMER_SHIMS))
        # charmer's receipt takes over the earlier PATH line verbatim.
        receipt = install_receipt.read(layout.install)
        self.assertEqual([(item.file, item.marker, item.line) for item in receipt.path_lines],
                         [(str(harness.tree.home / ".profile"), legacy.LEGACY_PATH_MARK, harness.tree.files["path-line"])])
        # Historical evidence keeps its bytes; no inhibition is left behind.
        self.assertEqual(lt._entry(layout.state / "gateway" / "persistence-hold.json"),
                         before[str(layout.legacy_state / "gateway" / "persistence-hold.json")])
        self.assertIsNone(gateway_inhibition.read(layout.state))
        self.assertEqual(len(set(harness.tokens)), 1)
        self.assertEqual(lm.pending(harness.environ), None)
        # Rerunning a completed move changes nothing.
        after = harness.charmer_view()
        self.assertIn("already moved", harness.migrate().lines[0])
        self.assertEqual(harness.charmer_view(), after)

    def test_split_configuration_authorities_move_once_each(self) -> None:
        base = Path(tempfile.mkdtemp(prefix="charmer-legacy-"))
        self.addCleanup(shutil.rmtree, base, True)
        harness = Harness(self, xdg_config=base / "xdg")
        layout = harness.layout
        self.assertNotEqual(layout.legacy_config, layout.legacy_gateway)
        config = lt.snapshot(layout.legacy_config)
        harness.migrate()
        self.assertEqual(sorted(lt.snapshot(layout.config)), sorted(str(moved(layout, Path(path))) for path in config))
        self.assertTrue((layout.gateway / "continuity.json").is_file())
        self.assertTrue((layout.config / "custom.json").is_file())
        self.assertFalse((layout.config / "continuity.json").exists())
        self.assertEqual(layout.unit_dir, base / "xdg" / "systemd" / "user")
        self.assertFalse(os.path.lexists(layout.legacy_config))
        self.assertFalse(os.path.lexists(layout.legacy_gateway))

    def test_the_key_file_pointer_moves_only_with_its_file(self) -> None:
        for pointer, expected in (("inside", "~/.config/charmer/key-file"), ("absolute", None),
                                  ("outside", "unchanged"), ("install", "unchanged")):
            with self.subTest(pointer=pointer):
                harness = Harness(self, pointer=pointer)
                before = lt.read_json(harness.layout.legacy_gateway / "secret-file.json")["path"]
                harness.migrate()
                after = lt.read_json(harness.layout.gateway / "secret-file.json")["path"]
                if expected == "unchanged":
                    self.assertEqual(after, before)
                elif expected is None:
                    self.assertEqual(after, str(harness.layout.gateway / "key-file"))
                else:
                    self.assertEqual(after, expected)

    def test_an_on_demand_gateway_starts_no_service(self) -> None:
        harness = Harness(self, service=False)
        endpoint_bytes = (harness.layout.legacy_gateway / "endpoint.json").read_bytes()
        harness.migrate()
        self.assertEqual(harness.calls, ["shims", "verify"])
        self.assertEqual(harness.systemd.verbs(), [])
        self.assertEqual((harness.layout.gateway / "endpoint.json").read_bytes(), endpoint_bytes)
        self.assertFalse(harness.journal().document["service"])

    def test_a_failed_verification_keeps_the_journal_unfinished(self) -> None:
        harness = Harness(self)
        harness.verify_problems.append("BLOCKED: something")
        with self.assertRaises(lm.MigrationError) as raised:
            harness.migrate()
        self.assertIn("does not verify: BLOCKED: something", str(raised.exception))
        self.assertIsNotNone(lm.pending(harness.environ))
        self.assertIn("doctor: BLOCKED: something", "\n".join(lm.status_lines(harness.environ)))
        harness.verify_problems.clear()
        harness.migrate()
        self.assertEqual(harness.journal().status, lm.DONE)

    def test_a_hold_found_at_the_stop_refuses_before_it(self) -> None:
        harness = Harness(self)
        seams = harness.seams()
        calls = []

        def hold(_layout):
            calls.append(1)
            return [] if len(calls) == 1 else ["instance x: a credential save failed"]

        seams.hold = hold
        with self.assertRaises(lm.MigrationError) as raised:
            lm.migrate(harness.environ, seams)
        self.assertIn(f"{lm.BACK}; resolve the hold", raised.exception.remedy)
        self.assertEqual(harness.systemd.verbs(), [])
        self.assertTrue(harness.tree.unit(OLD_UNIT).is_file())
        harness.back()
        self.assertEqual(harness.journal().status, lm.REVERTED)

    def test_a_move_across_file_systems_found_at_the_rename(self) -> None:
        harness = Harness(self)
        harness.remember()
        real = os.rename

        def rename(source, target):
            if Path(source) == harness.layout.legacy_state:
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return real(source, target)

        with mock.patch.object(lm.os, "rename", rename):
            with self.assertRaises(lm.MigrationError) as raised:
                harness.migrate()
        self.assertIn("different file systems, and nothing is copied", str(raised.exception))
        self.assertEqual(raised.exception.remedy, f"undo the moves so far: {lm.BACK}")
        harness.back()
        self.assertEqual(harness.legacy_view(), harness._before)


class CrashTests(unittest.TestCase):
    """A kill after any step's effects (before its journal entry) converges
    under a rerun, and ``--back`` restores the earlier installation from it,
    its unit files byte for byte."""

    # Beside every step: a death right after the first journal write (before
    # the inhibition) and right after the inhibition began (before step 1).
    POINTS = ("journal-written", "inhibition-begun", *lm.STEPS, "first-move")

    def crash_at(self, harness: Harness, point: str) -> None:
        if point in ("journal-written", "inhibition-begun"):
            real_begin = lm._begin_inhibition

            def begin(root):
                if point == "journal-written":
                    self.assertEqual(lm.locate(harness.layout).status, lm.RUNNING)
                    self.assertIsNone(gateway_inhibition.read(root))
                    raise Crash(point)
                real_begin(root)
                raise Crash(point)

            patch = mock.patch.object(lm, "_begin_inhibition", begin)
        elif point == "first-move":
            real = os.rename
            count = []

            def rename(source, target):
                result = real(source, target)
                count.append(1)
                if len(count) == 1:
                    raise Crash(point)
                return result

            patch = mock.patch.object(lm.os, "rename", rename)
        else:
            real_mark = lm._mark

            def mark(journal, layout, seams, report, step):
                if step == point:
                    raise Crash(point)
                return real_mark(journal, layout, seams, report, step)

            patch = mock.patch.object(lm, "_mark", mark)
        with patch, self.assertRaises(Crash):
            harness.migrate()

    def test_a_rerun_converges_from_every_point(self) -> None:
        reference = Harness(self)
        reference.migrate()
        expected = self.normalized(reference)
        for point in self.POINTS:
            with self.subTest(point=point):
                harness = Harness(self)
                self.crash_at(harness, point)
                self.assertIsNotNone(lm.pending(harness.environ))
                harness.migrate()
                self.assertEqual(harness.journal().status, lm.DONE)
                self.assertEqual(self.normalized(harness), expected)
                self.assertIsNone(gateway_inhibition.read(harness.layout.state))

    def test_back_restores_the_earlier_installation_from_every_point(self) -> None:
        for point in (*self.POINTS, "done"):
            with self.subTest(point=point):
                harness = Harness(self)
                before = harness.remember()
                units = {name: harness.tree.unit(name).read_bytes() for name in (OLD_UNIT, OLD_FAILURE)}
                if point == "done":
                    harness.migrate()
                else:
                    self.crash_at(harness, point)
                report = harness.back()
                self.assertIn("moved the roots back", report.lines)
                self.assertEqual(harness.legacy_view(), before)
                for name, data in units.items():
                    self.assertEqual(harness.tree.unit(name).read_bytes(), data)
                    self.assertEqual(oct(os.stat(harness.tree.unit(name)).st_mode & 0o777), "0o644")
                self.assertEqual(harness.systemd.unit(OLD_UNIT), {"enabled": True, "runtime": False, "active": True})
                for name, _path in harness.layout.charmer_units():
                    self.assertFalse(os.path.lexists(harness.layout.unit_dir / f"{name}.service"))
                self.assertFalse(any(os.path.lexists(harness.layout.state / "bin" / name)
                                     for name in lm.CHARMER_SHIMS))
                self.assertEqual(harness.journal().status, lm.REVERTED)
                self.assertIsNone(lm.pending(harness.environ))
                self.assertIsNone(gateway_inhibition.read(harness.layout.legacy_state))
                # No session ran under charmer: the earlier launcher is not run.
                self.assertNotIn("repair", harness.calls)
                self.assertIsNone(harness.journal().document["repair"])
                # A reverted move can be run again from the start.
                harness.migrate()
                self.assertEqual(harness.journal().status, lm.DONE)

    def test_back_after_a_crash_in_back_converges(self) -> None:
        harness = Harness(self)
        before = harness.remember()
        harness.migrate()
        real = lm._undo_roots

        def undo_roots(journal):
            real(journal)
            raise Crash("back")

        with mock.patch.object(lm, "_undo_roots", undo_roots), self.assertRaises(Crash):
            harness.back()
        self.assertEqual(lm.pending(harness.environ).status, lm.REVERTING)
        with self.assertRaises(lm.MigrationError):
            harness.migrate()
        harness.back()
        self.assertEqual(harness.legacy_view(), before)

    def normalized(self, harness: Harness) -> dict:
        """The charmer view of ``harness`` with its HOME written as ``HOME``
        (in paths and in file contents), so two trees compare."""

        home = str(harness.tree.home)
        found = {}
        for path, (kind, content) in harness.charmer_view().items():
            if kind == "file":
                data = Path(path).read_bytes().replace(home.encode(), b"HOME")
                if Path(path).name == install_receipt.NAME:
                    # A launcher digest covers the launcher's HOME-bearing bytes.
                    document = json.loads(data)
                    for launcher in document["launchers"]:
                        launcher["sha256"] = "digest"
                    data = json.dumps(document, sort_keys=True).encode()
                content = (content[0], hashlib.sha256(data).hexdigest())
            elif kind == "link":
                content = content.replace(home, "HOME")
            found[path.replace(home, "HOME")] = (kind, content)
        return found


class BackHandsTheSessionsBackTests(unittest.TestCase):
    """After ``--back`` the earlier release finds every session as it left
    it: a record charmer wrote names the earlier selectors again, and the
    earlier launcher's own repair recompiles each scope charmer compiled
    (run once, last, with every guard of the run released)."""

    def test_a_session_resumed_under_charmer_is_handed_back(self) -> None:
        harness = Harness(self)
        harness.migrate()
        before = resumed_under_charmer(harness)
        report = harness.back()
        layout, mid = harness.layout, harness.tree.mid
        self.assertEqual(harness.calls.count("repair"), 1)
        self.assertEqual(harness.calls[-1], "repair")
        self.assertEqual(harness.repaired, [mid])
        self.assertEqual(lm._charmer_scopes(layout), [])
        path = layout.legacy_state / "sessions" / f"{mid}.json"
        # The record charmer wrote is the earlier one again, applied_hash and
        # launch fence included, written as the session store writes a
        # record; the scope's launch files are the ones the earlier compiler
        # wrote (the rest is the earlier repair's).
        self.assertEqual(strict_json.loads(path.read_bytes()), before["record"])
        self.assertEqual(path.read_bytes(), strict_json.canonical_file_bytes(before["record"]))
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
        live = layout.legacy_state / "scopes" / mid
        self.assertEqual(json.loads((live / scope.SETTINGS_RELPATH).read_bytes()),
                         json.loads(before[scope.SETTINGS_RELPATH]))
        self.assertEqual((live / scope.LEAD_SET_JSON).read_bytes(), before[scope.LEAD_SET_JSON])
        self.assertEqual(report.lines[-2:], [
            "restored the earlier gateway units, launchers and shims",
            f"{legacy.NAME} recompiled 1 session scope(s) charmer had compiled ({lm.REPAIR})"])
        self.assertIn("wrote 1 session(s) charmer resumed or started back in the earlier release's names "
                      "(records and launch settings)", report.lines)
        journal = harness.journal()
        self.assertEqual(journal.status, lm.REVERTED)
        self.assertEqual(journal.document["sessions_restored"], [mid])
        self.assertEqual(journal.document["repair"]["recompiled"], [mid])
        self.assertTrue(journal.document["repair"]["ok"])
        self.assertIsNone(journal.document["error"])
        self.assertIn(f"{lm.REPAIR}: recompiled 1 session scope(s)", lm.status_lines(harness.environ)[-1])
        self.assertIsNone(lm.pending(harness.environ))

    def test_a_scope_the_earlier_repair_left_fails_the_run_with_its_fix(self) -> None:
        harness = Harness(self)
        harness.migrate()
        resumed_under_charmer(harness)
        harness.repair_converges = False
        with self.assertRaises(lm.MigrationError) as raised:
            harness.back()
        mid = harness.tree.mid
        self.assertIn(f"1 session scope(s) still carry a charmer name after {lm.REPAIR} ({mid[:8]})",
                      str(raised.exception))
        self.assertEqual(raised.exception.remedy, f"{legacy.NAME} doctor --repair {mid}; if {legacy.NAME} doctor "
                                                  f"still names it, resume it once: {legacy.NAME} -r {mid}")
        self.assertIn("Repair-all: 1 session(s) converged to record authority.", raised.exception.lines)
        # The roots are back and the journal says so; nothing refuses.
        journal = harness.journal()
        self.assertEqual((journal.status, journal.root), (lm.REVERTED, harness.layout.legacy_state))
        self.assertEqual(journal.document["repair"]["left"], [mid])
        self.assertIn("still carry a charmer name", journal.document["error"])
        self.assertIn(f"  {lm.REPAIR}: left 1 session scope(s) with a charmer name", lm.status_lines(harness.environ))
        self.assertIsNone(lm.pending(harness.environ))

    def test_a_failed_earlier_repair_fails_the_run_with_its_lines(self) -> None:
        harness = Harness(self)
        harness.migrate()
        resumed_under_charmer(harness)
        harness.repair_result = (False, ["Repair-all: 0 session(s) converged to record authority.",
                                         "  - FAILED 11111111-1111-4111-8111-111111111111: unreadable",
                                         "Re-run `doctor` to confirm"])
        with self.assertRaises(lm.MigrationError) as raised:
            harness.back()
        self.assertIn(f"{lm.REPAIR} reported a failure", str(raised.exception))
        self.assertEqual(raised.exception.remedy, f"fix what it names, then: {lm.REPAIR}")
        self.assertEqual(list(raised.exception.lines[1:]), [
            "Repair-all: 0 session(s) converged to record authority.",
            "- FAILED 11111111-1111-4111-8111-111111111111: unreadable"])
        self.assertEqual(harness.journal().status, lm.REVERTED)

    def test_a_kill_in_the_earlier_repair_converges_under_a_rerun(self) -> None:
        harness = Harness(self)
        harness.migrate()
        before = resumed_under_charmer(harness)
        harness.repair_crash = True
        with self.assertRaises(Crash):
            harness.back()
        self.assertEqual(lm.pending(harness.environ).status, lm.REVERTING)
        with self.assertRaises(lm.MigrationError):
            harness.migrate()
        harness.repair_crash = False
        harness.back()
        self.assertEqual(harness.calls.count("repair"), 2)
        self.assertEqual(harness.journal().status, lm.REVERTED)
        self.assertEqual(harness.journal().document["sessions_restored"], [harness.tree.mid])
        path = harness.layout.legacy_state / "sessions" / f"{harness.tree.mid}.json"
        self.assertEqual(strict_json.loads(path.read_bytes()), before["record"])
        self.assertEqual(lm._charmer_scopes(harness.layout), [])

    def test_a_kill_between_the_record_and_its_launch_files_converges(self) -> None:
        harness = Harness(self)
        harness.migrate()
        before = resumed_under_charmer(harness)
        real = lm.state.atomic_write

        def write(path, data):
            if Path(path).name == scope.SETTINGS_RELPATH:
                raise Crash("launch files")
            return real(path, data)

        with mock.patch.object(lm.state, "atomic_write", write), self.assertRaises(Crash):
            harness.back()
        layout, mid = harness.layout, harness.tree.mid
        # The record names the fence of the launch files still to come.
        record = strict_json.loads((layout.legacy_state / "sessions" / f"{mid}.json").read_bytes())
        self.assertEqual(record, before["record"])
        self.assertEqual(lm.pending(harness.environ).status, lm.REVERTING)
        harness.back()
        live = layout.legacy_state / "scopes" / mid
        self.assertEqual(json.loads((live / scope.SETTINGS_RELPATH).read_bytes()),
                         json.loads(before[scope.SETTINGS_RELPATH]))
        self.assertEqual((live / scope.LEAD_SET_JSON).read_bytes(), before[scope.LEAD_SET_JSON])
        self.assertEqual(harness.journal().status, lm.REVERTED)

    def test_a_record_that_names_no_charmer_selector_keeps_its_bytes(self) -> None:
        harness = Harness(self)
        mid = harness.tree.mid
        path = harness.layout.legacy_state / "sessions" / f"{mid}.json"
        raw = path.read_bytes()
        harness.migrate()
        self.assertEqual(lm._restore_sessions(harness.layout), [])
        harness.back()
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(harness.journal().document["sessions_restored"], [])

    def test_the_earlier_names_are_the_renamed_map_backwards(self) -> None:
        renamed = renamed_map()
        self.assertEqual(len(renamed), 31)
        for old, spec in renamed.items():
            with self.subTest(old=old):
                self.assertEqual(lm.earlier_selector(renamed, spec["selector"]), old)
                self.assertEqual(catalog.renamed_selector(renamed, old), spec["selector"])
                base = spec["selector"].removesuffix("[1m]")
                self.assertEqual(lm.earlier_selector(renamed, f"{base}[1m]"),
                                 f"{old.removesuffix('[1m]')}[1m]")
        retired = f"{legacy.LEGACY_SELECTOR_PREFIX}qwen38-max[1m]"
        for kept in ("charmer-render-probe", "gpt-multi-gpt55-high", "custom-131072", "claude-opus-5", retired, 7):
            self.assertEqual(lm.earlier_selector(renamed, kept), kept)

    def test_the_command_says_done_or_names_the_fix(self) -> None:
        import charmer.cli.commands.migration as command

        args = SimpleNamespace(legacy_back=True, json_output=False)
        for converges in (True, False):
            with self.subTest(converges=converges):
                harness = Harness(self)
                harness.migrate()
                resumed_under_charmer(harness)
                harness.repair_converges = converges
                out, err = io.StringIO(), io.StringIO()
                with mock.patch("charmer.cli.consent.require_human"):
                    code = command._migrate_from_legacy(args, out, error_stream=err, environ=harness.environ,
                                                        seams=harness.seams())
                if converges:
                    self.assertEqual(code, 0, err.getvalue())
                    self.assertIn("Done. The earlier installation works as before", out.getvalue())
                    self.assertIn(f"({lm.REPAIR})", out.getvalue())
                else:
                    self.assertEqual(code, 1)
                    self.assertNotIn("Done.", out.getvalue())
                    self.assertIn(f"fix: {legacy.NAME} doctor --repair {harness.tree.mid}", err.getvalue())

    def test_the_live_repair_runs_the_retained_launcher_once(self) -> None:
        import charmer.cli.commands.migration as command

        harness = Harness(self)
        launcher = harness.layout.legacy_install / lm.EARLIER_LAUNCHER
        environ = {**harness.environ, gateway_inhibition.TOKEN_ENV: "token-of-this-run"}
        ok, lines = command._repair_earlier(environ)
        self.assertFalse(ok)
        self.assertEqual(lines, [f"~/.local/share/{legacy.LEGACY_DIR}/install/{lm.EARLIER_LAUNCHER} is missing or "
                                 "not executable"])
        seen = harness.base / "seen"
        for code in (0, 1):
            with self.subTest(code=code):
                lt.write(launcher, "#!/bin/sh\n"
                                   f"printf '%s|%s\\n' \"$*\" \"${{{gateway_inhibition.TOKEN_ENV}-unset}}\" > '{seen}'\n"
                                   "echo 'Repair-all: 0 session(s) converged to record authority.'\n"
                                   f"exit {code}\n", 0o755)
                ok, lines = command._repair_earlier(environ)
                self.assertEqual(ok, code == 0)
                self.assertEqual(lines, ["Repair-all: 0 session(s) converged to record authority."])
                self.assertEqual(seen.read_text(), f"{' '.join(lm.REPAIR_ARGS)}|unset\n")


@contextlib.contextmanager
def held_elsewhere(path: Path, *, shared: bool = False):
    """A ``flock`` on ``path`` held by another process, so no lock helper of
    this process can mask the contention."""

    code = ("import fcntl, os, sys\n"
            "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
            "fcntl.flock(fd, fcntl.LOCK_SH if sys.argv[2] == 'shared' else fcntl.LOCK_EX)\n"
            "print('held', flush=True)\n"
            "sys.stdin.read()\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen([sys.executable, "-c", code, str(path), "shared" if shared else "exclusive"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout is not None and process.stdout.readline().strip() == "held"
        yield
    finally:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.close()
        process.wait(timeout=30)
        process.stdout.close()


@contextlib.contextmanager
def install_lock_held(root: Path):
    """An install lock (``<root>/.lock``) another live process holds."""

    lock = root / ".lock"
    lock.mkdir(mode=0o700)
    (lock / "pid").write_text(f"{os.getppid()}\n")
    try:
        yield
    finally:
        shutil.rmtree(lock, ignore_errors=True)


class TransactionLockTests(unittest.TestCase):
    """migrate, --back and --purge share one transaction lock: each reads
    the journal under it and holds it through its last write."""

    def test_a_back_between_the_purge_question_and_the_purge(self) -> None:
        import charmer.cli.commands.migration as command

        harness = Harness(self)
        harness.migrate()
        layout = harness.layout
        auth = moved(layout, layout.legacy_data / "auth")

        def answer(*_args, **_kwargs):
            # The person takes their time; --back finishes meanwhile.
            harness.back()
            return True

        args = SimpleNamespace(legacy_purge=True, json_output=False)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("charmer.cli.consent.require_human"), mock.patch("charmer.cli.consent.confirm", answer):
            code = command._migrate_from_legacy(args, out, error_stream=err, environ=harness.environ,
                                                seams=harness.seams())
        self.assertEqual(code, 1)
        self.assertIn("no completed migration was found; nothing was removed", err.getvalue())
        # The restored earlier installation is whole, and no stale journal
        # stands under charmer beside the reverted one.
        self.assertTrue((layout.legacy_data / "auth").is_dir())
        self.assertFalse(os.path.lexists(auth))
        self.assertTrue(layout.legacy_install.is_dir())
        self.assertIsNone(lm.read_journal(layout.state))
        self.assertEqual(harness.journal().status, lm.REVERTED)
        self.assertIsNone(harness.journal().document["purged"])

    def test_a_held_transaction_lock_refuses_every_one_of_them(self) -> None:
        harness = Harness(self)
        layout = harness.layout
        for root in (layout.install, layout.legacy_install):
            with self.subTest(root=root), install_lock_held(root):
                with self.assertRaises(lm.MigrationError) as raised:
                    harness.migrate()
                self.assertIn("another install or update is running", str(raised.exception))
                self.assertIsNone(lm.locate(layout))
        harness.migrate()
        for action in (harness.back, lambda: lm.purge(harness.environ, harness.seams())):
            with self.subTest(action=action), install_lock_held(layout.install):
                with self.assertRaises(lm.MigrationError):
                    action()
                self.assertEqual(harness.journal().status, lm.DONE)
                self.assertTrue(layout.legacy_install.is_dir())


class GuardContentionTests(unittest.TestCase):
    """Every guard is real: held by another process it refuses before the
    service stops or a root moves (the guards of the roots where they are)
    and, after the move, before a value is rewritten (charmer's own guards);
    released, the same command finishes."""

    def guards(self, layout: lm.Layout, state_root: Path, gateway_dir: Path, data_root: Path):
        return {
            "migration lock": lambda: held_elsewhere(sessions.migration_lock(state_root).lock_path),
            "served-change barrier": lambda: held_elsewhere(
                state.FileLock(state.served_barrier_target(gateway_dir)).lock_path),
            "a Claude Code copy in use": lambda: held_elsewhere(data_root / "claude" / lt.CLIENT / pin.IN_USE,
                                                                shared=True),
        }

    def test_the_guards_of_the_roots_where_they_are(self) -> None:
        for name in ("migration lock", "served-change barrier", "a Claude Code copy in use"):
            with self.subTest(guard=name):
                harness = Harness(self)
                layout = harness.layout
                before = lt.snapshot(*layout.legacy_roots())
                hold = self.guards(layout, layout.legacy_state, layout.legacy_gateway, layout.legacy_data)[name]
                with mock.patch.object(lm, "LOCK_WAIT", 0.2), hold():
                    with self.assertRaises(lm.MigrationError):
                        harness.migrate()
                self.assertEqual(harness.systemd.verbs(), [])
                self.assertTrue(layout.legacy_state.is_dir())
                after = lt.snapshot(*layout.legacy_roots(), skip=Harness._guard_file)
                self.assertEqual(after, {path: entry for path, entry in before.items()
                                         if path in after or not Harness._guard_file(Path(path))})
                harness.migrate()
                self.assertEqual(harness.journal().status, lm.DONE)

    def test_charmers_own_guards_after_the_move(self) -> None:
        for name in ("migration lock", "served-change barrier", "a Claude Code copy in use"):
            with self.subTest(guard=name):
                harness = Harness(self)
                layout = harness.layout
                continuity_before = (layout.legacy_gateway / "continuity.json").read_bytes()
                real = lm._handover
                holder = contextlib.ExitStack()
                self.addCleanup(holder.close)

                def handover(root, token):
                    fresh = real(root, token)
                    holder.enter_context(self.guards(layout, layout.state, layout.gateway, layout.data)[name]())
                    return fresh

                with mock.patch.object(lm, "LOCK_WAIT", 0.2), mock.patch.object(lm, "_handover", handover):
                    with self.assertRaises(lm.MigrationError):
                        harness.migrate()
                journal = harness.journal()
                self.assertEqual(journal.done, [lm.UNITS, lm.ROOTS])
                # Nothing was rewritten under a held guard.
                self.assertEqual((layout.gateway / "continuity.json").read_bytes(), continuity_before)
                holder.close()
                harness.migrate()
                self.assertEqual(harness.journal().status, lm.DONE)


class OAuthRefreshHoldTests(unittest.TestCase):
    """Migration and --back evaluate account refreshes under the gateway's
    pool names, without widening the credential-save parser's allowlist."""

    def journal_hold(self, *, back: bool, providers: tuple[str, ...] = ("claude", "codex"),
                     result: str = "persisted", journald: str = "before") -> tuple[list[str], set[str]]:
        import datetime

        import charmer.cli.commands.migration as command
        from charmer import service
        from charmer.platform import observation
        from test_gateway_lifecycle import save_line

        harness = Harness(self)
        if back:
            harness.migrate()
        layout = harness.layout
        root = layout.state if back else layout.legacy_state
        unit = lm.CHARMER_UNIT if back else OLD_UNIT
        marker = service.start_marker_prefix if back else legacy.legacy_start_marker
        started = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0) - datetime.timedelta(minutes=1)
        instance = "0123456789abcdef"
        service.write_exec_stamp(service.gateway_workdir(root), service.ExecStamp(
            launcher_version="2.0.0" if back else "1.1.0", signature="test", pid=4242,
            binary="/nonexistent/cli-proxy-api", started_at=started.isoformat(), instance=instance))
        records = [observation.LogRecord(marker(instance) + " (pid 4242)", started, "boot:invocation", "c0")]
        for index, provider in enumerate(providers, 1):
            at = started + datetime.timedelta(seconds=index)
            message = save_line(result, at=at.strftime("%Y-%m-%d %H:%M:%S")).replace(
                "provider=claude", f"provider={provider}")
            records.append(observation.LogRecord(message, at, "boot:invocation", f"c{index}"))
        hold = gateway_hold.hold_path(root)
        recorded = hold.read_bytes()
        journald_at = {"before": started - datetime.timedelta(days=1), "equal": started,
                       "restarted": started + datetime.timedelta(seconds=30), "unavailable": None}[journald]
        with mock.patch.object(assets, "default_asset_root", return_value=FIXTURE_ROOT), \
                mock.patch("charmer.platform.linux_service.unit_properties",
                           return_value={"ActiveState": "active"}) as properties, \
                mock.patch.object(service, "read_hold_journal",
                                  return_value=observation.LogWindow(tuple(records), "bounded")) as reader, \
                mock.patch.object(service, "journal_reaches", return_value=None) as reaches, \
                mock.patch.object(service, "journal_drops", return_value=()) as drops, \
                mock.patch.object(service, "journald_start", return_value=journald_at) as journald_start, \
                mock.patch.object(gateway_hold, "evaluate_journal", wraps=gateway_hold.evaluate_journal) as evaluate:
            reasons = command._charmer_hold(layout) if back else command._legacy_hold(layout)
        properties.assert_called_once_with(unit, subprocess.run)
        reader.assert_called_once()
        asked = dict(reader.call_args.kwargs)
        since = int(asked.pop("since").removeprefix("@"))
        self.assertEqual(asked, {"unit": unit, "evidence": gateway_events.SAVE_LINE_PATTERN,
                                 "marker": marker(instance), "max_bytes": gateway_hold.JOURNAL_BUDGET})
        # The earlier of 24 hours ago and the instance's start (a minute ago).
        day_ago = (datetime.datetime.now(datetime.timezone.utc) - gateway_hold.JOURNAL_WINDOW).timestamp()
        self.assertLessEqual(abs(since - day_ago), 60)
        reaches.assert_not_called()  # the start line is in the read
        drops.assert_called_once()
        journald_start.assert_called_once_with()
        self.assertIs(evaluate.call_args.kwargs["journald_start"], journald_start)
        self.assertEqual(evaluate.call_args.kwargs["running"], (instance, started))
        self.assertEqual(hold.read_bytes(), recorded)  # the precondition records nothing
        return reasons, set(evaluate.call_args.args[1])

    def assert_account_refreshes_clear(self, *, back: bool) -> None:
        reasons, providers = self.journal_hold(back=back)
        self.assertEqual(reasons, [])
        self.assertEqual(providers, {*catalog.load_catalog(FIXTURE_ROOT).providers, "claude", "codex"})

    def test_legacy_journal_accepts_persisted_account_refreshes(self) -> None:
        self.assert_account_refreshes_clear(back=False)

    def test_back_journal_accepts_persisted_account_refreshes(self) -> None:
        self.assert_account_refreshes_clear(back=True)

    def test_journald_restart_risk_vetoes_both_migration_directions(self) -> None:
        for back in (False, True):
            with self.subTest(back=back):
                reasons, _providers = self.journal_hold(back=back, journald="restarted")
                self.assertEqual(reasons, ["service journal: journald restarted; "
                                           "credential-save evidence may be missing"])

    def test_equal_journald_start_does_not_veto_either_migration_direction(self) -> None:
        for back in (False, True):
            with self.subTest(back=back):
                reasons, _providers = self.journal_hold(back=back, journald="equal")
                self.assertEqual(reasons, [])

    def test_unavailable_journald_reports_missing_coverage_in_both_directions(self) -> None:
        for back in (False, True):
            with self.subTest(back=back):
                reasons, _providers = self.journal_hold(back=back, journald="unavailable")
                self.assertEqual(reasons, ["service journal: journald's current process start could not be observed; "
                                           "credential-save evidence may be missing"])

    def test_failed_saves_still_hold_alongside_journald_risk_in_both_directions(self) -> None:
        for back in (False, True):
            for journald in ("restarted", "unavailable"):
                with self.subTest(back=back, journald=journald):
                    reasons, _providers = self.journal_hold(back=back, result="failed", journald=journald)
                    self.assertEqual(len(reasons), 2)
                    self.assertTrue(any("a credential save failed" in reason for reason in reasons))

    def test_supervised_absent_instance_does_not_query_journald(self) -> None:
        import charmer.cli.commands.migration as command
        from charmer import service
        from charmer.platform import observation

        for back in (False, True):
            with self.subTest(back=back):
                harness = Harness(self)
                if back:
                    harness.migrate()
                with mock.patch.object(assets, "default_asset_root", return_value=FIXTURE_ROOT), \
                        mock.patch("charmer.platform.linux_service.unit_properties",
                                   return_value={"ActiveState": "inactive"}), \
                        mock.patch("charmer.platform.posix_fs.lock_held", return_value=False), \
                        mock.patch.object(service, "read_hold_journal", return_value=observation.LogWindow((), "bounded")), \
                        mock.patch.object(service, "journal_reaches", side_effect=AssertionError("reach probe")), \
                        mock.patch.object(service, "journal_drops", return_value=()), \
                        mock.patch.object(service, "journald_start", side_effect=AssertionError("process probe")) as probe:
                    reasons = command._charmer_hold(harness.layout) if back else command._legacy_hold(harness.layout)
                self.assertEqual(reasons, [])
                probe.assert_not_called()

    def test_unknown_journal_provider_still_holds_with_incomplete_coverage(self) -> None:
        for back in (False, True):
            with self.subTest(back=back):
                reasons, _providers = self.journal_hold(back=back, providers=("claude", "codex", "unknown-provider"))
                self.assertEqual(reasons, ["service journal: the log could not be evaluated completely "
                                           "(coverage incomplete); a credential-save line did not parse "
                                           "(an unknown provider name or a damaged line)"])

    def test_failed_account_refresh_still_holds(self) -> None:
        for back in (False, True):
            with self.subTest(back=back):
                reasons, _providers = self.journal_hold(back=back, result="failed")
                self.assertEqual(len(reasons), 1)
                self.assertIn("a credential save failed", reasons[0])
                self.assertNotIn("coverage", reasons[0])

    def test_a_week_long_earlier_journal_read_through_journalctl_holds_nothing(self) -> None:
        # Revert-failing: b4d8004f read the newest JOURNAL_BUDGET bytes of the
        # earlier unit's journal, found its start line cut off and held.
        import datetime

        import _fake_journalctl as fake
        import charmer.cli.commands.migration as command
        from charmer import service
        from test_gateway_lifecycle import week_long_entries

        harness = Harness(self)
        layout = harness.layout
        root = layout.legacy_state
        instance = "0123456789abcdef"
        started = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0) - datetime.timedelta(days=7)
        service.write_exec_stamp(service.gateway_workdir(root), service.ExecStamp(
            launcher_version="1.1.0", signature="test", pid=4242, binary="/nonexistent/cli-proxy-api",
            started_at=started.isoformat(), instance=instance))
        bindir = Path(tempfile.mkdtemp(prefix="cm-fake-journalctl-"))
        self.addCleanup(shutil.rmtree, bindir, True)
        log = fake.install(bindir, week_long_entries(started, instance, unit=f"{OLD_UNIT}.service",
                                                     marker=legacy.legacy_start_marker),
                           [fake.system_entry(int(started.timestamp() * 1_000_000))])
        hold = gateway_hold.hold_path(root)
        recorded = hold.read_bytes()
        with mock.patch.dict(os.environ, {"PATH": str(bindir)}), \
                mock.patch.object(assets, "default_asset_root", return_value=FIXTURE_ROOT), \
                mock.patch("charmer.platform.linux_service.unit_properties", return_value={"ActiveState": "active"}), \
                mock.patch.object(service, "journald_start", return_value=started - datetime.timedelta(days=1)), \
                mock.patch.object(gateway_hold, "JOURNAL_BUDGET", 256 << 10):
            reasons = command._legacy_hold(layout)
        self.assertEqual(reasons, [])
        self.assertEqual(hold.read_bytes(), recorded)  # the precondition records nothing
        calls = fake.calls(log)
        self.assertTrue(calls, "the fake journalctl is the one that answered")
        self.assertEqual(calls[0][calls[0].index("-u") + 1], OLD_UNIT)
        self.assertIn(f"--grep={gateway_events.SAVE_LINE_PATTERN}|^{legacy.legacy_start_marker(instance)}", calls[0])

    def test_on_demand_back_accepts_persisted_account_refreshes(self) -> None:
        import charmer.cli.commands.migration as command
        from charmer import service
        from charmer.platform import file_log
        from test_gateway_lifecycle import NOW, save_line

        harness = Harness(self, service=False)
        harness.migrate()
        layout = harness.layout
        descriptor, _log = file_log.create_instance_log(
            service.gateway_workdir(layout.state) / service.GATEWAY_LOGS, NOW, "fedcba9876543210")
        lines = [save_line("persisted").replace("provider=claude", f"provider={provider}")
                 for provider in ("claude", "codex")]
        os.write(descriptor, ("\n".join(lines) + "\n").encode())
        os.close(descriptor)
        hold = gateway_hold.hold_path(layout.state)
        recorded = hold.read_bytes()
        with mock.patch.object(assets, "default_asset_root", return_value=FIXTURE_ROOT), \
                mock.patch("charmer.platform.linux_service.unit_properties") as properties, \
                mock.patch.object(service, "journald_start", side_effect=AssertionError("process probe")) as probe:
            self.assertEqual(command._charmer_hold(layout), [])
        properties.assert_not_called()  # on demand: the instance logs, never the service manager
        probe.assert_not_called()
        self.assertEqual(hold.read_bytes(), recorded)


class BackGatewayTests(unittest.TestCase):
    """--back stops charmer's gateway through its own stop, after its own
    persistence hold, and proves it stopped before a root moves."""

    def assert_nothing_moved_back(self, harness: Harness) -> None:
        for root in harness.layout.legacy_roots():
            self.assertFalse(os.path.lexists(root))
        self.assertTrue(harness.layout.state.is_dir())

    def test_charmers_hold_refuses_before_anything_stops(self) -> None:
        harness = Harness(self)
        harness.migrate()
        hold = gateway_hold.hold_path(harness.layout.state)
        recorded = hold.read_bytes()
        verbs = list(harness.systemd.verbs())
        harness.charmer_holds.append("instance 0123456789abcdef: a credential save failed")
        with self.assertRaises(lm.MigrationError) as raised:
            harness.back()
        self.assertIn("charmer's gateway may hold an unsaved credential", str(raised.exception))
        self.assertIn("charmer gateway clear-hold", raised.exception.remedy)
        self.assertEqual(harness.journal().status, lm.DONE)  # charmer's commands still resolve it
        self.assertEqual(harness.hold_seen, [lm.DONE])
        self.assertEqual(harness.systemd.verbs(), verbs)
        self.assertNotIn("stop", harness.calls)
        self.assertEqual(hold.read_bytes(), recorded)
        self.assert_nothing_moved_back(harness)
        harness.charmer_holds.clear()
        harness.back()
        self.assertEqual(harness.journal().status, lm.REVERTED)

    def test_an_unfinished_move_lets_the_hold_commands_run(self) -> None:
        from charmer.cli import entry

        harness = Harness(self)
        harness.verify_problems.append("BLOCKED: x")
        with self.assertRaises(lm.MigrationError):
            harness.migrate()
        harness.verify_problems.clear()
        harness.charmer_holds.append("instance 0123456789abcdef: a credential save failed")
        with self.assertRaises(lm.MigrationError) as raised:
            harness.back()
        self.assertIn("charmer gateway clear-hold", raised.exception.remedy)
        journal = harness.journal()
        self.assertEqual(journal.status, lm.RUNNING)
        for verb in ("status", "logs", "clear-hold", "stop", "start"):
            self.assertEqual(lm.runs_while_pending(journal, "gateway", verb), verb in lm.HOLD_COMMANDS)
        self.assertFalse(lm.runs_while_pending(journal, "doctor", None))
        early = lm.Journal(journal.root, {**journal.document, "done": [lm.UNITS, lm.ROOTS]})
        self.assertFalse(lm.runs_while_pending(early, "gateway", "status"))
        reverting = lm.Journal(journal.root, {**journal.document, "status": lm.REVERTING, lm.UNWOUND: True})
        self.assertFalse(lm.runs_while_pending(reverting, "gateway", "status"))
        ran = []
        for argv, expected in ((["gateway", "status"], 0), (["gateway", "stop"], 1)):
            with self.subTest(argv=argv):
                err = io.StringIO()
                with mock.patch.dict(os.environ, harness.environ, clear=True), mock.patch("sys.stderr", err), \
                        mock.patch("charmer.cli.commands.gateway.command",
                                   side_effect=lambda _runtime, args, **_kw: ran.append(args.gateway_command) or 0):
                    self.assertEqual(entry.main(argv, output_stream=io.StringIO()), expected)
                if expected:
                    self.assertIn("the move from the earlier installation did not finish", err.getvalue())
        self.assertEqual(ran, ["status"])
        harness.charmer_holds.clear()
        harness.back()
        self.assertEqual(harness.journal().status, lm.REVERTED)

    def test_a_hold_found_only_at_the_checked_stop_stays_resolvable(self) -> None:
        from charmer.cli import entry

        harness = Harness(self)
        harness.migrate()
        harness.stop_ok = False
        harness.stop_refusal = ("refused while the persistence hold is active (instance 0123456789abcdef: a "
                                "credential save failed)")
        with self.assertRaises(lm.MigrationError) as raised:
            harness.back()
        self.assertEqual(harness.hold_seen, [lm.DONE])  # the first check found nothing
        self.assertIn("persistence hold", str(raised.exception))
        self.assertIn("charmer gateway clear-hold", raised.exception.remedy)
        journal = harness.journal()
        self.assertEqual(journal.status, lm.REVERTING)
        self.assertNotIn(lm.UNWOUND, journal.document)
        self.assert_nothing_moved_back(harness)
        self.assertTrue(os.path.lexists(harness.layout.unit_dir / f"{lm.CHARMER_UNIT}.service"))
        for verb in lm.HOLD_COMMANDS:
            self.assertTrue(lm.runs_while_pending(journal, "gateway", verb), verb)
        ran = []
        for argv, expected in ((["gateway", "clear-hold"], 0), (["gateway", "stop"], 1)):
            with self.subTest(argv=argv):
                err = io.StringIO()
                with mock.patch.dict(os.environ, harness.environ, clear=True), mock.patch("sys.stderr", err), \
                        mock.patch("charmer.cli.commands.gateway.command",
                                   side_effect=lambda _runtime, args, **_kw: ran.append(args.gateway_command) or 0):
                    self.assertEqual(entry.main(argv, output_stream=io.StringIO()), expected)
                if expected:
                    self.assertIn("moving the earlier installation back did not finish", err.getvalue())
        self.assertEqual(ran, ["clear-hold"])
        harness.stop_ok = True
        harness.back()
        self.assertEqual(harness.journal().status, lm.REVERTED)

    def test_once_back_changed_anything_the_hold_commands_refuse(self) -> None:
        harness = Harness(self)
        harness.migrate()
        with mock.patch.object(lm, "_undo_roots", side_effect=OSError(errno.EIO, "I/O error")), \
                self.assertRaises(lm.MigrationError):
            harness.back()
        journal = harness.journal()
        self.assertEqual(journal.status, lm.REVERTING)
        self.assertIs(journal.document.get(lm.UNWOUND), True)
        for verb in lm.HOLD_COMMANDS:
            self.assertFalse(lm.runs_while_pending(journal, "gateway", verb), verb)
        harness.back()
        self.assertEqual(harness.journal().status, lm.REVERTED)

    def test_the_live_hold_reads_charmers_own_evidence(self) -> None:
        import charmer.cli.commands.migration as command
        from charmer import service
        from charmer.platform import file_log
        from test_gateway_lifecycle import NOW, save_line

        cases = {}
        harness = Harness(self, service=False)
        harness.migrate()
        layout = harness.layout
        self.assertEqual(command._charmer_hold(layout), [])
        # A recorded, unresolved hold.
        document = {"version": gateway_hold.HOLD_VERSION, "cleared": {}, "cleared_through": None,
                    "holds": [{"instance": "0123456789abcdef", "reason": "a refreshed credential was not saved",
                               "recorded_at": "2026-10-17T12:00:00+00:00"}]}
        hold = gateway_hold.hold_path(layout.state)
        original = hold.read_bytes()
        lt.dump(hold, document)
        cases["recorded"] = command._charmer_hold(layout)
        state.atomic_write(hold, original)
        # A failed save in the current instance's log, never recorded.
        descriptor, log = file_log.create_instance_log(service.gateway_workdir(layout.state) / service.GATEWAY_LOGS,
                                                        NOW, "fedcba9876543210")
        os.write(descriptor, (save_line("failed") + "\n").encode())
        os.close(descriptor)
        cases["unrecorded"] = command._charmer_hold(layout)
        self.assertEqual(hold.read_bytes(), original)
        os.unlink(log)
        # Evidence that cannot be read.
        hold.write_bytes(b"{")
        cases["unavailable"] = command._charmer_hold(layout)
        for name, reasons in cases.items():
            with self.subTest(case=name):
                self.assertTrue(reasons)
        self.assertIn("a refreshed credential was not saved", cases["recorded"][0])
        self.assertIn("fedcba9876543210", cases["unrecorded"][0])
        self.assertIn("a credential save failed", cases["unrecorded"][0])
        self.assertNotIn("coverage", cases["unrecorded"][0])
        self.assertIn("not valid JSON", cases["unavailable"][0])

    def test_the_preflight_reads_the_running_instances_window_or_holds(self) -> None:
        import datetime

        import charmer.cli.commands.migration as command
        from charmer import service
        from charmer.platform import observation
        from test_gateway_lifecycle import save_line

        harness = Harness(self)
        harness.migrate()
        layout = harness.layout
        now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
        started, failed_at = now - datetime.timedelta(days=3), now - datetime.timedelta(days=2)
        instance = "0123456789abcdef"
        workdir = service.gateway_workdir(layout.state)
        service.write_exec_stamp(workdir, service.ExecStamp(
            launcher_version="2.0.0", signature="test", pid=4242, binary="/nonexistent/cli-proxy-api",
            started_at=started.isoformat(), instance=instance))
        records = (observation.LogRecord(service.start_marker_prefix(instance) + " (pid 4242)", started,
                                         "boot:invocation", "c0"),
                   observation.LogRecord(save_line("failed", at=failed_at.strftime("%Y-%m-%d %H:%M:%S")),
                                         failed_at, "boot:invocation", "c1"))
        asked = []

        def journal(*, max_bytes, unit, since, evidence, marker):
            self.assertEqual((evidence, marker), (gateway_events.SAVE_LINE_PATTERN,
                                                  service.start_marker_prefix(instance)))
            asked.append(since)
            since_at = datetime.datetime.fromtimestamp(int(since.removeprefix("@")), datetime.timezone.utc)
            return observation.LogWindow(tuple(record for record in records if record.timestamp >= since_at),
                                         "bounded")

        hold = gateway_hold.hold_path(layout.state)
        recorded = hold.read_bytes()
        cases = {}
        for name, properties in (("unreadable", None), ("active", {"ActiveState": "active"})):
            with mock.patch("charmer.platform.linux_service.unit_properties", return_value=properties), \
                    mock.patch.object(service, "read_hold_journal", side_effect=journal), \
                    mock.patch.object(service, "journal_reaches", return_value=None), \
                    mock.patch.object(service, "journal_drops", return_value=None), \
                    mock.patch.object(service, "journald_start", return_value=started - datetime.timedelta(days=1)):
                cases[name] = command._charmer_hold(layout)
        self.assertIn("cannot be read", cases["unreadable"][0])
        # The same window the stop reads: from the instance's start, so the
        # failed save two days ago holds.
        self.assertEqual(len(cases["active"]), 1, cases)
        self.assertIn("credential", cases["active"][0])
        self.assertNotIn("coverage", cases["active"][0])
        self.assertEqual(asked, [f"@{int((started - gateway_hold.START_MARGIN).timestamp())}"])
        # A running instance without a readable start holds too.
        os.unlink(workdir / service.EXEC_STAMP)
        with mock.patch("charmer.platform.linux_service.unit_properties", return_value={"ActiveState": "active"}), \
                mock.patch.object(service, "read_hold_journal", side_effect=journal):
            self.assertIn("start cannot be read", command._charmer_hold(layout)[0])
        self.assertEqual(hold.read_bytes(), recorded)  # nothing recorded
        # Through --back: refused before anything, the journal as it was.
        seams = harness.seams()
        seams.charmer_hold = command._charmer_hold
        with mock.patch("charmer.platform.linux_service.unit_properties", return_value=None), \
                self.assertRaises(lm.MigrationError) as raised:
            lm.back(harness.environ, seams)
        self.assertIn("charmer's gateway may hold an unsaved credential", str(raised.exception))
        self.assertEqual(harness.journal().status, lm.DONE)
        self.assertNotIn("stop", harness.calls)
        self.assert_nothing_moved_back(harness)

    def test_a_failed_stop_or_disable_moves_nothing(self) -> None:
        for failure in ("stop", "disable"):
            with self.subTest(failure=failure):
                harness = Harness(self)
                harness.migrate()
                if failure == "stop":
                    harness.stop_ok = False
                else:
                    harness.systemd.fail.add(f"disable:{lm.CHARMER_UNIT}")
                with self.assertRaises(lm.MigrationError) as raised:
                    harness.back()
                self.assertIn("nothing was moved back", str(raised.exception))
                self.assertEqual(harness.journal().status, lm.REVERTING)
                self.assert_nothing_moved_back(harness)
                self.assertTrue(os.path.lexists(harness.layout.unit_dir / f"{lm.CHARMER_UNIT}.service"))
                harness.stop_ok = True
                harness.systemd.fail.clear()
                harness.back()
                self.assertEqual(harness.journal().status, lm.REVERTED)
                self.assertFalse(os.path.lexists(harness.layout.unit_dir / f"{lm.CHARMER_UNIT}.service"))

    def test_an_on_demand_gateway_still_holding_its_lock_moves_nothing(self) -> None:
        from charmer import service

        harness = Harness(self, service=False)
        harness.migrate()
        instance = service.gateway_workdir(harness.layout.state) / service.INSTANCE_LOCK
        with held_elsewhere(instance):
            with self.assertRaises(lm.MigrationError) as raised:
                harness.back()
        self.assertIn("a gateway still runs on charmer's state root", str(raised.exception))
        self.assertEqual(harness.calls.count("stop"), 1)
        self.assertEqual(harness.journal().status, lm.REVERTING)
        self.assert_nothing_moved_back(harness)
        harness.back()
        self.assertEqual(harness.journal().status, lm.REVERTED)

    def test_the_earlier_units_come_back_as_they_were(self) -> None:
        for enabled, active, runtime in ((True, True, False), (True, False, False), (False, True, False),
                                         (False, False, False), (True, True, True)):
            with self.subTest(enabled=enabled, active=active, runtime=runtime):
                harness = Harness(self, enabled=enabled, active=active, runtime=runtime)
                harness.migrate()
                self.assertEqual(harness.systemd.unit(OLD_UNIT), {"enabled": False, "runtime": False,
                                                                  "active": False})
                harness.back()
                # Enablement as it was; started when it was enabled or running.
                self.assertEqual(harness.systemd.unit(OLD_UNIT), {"enabled": enabled, "runtime": runtime,
                                                                  "active": enabled or active})
                self.assertNotIn(("enable", f"{OLD_UNIT}.service"),
                                 [] if enabled else harness.systemd.verbs())

    def test_an_interrupted_move_restores_the_units_as_they_were(self) -> None:
        harness = Harness(self, enabled=False, active=True)
        with mock.patch.object(lm, "_mark", side_effect=Crash("units")), self.assertRaises(Crash):
            harness.migrate()
        harness.back()
        self.assertEqual(harness.systemd.unit(OLD_UNIT), {"enabled": False, "runtime": False, "active": True})


class ReversePlanTests(unittest.TestCase):
    """--back checks the whole reverse plan before it stops or moves
    anything, and refuses a conflict or missing data, resumable."""

    def test_an_earlier_root_that_exists_again_refuses_first(self) -> None:
        harness = Harness(self)
        harness.migrate()
        layout = harness.layout
        lt.write(layout.legacy_state / "recreated.txt", "an earlier command ran\n")
        verbs = list(harness.systemd.verbs())
        with self.assertRaises(lm.MigrationError) as raised:
            harness.back()
        self.assertIn("exists again and is not empty", str(raised.exception))
        self.assertEqual(harness.journal().status, lm.DONE)
        self.assertEqual(harness.systemd.verbs(), verbs)
        self.assertNotIn("stop", harness.calls)
        self.assertTrue(layout.state.is_dir())
        shutil.move(layout.legacy_state / "recreated.txt", harness.base / "recreated.txt")
        # An earlier root re-created empty is removed first (the explicit rule).
        harness.back()
        self.assertEqual(harness.journal().status, lm.REVERTED)
        self.assertFalse(os.path.lexists(layout.state))

    def test_an_empty_earlier_gateway_folder_still_gets_the_saved_values_back(self) -> None:
        """An empty earlier configuration (or gateway) folder created after
        the move: the plan removes it and moves charmer's back, and the
        values the move rewrote come back byte for byte first."""

        for xdg in (False, True):
            with self.subTest(separate_gateway_folder=xdg):
                harness = Harness(self, **({"xdg_config": lambda home: home.parent / "xdg"} if xdg else {}))
                layout = harness.layout
                names = (continuity.FILE_NAME, lm.SECRET_POINTER, endpoint.ENDPOINT_FILE)
                before = {name: (layout.legacy_gateway / name).read_bytes() for name in names
                          if (layout.legacy_gateway / name).exists()}
                self.assertIn(continuity.FILE_NAME, before)
                self.assertIn(endpoint.ENDPOINT_FILE, before)
                harness.migrate()
                self.assertIn(continuity.FILE_NAME, harness.journal().document["rewritten"])
                layout.legacy_gateway.mkdir(parents=True)
                harness.back()
                self.assertEqual(harness.journal().status, lm.REVERTED)
                for name, data in before.items():
                    self.assertEqual((layout.legacy_gateway / name).read_bytes(), data, name)

    def test_missing_data_refuses_and_a_vanished_data_child_is_noted(self) -> None:
        harness = Harness(self)
        harness.migrate()
        layout = harness.layout
        os.unlink(moved(layout, layout.legacy_data / "proxy.pid"))
        aside = harness.base / "config-aside"
        os.rename(layout.config, aside)
        with self.assertRaises(lm.MigrationError) as raised:
            harness.back()
        self.assertIn(f"neither {layout.show(layout.legacy_config)} nor {layout.show(layout.config)} exists",
                      str(raised.exception))
        self.assertEqual(harness.journal().status, lm.DONE)
        os.rename(aside, layout.config)
        report = harness.back()
        self.assertIn(f"{layout.show(moved(layout, layout.legacy_data / 'proxy.pid'))} is gone (removed after the "
                      "move); nothing to move back", report.lines)
        self.assertEqual(harness.journal().status, lm.REVERTED)


class StrandedInhibitionTests(unittest.TestCase):
    def test_an_inhibition_left_without_a_journal_is_recovered(self) -> None:
        for command in ("migrate", "back"):
            with self.subTest(command=command):
                harness = Harness(self)
                # What an earlier build left when it died between beginning
                # its inhibition and writing its journal (this process owns
                # the record, which makes it stale).
                lm._begin_inhibition(harness.layout.legacy_state)
                self.assertIsNone(lm.locate(harness.layout))
                if command == "migrate":
                    harness.migrate()
                    self.assertEqual(harness.journal().status, lm.DONE)
                    self.assertIsNone(gateway_inhibition.read(harness.layout.state))
                else:
                    report = harness.back()
                    self.assertIn("cleared the inhibition", report.lines[0])
                    self.assertIsNone(gateway_inhibition.read(harness.layout.legacy_state))
                    self.assertIsNone(lm.locate(harness.layout))


class PurgeSafetyTests(unittest.TestCase):
    def test_a_key_file_charmer_selects_in_the_install_tree(self) -> None:
        import charmer.cli.commands.migration as command

        for variant in ("named", "linked"):
            with self.subTest(variant=variant):
                harness = Harness(self, pointer="install")
                layout = harness.layout
                harness.migrate()
                pointer = layout.gateway / lm.SECRET_POINTER
                if variant == "linked":
                    # The pointer names a link elsewhere that resolves into the tree.
                    link = harness.tree.home / "keys-link"
                    os.symlink(layout.legacy_install / "key-file", link)
                    lt.dump(pointer, {"version": 1, "path": str(link)})
                pointer_bytes = pointer.read_bytes()
                key = layout.legacy_install / "key-file"
                key_entry = lt._entry(key)
                problems = lm.purge_problems(layout, lm.purge_targets(layout))
                self.assertTrue(any("charmer's gateway key file" in problem for problem in problems), problems)
                if variant == "named":
                    # The tree that is deleted, not the emptied folder around it.
                    self.assertIn(f"charmer's gateway key file {layout.show(key)} is in "
                                  f"{layout.show(layout.legacy_install)}, which --purge removes", problems)
                with self.assertRaises(lm.MigrationError) as raised:
                    lm.purge(harness.environ, harness.seams())
                self.assertIn("charmer setup --keys-file", raised.exception.remedy)
                # Refused before the question, too.
                args = SimpleNamespace(legacy_purge=True, json_output=False)
                out, err = io.StringIO(), io.StringIO()
                with mock.patch("charmer.cli.consent.require_human"), \
                        mock.patch("charmer.cli.consent.confirm") as confirm:
                    self.assertEqual(command._migrate_from_legacy(args, out, error_stream=err,
                                                                  environ=harness.environ,
                                                                  seams=harness.seams()), 1)
                confirm.assert_not_called()
                self.assertEqual(lt._entry(key), key_entry)
                self.assertEqual(pointer.read_bytes(), pointer_bytes)
                self.assertIsNone(harness.journal().document["purged"])

    def test_a_symlinked_directory_never_leads_purge_into_protected_content(self) -> None:
        for protected in (Path(".claude"), Path(".local") / "state" / "unrelated-tool"):
            with self.subTest(protected=protected):
                harness = Harness(self)
                home = harness.tree.home
                harness.migrate()
                target = home / protected
                inside = lt.write(target / legacy.LEGACY_CACHE_DIRS[0].name / "protected.txt", "keep\n")
                if os.path.lexists(home / ".cache"):
                    shutil.rmtree(home / ".cache")
                os.symlink(target, home / ".cache")
                with self.assertRaises(lm.MigrationError) as raised:
                    lm.purge(harness.environ, harness.seams())
                self.assertIn("~/.cache is a link (or not a folder) above", str(raised.exception))
                self.assertIn("never follows a link", str(raised.exception))
                self.assertIn("where the link leads", raised.exception.remedy)
                self.assertEqual(inside.read_text(), "keep\n")
                self.assertTrue(harness.layout.legacy_install.is_dir())

    def test_a_cache_that_is_itself_a_link_is_removed_as_a_link(self) -> None:
        harness = Harness(self)
        home = harness.tree.home
        harness.migrate()
        elsewhere = lt.write(home / ".claude" / "kept" / "file.txt", "keep\n")
        cache = home / legacy.LEGACY_CACHE_DIRS[0]
        cache.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(elsewhere.parent, cache)
        report = lm.purge(harness.environ, harness.seams())
        self.assertIn(f"removed {harness.layout.show(cache)}", report.lines)
        self.assertFalse(os.path.lexists(cache))
        self.assertEqual(elsewhere.read_text(), "keep\n")

    def test_an_ancestor_replaced_after_the_check_never_redirects_the_removal(self) -> None:
        """~/.cache swapped for a link to ~/.claude between the check and the
        removal: the removal acts on the directories the check walked."""

        harness = Harness(self)
        harness.migrate()
        home, layout = harness.tree.home, harness.layout
        cache = home / legacy.LEGACY_CACHE_DIRS[0]
        lt.write(cache / "artifact.txt", "an earlier build\n")
        protected = lt.write(home / ".claude" / cache.name / "protected.txt", "keep\n")
        real = lm._held_problems

        def replace_the_ancestor(layout_, held):
            problems = real(layout_, held)
            os.rename(home / ".cache", home / ".cache.checked")
            os.symlink(home / ".claude", home / ".cache")
            return problems

        with mock.patch.object(lm, "_held_problems", replace_the_ancestor):
            report = lm.purge(harness.environ, harness.seams())
        self.assertEqual(protected.read_text(), "keep\n")
        self.assertFalse(os.path.lexists(home / ".cache.checked" / cache.name))
        self.assertIn(f"removed {layout.show(cache)}", report.lines)
        self.assertTrue(harness.journal().document["purged"])

    def test_an_entry_replaced_after_the_check_stops_the_purge(self) -> None:
        harness = Harness(self)
        harness.migrate()
        install = harness.layout.legacy_install
        real = lm._held_problems

        def replace_the_entry(layout_, held):
            problems = real(layout_, held)
            os.rename(install, install.with_name("install.checked"))
            lt.write(install / "planted.txt", "not what was checked\n")
            return problems

        with mock.patch.object(lm, "_held_problems", replace_the_entry), \
                self.assertRaises(lm.MigrationError) as raised:
            lm.purge(harness.environ, harness.seams())
        self.assertIn("changed while --purge ran", str(raised.exception))
        self.assertEqual((install / "planted.txt").read_text(), "not what was checked\n")
        self.assertIsNone(harness.journal().document["purged"])

    def test_a_key_selection_reached_through_a_link_in_the_install_tree(self) -> None:
        """selected-key -> install/key-link -> a key file outside the tree:
        both ends lie outside what goes, the hop does not."""

        harness = Harness(self)
        layout = harness.layout
        harness.migrate()
        home = harness.tree.home
        outside = lt.write(home / "keys" / "real-key", lt.SECRET)
        hop = layout.legacy_install / "key-link"
        os.symlink(outside, hop)
        selected = home / "selected-key"
        os.symlink(hop, selected)
        lt.dump(layout.gateway / lm.SECRET_POINTER, {"version": 1, "path": str(selected)})
        problems = lm.purge_problems(layout, lm.purge_targets(layout))
        self.assertIn(f"charmer's gateway key file {layout.show(selected)} is selected through the link "
                      f"{layout.show(selected)}", problems)
        with self.assertRaises(lm.MigrationError) as raised:
            lm.purge(harness.environ, harness.seams())
        self.assertIn(f"select the key file by a plain path outside {layout.show(layout.legacy_data)} (charmer "
                      "setup --keys-file PATH), then run --purge again", raised.exception.remedy)
        self.assertTrue(os.path.islink(hop))
        self.assertEqual(outside.read_bytes(), lt.SECRET)
        self.assertIsNone(harness.journal().document["purged"])

    def test_an_xdg_base_swapped_for_a_link_during_the_walk_is_never_followed(self) -> None:
        """XDG_CACHE_HOME=$HOME/.cache, and .cache becomes a link into
        ~/.claude between two steps of the walk: the walk refuses it."""

        harness = Harness(self)
        harness.migrate()
        home, layout = harness.tree.home, harness.layout
        environ = {**harness.environ, "XDG_CACHE_HOME": str(home / ".cache")}
        cache = home / legacy.LEGACY_CACHE_DIRS[0]
        lt.write(cache / "artifact.txt", "an earlier build\n")
        nested = home / ".claude" / "nested"
        protected = lt.write(nested / cache.name / "protected.txt", "keep\n")
        real_open = os.open
        swapped = []

        def open_(target, flags, *args, **kwargs):
            if not swapped and os.fspath(target) in (".cache", str(home / ".cache")):
                os.rename(home / ".cache", home / ".cache.real")
                os.symlink(nested, home / ".cache")
                swapped.append(target)
            return real_open(target, flags, *args, **kwargs)

        with mock.patch.object(lm.os, "open", open_), self.assertRaises(lm.MigrationError) as raised:
            lm.purge(environ, harness.seams())
        self.assertTrue(swapped)
        self.assertIn("never follows a link", str(raised.exception))
        self.assertEqual(protected.read_text(), "keep\n")
        self.assertTrue((home / ".cache.real" / cache.name / "artifact.txt").is_file())
        self.assertTrue(layout.legacy_install.is_dir())
        self.assertIsNone(harness.journal().document["purged"])

    def test_a_home_or_intermediate_folder_behind_a_link_refuses(self) -> None:
        for where in ("home", "intermediate"):
            with self.subTest(where=where):
                harness = Harness(self)
                harness.migrate()
                home = harness.tree.home
                if where == "home":
                    link = harness.base / "home-link"
                    os.symlink(home, link)
                    environ = {**harness.environ, "HOME": str(link)}
                    shown = "~"
                else:
                    os.rename(home / ".local", home / ".local.real")
                    os.symlink(home / ".local.real", home / ".local")
                    environ = harness.environ
                    shown = "~/.local"
                layout = lm.Layout.of(environ)
                self.assertTrue(os.path.isdir(layout.legacy_install))
                with self.assertRaises(lm.MigrationError) as raised:
                    lm.purge(environ, harness.seams())
                self.assertIn(f"{shown} is a link (or not a folder) above", str(raised.exception))
                self.assertIn("where the link leads", raised.exception.remedy)
                self.assertTrue(harness.layout.legacy_install.is_dir())
                self.assertIsNone(harness.journal().document["purged"])

    def test_a_key_selection_through_dotdot_refuses(self) -> None:
        """~/sel-dir/../real-key with sel-dir a link into the install tree
        (really inside it), and install/../keys/real-key (the key stays,
        its selected path would break): both refuse, nothing removed."""

        for variant in ("link-then-dotdot", "through-the-install-tree"):
            with self.subTest(variant=variant):
                harness = Harness(self)
                layout = harness.layout
                harness.migrate()
                home = harness.tree.home
                if variant == "link-then-dotdot":
                    key = lt.write(layout.legacy_install / "real-key", lt.SECRET.decode())
                    (layout.legacy_install / "nested").mkdir()
                    os.symlink(layout.legacy_install / "nested", home / "sel-dir")
                    named = "~/sel-dir/../real-key"
                    self.assertEqual(Path(os.path.realpath(home / "sel-dir" / ".." / "real-key")), key)
                else:
                    key = lt.write(layout.legacy_data / "keys" / "real-key", lt.SECRET.decode())
                    named = str(layout.legacy_install / ".." / "keys" / "real-key")
                lt.dump(layout.gateway / lm.SECRET_POINTER, {"version": 1, "path": named})
                with self.assertRaises(lm.MigrationError) as raised:
                    lm.purge(harness.environ, harness.seams())
                self.assertIn("is selected through a .. component", str(raised.exception))
                self.assertIn("plain path outside", raised.exception.remedy)
                self.assertEqual(key.read_bytes(), lt.SECRET)
                self.assertTrue(layout.legacy_install.is_dir())
                self.assertIsNone(harness.journal().document["purged"])

    def test_a_home_spelled_with_a_double_slash_never_hides_a_key_in_the_tree(self) -> None:
        """HOME=//tmp/.../home names the same folders; a key file inside the
        install tree, selected by the pointer or by the environment, still
        refuses the purge before the question, and nothing changes."""

        import charmer.cli.commands.migration as command

        for source in ("pointer", "environment"):
            with self.subTest(source=source):
                harness = Harness(self, pointer="install")
                layout = harness.layout
                harness.migrate()
                home = harness.tree.home
                environ = {**harness.environ, "HOME": "/" + str(home)}
                self.assertTrue(environ["HOME"].startswith("//"))
                key = layout.legacy_install / "key-file"
                if source == "environment":
                    elsewhere = lt.write(home / "keys" / "plain-key", lt.SECRET.decode())
                    lt.dump(layout.gateway / lm.SECRET_POINTER, {"version": 1, "path": str(elsewhere)})
                    environ["CHARMER_SECRET_ENV"] = "/" + str(key)
                key_bytes = key.read_bytes()
                journal = lm.journal_path(layout.state)
                journal_bytes = journal.read_bytes()
                spelled = lm.Layout.of(environ)
                self.assertTrue(str(spelled.legacy_install).startswith("//"))
                problems = lm.purge_problems(spelled, lm.purge_targets(spelled))
                self.assertTrue(any("charmer's gateway key file" in problem and "which --purge removes" in problem
                                    for problem in problems), problems)
                args = SimpleNamespace(legacy_purge=True, json_output=False)
                out, err = io.StringIO(), io.StringIO()
                with mock.patch("charmer.cli.consent.require_human"), \
                        mock.patch("charmer.cli.consent.confirm") as confirm:
                    self.assertEqual(command._migrate_from_legacy(args, out, error_stream=err, environ=environ,
                                                                  seams=harness.seams()), 1)
                confirm.assert_not_called()
                self.assertIn("select the key file by a plain path outside", err.getvalue())
                with self.assertRaises(lm.MigrationError):
                    lm.purge(environ, harness.seams())
                self.assertEqual(key.read_bytes(), key_bytes)
                self.assertEqual(journal.read_bytes(), journal_bytes)
                self.assertTrue(layout.legacy_install.is_dir())

    def test_the_default_key_file_and_the_supervised_selection_are_checked(self) -> None:
        """No pointer: the default key file (~/.config/charmer/secrets/...)
        is what the store reads. Its folder a link into the install tree
        refuses; so it does when a shell override selects a plain file but
        the supervised gateway (whose unit drops the override) reads the
        default; a plain default lets the purge run."""

        from charmer import paths, secret_store

        for case in ("linked default", "override, linked supervised default", "plain default"):
            with self.subTest(case=case):
                harness = Harness(self)
                layout = harness.layout
                harness.migrate()
                os.unlink(layout.gateway / lm.SECRET_POINTER)
                environ = dict(harness.environ)
                default = paths.secret_env_default(environ)
                self.assertEqual(default.parent, layout.gateway / "secrets")
                if default.parent.exists():
                    shutil.rmtree(default.parent)
                if case == "plain default":
                    key = lt.write(default, lt.SECRET.decode(), 0o600)
                else:
                    key = lt.write(layout.legacy_install / "keys" / default.name, lt.SECRET.decode(), 0o600)
                    os.symlink(key.parent, default.parent)
                if case.startswith("override"):
                    plain = lt.write(harness.tree.home / "keys" / "shell-key", lt.SECRET.decode(), 0o600)
                    environ[secret_store.SECRET_ENV_OVERRIDE] = str(plain)
                    self.assertEqual(secret_store.key_file_location(environ).path, plain)
                    self.assertEqual(secret_store.service_key_file(environ).path, default)
                else:
                    self.assertEqual(secret_store.key_file_location(environ).path, default)
                if case == "plain default":
                    lm.purge(environ, harness.seams())
                    self.assertFalse(os.path.lexists(layout.legacy_install))
                    self.assertEqual(key.read_bytes(), lt.SECRET)
                    continue
                problems = lm.purge_problems(lm.Layout.of(environ), lm.purge_targets(layout))
                self.assertIn(f"charmer's gateway key file {layout.show(default)} is selected through the link "
                              f"{layout.show(default.parent)}", problems)
                with self.assertRaises(lm.MigrationError) as raised:
                    lm.purge(environ, harness.seams())
                self.assertIn("select the key file by a plain path outside", raised.exception.remedy)
                self.assertEqual(key.read_bytes(), lt.SECRET)
                self.assertTrue(layout.legacy_install.is_dir())
                self.assertIsNone(harness.journal().document["purged"])

    @unittest.skipIf(not hasattr(os, "O_PATH"), "traversal-only descriptors need O_PATH (Linux)")
    def test_a_search_only_folder_on_the_way_is_passable(self) -> None:
        """A folder above HOME that this user may search but not read (as a
        multi-user host's 0711 /home is for others): the walk passes it."""

        harness = Harness(self)
        layout = harness.layout
        harness.migrate()
        os.chmod(harness.base, 0o100)
        self.addCleanup(os.chmod, harness.base, 0o700)
        if os.access(harness.base, os.R_OK):
            self.skipTest("this user reads any folder (root)")
        report = lm.purge(harness.environ, harness.seams())
        self.assertIn(f"removed {layout.show(layout.legacy_install)}", report.lines)
        self.assertTrue(harness.journal().document["purged"])

    def test_a_plain_key_path_outside_lets_the_purge_run(self) -> None:
        harness = Harness(self)
        layout = harness.layout
        harness.migrate()
        key = lt.write(harness.tree.home / "keys" / "real-key", lt.SECRET.decode())
        lt.dump(layout.gateway / lm.SECRET_POINTER, {"version": 1, "path": "~/keys/real-key"})
        environ = {**harness.environ, "CHARMER_SECRET_ENV": str(key)}
        self.assertEqual(lm.purge_problems(layout, lm.purge_targets(layout)), [])
        lm.purge(environ, harness.seams())
        self.assertFalse(os.path.lexists(layout.legacy_install))
        self.assertEqual(key.read_bytes(), lt.SECRET)
        self.assertTrue(harness.journal().document["purged"])

    def test_the_question_names_a_file_at_a_former_root_as_kept(self) -> None:
        import charmer.cli.commands.migration as command

        harness = Harness(self)
        layout = harness.layout
        harness.migrate()
        lt.write(layout.legacy_config, "a file written where the folder was\n")
        args = SimpleNamespace(legacy_purge=True, json_output=False)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("charmer.cli.consent.require_human"), \
                mock.patch("charmer.cli.consent.confirm", return_value=False):
            self.assertEqual(command._migrate_from_legacy(args, out, error_stream=err, environ=harness.environ,
                                                          seams=harness.seams()), 3)
        self.assertIn(f"  {layout.show(layout.legacy_config)} (a file, kept)\n", out.getvalue())
        self.assertNotIn(f"{layout.show(layout.legacy_config)} (only if nothing is left in it)", out.getvalue())
        self.assertIn(f"  {layout.show(layout.legacy_data)} (only if nothing is left in it)\n", out.getvalue())

    def test_a_former_root_that_is_not_an_empty_folder_stays(self) -> None:
        harness = Harness(self)
        layout = harness.layout
        harness.migrate()
        notes = lt.write(layout.legacy_config, "a file written where the folder was\n")
        link = layout.legacy_state
        os.symlink(harness.tree.home / "elsewhere", link)
        report = lm.purge(harness.environ, harness.seams())
        self.assertEqual(notes.read_text(), "a file written where the folder was\n")
        self.assertTrue(os.path.islink(link))
        self.assertIn(f"kept {layout.show(layout.legacy_config)}: it is a file, not the folder the earlier "
                      "installation left (check it; remove it yourself)", report.lines)
        self.assertIn(f"kept {layout.show(link)}: it is a link, not the folder the earlier installation left "
                      "(check it; remove it yourself)", report.lines)
        self.assertFalse(os.path.lexists(layout.legacy_install))

    def test_a_former_root_whose_type_changes_after_the_confirmation_stays(self) -> None:
        import charmer.cli.commands.migration as command

        harness = Harness(self)
        layout = harness.layout
        harness.migrate()
        layout.legacy_config.mkdir(parents=True)
        written = "written after the confirmation\n"

        def confirm(*_args, **_kwargs):
            layout.legacy_config.rmdir()
            lt.write(layout.legacy_config, written)
            return True

        args = SimpleNamespace(legacy_purge=True, json_output=False)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("charmer.cli.consent.require_human"), \
                mock.patch("charmer.cli.consent.confirm", side_effect=confirm):
            self.assertEqual(command._migrate_from_legacy(args, out, error_stream=err, environ=harness.environ,
                                                          seams=harness.seams()), 0)
        self.assertIn(f"{layout.show(layout.legacy_config)} (only if nothing is left in it)", out.getvalue())
        self.assertEqual(layout.legacy_config.read_text(), written)
        self.assertIn(f"kept {layout.show(layout.legacy_config)}: it is a file", out.getvalue())

    def test_what_appeared_in_the_earlier_data_root_after_the_move_stays(self) -> None:
        harness = Harness(self)
        layout = harness.layout
        harness.migrate()
        created = lt.dump(layout.legacy_data / "auth" / "account-created-after-move.json", {"type": "claude"})
        report = lm.purge(harness.environ, harness.seams())
        self.assertFalse(os.path.lexists(layout.legacy_install))
        self.assertEqual(lt.read_json(created), {"type": "claude"})
        line = next(line for line in report.lines if line.startswith(f"kept {layout.show(layout.legacy_data)}"))
        self.assertIn("it holds auth", line)
        self.assertTrue(harness.journal().document["purged"])


class UnicodePathTests(unittest.TestCase):
    """The values the move rewrites are found by what they decode to: a path
    written as UTF-8 (charmer's own writers) or with \\u escapes, under a
    Unicode HOME or XDG_STATE_HOME; every other byte stays, and --back
    returns the files byte for byte."""

    def test_unicode_roots_in_both_spellings(self) -> None:
        for where in ("home", "xdg_state"):
            for spelling in ("literal", "escaped"):
                with self.subTest(where=where, spelling=spelling):
                    build = {"home": "hömé-nébo"} if where == "home" else {
                        "xdg_state": lambda home: home.parent / "stäte-ünïcode"}
                    harness = Harness(self, **build)
                    layout = harness.layout
                    self.assertFalse(str(layout.legacy_state).isascii() and str(layout.home).isascii())
                    key = layout.legacy_gateway / "schlüssel"
                    lt.write(key, lt.SECRET)
                    documents = {"continuity.json": {**lt.read_json(layout.legacy_gateway / "continuity.json"),
                                                     "state_root": str(layout.legacy_state)},
                                 lm.SECRET_POINTER: {"version": 1, "path": str(key)}}
                    raw = {}
                    for name, document in documents.items():
                        data = (strict_json.pretty_file_bytes(document) if spelling == "literal"
                                else (json.dumps(document, indent=2, sort_keys=True) + "\n").encode())
                        state.atomic_write(layout.legacy_gateway / name, data)
                        raw[name] = data
                    self.assertEqual(spelling == "escaped", b"\\u" in raw["continuity.json"])
                    harness.migrate()
                    changes = {"continuity.json": (str(layout.legacy_state), str(layout.state)),
                               lm.SECRET_POINTER: (str(key), str(layout.gateway / "schlüssel"))}
                    for name, (old, new) in changes.items():
                        data = (layout.gateway / name).read_bytes()
                        ascii_only = spelling == "escaped"
                        self.assertEqual(data.replace(json.dumps(new, ensure_ascii=ascii_only).encode(),
                                                      json.dumps(old, ensure_ascii=ascii_only).encode()), raw[name])
                        self.assertEqual(lt.read_json(layout.gateway / name)[
                            "state_root" if name == "continuity.json" else "path"], new)
                    harness.back()
                    for name, data in raw.items():
                        self.assertEqual((layout.legacy_gateway / name).read_bytes(), data)

    def test_a_tilde_pointer_with_a_unicode_name(self) -> None:
        harness = Harness(self)
        layout = harness.layout
        key = lt.write(layout.legacy_gateway / "klüch", lt.SECRET)
        relative = key.relative_to(layout.home).as_posix()
        raw = strict_json.pretty_file_bytes({"version": 1, "path": f"~/{relative}"})
        state.atomic_write(layout.legacy_gateway / lm.SECRET_POINTER, raw)
        harness.migrate()
        self.assertEqual(lt.read_json(layout.gateway / lm.SECRET_POINTER)["path"], "~/.config/charmer/klüch")
        self.assertEqual((layout.gateway / lm.SECRET_POINTER).read_bytes(),
                         raw.replace(f"/{legacy.LEGACY_DIR}/".encode(), b"/charmer/"))


class FreshStartTests(unittest.TestCase):
    """An earlier installation that cannot move names the way to start
    fresh; following it lets charmer's setup run, the earlier data kept."""

    def follow(self, harness: Harness) -> dict:
        roots = [root for root in harness.layout.legacy_roots() if os.path.lexists(root)]
        kept = lt.snapshot(*roots)
        for root in roots:
            os.rename(root, root.with_name(root.name + ".retired"))
        self.assertFalse(lm.fresh_with_legacy(harness.environ))
        self.assertFalse(harness.layout.legacy_present())
        return kept

    def refused_with_the_remedy(self, harness: Harness, needle: str) -> str:
        checks = lm.check_preconditions(harness.layout, harness.seams())
        failed = next(check for check in checks if not check.ok and needle in check.name)
        self.assertIn("start fresh instead", failed.remedy)
        self.assertIn(lm.FRESH_START_GUIDE, failed.remedy)
        for root in harness.layout.legacy_roots():
            if os.path.lexists(root):
                self.assertIn(harness.layout.show(root), failed.remedy)
        return failed.remedy

    def test_a_removed_nix_installation(self) -> None:
        harness = Harness(self, xdg_config=lambda home: home.parent / "xdg")
        lt.write(harness.layout.legacy_state / "channel", "nix\n")
        self.assertNotEqual(harness.layout.legacy_config, harness.layout.legacy_gateway)
        self.refused_with_the_remedy(harness, "release bundle installation")
        self.assertIn("start fresh instead", lm.first_run_hint(harness.environ))
        roots = [root for root in harness.layout.legacy_roots() if os.path.lexists(root)]
        before = {root: lt.snapshot(root) for root in roots}
        self.follow(harness)
        for root, entries in before.items():
            aside = root.with_name(root.name + ".retired")
            self.assertEqual(sorted(lt.snapshot(aside)), sorted(path.replace(str(root), str(aside), 1)
                                                                for path in entries))

    def test_configuration_without_state(self) -> None:
        harness = Harness(self)
        shutil.rmtree(harness.layout.legacy_state)
        self.refused_with_the_remedy(harness, "its state root exists")
        self.follow(harness)

    def test_the_fresh_start_stops_the_earlier_gateway_before_any_rename(self) -> None:
        from _layout import REPO_ROOT

        guide = (REPO_ROOT / "docs" / "guides" / "legacy-migration.md").read_text()
        section = guide.split("\n## Start fresh instead\n", 1)[1].split("\n## ", 1)[0]
        rename = section.index("2. Rename its folders aside")
        for step in (f"`{legacy.NAME} gateway service uninstall`", f"`{legacy.NAME} gateway stop`",
                     f"`{legacy.NAME} gateway status`", "may start it again", "when its launcher is gone",
                     "none of its gateways still runs"):
            self.assertLess(section.index(step), rename, step)
        self.assertLess(section.index(f"`{legacy.NAME} gateway service uninstall`"),
                        section.index(f"`{legacy.NAME} gateway stop`"))
        harness = Harness(self)
        self.assertIn("stop its gateway (no process of it may run), rename", lm.fresh_start(harness.layout))

    def test_setup_runs_once_the_earlier_folders_are_aside(self) -> None:
        from charmer.cli import entry

        harness = Harness(self)
        os.unlink(harness.layout.legacy_install / self_update_current())
        err = io.StringIO()
        with mock.patch.dict(os.environ, harness.environ, clear=True), mock.patch("sys.stderr", err):
            self.assertEqual(entry.main(["setup"], output_stream=io.StringIO()), 1)
        self.assertIn("start fresh instead", err.getvalue())
        self.follow(harness)
        self.assertFalse(lm.fresh_with_legacy(harness.environ))


def self_update_current() -> str:
    from charmer import self_update

    return self_update.CURRENT


class GatewayStepTextTests(unittest.TestCase):
    def test_the_step_says_what_the_service_decision_was(self) -> None:
        for service, text in ((True, lm.GATEWAY_TEXT[True]), (False, lm.GATEWAY_TEXT[False])):
            with self.subTest(service=service):
                harness = Harness(self, service=service)
                report = harness.migrate()
                self.assertIn(text, report.lines)
                self.assertIn(f"  done  {text}", lm.status_lines(harness.environ))
        harness = Harness(self)
        harness.verify_problems.append("BLOCKED: x")
        with mock.patch.object(lm, "_step_gateway", side_effect=lm.MigrationError("stopped")):
            with self.assertRaises(lm.MigrationError):
                harness.migrate()
        self.assertIn(f"  todo  {lm.STEP_TEXT[lm.GATEWAY]}", lm.status_lines(harness.environ))


class LineupRefusalTests(unittest.TestCase):
    """A lineup request (it writes records) refuses while a move is
    unfinished, through the skill transport too, before anything is read."""

    def test_a_lineup_request_refuses_while_the_move_is_unfinished(self) -> None:
        from charmer.cli import entry

        harness = Harness(self)
        harness.verify_problems.append("BLOCKED: x")
        with self.assertRaises(lm.MigrationError):
            harness.migrate()
        self.assertIsNotNone(lm.pending(harness.environ))
        self.assertIsNone(gateway_inhibition.read(harness.layout.state))  # released before verify
        record = harness.layout.state / "sessions" / f"{harness.tree.mid}.json"
        before = record.read_bytes()
        for skill in (True, False):
            with self.subTest(skill=skill):
                argv = ["lineup", *(["--session", harness.tree.mid] if skill else []), "pin"]
                out, err = io.StringIO(), io.StringIO()
                with mock.patch.dict(os.environ, harness.environ, clear=True), mock.patch("sys.stderr", err):
                    code = entry.main(argv, output_stream=out)
                stream = out.getvalue() if skill else err.getvalue()
                self.assertEqual(code, 0 if skill else 1)
                self.assertIn("the move from the earlier installation did not finish; nothing was run", stream)
                self.assertIn(lm.BACK, stream)
                self.assertEqual(record.read_bytes(), before)


class StatusTests(unittest.TestCase):
    def test_status_and_the_refusal_of_every_other_command(self) -> None:
        harness = Harness(self)
        self.assertEqual(lm.status_lines(harness.environ),
                         [f"no migration is recorded; an installation of {legacy.NAME} exists"])
        harness.verify_problems.append("BLOCKED: x")
        with self.assertRaises(lm.MigrationError):
            harness.migrate()
        unfinished = lm.pending(harness.environ)
        message, remedy = lm.pending_refusal(unfinished)
        self.assertIn("did not finish; nothing was run", message)
        self.assertIn(lm.BACK, remedy)
        lines = lm.status_lines(harness.environ)
        self.assertTrue(lines[0].startswith("migration: running"))
        self.assertIn(f"  todo  {lm.STEP_TEXT[lm.VERIFY]}", lines)
        # An unreadable journal counts as unfinished.
        lm.journal_path(harness.layout.state).write_text("{")
        self.assertIsNotNone(lm.pending(harness.environ))


class PurgeTests(unittest.TestCase):
    def decoys(self, harness: Harness) -> list[Path]:
        home = harness.tree.home
        return [lt.write(home / ".local" / "state" / "unrelated-tool" / "anchor", "x"),
                lt.write(home / ".local" / "state" / "other-journal" / "journal", "x"),
                lt.write(harness.layout.unit_dir / "unrelated-tool.service", "x", 0o644),
                lt.write(home / ".claude" / "settings.json", "{}"),
                lt.write(home / ".ssh" / "id", "x")]

    def test_purge_removes_only_the_retained_installation(self) -> None:
        harness = Harness(self)
        decoys = {str(path): lt._entry(path) for path in self.decoys(harness)}
        caches = [lt.mkdirs(harness.tree.home / relative) for relative in legacy.LEGACY_CACHE_DIRS]
        with self.assertRaises(lm.MigrationError) as raised:
            lm.purge(harness.environ, harness.seams())
        self.assertIn("no completed migration", str(raised.exception))
        harness.migrate()
        layout = harness.layout
        targets = lm.purge_targets(layout)
        self.assertEqual(targets, [lm.Removal(layout.legacy_install, True), *(lm.Removal(path, True) for path in caches),
                                   lm.Removal(layout.legacy_data, False)])
        self.assertEqual(lm.purge_problems(layout, targets), [])
        report = lm.purge(harness.environ, harness.seams())
        self.assertEqual(report.lines, [f"removed {layout.show(target.path)}" for target in targets])
        for target in targets:
            self.assertFalse(os.path.lexists(target.path))
        self.assertEqual({path: lt._entry(Path(path)) for path in decoys}, decoys)
        self.assertTrue(harness.journal().document["purged"])
        with self.assertRaises(lm.MigrationError) as raised:
            harness.back()
        self.assertIn("purged", str(raised.exception))

    def test_purge_asks_first(self) -> None:
        import charmer.cli.commands.migration as command

        harness = Harness(self)
        harness.migrate()
        args = SimpleNamespace(legacy_purge=True, json_output=False)
        out = io.StringIO()
        with mock.patch("charmer.cli.consent.require_human"), \
                mock.patch("charmer.cli.consent.confirm", return_value=False):
            self.assertEqual(command._migrate_from_legacy(args, out, environ=harness.environ,
                                                          seams=harness.seams()), 3)
        self.assertIn("Nothing was removed.", out.getvalue())
        self.assertTrue(harness.layout.legacy_data.is_dir())


class DoctorAndFirstRunTests(unittest.TestCase):
    def test_remnants_before_and_after(self) -> None:
        harness = Harness(self)
        findings = dict(lm.remnants(harness.environ))
        self.assertIn(f"{lm.DRY_RUN}, then {lm.COMMAND}", findings.values())
        self.assertTrue(lm.fresh_with_legacy(harness.environ))
        harness.migrate()
        self.assertFalse(lm.fresh_with_legacy(harness.environ))
        findings = lm.remnants(harness.environ)
        text = "\n".join(f"{finding} | {remedy}" for finding, remedy in findings)
        self.assertIn(f"the earlier installation is kept for {lm.BACK}", text)
        self.assertIn(lm.PURGE, text)
        self.assertIn("need revalidation", text)
        self.assertIn(".env exists", text)
        self.assertNotIn(str(harness.tree.home), text)
        lt.write(harness.tree.launcher(legacy.LEGACY_LAUNCHERS[0]), "#!/bin/sh\n", 0o755)
        lt.write(harness.tree.unit(OLD_UNIT), "x", 0o644)
        text = "\n".join(finding for finding, _remedy in lm.remnants(harness.environ))
        self.assertIn(f"{legacy.NAME} launchers remain", text)
        self.assertIn(f"{legacy.NAME} gateway unit files remain", text)

    def test_a_fresh_account_pays_nothing(self) -> None:
        base = Path(tempfile.mkdtemp(prefix="charmer-legacy-"))
        self.addCleanup(shutil.rmtree, base, True)
        environ = {"HOME": str(base)}
        self.assertEqual(lm.remnants(environ), [])
        self.assertFalse(lm.fresh_with_legacy(environ))

    def test_setup_and_a_bare_launch_point_to_the_migration(self) -> None:
        from charmer.cli import entry

        harness = Harness(self)
        err = io.StringIO()
        with mock.patch.dict(os.environ, harness.environ, clear=True), mock.patch("sys.stderr", err):
            self.assertEqual(entry.main(["setup"], output_stream=io.StringIO()), 1)
        self.assertIn(lm.COMMAND, err.getvalue())
        self.assertIn(lm.DRY_RUN, err.getvalue())

    def test_every_other_command_refuses_while_a_journal_is_unfinished(self) -> None:
        from charmer.cli import entry

        harness = Harness(self)
        harness.verify_problems.append("BLOCKED: x")
        with self.assertRaises(lm.MigrationError):
            harness.migrate()
        err = io.StringIO()
        with mock.patch.dict(os.environ, harness.environ, clear=True), mock.patch("sys.stderr", err):
            code = entry.main(["doctor"], output_stream=io.StringIO())
        self.assertNotEqual(code, 0)
        self.assertIn("did not finish; nothing was run", err.getvalue())

    def test_the_installer_hands_off_to_the_migration(self) -> None:
        from charmer import install_txn

        harness = Harness(self)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(install_txn.main(["legacy"], environ=harness.environ, stdout=out)
                             if "stdout" in install_txn.main.__code__.co_varnames
                             else install_txn.main(["legacy"], environ=harness.environ), install_txn.EXIT_OK)
        self.assertIn(f"run `{lm.COMMAND}` instead of `charmer setup`", out.getvalue())
        fresh = Path(tempfile.mkdtemp(prefix="charmer-legacy-"))
        self.addCleanup(shutil.rmtree, fresh, True)
        self.assertEqual(install_txn.main(["legacy"], environ={"HOME": str(fresh)}), install_txn.EXIT_REFUSED)

    def test_a_session_of_the_earlier_release_counts_as_a_session(self) -> None:
        from charmer.cli import consent

        with self.assertRaises(consent.ConsentRefused) as raised:
            consent.require_human(lm.COMMAND, {legacy.LEGACY_MANAGED_ID_ENV: "x"})
        self.assertIn(legacy.LEGACY_MANAGED_ID_ENV, str(raised.exception))


class ResumeAfterMigrationTests(V4Case):
    """The session the earlier release recorded, unchanged by the move,
    passes doctor and converges, and resumes through the real prepare and
    launch path (a fake client): the same bindings on the same routes and
    windows, under charmer's selectors, hooks and environment."""

    def setUp(self) -> None:
        unittest.TestCase.setUp(self)
        self.harness = Harness(self)
        self.layout = self.harness.layout
        self.mid = self.harness.tree.mid
        self.record_before = (self.layout.legacy_state / "sessions" / f"{self.mid}.json").read_bytes()
        self.scope_before = self.tree(self.layout.legacy_state / "scopes" / self.mid)
        self.harness.migrate()
        self.root = self.harness.base
        self.project = self.harness.tree.project
        self.env = dict(self.harness.environ)
        state.atomic_write(self.layout.gateway / "api-key", (FIXTURE_GATEWAY_TOKEN + "\n").encode())
        self.platform = pin.host_platform()
        install = pin.owned_path(self.env, "2.1.281", self.platform)
        state.ensure_private_dir(install.parent)
        install.write_bytes(b"#!/bin/fake-claude\n")
        install.chmod(0o755)
        self.fake_claude = {
            "version": "2.1.281",
            "platforms": {self.platform: {"sha256": hashlib.sha256(install.read_bytes()).hexdigest(),
                                          "size": install.stat().st_size}},
            "manifest_sha256": "1" * 64, "signature_sha256": "2" * 64,
            "key_fingerprint": "31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE", "verified_at": "2026-09-25",
            "evidence": {self.platform: "battery", "receipt_sha256": "3" * 64},
        }
        self.execs = []
        self.runtime = self.make_runtime()

    def complete_renames(self) -> Path:
        """The fixture catalog with a ``renamed`` entry for every selector it
        serves under charmer's prefix (it lists only the ones the fixture
        record names), as catalog 39 renames every gateway selector."""

        root = self.harness.base / "assets"
        shutil.copytree(FIXTURE_ROOT, root)
        path = root / "catalog" / "retired.json"
        document = json.loads(path.read_text())
        renamed = document["renamed"]
        known = {spec["selector"] for spec in renamed.values()}
        since = next(iter(renamed.values()))["since_catalog"]
        for entry in catalog.load_catalog(FIXTURE_ROOT).lines.values():
            for _level, selector, _contract in catalog.line_selectors(entry):
                if selector.startswith(lm.CHARMER_ENV_PREFIX.lower().rstrip("_") + "-") and selector not in known:
                    renamed[legacy.LEGACY_SELECTOR_PREFIX + selector.removeprefix("charmer-")] = {
                        "selector": selector, "since_catalog": since}
        path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
        catalog.load_catalog(root)  # still a valid catalog
        return root

    def routes(self, applied: dict) -> dict:
        """Each slot's (key, effort) and the route its selector names in the catalog."""

        lcat = self.runtime.catalog
        found = {}
        for slot, binding in [("lead", applied["lead"]), *sorted(applied["agents"].items())]:
            selector = lcat.current_selector(binding["selector"])
            line = lcat.lines[binding["key"]]
            efforts = {sel: contract for _level, sel, contract in __import__("charmer").catalog.line_selectors(line)}
            found[slot] = (binding["key"], binding["effort"], line["provider"], line["wire_model"],
                           efforts.get(selector), line["context"]["provider_tokens"],
                           selector.endswith("[1m]"))
        return found

    def test_the_moved_record_and_scope_keep_their_bytes(self) -> None:
        self.assertEqual(self.store.read_record_bytes(self.mid), self.record_before)
        self.assertEqual(self.tree(self.live(self.mid)), self.scope_before)
        record = self.store.load(self.mid)
        self.assertTrue(any(binding["selector"].startswith(legacy.LEGACY_SELECTOR_PREFIX)
                            for binding in record["applied"]["agents"].values()))

    def test_doctor_reports_the_stale_scope_without_a_block(self) -> None:
        import charmer.cli.doctor as doctor

        problems, _info, attention = doctor._collect_doctor_reports(self.runtime)
        self.assertEqual([str(line) for line in problems], [])
        text = "\n".join(map(str, attention))
        self.assertIn(f"scope compiled by {legacy.NAME} before the move to charmer", text)
        self.assertIn(f"charmer -r {self.mid}", text)

    def test_the_earlier_scopes_are_one_line_that_repair_all_clears(self) -> None:
        import charmer.cli.doctor as doctor
        from charmer.cli import doctor_actions

        def earlier_rows() -> list:
            problems, _info, attention = doctor._collect_doctor_reports(self.runtime)
            return [row for row in doctor.present_report(problems, attention).rows if row.action == "earlier-scopes"]

        rows = earlier_rows()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual((rows[0].level, rows[0].area, rows[0].remedy),
                         ("attention", "Sessions", "charmer doctor --repair-all"))
        self.assertIn(self.mid[:8], rows[0].text)
        self.assertIn("charmer doctor --repair-all recompiles the stopped ones now", rows[0].text)
        self.assertGreaterEqual(len(rows[0].originals), 2)  # the hook-target and scope-integrity lines
        # The moved record has no recorded end: live or unknown, so repair-all leaves it.
        out = io.StringIO()
        self.assertEqual(doctor_actions._doctor_repair_all(self.runtime, out), 0, out.getvalue())
        self.assertIn("Repair-all: 0 session(s) converged", out.getvalue())
        self.assertIn(f"skipped {self.mid[:8]}: last event none (live or unknown)", out.getvalue())
        self.assertEqual(len(earlier_rows()), 1)
        from charmer.cli import session_actions

        no_proc = self.root / "no-proc"
        no_proc.mkdir()
        self.assertEqual(session_actions._mark_ended_ids(self.runtime, [self.mid], proc_root=no_proc),
                         [(self.mid, "marked ended")])
        out = io.StringIO()
        self.assertEqual(doctor_actions._doctor_repair_all(self.runtime, out), 0, out.getvalue())
        self.assertIn("Repair-all: 1 session(s) converged", out.getvalue())
        settings = json.loads((self.live(self.mid) / "settings.json").read_bytes())
        self.assertEqual({path.parent for path in scope.hook_targets(settings)}, {self.layout.state / "bin"})
        self.assertEqual(earlier_rows(), [])

    def test_converge_recompiles_the_stale_scope_on_the_same_routes(self) -> None:
        from charmer import transition

        record = self.store.load(self.mid)
        before = self.routes(record["applied"])
        report = transition.converge(self.store.root, self.store, self.mid,
                                     runtime_parts=self.runtime.converge_parts())
        self.assertTrue(report)
        live = self.tree(self.live(self.mid))
        agent = live[".claude/agents/cm-reviewer-strong.md"]
        self.assertIn(b"charmer-grok46-xhigh", agent)
        self.assertNotIn(legacy.LEGACY_SELECTOR_PREFIX.encode(), agent)
        self.assertEqual(self.routes(self.store.load(self.mid)["applied"]), before)

    def test_the_resume_keeps_the_bindings_under_charmers_names(self) -> None:
        record = self.store.load(self.mid)
        before = self.routes(record["applied"])
        self.write_transcript(sessions.runtime_session_id(record), str(self.project))
        prepared = self.prepare_resume(self.mid)
        self.assertEqual(prepared.diff, ())
        self.runtime.perform(prepared)
        resumed = self.store.load(self.mid)
        self.assertEqual(self.routes(resumed["applied"]), before)
        for binding in [resumed["applied"]["lead"], *resumed["applied"]["agents"].values()]:
            self.assertFalse(binding["selector"].startswith(legacy.LEGACY_SELECTOR_PREFIX), binding)
        self.assertIn("charmer-grok46-xhigh", resumed["applied"]["agents"]["cm-reviewer-strong"]["selector"])
        live = self.tree(self.live(self.mid))
        for name, data in live.items():
            with self.subTest(name=name):
                self.assertNotIn(legacy.NAME.encode(), data)
                self.assertNotIn(legacy.ENVIRONMENT_PREFIX.encode(), data)
        settings = json.loads(live["settings.json"])
        hooks = scope.hook_targets(settings)
        self.assertTrue(hooks)
        self.assertEqual({path.parent for path in hooks}, {self.layout.state / "bin"})
        _path, _argv, env = self.execs[-1]
        self.assertEqual(env.get("CHARMER_MANAGED_ID"), self.mid)
        self.assertFalse([name for name in env if name.startswith(legacy.ENVIRONMENT_PREFIX)])

    def test_back_hands_resumed_and_new_sessions_to_the_earlier_release(self) -> None:
        """A session resumed under charmer and one first launched under it:
        ``--back`` writes both records back under the earlier selectors (the
        earlier session store reads them, ``applied_hash`` included) and the
        launch-time items of both scopes in the earlier release's form (the
        resumed one byte for byte what the earlier compiler wrote, the
        record's fence proving them), and the earlier launcher's own repair
        runs once over both scopes."""

        record = self.store.load(self.mid)
        self.write_transcript(sessions.runtime_session_id(record), str(self.project))
        self.runtime.perform(self.prepare_resume(self.mid))
        new = sessions.managed_id(self.launch_fresh())
        renamed = self.runtime.catalog.renamed
        written = {mid: self.store.load(mid) for mid in (self.mid, new)}
        for mid, record in written.items():
            self.assertTrue(charmer_names(record, renamed), mid)
        complete = self.complete_renames()
        renamed = catalog.load_catalog(complete).renamed
        self.harness.environ["CHARMER_ASSETS"] = str(complete)
        report = self.harness.back()
        layout = lm.Layout.of(self.harness.environ)
        self.assertEqual(self.harness.calls.count("repair"), 1)
        self.assertEqual(sorted(self.harness.repaired), sorted(written))
        self.assertEqual(lm._charmer_scopes(layout), [])
        store = sessions.SessionStore(self.layout.legacy_state, sessions.default_schema(), read_only=True)
        # The launch-time items the earlier repair keeps (as it found them
        # when it ran) are the earlier release's own: exactly what its
        # compiler wrote for this record before the move (the fixture scope),
        # but for the launch epoch the resume under charmer advanced; and the
        # record's launch fence proves them.
        settings, lead_set = self.harness.launch_items[self.mid]
        compiled = json.loads(self.scope_before["settings.json"])
        expected = {key: compiled[key] for key in sessions.LAUNCH_TIME_SETTINGS_KEYS}
        record = store.load(self.mid)
        self.assertGreater(record["launch_epoch"], 0)
        expected["env"] = {**expected["env"], f"{legacy.ENVIRONMENT_PREFIX}LAUNCH_EPOCH": str(record["launch_epoch"])}
        self.assertEqual(settings, expected)
        self.assertEqual(lead_set, self.scope_before[scope.LEAD_SET_JSON])
        for mid in written:
            with self.subTest(mid=mid):
                settings, lead_set = self.harness.launch_items[mid]
                record = store.load(mid)
                self.assertEqual(record["launch_fence"], sessions.launch_digest(settings, lead_set))
                # The earlier doctor's own check: the fence admits every binding.
                bound = [record["applied"]["lead"]["selector"],
                         *(binding["selector"] for binding in record["applied"]["agents"].values())]
                self.assertEqual(scope.fence_gaps(settings["availableModels"], bound), ())
                self.assertFalse([key for key in settings["env"] if key.startswith(lm.CHARMER_ENV_PREFIX)])
                self.assertEqual({key for key in settings["env"] if key.startswith(legacy.ENVIRONMENT_PREFIX)},
                                 {f"{legacy.ENVIRONMENT_PREFIX}{name}" for name in
                                  ("LAUNCH_EPOCH", "MANAGED_ID", "SESSION_ID")})
                self.assertTrue(all(option["description"].startswith(f"{legacy.NAME} · ")
                                    for option in settings["modelPicker"]["options"]))
                # The repair kept them (they are the fence's), compiled the rest.
                live = json.loads((self.layout.legacy_state / "scopes" / mid / scope.SETTINGS_RELPATH).read_bytes())
                self.assertEqual({key: live[key] for key in sessions.LAUNCH_TIME_SETTINGS_KEYS}, settings)
        self.assertIn("wrote 2 session(s) charmer resumed or started back in the earlier release's names "
                      "(records and launch settings)", report.lines)
        self.assertEqual(report.lines[-1], f"{legacy.NAME} recompiled 2 session scope(s) charmer had compiled "
                                           f"({lm.REPAIR})")
        for mid, before in written.items():
            with self.subTest(mid=mid):
                after = store.load(mid)  # the store's own checks, applied_hash included
                self.assertEqual(charmer_names(after, renamed), [])
                self.assertTrue(any(value.startswith(legacy.LEGACY_SELECTOR_PREFIX) for value in lm._strings(after)))
                # Exactly charmer's record, its names read backwards.
                self.assertEqual(forward(without_digests(after), renamed), without_digests(before))
                self.assertEqual(store.read_record_bytes(mid), strict_json.canonical_file_bytes(after))
        journal = self.harness.journal()
        self.assertEqual((journal.status, sorted(journal.document["repair"]["recompiled"])),
                         (lm.REVERTED, sorted(written)))


if __name__ == "__main__":
    unittest.main()
