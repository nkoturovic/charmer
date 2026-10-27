"""Settings adapters for the ordinary export/import command services.

The file chooser supplies paths, never CLI text to execute. The commands
keep their receipts, human guard, per-origin consent and stale-plan checks.
Blank export has the CLI's stdout semantics: display JSON, without a receipt.
"""

from __future__ import annotations

import argparse
import contextlib
import io
from pathlib import Path
from typing import Any, Callable

from charmer import errors, paths, tui
from charmer.cli.commands import portability as commands
from charmer.cli.onboarding import Answers
from charmer.cli.screens import common

TITLES = {"export": "Export configuration", "import-preview": "Import preview", "import": "Import configuration"}


def run(runtime: Any, win: Any, palette: tui.Palette, action: str, *,
        background: Callable[[Any], None] | None = None) -> None:
    """Choose a file, then use the CLI service without replacing its guards."""

    exporting = action == "export"
    title = TITLES[action]
    label = "Export path (blank: show JSON):" if exporting else "Export file to import:"
    lines = (["No secrets, keys, sessions, logs or evidence are exported.",
              "A saved file records the confirmed-export receipt."] if exporting else
             ["Preview writes nothing. Imported trust is never active."])
    field = tui.TextInput(max_length=4096)
    choice = tui.Modal(title, [*lines, label], input=field,
                       buttons=(("Cancel", None), ("Export" if exporting else "Preview", "open"))).run(
                           win, palette, background=background)
    if choice != "open":
        return
    text = field.value.strip()
    if not exporting and not text:
        return
    target = (paths.home(runtime.environ) / text[2:] if text.startswith("~/") else Path(text)) if text else None
    if target is not None and not target.is_absolute():
        target = runtime.cwd / target
    writing = (exporting and target is not None) or action == "import"
    if writing and not runtime.allow_state_writes:
        tui.TextView(title, ["This run is read-only — nothing written."], palette=palette).run(win)
        return
    output = io.StringIO()
    shown = 0

    def confirm(question: str) -> bool:
        nonlocal shown
        # Every served-change line is scrollable before Cancel-focused
        # consent. No command output is parsed as authority or as a path.
        preview = output.getvalue()[shown:]
        if preview.strip():
            tui.TextView("Import preview", preview.splitlines(), palette=palette).run(win)
        shown = output.tell()
        final = question == commands.APPLY_QUESTION
        question_title = "Apply imported configuration?" if final else "Declare keyless provider?"
        body = question.removesuffix("[y/N] ").rstrip()
        return bool(tui.Modal(question_title, common.modal_lines(body, win),
                              buttons=(("Cancel", False), ("Apply" if final else "Declare", True)),
                              default=False).run(win, palette, background=background))

    args = argparse.Namespace(command="export" if exporting else "import", export_out=target,
                              import_file=target, import_apply=action == "import")
    with contextlib.redirect_stderr(output):
        try:
            commands.command(runtime, args, input_stream=Answers(confirm), output_stream=output)
        except (errors.CharmerError, OSError, ValueError) as exc:
            output.write(str(exc) + "\n")
    tui.TextView(title, output.getvalue()[shown:].splitlines(), palette=palette).run(win)
