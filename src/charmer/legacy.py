"""The names this program had before it was charmer.

charmer was released as ``claude-multi`` up to 1.1.0. charmer never runs
under those names, never writes them and never verifies a release made
under them; it only has to recognise them where something recorded before
the rename can still reach it. This module is the one place in the package
that spells them (the repository's name census holds every other file to
the new names), and each constant says who reads it and how long it stays.

Standard library only, no imports: a leaf any module (and the test runner,
which reads ``ENVIRONMENT`` without importing it) can use.
"""

# The earlier product name and the prefix of its environment variables: the
# name its commands, roots, units, shims and markers were spelled with. The
# tests translate the historical reference gateway unit with them; the name
# census allows the old spellings nowhere else in the package. Stay while
# anything recorded under the old name can reach charmer.
NAME = "claude-multi"
ENVIRONMENT_PREFIX = "CLAUDE_MULTI_"
# Every spelling of the earlier name: the command and path form, the package
# form, the environment form and the class-name form. The repository's name
# census and the documentation rule against the former name search for
# exactly these.
SPELLINGS = ("claude-multi", "claude_multi", "CLAUDE_MULTI", "ClaudeMulti")

# The prefix of the gateway selectors up to catalog 38; catalog 39 serves
# each of them as ``charmer-<rest>``. The catalog checks every renamed
# selector against it (``retired.json`` ``renamed``), and the fixture
# catalog recipe rewrites its frozen catalog-32 input with it. Stays while
# a shipped catalog carries renamed selectors for recorded sessions.
SELECTOR_PREFIX = "claude-multi-"

# The variables the old commands exported into a session or set in their
# wrappers. charmer reads none of them; the test runner clears them so a
# shell started inside an old session never leaks them into a run. Stays
# while old installations can still start sessions.
ENVIRONMENT = (
    "CLAUDE_MULTI_ASSETS",
    "CLAUDE_MULTI_CHANNEL",
    "CLAUDE_MULTI_GATEWAY",
    "CLAUDE_MULTI_HOOK_COMMAND",
    "CLAUDE_MULTI_INHIBITION_TOKEN",
    "CLAUDE_MULTI_LAUNCH_EPOCH",
    "CLAUDE_MULTI_MANAGED_ID",
    "CLAUDE_MULTI_PROXY_BIN",
    "CLAUDE_MULTI_PROXY_PATCHES",
    "CLAUDE_MULTI_REGISTRY_DIR",
    "CLAUDE_MULTI_SECRET_ENV",
    "CLAUDE_MULTI_SESSION_ID",
)
