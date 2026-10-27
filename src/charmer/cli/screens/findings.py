"""H: doctor's findings as a list, and the fixed set of actions on them.

The rows are ``doctor.present_report``'s, the ones plain ``charmer doctor``
prints: BLOCKED, then Attention under area headers (a held gateway first),
then the advisories (optional evidence), collapsed behind their header
until Enter opens it. Each row wraps in full (no command in it is cut
off); what Enter does on the selected one shows under the list, with how
many findings a collapsed row stands for. ``V`` is the full report as
``doctor -v`` prints it, ``W`` the gateway's own actions
(``gateway_actions.dialog``), Esc back.

Enter acts only on the rows a collector tagged with one of
``doctor.ACTIONS``, each through the service and guards its command uses:
the persistence hold shows why it holds and the clear-hold command (clearing
stays a typed confirmation outside Claude Code); a missing key opens the key
entry of Providers (G → K); scopes to repair offer stopped-session repair-all
or a separately confirmed live-inclusive pass; an unused alias is pruned;
generated leftovers get the prune preview;
sessions without a recorded end that are not running are marked ended; a
newer shipped profile is reseeded; an advisory model gets its optional
diagnostics (M → Q); sessions that need a lead open Sessions. Each one asks
first (Cancel focused, or the preview's y/N) and refreshes the report after.
Any other row shows its exact command. Finding text is data: it is drawn,
never parsed or run. Opening the list runs doctor's local checks only.
A separate explicit verification row offers offline signed metadata or an
online fetch confirmed before it starts; it never refreshes the doctor report.
"""

from __future__ import annotations

import io
import textwrap
from dataclasses import dataclass
from typing import Any, Callable, Sequence, TextIO, TYPE_CHECKING

if TYPE_CHECKING:
    from charmer.cli.screens.launch_sessions import _WayBack

from charmer import errors, termtext, tui, views
import charmer.cli.doctor as doctor
import charmer.cli.doctor_actions as doctor_actions
import charmer.cli.screens.common as screens_common
import charmer.cli.screens.gateway_actions as screens_gateway
import charmer.cli.text as cli_text

_MARKS = {"block": "✗", "attention": "!", doctor.ADVISORY: "·"}
_ROLES = {"block": "error", "attention": "warn", doctor.ADVISORY: "dim"}
# Under the list: what the selected row stands for (the advisories' note takes two).
_INFO_LINES = 2


@dataclass(frozen=True)
class Item:
    """One line of the list: a section or area header, the advisories'
    header (``toggle``), a finding, or the empty report's line."""

    kind: str  # section | area | toggle | finding | none | verify
    text: str
    role: str = "normal"
    row: doctor.Row | None = None

    @property
    def selectable(self) -> bool:
        return self.kind in ("finding", "toggle", "verify")


def items(presentation: doctor.Presentation, *, expanded: bool,
          say: Callable[[str], str] = str) -> list[Item]:
    """The list's lines for ``presentation``; ``say`` words a finding for
    the card (its key route)."""

    out: list[Item] = []

    def findings(rows: Sequence[doctor.Row], indent: str) -> None:
        for row in rows:
            out.append(Item("finding", f"{indent}{_MARKS[row.level]} {say(row.text)}", _ROLES[row.level], row))

    blocked = presentation.of("block")
    if blocked:
        out.append(Item("section", f"BLOCKED ({len(blocked)})", "error"))
        findings(blocked, "  ")
    attention = presentation.of("attention")
    if attention:
        out.append(Item("section", f"Attention ({len(attention)})", "warn"))
        for area, rows in presentation.areas("attention"):
            out.append(Item("area", f"  {area} ({len(rows)})", "accent"))
            findings(rows, "    ")
    advisories = presentation.of(doctor.ADVISORY)
    if advisories:
        mark = "▾" if expanded else "▸"
        out.append(Item("toggle", f"{mark} {doctor.ADVISORIES_TITLE} ({len(advisories)})", "dim"))
        if expanded:
            findings(advisories, "  ")
    if not out:
        out.append(Item("none", cli_text.FINDINGS_NONE, "ok"))
    out.append(Item("verify", "Verify this installation (explicit; signed release)", "accent"))
    return out


