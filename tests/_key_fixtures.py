"""Enumerated offline screen states for the key-table census.

Bars come from the real screen selectors (or their draw method when the
selector is inline), never from asking the action table for every key.
"""
from __future__ import annotations

import contextlib
import dataclasses
import io
import re
from dataclasses import dataclass
from unittest import mock

from charmer import catalog, cli, operator, sessions, tui, views
from charmer.cli import text as cli_text
from charmer.cli.screens import (
    actions, direct, findings, gateway_actions, get_started, launch_sessions,
    models, profiles, propagation, providers, settings, transition,
)

import _tui_fixture as fx
import _tui_render as render
from test_tui import FakeWindow

WIDTHS = (80, 100, 120)
# This prefix is a status sentence, not a handled key or action candidate.
BAR_NOTES = {"settings": frozenset({cli_text.SETTINGS_READ_ONLY_KEY})}


def _all_keys(screen):
    return {row.key for row in actions.ACTIONS[screen]}


@dataclass(frozen=True)
class Case:
    screen: str
    state: str
    bar: tui.KeyBar
    active: frozenset[str] | None = None
    enter_duplicates: frozenset[str] = frozenset()

    def __post_init__(self):
        if self.active is None and self.screen in actions.ACTIONS:
            # Unconditional widgets offer the whole table. Contextual
            # screens below enumerate their applicable actions explicitly.
            object.__setattr__(self, "active", frozenset(row.key for row in actions.ACTIONS[self.screen]))


@dataclass(frozen=True)
class Frame:
    screen: str
    state: str
    width: int
    bindings: tuple[tuple[str, str, int], ...]
    parts: tuple[str, ...]
    active: frozenset[str] | None = None
    enter_duplicates: frozenset[str] = frozenset()

    @property
    def drawn(self) -> set[str]:
        keys = set()
        for key, label, _rank in self.bindings:
            if f"{key} {label}".strip() in self.parts:
                keys.add(key)
            elif key in ("?", "F1") and any(re.fullmatch(re.escape(key) + r" [1-9][0-9]* more", part)
                                            for part in self.parts):
                keys.add(key)
        return keys


def draw(case: Case, width: int) -> Frame:
    win = FakeWindow([], width=width, height=24)
    case.bar.draw(win, 23, tui.MONO_PALETTE)
    # Match whole bindings, not substrings ("A add" must not match "^A add"
    # or "A add another"). A wrapped bar still has complete binding units.
    parts = tuple(part for line in win.text().splitlines() if line.strip()
                  for part in line.strip().split(" · "))
    bindings = tuple((key, label, rank) for (key, label), rank in zip(case.bar.bindings, case.bar.ranks))
    return Frame(case.screen, case.state, width, bindings, parts, case.active, case.enter_duplicates)


def reachability_errors(screen, rows, frames):
    candidates = {row.key for row in rows if not row.silent and not row.help_only}
    drawn = set().union(*(frame.drawn for frame in frames))
    return [f"{screen} {key}: no fixture draws this bar candidate" for key in sorted(candidates - drawn)]


def availability_errors(screen, rows, frames):
    """Every distinct applicable action must be drawn in that fixture state.

    Overflow can move a key into help at a narrower width, but a different
    row cannot supply evidence for this one. Only an exact Enter duplicate
    (not an action reached through another choice) or an explicit help-only
    key may be absent at every enumerated width.
    """

    documented = {row.key for row in rows if row.silent or row.help_only}
    states = {}
    for frame in frames:
        if frame.active is not None:
            states.setdefault(frame.state, []).append(frame)
    errors = []
    for state, variants in states.items():
        active, duplicates = variants[0].active, variants[0].enter_duplicates
        drawn = set().union(*(frame.drawn for frame in variants))
        if duplicates and "Enter" not in drawn:
            errors.append(f"{screen} Enter ({state}): duplicate bindings have no drawn primary action")
        for key in sorted(active - documented - duplicates - drawn):
            errors.append(f"{screen} {key} ({state}): handled action is neither drawn nor an Enter duplicate")
    return errors


def frame_errors(frame, rows):
    candidates = {row.key: row for row in rows if not row.silent and not row.help_only}
    errors = []
    for key, _label, rank in frame.bindings:
        where = f"{frame.screen} {key} ({frame.state}, {frame.width} columns)"
        if key in BAR_NOTES.get(frame.screen, ()):
            continue
        if key not in candidates:
            errors.append(f"{where}: bar key has no non-silent bar candidate")
        elif (rank == 1 or candidates[key].rank == 1) and key not in frame.drawn:
            errors.append(f"{where}: applicable rank-1 key is hidden")
    return errors


