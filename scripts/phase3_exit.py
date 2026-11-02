"""
phase3_exit.py - Phase 3 exit test: a long multi-file task must survive 3+
compactions without losing its goal.

Run (llama-server must be running, and %APPDATA%\\Danica\\config.toml should
not set [model] context):
    python -m scripts.phase3_exit            # window of 9000 tokens
    python -m scripts.phase3_exit 7000       # smaller window = more compactions

This script only MEASURES. It never fixes anything Danica wrote. Results vary
from run to run because the model is not deterministic: run it twice.
Takes roughly 10-25 minutes on a local model.
"""

import ast
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.markup import escape

from danica.config import APP_DIR, CONFIG
from danica.agent import Agent
from danica.compaction import is_summary
from danica.permissions import PermissionPolicy, Tier
from danica.sessions import repair
from danica.workspace import Workspace

console = Console()

FIRST_LINE = "# project: PELICAN"

# Request 1 carries the standing rules. It must stay under 400 characters,
# because the harness keeps user requests verbatim only up to that length.
GOAL = (
    "I'm building a small Python project called Inventory Tracker in this workspace. "
    "Rules for the WHOLE project: (a) standard library only, (b) the first line of EVERY "
    ".py file must be exactly `# project: PELICAN`, (c) every function needs a docstring. "
    "First, write items.py with an Item dataclass (name, qty, price) and test_items.py "
    "using unittest, then run the tests."
)

STEPS = [
    ("goal + items", GOAL),
    ("storage", "Add storage.py with save_items(path, items) and load_items(path) using JSON, plus "
                "test_storage.py. Then run all the tests."),
    ("reports", "Add reports.py with total_value(items) and low_stock(items, threshold), plus "
                "test_reports.py. Then run all the tests."),
    ("padding 1 (plain text)", "Explain in about 250 words how the modules fit together so far."),
    ("padding 2 (plain text)", "Describe in about 250 words how you would add logging to this "
                               "project without breaking any of my three rules."),
    ("padding 3 (plain text)", "Describe in about 250 words the edge cases each module still "
                               "needs tests for."),
    ("pricing", "Add pricing.py with apply_discount(item, percent) that returns a NEW Item and raises "
                "ValueError for a percent outside 0-100, plus test_pricing.py. Then run all the tests."),
    ("search", "Add search.py with find_by_name(items, text), case-insensitive, plus test_search.py. "
               "Then run all the tests."),
    ("export", "Add export.py with to_csv(items) returning a string with a header row, plus "
               "test_export.py. Then run all the tests."),
    ("audit by reading", "Use read_file to read every .py file in full, one call per file, then list "
                         "any file that breaks one of my three project rules."),
    ("fix + final run", "Fix every violation you found (if there were none, say so). Then run "
                        "`python -m unittest discover` once and report the result."),
]

RECALL = ("Without using any tools: what is the project called, and what were the three rules "
          "I gave you at the very start? Answer in one short paragraph.")

EXPECTED_MODULES = ["items", "storage", "reports", "pricing", "search", "export"]
WINDOW_DEFAULT = 9000


def py_files(ws: Workspace) -> list[Path]:
    return sorted(p for p in ws.root.rglob("*.py") if "__pycache__" not in p.parts)


def rule_checks(ws: Workspace) -> dict:
    """Inspect the files on disk. Never asks the model anything."""
    local_modules = {p.stem for p in py_files(ws)}
    out = {"bad_first_line": [], "bad_imports": [], "no_docstring": [],
           "test_no_docstring": [], "syntax_errors": []}
    for p in py_files(ws):
        name = p.relative_to(ws.root).as_posix()
        text = p.read_text(encoding="utf-8-sig", errors="replace")
        lines = text.splitlines()
        if not lines or lines[0].strip() != FIRST_LINE:
            out["bad_first_line"].append(name)
        try:
            tree = ast.parse(text)
        except SyntaxError:
            out["syntax_errors"].append(name)
            continue
        for node in ast.walk(tree):
            # Imports: only stdlib or the project's own modules are allowed
            if isinstance(node, ast.Import):
                roots = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots = [node.module.split(".")[0]]
            else:
                roots = []
            for root in roots:
                if root not in sys.stdlib_module_names and root not in local_modules:
                    out["bad_imports"].append(f"{name}: {root}")
            # Docstrings: hard rule for modules, only a warning for test files
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not ast.get_docstring(node):
                bucket = "test_no_docstring" if p.name.startswith("test_") else "no_docstring"
                out[bucket].append(f"{name}:{node.name}")
    return out


def run_unittests(ws: Workspace) -> tuple[bool, int, str]:
    """Run the project's tests ourselves. Returns (passed, tests_run, tail_of_output)."""
    proc = subprocess.run([sys.executable, "-m", "unittest", "discover"], cwd=ws.root,
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=120)
    output = (proc.stdout + proc.stderr).strip()
    m = re.search(r"Ran (\d+) tests?", output)
    return proc.returncode == 0, int(m.group(1)) if m else 0, output[-300:]


