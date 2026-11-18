"""
commands.py - Slash commands for the TUI.

Every handler is `handler(app, args)` and runs in a WORKER THREAD (so it may call blocking
dialogs such as app.ui.confirm). Output goes through app.ui.say / app.ui.log, which are safe to
call from any thread. A handler may return ("request", text) to start an agent request after the
command finishes (used by `/plan <request>`).

Ported: /exit /help /think /ws /mode /context /todo /summary /new
        /sessions /resume /project /openmode /chat /export /instructions /config /skills /prune /compact /plan
        /fullaccess /sandbox /audit /undo /rewind /taint /untaint /sources /artifacts /open /save
        /memory /knowledge /reminder /agents /diagrams
Not yet: /imagetest (see NOT_PORTED)
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable
import shutil
import socket
import subprocess
import sys
from typing import Optional
from danica.app_core import ResumeCancelled
from danica.tui.pack_cmd import render_help_menu

COMMANDS: dict[str, Callable] = {}

NOT_PORTED = {"/imagetest"}

HELP = """Commands
  Chat      /think  toggle thinking      /think auto  use automatic setting      /new  fresh session, same mode      /exit
  Plans     /plan [<request>]  /plan show  /plan auto ask|off
  Project   /mode  /project [path]  /openmode [folder]  /chat  /instructions
  Context   /context  /prune  /compact [tokens]  /summary  /todo [clear]
  Sessions  /sessions [delete n]  /resume [number|id]  /export
  Files     /artifacts  /open <n>  /save <n> [folder]  /upload [path]  /attach <picture>
  Safety    /fullaccess  /sandbox  /audit  /undo  /rewind [id]  /taint  /untaint
  Web       /sources  /webcache [status | list | search <words> | |reindex | clear]
  Memory    /memory [list | add [scope] <text> | review | capture | search <words> | merge | forget <id> | show <id> | status | reindex | path]
  Knowledge /knowledge [index|search|watch|stop|status|digest|graph|commit <preview-id>]
  Reminder  /reminder add <ISO datetime> <text>  | list
  Other     /ws  /skills  /agents [run <task> | peek|wait|kill <id>]  /config  /diagrams  /help
