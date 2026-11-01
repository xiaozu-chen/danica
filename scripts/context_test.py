"""
context_test.py - Offline checks for the context tracker.
Run from the project root:  python -m scripts.context_test
"""

from danica.context import ContextTracker, breakdown, estimate_tokens


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


msgs = [{"role": "system", "content": "x" * 350}]

# --- estimate ---
est = estimate_tokens(msgs)
check("estimate is in a sane range (~100 tokens for 350 chars)", 90 <= est <= 130)
check("breakdown groups by role", set(breakdown(msgs)) == {"system"})

# --- levels use a small fake window so the numbers are easy ---
t = ContextTracker(window=1000)
check("level ok when empty", t.level([]) == "ok")
t.record(650, 0)
check("65% -> prune", t.level([]) == "prune")
t.record(860, 0)
check("86% -> compact", t.level([]) == "compact")
t.record(900, 0)
check("90% -> stop", t.level([]) == "stop")

# --- exact count + estimate of newer messages ---
t = ContextTracker(window=1000)
history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "y" * 700}]
t.record(100, 1)  # server said 100 tokens when only message 0 existed
check("used = exact + estimate of newer messages",
      100 + 150 <= t.used(history) <= 100 + 260)

# --- bar ---
t = ContextTracker(window=1000)
t.record(500, 0)
check("bar is half full at 50%", "[##########----------]" in t.bar([]) and "50%" in t.bar([]))

print("\nAll good.")