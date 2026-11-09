"""
commands.py - Slash commands for the TUI.

Every handler is `handler(app, args)` and runs in a WORKER THREAD (so it may call blocking
dialogs such as app.ui.confirm). Output goes through app.ui.say / app.ui.log, which are safe to
call from any thread. Handlers that change app state on the UI thread use app.call_from_thread.

Ported so far (Phase 6, batch 1): /exit /help /think /ws /mode /context /todo /summary /new
The rest of the old command set is listed in NOT_PORTED and answers with a clear message.
"""

from __future__ import annotations

from typing import Callable

COMMANDS: dict[str, Callable] = {}

# Everything the old CLI had that is not ported yet (kept in sync with cli.HELP)
NOT_PORTED = {
    "/plan", "/chat", "/project", "/instructions", "/prune", "/compact", "/sessions", "/resume",
    "/export", "/artifacts", "/open", "/save", "/memory", "/fullaccess", "/sandbox", "/audit",
    "/undo", "/rewind", "/taint", "/untaint", "/sources", "/knowledge", "/reminder", "/skills",
    "/agents", "/config", "/imagetest", "/diagrams",
}

HELP = """Commands (ported to the TUI)
  /think  toggle thinking      /think auto  use automatic setting      /new  fresh session      /exit
  /mode   /context   /todo [clear]   /summary   /ws   /help
Not ported yet (still available in `python -m danica.cli`):
  /plan /project /chat /instructions /prune /compact /sessions /resume /export /artifacts /open /save
  /memory /fullaccess /sandbox /audit /undo /rewind /taint /untaint /sources /knowledge /reminder
  /skills /agents /config /imagetest /diagrams
Esc or Ctrl+C stops the current task. Ctrl+O expands tool output. Ctrl+D quits."""


def command(name: str):
    def deco(fn):
        COMMANDS[name] = fn
        return fn
    return deco


def dispatch(app, text: str) -> None:
    name, _, args = text.strip().partition(" ")
    args = args.strip()
    handler = COMMANDS.get(name)
    if handler is not None:
        handler(app, args)
    elif name in NOT_PORTED:
        app.ui.log("info", f"{name} is not ported to the TUI yet. It still works in `python -m danica.cli`.")
    else:
        app.ui.log("warning", f"unknown command {name}. /help lists them.")


@command("/exit")
def _exit(app, args):
    app.call_from_thread(app.exit)


@command("/help")
def _help(app, args):
    app.ui.say(HELP)


@command("/think")
def _think(app, args):
    agent = app.agent
    if args == "auto":
        agent.thinking_override = None
        agent.thinking = False
        app.ui.log("info", "thinking override cleared; automatic setting will apply.")
        return
    agent.thinking = not agent.thinking
    agent.thinking_override = agent.thinking
    app.ui.log("info", f"thinking: {'on' if agent.thinking else 'off'} (manual override)")


@command("/ws")
def _ws(app, args):
    app.ui.say(str(app.ws.root))


@command("/mode")
def _mode(app, args):
    if app.project:
        app.ui.say(f"project mode: {app.project['name']}\n  folder: {app.ws.root}")
    else:
        app.ui.say(f"normal chat (throwaway workspace)\n  folder: {app.ws.root}\n"
                   f"  /project trusts and opens a real folder (not ported yet)")


@command("/context")
def _context(app, args):
    from danica.context import breakdown
    agent = app.agent
    lines = [agent.tracker.bar(agent.messages)]
    for role, tokens in sorted(breakdown(agent.messages).items(), key=lambda kv: -kv[1]):
        lines.append(f"  {role:<10} ~{tokens:,} tokens")
    app.ui.say("\n".join(lines))


@command("/todo")
def _todo(app, args):
    from danica.tools.todo import read_todo
    ws = app.ws
    if args == "clear":
        if ws.todo_path and ws.todo_path.exists():
            ws.todo_path.unlink()
        app.ui.log("warning", "TODO cleared")
    else:
        app.ui.say(read_todo(ws) or "(no TODO yet)")


@command("/summary")
def _summary(app, args):
    from danica.compaction import is_summary
    agent = app.agent
    if len(agent.messages) > 1 and is_summary(agent.messages[1]):
        app.ui.say(agent.messages[1]["content"])
    else:
        app.ui.say("(no compaction has happened yet)")


@command("/new")
def _new(app, args):
    # Same mode as now: a project session reopens the same folder, a chat gets a new workspace
    app.swap_session(app.project)