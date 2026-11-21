"""
shell.py - run_shell: execute a PowerShell command inside the workspace.

Protections in this file (sandbox level 1, see PROJECT.md):
  - working directory is the workspace root
  - environment variables are a whitelist, so API keys etc. don't leak in
  - hard timeout that kills the WHOLE process tree
  - stdin is closed so interactive prompts can't hang the agent
  - output is capped so a noisy command can't flood the context window
NOT protected yet: the command can still touch files outside the workspace.
Phase 2 adds permission prompts and Job Object limits.
"""

import os
import re
import subprocess

from danica.tools.registry import tool
from danica.workspace import Workspace
from danica.config import CONFIG
from danica.sandbox import Job, SandboxError

DEFAULT_TIMEOUT = 2500
MAX_TIMEOUT = 3000
MAX_OUTPUT_CHARS = 8000   # roughly 2-3k tokens

# Only these variables are passed to the child process.
ENV_ALLOWLIST = [
    "SystemRoot", "WINDIR", "COMSPEC", "PATH", "PATHEXT",
    "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMDATA",
]


def _clean_env() -> dict[str, str]:
    env = {k: os.environ[k] for k in ENV_ALLOWLIST if k in os.environ}
    # Make Python children print UTF-8 so we decode output reliably
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _cap(text: str) -> str:
    """Keep the start and end of long output (errors usually live at the end)."""
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    half = MAX_OUTPUT_CHARS // 2
    skipped = len(text) - MAX_OUTPUT_CHARS
    return f"{text[:half]}\n... [{skipped} characters truncated] ...\n{text[-half:]}"


# `python game.py` / `py -3 game.py`: the script that would run
_PY_SCRIPT = re.compile(r"\bpy(?:thon[\d.]*)?(?:\.exe)?\s+(?:-\S+\s+)*[\"']?([^\s;&|\"']+\.py)\b", re.I)


def _pygame_script(ws: Workspace, command: str) -> str | None:
    """The workspace path of the pygame game this command would start, or None. A pygame game opens a window and
    never exits, so run_shell would just sit until its timeout. (A command that sets SDL_VIDEODRIVER is headless on
    purpose and is left alone.)"""
    if "sdl_videodriver" in command.lower():
        return None
    for name in _PY_SCRIPT.findall(command):
        try:
            path = ws.resolve(name)
            head = path.read_text(encoding="utf-8", errors="ignore")[:30000] if path.is_file() else ""
        except (OSError, ValueError):
            continue
        if re.search(r"^\s*(import pygame|from pygame)", head, re.M):
            return name
    return None


@tool(
    "run_shell",
    "Run a Windows PowerShell command in the workspace folder. Use it to run scripts, "
    "tests and installs. Do not use it for reading or editing files; use the file tools "
    "and, for web pages, web_fetch.",
    {
        "command": {"type": "string", "description": "PowerShell command to run"},
        "timeout": {"type": "integer", "description": "Seconds before the command is killed (max 120)", "default": 30},
    },
)
def run_shell(ws: Workspace, command: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Run a Windows PowerShell command in the workspace folder."""
    timeout = min(max(1, timeout), MAX_TIMEOUT)

    # A pygame game would open a window and never exit: point the model at game_run (and load it, if it was off)
    game = _pygame_script(ws, command)
    if game:
        from danica import toolbook
        toolbook.load(ws, ["game_run"])
        return (f"ERROR: {game} is a pygame game. Running it with run_shell opens a window and never exits, so it was NOT run. "
                f"Use the game_run tool instead (it is loaded now): game_run(game=\"{game}\", frames=120) plays it headless and "
                f"returns a picture, crashes with a traceback and printed text. The user can open it in a real window with /play.")

    # Force UTF-8 output from PowerShell itself, then run the model's command
    full_command = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + command

    limits = CONFIG["sandbox"]
    try:
        job = Job(memory_mb=limits["memory_mb"], max_processes=limits["max_processes"])
    except SandboxError as e:
        # Fail closed: no sandbox, no command
        return f"ERROR: the sandbox could not start, so the command was NOT run: {e}"

    timed_out = False
    # Leaving this block kills anything the command left running in the background
    with job:
        try:
            proc = job.popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", full_command],
                cwd=ws.root,
                env=_clean_env(),
                stdin=subprocess.DEVNULL,           # nothing can wait for keyboard input
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,           # merge errors into the same stream
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        except SandboxError as e:
            return f"ERROR: could not confine the command, so it was NOT run: {e}"

        try:
            raw, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            job.kill()                   # whole tree, even reparented grandchildren
            raw, _ = proc.communicate()  # collect whatever was printed before the kill

    output = _cap(raw.decode("utf-8", errors="replace").strip())

    if timed_out:
        return f"[TIMED OUT after {timeout}s, process killed]\n{output}"

    # Tell the model WHY it ran out of memory, or it will assume its code is buggy
    low = output.lower()
    if proc.returncode != 0 and ("memoryerror" in low or "out of memory" in low
                                 or "insufficient memory" in low):
        output += (f"\n[NOTE: commands run under a {limits['memory_mb']} MB memory limit. "
                   f"Use less memory, for example by processing data in chunks.]")
    return f"[exit code {proc.returncode}]\n{output}"