def help_errors(screen, rows, text):
    lines = text.splitlines()
    return [f"{screen} {row.key}: help line is missing" for row in rows if not row.silent and row.help not in lines]


def screen_helps():
    """The actual help strings used by the screens, not regenerated expectations."""
    return {
        "card": launch_sessions.CARD_HELP, "resume-card": launch_sessions.RESUME_HELP,
        "sessions": launch_sessions.SESSIONS_HELP, "lineup-dialog": launch_sessions.LINEUP_HELP,
        "lineup-per-agent": launch_sessions.PER_AGENT_HELP,
        "providers": providers.HELP, "profiles": profiles.HELP, "propagation": propagation.HELP,
        "models": models.HELP, "direct": direct.HELP, "settings": settings.HELP, "findings": findings.HELP,
        "gateway": gateway_actions.HELP, "get-started": get_started.HELP,
        "provider-picker": get_started.PICKER_HELP, "transition": transition.HELP,
        "editor": tui.PROFILE_EDITOR_HELP, "form": tui.PROFILE_FORM_GENERAL_HELP,
        "native-form": tui.PROFILE_FORM_NATIVE_HELP, "binding-picker": tui.BINDING_PICKER_HELP,
        "named-bindings": tui.NAMED_BINDINGS_HELP, "routing": tui.ROUTING_HELP,
        "onboarding-form": tui.ONBOARDING_HELP,
    }


def _drawn_bar(draw_screen):
    """Capture an inline selector by drawing the real screen, without keys that act."""
    bars = []
    with mock.patch.object(tui.KeyBar, "draw", lambda bar, *_args, **_kwargs: bars.append(bar)):
        draw_screen(FakeWindow(["\x1b"], width=120, height=40))
    assert len(bars) == 1, f"fixture must draw exactly one bar, got {len(bars)}"
    return bars[0]


def _provider_actions(screen, fact):
    # These are the handlers' row conditions, independent of the bar selector.
    # In particular, opening an account menu is NOT the same action as X's
    # sign-out. Only V/details, K/set-key and L/account repeat Enter exactly.
    active = _all_keys("providers") - {"Enter", "P", "A", "E", "L", "K", "X", "T", "Space", "V"}
    duplicates = set()
    if screen.apply_offer:
        active.add("P")
    if fact is not None:
        active.update(("Enter", "A", "Space", "V"))
        if screen._own(fact):
            active.update(("E", "X"))
        if screen._account_capable(fact):
            active.add("L")
        if screen._key_choice(fact) or (not screen._account(fact) and not screen._keyless(fact)):
            active.add("K")
        connection = screen.connections.get(str(fact["id"]))
        if screen._account(fact) and connection is not None and connection.accounts:
            active.add("X")
        if not screen._account(fact) and not screen._keyless(fact) and str(fact.get("credential", "")).endswith(" present"):
            active.add("X")
        if str(fact["id"]) in screen.testable:
            active.add("T")
        duplicates = {"details": {"V"}, "set-key": {"K"}, "account": {"L"}}.get(screen._primary(fact), set())
    return frozenset(active), frozenset(duplicates)


def _provider_cases(runtime):
    screen = providers._ProvidersScreen(runtime, palette=tui.MONO_PALETTE, journal=lambda: None)
    original = screen.facts
    connections = screen.connections
    account = next(f for f in original if f["kind"] == "oauth-pool")
    keyed = next(f for f in original if f["kind"] != "oauth-pool" and f.get("auth") != "none")
    variants = {
        "account": account,
        "account-signed-in": account,
        "account-signed-in-no-key-choice": account,
        "account-key": dict(account, kind="direct-anthropic", auth="bearer", credential="EXAMPLE_KEY present"),
        "key-set": keyed,
        "key-missing": dict(keyed, credential="EXAMPLE_KEY missing"),
        "own-unapproved": dict(keyed, source="operator", route="unapproved"),
        "own-changed-missing": dict(keyed, source="operator", route="changed", credential="EXAMPLE_KEY missing"),
        "own-keyless": dict(keyed, source="operator", kind="openai-compat", auth="none", route="unapproved"),
        "keyless": dict(keyed, kind="openai-compat", auth="none"),
        "empty": None,
    }
    for name, fact in variants.items():
        screen.connections = dict(connections)
        if name.startswith("account-signed-in"):
            pid = str(fact["id"])
            screen.connections[pid] = dataclasses.replace(connections[pid], accounts=("fixture@example.com",))
        choice = (mock.patch.object(screen, "_key_choice", return_value=False)
                  if name.endswith("no-key-choice") else contextlib.nullcontext())
        with choice:
            for down in (False, True):
                for apply in (False, True):
                    screen.facts = [fact] if fact is not None else []
                    screen.selected = 0
                    screen.gateway_down, screen.apply_offer = down, apply
                    screen.testable = {f["id"] for f in original}
                    active, duplicates = _provider_actions(screen, fact)
                    yield Case("providers", f"{name}/down={down}/apply={apply}", screen._keybar(), active, duplicates)


