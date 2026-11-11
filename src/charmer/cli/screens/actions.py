"""The TUI's keys by stable id, shared by bars, help and documentation.

Screens select applicable ids using their handler predicates. Labels and
ranks may follow the selected row; help always names every non-silent key.
Help-only keys carry a reason; compatibility aliases stay silent. The key
census checks handlers, table rows, rendered bars and help together.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping


@dataclass(frozen=True)
class Action:
    """One key of one screen: ``id`` is ``<screen>.<verb>``; ``command`` is
    the shell equivalent (None when the action exists only in the TUI)."""

    id: str
    key: str
    does: str
    command: str | None = None
    label: str = ""
    rank: int = 2
    help: str = ""
    help_only: str | None = None
    silent: bool = False

    def __post_init__(self) -> None:
        if self.rank not in (1, 2, 3):
            raise ValueError(f"{self.id}: rank must be 1, 2 or 3")
        if self.help_only is not None and (not self.help_only.strip() or self.rank == 1):
            raise ValueError(f"{self.id}: help-only keys need a reason and cannot be primary")
        if not self.help:
            object.__setattr__(self, "help", f"{self.key} — {self.does}")


def _rows(screen: str, *rows: tuple[str, str, str] | tuple[str, str, str, str | None]) -> tuple[Action, ...]:
    return tuple(Action(f"{screen}.{row[0]}", row[1], row[2], row[3] if len(row) > 3 else None) for row in rows)


_HELP = ("help", "?", "this screen's help")

ACTIONS: dict[str, tuple[Action, ...]] = {
    "card": _rows(
        "card",
        ("launch", "Enter", "launch the profile on the card (while nothing is connected: open Get started)",
         "charmer --profile <name>"),
        ("launch-anyway", "L", "launch while nothing is connected yet", "charmer --profile <name>"),
        ("update", "U", "check for and install a newer charmer", "charmer update"),
        ("get-started", "W", "open Get started", "charmer setup"),
        ("edit", "E", "edit the profile on the card", "charmer profile edit <name>"),
        ("next-profile", "Tab", "show the next profile (Shift-Tab: the previous one)"),
        ("profiles", "P", "open Profiles", "charmer profile list"),
        ("direct", "D", "open Direct", "charmer direct <model>"),
        ("details", "V", "every card row in full, the /model lead set and the kept environment"),
        ("sessions", "S", "open Sessions", "charmer sessions list"),
        ("providers", "G", "open Providers", "charmer providers list"),
        ("models", "M", "open Models", "charmer models list"),
        ("settings", "O", "open Settings"),
        ("doctor", "H", "doctor's findings: act on a row, the full report, the gateway's actions",
         "charmer doctor"),
        _HELP,
        ("quit", "Esc", "leave without launching"),
    ),
    "resume-card": _rows(
        "resume-card",
        ("resume", "Enter", "resume the session with the lineup shown", "charmer -r <session>"),
        ("update", "U", "check for and install a newer charmer", "charmer update"),
        ("details", "V", "every card row in full, the /model lead set and the kept environment"),
        ("sessions", "S", "open Sessions", "charmer sessions list"),
        ("doctor", "H", "doctor's findings: act on a row, the full report, the gateway's actions",
         "charmer doctor"),
        _HELP,
        ("cancel", "Esc", "go back without resuming"),
    ),
    "sessions": _rows(
        "sessions",
        ("resume", "Enter", "resume the selected session", "charmer -r <session>"),
        ("details", "V", "identity, directory, lineup, pending change, drift and usage", "charmer sessions show <session>"),
        ("lineup", "T", "change the session's lineup, with its effect first", "charmer lineup --session <session>"),
        ("follow", "F", "follow the session's profile or pin its lineup", "charmer lineup --session <session> follow"),
        ("rename", "R", "rename the session (shown here only)"),
        ("forget", "X", "forget the record and its generated scope", "charmer sessions forget <session>"),
        ("stop", "E", "stop a session running in the background", "charmer sessions stop <session>"),
        ("mark-ended", "M", "record the end of a session that exited without one", "charmer sessions mark-ended <session>"),
        ("repair", "P", "rebuild the session's generated scope", "charmer doctor --repair <session>"),
        ("link", "L", "manage a native session", "charmer sessions link <runtime-id>"),
        ("directories", "C", "this directory or all directories", "charmer sessions list"),
        ("filter", "/", "cycle session filters", "charmer sessions list --ended"),
        ("filters", "B", "combine directory, end, lead-choice and age filters with AND", "charmer sessions list --ended --needs-choice"),
        ("mark", "Space", "mark or unmark the session"),
        ("mark-all", "*", "mark every visible session, or clear the marks"),
        ("save-choice", "Q", "save a profile or direct lead for the next resume without launching", "charmer sessions choose"),
        _HELP,
        ("back", "Esc", "back"),
    ),
    "findings": _rows(
        "findings",
        ("act", "Enter", "act on the finding (its fixed action, asked first) or show its exact command"),
        ("details", "V", "the full report, every original line", "charmer doctor -v"),
        ("gateway", "W", "the gateway's actions: start, restart, service install, log, plan", "charmer gateway status"),
        _HELP,
        ("back", "Esc", "back to the card"),
    ),
    "gateway": _rows(
        "gateway",
        ("plan", "P", "show the read-only served-change plan", "charmer plan"),
        _HELP,
        ("back", "Esc", "close the gateway dialog"),
    ),
    "lineup-dialog": _rows(
        "lineup-dialog",
        ("apply", "Enter", "apply the previewed change", "charmer lineup --session <session>"),
        ("target", "Tab", "move to the next kind of change"),
        ("choose", "← →", "cycle the profiles or fallback providers"),
        ("pick", "Space", "list the choices of the current target"),
        _HELP,
        ("cancel", "Esc", "close without writing"),
    ),
    "direct": _rows(
        "direct",
        ("launch", "Enter", "launch the model as a direct session", "charmer direct <model>"),
        ("save", "Tab", "save the choice as a lead-only profile"),
        ("providers", "G", "open Providers", "charmer providers list"),
        ("effort", "← →", "choose the effort of a gateway-effort model"),
        ("gateway", "W", "start the local gateway or read its log", "charmer gateway start"),
        _HELP,
        ("back", "Esc", "back"),
    ),
    "providers": (
        Action("providers.primary", "Enter", "the row's main action: set a key, sign in, connect, approve or details",
               label="details", rank=1),
        Action("providers.apply", "P", "make the gateway serve your changes", "charmer providers apply",
               label="apply", rank=1, help="P applies your changes when the gateway is not serving them yet."),
        Action("providers.add-models", "A", "add models to the provider", "charmer models add",
               label="add models", rank=1, help="A adds models (a listing you confirm, or by hand)."),
        Action("providers.edit", "E", "edit a provider you added", "charmer providers edit <provider>",
               label="edit", rank=1, help="E edits a provider you added; Enter approves its route."),
        Action("providers.sign-in", "L", "sign in or out of an account provider", "charmer providers sign-in <provider>",
               label="sign in/out", rank=1,
               help="L signs in to or out of a Claude or ChatGPT account (personal use; it runs in this terminal)."),
        Action("providers.set-key", "K", "set or replace an API key", "charmer providers set-key <provider>",
               label="set key", rank=1,
               help="K sets or replaces an API key (typed masked; saving reloads the gateway; nothing is sent to the provider)."),
        Action("providers.remove", "X", "remove the key, the sign-in or a provider you added", "charmer providers remove-key <provider>",
               label="remove key", rank=1,
               help="X removes a key, signs out, or removes a provider you added; profiles that use it stop being connected."),
        Action("providers.test", "T", "test the provider", "charmer providers test <provider>", label="test",
               help="T tests a provider with one request (you confirm first; it may be billed)."),
        Action("providers.toggle", "Space", "turn the provider on or off", "charmer providers enable <provider>", label="on/off",
               help="Space turns a provider on or off for new sessions; running sessions keep theirs."),
        Action("providers.details", "V", "the provider's details", "charmer providers show <provider>", label="details",
               help="V shows details: models, served state, signed-in accounts, quota."),
        Action("providers.new", "N", "add a provider", "charmer providers add", label="new",
               help="N adds your own endpoint (from a reviewed preset or by hand) or a server on your network."),
        Action("providers.refresh", "R", "read the gateway again", label="refresh", rank=3),
        Action("providers.gateway", "W", "start the local gateway or read its log", "charmer gateway start", label="gateway", rank=3,
               help="W opens the local gateway: start or restart it now, or read its log (stopping it is a terminal command: charmer gateway stop)."),
        Action("providers.key-file", "F", "choose the private key file used for every API key", "charmer setup --keys-file <file>",
               label="key file", rank=3,
               help="F — key file: choose an existing private NAME=value file for all provider API keys."),
        Action("providers.remove-orphan-key", "O", "list saved keys no provider uses and remove one",
               "charmer providers remove-key <provider> --name <key>", label="orphan keys", rank=3,
               help="O — orphan keys: list saved names no provider uses and confirm a removal. Values are never shown."),
        Action("providers.discover-all", "D", "list every connected provider's models in one consented pass", "charmer discover --all",
               label="list all", rank=3,
               help="D — list all: disclose every listing request before one confirmation; nothing is declared or admitted."),
        Action("providers.help", "?", "this screen's help", label="help"),
        Action("providers.back", "Esc", "back", label="back"),
        Action("providers.details-alias", "Q", "the provider's details", silent=True),
    ),
    "models": _rows(
        "models",
        ("primary", "Enter", "inspect a line or add/remove its optional admission badge", "charmer models show <key>"),
        ("qualify", "Q", "run optional diagnostics with explicit consent", "charmer models qualify <key>"),
        ("edit", "E", "edit a model you declared", "charmer models edit <key>"),
        ("remove", "X", "remove a model you declared", "charmer models rm <key>"),
        ("details", "V", "the line's details", "charmer models show <key>"),
        _HELP,
        ("back", "Esc", "back"),
    ),
    "settings": _rows(
        "settings",
        ("edit", "Enter", "edit the value, open the screen it summarizes or rotate the token"),
        ("reset", "R", "reset the value to its default (Cancel is focused first)"),
        _HELP,
        ("back", "Esc", "back"),
    ),
    "profiles": _rows(
        "profiles",
        ("use", "Enter", "use the profile for this launch"),
        ("new", "N", "create a profile", "charmer profile new <name>"),
        ("edit", "E", "edit the profile (or its file when it cannot be loaded)", "charmer profile edit <name>"),
        ("copy", "C", "copy the profile", "charmer profile duplicate <name> <new>"),
        ("rename", "R", "rename the profile", "charmer profile rename <name> <new>"),
        ("delete", "X", "delete the profile (a copy is kept)", "charmer profile rm <name>"),
        ("reseed", "U", "update a shipped profile, or restore one that cannot be loaded", "charmer profile reseed <name>"),
        ("default", "D", "make the profile the default", "charmer profile default <name>"),
        ("fallback", "F", "list only the fallback profiles"),
        ("bindings", "B", "edit the named bindings"),
        ("get-started", "W", "open Get started", "charmer setup"),
        _HELP,
        ("back", "Esc", "back"),
    ),
    "get-started": _rows(
        "get-started",
        ("open", "Enter", "open the selected step"),
        ("add-provider", "A", "add a provider", "charmer providers add"),
        ("profiles", "P", "open Profiles"),
        ("offline-client", "F", "on the Claude Code step: verify and copy an offline Claude Code file",
         "charmer setup --claude-from <file>"),
        _HELP,
        ("card", "Esc", "go to the launch card"),
    ),
    "editor": _rows(
        "editor",
        ("change", "Enter", "change the selected row"),
        ("unbind", "U", "unbind the selected agent"),
        ("named-bindings", "B", "edit the named bindings"),
        ("routing", "R", "show the review routing"),
        ("save", "^S", "save the profile"),
        _HELP,
        ("back", "Esc", "back"),
    ),
    "binding-picker": _rows(
        "binding-picker",
        ("effort", "← →", "choose the effort"),
        ("choose", "Enter", "bind the model, use a named binding or turn the slot off"),
        ("details", "V", "the model's details"),
        _HELP,
        ("back", "Esc", "back"),
    ),
    "named-bindings": _rows(
        "named-bindings",
        ("edit", "Enter", "change the binding's model or effort"),
        ("add", "A", "add a named binding"),
        ("delete", "X", "delete the named binding"),
        _HELP,
        ("back", "Esc", "back"),
    ),
}


def bar(screen: str, ids: Iterable[str], *, labels: Mapping[str, str] | None = None,
        ranks: Mapping[str, int] | None = None) -> tuple[tuple[str, str, int], ...]:
    """Applicable keys in table order. Ids may be local verbs or full ids."""

    def local(name: str) -> str:
        return name.removeprefix(screen + ".")

    selected = {local(name) for name in ids}
    labels = {local(name): value for name, value in (labels or {}).items()}
    ranks = {local(name): value for name, value in (ranks or {}).items()}
    rows = ACTIONS[screen]
    unknown = (selected | labels.keys() | ranks.keys()) - {local(row.id) for row in rows}
    if unknown:
        raise KeyError(f"{screen}: unknown actions {', '.join(sorted(unknown))}")
    bindings = []
    for row in rows:
        verb = local(row.id)
        if verb not in selected or row.silent or row.help_only:
            continue
        label, rank = labels.get(verb, row.label), ranks.get(verb, row.rank)
        if not label or rank not in (1, 2, 3):
            raise ValueError(f"{row.id}: a bar key needs a label and rank 1, 2 or 3")
        bindings.append((row.key, label, rank))
    return tuple(bindings)


def help_text(screen: str, notes: str = "") -> str:
    """Every non-silent key, one help line each, then the screen's notes."""

    keys = "\n".join(row.help for row in ACTIONS[screen] if not row.silent)
    return keys + ("\n\n" + notes if notes else "")


def action(action_id: str) -> Action:
    """The action with ``action_id`` (KeyError when there is none)."""

    found = next((row for row in ACTIONS.get(action_id.split(".", 1)[0], ()) if row.id == action_id), None)
    if found is None:
        raise KeyError(action_id)
    return found
