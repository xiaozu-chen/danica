"""
perf.py - small timing helpers, so "it feels slow" can become numbers. See /perf.

  mark("name")        remember how long after Danica's start this moment was (startup phases)
  record_call(...)    llm.stream_chat leaves one entry per model call (time to first token, tokens, cache hits)
Nothing here talks to the network or the disk, and it costs microseconds.
"""

import collections
import time

START = time.perf_counter()
MARKS: list[tuple[str, float]] = []                 # (phase, seconds since start)
CALLS: collections.deque = collections.deque(maxlen=40)


def mark(name: str) -> None:
    """Remember this moment under `name` (only the first time: later sessions do not repeat the start-up phases)."""
    if not any(n == name for n, _ in MARKS):
        MARKS.append((name, time.perf_counter() - START))


def record_call(**entry) -> None:
    entry["at"] = time.time()
    CALLS.append(entry)


def startup_lines() -> list[str]:
    out, last = [], 0.0
    for name, at in MARKS:
        out.append(f"  {at:6.2f}s  (+{at - last:5.2f}s)  {name}")
        last = at
    return out


def call_lines(count: int = 10) -> list[str]:
    """One line per recent model call: what it was, how long until the first word, how much of the prompt was NEW work."""
    out = []
    for c in list(CALLS)[-count:]:
        cached, new = c.get("cache_n"), c.get("prompt_n")
        prompt = c.get("prompt_tokens", 0)
        reuse = (f"cache reused {cached:,} / new {new:,}" if cached is not None and new is not None
                 else f"prompt {prompt:,} tokens")
        out.append(f"  {c['label']:<8} first word {c['first']:5.1f}s  total {c['total']:5.1f}s  {reuse}  "
                   f"answer {c.get('completion_tokens', 0):,} tokens  tools {c.get('tools_chars', 0) // 4:,}t"
                   + ("  thinking" if c.get("thinking") else ""))
    return out
