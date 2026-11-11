"""A small, source-only census of the TUI's key-loop idioms.

This is deliberately not a Python evaluator: comparisons, literal key
collections, dispatch dictionaries and widget shortcuts are supported.
An unfamiliar key expression fails closed instead of silently losing keys.
"""
from __future__ import annotations

import ast
from pathlib import Path

from _layout import REPO_ROOT

_TUI = "src/charmer/tui.py"
_SCREENS = "src/charmer/cli/screens/"

# (source, class/qualified owner, method); an empty owner names a function.
LOOPS = {
    "card": (_SCREENS + "launch_sessions.py", "_LaunchCardScreen", "run"),
    "resume-card": (_SCREENS + "launch_sessions.py", "_LaunchCardScreen", "run"),
    "sessions": (_SCREENS + "launch_sessions.py", "_SessionsScreen", "run"),
    "lineup-dialog": (_SCREENS + "launch_sessions.py", "_LineupDialog", "run"),
    "lineup-per-agent": (_SCREENS + "launch_sessions.py", "_PerAgentScreen", "run"),
    "providers": (_SCREENS + "providers.py", "_ProvidersScreen", "run"),
    "profiles": (_SCREENS + "profiles.py", "_ProfilesScreen", "run"),
    "propagation": (_SCREENS + "propagation.py", "_PropagationScreen", "run"),
    "models": (_SCREENS + "models.py", "_ModelsScreen", "run"),
    "direct": (_SCREENS + "direct.py", "_DirectScreen", "run"),
    "settings": (_SCREENS + "settings.py", "_SettingsScreen", "run"),
    "findings": (_SCREENS + "findings.py", "FindingsScreen", "run"),
    "gateway": (_SCREENS + "gateway_actions.py", "", "dialog"),
    "get-started": (_SCREENS + "get_started.py", "_GetStartedScreen", "run"),
    "provider-picker": (_SCREENS + "get_started.py", "_ProviderPicker", "run"),
    "transition": (_SCREENS + "transition.py", "_TransitionScreen", "run"),
    "editor": (_TUI, "ProfileEditorScreen", "run"),
    "binding-picker": (_TUI, "BindingPicker", "run"),
    "named-bindings": (_TUI, "NamedBindingsScreen", "run"),
    "form": (_TUI, "_GeneralForm", "run"),
    "native-form": (_TUI, "_NativeForm", "run"),
    "routing": (_TUI, "RoutingPreviewScreen", "run"),
    "onboarding-form": (_TUI, "OnboardingForm", "_run"),
}

# These widgets have fixed bars/buttons, checked separately, not exemptions.
FIXED_LOOPS = {
    "select-list": (_TUI, "SelectList", "run"),
    "modal": (_TUI, "Modal", "run"),
    "text-view": (_TUI, "TextView", "run"),
    "text-report": (_SCREENS + "common.py", "_TextReportScreen", "run"),
    "scroll-modal": (_SCREENS + "common.py", "_ScrollModal", "run"),
    "listing-consent": (_SCREENS + "providers.py", "_ListingConsent", "run"),
    "step-view": (_SCREENS + "get_started.py", "_StepView", "run"),
}
# Gateway delegates its read to Modal. The other two widget callers supply
# literal shortcuts to generic loops; inventory every shortcuts= call too.
DELEGATED_LOOPS = (LOOPS["gateway"],)
SHORTCUT_LOOPS = {
    "select-list": (_SCREENS + "common.py", "OnboardingActions", "add_models"),
    "step-view": (_SCREENS + "get_started.py", "_ProfileStepView", "run"),
}

# Raw navigation is declared once. Only lowercase, *unfolded* j/k scroll;
# K (including key.ch.upper() == "K") remains an action, e.g. set a key.
NAVIGATION = {
    "kind": frozenset({"up", "down", "home", "end", "pageup", "pagedown", "resize"}),
    "ch": frozenset({"j", "k"}),
    "ctrl": frozenset({"c"}),
}
KIND_KEYS = {"enter": "Enter", "esc": "Esc", "tab": "Tab", "btab": "Shift-Tab",
             "left": "← →", "right": "← →", "f1": "F1", "backspace": "Backspace"}