def main() -> None:
    window = int(sys.argv[1]) if len(sys.argv) > 1 else WINDOW_DEFAULT
    assert len(GOAL) < 400, "GOAL must stay under 400 characters (harvest truncates there)"

    run_dir = APP_DIR / "phase3-run"
    shutil.rmtree(run_dir, ignore_errors=True)       # every run starts clean
    ws = Workspace(run_dir / "workspace", todo_path=run_dir / "TODO.md")

    denied: list[str] = []

    def ask(tool, summary, tier, reason, can_remember) -> str:
        """Unattended permission policy: allow remembered program runs, deny the rest."""
        if tier == Tier.EXECUTE and can_remember:
            return "session"
        denied.append(f"{tier.value}: {summary[:80]}")
        return "deny"

    def on_event(kind: str, data) -> None:
        if kind == "tool_call":
            console.print(f"    > {data[0]}", style="dim", markup=False)
        elif kind == "notice":
            console.print(f"    {data}", style="yellow", markup=False)

    # Shrink only Danica's bookkeeping window, then restore it no matter what
    original = CONFIG["model"]["context"]
    CONFIG["model"]["context"] = window
    try:
        agent = Agent(ws, transcript_path=run_dir / "transcript.jsonl", on_event=on_event,
                      policy=PermissionPolicy(ask=ask), max_steps=40)

        console.print(f"Phase 3 exit test: window={window} tokens, {len(STEPS)} steps + recall\n")
        step_log, problems, history_ok = [], [], True

        for n, (label, text) in enumerate(STEPS, start=1):
            started, before = time.time(), agent.compactions
            console.print(f"[{n}/{len(STEPS)}] {label}", markup=False)
            try:
                reply = agent.run(text)
            except Exception as e:                    # a crash is a result, not a reason to quit
                reply = f"[crashed: {type(e).__name__}: {e}]"
            if reply.startswith(("[stopped", "[crashed")):
                problems.append(f"step {n} ({label}): {reply[:120]}")
            if repair(agent.messages)[1] != 0:
                history_ok = False                    # tool calls and results no longer pair up
            step_log.append({"step": n, "label": label, "seconds": round(time.time() - started, 1),
                             "compactions_during": agent.compactions - before,
                             "used_tokens": agent.tracker.used(agent.messages)})
            console.print(f"    done in {step_log[-1]['seconds']}s | compactions so far: "
                          f"{agent.compactions} | context: {agent.tracker.bar(agent.messages)}\n",
                          style="cyan", markup=False)

        # Top up with forced compactions if the model needed fewer than 3 on its own
        natural, forced = agent.compactions, 0
        while agent.compactions < 3 and forced < 3:
            if not agent.compact(300):
                break
            forced += 1

        console.print("[recall question]", markup=False)
        try:
            recall = agent.run(RECALL)
        except Exception as e:
            recall = f"[crashed: {type(e).__name__}: {e}]"
    finally:
        CONFIG["model"]["context"] = original

    # ---------------------------------------------------------------- checks
    results: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))

    rules = rule_checks(ws)
    tests_ok, tests_run, tests_tail = run_unittests(ws)
    summary_text = agent.messages[1]["content"] if len(agent.messages) > 1 and is_summary(agent.messages[1]) else ""
    low = recall.lower()

    add("3+ compactions happened", agent.compactions >= 3,
        f"{agent.compactions} total ({natural} natural, {forced} forced)")
    add("no step stopped or crashed", not problems, "; ".join(problems))
    add("history stayed valid (calls and results pair up)", history_ok)
    add("summary keeps the first request verbatim",
        " ".join(GOAL.split()) in summary_text)
    missing = [m for m in EXPECTED_MODULES if not (ws.root / f"{m}.py").exists()]
    add("all 6 modules exist", not missing, ", ".join(missing))
    add("rule (b): every file starts with the project line", not rules["bad_first_line"],
        ", ".join(rules["bad_first_line"]))
    add("rule (a): standard library only", not rules["bad_imports"], ", ".join(rules["bad_imports"]))
    add("rule (c): docstrings in non-test files", not rules["no_docstring"], ", ".join(rules["no_docstring"][:6]))
    add("every file parses", not rules["syntax_errors"], ", ".join(rules["syntax_errors"]))
    add("unittest discover passes with 6+ tests", tests_ok and tests_run >= 6,
        f"{tests_run} tests; {'ok' if tests_ok else tests_tail}")
    add("recall: names the project", "pelican" in low)
    add("recall: names the stdlib rule", any(w in low for w in ("standard library", "stdlib", "standard-library")))
    add("recall: names the docstring rule", "docstring" in low)

    table = Table(title="Phase 3 exit test")
    table.add_column("check")
    table.add_column("result")
    table.add_column("detail", overflow="fold")
    for name, ok, detail in results:
        # escape(): details contain "[stopped: ...]", which rich would treat as a style tag
        table.add_row(name, "[green]PASS[/green]" if ok else "[red]FAIL[/red]", escape(detail))
    console.print(table)

    if rules["test_no_docstring"]:
        console.print(f"warning (not scored): {len(rules['test_no_docstring'])} test function(s) "
                      f"have no docstring", style="yellow", markup=False)
    if denied:
        console.print(f"warning: {len(denied)} action(s) were denied unattended, e.g. {denied[0]}",
                      style="yellow", markup=False)
    console.print(f"\nrecall answer: {recall[:400]}", style="dim", markup=False)
    console.print(f"Inspect the run: {run_dir}  (/summary-style files are in 'summaries')", markup=False)

    passed = sum(ok for _, ok, _ in results)
    console.print(f"\n{passed}/{len(results)} checks passed", style="bold", markup=False)

    # Keep every run so you can compare them while tuning
    out_dir = Path(__file__).resolve().parent.parent / "evals" / "phase3"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (out_dir / f"{stamp}.json").write_text(json.dumps({
        "window": window, "compactions": agent.compactions, "natural": natural, "forced": forced,
        "steps": step_log, "denied": denied, "recall": recall,
        "checks": [{"name": n, "passed": ok, "detail": d} for n, ok, d in results],
    }, indent=2), encoding="utf-8")

    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()