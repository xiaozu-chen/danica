"""
cli.py - The terminal frontend.
Run from the project root:  python -m danica.cli
Commands: /think (toggle thinking), /ws (show workspace path), /exit
Ctrl+C stops the current task. Ctrl+D or /exit quits.
"""

from datetime import datetime

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.formatted_text import HTML
from rich.console import Console

from danica.agent import Agent
from danica.config import APP_DIR
from danica.workspace import Workspace
from danica.context import breakdown

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

    def finish() -> None:
        newline_if_needed()

    return on_event, finish


def main() -> None:
    # Every run is a new session: sessions/<timestamp>/{workspace/, transcript.jsonl}
    session_dir = APP_DIR / "sessions" / datetime.now().strftime("%Y%m%d-%H%M%S")
    ws = Workspace(session_dir / "workspace")

    on_event, finish = make_event_printer()
    agent = Agent(
        ws,
        transcript_path=session_dir / "transcript.jsonl",
        on_event=on_event,
    )

    # FileHistory gives up-arrow recall across restarts
    session = PromptSession(history=FileHistory(str(APP_DIR / "history.txt")))

    def toolbar():
        """Bottom-of-screen context bar, colored by pressure level."""
        level = agent.tracker.level(agent.messages)
        color = {"ok": "ansigreen", "prune": "ansiyellow", "compact": "ansiyellow", "stop": "ansired"}[level]
        mode = "think" if agent.thinking else "fast"
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

        # --- normal request: hand it to the agent ---
        try:
            agent.run(text)
            finish()
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