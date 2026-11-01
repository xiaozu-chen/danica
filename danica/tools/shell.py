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
import subprocess

from danica.tools.registry import tool
from danica.workspace import Workspace

DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 120
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


@tool(
    "run_shell",
    "Run a Windows PowerShell command in the workspace folder. Use it to run scripts, "
    "tests and installs. Do not use it for reading or editing files; use the file tools.",
    {
        "command": {"type": "string", "description": "PowerShell command to run"},
        "timeout": {"type": "integer", "description": "Seconds before the command is killed (max 120)", "default": 30},
    },
)
def run_shell(ws: Workspace, command: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    timeout = min(max(1, timeout), MAX_TIMEOUT)

    # Force UTF-8 output from PowerShell itself, then run the model's command
    full_command = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + command

    proc = subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", full_command],
        cwd=ws.root,
        env=_clean_env(),
        stdin=subprocess.DEVNULL,           # nothing can wait for keyboard input
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,           # merge errors into the same stream
        # Own process group so we can kill the tree cleanly (Windows only flag)
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )

    timed_out = False
    try:
        raw, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        # proc.kill() only kills PowerShell, not what it started.
        # taskkill /T kills the whole tree, /F forces it.
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        raw, _ = proc.communicate()  # collect whatever was printed before the kill

    output = _cap(raw.decode("utf-8", errors="replace").strip())

    if timed_out:
        return f"[TIMED OUT after {timeout}s, process killed]\n{output}"
    return f"[exit code {proc.returncode}]\n{output}"