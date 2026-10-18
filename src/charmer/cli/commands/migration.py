"""The ``migrate`` and ``restore-2x`` commands."""

from __future__ import annotations

from charmer import migrate as migrate_mod
from charmer import paths as product_paths
from charmer import profile as profile_mod
from charmer import scope as scope_mod
from charmer import sessions
from pathlib import Path
from typing import Any
from typing import Callable
from typing import Mapping
from typing import TextIO
import argparse
import json
import os
import sys
import charmer.cli.resume_checks as resume_checks
import charmer.cli.session_facts as session_facts
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import charmer.cli.runtime as runtime_mod


def _restore_2x(
    state_path: Path,
    output_stream: TextIO,
    args: argparse.Namespace | None = None,
    *,
    input_stream: TextIO | None = None,
    interactive: bool | None = None,
    error_stream: TextIO | None = None,
    proc_root: Path | str = "/proc",
    environ: Mapping[str, str] | None = None,
) -> int:
    """``charmer restore-2x`` (dispatched before ``main``'s marker gate).

    Marker > 4, malformed or unsafe -> the marker error, exit 2, nothing
    touched (sc[15]); missing (3) -> nothing to restore, exit 0 — both before
    any store directory is created. Marker 4 -> ``migrate.restore_2x`` with a
    bare ``SessionStore`` (no Runtime, no shim refresh, no catalog). A
    refusal is exit 2 with nothing changed; on a terminal the live/unknown
    records are listed with a y/N (yes records their end).

    A narrow exception: when the caller's own
    ``CHARMER_MANAGED_ID`` is listed in ``--assume-dead``, that record
    alone is checked against the ``/proc`` argvs of processes that are not
    the caller or its ancestors (its own client names it). The daemon ●
    check and ``--not-running`` are never overridden. ``proc_root`` and
    ``environ`` (default ``os.environ``) are the test seams.
    """

    errors = error_stream or sys.stderr
    if getattr(args, "check", False):
        if getattr(args, "not_running", None) or getattr(args, "assume_dead", None):
            errors.write("charmer: --check cannot record liveness overrides\n")
            return 2
        env = dict(os.environ if environ is None else environ)
        report = migrate_mod.restore_check(state_path, product_paths.config_root(env))
        if getattr(args, "json_output", False):
            output_stream.write(json.dumps(report, sort_keys=True) + "\n")
        else:
            output_stream.write(migrate_mod.RESTORE_CHECK_CAVEAT + "\n")
            output_stream.write(f"State root: {state_path}\nMetadata: {report['status']}\n")
            for item in report["checks"]:
                output_stream.write(f"  {item['path']}: {item['status']}\n")
        return 1 if report["status"] == "refused" else 0
    if getattr(args, "json_output", False):
        errors.write("charmer: --json requires --check\n")
        return 2
    try:
        version = sessions.check_state_marker(state_path)
    except sessions.StateMarkerError as exc:
        errors.write(f"charmer: {exc}\n")
        return 1
    if version <= sessions.UNMARKED_STATE_VERSION:
        output_stream.write(
            "No current-format state exists here; nothing to restore.\n"
        )
        return 0
    not_running = list(getattr(args, "not_running", None) or [])
    assume_dead = list(getattr(args, "assume_dead", None) or [])
    for value in not_running + assume_dead:
        if not sessions.UUID4.fullmatch(value):
            errors.write(f"charmer: restore-2x: {value!r} is not a managed session UUID\n")
            return 2
    store = sessions.SessionStore(state_path, sessions.default_schema())
    self_id = (os.environ if environ is None else environ).get("CHARMER_MANAGED_ID")
    override = self_id if self_id in assume_dead else None

    def liveness() -> Callable[[dict[str, Any]], bool]:
        """A fresh daemon + ``/proc`` inventory; RestoreRefused when unknown.
        Read for the preview and again inside the commit phase."""

        daemon = sessions.background_liveness()
        if not daemon.known:
            raise migrate_mod.RestoreRefused([
                "charmer: restore-2x: cannot tell which sessions are live in the background "
                f"({daemon.reason}); nothing restored"
            ])
        prefixes = daemon.prefixes
        scan = sessions.proc_session_scan(proc_root)
        foreign_scan = scan
        if override is not None and scan.known:
            foreign_scan = sessions.proc_session_scan(
                proc_root, exclude_pids=sessions.ancestor_pids(proc_root)
            )
        if not (scan.known and foreign_scan.known):
            reason = scan.reason if not scan.known else foreign_scan.reason
            raise migrate_mod.RestoreRefused([
                f"charmer: restore-2x: cannot tell which sessions are running (the process "
                f"table is unreadable: {reason}); nothing restored"
            ])
        proc_ids, foreign = scan.ids, foreign_scan.ids

        def is_live(view: dict[str, Any]) -> bool:
            own = override is not None and (
                view.get("managed_id", view.get("session_id")) == override
            )
            return session_facts._record_view_is_live(
                view, prefixes=prefixes, proc_ids=foreign if own else proc_ids
            )

        return is_live

    try:
        is_live = liveness()
    except migrate_mod.RestoreRefused as exc:
        for line in exc.lines:
            errors.write(f"{line}\n")
        return 1

    confirm: Callable[[Any], bool] | None = None
    declined: list[bool] = []
    if interactive:
        reader = input_stream or sys.stdin

        def ask_restore(pairs: Any) -> bool:
            output_stream.write(
                f"{len(pairs)} session(s) have no recorded end (live or unknown):\n"
            )
            for managed, source in pairs:
                output_stream.write(f"  {managed}  last event {source or 'none'}\n")
            output_stream.write(
                "Treat them as exited and record their end, then restore? [y/N] "
            )
            output_stream.flush()
            answer = reader.readline()
            output_stream.write("\n" if not answer.endswith("\n") else "")
            agreed = answer.strip().lower() in ("y", "yes")
            if not agreed:
                declined.append(True)
            return agreed

        confirm = ask_restore

    try:
        report = migrate_mod.restore_2x(
            store,
            # The commit phase takes the gateway served-change
            # barrier, which is HOME-relative like the api-key leaf.
            home=product_paths.home(os.environ if environ is None else environ),
            not_running=not_running,
            assume_dead=assume_dead,
            is_live=is_live,
            confirm=confirm,
            refresh_live=liveness,
        )
    except migrate_mod.RestoreRefused as exc:
        for line in exc.lines:
            errors.write(line if line.startswith("charmer: ") or line.startswith("  ")
                         else f"charmer: {line}")
            errors.write("\n")
        return 3 if declined else 1
    except migrate_mod.RestoreStopped as exc:
        output_stream.write(migrate_mod.render_restore(exc.done))
        errors.write(f"charmer: {exc}\n")
        return 1
    output_stream.write(migrate_mod.render_restore(report))
    return 0


