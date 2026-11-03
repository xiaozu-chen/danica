"""
cli.py - The terminal frontend.
Run from the project root:  python -m danica.cli
Commands: /think (toggle thinking), /ws (show workspace path), /exit
Ctrl+C stops the current task. Ctrl+D or /exit quits.
"""

import sqlite3
from pathlib import Path
from datetime import datetime

from prompt_toolkit import PromptSession, prompt
from prompt_toolkit.history import FileHistory
from prompt_toolkit.formatted_text import HTML
from rich.console import Console
from rich.text import Text

from danica.agent import Agent
from danica.config import APP_DIR, CONFIG
from danica.workspace import Workspace
from danica.context import breakdown
from danica.permissions import PermissionPolicy, Tier
from danica.audit import AuditLog, verify
from danica.checkpoints import CheckpointError, Checkpoints
from danica.tools.todo import read_todo
from danica.compaction import is_summary
from danica.sessions import SESSIONS_DIR, list_sessions, load_records, resolve_session
from danica.projects import (ProjectError, check_allowed, load_session_meta, lookup,
                             register, save_session_meta, scan, approve_instructions, read_instructions)
from danica.export import export_session
from danica.memory import MemoryError, MemoryStore
from danica.planning import PlanError, needs_plan, normalize_plan
from danica.skills import discover_skills

console = Console()

HELP = """Commands
  Chat      /think  toggle thinking      /new  fresh session, same mode      /exit
  Project   /mode  /project [path]  /chat  /instructions
  Context   /context  /prune  /compact [tokens]  /summary  /todo [clear]
  Sessions  /sessions  /resume [number|id]  /export
  Safety    /fullaccess  /sandbox  /audit  /undo  /rewind [id]  /taint  /untaint
  Web       /sources
  Memory    /memory [review|merge <source-id> <target-id>]  /forget <id>
  Skills    /skills  (list local skills; bodies load only on request)
  Output    /artifacts  (click a generated artifact link or copy its path)
  Other     /ws  /config  /help
Ctrl+C stops the current task. Ctrl+D quits."""


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
        elif kind == "citations":
            newline_if_needed()
            # Yellow when there is a warning, green when everything checked out
            console.print(data, style="yellow" if "WARNING" in data else "green",
                          markup=False, highlight=False)
        elif kind == "present":
            newline_if_needed()
            path = Path(data)
            link = Text(f"Open presentation: {path}", style=f"link {path.as_uri()}")
            console.print(link, markup=False, highlight=False)
        elif kind == "artifacts":
            newline_if_needed()
            console.print("Artifacts from this task (click to open, or copy the path):",
                          style="bold green", markup=False)
            for path in data:
                console.print(Text(f"  {path}", style=f"link {Path(path).as_uri()}"),
                              markup=False, highlight=False)
    def finish() -> None:
        newline_if_needed()

    return on_event, finish


def make_ask(finish):
    """Build the callback that asks the user for permission in the terminal."""

    def ask(tool: str, summary: str, tier: Tier, reason: str, can_remember: bool) -> str:
        finish() # make sure we're on a fresh line after streamed text
        if tier == Tier.MEMORY:
            console.print("Memory proposal (nothing is saved unless you accept):",
                          style="bold yellow", markup=False)
            console.print(summary, style="yellow", markup=False, highlight=False)
            try:
                return "once" if prompt("[y] save  [n] reject > ").strip().lower() in ("y", "yes") else "deny"
            except (EOFError, KeyboardInterrupt):
                return "deny"
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