def _profile_cases(runtime):
    screen = profiles._ProfilesScreen(runtime, tui.MONO_PALETTE, now=fx.FIXED_NOW)
    seed = next(row for row in screen.rows if row.origin == "seed" and row.loadable)
    yours = dataclasses.replace(seed, name="fixture-profile", origin="yours", seed_state=None, is_default=False)
    variants = {
        "seed-current": seed, "seed-edited": dataclasses.replace(seed, seed_state="edited"),
        "seed-stale": dataclasses.replace(seed, seed_state="unedited-stale", seed_stale=True),
        "yours": yours, "yours-unloadable": dataclasses.replace(yours, loadable=False),
        "seed-unloadable": dataclasses.replace(seed, loadable=False), "empty": None,
    }
    for name, row in variants.items():
        for filtered in (False, True):
            screen.rows, screen.index, screen.fallback_only = (() if row is None else (row,)), 0, filtered
            active = _all_keys("profiles") - {"Enter", "E", "C", "R", "X", "U", "D"}
            if row is not None:
                active.add("E")
                if not row.loadable:
                    active.add("U" if row.origin == "seed" else "X")
                else:
                    active.update(("Enter", "C", "D"))
                    if row.origin != "seed":
                        active.update(("R", "X"))
                    elif row.seed_state != "current":
                        active.add("U")
            yield Case("profiles", f"{name}/fallback={filtered}", screen._keybar(), frozenset(active))


def _card_cases(runtime, record):
    target = render.profile_target(runtime, catalog.DEFAULT_SEED)
    card = render.card_screen(runtime, target)
    for connected in (True, False):
        for hint in (None, ("1.0.0", "1.0.1")):
            card.not_connected, card.update_hint = not connected, hint
            card._replan()
            active = _all_keys("card") - {"L", "U"}
            if not connected:
                active.add("L")
            if hint is not None:
                active.add("U")
            yield Case("card", f"connected={connected}/update={hint is not None}", card._keybar(), frozenset(active),
                       frozenset({"W"}) if not connected else frozenset())
    prepared = runtime.prepare(cli._record_resume_target(record), action="resume", passthrough=[],
                               session_id=fx.FIXED_ID)
    card = render.card_screen(runtime, prepared.target, action="resume", prepared=prepared)
    for hint in (None, ("1.0.0", "1.0.1")):
        card.update_hint = hint
        card._replan()
        active = _all_keys("resume-card") - ({"U"} if hint is None else set())
        yield Case("resume-card", f"update={hint is not None}", card._keybar(), frozenset(active))


def _session_cases(runtime):
    screen = launch_sessions._SessionsScreen(runtime, palette=tui.MONO_PALETTE, now=fx.FIXED_NOW)
    base = _all_keys("sessions") - {"E", "M", "P"}
    for root in (False, True):
        for all_dirs in (False, True):
            screen.root, screen.cwd_filter = root, not all_dirs
            for index, record in enumerate(screen.records):
                screen.selected = index
                state = screen.states[record["managed_id"]]
                active = set(base)
                if state == "live":
                    active.add("E")
                elif record.get("last_event_source") != "end":
                    active.add("M")
                if record.get("version") == sessions.RECORD_VERSION:
                    active.add("P")
                yield Case("sessions", f"{state}/root={root}/all-dirs={all_dirs}", screen._keybar(), frozenset(active))
            screen.marked = {fx.FIXED_ID, fx.OTHER_ID}
            for index, record in enumerate(screen.records):
                screen.selected = index
                state = screen.states[record["managed_id"]]
                active = base | {"M"}
                if state == "live":
                    active.add("E")
                if record.get("version") == sessions.RECORD_VERSION:
                    active.add("P")
                yield Case("sessions", f"marked/{state}/root={root}/all-dirs={all_dirs}", screen._keybar(), frozenset(active))
            screen.marked.clear()
    screen.records = []
    screen.selected = 0
    yield Case("sessions", "empty", screen._keybar(), frozenset(base - {"Enter", "N", "T", "V", "F", "X", "R", "Q", "Space", "*"}))


