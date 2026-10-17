"""Shared domain-error base; deliberately imports no charmer modules."""

from __future__ import annotations


class CharmerError(Exception):
    """Base of every charmer domain error.

    Subclasses retain their historical builtin base through multiple
    inheritance, so existing ValueError/OSError handlers keep working.
    ``remedy`` is an optional command-naming fix for entry points to print.
    """

    remedy: str | None = None

    def __init__(self, *args: object, remedy: str | None = None) -> None:
        super().__init__(*args)
        if remedy is not None:
            self.remedy = remedy



class CLIError(CharmerError, RuntimeError):
    """Actionable command or interaction failure."""