def _migrate_command(
    runtime: runtime_mod.Runtime, args: argparse.Namespace, output_stream: TextIO
) -> int:
    """``charmer migrate [--dry-run] [--json]``."""

    store = runtime.session_store
    lcat = runtime.lineup_catalog()
    profiles = profile_mod.ProfileStore.for_catalog(runtime.catalog, runtime.environ)
    if args.dry_run:
        user_settings = _user_claude_settings(runtime)
        report = migrate_mod.plan(
            store,
            lcat,
            profile_exists=profiles.contains,
            transcript_check=lambda view: _transcript_presence(runtime, view),
            cleanup_days=migrate_mod.cleanup_period_days(user_settings),
        )
    else:
        report = migrate_mod.run(
            store,
            lcat,
            hook_command=runtime.resolved_hook_command,
            hook_shim_path=scope_mod.hook_shim_path(store.root),
            profile_exists=profiles.contains,
            home=runtime.home,
            barrier_timeout=runtime.served_barrier_timeout,
        )
    if args.json_output:
        output_stream.write(migrate_mod.render_json(report).decode("utf-8"))
    else:
        output_stream.write(migrate_mod.render_text(report))
    output_stream.flush()
    return 0 if report.ok else 1


def _user_claude_settings(runtime: runtime_mod.Runtime) -> Any:
    """The managed client's ``~/.claude/settings.json`` (≤ 1 MiB; tolerant).

    Never ``$CLAUDE_CONFIG_DIR``: managed sessions run without it, so their
    client applies HOME's ``cleanupPeriodDays``."""

    base = product_paths.claude_settings_dir(runtime.environ)
    path = base / "settings.json"
    try:
        with open(path, "rb") as handle:
            raw = handle.read((1 << 20) + 1)
        if len(raw) > (1 << 20):
            return None
        return json.loads(raw)
    except (OSError, ValueError):
        return None


