"""
context.py - Token accounting for the 65k context window.

Strategy: llama-server tells us the EXACT prompt size after every model call.
Messages added since that call (tool results, the assistant's reply) are
estimated from character counts. The next call replaces the estimate with
the exact number again, so estimation errors never pile up.
"""

import json
import math
from rich.text import Text

# Rough rule of thumb for code and English mixed: ~3.5 characters per token.
CHARS_PER_TOKEN = 3.5
MESSAGE_OVERHEAD = 4  # chat-template tokens wrapped around every message

# Tiered pressure thresholds from the design doc (fractions of the window)
PRUNE_AT = 0.60    # Phase 3: start pruning old tool outputs
COMPACT_AT = 0.85  # Phase 3: summarize the conversation
STOP_AT = 0.90     # hard stop: refuse to call the model


def estimate_tokens(messages: list[dict]) -> int:
    """Cheap offline estimate. Serializing the whole message also counts tool_calls."""
    total = 0
    for m in messages:
        chars = len(json.dumps(m, ensure_ascii=False))
        total += math.ceil(chars / CHARS_PER_TOKEN) + MESSAGE_OVERHEAD
    return total


def breakdown(messages: list[dict]) -> dict[str, int]:
    """Estimated tokens per role. Used by /context to show where the space went."""
    out: dict[str, int] = {}
    for m in messages:
        role = m.get("role", "?")
        out[role] = out.get(role, 0) + estimate_tokens([m])
    return out


class ContextTracker:
    def __init__(self, window: int):
        self.window = window
        # Exact prompt size from the last model call, and how many messages
        # the conversation had at that moment. None until the first call.
        self.known_tokens: int | None = None
        self.known_count = 0

    def record(self, prompt_tokens: int, message_count: int) -> None:
        """Called after each model call with the server's exact numbers."""
        self.known_tokens = prompt_tokens
        self.known_count = message_count

    def adjust(self, saved: int) -> None:
        """Subtraction tokens freed by pruning from the last exact count.
        Message COUNT is unchanged (we edit in place), so known_count stays valid."""
        if self.known_tokens is not None:
            self.known_tokens = max(0, self.known_tokens - saved)

    def used(self, messages: list[dict]) -> int:
        """Best current estimate of how many tokens the next call would send."""
        if self.known_tokens is None:
            return estimate_tokens(messages)  # nothing exact yet
        # exact count for the old part + estimate for whatever was added since
        return self.known_tokens + estimate_tokens(messages[self.known_count:])

    def fraction(self, messages: list[dict]) -> float:
        return self.used(messages) / self.window

    def level(self, messages: list[dict]) -> str:
        """One of: ok, prune, compact, stop."""
        f = self.fraction(messages)
        if f >= STOP_AT:
            return "stop"
        if f >= COMPACT_AT:
            return "compact"
        if f >= PRUNE_AT:
            return "prune"
        return "ok"

    def bar(self, messages: list[dict], width: int = 10) -> str:
        """Text bar like  [███▍      ] 34%  22,300 / 65,536"""
        partials = ' ▏▎▍▌▋▊▉'                       # 0/8 .. 7/8 of a cell
        f = min(self.fraction(messages), 1.0)
        units = round(f * width * 8)                # each cell = 8 units
        full, rem = divmod(units, 8)
        bar = '█' * full + (partials[rem] if rem else '')
        white_space = ' ' * (width - len(bar))
        return (f"[{bar}{white_space}] {f:.0%}  "
                f"{self.used(messages):,} / {self.window:,}")