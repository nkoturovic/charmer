"""The lead prompt's rules that stand on measured client behaviour.

* An isolated agent whose worktree is gone continues in the lead's own
  checkout (``client check watch-worktree``): the recovery rule and the
  finish-the-failed-parts rule continue a worktree-isolated agent only while
  its worktree exists.
* ``/effort`` saves into the user's global settings for the model
  (``client check watch-effort``): the lead says so before the user uses it.
"""

from __future__ import annotations

import unittest

from _layout import RESOURCES_ROOT

_PROMPT = RESOURCES_ROOT / "catalog" / "prompts" / "cm-lead.md"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _section(text: str, heading: str) -> str:
    return text.split(f"## {heading}", 1)[1].split("\n## ", 1)[0]


def _bullet(section: str, start: str) -> str:
    return next(bullet for bullet in section.split("\n- ") if bullet.lstrip("- ").startswith(start))


class LeadPromptRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = _PROMPT.read_text(encoding="utf-8")

    def test_a_dead_isolated_agent_is_continued_only_while_its_worktree_exists(self) -> None:
        recovery = _flat(_bullet(_section(self.text, "Failure handling"), "You own delegated-agent recovery."))
        self.assertIn("A worktree-isolated agent is continued only while its worktree still exists "
                      "(`git -C <path> status --short` answers): a continuation whose worktree is gone runs in "
                      "your own checkout without isolation, so dispatch it fresh with its partial results instead.",
                      recovery)
        # The rule sits after the generation rule, before the workflow rule.
        self.assertLess(recovery.index("is never continued: dispatch it fresh"),
                        recovery.index("A worktree-isolated agent"))
        self.assertLess(recovery.index("A worktree-isolated agent"),
                        recovery.index("An agent a workflow runs is never continued"))

    def test_a_finished_isolated_agent_too(self) -> None:
        failures = _flat(_bullet(_section(self.text, "Failure handling"), "An agent that completes with reported"))
        self.assertIn("A finished worktree-isolated agent is continued only while its worktree exists (the "
                      "recovery rule above); otherwise dispatch fresh with its results.", failures)

    def test_effort_saves_into_the_users_settings(self) -> None:
        lineup = _flat(_section(self.text, "Lineup changes and your own model"))
        self.assertIn("`/effort` changes your effort for this session and also saves it into the user's global "
                      "settings for your model (`modelSettings.<model>.effortLevel`), where every Claude Code "
                      "session on that model that states no effort of its own starts with it; charmer's launches "
                      "and resumes state theirs. Tell the user so before they use it.", lineup)


if __name__ == "__main__":
    unittest.main()