def action_hint(row: doctor.Row) -> str:
    """What Enter does on ``row``."""

    return cli_text.FINDINGS_ACTIONS.get(row.action or "", cli_text.FINDINGS_COMMAND_HINT)


class FindingsScreen:
    """The findings list (see the module docstring). Session resumes keep the
    card's passthrough and way back; ``say`` words a finding for the card."""

    what = "the doctor findings"

    def __init__(self, runtime: Any, *, palette: tui.Palette, tty_in: TextIO, tty_out: TextIO,
                 say: Callable[[str], str] | None = None,
                 passthrough: Sequence[str] = (), way_back: _WayBack | None = None,
                 report: Callable[[], tuple[list[str], list[str], list[str]]] | None = None):
        self.runtime = runtime
        self.palette = palette
        self.tty_in = tty_in
        self.tty_out = tty_out
        self.say = say or str
        self.passthrough = tuple(passthrough)
        self.way_back = way_back
        self.report = report or (lambda: doctor._collect_doctor_reports(runtime))
        self.expanded = False
        self.selected = 0
        self.offset = 0
        self.message = ""
        self.message_role = "accent"
        self.result: tuple | None = None
        self._collect()

    # -- the report ------------------------------------------------------------

    def _collect(self) -> None:
        """Run doctor again and rebuild the list (on open and after every action)."""

        self.problems, self.info, self.attention = self.report()
        self.presentation = doctor.present_report(self.problems, self.attention, info=self.info)
        self._rebuild()

    def _rebuild(self, keep: doctor.Row | None = None) -> None:
        current = keep or self.row()
        self.lines = items(self.presentation, expanded=self.expanded, say=self.say)
        selectable = [index for index, item in enumerate(self.lines) if item.selectable]
        self.selectable = selectable
        if not selectable:
            self.selected = 0
            return
        if current is not None:
            same = [i for i, index in enumerate(selectable)
                    if self.lines[index].row is not None and self.lines[index].row.text == current.text]
            if same:
                self.selected = same[0]
                return
        self.selected = max(0, min(self.selected, len(selectable) - 1))

    def item(self) -> Item | None:
        if not self.selectable:
            return None
        return self.lines[self.selectable[self.selected]]

    def row(self) -> doctor.Row | None:
        item = self.item() if hasattr(self, "lines") else None
        return item.row if item is not None else None

    # -- layout ----------------------------------------------------------------

    def _keybar(self) -> tui.KeyBar:
        return tui.KeyBar(cli_text.FINDINGS_KEYBAR)

    def min_size(self, width: int) -> tuple[int, int]:
        return views.ScreenFloor.compute(
            chrome=cli_text.FINDINGS_CHROME, list_rows=len(self.lines), detail_reserve=0,
            bar_rows=self._keybar().rows(width), cols=cli_text.FINDINGS_MIN_COLS,
        ).as_tuple()

    def _title(self) -> str:
        status = views.CARD_STATUS_TEXT[self.presentation.status]
        counts = [f"{len(self.presentation.of(level))} {name}" for level, name in
                  (("block", "blocking"), ("attention", "attention"), (doctor.ADVISORY, "advisory"))
                  if self.presentation.of(level)]
        return f"{cli_text.FINDINGS_TITLE} — {status}" + (f" · {', '.join(counts)}" if counts else "")

    def _wrapped(self, width: int) -> list[tuple[int, str]]:
        """The list as drawn: ``(line index, text)`` per screen row. A long
        finding wraps under its own indent, so no command or key route in
        it is ever cut off."""

        room = max(20, width - 3)
        drawn: list[tuple[int, str]] = []
        for index, item in enumerate(self.lines):
            visible = termtext.visible_text(item.text)
            indent = len(visible) - len(visible.lstrip(" "))
            pieces = textwrap.wrap(visible[indent:], room, initial_indent=" " * indent,
                                   subsequent_indent=" " * (indent + 2), break_on_hyphens=False) or [visible]
            drawn.extend((index, piece) for piece in pieces)
        return drawn

    def _info(self, width: int) -> list[str]:
        """Under the list: how many findings the selected row stands for,
        or what the advisories are."""

        item = self.item()
        if item is None:
            return []
        if item.kind == "verify":
            return ["Read-only integrity check; never repairs or restarts.",
                    "Choose downloaded release files, or confirm an exact-version fetch."]
        if item.row is None:
            return textwrap.wrap(cli_text.FINDINGS_ADVISORIES_NOTE, max(20, width - 3))[:_INFO_LINES]
        if len(item.row.originals) > 1:
            return [cli_text.FINDINGS_MORE.format(n=len(item.row.originals))]
        return []

    def _draw(self, win: Any) -> None:
        palette = self.palette
        height, width = win.getmaxyx()
        rows_needed, cols_needed = self.min_size(width)
        if height < rows_needed or width < cols_needed:
            screens_common._screen_too_small(win, palette, self.what, rows_needed, cols_needed)
            return
        win.erase()
        keybar = self._keybar()
        bar_rows = keybar.rows(width)
        message_row = height - bar_rows - 1
        hint_row = message_row - 1
        info_top = hint_row - _INFO_LINES
        rule_row = info_top - 1
        role = views.CARD_STATUS_ROLE[self.presentation.status]
        tui.safe_add(win, 1, 2, views.clip(self._title(), width - 3), palette.attr(role) | tui.curses.A_BOLD)
        tui.safe_add(win, 2, 2, views.rule(width), palette.attr("dim"))
        visible = max(1, rule_row - 3)
        drawn = self._wrapped(width)
        current = self.selectable[self.selected] if self.selectable else -1
        span = [row for row, (index, _text) in enumerate(drawn) if index == current]
        if span:
            first, last = span[0], span[-1]
            if first < self.offset:
                self.offset = first
            elif last >= self.offset + visible:
                # The whole finding when it fits, else its start.
                self.offset = min(first, last - visible + 1)
        self.offset = max(0, min(self.offset, max(0, len(drawn) - visible)))
        shown_first = None
        for y, (index, line) in enumerate(drawn[self.offset:self.offset + visible], 3):
            item = self.lines[index]
            focused = item.selectable and index == current
            attr = palette.attr(item.role) | (tui.curses.A_BOLD if item.kind in ("section", "area") else 0)
            if focused:
                if shown_first is None:
                    shown_first = y
                    tui.safe_add(win, y, 0, "›", palette.attr("accent"))
                attr |= tui.curses.A_REVERSE
            tui.safe_add(win, y, 2, line, attr)
        tui.safe_add(win, rule_row, 2, views.rule(width), palette.attr("dim"))
        if self.offset + visible < len(drawn):
            tui.safe_add(win, rule_row, 2, tui.MORE_ROWS.format(n=len(drawn) - self.offset - visible) + " ",
                         palette.attr("dim"))
        for y, line in enumerate(self._info(width), info_top):
            tui.safe_add(win, y, 2, line, palette.attr("dim"))
        item = self.item()
        if item is not None:
            hint = ("Enter: verify this installation" if item.kind == "verify" else
                    action_hint(item.row) if item.row is not None else
                    cli_text.FINDINGS_ADVISORIES_OPEN if self.expanded else cli_text.FINDINGS_ADVISORIES_CLOSED)
            tui.safe_add(win, hint_row, 2, views.clip(hint, width - 3), palette.attr("accent"))
        if self.message:
            tui.safe_add(win, message_row, 2, views.clip(self.message.replace("\n", " "), width - 3),
                         palette.attr(self.message_role))
        keybar.draw(win, height - 1, palette)
        win.refresh()

    # -- keys ------------------------------------------------------------------

    def _say(self, text: str, role: str = "accent") -> None:
        self.message = text
        self.message_role = role

    def _move(self, delta: int) -> None:
        if self.selectable:
            self.selected = max(0, min(len(self.selectable) - 1, self.selected + delta))

    def run(self, win: Any) -> tuple | None:
        """None (Esc), or the perform intent Sessions returned."""

        tui.hide_cursor()
        while True:
            self._draw(win)
            key = tui.read_key(win)
            if key.kind == "resize":
                continue
            if key.kind == "ctrl" and key.ch == "c":
                raise KeyboardInterrupt
            if key.kind == "esc":
                return None
            height, width = win.getmaxyx()
            rows_needed, cols_needed = self.min_size(width)
            if height < rows_needed or width < cols_needed:
                continue
            self.message = ""
            ch = key.ch.lower() if key.kind == "char" and key.ch else ""
            if key.kind in ("up", "down"):
                self._move(-1 if key.kind == "up" else 1)
            elif key.kind in ("pageup", "pagedown"):
                self._move((-1 if key.kind == "pageup" else 1) * max(1, height - cli_text.FINDINGS_CHROME))
            elif key.kind == "home":
                self.selected = 0
            elif key.kind == "end":
                self._move(len(self.selectable))
            elif key.kind == "enter":
                result = self._enter(win)
                if result is not None:
                    return result
            elif ch == "v":
                self._details(win)
            elif ch in ("w", "g"):  # G stays a silent compatibility key until 3.0.0.
                self._gateway(win)
            elif ch == "?":
                self._help(win)

    def _enter(self, win: Any) -> tuple | None:
        item = self.item()
        if item is None:
            return None
        if item.kind == "verify":
            run_install_verification(self.runtime, win, self.palette, background=self._draw)
            return None
        if item.kind == "toggle":
            self.expanded = not self.expanded
            self._rebuild(keep=None)
            return None
        row = item.row
        assert row is not None
        handler = {
            "hold": self._hold, "hold-coverage": self._hold, "missing-key": self._set_key,
            "earlier-scopes": self._repair_all, "scope-repair": self._repair_all,
            "unused-alias": self._prune_alias, "prune": self._prune, "dead-sessions": self._mark_dead,
            "seed-update": self._reseed, "advisory-model": self._qualify, "needs-choice": self._sessions,
        }.get(row.action or "", self._command)
        return handler(win, row)

    def _details(self, win: Any) -> None:
        lines = [line for _role, line in doctor.report_lines(self.problems, self.info, self.attention,
                                                               verbose=True, say=self.say)]
        tui.TextView(f"{cli_text.FINDINGS_TITLE} -v", lines, palette=self.palette).run(win)

    def _gateway(self, win: Any) -> None:
        """The shared gateway dialog: start, restart, service install, log."""

        message = screens_gateway.dialog(self.runtime, win, self.palette, background=self._draw,
                                         tty_in=self.tty_in, tty_out=self.tty_out)
        self._collect()
        if message:
            self._say(message)

    def _help(self, win: Any) -> None:
        wrap = max(20, min(100, win.getmaxyx()[1] - 8))
        screens_common._ScrollModal(cli_text.FINDINGS_HELP_TITLE,
                                    screens_common._wrap_help_lines(cli_text.FINDINGS_HELP, wrap)).run(
            win, self.palette, background=self._draw)

    # -- the actions -----------------------------------------------------------

    def _writable(self) -> bool:
        if getattr(self.runtime, "allow_state_writes", True):
            return True
        self._say(cli_text.FINDINGS_READ_ONLY, "warn")
        return False

    def _confirm(self, win: Any, title: str, body: str, buttons: Sequence[tuple[str, Any]]) -> bool:
        lines = screens_common.modal_lines(termtext.visible_text(body), win)
        return bool(tui.Modal(title, lines, buttons=tuple(buttons), default=False).run(
            win, self.palette, background=self._draw))

    def _show_result(self, win: Any, what: str, run: Callable[[TextIO], int]) -> None:
        """Run one doctor action with its output kept, show it, refresh."""

        out = io.StringIO()
        try:
            run(out)
        except (errors.CharmerError, OSError) as exc:
            out.write(termtext.visible_message(exc) + "\n")
        lines = [termtext.visible_text(line) for line in out.getvalue().splitlines()] or ["(no output)"]
        tui.TextView(cli_text.FINDINGS_RESULT_TITLE.format(what=what), lines, palette=self.palette).run(win)
        self._collect()

    def _command(self, win: Any, row: doctor.Row) -> None:
        """A row without an action: the finding and its exact command. A
        row that stands for its lines in other words (a collapsed row, a
        model row with its counts and optional next step) shows that row
        first, then every line it stands for."""

        lines = [self.say(row.text), ""] if row.collapsed else []
        lines += [self.say(line) for line in row.originals]
        if row.remedy:
            lines += ["", cli_text.FINDINGS_COMMAND.format(command=row.remedy)]
        tui.TextView(cli_text.FINDINGS_COMMAND_TITLE, [termtext.visible_text(line) for line in lines],
                     palette=self.palette).run(win)

    def _hold(self, win: Any, row: doctor.Row) -> None:
        route = screens_gateway.HOLD_COVERAGE_ROUTE if row.action == "hold-coverage" else \
            screens_gateway.HOLD_FAILURE_ROUTE
        lines = [*row.originals, "", route, "", cli_text.FINDINGS_HOLD_CLEAR]
        tui.TextView(cli_text.FINDINGS_HOLD_TITLE, [termtext.visible_text(line) for line in lines],
                     palette=self.palette).run(win)

    def _set_key(self, win: Any, row: doctor.Row) -> None:
        import charmer.cli.screens.providers as screens_providers

        if row.subject is None:
            self._command(win, row)
            return
        outcome = screens_providers.ConnectActions(self.runtime, win, self.palette, background=self._draw).set_key(
            row.subject)
        if outcome is not None and outcome.text:
            self._say(outcome.text, outcome.role)
        self._collect()

    def _repair_all(self, win: Any, row: doctor.Row) -> None:
        if not self._writable():
            return
        mode = tui.Modal(cli_text.FINDINGS_REPAIR_TITLE,
                         screens_common.modal_lines(cli_text.FINDINGS_REPAIR_BODY, win),
                         buttons=cli_text.FINDINGS_REPAIR_BUTTONS, default=False).run(
                             win, self.palette, background=self._draw)
        if not mode:
            self._say(cli_text.FINDINGS_CANCELLED)
            return
        if mode == "live":
            self._repair_including_live(win)
            return
        self._show_result(win, "charmer doctor --repair-all",
                          lambda out: doctor_actions._doctor_repair_all(self.runtime, out))

    def _repair_including_live(self, win: Any) -> None:
        from charmer import sessions
        import charmer.cli.session_facts as session_facts

        records, problems, _ids = session_facts._session_record_scan(self.runtime)
        if problems:
            self._say("Cannot preview every repair target: " + termtext.visible_text(problems[0]), "warn")
            return
        records = [record for record in records if record.get("version") == sessions.RECORD_VERSION
                   and record.get("lineup_generation", 0) >= 1]
        if not records:
            self._say("No current-format scopes to repair.")
            return
        labels, unknown = screens_gateway.running_sessions(self.runtime)
        body = [cli_text.FINDINGS_REPAIR_LIVE_BODY]
        body += ["Running now:", *(f"  {label}" for label in labels)] if labels else []
        if unknown:
            body.append(f"Liveness is incomplete: {unknown}")
        body += ["Every scope this pass may change (including live or unknown sessions):"]
        body += [f"  {sessions.managed_id(record)} ({session_facts._record_target_label(record)})" for record in records]
        lines = screens_common.modal_lines("\n".join(termtext.visible_text(line) for line in body), win)
        # A plain modal clips a long body. Keep every target accessible before
        # asking, and still focus Cancel in the final confirmation.
        if len(lines) > win.getmaxyx()[0] - 5:
            if not tui.TextView(cli_text.FINDINGS_REPAIR_LIVE_TITLE,
                                [*lines, "", "Continue to the repair confirmation?"],
                                palette=self.palette, confirm=True).run(win):
                self._say(cli_text.FINDINGS_CANCELLED)
                return
            lines = screens_common.modal_lines(
                f"Repair the {len(records)} sessions listed in the preview, including running sessions? "
                + cli_text.FINDINGS_REPAIR_LIVE_BODY, win)
        if not tui.Modal(cli_text.FINDINGS_REPAIR_LIVE_TITLE, lines,
                         buttons=(("Cancel", False), ("Repair", True)), default=False).run(
                             win, self.palette, background=self._draw):
            self._say(cli_text.FINDINGS_CANCELLED)
            return
        ids = frozenset(sessions.managed_id(record) for record in records)
        self._show_result(win, "charmer doctor --repair-all --include-live",
                          lambda out: doctor_actions._doctor_repair_all(
                              self.runtime, out, include_live=True, session_ids=ids))

    def _prune_alias(self, win: Any, row: doctor.Row) -> None:
        if row.subject is None:
            self._command(win, row)
            return
        if not self._writable():
            return
        alias = row.subject
        if not self._confirm(win, cli_text.FINDINGS_PRUNE_ALIAS_TITLE.format(alias=alias),
                             cli_text.FINDINGS_PRUNE_ALIAS_BODY.format(alias=alias),
                             cli_text.FINDINGS_PRUNE_ALIAS_BUTTONS):
            self._say(cli_text.FINDINGS_CANCELLED)
            return
        self._show_result(win, f"charmer doctor --prune-aliases {alias}",
                          lambda out: doctor_actions._doctor_prune_aliases(self.runtime, [alias], out))

    def _prune(self, win: Any, row: doctor.Row) -> None:
        if not self._writable():
            return
        preview = io.StringIO()
        code = doctor_actions._doctor_prune_preview(self.runtime, preview)
        lines = [termtext.visible_text(line) for line in preview.getvalue().splitlines()]
        if code != 0:
            tui.TextView(cli_text.FINDINGS_PRUNE_TITLE, lines, palette=self.palette).run(win)
            self._say(cli_text.FINDINGS_CANCELLED)
            return
        if not tui.TextView(cli_text.FINDINGS_PRUNE_TITLE, [*lines, "", cli_text.FINDINGS_PRUNE_QUESTION],
                            palette=self.palette, confirm=True).run(win):
            self._say(cli_text.FINDINGS_CANCELLED)
            return
        self._show_result(win, "charmer doctor --prune", lambda out: doctor_actions._doctor_prune(self.runtime, out))

    def _mark_dead(self, win: Any, row: doctor.Row) -> None:
        """``sessions mark-ended --all-dead``: the sessions without a
        recorded end that no check finds running, after one confirmation
        that names them; the mark itself decides again under each lock."""

        import charmer.cli.session_actions as session_actions
        import charmer.cli.session_facts as session_facts

        if not self._writable():
            return
        liveness = doctor._session_liveness(self.runtime)  # read now: no report snapshot is open here
        daemon, scan = liveness
        if not daemon.known or not scan.known:
            reason = daemon.reason if not daemon.known else scan.reason
            self._say(f"whether sessions run cannot be told ({reason}); nothing was marked", "warn")
            return
        # The sessions doctor names: the same records and the same checks.
        dead = doctor.dead_session_ids(session_facts._session_records(self.runtime), liveness)
        if not dead:
            self._say(cli_text.FINDINGS_DEAD_NONE)
            return
        if not self._confirm(win, cli_text.FINDINGS_DEAD_TITLE.format(n=len(dead)),
                             cli_text.FINDINGS_DEAD_BODY.format(ids=", ".join(mid[:8] for mid in dead)),
                             cli_text.FINDINGS_DEAD_BUTTONS):
            self._say(cli_text.FINDINGS_CANCELLED)
            return

        def mark(out: TextIO) -> int:
            # Each mark decides again under the session's lock, on the same process table.
            results = session_actions._mark_ended_ids(self.runtime, dead, proc_root=self.runtime.proc_root)
            for managed, status in results:
                out.write(f"{managed}  {status}\n")
            return 0

        self._show_result(win, "charmer sessions mark-ended --all-dead", mark)

    def _reseed(self, win: Any, row: doctor.Row) -> None:
        """``profile reseed``: the shipped changes, y/N, then the reseed and
        the offer to the sessions that follow the profile (Profiles → U)."""

        import charmer.cli.screens.propagation as screens_propagation
        from charmer.setup import model, profiles as setup_profiles

        name = row.subject
        if name is None:
            self._command(win, row)
            return
        if not self._writable():
            return
        try:
            plan = setup_profiles.plan_reseed(self.runtime, name)
        except (errors.CharmerError, OSError) as exc:
            self._say(termtext.visible_message(exc), "warn")
            return
        if plan.nothing:
            self._say(plan.lines[0])
            self._collect()
            return
        if not tui.TextView(cli_text.RESEED_VIEW_TITLE.format(name=name, v=plan.version), plan.lines,
                            palette=self.palette, confirm=True).run(win):
            self._say(model.NOTHING_CHANGED)
            return
        try:
            outcome = setup_profiles.apply_reseed(self.runtime, plan, model.Confirmation.given(plan))
        except (errors.CharmerError, OSError) as exc:
            self._say(termtext.visible_message(exc), "warn")
            return
        self._say(outcome.lines[0])
        screens_propagation.run_propagation_screen(self.runtime, win, self.palette, [name])
        self._collect()

    def _qualify(self, win: Any, row: doctor.Row) -> None:
        """M → Q for the row's model: the optional diagnostics with their
        own consent (nothing is sent before it)."""

        if row.subject is None:
            self._command(win, row)
            return
        screens_common.OnboardingActions(self.runtime, win, self.palette).qualify(row.subject)
        self._collect()

    def _sessions(self, win: Any, row: doctor.Row) -> tuple | None:
        import charmer.cli.screens.launch_sessions as launch_sessions

        screen = launch_sessions._SessionsScreen(self.runtime, palette=self.palette,
                                                 passthrough=self.passthrough, way_back=self.way_back,
                                                 tty_in=self.tty_in, tty_out=self.tty_out)
        screen.cwd_filter = False
        screen.filter_mode = "needs a choice"
        screen._reload()
        if not screen.records:
            self._say("No session needs a lead choice now.")
            self._collect()
            return None
        result = screen._needs_menu(win, screen.records)
        if result is not None:
            return result
        self._collect()
        return None