_FOCUS_NAVIGATION = frozenset({"Tab", "Shift-Tab"})
SCREEN_NAVIGATION = {screen: _FOCUS_NAVIGATION for screen in ("editor", "form", "native-form")}
FIXED_NAVIGATION = {
    "modal": _FOCUS_NAVIGATION | {"← →"},
    "step-view": _FOCUS_NAVIGATION,
    "scroll-modal": frozenset({"Space"}),  # same operation as PageDown, not an action
}
# A dormant callback retained for widget compatibility, not navigation. The
# source census asserts no product caller supplies it; any new caller must
# remove this exception and supply a real bar (or a mapped action table).
FIXED_SILENT = {"select-list": {"P": ("on_prefer", "unused compatibility callback; no product caller")}}

# Checked against the actual resume guards, not inferred from the table.
CARD_FRESH_KEYS = frozenset({"E", "P", "D", "G", "M", "O", "L", "W", "Tab", "Shift-Tab", "← →"})


def _call_name(node):
    return node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else ""


def _walk(node):
    """Nodes owned by this function, never a nested function's handlers."""
    yield node
    for child in ast.iter_child_nodes(node):
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            yield from _walk(child)


def _functions(node, parents=()):
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield (".".join(parents), child.name, child)
        scopes = parents + (child.name,) if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else parents
        yield from _functions(child, scopes)


def read_key_loops(source: str, path: str) -> dict[tuple[str, str, str], int]:
    """Inventory the whole source, including helpers not named ``run``.

    Counting reads also catches a second loop added inside a mapped method.
    The decoder's own non-loop recursion is not a screen reading input.
    """
    readers = {}
    tree = ast.parse(source)
    for owner, method, node in [("", "<module>", tree), *_functions(tree)]:
        calls = [item for item in _walk(node) if isinstance(item, ast.Call) and _call_name(item.func) == "read_key"]
        if path == _TUI and not owner and method == "read_key":
            assert len(calls) == 1 and isinstance(calls[0].func, ast.Name), "read_key decoder changed: review its recursion"
            assert any(isinstance(item, ast.Return) and item.value is calls[0] for item in _walk(node)), "decoder recursion must return its key"
            assert not any(calls[0] in tuple(_walk(item)) for item in _walk(node)
                           if isinstance(item, (ast.For, ast.While))), "decoder gained a key loop"
            continue
        if calls:
            readers[path, owner, method] = len(calls)
    return readers


def _label(loop):
    path, owner, method = loop
    return f"{path}:{owner + '.' if owner else ''}{method}"


def loop_errors(readers, mapped) -> list[str]:
    errors = [f"{_label(loop)} reads a key: no screen or fixed-bar mapping" for loop in sorted(readers.keys() - mapped)]
    errors += [f"{_label(loop)} mapped but no longer reads a key" for loop in sorted(mapped - readers.keys())]
    errors += [f"{_label(loop)} has {count} key reads: split or explicitly map its loops"
               for loop, count in sorted(readers.items()) if loop in mapped and count != 1]
    return errors


def shortcut_owners(source: str, path: str) -> set[tuple[str, str, str]]:
    tree = ast.parse(source)
    return {(path, owner, method) for owner, method, node in [("", "<module>", tree), *_functions(tree)]
            if any(isinstance(item, ast.keyword) and item.arg == "shortcuts" for item in _walk(node))}