def _transcript_presence(runtime: runtime_mod.Runtime, view: dict[str, Any]) -> tuple[str, float | None]:
    """The transcript's existence and mtime by path only."""

    try:
        status, detail, _decoded = resume_checks._resume_transcript_status(runtime, view)
    except (KeyError, OSError, sessions.SessionError):
        return ("elsewhere", None)
    if status != "present":
        return (status, None)
    try:
        return ("present", Path(detail).stat().st_mtime)
    except OSError:
        return ("missing", None)


# ------------------------------------------------------------ migrate from-legacy

def _legacy_mode(args: argparse.Namespace) -> str:
    if getattr(args, "legacy_status", False):
        return "status"
    if getattr(args, "legacy_back", False):
        return "back"
    if getattr(args, "legacy_purge", False):
        return "purge"
    if getattr(args, "legacy_dry_run", False) or getattr(args, "dry_run", False):
        return "dry-run"
    return "run"


def _migrate_from_legacy(
    args: argparse.Namespace,
    output_stream: TextIO,
    *,
    input_stream: TextIO | None = None,
    error_stream: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
    seams: Any = None,
) -> int:
    """``charmer migrate from-legacy [--dry-run | --status | --back | --purge]``
    (dispatched before any Runtime or session store: it moves the roots
    those would create). ``seams`` (a ``legacy_migration.Seams``) replaces
    the live effects in tests."""

    from charmer import legacy_migration as moving
    import charmer.cli.consent as consent

    errors_out = error_stream or sys.stderr
    env = dict(os.environ if environ is None else environ)
    if getattr(args, "json_output", False):
        errors_out.write("charmer: --json does not apply to migrate from-legacy\n")
        return 2
    mode = _legacy_mode(args)
    try:
        if mode == "status":
            for line in moving.status_lines(env):
                output_stream.write(f"{line}\n")
            return 0
        live = seams if seams is not None else _live_seams(env)
        if mode == "dry-run":
            lines, ready = moving.dry_run(env, live)
            for line in lines:
                output_stream.write(f"{line}\n")
            output_stream.write(("Ready: " + moving.COMMAND if ready
                                 else "Not ready: fix what the preconditions name, then run this again") + "\n")
            return 0 if ready else 1
        consent.require_human(moving.COMMAND + ("" if mode == "run" else f" --{mode}"), env)
        if mode == "purge":
            layout = moving.Layout.of(env)
            targets = moving.purge_targets(layout)
            if not targets:
                output_stream.write("Nothing of the earlier installation is left to remove.\n")
                return 0
            # Refused before the question, and again under the lock.
            problems = moving.purge_problems(layout, targets)
            if problems:
                raise moving.purge_refusal(problems, layout)
            output_stream.write("This removes, for good (no --back afterwards):\n")
            for line in moving.purge_listing(layout, targets):
                output_stream.write(f"  {line}\n")
            output_stream.flush()
            if not consent.confirm("Remove them? [y/N] ", input_stream=input_stream):
                output_stream.write("Nothing was removed.\n")
                return 3
            report = moving.purge(env, live, confirmed=[target.path for target in targets])
        elif mode == "back":
            report = moving.back(env, live)
        else:
            report = moving.migrate(env, live)
    except moving.MigrationError as exc:
        errors_out.write(f"charmer: {exc}\n")
        for line in exc.lines[1:]:
            errors_out.write(f"  also: {line}\n")
        if exc.remedy:
            errors_out.write(f"  fix: {exc.remedy}\n")
        return 1
    for line in report.lines:
        output_stream.write(f"{line}\n")
    if mode == "run":
        output_stream.write(f"Done. The earlier installation is kept for {moving.BACK}; once you are content, "
                            f"{moving.PURGE} removes it.\n")
    elif mode == "back":
        output_stream.write("Done. The earlier installation works as before; charmer stays installed.\n")
    output_stream.flush()
    return 0


