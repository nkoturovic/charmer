"""Gateway actions in the TUI: start or restart now, the log tail, the service.

Offered from H (the card's health report) and from the gateway-down banners
of the Providers and Direct screens (key W). Start is offered for a stopped
gateway and restart only for a gateway proven yours; anything else on the
port is shown, never acted on, and a fixture runtime (injected loopback
seams) is never observed or started. The supervised service's state is
shown, and installing (or refreshing) it is offered where it can run.
While a transaction owner holds the gateway's inhibition the view says so,
with its remedy, and offers no start, restart or service install. The
persistence hold is evaluated for a live view as ``charmer gateway status``
does: the view shows it (a coverage-only hold as such, otherwise the
failure), offers no restart while it holds and names the clear-hold route;
clearing stays a typed confirmation in a terminal outside Claude Code. A
restart first lists the managed sessions running now and asks y/N; while a
session record cannot be read it says that whether sessions run is unknown,
never that none does. A stopped gateway of an installed release whose
rollback would bring back another gateway shows that way back
(``charmer update --rollback``, a command-line action).
Stopping and removing the service stay CLI verbs (:data:`CLI_ONLY`). The log
tail opens in a ``TextView``. After an action the runtime's catalog view is
reloaded (:func:`refresh_endpoint`), so a port the action recorded is what
the calling screen re-queries and what the next launch compiles.
"""

from __future__ import annotations

import argparse
import io
import sys
from dataclasses import dataclass
from typing import Any, Callable, TextIO

from charmer import errors, gateway_hold, gateway_inhibition, gateway_lifecycle, termtext, tui
import charmer.cli.screens.common as screens_common

# Gateway operations the TUI deliberately leaves to the CLI, with the reason.
CLI_ONLY = {
    "gateway stop": "a launch never needs it and it is refused while the persistence hold is active; "
                    "run charmer gateway stop in a terminal",
    "gateway clear-hold": "it needs a typed confirmation in a terminal outside Claude Code: "
                          "charmer gateway clear-hold",
    "gateway service uninstall": "it reverses a deliberate choice and changes the service manager; run "
                                 "charmer gateway service uninstall in a terminal",
}
LOG_TAIL_LINES = 200
TITLE = "Local gateway"
HOLD_TITLE = "Persistence hold"
HOLD_COVERAGE = "held — log coverage incomplete, not a failed save"
CLEAR_ROUTE = "charmer gateway clear-hold (in a terminal outside Claude Code, with a typed confirmation)"
HOLD_COVERAGE_ROUTE = ("Restart is not offered while it holds. Keep the gateway running, check the credentials "
                       f"(charmer providers list), then clear it: {CLEAR_ROUTE}")
HOLD_FAILURE_ROUTE = (f"Restart is not offered while it holds. Fix: {gateway_hold.REMEDY} (clear-hold runs in a "
                      "terminal outside Claude Code, with a typed confirmation)")
RESTART_QUESTION = "Restart the gateway now?"
KEYBAR = (("P", "plan"), ("?", "help"), ("Esc", "close"))
HELP = ("P shows the read-only served-change plan (charmer plan); it sends no request and writes nothing. "
        "Tab chooses a gateway action; Enter opens it. Esc closes this dialog. Stop, clear-hold and service "
        "uninstall stay terminal commands.")


