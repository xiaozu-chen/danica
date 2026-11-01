"""
cli.py - The terminal frontend.
Run from the project root:  python -m danica.cli
Commands: /think (toggle thinking), /ws (show workspace path), /exit
Ctrl+C stops the current task. Ctrl+D or /exit quits.
"""

from datetime import datetime

from prompt_toolkit import PromptSession, prompt
from prompt_toolkit.history import FileHistory
from prompt_toolkit.formatted_text import HTML
from rich.console import Console

from danica.agent import Agent
from danica.config import APP_DIR, CONFIG
from danica.workspace import Workspace
from danica.context import breakdown
from danica.permissions import PermissionPolicy, Tier
from danica.audit import AuditLog, verify
from danica.checkpoints import CheckpointError, Checkpoints
from danica.tools.todo import read_todo

console = Console()


def make_event_printer():
    """Build the on_event callback. Tracks whether we're mid-line so tool
    lines always start on a fresh line."""
    state = {"mid_line": False}

    def newline_if_needed() -> None:
        if state["mid_line"]:
            console.print()
            state["mid_line"] = False

    def on_event(kind: str, data) -> None:
        # markup=False: model text can contain [brackets] that rich would
        # otherwise try to interpret as styling tags.
        if kind == "thinking":
            console.print(data, style="dim", end="", markup=False, highlight=False)
            state["mid_line"] = True
        elif kind == "content":
            console.print(data, end="", markup=False, highlight=False)
            state["mid_line"] = True
        elif kind == "tool_call":
            newline_if_needed()
            name, args = data
            console.print(f"> {name} {args}"[:300], style="cyan", markup=False, highlight=False)
        elif kind == "tool_result":
            preview = str(data)
            if len(preview) > 400:
                preview = preview[:400] + f"... ({len(str(data)) - 400} more chars)"
            console.print(preview, style="dim", markup=False, highlight=False)
        elif kind == "notice":
            newline_if_needed()
            console.print(data, style="yellow", markup=False)

    def finish() -> None:
        newline_if_needed()

    return on_event, finish


def make_ask(finish):
    """Build the callback that asks the user for permission in the terminal."""

    def ask(tool: str, summary: str, tier: Tier, reason: str, can_remember: bool) -> str:
        finish() # make sure we're on a fresh line after streamed text
        console.print(f"Permission needed ({tier.value}): {reason}", style="bold yellow", markup=False)
        console.print(summary, style="yellow", markup=False, highlight=False)
        options = "[y] once  [s] this session  [n] deny" if can_remember else "[y] once [n] deny"
        while True:
            try:
                answer = prompt(f"{options} > ").strip().lower()
            except EOFError:
                return "deny" # Ctrl+D = no
            if answer in ("y", "yes"):
                return "once"
            if answer in ("s", "session") and can_remember:
                return "session"
            if answer in ("n", "no", ""): # Enter alone = deny (safe default)
                return "deny"
    return ask