def _settings_cases(runtime):
    screen = settings._SettingsScreen(runtime, tui.MONO_PALETTE, card_lineup=render.default_lineup(runtime),
                                       tty_in=io.StringIO(), tty_out=io.StringIO())
    original = runtime.allow_state_writes
    try:
        for writable in (False, True):
            runtime.allow_state_writes = writable
            for index, (kind, row) in enumerate(screen.entries):
                screen.index = index
                name = row.key if kind == "row" else str(kind)
                active = _all_keys("settings") - {"Enter", "R", "← →"}
                if kind == "row":
                    if row.edit in views.SETTINGS_NAVIGATION_EDITS or (row.edit == "token" and writable):
                        active.add("Enter")
                    elif screen._can_edit(row) or (row.key == views.SETTINGS_ENV_KEEP_KEY and row.editable and writable):
                        active.update(("Enter", "R"))
                        if row.key == "review_round_cap":
                            active.add("← →")
                yield Case("settings", f"{name}/writable={writable}", screen._keybar(), frozenset(active))
    finally:
        runtime.allow_state_writes = original


def _direct_cases(runtime):
    for purpose in (None, "link"):
        screen = direct._DirectScreen(runtime, palette=tui.MONO_PALETTE, purpose=purpose)
        served = screen.served
        for down in (False, True):
            screen.served = None if down else served
            base = (_all_keys("direct") - {"Tab", "G", "W", "← →"}) | ({"G", "Tab"} if purpose is None else set()) | ({"W"} if down else set())
            for index, row in enumerate(screen.rows):
                screen.selected = index
                active = base | ({"← →"} if row.mode == "gateway" and screen._shows_effort() and row.efforts else set())
                yield Case("direct", f"{row.key}/purpose={purpose}/down={down}", screen._keybar(), frozenset(active))
            original = screen.rows
            screen.rows = []
            yield Case("direct", f"empty/purpose={purpose}/down={down}", screen._keybar(), frozenset(base - {"Enter", "Tab"}))
            screen.rows = original


def _editor_cases(runtime):
    state = render.editor_state(runtime)
    editor = tui.ProfileEditorScreen(state, palette=tui.MONO_PALETTE)
    yield Case("editor", "profile", _drawn_bar(editor._draw))
    form = tui._GeneralForm(state, tui.MONO_PALETTE)
    for index, field in enumerate(form.FIELDS):
        form.focus = index
        # ? types in a text field; elsewhere it is the displayed binding
        # for the same help action that F1 also opens.
        active = _all_keys("form") - ({"?"} if field in form.inputs else {"F1"})
        yield Case("form", field, _drawn_bar(form._draw), frozenset(active))
    native = tui._NativeForm(state, tui.MONO_PALETTE)
    yield Case("native-form", "choices", _drawn_bar(native._draw))
    rows = views.line_rows(state.cat, state.effective, custom_ids=frozenset())
    model = views.picker_rows(rows, slot="cm-implementer", bindings=state.bindings, lcat=state.cat,
                             eff=state.effective, current=state.slot_binding("cm-implementer"))
    picker = tui.BindingPicker(model, palette=tui.MONO_PALETTE)
    yield Case("binding-picker", "agent", _drawn_bar(picker._draw))
    bindings = tui.NamedBindingsScreen(state.cat, state.effective, palette=tui.MONO_PALETTE, bindings=state.bindings)
    yield Case("named-bindings", "list", _drawn_bar(bindings._draw))
    routing = tui.RoutingPreviewScreen(render.default_lineup(runtime), name=catalog.DEFAULT_SEED, palette=tui.MONO_PALETTE)
    yield Case("routing", "profile", _drawn_bar(routing._draw))
    onboarding = tui.OnboardingForm("new provider", views.provider_form_fields(), palette=tui.MONO_PALETTE)
    for index, (name, _label, _default, choices) in enumerate(onboarding.fields):
        onboarding.index = index
        active = _all_keys("onboarding-form") - ({"F1"} if choices else {"?", "↑↓"})
        yield Case("onboarding-form", name, _drawn_bar(onboarding._draw), frozenset(active))