def _live_seams(environ: Mapping[str, str]) -> Any:
    """The live effects of ``migrate from-legacy`` (``legacy_migration.Seams``)."""

    from charmer import legacy_migration as moving

    return moving.Seams(installed=_installed_problem, liveness=_live_sessions, hold=_legacy_hold,
                        shims=_write_shims, gateway=_start_gateway, verify=_verify, repair=_repair_earlier,
                        charmer_hold=_charmer_hold, stop_gateway=_stop_gateway)


def _installed_problem(environ: Mapping[str, str]) -> str | None:
    from charmer import endpoint, installs

    if endpoint.channel(environ) != "bundle":
        return "this charmer does not run from an installed release bundle"
    if not os.path.lexists(installs.bundle_link(environ)):
        return f"no installed release was found ({product_paths.display(installs.bundle_link(environ), environ)})"
    return None


def _live_sessions(root: Path) -> list[str]:
    """The managed sessions under ``root`` that run now, by the daemon and
    ``/proc`` inventories (unknown refuses), as ``restore-2x`` tells them."""

    from charmer import legacy_migration as moving

    daemon = sessions.background_liveness()
    if not daemon.known:
        raise moving.LivenessUnknown(f"the background daemon cannot be read: {daemon.reason}")
    scan = sessions.proc_session_scan("/proc")
    if not scan.known:
        raise moving.LivenessUnknown(f"the process table is unreadable: {scan.reason}")
    store = sessions.SessionStore(root, sessions.default_schema(), read_only=True)
    live = []
    for path in store.scan_uuid_records():
        try:
            view = store.load(path.stem)
        except (sessions.SessionError, OSError, ValueError) as exc:
            raise moving.LivenessUnknown(f"the session record {path.name} cannot be read ({exc})") from exc
        if session_facts._record_view_is_live(view, prefixes=daemon.prefixes, proc_ids=scan.ids):
            live.append(path.stem)
    return live


def _legacy_hold(layout: Any) -> list[str]:
    """The earlier gateway's persistence hold, as its own stop would see it:
    the recorded holds, then the earlier unit's journal read from the
    running instance's start (its exec stamp) and searched for the earlier
    start line, or its instance logs when it runs on demand. Nothing is
    recorded: ``persistence-hold.json`` keeps its bytes."""

    from charmer import legacy

    unit = dict(layout.legacy_units())[legacy.LEGACY_UNIT]
    return _gateway_hold(layout.legacy_state, legacy.LEGACY_UNIT, os.path.lexists(unit), legacy.legacy_start_marker)


def _charmer_hold(layout: Any) -> list[str]:
    """charmer's own gateway's persistence hold on charmer's state root, as
    its own stop would see it: the recorded holds, then (supervised:
    ``endpoint.json`` says so or a charmer unit file exists) its unit's
    journal from the running instance's start, searched for charmer's start
    line, or its instance logs (on demand). Evidence that cannot be read
    holds. Nothing is recorded."""

    from charmer import endpoint, service
    from charmer import legacy_migration as moving

    document = moving._endpoint_document(layout.gateway) or {}
    supervised = document.get("backend") == endpoint.SYSTEMD or any(
        os.path.lexists(unit) for _name, unit in layout.charmer_units())
    unit = document.get("unit") if isinstance(document.get("unit"), str) else moving.CHARMER_UNIT
    return _gateway_hold(layout.state, unit, supervised, service.start_marker_prefix)


