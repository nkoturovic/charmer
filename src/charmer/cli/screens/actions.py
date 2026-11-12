"""The TUI's keys by stable id, shared by bars, help and documentation.

Screens select applicable ids using their handler predicates. Labels and
ranks may follow the selected row; help always names every non-silent key.
Help-only keys carry a reason; compatibility aliases stay silent. The key
census checks handlers, table rows, rendered bars and help together.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from charmer import termtext


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


def _rows(screen: str, *rows: tuple) -> tuple[Action, ...]:
    return tuple(Action(f"{screen}.{row[0]}", *row[1:]) for row in rows)


_HELP = ("help", "?", "this screen's help", None, "help")
_BACK = ("back", "Esc", "back", None, "back")
_CARD_HELP_KEY = ("help", "?", "this screen's help", None, "help", 2,
                  "? — this help, then the workflow guarantees below.")
_UPDATE_HELP = ("U — check for and install a newer charmer: an installed release checks, asks and "
                "applies; a Nix install updates through its flake. Claude Code itself: W → Claude Code.")
_DOCTOR_HELP = ("H — doctor: its findings as a list (BLOCKED, Attention by area, then the advisories); "
                "Enter acts on a row or shows its command, V the full report, W the gateway's actions.")
_DETAILS_HELP = "V — full details: every row, the /model lead set, the kept environment, managed differences."

ACTIONS: dict[str, tuple[Action, ...]] = {
    "card": _rows(
        "card",
        ("launch", "Enter", "launch the profile on the card (while nothing is connected: open Get started)",
         "charmer --profile <name>", "launch", 1, "Enter — launch this profile (a fresh durable session)."),
        ("launch-anyway", "L", "launch while nothing is connected yet", "charmer --profile <name>", "launch anyway", 1),
        ("update", "U", "check for and install a newer charmer", "charmer update", "update", 1, _UPDATE_HELP),
        ("edit", "E", "edit the profile on the card", "charmer profile edit <name>", "edit", 1,
         "E — edit this profile (^S saves; sessions that follow it are offered the change)."),
        ("next-profile", "Tab", "show the next profile (Shift-Tab: the previous one)", None, "next", 1,
         "Tab / Shift-Tab — next / previous profile (connected first, then most recently used)."),
        ("profiles", "P", "open Profiles", "charmer profile list", "profiles", 2,
         "P — profiles: use, create, copy, rename, delete, restore shipped versions, make one the default; "
         "B there edits named bindings; F lists fallback profiles (one provider each) for an outage."),
        ("direct", "D", "open Direct", "charmer direct <model>", "direct", 2,
         "D — direct session: one model as the lead, no agents (custom models too)."),
        ("details", "V", "every card row in full, the /model lead set and the kept environment", None, "details", 2, _DETAILS_HELP),
        ("sessions", "S", "open Sessions", "charmer sessions list", "sessions", 2,
         "S — sessions: resume, details (V), change a lineup (T), follow/pin (F), rename (R), forget (X)."),
        ("providers", "G", "open Providers", "charmer providers list", "providers", 2,
         "G — providers: Enter connects, K API key, L sign in or out, T test, X remove, P apply. A add models; N new."),
        ("models", "M", "open Models", "charmer models list", "models", 2,
         "M — models: Enter inspects a line (or admits a new one); candidates can be declared. A adds a model."),
        ("settings", "O", "open Settings", None, "settings", 2,
         "O — settings: compaction, Explore, workflow default, review rounds, kept environment, token."),
        ("doctor", "H", "doctor's findings (first-run checks while nothing is connected)", "charmer doctor", "doctor", 2,
         _DOCTOR_HELP + " While nothing is connected, H opens the first-run checks instead."),
        ("get-started", "W", "open Get started", "charmer setup", "", 3,
         "New here? W — Get started: connect a provider (API key or account sign-in) and pick a profile.",
         "Enter already opens Get started when nothing is connected; W remains a help shortcut."),
        _CARD_HELP_KEY,
        ("quit", "Esc", "leave without launching", None, "quit", 2,
         "Esc — quit without launching (in a dialog or a text field, Esc cancels it)."),
        ("previous-profile", "Shift-Tab", "show the previous profile", None, "", 2, "", None, True),
        ("cycle-profile", "← →", "show the previous or next profile", None, "", 2, "", None, True),
    ),
    "resume-card": _rows(
        "resume-card",
        ("resume", "Enter", "resume the session with the lineup shown", "charmer -r <session>", "resume", 1,
         "Enter — resume this session with the lineup shown (a resume keeps the session's lineup)."),
        ("update", "U", "check for and install a newer charmer", "charmer update", "update", 1, _UPDATE_HELP),
        ("details", "V", "every card row in full, the /model lead set and the kept environment", None, "details", 2, _DETAILS_HELP),
        ("sessions", "S", "open Sessions", "charmer sessions list", "sessions", 2,
         "S — sessions: change this session's lineup (T), follow/pin (F), rename (R), forget (X)."),
        ("doctor", "H", "doctor's findings: act on a row, the full report, the gateway's actions", "charmer doctor", "doctor", 2,
         _DOCTOR_HELP),
        _CARD_HELP_KEY,
        ("cancel", "Esc", "go back without resuming", None, "cancel", 2,
         "Esc — cancel: back without resuming (in a dialog or a text field, Esc cancels it)."),
    ),
    "sessions": _rows(
        "sessions",
        ("resume", "Enter", "resume the selected session", "charmer -r <session>", "resume", 1),
        ("fork", "N", "start a new managed session from a copy of the session's conversation",
         "charmer -r <session> --fork", "fork", 1,
         "N — fork: start a new managed session from a copy of this session's conversation (its own record and "
         "scope; the lineup this session would get at its next resume); the original is not changed and may keep "
         "running."),
        ("lineup", "T", "change the session's lineup, with its effect first", "charmer lineup --session <session>", "lineup", 1),
        ("details", "V", "identity, directory, lineup, pending change, drift and usage", "charmer sessions show <session>", "details", 1),
        ("follow", "F", "follow the session's profile or pin its lineup", "charmer lineup --session <session> follow", "follow/pin", 1),
        ("forget", "X", "forget the record and its generated scope", "charmer sessions forget <session>", "forget", 1),
        ("stop", "E", "stop a session running in the background", "charmer sessions stop <session>", "stop", 1),
        ("mark-ended", "M", "record the end of a session that exited without one", "charmer sessions mark-ended <session>", "mark ended", 1),
        ("repair", "P", "rebuild the session's generated scope", "charmer doctor --repair <session>", "repair"),
        ("rename", "R", "rename the session (shown here only)", None, "rename"),
        ("save-choice", "Q", "save a profile or direct lead for the next resume without launching", "charmer sessions choose", "save"),
        ("link", "L", "manage a native session", "charmer sessions link <runtime-id>", "link"),
        ("mark", "Space", "mark or unmark the session", None, "mark"),
        ("mark-all", "*", "mark every visible session, or clear the marks", None, "all", 3),
        ("filter", "/", "cycle session filters", "charmer sessions list --ended", "filter", 3),
        ("filters", "B", "combine directory, end, lead-choice and age filters with AND", "charmer sessions list --ended --needs-choice", "filters", 3),
        ("directories", "C", "this directory or all directories", "charmer sessions list", "all dirs", 3),
        _HELP, _BACK,
    ),
    "findings": _rows(
        "findings",
        ("act", "Enter", "act on the finding (its fixed action, asked first) or show its exact command", None, "act", 1),
        ("details", "V", "the full report, every original line", "charmer doctor -v", "details"),
        ("gateway", "W", "the gateway's actions: start, restart, service install, log, plan", "charmer gateway status", "gateway"),
        _HELP, _BACK,
        ("gateway-alias", "G", "open the gateway dialog", None, "", 2, "", None, True),
    ),
    "gateway": _rows(
        "gateway", ("act", "Enter", "open the selected gateway action", None, "act", 1),
        ("plan", "P", "show the read-only served-change plan", "charmer plan", "plan"),
        _HELP, ("back", "Esc", "close the gateway dialog", None, "close"),
    ),
    "lineup-dialog": _rows(
        "lineup-dialog",
        ("apply", "Enter", "apply the previewed change", "charmer lineup --session <session>", "apply", 1),
        ("target", "Tab", "move to the next kind of change", None, "target"),
        ("choose", "← →", "cycle the profiles or fallback providers", None, "choose", 1),
        ("pick", "Space", "list the choices of the current target", None, "pick"),
        _HELP, ("cancel", "Esc", "close without writing", None, "cancel"),
        ("previous-target", "Shift-Tab", "move to the previous target", None, "", 2, "", None, True),
    ),
    "lineup-per-agent": _rows(
        "lineup-per-agent",
        ("pick", "Enter", "pick a model for the selected agent", None, "pick model", 1),
        ("unset", "U", "unset the agent (it falls back to its native default)", None, "unset", 1),
        _HELP, _BACK,
    ),
    "direct": _rows(
        "direct",
        ("launch", "Enter", "launch the model as a direct session", "charmer direct <model>", "launch", 1),
        ("save", "Tab", "save the choice as a lead-only profile", None, "save as profile"),
        ("providers", "G", "open Providers", "charmer providers list", "providers"),
        ("effort", "← →", "choose the effort of a gateway-effort model", None, "effort", 1),
        ("gateway", "W", "start the local gateway or read its log", "charmer gateway start", "gateway"),
        _HELP, _BACK,
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
        ("primary", "Enter", "inspect a line or add/remove its optional admission badge", "charmer models show <key>", "inspect", 1),
        ("add", "A", "choose a provider and add a model", "charmer models add", "add model", 1),
        ("qualify", "Q", "run optional diagnostics with explicit consent", "charmer models qualify <key>", "diagnostics"),
        ("edit", "E", "edit a model you declared", "charmer models edit <key>", "edit", 1),
        ("remove", "X", "remove a model you declared", "charmer models rm <key>", "remove", 1),
        ("details", "V", "the line's details", "charmer models show <key>", "details"),
        _HELP, _BACK,
    ),
    "settings": _rows(
        "settings",
        ("edit", "Enter", "edit the value, open the screen it summarizes or rotate the token", None, "edit", 1),
        ("reset", "R", "reset the value to its default (Cancel is focused first)", None, "reset to default"),
        ("rounds", "← →", "change review rounds on the review-rounds row", None, "rounds", 1),
        ("providers", "G", "open Providers", "charmer providers list", "providers", 3),
        ("models", "M", "open Models", "charmer models list", "models", 3),
        _HELP, _BACK,
    ),
    "profiles": _rows(
        "profiles",
        ("use", "Enter", "use the profile for this launch", None, "use", 1),
        ("new", "N", "create a profile", "charmer profile new <name>", "new"),
        ("edit", "E", "edit the profile (or its file when it cannot be loaded)", "charmer profile edit <name>", "edit", 1),
        ("copy", "C", "copy the profile", "charmer profile duplicate <name> <new>", "copy"),
        ("rename", "R", "rename the profile", "charmer profile rename <name> <new>", "rename"),
        ("delete", "X", "delete the profile (a copy is kept)", "charmer profile rm <name>", "delete", 1),
        ("reseed", "U", "update a shipped profile, or restore one that cannot be loaded", "charmer profile reseed <name>", "reseed", 1),
        ("default", "D", "make the profile the default", "charmer profile default <name>", "default"),
        ("fallback", "F", "list only the fallback profiles", None, "fallback only"),
        ("bindings", "B", "edit the named bindings", None, "bindings"),
        ("get-started", "W", "open Get started", "charmer setup", "get started"),
        _HELP, _BACK,
    ),
    "propagation": (
        Action("propagation.apply", "Enter", "apply the saved change to running followers now", label="apply to running", rank=1,
               help="Enter (or A) — apply the saved change to the running followers now, then /reload-plugins in each"),
        Action("propagation.apply-letter", "A", "apply the saved change to running followers now", silent=True),
        *_rows("propagation",
               ("later", "L", "show what happens at the next resume without applying live", None, "later (next resume)"),
               _HELP,
               ("back", "Esc", "leave the saved change for later", None, "later", 2,
                "Esc — later: the saved change is kept; running sessions are told at their next prompt")),
    ),
    "get-started": _rows(
        "get-started",
        ("open", "Enter", "open the selected step", None, "open", 1),
        ("add-provider", "A", "add a provider", "charmer providers add", "add a provider"),
        ("profiles", "P", "open Profiles", None, "profiles"),
        ("offline-client", "F", "on the Claude Code step: verify and copy an offline Claude Code file",
         "charmer setup --claude-from <file>", "offline file", 1),
        _HELP, ("card", "Esc", "go to the launch card", None, "launch card"),
    ),
    "provider-picker": _rows(
        "provider-picker", ("connect", "Enter", "connect the selected provider", None, "connect", 1), _HELP, _BACK,
    ),
    "editor": _rows(
        "editor",
        ("change", "Enter", "change the selected row", None, "change", 1),
        ("unbind", "U", "unbind the selected agent", None, "unbind"),
        ("named-bindings", "B", "edit the named bindings", None, "named bindings"),
        ("routing", "R", "show the review routing", None, "routing"),
        ("save", "^S", "save the profile", None, "save", 1),
        ("raw-json", "^G", "edit the raw JSON in $EDITOR", None, "", 2, "", "Advanced file editing; the row editor is the primary path."),
        _HELP, _BACK,
        ("save-alias", "^O", "save the profile", None, "", 2, "", None, True),
        ("bindings-alias", "N", "edit the named bindings", None, "", 2, "", None, True),
    ),
    "form": _rows(
        "form", ("change", "Enter", "change the selected field", None, "change", 1),
        ("apply", "^S", "apply the changes to the profile being edited", None, "apply", 1),
        _HELP, ("text-help", "F1", "help while typing (? types a character)", None, "help"),
        ("cancel", "Esc", "discard these field changes", None, "cancel"),
        ("apply-alias", "^O", "apply these field changes", None, "", 2, "", None, True),
    ),
    "native-form": _rows(
        "native-form", ("choose", "← →", "choose a value", None, "choose", 1),
        ("apply", "Enter", "apply these choices", None, "apply", 1), _HELP,
        ("cancel", "Esc", "discard these choices", None, "cancel"),
        ("apply-alias", "^S", "apply these choices", None, "", 2, "", None, True),
        ("save-alias", "^O", "apply these choices", None, "", 2, "", None, True),
    ),
    "binding-picker": _rows(
        "binding-picker", ("effort", "← →", "choose the effort", None, "effort", 1),
        ("choose", "Enter", "bind the model, use a named binding or turn the slot off", None, "choose", 1),
        ("details", "V", "the model's details", None, "details"), _HELP, _BACK,
    ),
    "named-bindings": _rows(
        "named-bindings", ("edit", "Enter", "change the binding's model or effort", None, "edit", 1),
        ("add", "A", "add a named binding", None, "add"),
        ("delete", "X", "delete the named binding", None, "delete", 1), _HELP, _BACK,
    ),
    "routing": _rows("routing", _HELP, _BACK),
    "transition": _rows(
        "transition", ("confirm", "Enter", "confirm that the target process has exited", None, "confirm exited", 1),
        _HELP, ("cancel", "Esc", "cancel without writing", None, "cancel"),
    ),
    "onboarding-form": _rows(
        "onboarding-form", ("next", "Enter", "accept this field and continue", None, "next", 1),
        ("previous", "Backspace", "previous field (in text: erase first)", None, "previous"),
        ("choose", "↑↓", "choose a value on a choice step", None, "choose", 1),
        _HELP, ("text-help", "F1", "help while typing (? types a character)", None, "help"),
        ("cancel", "Esc", "cancel without saving", None, "cancel"),
        ("choose-alias", "← →", "choose a value", None, "", 2, "", None, True),
    ),
}


def bar(screen: str, ids: Iterable[str] | None = None, *, labels: Mapping[str, str] | None = None,
        ranks: Mapping[str, int] | None = None) -> tuple[tuple[str, str, int], ...]:
    """Applicable keys in table order. Ids may be local verbs or full ids."""

    def local(name: str) -> str:
        return name.removeprefix(screen + ".")

    rows = ACTIONS[screen]
    selected = {local(name) for name in ids} if ids is not None else {local(row.id) for row in rows}
    labels = {local(name): value for name, value in (labels or {}).items()}
    ranks = {local(name): value for name, value in (ranks or {}).items()}
    unknown = (selected | labels.keys() | ranks.keys()) - {local(row.id) for row in rows}
    if unknown:
        raise KeyError(f"{screen}: unknown actions {', '.join(sorted(unknown))}")
    bindings = []
    for row in rows:
        verb = local(row.id)
        if verb not in selected or row.silent or row.help_only:
            continue
        label, rank = labels.get(verb, row.label), ranks.get(verb, row.rank)
        if (not label and row.key != "Esc") or rank not in (1, 2, 3):
            raise ValueError(f"{row.id}: a bar key needs a label and rank 1, 2 or 3")
        bindings.append((row.key, label, rank))
    return tuple(bindings)


def help_text(screen: str, notes: str = "") -> str:
    """Every non-silent key, one help line each, then the screen's notes."""

    lines = []
    for row in ACTIONS[screen]:
        if row.silent:
            continue
        lines.append(row.help)
        if screen in ("card", "resume-card") and row.key == "?":
            lines.append(termtext.CHEATSHEET_HINT)
    keys = "\n".join(lines)
    return keys + ("\n\n" + notes if notes else "")


def action(action_id: str) -> Action:
    """The action with ``action_id`` (KeyError when there is none)."""

    found = next((row for row in ACTIONS.get(action_id.split(".", 1)[0], ()) if row.id == action_id), None)
    if found is None:
        raise KeyError(action_id)
    return found