def main() -> None:
    # Every run is a new session: sessions/<id>/{workspace/, transcript.jsonl}
    session_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    session_dir = APP_DIR / "sessions" / session_id
    ws = Workspace(session_dir / "workspace", todo_path=session_dir / "TODO.md")

    # One audit log for ALL sessions, so the chain spans everything Danica ever did
    audit = AuditLog(APP_DIR / "logs" / "audit.jsonl", session=session_id)
    audit.append("session_start", workspace=str(ws.root))

    # Shadow git repo lives OUTSIDE the workspace, so the model can never touch it

    checkpoints = Checkpoints(ws, session_dir / "checkpoints" / "git")
    if not checkpoints.available:
        console.print("git was not found, so /undo and /    rewind are disabled. "
                      "Install it with: winget install Git.Git", style="yellow", markup=False)

    on_event, finish = make_event_printer()
    policy = PermissionPolicy(ask=make_ask(finish))
    agent = Agent(
        ws,
        transcript_path=session_dir / "transcript.jsonl",
        on_event=on_event,
        policy=policy,
        audit=audit,
        checkpoints=checkpoints,
    )

    # FileHistory gives up-arrow recall across restarts
    session = PromptSession(history=FileHistory(str(APP_DIR / "history.txt")))

    def toolbar():
        """Bottom-of-screen context bar, colored by pressure level."""
        level = agent.tracker.level(agent.messages)
        color = {"ok": "ansigreen", "prune": "ansiyellow", "compact": "ansiyellow", "stop": "ansired"}[level]
        mode = "think" if agent.thinking else "fast"
        if policy.full_access:
            mode += " | FULL ACCESS" # loud on purpose
        return HTML(f'<style fg="{color}">ctx {agent.tracker.bar(agent.messages)}</style> |{mode}')

    console.print("[bold]Danica[/bold] ready. /think toggles thinking, /ws shows the workspace, /context to show usage /exit quits.")
    console.print(f"Workspace: {ws.root}", style="dim", markup=False)

    while True:
        try:
            text = session.prompt("you> ", bottom_toolbar=toolbar).strip()
        except KeyboardInterrupt:
            continue          # Ctrl+C at the prompt just clears the line
        except EOFError:
            break             # Ctrl+D quits

        if not text:
            continue

        # --- slash commands (more arrive in later phases) ---
        if text == "/exit":
            break
        if text == "/think":
            agent.thinking = not agent.thinking
            console.print(f"thinking: {'on' if agent.thinking else 'off'}", style="yellow")
            continue
        if text == "/ws":
            console.print(str(ws.root), markup=False)
            continue
        if text == "/context":
            console.print(agent.tracker.bar(agent.messages), markup=False)
            for role, tokens in sorted(breakdown(agent.messages).items(), key=lambda kv: -kv[1]):
                console.print(f"  {role:<10} ~{tokens:,} tokens", markup=False)
            continue
        if text == "/audit":
            ok, count, problem = verify(audit.path)
            if ok:
                console.print(f"audit log OK: {count} entries. chain intact", style="green", markup=False)
            else:
                console.print(f"AUDIT LOG PROBLEM: {problem}", style="bold red", markup=False)
            continue
        if text == "/fullaccess":
            policy.full_access = not policy.full_access
            audit.append("full_access", enabled=policy.full_access)
            if policy.full_access:
                console.print("FULL ACCESS ON: Danica can run ANY command without asking. /fullaccess again to turn it off.", style="bold red", markup=False)
            else:
                console.print("full access off", style="yellow", markup=False)
            continue
        if text.split()[0] in ("/undo", "/rewind"):
            if not checkpoints.available:
                console.print("checkpoints are disabled (git not found)", style="yellow", markup=False)
                continue
            parts = text.split()
            try:
                if parts[0] == "/undo":
                    summary, safety = checkpoints.undo()
                elif len(parts) == 1:
                    # "/rewind" on its own just lists recent checkpoints
                    for ident, when, label in checkpoints.list_checkpoints():
                        console.print(f"{ident} {when} {label}", markup=False, highlight=False)
                    continue
                else:
                    summary, safety = checkpoints.rewind(parts[1])
                console.print(summary, style="green", markup=False)
                if safety:
                    audit.append("restore", command=parts[0], safety=safety)
            except CheckpointError as e:
                console.print(f"ERROR: {e}", style="red", markup=False)
            continue
        if text == "/sandbox":
            sb = CONFIG["sandbox"]
            console.print(
                "Shell sandbox\n"
                "  level 1: workspace path jail for file tools, stripped environment variables,\n"
                "           timeout, output cap, permission prompts\n"
                f"  level 2: Job Object: {sb['memory_mb']} MB memory cap, {sb['max_processes']} process,\n"
                "           whole process tree killed on timeout/exit/crash, no clipboard/desktop access\n"
                "  level 3: Docker/WSL isolation: not built yet\n"
                "\n"
                "What this does NOT stop: a command reading or writing files outside the\n "
                "workspace (only the permission prompts guard that), network access, CPU use.\n"
                f"Full access: {'ON' if policy.full_access else 'off'}",
                markup=False
            )
            continue
        if text.split()[0] == "/todo":
            if text.split()[1:] == ["clear"]:
                if ws.todo_path and ws.todo_path.exists():
                    ws.todo_path.unlink()
                console.print("TODO cleared", style="yellow", markup=False)
            else:
                console.print(read_todo(ws) or "(no TODO yet)", markup=False)
            continue

        if text == "/prune":
            # Manual prune, keeping only the 2 newest tool results (handy for testing)
            if not agent.prune(keep_recent=2):
                console.print("nothing to prune", style="yellow", markup=False)
            continue



        # --- normal request: hand it to the agent ---
        try:
            result = agent.run(text)
            finish()
            # Normal answers were already streamed to the screen; only the
            # harness's "[stopped: ...]" notices still need printing
            if result.startswith("[stopped]"):
                console.print(result, style="yellow", markup=False)
        except KeyboardInterrupt:
            finish()
            console.print("[interrupted]", style="yellow", markup=False)
        except Exception as e:
            # Most likely cause: llama-server isn't running
            finish()
            console.print(f"ERROR: {type(e).__name__}: {e}", style="red", markup=False)

        console.print()  # blank line between turns


if __name__ == "__main__":
    main()