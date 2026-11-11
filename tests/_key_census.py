"""Read handled keys from screen loops without importing or running them."""
from __future__ import annotations

import ast
from pathlib import Path

from _layout import REPO_ROOT

# Each migrated screen names its own loop; shared navigation is not an action.
LOOPS = {"providers": ("src/charmer/cli/screens/providers.py", "_ProvidersScreen", "run")}
NAVIGATION = frozenset({"up", "down", "home", "end", "pageup", "pagedown", "resize"})
KIND_KEYS = {"enter": "Enter", "esc": "Esc", "tab": "Tab", "btab": "Shift-Tab",
             "left": "← →", "right": "← →", "f1": "F1", "backspace": "Backspace"}


def _strings(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return [value for item in node.elts for value in _strings(item)]
    return []


def _subject(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name) and node.id in ("letter", "ch"):
        return "ch"
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "key":
        return node.attr if node.attr in ("kind", "ch") else None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("upper", "lower"):
        return _subject(node.func.value)
    return None


def handled_keys(source: str, class_name: str, method: str = "run") -> set[str]:
    tree = ast.parse(source)
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    loop = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == method)
    keys: set[str] = set()
    for branch in ast.walk(loop):
        if not isinstance(branch, (ast.If, ast.IfExp)):
            continue
        kinds, chars = set(), set()
        for test in ast.walk(branch.test):
            if not isinstance(test, ast.Compare) or len(test.ops) != 1 or not isinstance(test.ops[0], (ast.Eq, ast.In)):
                continue
            subject = _subject(test.left)
            if subject is not None:
                (kinds if subject == "kind" else chars).update(_strings(test.comparators[0]))
        if "ctrl" in kinds:
            keys.update("^" + ch.upper() for ch in chars if ch != "c")
            continue
        keys.update(KIND_KEYS.get(kind, kind) for kind in kinds - NAVIGATION - {"char", "ctrl"})
        keys.update("Space" if ch == " " else ch.upper() for ch in chars if ch not in ("j", "k"))
    return keys


def screen_keys(screen: str, root: Path = REPO_ROOT) -> set[str]:
    path, class_name, method = LOOPS[screen]
    return handled_keys((root / path).read_text(), class_name, method)


def unmapped(screen: str, handled: set[str], rows) -> list[str]:
    declared = {row.key for row in rows}
    return [f"{screen} handles {key}: not in its bar candidates nor its help" for key in sorted(handled - declared)]