def _gateway_hold(state_root: Path, unit: str, supervised: bool, marker: Callable[[str], str]) -> list[str]:
    """The persistence hold's reasons for the gateway of ``state_root``
    (empty: it may stop), never recorded. A supervised gateway's journal is
    read over the window its own stop reads: from the running instance's
    start (its exec stamp) when the unit is active or the instance lock is
    held. Evidence that cannot be told (the unit's state unreadable, or a
    running instance without a readable start) holds."""

    import datetime
    import subprocess

    from charmer import assets, catalog, gateway_hold, service
    from charmer.platform import linux_service, posix_fs

    try:
        document = gateway_hold.read_document(state_root)
    except gateway_hold.HoldError as exc:
        return [str(exc)]
    reasons = [str(item.get("reason") or "a recorded hold") for item in document["holds"]]
    if reasons:
        return reasons
    providers = gateway_hold.credential_save_providers(
        catalog.load_catalog(assets.default_asset_root()).docs["providers"])
    if not supervised:
        report = gateway_hold.evaluate_files(state_root, providers)
        return list(report.reasons) if report.held else []
    properties = linux_service.unit_properties(unit, subprocess.run)
    if properties is None:
        return [f"the service manager's state of {unit}.service cannot be read, so the running instance's "
                "credential saves cannot be evaluated"]
    workdir = service.gateway_workdir(state_root)
    running = None
    if properties.get("ActiveState") in ("active", "activating", "reloading") \
            or posix_fs.lock_held(workdir / service.INSTANCE_LOCK) is not False:
        stamp = service.read_exec_stamp(workdir)
        started = None
        if stamp is not None:
            try:
                started = datetime.datetime.fromisoformat(stamp.started_at)
            except ValueError:
                started = None
        if stamp is None or started is None or started.tzinfo is None:
            return [f"{unit}.service runs, but its running instance's start cannot be read, so its credential "
                    "saves cannot be evaluated"]
        running = (stamp.instance, started)
    reader, reaches, drops = gateway_hold.journal_sources(
        unit, marker(running[0]) if running is not None and running[0] else None)
    report = gateway_hold.evaluate_journal(
        state_root, providers, running=running, marker=marker, record=False,
        reader=reader, reaches=reaches, drops=drops)
    return list(report.reasons) if report.held else []


def _stop_gateway(environ: Mapping[str, str]) -> tuple[bool, str]:
    """charmer's own gateway stop under this run's inhibition: its
    persistence hold again, then a stop proven for its backend (the
    service's stop and the process's exit, or the on-demand process's
    exit). ``(ok, message)``; a stop that cannot be made or told is not ok."""

    from charmer import errors as product_errors
    from charmer import gateway_inhibition

    try:
        runtime = _runtime(environ, refresh_shims=False, initialize_session_store=False)
        gateway = runtime.gateway()
        gateway.inhibition_token = environ.get(gateway_inhibition.TOKEN_ENV) or None
        outcome = gateway.stop()
    except (product_errors.CharmerError, OSError) as exc:
        return False, str(exc)
    return outcome.ok, outcome.message + (f" (fix: {outcome.remedy})" if outcome.remedy else "")


# The earlier repair's ceiling (it converges every session scope once).
REPAIR_TIMEOUT = 900


