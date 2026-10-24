"""``charmer statusline subagents|main``: the status lines Claude Code runs.

A plumbing command in the class of ``session-event``: Claude Code runs it,
a person never needs to. Every managed scope compiles ``subagentStatusLine``
to the stable shim ``<state>/bin/charmer-statusline``, which runs
``charmer statusline subagents`` (unless a settings layer of the user's own
defines that key); ``main`` is the renderer a user may name in their own
``statusLine``. It is dispatched before the command-line runtime is built:
``charmer.entrypoints`` routes the exact argv the shim passes before this
package loads, and ``cli.entry.main`` routes every other spelling right
after the parse. It reads stdin, writes the lines to stdout and exits 0
(:mod:`charmer.statusline`); the subagent rows read no catalog, state or
configuration, and nothing is written.
"""

from __future__ import annotations

from charmer import statusline

KINDS = statusline.KINDS


def run(kind: str, *, stdin: object = None, stdout: object = None, environ: object = None) -> int:
    """Render one status line from stdin; always 0."""

    return statusline.run(kind, stdin, stdout, environ)
