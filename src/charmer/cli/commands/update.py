"""The ``update`` command: updating charmer itself.

``charmer update`` checks for a newer release, shows its plan, asks and
installs it; ``--check`` only checks, ``--rollback`` returns to the previous
version. The journey (and its refusals and exit codes) is
:mod:`charmer.release_update`, shared with the card's U. ``--verify`` is a
separate read-only entry through :mod:`charmer.install_verify`, before Runtime.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Mapping, TextIO, TYPE_CHECKING

from charmer import install_verify, release_update, strict_json, termtext

if TYPE_CHECKING:
    import charmer.cli.runtime as runtime_mod


def _verify_command(args: argparse.Namespace, *, environ: Mapping[str, str],
                    output_stream: TextIO, error_stream: TextIO, state_root: Path | None = None) -> int:
    """The read-only entry: one shared result, disclosure before any fetch."""

    def disclose(text: str) -> None:
        error_stream.write(termtext.visible_text(text) + "\n")
        error_stream.flush()

    directory = getattr(args, "update_from_dir", None)
    result = install_verify.verify_current(environ=environ, state_root=state_root,
                                          from_dir=Path(directory) if directory is not None else None,
                                          disclose=disclose)
    if args.json:
        output_stream.write(strict_json.pretty_file_bytes(result.document()).decode("utf-8"))
    else:
        output_stream.write("\n".join(termtext.visible_text(line) for line in result.lines()) + "\n")
    output_stream.flush()
    return result.exit_code


def _interactive(input_stream: TextIO | None, output_stream: TextIO) -> bool:
    try:
        return bool(input_stream is not None and input_stream.isatty() and output_stream.isatty())
    except (AttributeError, ValueError):
        return False


def _update_command(
    runtime: runtime_mod.Runtime,
    args: argparse.Namespace,
    *,
    output_stream: TextIO,
    input_stream: TextIO | None = None,
) -> int:
    """Check, plan, confirm and apply an update, or roll back (exit codes in
    :mod:`charmer.release_update`)."""

    mode = "rollback" if getattr(args, "update_rollback", False) else (
        "check" if getattr(args, "update_check", False) else "update")
    journey = release_update.journey_for(runtime)
    ask = release_update.terminal_ask(input_stream, output_stream,
                                      interactive=_interactive(input_stream, output_stream))
    return release_update.run(journey, mode=mode, assume_yes=bool(getattr(args, "update_yes", False)), ask=ask,
                              out=output_stream, from_dir=getattr(args, "update_from_dir", None),
                              base_url=getattr(args, "update_base_url", None))