class _VerificationCancelled(Exception):
    """The explicit online disclosure was declined, before any fetch."""


def run_install_verification(runtime: Any, win: Any, palette: tui.Palette, *,
                             background: Callable | None = None) -> None:
    """The update pane and H share this explicit, read-only verification flow."""
    from pathlib import Path
    from charmer import install_verify

    title = "Verify this installation"
    mode = tui.Modal(title, screens_common.modal_lines(
        "Verify the exact installed release with installed trust. Downloaded files are fully offline. "
        "Online verification asks before fetching. No repair or restart.", win),
        buttons=(("Cancel", None), ("Downloaded files", "local"), ("Online", "online")), default=None).run(
            win, palette, background=background)
    if mode is None:
        return
    directory = None
    if mode == "local":
        values = tui.OnboardingForm(title, (("directory", "Downloaded release directory", "", ()),),
                                    palette=palette, footer="Read-only, fully offline; nothing is changed.",
                                    help_text="Use the installed exact version's MANIFEST.json, SHA256SUMS and "
                                              "SHA256SUMS.sshsig. Trust comes from the installation, not this folder.",
                                    checks={"directory": lambda value: None if value else
                                            "Enter a downloaded release directory, or Esc to cancel."}).run(win)
        if values is None:
            return
        directory = Path(values["directory"]).expanduser()

    def disclose(text: str) -> None:
        if not tui.Modal("Fetch signed release metadata?",
                         screens_common.modal_lines(termtext.visible_text(text), win),
                         buttons=(("Cancel", False), ("Verify", True)), default=False).run(
                             win, palette, background=background):
            raise _VerificationCancelled

    try:
        result = install_verify.verify_current(environ={**runtime.environ, "HOME": str(runtime.home)},
                                               state_root=runtime._session_store_root,
                                               from_dir=directory, disclose=disclose)
    except _VerificationCancelled:
        return
    tui.TextView(title, [termtext.visible_text(line) for line in result.lines(surface="tui")], palette=palette).run(win)


def run_findings_screen(runtime: Any, win: Any, palette: tui.Palette, *, tty_in: TextIO, tty_out: TextIO,
                        say: Callable[[str], str] | None = None,
                        passthrough: Sequence[str] = (), way_back: _WayBack | None = None) -> tuple | None:
    """Open the findings list on ``win``: None, or the perform intent of a
    resume chosen in Sessions."""

    return FindingsScreen(runtime, palette=palette, tty_in=tty_in, tty_out=tty_out, say=say,
                          passthrough=passthrough, way_back=way_back).run(win)