def _scope(source, owner, method):
    tree = ast.parse(source)
    node = next((node for cls, name, node in _functions(tree) if (cls, name) == (owner, method)), None)
    assert node is not None, f"missing key loop {owner}.{method}"
    # Only module/class constants and assignments local to the chosen method.
    bindings = {}
    scopes = [tree]
    if owner:
        scopes += [item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == owner]
    assignments = [item for scope in scopes for item in scope.body] + list(_walk(node))
    for item in assignments:
        if isinstance(item, (ast.Assign, ast.AnnAssign)) and item.value is not None:
            targets = item.targets if isinstance(item, ast.Assign) else [item.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    bindings[target.id] = item.value
    return node, bindings


def _resolve(node, bindings, seen=frozenset()):
    name = node.id if isinstance(node, ast.Name) else None
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
        name = node.attr
    if name in bindings and name not in seen:
        return _resolve(bindings[name], bindings, seen | {name})
    return node


def _subject(node, bindings, seen=frozenset()):
    if isinstance(node, ast.Name) and node.id in bindings and node.id not in seen:
        return _subject(bindings[node.id], bindings, seen | {node.id})
    if isinstance(node, ast.IfExp):
        return _subject(node.body, bindings, seen)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("upper", "lower"):
        subject = _subject(node.func.value, bindings, seen)
        return (subject[0], True) if subject else None
    if isinstance(node, ast.Attribute) and node.attr in ("kind", "ch"):
        value = _resolve(node.value, bindings)
        if (isinstance(value, ast.Call) and _call_name(value.func) == "read_key"
                or isinstance(node.value, ast.Name) and node.value.id == "key"):
            return node.attr, False
    return None


def _literals(node, bindings, *, membership=False):
    node = _resolve(node, bindings)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return list(node.value) if membership else [node.value]
    if isinstance(node, (ast.Tuple, ast.List, ast.Set, ast.Dict)):
        items = node.keys if isinstance(node, ast.Dict) else node.elts
        return [value for item in items for value in _literals(item, bindings)]
    raise AssertionError(f"cannot resolve key literal: {ast.unparse(node) if node is not None else '**expansion'}")


def _canonical(values, subject="ch", *, folded=False):
    result = set()
    for value in values:
        if value in NAVIGATION.get(subject, ()) and not (subject == "ch" and folded):
            continue
        if subject == "kind":
            if value not in ("char", "ctrl"):
                result.add(KIND_KEYS.get(value, value))
        elif subject == "ctrl":
            result.add("^" + value.upper())
        else:
            result.add("Space" if value == " " else value.upper())
    return result


def _requires_ctrl(node, bindings):
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
        return any(_requires_ctrl(value, bindings) for value in node.values)
    return (isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq)
            and _subject(node.left, bindings) == ("kind", False)
            and isinstance(node.comparators[0], ast.Constant) and node.comparators[0].value == "ctrl")


def _dynamic(node):
    return isinstance(node, (ast.Name, ast.Attribute)) and _call_name(node) == "shortcuts"


def _condition_keys(node, bindings, *, ctrl=False, dynamic=False):
    if isinstance(node, ast.BoolOp):
        ctrl = ctrl or _requires_ctrl(node, bindings)
        return set().union(*(_condition_keys(value, bindings, ctrl=ctrl, dynamic=dynamic) for value in node.values))
    if not isinstance(node, ast.Compare) or len(node.ops) != 1 or not isinstance(node.ops[0], (ast.Eq, ast.In)):
        return set()
    subject = _subject(node.left, bindings)
    if subject is None or (dynamic and _dynamic(_resolve(node.comparators[0], bindings))):
        return set()
    kind, folded = subject
    values = _literals(node.comparators[0], bindings, membership=isinstance(node.ops[0], ast.In))
    return _canonical(values, "ctrl" if ctrl and kind == "ch" else kind, folded=folded)


