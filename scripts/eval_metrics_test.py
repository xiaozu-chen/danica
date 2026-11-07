"""Regression gates for latency/success comparisons."""

from evals.metrics import compare_to_baseline, summarize


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


baseline = summarize([
    {"name": "a", "passed": True, "seconds": 10},
    {"name": "b", "passed": True, "seconds": 20},
])
faster = summarize([
    {"name": "a", "passed": True, "seconds": 8},
    {"name": "b", "passed": True, "seconds": 18},
])
slower = summarize([
    {"name": "a", "passed": True, "seconds": 10},
    {"name": "b", "passed": False, "seconds": 1},
])
more_tasks_lower_rate = summarize([
    {"name": "a", "passed": True, "seconds": 10},
    {"name": "b", "passed": True, "seconds": 10},
    {"name": "c", "passed": True, "seconds": 10},
    {"name": "d", "passed": False, "seconds": 10},
])
check("summary reports success and latency percentiles",
      baseline["passed"] == 2 and baseline["median_seconds"] == 15
      and baseline["p95_seconds"] == 20)
check("promotion requires measured speedup without per-task regression",
      compare_to_baseline(baseline, faster)["eligible"]
      and not compare_to_baseline(baseline, faster, task_regressions=["b"])["eligible"]
      and not compare_to_baseline(baseline, slower)["eligible"]
      and not compare_to_baseline(baseline, more_tasks_lower_rate)["eligible"])
check("a first run establishes a baseline rather than claiming improvement",
      not compare_to_baseline(None, faster)["eligible"])

print("\nAll good.")
