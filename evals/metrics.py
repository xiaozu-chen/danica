"""Comparable aggregate metrics for task-success and latency eval runs."""

import math
import statistics


def summarize(results: list[dict]) -> dict:
    if not results:
        raise ValueError("eval run has no task results")
    durations = [max(0.0, float(row["seconds"])) for row in results]
    passed = sum(bool(row["passed"]) for row in results)
    ordered = sorted(durations)
    p95_index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * 0.95) - 1))
    return {
        "passed": passed,
        "total": len(results),
        "success_rate": passed / len(results),
        "mean_seconds": statistics.fmean(durations),
        "median_seconds": statistics.median(durations),
        "p95_seconds": ordered[p95_index],
    }


def compare_to_baseline(baseline: dict | None, candidate: dict,
                        minimum_speedup: float = 0.05,
                        task_regressions: list[str] | None = None) -> dict:
    if baseline is None:
        return {"eligible": False, "reason": "no baseline run"}
    task_regressions = task_regressions or []
    no_success_regression = (
        candidate["success_rate"] >= baseline["success_rate"] and not task_regressions
    )
    baseline_median = float(baseline["median_seconds"])
    candidate_median = float(candidate["median_seconds"])
    speedup = 0.0 if baseline_median <= 0 else 1 - candidate_median / baseline_median
    eligible = no_success_regression and speedup >= minimum_speedup
    reason = ("meets success and latency thresholds" if eligible else
              "task success regressed" if not no_success_regression else
              f"median latency improvement below {minimum_speedup:.0%}")
    return {"eligible": eligible, "success_no_regression": no_success_regression,
            "task_regressions": task_regressions,
            "median_speedup": speedup, "reason": reason}