@dataclass(frozen=True)
class GatewayView:
    gateway: Any | None
    seen: gateway_lifecycle.Observation | None  # None: not observed (fixture or error)
    error: str | None = None
    service: Any | None = None  # gateway_service.GatewayService (live views only)
    service_status: Any | None = None
    runtime: Any | None = None  # the runtime the view was observed for (refreshed after an action)
    inhibition: str | None = None  # the inhibited text (with its remedy) while one is recorded
    hold: gateway_hold.HoldReport | None = None  # the persistence hold (live views only)
    # The way back after a gateway-changing update, while the gateway is
    # stopped (``gateway_lifecycle.rollback_remedy``); rollback itself
    # stays a command-line action, so the view shows its command.
    rollback: str | None = None
    drift: str | None = None  # on-disk binary comparison, separate from ownership

    @property
    def can_install_service(self) -> bool:
        status = self.service_status
        return (self.seen is not None and status is not None and status.supported is None and self.inhibition is None
                and not status.foreign and (not status.installed or bool(status.stale)))

    def service_line(self) -> str | None:
        status = self.service_status
        if status is None:
            return None
        if status.installed:
            return f"service: installed ({status.name})" + (" — stale, install refreshes it" if status.stale else "")
        if status.supported is not None:
            return "service: not available here (on-demand only)"
        return "service: not installed (the gateway starts on demand)"

    @property
    def can_start(self) -> bool:
        return self.seen is not None and self.seen.state == gateway_lifecycle.STOPPED and self.inhibition is None

    @property
    def held(self) -> bool:
        return self.hold is not None and self.hold.held

    @property
    def can_restart(self) -> bool:
        return (self.seen is not None and self.seen.state == gateway_lifecycle.OURS and self.inhibition is None
                and not self.held)

    def hold_summary(self) -> list[str]:
        """The hold in a line or two: its state, then (held) the clear-hold route."""

        hold = self.hold
        if hold is None:
            return []
        if not hold.held:
            return ["persistence hold: none" + (f" — {hold.evaluated}" if hold.evaluated else "")]
        if hold.coverage_only:
            return [f"persistence hold: {HOLD_COVERAGE}", HOLD_COVERAGE_ROUTE]
        first = hold.reasons[0] if hold.reasons else "the persistence hold"
        more = f" (and {len(hold.reasons) - 1} more)" if len(hold.reasons) > 1 else ""
        return [f"persistence hold: held — {first}{more}", HOLD_FAILURE_ROUTE]

    def hold_lines(self) -> list[str]:
        """The hold in full: every reason, then the route (the details view, H)."""

        hold = self.hold
        if hold is None or not hold.held:
            return self.hold_summary()
        head, route = self.hold_summary()
        return [head, *(f"  {reason}" for reason in hold.reasons), route]

    def headline(self) -> str:
        if self.error is not None:
            return f"Gateway: {self.error}"
        if self.seen is None:
            return "Gateway: not observed here"
        return f"Gateway: {self.seen.state} — {self.seen.detail}"


def observe(runtime: Any) -> GatewayView:
    """One credential-free observation of the runtime's gateway."""

    try:
        gateway = runtime.gateway()
    except (errors.CharmerError, OSError) as exc:
        return GatewayView(None, None, termtext.visible_message(exc), runtime=runtime)
    live = runtime.gateway_seams is not None or (
        runtime.health_get is None and runtime.served_models_callback is None)
    if not live:
        return GatewayView(gateway, None, runtime=runtime)
    manager = status = None
    try:
        manager = runtime.gateway_service()
        status = manager.status()
    except (errors.CharmerError, OSError):
        manager = status = None
    seen = gateway.observe()
    return GatewayView(gateway, seen, service=manager, service_status=status, runtime=runtime,
                       inhibition=_inhibition_text(gateway), hold=_hold(gateway, seen),
                       rollback=_rollback(gateway) if seen.state == gateway_lifecycle.STOPPED else None,
                       drift=gateway.binary_drift(seen))


def _rollback(gateway: Any) -> str | None:
    try:
        return gateway.rollback_remedy()
    except (OSError, errors.CharmerError):
        return None


def observe_hold(runtime: Any) -> gateway_hold.HoldReport | None:
    """The persistence hold alone, as :func:`observe` evaluates it, for the
    card's health check (a live runtime only; None when it is not observed
    here)."""

    live = runtime.gateway_seams is not None or (
        runtime.health_get is None and runtime.served_models_callback is None)
    if not live:
        return None
    try:
        gateway = runtime.gateway()
        seen = gateway.observe()
    except (errors.CharmerError, OSError):
        return None
    return _hold(gateway, seen)


def _hold(gateway: Any, seen: gateway_lifecycle.Observation) -> gateway_hold.HoldReport:
    """The persistence hold as ``charmer gateway status`` evaluates it (an
    evaluation that fails holds)."""

    try:
        return gateway.persistence_hold(seen)
    except (OSError, errors.CharmerError) as exc:
        return gateway_hold.HoldReport(True, (f"the hold cannot be evaluated: {termtext.visible_message(exc)}",),
                                       "unknown")