Not ported to the TUI yet (still in `python -m danica.cli`): /imagetest
Esc or Ctrl+C stops the current task. Ctrl+O expands tool output. Ctrl+D quits."""


DESCRIPTIONS = {
    "/think": "toggle thinking (auto to reset)", "/new": "fresh session, same mode", "/exit": "quit",
    "/plan": "draft a plan first (show | auto ask|off)", "/mode": "chat or project mode",
    "/project": "trust and open a real folder",
    "/openmode": "OPEN MODE: whole-disk access (warns first)", "/chat": "back to a throwaway chat",
    "/instructions": "show the loaded DANICA.md", "/context": "how full the context is",
    "/prune": "drop old tool output", "/compact": "summarize earlier messages",
    "/summary": "show the compaction summary", "/todo": "show the TODO (clear to reset)",
    "/sessions": "list saved sessions", "/resume": "reopen a saved session", "/export": "export this session",
    "/artifacts": "files from the last request", "/open": "open a file from the last request",
    "/save": "copy a file out (default: Downloads)", "/memory": "saved notes (review, merge, forget…)",
    "/fullaccess": "toggle FULL ACCESS", "/sandbox": "explain the shell sandbox",
    "/audit": "verify the audit log", "/undo": "undo the last request", "/rewind": "list or restore checkpoints",
    "/taint": "show untrusted web content", "/untaint": "clear the taint flag", "/sources": "web sources so far", "/webcache": "stored web pages (status, list, search, reindex, clear)",
    "/knowledge": "local notes index (index, search, watch…)", "/reminder": "add or list reminders",
    "/agents": "subagents (run, peek, wait, kill)", "/ws": "show the workspace path", "/skills": "list global skills",
    "/config": "show the settings", "/diagrams": "diagram engine status", "/help": "list every command",
    "/imagetest": "not ported yet (use the classic CLI)",
}

def command_names() -> list[tuple[str, str]]:
    """(name, description) for every slash command, shortest first so an exact match sorts to the top."""
    names = set(COMMANDS) | set(NOT_PORTED)
    return [(n, DESCRIPTIONS.get(n, "")) for n in sorted(names, key=lambda n: (len(n), n))]


def command(*names: str):
    def deco(fn):
        for name in names:
            COMMANDS[name] = fn
        return fn
    return deco


def dispatch(app, text: str):
    name, _, args = text.strip().partition(" ")
    args = args.strip()
    handler = COMMANDS.get(name)
    if handler is not None:
        app.current_command = name          # lets one handler serve /undo + /rewind, /open + /save
        return handler(app, args)
    if name in NOT_PORTED:
        app.ui.log("info", f"{name} is not ported to the TUI yet. It still works in `python -m danica.cli`.")
    else:
        app.ui.log("warning", f"unknown command {name}. /help lists them.")
    return None


# ------------------------------------------------------------------------------ batch 1

@command("/exit")
def _exit(app, args):
    app.call_from_thread(app.exit)


@command("/help")
def _help(app, args):
    app.ui.show(render_help_menu())


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
    if app.open_root is not None:
        app.ui.say(f"\u26a0 OPEN MODE (full disk access; protected areas stay blocked)\n"
                   f"  start folder: {app.ws.root}\n  /chat, /project or /new leave Open Mode")
    elif app.project:
        app.ui.say(f"project mode: {app.project['name']}\n  folder: {app.ws.root}")
    else:
        app.ui.say(f"normal chat (throwaway workspace)\n  folder: {app.ws.root}\n"
                   f"  /project trusts and opens a real folder")


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


# ------------------------------------------------------------------------------ batch 2

@command("/sessions")
def _sessions(app, args):
    from danica.sessions import list_sessions
    infos = list_sessions()
    if not infos:
        app.ui.say("(no saved sessions yet)")
        return
    lines = []
    for n, info in enumerate(infos, start=1):
        tag = "  (current)" if info["id"] == app.session_id else ""
        lines.append(f"{n:>2}. {info['id']}  {info['modified']:%d %b %H:%M}  "
                     f"{info['requests']} request(s), {info['compactions']} compaction(s){tag}")
        lines.append(f"      {info.get('title') or info['preview']}")
    app.ui.say("\n".join(lines))


@command("/resume")
def _resume(app, args):
    from danica.app_core import open_resumed
    from danica.projects import load_session_meta, lookup
    from danica.sessions import SESSIONS_DIR, resolve_session
    try:
        arg = args.split()[0] if args else ""
        target = resolve_session(SESSIONS_DIR, arg, current=app.session_id)
        parts, report = open_resumed(target, app.ui, app.bridge)
    except ResumeCancelled as e:
        app.ui.log("info", str(e))
        return
    except ValueError as e:
        app.ui.log("error", f"{e}")
        return
    except Exception as e:      # the old session stays active if loading failed
        app.ui.log("error", f"could not resume: {type(e).__name__}: {e}")
        return
    meta = load_session_meta(SESSIONS_DIR / target)
    project = lookup(Path(meta["project"])) if meta.get("project") else None
    app.adopt_session(parts, target, project, report)


@command("/project")
def _project(app, args):
    from danica.app_core import ask_trust
    from danica.projects import ProjectError, check_allowed, lookup, register
    raw = args.strip().strip('"') or str(app.launch_dir)
    try:
        path = check_allowed(Path(raw))
        info = lookup(path)
        if info is None:                      # never trusted: ask first
            if not ask_trust(path, app.ui):
                app.ui.log("warning", "not trusted, nothing changed")
                return
            info = register(path)
            app.audit.append("project_trust", path=str(path), id=info["id"])
        app.swap_session(info)
    except ProjectError as e:
        app.ui.log("error", f"{e}")


@command("/openmode")
def _openmode(app, args):
    """Enter Open Mode: the whole disk, minus the protected areas (see openmode.py)."""
    from danica.app_core import confirm_open_mode, resolve_open_start
    # a folder you name is used as given; with no argument the launch folder, or ~/.danica if it won't do
    start, notice, problem = resolve_open_start(args, app.launch_dir)
    if problem:
        app.ui.log("error", f"Open Mode can't start there: {problem}")
        return
    if notice:
        app.ui.log("warning", notice)
    if not confirm_open_mode(start.resolve(), app.ui):         # the warning, every single time
        app.ui.log("warning", "Open Mode not enabled, nothing changed")
        return
    app.audit.append("open_mode_enter", start=str(start.resolve()))
    app.swap_session(None, open_root=start.resolve())


@command("/chat")
def _chat(app, args):
    app.swap_session(None, note="Normal chat. New session {id}.")


@command("/instructions")
def _instructions(app, args):
    app.ui.say(app.agent.project_instructions or "(no project instructions loaded)")


@command("/export")
def _export(app, args):
    from danica.config import APP_DIR
    from danica.export import export_session
    try:
        out = export_session(APP_DIR / "sessions" / app.session_id, app.ws,
                             app.project or ({"name": "Open Mode", "path": str(app.ws.root)} if app.open_root else None))
    except (ValueError, OSError) as e:
        app.ui.log("error", f"{e}")
        return
    app.audit.append("export", folder=str(out))
    app.ui.log("ok", f"Exported to {out}\nRead conversation.md before sharing it: redaction "
                     f"only catches well-known secret formats.")


@command("/config")
def _config(app, args):
    from danica.config import CONFIG, CONFIG_PATH
    state = "found" if CONFIG_PATH.exists() else "not created (all defaults)"
    lines = [f"config file: {CONFIG_PATH} ({state})"]
    for section, values in CONFIG.items():
        lines.append(f"[{section}]")
        for key, value in values.items():
            hidden = any(w in key.lower() for w in ("key", "token", "secret", "password"))
            lines.append(f"  {key} = {'***' if hidden else value}")
    app.ui.say("\n".join(lines))


@command("/skills")
def _skills(app, args):
    from danica.config import SKILLS_DIR
    from danica.tools.skills import skill_catalog
    skills, errors = skill_catalog()
    lines = [f"Global skills: {SKILLS_DIR}"]
    if skills:
        lines += [f"  {skill.name}: {skill.description}" for skill in skills]
    else:
        lines.append("(no global skills found; add a folder with a SKILL.md)")
    app.ui.say("\n".join(lines))
    for error in errors:
        app.ui.log("warning", f"WARNING: {error}")


@command("/prune")
def _prune(app, args):
    # Manual prune, keeping only the 2 newest tool results (handy for testing)
    if not app.agent.prune(keep_recent=2):
        app.ui.log("warning", "nothing to prune")


@command("/compact")
def _compact(app, args):
    # "/compact" uses the normal tail; "/compact 500" keeps only ~500 tokens verbatim
    parts = args.split()
    tail = int(parts[0]) if parts and parts[0].isdigit() else None
    if not app.agent.compact(tail):
        app.ui.log("warning", "nothing worth compacting yet: everything fits in the kept tail. "
                              "Try /compact 500 to keep less.")


@command("/plan")
def _plan(app, args):
    from danica.plan_flow import plan_text
    rest = args.strip()
    if not rest:
        app.plan_next = True
        app.ui.log("ok", "Plan mode: your next request gets a plan first (read-only drafting, then you approve).")
        return None
    if rest.lower() == "show":
        plan = app.agent.last_plan
        app.ui.say(plan_text(plan) if plan else "(no plan yet in this session)")
        return None
    if rest.lower().startswith("auto"):
        value = rest[4:].strip().lower()
        if value in ("ask", "off"):
            app.plan_auto = value
            app.ui.say(f"automatic plan offers: {value}")
        else:
            app.ui.say("usage: /plan auto ask | /plan auto off")
        return None
    if rest.startswith("/"):
        app.ui.log("warning", "a slash command can't be planned")
        return None
    return ("request", rest)           # runs as an ordinary request, with a plan forced


# ------------------------------------------------------------------------------ batch 4

@command("/fullaccess")
def _fullaccess(app, args):
    policy = app.policy
    policy.full_access = not policy.full_access
    app.audit.append("full_access", enabled=policy.full_access)
    if policy.full_access:
        app.ui.say("FULL ACCESS ON: Danica can run ANY command without asking. "
                   "/fullaccess again to turn it off.", style="bold red")
    else:
        app.ui.log("warning", "full access off")


@command("/sandbox")
def _sandbox(app, args):
    from danica.config import CONFIG
    sb = CONFIG["sandbox"]
    app.ui.say(
        "Shell sandbox\n"
        "  level 1: workspace path jail for file tools, stripped environment variables,\n"
        "           timeout, output cap, permission prompts\n"
        f"  level 2: Job Object: {sb['memory_mb']} MB memory cap, {sb['max_processes']} process,\n"
        "           whole process tree killed on timeout/exit/crash, no clipboard/desktop access\n"
        "  level 3: Docker/WSL isolation: not built yet\n"
        "\n"
        "What this does NOT stop: a command reading or writing files outside the\n"
        "workspace (only the permission prompts guard that), network access, CPU use.\n"
        f"Full access: {'ON' if app.policy.full_access else 'off'}")


@command("/audit")
def _audit(app, args):
    from danica.audit import verify
    ok, count, problem = verify(app.audit.path)
    if ok:
        app.ui.log("ok", f"audit log OK: {count} entries. chain intact")
    else:
        app.ui.say(f"AUDIT LOG PROBLEM: {problem}", style="bold red")


@command("/undo", "/rewind")
def _undo(app, args):
    # `args` is empty for /undo and /rewind; the command name itself is not passed, so we
    # register one function and tell them apart through app.current_command (set by dispatch).
    from danica.checkpoints import CheckpointError
    checkpoints = app.checkpoints
    if not checkpoints.available:
        app.ui.log("warning", "checkpoints are disabled (git not found)")
        return
    name = app.current_command
    try:
        if name == "/undo":
            summary, safety = checkpoints.undo()
        elif not args:
            # "/rewind" on its own just lists recent checkpoints
            lines = [f"{ident} {when} {label}" for ident, when, label in checkpoints.list_checkpoints()]
            app.ui.say("\n".join(lines) or "(no checkpoints yet)")
            return
        else:
            summary, safety = checkpoints.rewind(args.split()[0])
        app.ui.log("ok", summary)
        if safety:
            app.audit.append("restore", command=name, safety=safety)
    except CheckpointError as e:
        app.ui.log("error", f"{e}")


@command("/taint")
def _taint(app, args):
    web = app.ws.web
    if web and web.tainted:
        app.ui.log("warning", "Untrusted web content is in this conversation. Sources:")
        app.ui.say("\n".join("  " + s for s in web.taint_sources) +
                   "\nWhile tainted, shell commands and fetches from new sites always ask.\n"
                   "/untaint clears the flag (it does not remove the content from context).")
    else:
        app.ui.say("not tainted")


@command("/untaint")
def _untaint(app, args):
    web = app.ws.web
    if web and web.tainted:
        web.clear_taint()
        app.audit.append("untaint")
        app.ui.log("warning", "taint cleared. The web content is still in the context.")
    else:
        app.ui.say("not tainted")


@command("/sources")
def _sources(app, args):
    web = app.ws.web
    sources = list(web.by_id.values()) if web else []
    if not sources:
        app.ui.say("(no web sources yet)")
        return
    lines = []
    for s in sources:
        lines.append(f"[{s.id}] {len(s.passages)} passages  {s.title[:60] or '(no title)'}")
        lines.append(f"      {s.final_url or s.url}")
    app.ui.say("\n".join(lines))


@command("/artifacts")
def _artifacts(app, args):
    if not app.artifacts:
        app.ui.say("(no files from the last request)")
        return
    app._call(app.show_artifacts_list, list(app.artifacts))


@command("/open", "/save")
def _open_save(app, args):
    from danica.artifacts import ArtifactError, downloads_dir, open_artifact, pick, save_copy
    parts = args.split(maxsplit=1)               # the number, then the rest (a folder path)
    arg = parts[0] if parts else ""
    rest = parts[1] if len(parts) > 1 else ""
    try:
        item = pick(arg, app.artifacts)
        if app.current_command == "/open":
            app.ui.log("ok", open_artifact(item))
            app.audit.append("artifact_open", path=item.rel)
        else:
            dest = Path(rest.strip().strip('"')) if rest.strip() else downloads_dir()
            saved = save_copy(item.path, dest)
            app.audit.append("artifact_save", path=item.rel, to=str(saved.parent))
            app.ui.log("ok", f"saved a copy to {saved}")
    except ArtifactError as e:
        app.ui.log("error", f"{e}")


# ------------------------------------------------------------------------------ batch 3

@command("/memory")
def _memory(app, args):
    from danica.memory_ui import handle_memory
    handle_memory(("/memory " + args).strip(), app.agent.memory, app.audit,
                  app.project is not None, app.agent, app.ui)


@command("/diagrams")
def _diagrams(app, args):
    from danica.diagram_engines import status_lines
    app.ui.say("\n".join(status_lines()))


@command("/knowledge")
def _knowledge(app, args):
    import json
    from danica.app_core import knowledge_index
    from danica.knowledge import KnowledgeError, render_hits
    ui, audit, ws = app.ui, app.audit, app.ws
    parts = args.split(maxsplit=1)
    index = knowledge_index()
    action = parts[0].lower() if parts else "status"
    argument = parts[1].strip().strip('"') if len(parts) > 1 else ""
    try:
        if action == "index" and argument:
            result = index.index_folder(Path(argument))
            audit.append("knowledge_index", root=result["root"],
                         files_changed=result["files_changed"], chunks_added=result["chunks_added"])
            ui.log("ok", f"Indexed {result['root']}: {result['files_changed']} changed, "
                         f"{result['files_unchanged']} unchanged, {result['files_removed']} removed, "
                         f"{result['chunks_added']} passages.")
            if result["embedding_note"]:
                ui.log("warning", result["embedding_note"])
        elif action == "search" and argument:
            hits = index.search(argument)
            audit.append("knowledge_search", hits=len(hits), query_chars=len(argument))
            ui.say(render_hits(hits))
            if index.embedding_note:
                ui.log("warning", f"Semantic search unavailable: {index.embedding_note}")
        elif action == "watch" and argument:
            root = index.start_watching(Path(argument))
            audit.append("knowledge_watch", root=root, action="start")
            ui.log("ok", f"Watching {root} while Danica is open.")
        elif action == "stop" and argument:
            stopped = index.stop_watching(Path(argument))
            audit.append("knowledge_watch", path=argument, action="stop", stopped=stopped)
            ui.say("Watcher stopped." if stopped else "No watcher for that folder.")
        elif action == "digest" and argument in ("daily", "weekly"):
            days = 1 if argument == "daily" else 7
            changes = index.recent_changes(days)
            if not changes:
                ui.say(f"No indexed notes changed in the last {days} day(s).", style="dim")
            else:
                label = "Daily" if days == 1 else "Weekly"
                lines = [f"{label} local knowledge digest:"]
                for root, path, excerpt in changes:
                    preview = " ".join((excerpt or "").split())[:240]
                    lines.append(f"- {root}:{path}: {preview}")
                ui.say("\n".join(lines))
            audit.append("knowledge_digest", period=action, entries=len(changes))
        elif action == "graph" and argument:
            target = ws.resolve(argument)
            output = index.write_graph(target)
            audit.append("knowledge_graph_export", path=ws.display(output))
            ui.log("ok", f"Interactive knowledge graph saved: {ws.display(output)}")
        elif action == "commit" and argument:
            preview = ws.knowledge_previews.get(argument)
            if preview is None:
                ui.log("warning", "No such preview in this session.")
                return
            target = Path(preview["path"])
            overwrite = target.exists()
            ok = ui.confirm("Overwrite the existing note?" if overwrite else "Write this note?",
                            body=f"Write-back preview for {target}:\n\n{preview['content']}",
                            yes="Overwrite" if overwrite else "Write", no="Cancel")
            if not ok:
                ui.log("warning", "No file was changed.")
                return
            written = index.commit_obsidian_preview(ws, argument, overwrite=overwrite)
            audit.append("obsidian_write", path=str(written), overwritten=overwrite)
            ui.log("ok", f"Wrote {written}")
        elif action == "status":
            ui.say(json.dumps(index.status(), ensure_ascii=False, indent=2))
        else:
            ui.say("Usage: /knowledge index <folder> | search <query> | watch <folder> | stop <folder> | "
                   "status | digest daily|weekly | graph <workspace-file.html> | commit <preview-id>")
    except (KnowledgeError, OSError) as exc:
        ui.log("error", f"{exc}")


@command("/reminder")
def _reminder(app, args):
    from datetime import datetime
    from danica.app_core import knowledge_index
    from danica.knowledge import KnowledgeError
    parts = ("/reminder " + args).split(maxsplit=3)
    index = knowledge_index()
    try:
        if len(parts) == 4 and parts[1] == "add":
            reminder_id = index.add_reminder(parts[2], parts[3])
            app.audit.append("reminder_add", id=reminder_id)
            app.ui.log("ok", f"Reminder {reminder_id} scheduled.")
        elif len(parts) == 2 and parts[1] == "list":
            reminders = index.list_reminders()
            if not reminders:
                app.ui.say("No pending reminders.")
            else:
                app.ui.say("\n".join(
                    f"{rid}  {datetime.fromtimestamp(due).astimezone().isoformat(timespec='minutes')}  {msg}"
                    for rid, due, msg in reminders))
        else:
            app.ui.say("Usage: /reminder add <ISO datetime> <text> | /reminder list")
    except KnowledgeError as exc:
        app.ui.log("error", f"{exc}")


@command("/webcache")
def _webcache(app, args):
    """Look at or empty the persistent web cache (see webcache.py)."""
    from danica.webcache import age_text, get_web_cache
    ui = app.ui
    cache = get_web_cache()
    if cache is None:
        ui.log("warning", "the web cache is switched off ([webcache] enabled = false in config.toml)")
        return
    sub, _, arg = args.partition(" ")
    arg = arg.strip()
    try:
        if sub in ("", "status"):
            st = cache.status()
            ui.say(
                f"Folder: {st['folder']}  (one .md file per page; the index rebuilds from them)\n"
                f"{st['pages']} page(s) stored ({st['stale_pages']} past their freshness window), "
                   f"{st['searches']} search(es), {st['vectors']} meaning-vector(s)."
                   + (f"\nMeaning search is off: {st['embedding_note']}" if st["embedding_note"] else ""))
        elif sub == "list":
            rows = cache.recent(25)
            ui.say("\n".join(f"{age_text(age):>6} ago{' OLD' if stale else '    '}  {title[:60] or '(no title)'}  {url[:80]}"
                             for url, title, age, stale in rows) or "The web cache is empty.")
        elif sub == "search" and arg:
            hits = cache.search(arg, 8)
            ui.say("\n".join(f"{h.score:.4f}  {h.title[:60]}  {h.url[:80]}  ({h.via}, {age_text(h.age)} ago)"
                             for h in hits) or "Nothing stored matches.")
        elif sub == "clear":
            if ui.confirm("Clear the web cache?", "Every stored page and search is deleted. "
                          "Danica will fetch from the web again.", yes="Clear", no="Keep"):
                ui.log("ok", f"Removed {cache.clear()} stored page(s).")
        elif sub == "reindex":                    # after editing or deleting .md files in bulk
            done = cache.reindex()
            ui.log("ok", f"Index rebuilt: {done['indexed']} file(s) read, {done['removed']} forgotten, "
                         f"{done['skipped']} skipped (not cache files).")
        else:
            ui.log("warning", "Usage: /webcache [status | list | search <words> | reindex | clear]")
    except Exception as exc:                  # a damaged cache file must not crash the command
        ui.log("error", f"web cache: {type(exc).__name__}: {exc}")


@command("/agents")
def _agents(app, args):
    import json
    from danica.subagents import SubagentError
    ui = app.ui
    supervisor = getattr(app.ws, "subagent_supervisor", None)
    if supervisor is None:
        ui.log("warning", "subagent supervisor is unavailable")
        return
    parts = args.split(maxsplit=1)
    if not parts:
        children = supervisor.list()
        if not children:
            ui.say("No subagents have run in this session.", style="dim")
        else:
            ui.say("\n".join(f"{c['id']}  {c['status']}  {c['elapsed_seconds']}s  {c['task']}"
                              for c in children))
        return
    sub, arg = parts[0], (parts[1].strip() if len(parts) > 1 else "")

    def find(child_id):
        return next((c for c in supervisor.list() if c["id"] == child_id), None)

    try:
        if sub == "run" and arg:
            child_id = supervisor.start(arg)
            ui.log("ok", f"Subagent {child_id} started. Use /agents to monitor or "
                         f"/agents kill {child_id} to stop it.")
        elif sub == "peek" and arg:
            child = find(arg)
            ui.say(json.dumps(child, ensure_ascii=False, indent=2) if child else "No such subagent.")
        elif sub == "wait" and arg:
            ui.log("info", f"Waiting for subagent {arg}…")
            finished = supervisor.wait(arg)
            child = find(arg)
            ui.say(json.dumps(child, ensure_ascii=False, indent=2) if finished and child
                   else "Subagent is still running." if child else "No such subagent.")
        elif sub == "kill" and arg:
            stopped = supervisor.kill(arg)
            ui.log("warning", "Subagent stopped." if stopped else "No running subagent with that id.")
        else:
            ui.log("warning", "Usage: /agents [run <task> | peek <id> | wait <id> | kill <id>]")
    except SubagentError as exc:
        ui.log("error", f"{exc}")


@command("/dialogs")
def _dialogs(app, args):
    """Debug: pop the confirm and permission dialogs without any model."""
    ok = app.ui.confirm("Test confirm", "Body text goes here.", "detail line", "Yes", "No")
    app.ui.log("info", f"confirm -> {ok}")

    answer = app.ui.permission(
        tool="write_file",
        summary="write_file(path='demo.py', 60 lines)",
        tier="ask",                      # use one of your real tier names
        reason="Writes outside the last-approved folder",
        can_remember=True,
    )
    app.ui.log("info", f"permission -> {answer}")   # "once", "session" or "deny"

from danica.tui import commands_view  # noqa: E402,F401
from danica.tui import commands_vision  # noqa: E402,F401
from danica.tui import commands_classifier  # noqa: E402,F401
from danica.tui import commands_game  # noqa: E402,F401
from danica.tui import commands_mail  # noqa: E402,F401
from danica.tui import commands_upload  # noqa: E402,F401
from danica.tui import commands_sessions  # noqa: E402,F401
