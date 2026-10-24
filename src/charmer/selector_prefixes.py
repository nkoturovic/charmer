"""The name spaces of the gateway selectors charmer's catalog defines.

A stdlib leaf with no imports, so a caller that must not load the catalog
(the status-line renderer, which Claude Code runs every few seconds) shares
the one definition with it: ``catalog`` imports these names, nothing here
imports ``catalog``.
"""

from __future__ import annotations

# Every gateway selector of a non-Anthropic catalog line: ``charmer-<rest>``,
# or ``gpt-multi-<rest>`` for the ChatGPT account pool's lines. An Anthropic
# line's selector is its canonical wire id, never in either name space.
CHARMER_SELECTOR_PREFIX = "charmer-"
CODEX_POOL_SELECTOR_PREFIX = "gpt-multi-"
GATEWAY_SELECTOR_PREFIXES = (CHARMER_SELECTOR_PREFIX, CODEX_POOL_SELECTOR_PREFIX)
