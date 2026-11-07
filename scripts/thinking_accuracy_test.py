"""Offline checks for the 30-item live thinking-classifier evaluation."""

from evals.thinking_accuracy import score
from evals.thinking_cases import CASES, SYSTEM_PROMPT


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


check("evaluation has exactly 30 unique ChatML user prompts",
      len(CASES) == 30 and len({text for text, _ in CASES}) == 30
      and "thinking_mode" in SYSTEM_PROMPT)
check("evaluation is balanced across both binary labels",
      sum(expected for _, expected in CASES) == 15
      and sum(not expected for _, expected in CASES) == 15)

summary = score([
    {"expected": expected, "predicted": expected}
    for _, expected in CASES
])
check("perfect predictions score 100 percent",
      summary["correct"] == 30 and summary["accuracy"] == 1
      and summary["false_positive"] == summary["false_negative"] == 0)

summary = score([
    {"expected": expected, "predicted": not expected}
    for _, expected in CASES
])
check("inverted predictions report all false positives and negatives",
      summary["correct"] == 0 and summary["accuracy"] == 0
      and summary["false_positive"] == summary["false_negative"] == 15)

print("\nAll good.")