def confirm_memory(prompt_text: str) -> bool:
    """Memory deletion and merge prompts fail closed on Ctrl+C or Ctrl+D."""
    try:
        return prompt(prompt_text).strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def review_plan(plan: str) -> str | None:
    """Let the user approve, edit, or cancel a large-task plan."""
    while True:
        console.print("\nProposed plan (saved in this session's PLAN.md):",
                      style="bold yellow", markup=False)
        console.print(plan, markup=False, highlight=False)
        try:
            answer = prompt("[a] approve  [e] edit  [c] cancel > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return None
        if answer in ("a", "approve", "y", "yes"):
            return plan
        if answer in ("c", "cancel", "n", "no", ""):
            return None
        if answer not in ("e", "edit"):
            console.print("Choose approve, edit, or cancel.", style="yellow", markup=False)
            continue
        try:
            edited = prompt(
                "Edit the Markdown plan (Esc then Enter to submit; Ctrl+C cancels):\n",
                multiline=True,
            )
            plan = normalize_plan(edited)
        except (EOFError, KeyboardInterrupt):
            return None
        except PlanError as exc:
            console.print(f"Invalid plan: {exc}", style="yellow", markup=False)

def apply_instructions(project: dict, agent, audit) -> None:
    """Load the project's DANICA.md. New or changed text must be approved first."""
    found = read_instructions(project["path"])
    if found is None:
        return
    text, sha, cut = found
    approved_now = False
    if project.get("instructions_sha") != sha:
        label = "changed" if project.get("instructions_sha") else "new"
        console.print(f"\nThis project has a {label} DANICA.md. Danica would follow it as "
                      f"instructions (it cannot override safety rules or permission prompts):",
                      style="bold yellow", markup=False)
        console.print(text, style="dim", markup=False, highlight=False)
        if cut:
            console.print("(longer than the limit: only the text above would be loaded)",
                          style="yellow", markup=False)
        try:
            ok = prompt("[y] load it  [n] skip > ").strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            ok = False
        if not ok:
            console.print("DANICA.md not loaded.", style="yellow", markup=False)
            return
        approve_instructions(project, sha)
        approved_now = True
    agent.set_project_instructions(text)
    audit.append("instructions", sha=sha[:12], approved_now=approved_now)
    console.print(f"Loaded DANICA.md ({len(text)} characters).", style="green", markup=False)

def build_session(session_id: str, finish, on_event, project: dict | None = None):
    """Create everything one session needs.
    project=None  -> normal chat: a throwaway workspace inside the session folder
    project=info  -> project mode: the workspace IS the user's trusted folder"""
    session_dir = APP_DIR / "sessions" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    root = Path(project["path"]) if project else session_dir / "workspace"
    # TODO.md, transcript and sources stay in the session folder either way,
    # so nothing of Danica's ever lands inside your project.
    ws = Workspace(root, todo_path=session_dir / "TODO.md",
                   sources_path=session_dir / "sources.json",
                   plan_path=session_dir / "PLAN.md",
                   plan_approval_path=session_dir / "PLAN.approved",
                   skill_dir=APP_DIR / "skills")
    ws.memory = MemoryStore(
        global_dir=APP_DIR / "memory" / "global",
        project_dir=Path(project["dir"]) / "memory" if project else None,
        project_id=project["id"] if project else None,
        chat_dir=session_dir / "memory",
        chat_id=session_id,
    )
    save_session_meta(session_dir, project["path"] if project else None)

    # One audit log for ALL sessions, so the chain spans everything Danica ever did
    audit = AuditLog(APP_DIR / "logs" / "audit.jsonl", session=session_id)

    # Shadow git repo lives OUTSIDE the workspace, so the model can never touch it.
    # A project keeps ONE repo shared by all its sessions (so /rewind spans sessions).
    git_dir = Path(project["dir"]) / "git" if project else session_dir / "checkpoints" / "git"
    checkpoints = Checkpoints(ws, git_dir)
    if not checkpoints.available:
        console.print("git was not found, so /undo and /rewind are disabled. "
                      "Install it with: winget install Git.Git", style="yellow", markup=False)

    # A fresh policy every time: approvals from another session never carry over
    policy = PermissionPolicy(ask=make_ask(finish))
    policy.project_mode = project is not None    # relaxes everyday dev commands (see permissions.py)
    agent = Agent(
        ws,
        transcript_path=session_dir / "transcript.jsonl",
        on_event=on_event,
        policy=policy,
        audit=audit,
        checkpoints=checkpoints,
    )
    if project:
        apply_instructions(project, agent, audit)
    return ws, audit, checkpoints, policy, agent


def open_resumed(target: str, finish, on_event):
    """Build a session around an existing folder and load its history.
    Returns (the 5 session objects, a text report for the user)."""
    records, bad = load_records(SESSIONS_DIR / target / "transcript.jsonl")
    # A project session must reopen in ITS folder, and only if that folder is still trusted
    project = None
    meta = load_session_meta(SESSIONS_DIR / target)
    if meta.get("project"):
        project = lookup(Path(meta["project"]))
        if project is None or not Path(project["path"]).is_dir():
            raise ValueError(f"that session worked in {meta['project']}, which is no longer "
                             f"trusted or no longer exists. Use /project <path> to trust it again.")
    parts = build_session(target, finish, on_event, project)
    ws, audit, checkpoints, policy, agent = parts
    fixed = agent.load_history(records)
    try:
        agent.restore_plan()
    except (PlanError, OSError, UnicodeError) as exc:
        console.print(f"WARNING: saved plan could not be restored: {exc}",
                      style="yellow", markup=False)
    audit.append("session_resume", workspace=str(ws.root), records=len(records),
                 unreadable_lines=bad, repaired=fixed)

    where = f"project {project['name']}" if project else "chat"
    lines = [f"Resumed session {target} ({where}): {len(agent.messages) - 1} messages in context, "
             f"{agent.compactions} compaction(s)."]
    if agent.user_requests:
        lines.append("Last request: " + agent.user_requests[-1][:120])
    if agent.current_plan:
        lines.append("An interrupted approved plan was restored from PLAN.md.")
    if bad or fixed:
        lines.append(f"Repairs: {bad} unreadable transcript line(s) skipped, "
                     f"{fixed} broken tool-call record(s) fixed.")
    lines.append("Permission approvals and the /undo stack start fresh "
                 "(/rewind still lists the old checkpoints). Try /todo and /summary.")
    return parts, "\n".join(lines)

def ask_trust(path: Path) -> bool:
    """The trust dialog. Returns True only on an explicit yes."""
    files, size, capped, has_git = scan(path)
    count = f"{files:,}+" if capped else f"{files:,}"
    console.print(f"\nTrust this folder as a Danica project?\n  {path}", style="bold yellow", markup=False)
    console.print(f"  {count} files, {size / 1_000_000:,.0f} MB (ignoring .git, .venv, node_modules)"
                  + ("; it is a git repository" if has_git else ""), markup=False)
    console.print("Trusting means Danica can read and change files here. Commands still ask for\n"
                  "permission, and every request is snapshotted so /undo can roll it back.\n"
                  "Snapshots live outside your folder and never touch your own .git.",
                  style="dim", markup=False)
    if capped or size > 500_000_000:
        console.print("This folder is big: the first snapshot may take a while.", style="yellow", markup=False)
    try:
        return prompt("[y] trust  [n] cancel > ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False     # Ctrl+C / Ctrl+D = no


def show_config() -> None:
    """Print the effective settings. Names that look like secrets are masked."""
    from danica.config import CONFIG_PATH          # local import: only this command needs it
    state = "found" if CONFIG_PATH.exists() else "not created (all defaults)"
    console.print(f"config file: {CONFIG_PATH} ({state})", markup=False, highlight=False)
    for section, values in CONFIG.items():
        console.print(f"[{section}]", style="bold", markup=False)
        for key, value in values.items():
            hidden = any(w in key.lower() for w in ("key", "token", "secret", "password"))
            console.print(f"  {key} = {'***' if hidden else value}", markup=False, highlight=False)
            

def main() -> None:
    launch_dir = Path.cwd()          # /project with no argument means THIS folder
    on_event, finish = make_event_printer()
    session_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    project = None                   # None = normal chat mode
    ws, audit, checkpoints, policy, agent = build_session(session_id, finish, on_event)
    audit.append("session_start", workspace=str(ws.root), mode="chat")

    # FileHistory gives up-arrow recall across restarts
    session = PromptSession(history=FileHistory(str(APP_DIR / "history.txt")))

    def toolbar():
        """Bottom-of-screen context bar, colored by pressure level."""
        level = agent.tracker.level(agent.messages)
        color = {"ok": "ansigreen", "prune": "ansiyellow", "compact": "ansiyellow", "stop": "ansired"}[level]
        mode = "think" if agent.thinking else "fast"
        if policy.full_access:
            mode += " | FULL ACCESS" # loud on purpose
        if project:
            mode += f" | project: {project['name']}"
        if ws.web and ws.web.tainted:
            mode += " | TAINTED"   # untrusted web content is in context
        return HTML(f'<style fg="{color}">ctx {agent.tracker.bar(agent.messages)}</style> |{mode}')

    console.print("[bold]Danica[/bold] ready. /help lists the commands, /project opens a real folder.")
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
        if text == "/artifacts":
            artifacts = [
                ws.resolve(path) for path in sorted(ws.artifacts)
                if Path(path).suffix.casefold() != ".html"
            ]
            if not artifacts:
                console.print("(no non-HTML artifacts from the current task)", markup=False)
            else:
                console.print("Artifacts from this task (click to open, or copy the path):",
                              style="bold green", markup=False)
                for path in artifacts:
                    console.print(Text(f"  {path}", style=f"link {path.as_uri()}"),
                                  markup=False, highlight=False)
            continue
        if text == "/skills":
            skills, warnings = discover_skills(APP_DIR / "skills")
            if not skills:
                console.print("(no valid skills; add <name>\\SKILL.md under "
                              f"{APP_DIR / 'skills'})", markup=False)
            for skill in skills:
                console.print(f"{skill.name}: {skill.description}", markup=False,
                              highlight=False)
            for warning in warnings:
                console.print(f"WARNING: {warning}", style="yellow", markup=False)
            continue
        if text == "/memory" or text.startswith("/memory "):
            parts = text.split()
            store = ws.memory
            try:
                if len(parts) == 1:
                    facts = store.list_facts()
                    if not facts:
                        console.print("(no saved memories)", markup=False)
                    for fact in facts:
                        console.print(
                            f"{fact['id'][:8]}  [{fact['scope']}] {fact['title']} "
                            f"({fact['updated'][:10]})",
                            markup=False, highlight=False,
                        )
                        console.print(f"      {fact['summary']}", style="dim",
                                      markup=False, highlight=False)
                elif parts[1] == "review" and len(parts) == 2:
                    pairs = store.duplicate_pairs()
                    if not pairs:
                        console.print("No likely duplicate memories found.", markup=False)
                    for left, right, similarity in pairs:
                        console.print(
                            f"{similarity:.0%} similar: {left['id'][:8]} {left['title']} "
                            f"<-> {right['id'][:8]} {right['title']}",
                            markup=False, highlight=False,
                        )
                        console.print(
                            f"  merge {left['id'][:8]} into {right['id'][:8]} with "
                            f"/memory merge {left['id'][:8]} {right['id'][:8]}",
                            style="dim", markup=False, highlight=False,
                        )
                elif parts[1] == "merge" and len(parts) == 4:
                    facts = store.list_facts()
                    sources = [fact for fact in facts if fact["id"].startswith(parts[2])]
                    targets = [fact for fact in facts if fact["id"].startswith(parts[3])]
                    if len(sources) != 1 or len(targets) != 1:
                        raise MemoryError("source and target must be known, unique memory ids")
                    source, target = sources[0], targets[0]
                    console.print(
                        f"Merge '{source['title']}' into '{target['title']}'? "
                        "The source record will be deleted.",
                        style="yellow", markup=False,
                    )
                    if not confirm_memory("[y] merge  [n] cancel > "):
                        console.print("Merge cancelled.", markup=False)
                        continue
                    merged = store.merge(source["id"], target["id"])
                    console.print(
                        f"Merged into {merged['id'][:8]}: {merged['title']}",
                        style="green", markup=False,
                    )
                    if merged["warning"]:
                        console.print(merged["warning"], style="yellow", markup=False)
                else:
                    console.print("Usage: /memory | /memory review | "
                                  "/memory merge <source-id> <target-id>", markup=False)
            except (MemoryError, OSError, ValueError, sqlite3.Error) as e:
                console.print(f"ERROR: {e}", style="red", markup=False)
            continue
        if text == "/forget" or text.startswith("/forget "):
            parts = text.split(maxsplit=1)
            if len(parts) != 2 or not parts[1].strip():
                console.print("Usage: /forget <memory-id>", markup=False)
                continue
            memory_id = parts[1].strip()
            try:
                matches = [fact for fact in ws.memory.list_facts()
                           if fact["id"].startswith(memory_id)]
                if len(matches) != 1:
                    raise MemoryError(
                        "no memory matches that id" if not matches
                        else "that id is ambiguous; use more characters"
                    )
                fact = matches[0]
                console.print(f"Delete '{fact['title']}' from Markdown and all indexes?",
                              style="yellow", markup=False)
                if not confirm_memory("[y] forget  [n] cancel > "):
                    console.print("Nothing deleted.", markup=False)
                    continue
                removed = ws.memory.forget(fact["id"])
                console.print(f"Forgot '{removed['title']}' and verified its removal.",
                              style="green", markup=False)
            except (MemoryError, OSError, ValueError, sqlite3.Error) as e:
                console.print(f"ERROR: {e}", style="red", markup=False)
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

        if text.split()[0] == "/compact":
            # "/compact" uses the normal tail; "/compact 500" keeps only ~500 tokens verbatim
            parts = text.split()
            tail = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
            if not agent.compact(tail):
                console.print("nothing worth compacting yet: everything fits in the kept tail. "
                              "Try /compact 500 to keep less.", style="yellow", markup=False)
            continue
        if text == "/summary":
            # The summary message always sits right after the system prompt
            if len(agent.messages) > 1 and is_summary(agent.messages[1]):
                console.print(agent.messages[1]["content"], markup=False)
            else:
                console.print("(no compaction has happened yet)", markup=False)
            continue

        if text == "/sessions":
            infos = list_sessions()
            if not infos:
                console.print("(no saved sessions yet)", markup=False)
            for n, info in enumerate(infos, start=1):
                tag = "  (current)" if info["id"] == session_id else ""
                console.print(f"{n:>2}. {info['id']}  {info['modified']:%d %b %H:%M}  "
                              f"{info['requests']} request(s), {info['compactions']} compaction(s){tag}",
                              markup=False, highlight=False)
                console.print(f"      {info['preview']}", style="dim", markup=False, highlight=False)
            continue

        if text.split()[0] == "/resume":
            try:
                arg = text.split()[1] if len(text.split()) > 1 else ""
                target = resolve_session(SESSIONS_DIR, arg, current=session_id)
                parts, report = open_resumed(target, finish, on_event)
            except ValueError as e:
                console.print(f"ERROR: {e}", style="red", markup=False)
                continue
            except Exception as e:      # the old session stays active if loading failed
                console.print(f"ERROR: could not resume: {type(e).__name__}: {e}", style="red", markup=False)
                continue
            ws, audit, checkpoints, policy, agent = parts    # switch over only on success
            session_id = target
            meta = load_session_meta(SESSIONS_DIR / target)
            project = lookup(Path(meta["project"])) if meta.get("project") else None
            console.print(report, style="green", markup=False)
            continue
        if text == "/taint":
            if ws.web and ws.web.tainted:
                console.print("Untrusted web content is in this conversation. Sources:", style="yellow", markup=False)
                for source in ws.web.taint_sources:
                    console.print("  " + source, markup=False, highlight=False)
                console.print("While tainted, shell commands and fetches from new sites always ask.\n"
                              "/untaint clears the flag (it does not remove the content from context).",
                              markup=False)
            else:
                console.print("not tainted", markup=False)
            continue
        if text == "/untaint":
            if ws.web and ws.web.tainted:
                ws.web.clear_taint()
                audit.append("untaint")
                console.print("taint cleared. The web content is still in the context.", style="yellow", markup=False)
            else:
                console.print("not tainted", markup=False)
            continue
        if text == "/sources":
            sources = list(ws.web.by_id.values()) if ws.web else []
            if not sources:
                console.print("(no web sources yet)", markup=False)
            for s in sources:
                console.print(f"[{s.id}] {len(s.passages)} passages  {s.title[:60] or '(no title)'}",
                              markup=False, highlight=False)
                console.print(f"      {s.final_url or s.url}", style="dim", markup=False, highlight=False)
            continue
        if text == "/mode":
            if project:
                console.print(f"project mode: {project['name']}\n  folder: {ws.root}", markup=False)
            else:
                console.print(f"normal chat (throwaway workspace)\n  folder: {ws.root}\n"
                              f"  /project trusts and opens a real folder", markup=False)
            continue

        if text.split()[0] == "/project":
            parts_ = text.split(maxsplit=1)           # the rest of the line, so paths with spaces work
            raw = parts_[1].strip().strip('"') if len(parts_) > 1 else str(launch_dir)
            try:
                path = check_allowed(Path(raw))
                info = lookup(path)
                if info is None:                      # never trusted: ask first
                    if not ask_trust(path):
                        console.print("not trusted, nothing changed", style="yellow", markup=False)
                        continue
                    info = register(path)
                    audit.append("project_trust", path=str(path), id=info["id"])
                new_id = datetime.now().strftime("%Y%m%d-%H%M%S")
                new_parts = build_session(new_id, finish, on_event, info)
            except ProjectError as e:
                console.print(f"ERROR: {e}", style="red", markup=False)
                continue
            ws, audit, checkpoints, policy, agent = new_parts
            session_id, project = new_id, info
            audit.append("session_start", workspace=str(ws.root), mode="project", project=info["id"])
            console.print(f"Project mode: {info['name']}\n  folder: {ws.root}\n"
                          f"New session {new_id}. The previous session stays available via /sessions and /resume.",
                          style="green", markup=False)
            continue

        if text == "/chat":
            session_id = datetime.now().strftime("%Y%m%d-%H%M%S")
            project = None
            ws, audit, checkpoints, policy, agent = build_session(session_id, finish, on_event)
            audit.append("session_start", workspace=str(ws.root), mode="chat")
            console.print(f"Normal chat. New session {session_id}.", style="green", markup=False)
            continue
        if text == "/instructions":
            console.print(agent.project_instructions or "(no project instructions loaded)", markup=False)
            continue
        if text == "/help":
            console.print(HELP, markup=False)
            continue

        if text == "/config":
            show_config()
            continue

        if text == "/new":
            session_id = datetime.now().strftime("%Y%m%d-%H%M%S")
            # Same mode as now: a project session reopens the same folder (and asks about
            # DANICA.md only if it changed); a chat session gets a new throwaway workspace
            ws, audit, checkpoints, policy, agent = build_session(session_id, finish, on_event, project)
            audit.append("session_start", workspace=str(ws.root),
                         mode="project" if project else "chat")
            console.print(f"New session {session_id}.", style="green", markup=False)
            continue

        if text == "/export":
            try:
                out = export_session(APP_DIR / "sessions" / session_id, ws, project)
            except (ValueError, OSError) as e:
                console.print(f"ERROR: {e}", style="red", markup=False)
                continue
            audit.append("export", folder=str(out))
            console.print(f"Exported to {out}\nRead conversation.md before sharing it: redaction "
                          f"only catches well-known secret formats.", style="green", markup=False)
            continue
        # --- normal request: hand it to the agent ---
        try:
            approved_plan = None
            continuing = bool(agent.current_plan) and text.strip().casefold() in {
                "continue", "continue the task", "keep going", "resume the task",
            }
            if needs_plan(text):
                proposed = agent.create_plan(text)
                proposed = agent.save_plan_draft(proposed)
                approved_plan = review_plan(proposed)
                if approved_plan is None:
                    agent.cancel_plan()
                    audit.append("plan_cancelled")
                    console.print("Plan cancelled; no task actions were run.",
                                  style="yellow", markup=False)
                    continue
                approved_plan = agent.approve_plan_draft(approved_plan)
                audit.append("plan_approved", chars=len(approved_plan), edited=approved_plan != proposed)
            elif continuing:
                approved_plan = agent.current_plan
            result = agent.run(text, approved_plan=approved_plan)
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