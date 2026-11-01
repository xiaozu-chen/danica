"""
run.py - Runs every eval task, prints a results table, saves a JSON report,
and flags regressions compared to the previous run.
Run from the project root:  python -m evals.run
"""

import json
import time
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

from danica.llm import chat
from evals.tasks import TASKS

console = Console()
RESULTS_DIR = Path(__file__).parent / "results"


def load_previous() -> dict[str, bool]:
    """Return {task_name: passed} from the most recent saved run, if any."""
    files = sorted(RESULTS_DIR.glob("*.json"))
    if not files:
        return {}
    data = json.loads(files[-1].read_text(encoding="utf-8"))
    return {r["name"]: r["passed"] for r in data["results"]}


def main() -> None:
    RESULTS_DIR.mkdir(exist_ok=True)
    previous = load_previous()  # must load BEFORE we save the new run
    results = []

    table = Table(title="Danica evals")
    table.add_column("task")
    table.add_column("thinking")
    table.add_column("result")
    table.add_column("secs", justify="right")

    for task in TASKS:
        start = time.time()
        reply = chat([{"role": "user", "content": task.prompt}], thinking=task.thinking)
        elapsed = time.time() - start

        # Only the visible answer is checked, never the reasoning text
        passed = task.check(reply["content"])

        results.append({
            "name": task.name,
            "passed": passed,
            "seconds": round(elapsed, 2),
            "answer": reply["content"][:500],  # truncated, just for debugging
        })
        table.add_row(
            task.name,
            "on" if task.thinking else "off",
            "[green]PASS[/green]" if passed else "[red]FAIL[/red]",
            f"{elapsed:.1f}",
        )

    console.print(table)

    passed_count = sum(r["passed"] for r in results)
    console.print(f"\n[bold]{passed_count}/{len(results)} passed[/bold]")

    # Regression check: anything that passed last time but fails now
    regressions = [r["name"] for r in results
                   if previous.get(r["name"]) is True and not r["passed"]]
    if regressions:
        console.print(f"[bold red]REGRESSIONS:[/bold red] {', '.join(regressions)}")

    # Save this run, named by timestamp so files sort chronologically
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (RESULTS_DIR / f"{stamp}.json").write_text(
        json.dumps({"results": results}, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()