def _repair_earlier(environ: Mapping[str, str]) -> tuple[bool, list[str]]:
    """The retained earlier launcher's own ``doctor --repair-all
    --include-live``, from its install tree, without this run's inhibition
    token and without a terminal (it never asks): ``(ok, its output lines)``.
    The one call ``--back`` makes to the earlier release."""

    import subprocess

    from charmer import gateway_inhibition, legacy
    from charmer import legacy_migration as moving

    launcher = legacy.data_root(environ) / legacy.LEGACY_INSTALL_DIR / moving.EARLIER_LAUNCHER
    shown = product_paths.display(launcher, environ)
    if not os.access(launcher, os.X_OK):
        return False, [f"{shown} is missing or not executable"]
    env = {name: value for name, value in environ.items() if name != gateway_inhibition.TOKEN_ENV}
    try:
        done = subprocess.run([str(launcher), *moving.REPAIR_ARGS], env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=REPAIR_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, [f"{shown} did not finish ({exc})"]
    output = [line for line in (done.stdout + done.stderr).splitlines() if line.strip()]
    return done.returncode == 0, output


def _runtime(environ: Mapping[str, str], **kwargs: Any) -> Any:
    from charmer import assets
    import charmer.cli.runtime as runtime_mod

    return runtime_mod.Runtime(asset_root=assets.default_asset_root(), environ=dict(environ), **kwargs)


def _write_shims(environ: Mapping[str, str]) -> None:
    runtime = _runtime(environ, refresh_shims=True, initialize_session_store=True)
    if runtime.shims_withheld is not None:
        from charmer import legacy_migration as moving

        raise moving.MigrationError(f"charmer's shims were not written ({runtime.shims_withheld})",
                                    remedy=f"{moving.COMMAND} again")


def _start_gateway(environ: Mapping[str, str]) -> tuple[bool, str]:
    """``gateway service install`` as ``charmer-gateway`` under this run's
    inhibition, then the unit enabled and started, and the gateway proven
    ready on the recorded endpoint."""

    from charmer import gateway_inhibition
    from charmer import legacy_migration as moving
    from charmer.platform import linux_service

    token = environ.get(gateway_inhibition.TOKEN_ENV) or None
    runtime = _runtime(environ, refresh_shims=False, initialize_session_store=False)
    manager = runtime.gateway_service()
    manager.gateway.inhibition_token = token
    outcome = manager.install(moving.CHARMER_UNIT)
    if not outcome.ok:
        return False, outcome.message + (f" (fix: {outcome.remedy})" if outcome.remedy else "")
    if not linux_service.enable_now(moving.CHARMER_UNIT, manager.seams.runner):
        return False, f"the service manager did not enable and start {moving.CHARMER_UNIT}"
    gateway = runtime.gateway()
    gateway.inhibition_token = token
    ready = gateway.ensure(destination=gateway.base_url)
    return ready.ok, ready.message


def _verify(environ: Mapping[str, str], since: Any) -> list[str]:
    """``gateway ensure`` on the recorded loopback endpoint, the proof that
    the gateway's configuration and its preparation receipt were rendered
    again by charmer since ``since`` (the move's start: neither is ever
    edited, the gateway's own preparation writes both), and a doctor pass:
    the BLOCK lines (none: verified)."""

    import datetime

    from charmer import proxy
    import charmer.cli.doctor as doctor

    runtime = _runtime(environ, initialize_session_store=False)
    gateway = runtime.gateway()
    ensured = gateway.ensure(destination=gateway.base_url)
    problems = [] if ensured.ok else [f"gateway ensure: {ensured.message}"]
    directory = product_paths.gateway_config_dir(environ)
    for name in ("config.yaml", proxy.PREPARED_STAMP):
        try:
            written = datetime.datetime.fromtimestamp(os.stat(directory / name).st_mtime, datetime.timezone.utc)
        except OSError:
            written = None
        if written is None or written < since:
            problems.append(f"{product_paths.display(directory / name, environ)} was not rendered again by the "
                            "gateway's preparation; restart it: charmer gateway restart")
    blocks, _info, _attention = doctor._collect_doctor_reports(runtime)
    return problems + [str(line) for line in blocks]
