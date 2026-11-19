"""
context_guard.py - decide whether a loaded session fits the model's context window, and shrink it
deterministically (no model call) as a last resort.

Pure functions, no Danica imports, so it is easy to test. Agent.context_report() and
Agent.fit_to_window() are the thin wrappers that use it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

LOAD_TARGET = 0.60       # a loaded history may fill at most this share of the window, so there is
                         # still room for the next request, the tool results and the model's answer
FIXED_LIMIT = 0.50       # if the system prompt + tool schemas alone exceed this share, the window is
                         # too small to work in at all
ESTIMATE_MARGIN = 1.25   # characters / 4 under-counts code and non-English text, so be conservative
TRUNC_MARK = "\n[... truncated to fit the context window ...]\n"


@dataclass(frozen=True)
class ContextReport:
    """Summarize context usage and the action recommended by the guard."""

    window: int          # the server's real n_ctx
    fixed: int           # system prompt + tool schemas (cannot be shrunk), estimated tokens
    history: int         # everything else, estimated tokens

    @property
    def used(self) -> int:
        """Return the current token estimate."""
        return self.fixed + self.history

    @property
    def budget(self) -> int:
        """Return the configured context token budget."""
        return int(self.window * LOAD_TARGET)

    @property
    def recommended_window(self) -> int:
        """Smallest window (rounded up to a multiple of 4096, never below 8192) that works."""
        need = int(self.fixed / (FIXED_LIMIT * 0.7))
        return max(8192, -(-need // 4096) * 4096)

    @property
    def status(self) -> str:
        """Return a short label for the current context pressure."""
        if self.fixed > self.window * FIXED_LIMIT:
            return "too_small"           # even an empty session barely fits: raise llama-server -c
        if self.used > self.budget:
            return "over"                # the history is too big for this window: shrink it
        return "ok"


def truncate_to_budget(
    messages: list[dict], is_over: Callable[[], bool], floor: int = 300
) -> int:
    """Last resort. Repeatedly halve the single biggest text message (keeping its start and end)
    until is_over() is False or nothing is left to cut. Never touches messages[0] (the system
    prompt) or tool_calls arguments (cutting JSON would break it). Returns how many cuts were made."""
    cuts = 0
    for _ in range(200):
        if not is_over():
            break
        index, size = -1, floor * 2          # only messages that can really get shorter qualify
        for i, message in enumerate(messages[1:], start=1):
            content = message.get("content")
            if isinstance(content, str) and len(content) > size:
                index, size = i, len(content)
        if index < 0:
            break
        content = messages[index]["content"]
        keep = max(floor, len(content) // 2)
        head = keep * 2 // 3
        tail = keep - head
        messages[index] = {**messages[index], "content": content[:head] + TRUNC_MARK + content[-tail:]}
        cuts += 1
    return cuts
