You are cleaning up the Python codebase in this repository. Your goal is to
improve readability and maintainability WITHOUT changing behavior.

## Context
- Python version: [e.g., 3.11]
- Package manager: [pip / poetry / uv / pipenv]
- Test command: [e.g., pytest]
- Areas to focus on: [e.g., src/mypackage] (or "entire repo")
- Areas to leave alone: [e.g., migrations/, generated code, vendored code]

## Before you start
1. Read the README, pyproject.toml / setup.cfg / requirements files, and
   explore the repo structure.
2. Run the tests and any configured linters to establish a baseline. Note
   pre-existing failures so they aren't mistaken for regressions.
3. Create a branch: `cleanup/<short-description>`.
4. Write a short plan of what you intend to change and wait for my approval
   before making large or risky changes.

## Tooling to use (install as dev tools only if not already configured)
- `ruff check --fix` and `ruff format` (or the project's existing
  linter/formatter such as black, isort, flake8)
- `vulture` to find dead code (treat results as candidates, not proof)
- `deptry` or `pip-check` to find unused or missing dependencies
- `mypy` or `pyright` to check types, using the project's existing config
- `radon cc` to find overly complex functions

## What to clean up
- Unused imports, variables, functions, classes, modules, and dependencies.
  Before deleting, grep for dynamic usage: getattr, importlib, entry points,
  plugin registries, Django/Flask/FastAPI routes and signals, Celery tasks,
  pytest fixtures, and __all__ exports.
- Duplicated logic that can be consolidated into shared helpers
- Long functions, deeply nested logic, and large classes or modules that
  should be split
- Unclear names; follow PEP 8 conventions
- Missing type hints on public functions; modernize hints
  (e.g., `list[str]`, `X | None`) to match the project's Python version
- Outdated idioms: `os.path` where `pathlib` is clearer, manual string
  formatting where f-strings fit, `open()` without context managers,
  mutable default arguments, bare `except:`, `== None`, unnecessary
  `list()`/`dict()` wrappers, manual loops that are clearer as comprehensions
- `print()` used for logging; replace with the `logging` module only where
  it's clearly debugging output, not CLI output
- Commented-out code and stale TODOs (list TODOs for me, don't delete them)
- Missing or outdated docstrings on public modules, classes, and functions.
  Match the existing style (Google, NumPy, or reST)
- Import ordering and circular import smells

## Rules
- Do NOT change external behavior, public APIs, function signatures used
  elsewhere, or database schemas/migrations.
- Do NOT add features or fix unrelated bugs. Report bugs instead.
- Do NOT upgrade or add runtime dependencies. Only remove ones confirmed unused.
- Do NOT reformat the entire repo in the same commit as logic changes.
  Put formatting-only changes in their own commit.
- Make small, focused commits with clear messages, one logical change each.
- Run tests and linters after each logical change. If something breaks and
  you can't fix it quickly, revert that change and note it.
- If coverage is thin around code you want to refactor, write characterization
  tests first.
- If you're unsure whether something is safe to remove or rename, leave it
  and flag it for me.

## Final deliverable
A summary containing:
1. What you changed, grouped by category
2. Files, functions, and dependencies removed
3. Test, lint, and type-check results before vs. after
4. Things you noticed but deliberately left alone (bugs, risky areas, TODOs)
5. Suggested follow-up work