def running_sessions(runtime: Any) -> tuple[list[str], str | None]:
    """The managed sessions that run now (labels), and why it cannot be
    told whether others run too (None when it can): a daemon pty socket or a
    ``/proc`` argv names them, as the Sessions screen's ● does. A record
    that cannot be read leaves it unknown; the readable live ones are still
    listed."""

    if runtime is None:
        return [], None
    from charmer import sessions
    import charmer.cli.session_facts as session_facts

    try:
        records, problems, _ids = session_facts._session_record_scan(runtime)
        prefixes, proc_ids = runtime._live_prefixes(), runtime._proc_ids()
    except (errors.CharmerError, OSError) as exc:
        return [], termtext.visible_message(exc)
    labels = []
    for record in records:
        if not session_facts._record_view_is_live(record, prefixes=prefixes, proc_ids=proc_ids):
            continue
        try:
            label = f"{sessions.managed_id(record)} ({session_facts._record_target_label(record)})"
        except (KeyError, TypeError, errors.CharmerError):
            label = str(record.get("managed_id") or record.get("runtime_session_id") or "a session")
        labels.append(termtext.visible_text(label))
    unknown = None
    if problems:
        unknown = (f"{len(problems)} session record(s) cannot be read" if len(problems) > 1
                   else termtext.visible_text(problems[0]))
    return labels, unknown


def confirm_restart(view: GatewayView, tty_in: TextIO, tty_out: TextIO) -> bool:
    """List the managed sessions running now, then ask y/N (default No).
    When the records cannot all be read it says so (never "no session"),
    after the live ones it can name."""

    labels, unknown = running_sessions(view.runtime)
    if labels:
        tty_out.write(f"{len(labels)} managed session(s) run now; a restart fails their requests in flight:\n")
        for label in labels:
            tty_out.write(f"  - {label}\n")
    if unknown is not None:
        more = "other " if labels else ""
        tty_out.write(f"Whether {more}managed sessions run now cannot be told ({unknown}).\n")
    elif not labels:
        tty_out.write("No managed session runs now.\n")
    tty_out.write(f"{RESTART_QUESTION} [y/N] ")
    tty_out.flush()
    try:
        answer = tty_in.readline()
    except KeyboardInterrupt:
        answer = ""
    if not answer.endswith("\n"):
        tty_out.write("\n")
    return answer.strip().lower() in ("y", "yes")


def _inhibition_text(gateway: Any) -> str | None:
    record = gateway_inhibition.read(gateway.state_root)
    if record is None:
        return None
    message, remedy = gateway_inhibition.refusal(record, "start, restart and service changes wait for it")
    return f"{message} — fix: {remedy}"


def refresh_endpoint(runtime: Any | None) -> None:
    """Reload the runtime's catalog view after a gateway action (a start may
    have recorded a new install's port, a service install its endpoint)."""

    if runtime is None:
        return
    try:
        runtime.reload_catalog()
    except (errors.CharmerError, OSError):
        pass  # the previous view stays; the next screen load reports the problem


def act(view: GatewayView, action: str) -> gateway_lifecycle.Outcome:
    """Run ``start`` (stopped only), ``restart`` (proven ours, not held; the
    restart itself evaluates the hold again) or the service install, then
    refresh the runtime's endpoint view."""

    runtime = view.runtime
    if runtime is not None and not getattr(runtime, "allow_state_writes", True):
        return gateway_lifecycle.Outcome("refused", "this run is read-only; nothing was done",
                                         "run charmer from a terminal to act on the gateway")
    if action == "start" and view.can_start:
        outcome = view.gateway.ensure(explicit=True, choose_port=True)
    elif action == "restart" and view.can_restart:
        outcome = view.gateway.restart()
    elif action == "service" and view.can_install_service:
        outcome = view.service.install()
    else:
        return gateway_lifecycle.Outcome("refused", f"{view.headline()}; nothing was done",
                                         "check it with: charmer gateway status")
    refresh_endpoint(runtime)
    return outcome