def _model_cases(runtime):
    # A checked-in declaration fixture supplies real operator lifecycle rows;
    # nothing approves a route, supplies a key or contacts its endpoint.
    import test_operator

    operator.write_provider_file(runtime.environ, "acme", test_operator._fixture_files()["acme"])
    screen = models._ModelsScreen(runtime, palette=tui.MONO_PALETTE)
    model = screen.model(120)
    for index, key in enumerate(screen.keys):
        screen.index = index
        active = _all_keys("models") - {"Enter", "V", "Q", "E", "X"}
        if key is not None:
            active.update(("Enter", "V"))
            if key in screen.lifecycle:
                active.update(("Q", "E"))
            if key != "__candidates__" and key not in runtime.catalog.docs["models-v2"]["models"]:
                active.add("X")
        yield Case("models", key or "heading", screen._keybar(model), frozenset(active))
        if key in screen.lifecycle:
            admitted = dataclasses.replace(model, admitted=frozenset((*model.admitted, key)))
            yield Case("models", f"{key}/admitted", screen._keybar(admitted), frozenset(active))
            screen.banner = "fixture: settings unavailable"
            yield Case("models", f"{key}/read-only", screen._keybar(model), frozenset(active))
            screen.banner = None


def cases(runtime):
    """Yield snapshots immediately: the next fixture may move the same screen."""
    runtime.profiles.install_seeds()
    record = fx.v4_record(runtime, catalog.DEFAULT_SEED, managed_id=fx.FIXED_ID, title="fixture live")
    fx.v4_record(runtime, catalog.DEFAULT_SEED, managed_id=fx.OTHER_ID, title="fixture stopped")
    fx.v4_record(runtime, catalog.DEFAULT_SEED, managed_id=fx.THIRD_ID, ended=True, title="fixture ended")
    runtime.live_prefixes = frozenset({fx.FIXED_ID[:8]})
    yield from _card_cases(runtime, record)
    yield from _session_cases(runtime)
    yield from _provider_cases(runtime)
    yield from _profile_cases(runtime)
    yield from _settings_cases(runtime)
    yield from _direct_cases(runtime)
    yield from _editor_cases(runtime)

    screen = findings.FindingsScreen(runtime, palette=tui.MONO_PALETTE, report=lambda: ([], [], []),
                                     tty_in=io.StringIO(), tty_out=io.StringIO())
    yield Case("findings", "empty report", screen._keybar())
    yield Case("gateway", "fixture observation", _drawn_bar(lambda win: gateway_actions.dialog(runtime, win, tui.MONO_PALETTE)))
    dialog = launch_sessions._LineupDialog(runtime, record, palette=tui.MONO_PALETTE, liveness="unknown")
    for target in dialog.TARGETS:
        dialog.target = target
        dialog._preview()
        yield Case("lineup-dialog", target, _drawn_bar(dialog._draw))
    per_agent = launch_sessions._PerAgentScreen(runtime, record, palette=tui.MONO_PALETTE,
                                                eff=runtime.current_effective())
    yield Case("lineup-per-agent", "agents", _drawn_bar(per_agent._draw))
    prop = propagation._PropagationScreen(runtime, tui.MONO_PALETTE, [catalog.DEFAULT_SEED])
    yield Case("propagation", "running follower", tui.KeyBar(prop.keybar()))
    prop.states = {mid: "ended" for mid in prop.states}
    yield Case("propagation", "no running followers", tui.KeyBar(prop.keybar()), frozenset(_all_keys("propagation") - {"A"}))
    started = get_started._GetStartedScreen(runtime, tui.MONO_PALETTE)
    for index, step in enumerate(started.state.steps):
        started.selected = index
        active = _all_keys("get-started") - ({"F"} if step.id != "claude" else set())
        yield Case("get-started", step.id, _drawn_bar(started._draw), frozenset(active))
    for only in (None, "other"):
        picker = get_started._ProviderPicker(runtime, tui.MONO_PALETTE, only=only)
        yield Case("provider-picker", f"only={only}", _drawn_bar(picker._draw))
    confirm = transition._TransitionScreen(["fixture lineup change"], palette=tui.MONO_PALETTE)
    yield Case("transition", "confirmation", _drawn_bar(confirm._draw))
    yield from _model_cases(runtime)
