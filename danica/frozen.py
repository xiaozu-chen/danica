"""Packaged (.exe) support: how Danica starts its own helper processes.

From source, helpers are started with `python -m danica.<module>`. In a PyInstaller build `sys.executable` is
Danica.exe itself, not Python, so those commands would just open a second Danica. Every place that starts a helper
asks THIS module for the command line, and `dispatch()` (called first thing in tui/__main__.py) makes the exe
understand three private switches:

    Danica.exe --run-module danica.subagent_worker <args...>   run a module as __main__   (subagents, plugins, screenshots, games)
    Danica.exe --run-script C:\\path\\game.py                    run a .py file as __main__   (/play)
    Danica.exe --run-code "print(1)"                           run a code string            (the file dialog)

From source nothing changes: the commands are exactly the ones used before.
"""

from __future__ import annotations

import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))          # PyInstaller sets sys.frozen on the exe


def module_cmd(module: str, script: Path | None = None, flags: tuple[str, ...] = ()) -> list[str]:
    """Command that runs `module` as a program. From source: `python <flags> <script>` if a script path is given,
    otherwise `python <flags> -m module`. Packaged: the exe's own --run-module switch (flags can't apply there)."""
    if FROZEN:
        return [sys.executable, "--run-module", module]
    if script is not None:
        return [sys.executable, *flags, str(script)]
    return [sys.executable, *flags, "-m", module]


def script_cmd(path: Path, flags: tuple[str, ...] = ()) -> list[str]:
    """Command that runs one .py file (a game) as a program."""
    if FROZEN:
        return [sys.executable, "--run-script", str(path)]
    return [sys.executable, *flags, str(path)]


def code_cmd(code: str) -> list[str]:
    """Command that runs a short piece of Python source."""
    if FROZEN:
        return [sys.executable, "--run-code", code]
    return [sys.executable, "-c", code]


def dispatch(argv: list[str]) -> None:
    """If the exe was started as a helper (one of the private switches above), run that and exit. Otherwise return
    and let the normal interface start. Only active in a packaged build, so `python -m danica.tui` is untouched."""
    if not FROZEN or len(argv) < 3 or argv[1] not in ("--run-module", "--run-script", "--run-code"):
        return
    import runpy
    switch, target, rest = argv[1], argv[2], argv[3:]
    if switch == "--run-module":
        sys.argv = [target, *rest]
        runpy.run_module(target, run_name="__main__", alter_sys=True)
    elif switch == "--run-script":
        script = str(Path(target).resolve())
        sys.path.insert(0, str(Path(script).parent))     # like `python game.py`: its own folder is importable
        sys.argv = [script, *rest]
        runpy.run_path(script, run_name="__main__")
    else:
        sys.argv = ["-c", *rest]
        exec(compile(target, "<string>", "exec"), {"__name__": "__main__"})
    raise SystemExit(0)