def handled_keys(source: str, class_name: str, method: str = "run", *, dynamic_shortcuts=False) -> set[str]:
    node, bindings = _scope(source, class_name, method)
    keys = set()

    def visit(item, ctrl=False):
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            return
        if isinstance(item, (ast.If, ast.IfExp)):
            keys.update(_condition_keys(item.test, bindings, ctrl=ctrl, dynamic=dynamic_shortcuts))
            body = item.body if isinstance(item, ast.If) else [item.body]
            other = item.orelse if isinstance(item, ast.If) else [item.orelse]
            for child in body:
                visit(child, ctrl or _requires_ctrl(item.test, bindings))
            for child in other:
                visit(child, ctrl)
            return
        if isinstance(item, (ast.BoolOp, ast.Compare)):
            keys.update(_condition_keys(item, bindings, ctrl=ctrl, dynamic=dynamic_shortcuts))
            return
        if isinstance(item, ast.Subscript) and _subject(item.slice, bindings):
            if not (dynamic_shortcuts and _dynamic(_resolve(item.value, bindings))):
                keys.update(_canonical(_literals(item.value, bindings), folded=True))
        if (isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute) and item.func.attr == "get"
                and item.args and _subject(item.args[0], bindings)):
            keys.update(_canonical(_literals(item.func.value, bindings), folded=True))
        if isinstance(item, ast.keyword) and item.arg == "shortcuts":
            keys.update(_canonical(_literals(item.value, bindings), folded=True))
        for child in ast.iter_child_nodes(item):
            visit(child, ctrl)

    for statement in node.body:
        visit(statement)
    return keys


def literal_keywords(source, owner, method, *, keyword):
    node, bindings = _scope(source, owner, method)
    return [ast.literal_eval(_resolve(item.value, bindings)) for item in _walk(node)
            if isinstance(item, ast.keyword) and item.arg == keyword]


def card_fresh_keys(source, owner="_LaunchCardScreen") -> set[str]:
    """The shared handler's explicit fresh-only guards (not a flow analyzer)."""
    node, bindings = _scope(source, owner, "run")
    keys = set()
    for branch in _walk(node):
        if not isinstance(branch, ast.If):
            continue
        terms = branch.test.values if isinstance(branch.test, ast.BoolOp) and isinstance(branch.test.op, ast.And) else [branch.test]
        resume = lambda item: (isinstance(item, ast.Attribute) and item.attr == "resume"
                               and isinstance(item.value, ast.Name) and item.value.id == "self")
        fresh_guard = any(isinstance(term, ast.UnaryOp) and isinstance(term.op, ast.Not) and resume(term.operand) for term in terms)
        resume_skip = any(resume(term) for term in terms) and len(branch.body) == 1 and isinstance(branch.body[0], ast.Continue)
        if fresh_guard or resume_skip:
            keys.update(_condition_keys(branch.test, bindings))
    return keys


def screen_keys(screen: str, root: Path = REPO_ROOT) -> set[str]:
    path, owner, method = LOOPS[screen]
    keys = handled_keys((root / path).read_text(), owner, method)
    if screen == "gateway":
        keys |= fixed_keys("modal", root) - FIXED_NAVIGATION["modal"]
    if screen == "resume-card":
        keys -= CARD_FRESH_KEYS
    return keys - SCREEN_NAVIGATION.get(screen, frozenset())


def fixed_keys(widget: str, root: Path = REPO_ROOT) -> set[str]:
    path, owner, method = FIXED_LOOPS[widget]
    keys = handled_keys((root / path).read_text(), owner, method,
                        dynamic_shortcuts=widget in ("modal", "select-list", "step-view"))
    if widget in SHORTCUT_LOOPS:
        path, owner, method = SHORTCUT_LOOPS[widget]
        keys |= handled_keys((root / path).read_text(), owner, method)
    return keys


def unmapped(screen: str, handled: set[str], rows) -> list[str]:
    declared = {row.key for row in rows}
    return [f"{screen} handles {key}: not in its bar candidates nor its help" for key in sorted(handled - declared)]


def fixed_unmapped(widget: str, handled: set[str], bar_keys: set[str]) -> list[str]:
    missing = handled - bar_keys - FIXED_NAVIGATION.get(widget, frozenset())
    return [f"{widget} handles {key}: not in its fixed bar or navigation" for key in sorted(missing)]
