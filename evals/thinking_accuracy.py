"""Live accuracy test for the configured binary thinking classifier.

Run: python -m evals.thinking_accuracy
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from danica.config import CONFIG
from danica.thinking import ThinkingClassifier
from evals.thinking_cases import CASES


def score(results: list[dict]) -> dict:
    if not results:
        raise ValueError("no results to score")
    tp = sum(row["expected"] and row["predicted"] for row in results)
    tn = sum(not row["expected"] and not row["predicted"] for row in results)
    fp = sum(not row["expected"] and row["predicted"] for row in results)
    fn = sum(row["expected"] and not row["predicted"] for row in results)
    return {
        "total": len(results),
        "correct": tp + tn,
        "accuracy": (tp + tn) / len(results),
        "true_positive": tp,
        "true_negative": tn,
        "false_positive": fp,
        "false_negative": fn,
        "thinking_precision": tp / (tp + fp) if tp + fp else 0.0,
        "thinking_recall": tp / (tp + fn) if tp + fn else 0.0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        help="optional JSON report path")
    args = parser.parse_args(argv)

    if len(CASES) != 30:
        print(f"ERROR: expected 30 test cases, found {len(CASES)}", file=sys.stderr)
        return 2
    config = CONFIG["thinking"]
    if not config.get("classifier_enabled", False):
        print("ERROR: live classifier evaluation requires "
              "thinking.classifier_enabled = true in config.toml.", file=sys.stderr)
        return 2

    classifier = ThinkingClassifier(config)
    results = []
    for number, (text, expected) in enumerate(CASES, start=1):
        decision = classifier.classify(text)
        if decision.source != "smollm2-135m":
            print(f"ERROR: item {number} did not receive a classifier result "
                  f"(source={decision.source}; {decision.error or 'fallback used'}). "
                  "No accuracy score is reported.", file=sys.stderr)
            return 2
        result = {
            "item": number,
            "text": text,
            "expected": expected,
            "predicted": decision.thinking,
            "correct": decision.thinking is expected,
        }
        results.append(result)
        marker = "PASS" if result["correct"] else "FAIL"
        print(f"{number:02d}/30 {marker} expected={str(expected).lower():5} "
              f"predicted={str(decision.thinking).lower():5} | {text}")

    summary = score(results)
    print(f"\nAccuracy: {summary['correct']}/{summary['total']} "
          f"({summary['accuracy']:.1%})")
    print(f"Thinking precision: {summary['thinking_precision']:.1%} | "
          f"recall: {summary['thinking_recall']:.1%}")
    print(f"False positives: {summary['false_positive']} | "
          f"false negatives: {summary['false_negative']}")

    if args.output:
        report = {
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "model": config.get("classifier_model", ""),
            "endpoint": config.get("classifier_url", ""),
            "summary": summary,
            "results": results,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                               encoding="utf-8")
        print(f"Report saved to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
