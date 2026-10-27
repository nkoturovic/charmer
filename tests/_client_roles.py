"""The Claude Code clients a release's real-client lanes use, by role.

* The candidate (N): the packaged pin (``catalog/native-contract.json``),
  staged into the private HOME by ``tools/test.py --claude PATH``.
* The previous release's pin (N-1), named by ``CHARMER_TEST_PREVIOUS_CLAUDE``:
  the downgrade-resume control, so the rollback a user can actually make
  (this release back to the previous one) is the pair that is proven.
* The fallback control, named by ``CHARMER_TEST_FALLBACK_CONTROL_CLAUDE``: an
  older client that still has the behaviours the gateway-fallback checks
  show the pin removed (the caching beta, the resent changing MCP call).

Each control is an absolute path accepted only at its recorded sha256; the
runner checks a named control before any test runs, and the tests check it
again where they use it. ``CONTROL_CLIENTS`` stays a literal: the runner reads
it without importing this module.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

PREVIOUS_ENV = "CHARMER_TEST_PREVIOUS_CLAUDE"
FALLBACK_CONTROL_ENV = "CHARMER_TEST_FALLBACK_CONTROL_CLAUDE"
CONTROL_CLIENTS = {
    "CHARMER_TEST_PREVIOUS_CLAUDE": ("2.1.310", "b73676430af12f97316a798b8371f81d640f90a58bdcb8f3ce56a835bb47df56"),
    "CHARMER_TEST_FALLBACK_CONTROL_CLAUDE": (
        "2.1.302", "60d7eb7f12fbb28a52533fe8be5884097403562aa02dc73991a021c7067e29a5"),
}
PREVIOUS = CONTROL_CLIENTS[PREVIOUS_ENV]
FALLBACK_CONTROL = CONTROL_CLIENTS[FALLBACK_CONTROL_ENV]


def named_control(variable: str, environ: Mapping[str, str] | None = None):
    """The control client ``variable`` names when it is that exact build (an
    absolute path to a regular file with the recorded sha256), else None."""

    from charmer import probe

    version, sha256 = CONTROL_CLIENTS[variable]
    named = (os.environ if environ is None else environ).get(variable, "")
    if not named or not os.path.isabs(named):
        return None
    path = Path(os.path.realpath(named))
    if not path.is_file() or probe._sha256_file(path) != sha256:
        return None
    return probe.TrustedExecutable(path, sha256)