def log_view(view: GatewayView) -> tuple[str, list[str]]:
    """``(title, lines)`` of the newest log tail (or why there is none)."""

    if view.gateway is None:
        return "gateway log", [view.headline()]
    try:
        lines = view.gateway.logs(lines=LOG_TAIL_LINES)
    except (gateway_lifecycle.LifecycleError, errors.CharmerError, OSError) as exc:
        return "gateway log", [termtext.visible_message(exc)]
    return f"gateway log — last {LOG_TAIL_LINES} lines", [termtext.visible_text(line) for line in lines] or ["(empty)"]


_ACTION_TEXT = {"start": "Starting the gateway", "restart": "Restarting the gateway",
                "service": "Installing the gateway service"}


def _run_suspended(win: Any, view: GatewayView, action: str, tty_in: TextIO, tty_out: TextIO) -> str:
    with tui.suspended_curses(win):
        if action == "restart" and not confirm_restart(view, tty_in, tty_out):
            tty_out.write("Not restarted; nothing was done.\n\nPress Enter to return to charmer.")
            tty_out.flush()
            try:
                tty_in.readline()
            except KeyboardInterrupt:
                pass
            return "gateway not restarted"
        tty_out.write(f"{_ACTION_TEXT[action]} (at most {gateway_lifecycle.START_READINESS_TIMEOUT:g} s)…\n")
        tty_out.flush()
        outcome = act(view, action)
        for line in outcome.lines():
            tty_out.write(termtext.visible_text(line) + "\n")
        tty_out.write("\nPress Enter to return to charmer.")
        tty_out.flush()
        try:
            tty_in.readline()
        except KeyboardInterrupt:
            pass
    return outcome.message


def dialog(runtime: Any, win: Any, palette: tui.Palette, *, background: Callable[[Any], None] | None = None,
           tty_in: TextIO | None = None, tty_out: TextIO | None = None) -> str:
    """The gateway dialog; returns a one-line status for the calling screen."""

    view = observe(runtime)
    buttons: list[tuple[str, str | None]] = []
    if view.can_start:
        buttons.append(("Start now", "start"))
    if view.can_restart:
        buttons.append(("Restart now", "restart"))
    if view.can_install_service:
        buttons.append(("Install service", "service"))
    if view.held:
        buttons.append(("Hold details", "hold"))
    buttons += [("Show log", "log"), ("Close", None)]
    lines = [view.headline()]
    if view.gateway is not None:
        lines.append(f"endpoint {view.gateway.base_url} · backend {view.gateway.backend}")
    if view.service_line() is not None:
        lines.append(view.service_line())
    if view.inhibition is not None:
        lines.append(view.inhibition)
    drift = getattr(view, "drift", None)
    if drift is not None:
        lines.append(drift)
    lines.extend(view.hold_summary())
    if view.rollback is not None:
        lines.append(view.rollback)
    if view.seen is not None and view.seen.state in (gateway_lifecycle.OURS, gateway_lifecycle.STARTING):
        lines.append("Stop it from a terminal: charmer gateway stop.")
    # Wrapped to the box, so no remedy is cut off (the inhibition's above all).
    body = [piece for line in lines for piece in screens_common.modal_lines(line, win)]
    while True:
        choice = tui.Modal(TITLE, body, buttons=tuple(buttons), shortcuts={"P": "plan", "?": "help"},
                           keybar=KEYBAR).run(win, palette, background=background)
        if choice != "help":
            break
        tui.TextView("Gateway — help", [HELP], palette=palette).run(win)
        if background is not None:
            background(win)
        else:
            win.erase()
    if choice == "plan":
        from charmer.cli.commands import plan

        output = io.StringIO()
        try:
            plan.command(runtime, argparse.Namespace(), output)
        except (errors.CharmerError, OSError) as exc:
            output.write(termtext.visible_message(exc) + "\n")
        tui.TextView("Served-change plan", output.getvalue().splitlines(), palette=palette).run(win)
        return ""
    if choice == "log":
        title, text = log_view(view)
        tui.TextView(title, text, palette=palette).run(win)
        return ""
    if choice == "hold":
        tui.TextView(HOLD_TITLE, [termtext.visible_text(line) for line in view.hold_lines()], palette=palette).run(win)
        return ""
    if choice in ("start", "restart", "service"):
        return _run_suspended(win, view, choice, tty_in or sys.stdin, tty_out or sys.stdout)
    return ""
