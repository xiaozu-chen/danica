"""
cli.py - The terminal frontend.
Run from the project root:  python -m danica.cli
Commands: /think (toggle thinking), /ws (show workspace path), /exit
Ctrl+C stops the current task. Ctrl+D or /exit quits.
"""

import asyncio
import json
import argparse
import subprocess
import sys
import shutil

from pathlib import Path
from datetime import datetime

from prompt_toolkit import PromptSession, prompt
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.history import FileHistory
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.styles import Style as PT_Style
from rich.console import Console
from rich.markup import escape
from rich.style import Style
from rich.text import Text
from rich import print
from rich.panel import Panel
from rich import box
from rich.table import Table
from rich.syntax import Syntax

from danica.agent import Agent
from danica.config import APP_DIR, CONFIG, SKILLS_DIR
from danica.workspace import Workspace
from danica.context import breakdown
from danica.permissions import PermissionPolicy, Tier
from danica.audit import AuditLog, verify
from danica.checkpoints import CheckpointError, Checkpoints
from danica.tools.todo import open_items, read_todo
from danica.compaction import harvest, is_summary
from danica.sessions import SESSIONS_DIR, list_sessions, load_records, resolve_session
from danica.projects import (ProjectError, check_allowed, load_session_meta, lookup,
                             register, save_session_meta, scan, approve_instructions, read_instructions)
from danica.export import export_session
from danica.memory import SCOPES, MemoryStore, MemoryStoreError
from danica.memory_index import FACTS_DB, MemoryIndex
from danica.embeddings import get_embedder
from danica.capture import ProposalQueue, extract
from danica.forget import forget
from danica.imageview import ImageError, check_image, display, show_image, test_image
from danica.present import PresentFilter
from danica.artifacts import (ArtifactError, ArtifactList, collect, is_openable, open_artifact,
                              pick, save_copy)
from danica.diagram_engines import status_lines as diagram_status
from danica.planner import PlanError, PlanFormatError, looks_big, reload_plan
from danica.tools.skills import skill_catalog
from danica.subagents import SubagentError, SubagentSupervisor
from danica.knowledge import KnowledgeError, KnowledgeIndex, render_hits
from danica.bundle import BundleError, export_bundle
from danica.extensions import ExtensionError, ExtensionManager

console = Console()
_KNOWLEDGE_INDEX: KnowledgeIndex | None = None
_EXTENSION_MANAGER: ExtensionManager | None = None

ACCENT = "#ff9e64"
EDGE = 1

def _w() -> int:
    return shutil.get_terminal_size().columns - EDGE

PROMPT_STYLE = PT_Style.from_dict({
    "edge": ACCENT,
    "bottom-toolbar": "noreverse",
    "rule": "#555555",
    "prompt": f"bold {ACCENT}",
    "bottom-toolbar.text": "noreverse"
})

def prompt_message():
    width = shutil.get_terminal_size().columns
    return HTML(f'<rule>{"—" * width}</rule>\n<prompt>&gt;</prompt> ')

TAG_COLORS = {
    "ERROR": "#FF0000",
    "WARNING": "#FFFF00",
    "OK": "#00FF00",
    "FAIL": "#FFA500",
    "INFO": "white",
    "THINKING": "#808080"
}
TAG_WIDTH = max(map(len, TAG_COLORS))   # 7, updates itself if you add a longer label


def _write_log(status, text):
    key = status.upper()
    color = TAG_COLORS.get(key, "white")
    label = f"{key:^{TAG_WIDTH}}"       # centered inside the badge
    console.print(f"[bold #000000 on {color}] {label} [/]::{escape(str(text))}", highlight=False)

def show_intro(ws, project=None, version="0.1") -> None:
    # left column: welcome + mascot + where we are
    left = Text(justify="center")
    left.append("Welcome back!\n\n", style="bold")
    left.append(r"  /\_/\ " + "\n" + " ( o.o )" + "\n" + r"  > ^ <" + "\n\n", style=ACCENT)
    left.append(f"project: {project['name']}\n" if project else "chat mode\n", style="dim")

    relative_ws = Path(ws.root).relative_to(APP_DIR / "sessions")

    left.append(str(relative_ws), style="dim")

    # right column: tips
    right = Text()
    right.append("Tips for getting started\n", style=f"bold {ACCENT}")
    right.append("/help      list every command\n")
    right.append("/project   open a real folder\n")
    right.append("/plan      draft a plan first\n")
    right.append("/memory    notes Danica remembers\n")
    right.append("\n")
    right.append("Ctrl+C stops a task · Ctrl+D quits", style="dim")

    body = Table(box=box.SQUARE, show_edge=False, show_header=False,
                 padding=(0, 2), expand=True)
    body.add_column(ratio=2, justify="center", vertical="middle")
    body.add_column(ratio=3, vertical="middle")
    body.add_row(left, right)

    console.print(Panel(
        body,
        title=f" Danica v{version} ",
        title_align="left",
        border_style=ACCENT,
        box=box.ROUNDED,
        padding=(1, 1),
    ))

def knowledge_index() -> KnowledgeIndex:
    global _KNOWLEDGE_INDEX
    if _KNOWLEDGE_INDEX is None:
        _KNOWLEDGE_INDEX = KnowledgeIndex(APP_DIR / "knowledge.db", get_embedder())
    return _KNOWLEDGE_INDEX


def extension_manager(audit=None, headless: bool = False) -> ExtensionManager:
    global _EXTENSION_MANAGER
    if _EXTENSION_MANAGER is None:
        _EXTENSION_MANAGER = ExtensionManager(
            APP_DIR, CONFIG, audit, enable_mcp=not headless, enable_hooks=not headless)
    else:
        _EXTENSION_MANAGER.audit = audit
        if headless:
            _EXTENSION_MANAGER.enable_hooks = False
    return _EXTENSION_MANAGER

HELP = """Commands
  Chat      /think  toggle thinking      /think auto  use automatic setting      /new  fresh session, same mode      /exit
  Plans     /plan [<request>]  /plan show  /plan auto ask|off
  Project   /mode  /project [path]  /chat  /instructions
  Context   /context  /prune  /compact [tokens]  /summary  /todo [clear]
  Sessions  /sessions  /resume [number|id]  /export
  Files     /artifacts  /open <n>  /save <n> [folder]
  Memory    /memory [list | add [scope] <text> | review | capture | search <words> | merge | forget <id> | show <id> | status | reindex | path]
  Safety    /fullaccess  /sandbox  /audit  /undo  /rewind [id]  /taint  /untaint
  Web       /sources
  Knowledge /knowledge [index|search|watch|stop|status|digest|graph|commit <preview-id>]
  Reminder  /reminder add <ISO datetime> <text>  | list
  Other     /ws  /skills  /agents [run <task> | peek|wait|kill <id>]  /config  /help  /imagetest  /diagrams
Ctrl+C stops the current task. Ctrl+D quits."""


class EventPrinter:
    """Prints the agent's events. Streamed answer text goes through a PresentFilter, so
    <present file="..."/> tags are removed from the text and the picture is shown instead."""

    def __init__(self, get_ws):
        self.get_ws = get_ws               # returns the CURRENT workspace (it changes on /project, /resume)
        self.mid_line = False              # is the cursor in the middle of a line of text?
        self.filter = PresentFilter()
        self.presented: set[str] = set()   # pictures already shown during this request

    def _newline_if_needed(self) -> None:
        if self.mid_line:
            console.print()
            self.mid_line = False

    def _text(self, text: str, style: str | None = None) -> None:
        # markup=False: model text can contain [brackets] that rich would treat as styling tags
        if text:
            console.print(text, style=style, end="", markup=False, highlight=False)
            self.mid_line = not text.endswith("\n")

    def _handle(self, kind: str, value: str) -> None:
        if kind == "text":
            self._text(value)
        else:
            self.show(value)

    def _flush_filter(self) -> None:
        for kind, value in self.filter.flush():
            self._handle(kind, value)

    def show(self, rel: str) -> None:
        """Show one picture the answer asked for. A problem is a one-line notice, never a crash."""
        self._newline_if_needed()
        ws = self.get_ws()
        try:
            path = check_image(ws, rel)
            self.presented.add(ws.display(path))
            show_image(path, ws.display(path), console)
        except ImageError as e:
            _write_log("warning", f"  Could not show {rel:80}: {e}")

    def begin_request(self) -> None:
        self.presented.clear()
        self.filter = PresentFilter()

    def present_pending(self, paths: list[str]) -> None:
        """Charts made during the request that the answer did not present."""
        todo = [p for p in paths if p not in self.presented]
        if todo:
            self._newline_if_needed()
            console.print("Charts made in this request:", style="dim", markup=False)
            for p in todo:
                self.show(p)

    def finish(self) -> None:
        self._flush_filter()
        self._newline_if_needed()

    def __call__(self, kind: str, data) -> None:
        if kind == "content":
            for k, v in self.filter.feed(data):
                self._handle(k, v)
            return
        self._flush_filter()               # anything else ends the text run: nothing may stay held back

        if kind == "thinking":
            self._text(data, style="dim")
        elif kind == "tool_call":
            self._newline_if_needed()
            name, args = data
            title = name.replace("_", " ").title()
            console.print(f"[bold #000000 on blue] TOOL [/] {escape(str(title))} {escape(str(args))}", style="cyan", highlight=False)

            # [proposed UI feature | status: pending] 
            # extract and syntax-highlight python content if present in argument
            # if isinstance(args, dict) and "content" in args:
            #     other_args = {k: v for k, v in args.items() if k != "content"}
            #     if other_args:
            #         console.print(str(other_args), style="cyan", highlight=False)
            #     syntax = Syntax(str(args["content"]), "python", theme="monokai", line_numbers=True, word_wrap=True)
            #     console.print(syntax)
            # else:
            #     console.print(str(args)[:300], style="cyan", highlight=False)
            
        elif kind == "tool_result":
            preview = str(data)
            if len(preview) > 400:
                preview = preview[:400] + f"... ({len(str(data)) - 400} more chars)"
            console.print(preview, style="dim", markup=False, highlight=False)
        elif kind == "notice":
            self._newline_if_needed()
            _write_log("warning", data)
        elif kind == "citations":
            self._newline_if_needed()
            # Yellow when there is a warning, green when everything checked out
            console.print(data, style="yellow" if "WARNING" in data else "green",
                          markup=False, highlight=False)


def make_ask(finish):
    """Build the callback that asks the user for permission in the terminal."""

    def ask(tool: str, summary: str, tier: Tier, reason: str, can_remember: bool) -> str:
        finish() # make sure we're on a fresh line after streamed text
        console.print(f"Permission needed ({tier.value}): {reason}", style="bold yellow", markup=False)
        _write_log("warning", summary)
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
            _write_log("warning", "  Longer than the limit: only the text above would be loaded")
        try:
            ok = prompt("[y] load it  [n] skip > ").strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            ok = False
        if not ok:
            _write_log("warning", "  DANICA.md not loaded.")
            return
        approve_instructions(project, sha)
        approved_now = True
    agent.set_project_instructions(text)
    audit.append("instructions", sha=sha[:12], approved_now=approved_now)
    _write_log("ok", f"  Loaded DANICA.md ({len(text)} characters.)")

def build_session(session_id: str, finish, on_event, project: dict | None = None,
                  headless: bool = False, workspace_root: Path | None = None):
    """Create everything one session needs.
    project=None  -> normal chat: a throwaway workspace inside the session folder
    project=info  -> project mode: the workspace IS the user's trusted folder"""
    session_dir = APP_DIR / "sessions" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    root = Path(project["path"]) if project else (
        Path(workspace_root) if headless and workspace_root else session_dir / "workspace")
    # TODO.md, transcript and sources stay in the session folder either way,
    # so nothing of Danica's ever lands inside your project.
    ws = Workspace(root, todo_path=session_dir / "TODO.md",
                   sources_path=session_dir / "sources.json")
    ws.knowledge_index = knowledge_index()
    save_session_meta(session_dir, project["path"] if project else None)

    # One audit log for ALL sessions, so the chain spans everything Danica ever did
    audit = AuditLog(APP_DIR / "logs" / "audit.jsonl", session=session_id)

    # Shadow git repo lives OUTSIDE the workspace, so the model can never touch it.
    # A project keeps ONE repo shared by all its sessions (so /rewind spans sessions).
    git_dir = Path(project["dir"]) / "git" if project else session_dir / "checkpoints" / "git"
    checkpoints = Checkpoints(ws, git_dir)
    if not checkpoints.available:
        _write_log("warning", "  `git` was not found, so /undo and /rewind are disabled. (Install it with: winget install Git.Git)")

    # A fresh policy every time: approvals from another session never carry over
    policy = PermissionPolicy(ask=None if headless else make_ask(finish))
    policy.project_mode = project is not None    # relaxes everyday dev commands (see permissions.py)
    supervisor = SubagentSupervisor(ws, policy, audit, session_id)
    ws.subagent_supervisor = supervisor
    extensions = extension_manager(audit, headless)
    ws.extension_manager = extensions
    for error in extensions.errors:
        on_event("notice", f"WARNING: extension disabled: {error}")
    agent = Agent(
        ws,
        transcript_path=session_dir / "transcript.jsonl",
        on_event=on_event,
        policy=policy,
        audit=audit,
        checkpoints=checkpoints,
    )
    # Memory: global notes, this project's notes (if any), and this session's notes.
    store = MemoryStore(APP_DIR / "memory",
                        Path(project["dir"]) / "memory" if project else None,
                        session_dir / "memory")
    # Keyword + meaning search (meaning needs the embedding server; without it search falls back to keywords)
    store.index = MemoryIndex(FACTS_DB, embedder=get_embedder(),
                              min_similarity=CONFIG["embeddings"]["min_similarity"])
    ws.memory = store                      # the recall tool reaches memory through the workspace
    agent.memory = store                   # the digest and auto-recall read it from the agent
    agent.proposals = ProposalQueue(session_dir / "proposals.json")   # notes waiting for /memory review
    agent.plans_dir = session_dir / "plans"                          # plan files live with the session, not in your project
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
    audit.append("session_resume", workspace=str(ws.root), records=len(records),
                 unreadable_lines=bad, repaired=fixed)

    where = f"project {project['name']}" if project else "chat"
    lines = [f"Resumed session {target} ({where}): {len(agent.messages) - 1} messages in context, "
             f"{agent.compactions} compaction(s)."]
    if agent.user_requests:
        lines.append("Last request: " + agent.user_requests[-1][:120])
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
    _write_log("warning", f"  Trust this folder as Danica project?\n{path}")
    console.print(f"  {count} files, {size / 1_000_000:,.0f} MB (ignoring .git, .venv, node_modules)"
                  + ("; it is a git repository" if has_git else ""), markup=False)
    console.print("Trusting means Danica can read and change files here. Commands still ask for\n"
                  "permission, and every request is snapshotted so /undo can roll it back.\n"
                  "Snapshots live outside your folder and never touch your own .git.",
                  style="dim", markup=False)
    if capped or size > 500_000_000:
        _write_log("warning", "  This folder is big: the first snapshot may take a while.")
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
            

def forget_note(store: MemoryStore, page) -> bool:
    """Forget one note for real and print every verification check. True if all passed."""
    report = forget(store, page)
    for name, ok, detail in report.checks:
        # console.print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f": {detail}" if detail else ""),
        #               style="green" if ok else "red", markup=False, highlight=False)
        _write_log(f"{'ok' if ok else 'fail'}", f"  {name}" + (f":{detail}" if detail else ""))
    if report.mentions:
        _write_log("warning", f"  Its text still appears in {len(report.mentions)} file(s) that forget does not edit (transcripts, summaries, exports):")
        for m in report.mentions[:5]:
            console.print("  " + m, markup=False, highlight=False)
        if len(report.mentions) > 5:
            console.print(f"  ... and {len(report.mentions) - 5} more", markup=False)
    console.print("The audit log and any backups of your AppData folder are never edited.",
                  style="dim", markup=False)
    return report.ok


def review_proposals(store: MemoryStore, queue: ProposalQueue, audit) -> None:
    """/memory review: go through proposed notes one at a time."""
    items = queue.items()
    if not items:
        _write_log("info", "  No proposed notes")
        return
    for item in items:
        scope, text = item["scope"], item["text"]
        if scope not in store.dirs:
            console.print(f"[{scope}] {text}\n  (left for later: {scope} memory is not open here)",
                          style="dim", markup=False)
            continue
        _write_log("info", f"  Proposed [{scope}]: {text}")
        try:
            answer = prompt("[y] save  [e] edit  [n] drop  [s] skip  [q] stop > ").strip().lower()
            edited = False
            if answer == "e":
                text = prompt("note> ", default=text).strip()
                answer, edited = "y", True
        except (EOFError, KeyboardInterrupt):
            return
        if answer == "q":
            return
        if answer == "n":
            queue.remove(item["id"])
        elif answer == "y":
            try:
                # A note you edited is your own wording: full confidence, source "user"
                page = store.add(scope, text, source="user" if edited else "captured",
                                 confidence="1.0" if edited else "0.8")
            except MemoryStoreError as e:
                _write_log("error", f"  {e}")
                queue.remove(item["id"])
                continue
            queue.remove(item["id"])
            audit.append("memory_add", scope=scope, id=page.id, source=page.source)
            _write_log("ok", f"  Saved to {scope} memory (id {page.id})")
        # anything else (s, Enter) leaves it in the queue


def merge_notes(store: MemoryStore, audit) -> None:
    """/memory merge: pairs of near-duplicate notes (same scope, almost the same meaning)."""
    if store.index is None:
        _write_log("info", "  Search is not available in this session")
        return
    pairs = store.index.near_duplicates(store, CONFIG["embeddings"]["merge_similarity"], 10)
    if not pairs:
        _write_log("info", "  No near-duplicate notes found")
        return
    for sim, ua, ub in pairs:
        a, b = store.get(ua), store.get(ub)
        if a is None or b is None:
            continue                       # one of them was already dealt with in this loop
        console.print(f"\nThese two {a.scope} notes look alike (similarity {sim:.2f}):",
                      style="bold", markup=False)
        console.print(f"  1. {a.body}\n  2. {b.body}", markup=False, highlight=False)
        try:
            answer = prompt("[1] keep first  [2] keep second  [m] write one merged note  "
                            "[s] skip  [q] stop > ").strip().lower()
            if answer == "m":
                merged = prompt("merged note> ", default=a.body).strip()
        except (EOFError, KeyboardInterrupt):
            return
        if answer == "q":
            return
        if answer == "1":
            drop = [b]
        elif answer == "2":
            drop = [a]
        elif answer == "m":
            try:
                page = store.add(a.scope, merged)       # saved FIRST: a refusal loses nothing
            except MemoryStoreError as e:
                _write_log("error", f"  {e}")
                continue
            audit.append("memory_add", scope=a.scope, id=page.id, source="merge")
            drop = [a, b]
        else:
            continue
        for old in drop:
            ok = forget_note(store, old)
            audit.append("memory_forget", scope=old.scope, id=old.id, ok=ok, reason="merge")


def handle_memory(text: str, store: MemoryStore, audit, in_project: bool, agent) -> None:
    """The /memory command. Sub-commands: list, add, review, capture, show, forget (delete),
    merge, search, status, reindex, path."""
    parts = text.split(maxsplit=2)               # "/memory", the sub-command, then the rest
    sub = parts[1].lower() if len(parts) > 1 else "list"
    rest = parts[2] if len(parts) > 2 else ""
    try:
        if sub == "list":
            pages = store.list_pages()
            if not pages:
                _write_log("info", "  No saved memory yet. Try: /memory")
            for p in pages:
                console.print(f"{p.id}  {p.scope:<7} {p.source:<8} {p.updated[:10]}  {p.summary}",
                              markup=False, highlight=False)
            if store.skipped:
                console.print(f"({store.skipped} file(s) in the memory folders were skipped: not valid "
                              f"notes, or too big)", style="dim", markup=False)
            if agent.proposals and agent.proposals.count():
                _write_log("warning", f"({agent.proposals.count()} proposed note(s) waiting: /memory review)")

        elif sub == "add":
            words = rest.split(maxsplit=1)
            if words and words[0].lower() in SCOPES:          # /memory add global <text>
                scope, body = words[0].lower(), (words[1] if len(words) > 1 else "")
            else:                                             # default: project if open, else global
                scope, body = ("project" if in_project else "global"), rest
            page = store.add(scope, body)
            audit.append("memory_add", scope=scope, id=page.id)   # the text itself is never logged
            _write_log("ok", f"  Saved to {scope} memory (id {page.id})")

        elif sub == "review":
            review_proposals(store, agent.proposals, audit)

        elif sub == "capture":
            recent = harvest(agent.messages)[0][-5:]          # your last requests, verbatim
            if not recent:
                _write_log("info", "  Nothing to look at yet: send a request first")
            else:
                added = 0
                try:
                    for request in recent:
                        for scope, note in extract(request, store):
                            added += agent.proposals.add(scope, note)
                except Exception as e:                        # most likely: llama-server is down
                    _write_log("error", f"  Could not ask the model ({type(e).__name__})")
                _write_log("info", f"  {added} note(s) proposed. /memory review shows them.")

        elif sub == "show":
            page = store.find(rest)
            console.print(f"[{page.scope}] {page.title}\nid {page.id}   source {page.source}   "
                          f"confidence {page.confidence}\ncreated {page.created}   updated {page.updated}"
                          f"\n\n{page.body}", markup=False, highlight=False)

        elif sub in ("forget", "delete"):
            page = store.find(rest)
            _write_log("warning", f"  Forget [{page.scope}] {page.summary}")
            try:
                sure = prompt("[y] forget it for good  [n] keep > ").strip().lower() in ("y", "yes")
            except (EOFError, KeyboardInterrupt):
                sure = False
            if sure:
                ok = forget_note(store, page)
                audit.append("memory_forget", scope=page.scope, id=page.id, ok=ok)
            else:
                _write_log("info", "  Kept")

        elif sub == "merge":
            merge_notes(store, audit)

        elif sub == "search":
            if store.index is None:
                _write_log("info", "  Search is not available in this session")
            else:
                hits = store.index.recall(store, rest, 5)
                for h in hits:
                    sim = f"{h.sim:.2f}" if h.sim is not None else " - "
                    console.print(f"{h.id}  {h.scope:<7} {h.via:<8} sim {sim}  {h.summary}",
                                  markup=False, highlight=False)
                if not hits:
                    _write_log("info", "  No matching notes")
                    if store.index.nearest is not None:
                        console.print(f"(closest by meaning: {store.index.nearest:.2f}, below the "
                                      f"{store.index.min_similarity:.2f} threshold)", style="dim", markup=False)
                if store.index.note:
                    _write_log("warning", f"keyword search only: {store.index.note}")

        elif sub == "status":
            if store.index is None:
                console.print("search is not available in this session", markup=False)
            else:
                for line in store.index.status_lines(store):
                    console.print(line, markup=False, highlight=False)

        elif sub == "reindex":
            if store.index is None:
                console.print("search is not available in this session", markup=False)
            else:
                n = store.index.reindex()
                console.print(f"cleared {n} vector(s); they are rebuilt on the next search", markup=False)

        elif sub == "path":
            for scope, folder in store.dirs.items():
                console.print(f"{scope:<8} {folder}", markup=False, highlight=False)

        else:
            console.print("usage: /memory [list | add [scope] <text> | review | capture | search <words> | "
                          "merge | forget <id> | show <id> | status | reindex | path]", markup=False)
    except MemoryStoreError as e:
        _write_log("error", f"{e}")

def show_artifacts(found: ArtifactList) -> None:
    """Print the files a request made. Openable ones are ctrl+click links."""
    if not found.items and not found.deleted:
        return
    console.print("Files from this request:", style="dim", markup=False)
    for n, a in enumerate(found.items, start=1):
        size = f"{a.size / 1_000_000:.1f} MB" if a.size >= 1_000_000 else f"{max(1, a.size // 1024)} KB"
        line = f"  {n}. {a.status:<7} {a.rel}  {size}"
        if is_openable(a.path):
            console.print(Text(line, style=Style(link=a.path.as_uri())))
        else:
            console.print(line, markup=False, highlight=False)
    if found.more:
        console.print(f"  ... and {found.more} more", style="dim", markup=False)
    if found.deleted:
        console.print(f"  ({found.deleted} file(s) deleted)", style="dim", markup=False)
    if found.items:
        console.print("  /open <n> opens a file, /save <n> [folder] copies it out (default: Downloads)",
                      style="dim", markup=False)


def handle_artifact_command(text: str, artifacts: list, audit) -> None:
    """/open <n> and /save <n> [folder]."""
    parts = text.split(maxsplit=2)               # the command, the number, then the rest (a folder path)
    cmd, arg = parts[0], (parts[1] if len(parts) > 1 else "")
    rest = parts[2] if len(parts) > 2 else ""
    try:
        item = pick(arg, artifacts)
        if cmd == "/open":
            _write_log("ok", open_artifact(item))
            audit.append("artifact_open", path=item.rel)
        else:
            dest = Path(rest.strip().strip('"')) if rest.strip() else Path.home() / "Downloads"
            saved = save_copy(item.path, dest)
            audit.append("artifact_save", path=item.rel, to=str(saved.parent))
            _write_log("ok", f"saved a copy to {saved}")
    except ArtifactError as e:
        _write_log("error", f"{e}")

MAX_DRAFTS = 3          # plans drafted for one request (the first, plus replans)


def open_editor(path: Path) -> None:
    """Open a file in Notepad (a fixed, predictable editor). Tests replace this."""
    subprocess.Popen(["notepad.exe", str(path)])


def show_plan(plan) -> None:
    console.print(f"\nPlan ({len(plan.steps)} steps)", style="bold", markup=False)
    console.print(f"Goal: {plan.goal}", markup=False, highlight=False)
    for i, step in enumerate(plan.steps, start=1):
        console.print(f"  {i}. {step}", markup=False, highlight=False)
    if plan.notes:
        console.print("Notes:", markup=False)
        for note in plan.notes:
            console.print(f"  - {note}", markup=False, highlight=False)
    if plan.path:
        console.print(f"(file: {plan.path})", style="dim", markup=False, highlight=False)


def plan_flow(request: str, agent, audit) -> tuple[str, object]:
    """Draft a plan (read-only), show it, and let the user approve, edit, replan or cancel.
    Returns ("run", plan) to run the request with the approved plan, ("run", None) to run it without
    one (drafting failed and the user said go ahead), or ("cancel", None)."""
    plan, feedback, drafts = None, "", 0
    while True:
        drafts += 1
        _write_log("warning", "[plan] drafting a plan (read-only: nothing is changed)...")
        try:
            plan = agent.draft_plan(request, feedback, plan)
        except KeyboardInterrupt:
            _write_log("warning", "(planning stopped)")
            return "cancel", None
        except Exception as e:                        # PlanError, or llama-server not answering
            why = str(e) if isinstance(e, PlanError) else type(e).__name__
            _write_log("error", f"Could not make a plan: {why}")
            try:
                go = prompt("[y] run the request without a plan  [n] cancel > ").strip().lower() in ("y", "yes")
            except (EOFError, KeyboardInterrupt):
                go = False
            return ("run", None) if go else ("cancel", None)

        agent.save_plan(plan)
        audit.append("plan", action="proposed", steps=len(plan.steps), draft=drafts)
        show = True
        while True:
            if show:
                show_plan(plan)
                show = False
            try:
                answer = prompt("[y] approve  [e] edit  [r] replan with feedback  [n] cancel > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = "n"

            if answer in ("y", "yes"):                # only an explicit yes approves: Enter just asks again
                agent.install_plan(plan)
                audit.append("plan", action="approved", steps=len(plan.steps))
                _write_log("ok", f"Plan approved: {len(plan.steps)} steps. It is now your TODO (see /todo).")
                return "run", plan
            if answer in ("n", "no", "q"):
                audit.append("plan", action="cancelled")
                return "cancel", None
            if answer == "e":
                if plan.path is None:
                    _write_log("warning", "this plan has no file to edit")
                    continue
                try:
                    open_editor(plan.path)
                    prompt("Edit the file, save it, then press Enter to reload > ")
                    plan = reload_plan(plan)
                    audit.append("plan", action="edited", steps=len(plan.steps))
                    show = True
                except PlanFormatError as e:
                    _write_log("error", f"Problem with the edited plan: {e}\n(the plan shown above is the last valid "
                                  f"version; press e to fix the file)")
                except OSError as e:
                    _write_log("error", f"could not open the editor ({e}); edit this file yourself: {plan.path}")
                except (EOFError, KeyboardInterrupt):
                    pass
                continue
            if answer == "r":
                if drafts >= MAX_DRAFTS:
                    _write_log("warning", f"That is {MAX_DRAFTS} drafts: edit this one (e), approve it (y) or cancel (n).")
                    continue
                try:
                    feedback = prompt("what should change? > ").strip()
                except (EOFError, KeyboardInterrupt):
                    feedback = ""
                if feedback:
                    audit.append("plan", action="replan")
                    break                              # leave the review loop: draft again with the feedback


def ask_plan_first(why: str) -> str:
    """The offer for a big-looking request: "yes", "no" or "never" (for this session)."""
    _write_log("warning", f"\nThis looks like a multi-step task ({why}).")
    try:
        answer = prompt("Draft a plan first? [y] yes (Enter)  [n] just do it  [x] don't ask again this session > "
                        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return "no"
    if answer in ("", "y", "yes"):
        return "yes"
    return "never" if answer in ("x", "never") else "no"


def run_plan_mode(text: str, forced: bool, plan_auto: str, agent, audit) -> tuple[str, object, str]:
    """Decide whether this request gets a plan, and if so run the dialog.
    forced = the user asked (/plan). Returns (decision, plan, plan_auto): decision is "run" or
    "cancel", plan is None when the request runs without one, plan_auto may become "off"."""
    if not forced and plan_auto == "ask":
        big, why, score = looks_big(text, CONFIG["planning"]["min_score"])
        # An unfinished TODO means the user is continuing a plan: don't interrupt with an offer
        if big and not open_items(read_todo(agent.ws)):
            answer = ask_plan_first(why)
            audit.append("plan_offer", score=score, answer=answer)      # the text itself is never logged
            if answer == "never":
                plan_auto = "off"
            forced = answer == "yes"
    if not forced:
        return "run", None, plan_auto
    decision, plan = plan_flow(text, agent, audit)
    return decision, plan, plan_auto

# -- c --



def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="danica")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("-p", "--print", dest="prompt_text",
                      help="run one request headlessly and print the streamed answer")
    mode.add_argument("--export-bundle", type=Path,
                      help="create a secret-scanned ZIP bundle inside the workspace")
    parser.add_argument("--workspace", type=Path, default=Path.cwd(),
                        help="workspace used by headless mode and bundle export")
    args = parser.parse_args(argv)
    if args.export_bundle:
        ws = Workspace(args.workspace)
        try:
            result = export_bundle(ws, args.export_bundle)
        except BundleError as exc:
            _write_log("error", f"{exc}")
            raise SystemExit(1) from exc
        _write_log("ok", f"Exported {result['files']} files ({result['bytes']} bytes) to "
                      f"{ws.display(result['path'])}; skipped {len(result['skipped'])} sensitive "
                      "or unsupported file(s).")
        return
    if args.prompt_text is not None:
        session_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        ws = Workspace(args.workspace)
        printer = EventPrinter(lambda: ws)
        ws, audit, _, _, agent = build_session(
            session_id, printer.finish, printer, headless=True, workspace_root=args.workspace)
        audit.append("session_start", workspace=str(ws.root), mode="headless")
        printer.begin_request()
        try:
            agent.run(args.prompt_text)
            printer.finish()
        except Exception as exc:
            printer.finish()
            _write_log("error", f"{type(exc).__name__}: {exc}")
            raise SystemExit(1) from exc
        if _KNOWLEDGE_INDEX is not None:
            _KNOWLEDGE_INDEX.close()
        if _EXTENSION_MANAGER is not None:
            _EXTENSION_MANAGER.close()
        return

    launch_dir = Path.cwd()          # /project with no argument means THIS folder
    # The lambda reads main's CURRENT ws, so it follows /project, /chat and /resume
    printer = EventPrinter(lambda: ws)
    on_event, finish = printer, printer.finish
    session_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    project = None                   # None = normal chat mode
    artifacts: list = []             # files the LAST request made (for /open and /save)
    plan_auto = CONFIG["planning"]["auto"]     # "ask": offer a plan for big-looking requests; "off": only /plan
    plan_next = False                           # set by a bare /plan: the NEXT request gets a plan
    ws, audit, checkpoints, policy, agent = build_session(session_id, finish, on_event)
    audit.append("session_start", workspace=str(ws.root), mode="chat")

    # FileHistory gives up-arrow recall across restarts
    session = PromptSession(history=FileHistory(str(APP_DIR / "history.txt")), style=PROMPT_STYLE)
    reminder_task = None

    async def reminder_loop():
        while True:
            await asyncio.sleep(1)
            try:
                due = knowledge_index().pop_due_reminders()
            except KnowledgeError as exc:
                error = str(exc)
                await run_in_terminal(
                    lambda: _write_log("error", f"checking reminders: {error}")
                )
                return
            if due:
                def announce():
                    for reminder_id, message in due:
                        console.print(f"REMINDER: {message}", style="bold yellow", markup=False)
                        audit.append("reminder_due", id=reminder_id)
                await run_in_terminal(announce)

    def start_reminder_loop():
        nonlocal reminder_task
        if reminder_task is None or reminder_task.done():
            reminder_task = session.app.create_background_task(reminder_loop())

    def toolbar():
        """Bottom-of-screen context bar, colored by pressure level."""
        level = agent.tracker.level(agent.messages)
        color = {"ok": "ansigreen", "prune": "ansiyellow", "compact": "ansiyellow", "stop": "ansired"}[level]
        mode = "think" if agent.thinking else "fast"
        relative_ws = Path(ws.root).relative_to(APP_DIR / "sessions")
        if policy.full_access:
            mode += " | FULL ACCESS" # loud on purpose
        if project:
            mode += f" | project: {project['name']}"
        if plan_next:
            mode += " | plan next"
        if agent.proposals and agent.proposals.count():
            mode += f" | {agent.proposals.count()} proposed note(s)"
        if ws.web and ws.web.tainted:
            mode += " | TAINTED"   # untrusted web content is in context
        return HTML(f'<style fg="{color}">ctx {agent.tracker.bar(agent.messages)}</style> | {mode} | <style fg="#C6C0BE">Workspace: {relative_ws}</style>')

    relative_ws = Path(ws.root).relative_to(APP_DIR / "sessions")
    
    show_intro(ws, project)

    while True:
        try:
            text = session.prompt(prompt_message, bottom_toolbar=toolbar,
                                  pre_run=start_reminder_loop).strip()
        except KeyboardInterrupt:
            continue          # Ctrl+C at the prompt just clears the line
        except EOFError:
            break             # Ctrl+D quits

        if not text:
            continue
        force_plan = False       # set below when the user typed /plan <request>

        # --- slash commands (more arrive in later phases) ---
        if text == "/exit":
            break
        if text == "/think" or text == "/think auto":
            if text == "/think auto":
                agent.thinking_override = None
                agent.thinking = False
                _write_log("info", "thinking override cleared; automatic setting will apply.")
                continue
            agent.thinking = not agent.thinking
            agent.thinking_override = agent.thinking
            _write_log("info", f"thinking: {'on' if agent.thinking else 'off'} (manual override)")
            continue
        if text == "/ws":
            console.print(str(ws.root), markup=False)
            continue
        if text == "/knowledge" or text.startswith("/knowledge "):
            parts = text.split(maxsplit=2)
            index = knowledge_index()
            action = parts[1].lower() if len(parts) > 1 else "status"
            argument = parts[2].strip().strip('"') if len(parts) > 2 else ""
            try:
                if action == "index" and argument:
                    result = index.index_folder(Path(argument))
                    audit.append("knowledge_index", root=result["root"],
                                 files_changed=result["files_changed"], chunks_added=result["chunks_added"])
                    _write_log("ok", f"Indexed {result['root']}: {result['files_changed']} changed, "
                                  f"{result['files_unchanged']} unchanged, {result['files_removed']} removed, "
                                  f"{result['chunks_added']} passages.")
                    if result["embedding_note"]:
                        _write_log("warning", result["embedding_note"])
                elif action == "search" and argument:
                    hits = index.search(argument)
                    audit.append("knowledge_search", hits=len(hits), query_chars=len(argument))
                    console.print(render_hits(hits), markup=False, highlight=False)
                    if index.embedding_note:
                        _write_log("warning", f"Semantic search unavailable: {index.embedding_note}")
                elif action == "watch" and argument:
                    root = index.start_watching(Path(argument))
                    audit.append("knowledge_watch", root=root, action="start")
                    _write_log("ok", f"Watching {root} while Danica is open.")
                elif action == "stop" and argument:
                    stopped = index.stop_watching(Path(argument))
                    audit.append("knowledge_watch", path=argument, action="stop", stopped=stopped)
                    console.print("Watcher stopped." if stopped else "No watcher for that folder.",
                                  markup=False)
                elif action == "digest" and argument in ("daily", "weekly"):
                    days = 1 if argument == "daily" else 7
                    changes = index.recent_changes(days)
                    if not changes:
                        console.print(f"No indexed notes changed in the last {days} day(s).",
                                      style="dim", markup=False)
                    else:
                        label = "Daily" if days == 1 else "Weekly"
                        console.print(f"{label} local knowledge digest:", style="bold", markup=False)
                        for root, path, excerpt in changes:
                            preview = " ".join((excerpt or "").split())[:240]
                            console.print(f"- {root}:{path}: {preview}", markup=False, highlight=False)
                    audit.append("knowledge_digest", period=action, entries=len(changes))
                elif action == "graph" and argument:
                    target = ws.resolve(argument)
                    output = index.write_graph(target)
                    audit.append("knowledge_graph_export", path=ws.display(output))
                    _write_log("ok", f"Interactive knowledge graph saved: {ws.display(output)}")
                elif action == "commit" and argument:
                    preview = ws.knowledge_previews.get(argument)
                    if preview is None:
                        _write_log("warning", "No such preview in this session.")
                        continue
                    target = Path(preview["path"])
                    console.print(f"Write-back preview for {target}:\n\n{preview['content']}",
                                  markup=False, highlight=False)
                    overwrite = target.exists()
                    question = ("Overwrite the existing note? [y/N] " if overwrite
                                else "Write this note? [y/N] ")
                    try:
                        answer = prompt(question).strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        answer = ""
                    if answer not in ("y", "yes"):
                        _write_log("warning", "No file was changed.")
                        continue
                    written = index.commit_obsidian_preview(ws, argument, overwrite=overwrite)
                    audit.append("obsidian_write", path=str(written), overwritten=overwrite)
                    _write_log("ok", f"Wrote {written}")
                elif action == "status":
                    status = index.status()
                    console.print(json.dumps(status, ensure_ascii=False, indent=2),
                                  markup=False, highlight=False)
                else:
                    console.print("Usage: /knowledge index <folder> | search <query> | "
                                  "watch <folder> | stop <folder> | status | digest daily|weekly | "
                                  "graph <workspace-file.html> | commit <preview-id>",
                                  markup=False)
            except (KnowledgeError, OSError) as exc:
                _write_log("error", f"{exc}")
            continue
        if text == "/reminder" or text.startswith("/reminder "):
            parts = text.split(maxsplit=3)
            index = knowledge_index()
            try:
                if len(parts) == 4 and parts[1] == "add":
                    reminder_id = index.add_reminder(parts[2], parts[3])
                    audit.append("reminder_add", id=reminder_id)
                    _write_log("ok", f"Reminder {reminder_id} scheduled.")
                elif len(parts) == 2 and parts[1] == "list":
                    reminders = index.list_reminders()
                    if not reminders:
                        console.print("No pending reminders.", markup=False)
                    for reminder_id, due, message in reminders:
                        local_time = datetime.fromtimestamp(due).astimezone().isoformat(timespec="minutes")
                        console.print(f"{reminder_id}  {local_time}  {message}", markup=False)
                else:
                    console.print("Usage: /reminder add <ISO datetime> <text> | /reminder list",
                                  markup=False)
            except KnowledgeError as exc:
                _write_log("error", f"{exc}")
            continue
        if text == "/agents" or text.startswith("/agents "):
            parts = text.split(maxsplit=2)
            supervisor = getattr(ws, "subagent_supervisor", None)
            if supervisor is None:
                _write_log("warning", "subagent supervisor is unavailable")
                continue
            if len(parts) == 1:
                children = supervisor.list()
                if not children:
                    console.print("No subagents have run in this session.", style="dim", markup=False)
                for child in children:
                    console.print(f"{child['id']}  {child['status']}  "
                                  f"{child['elapsed_seconds']}s  {child['task']}", markup=False)
                continue
            if len(parts) == 3 and parts[1] == "run":
                try:
                    child_id = supervisor.start(parts[2])
                    _write_log("ok", f"Subagent {child_id} started. Use /agents to monitor or "
                                  f"/agents kill {child_id} to stop it.")
                except SubagentError as exc:
                    _write_log("error", f"{exc}")
                continue
            if len(parts) == 3 and parts[1] == "peek":
                child = next((item for item in supervisor.list() if item["id"] == parts[2]), None)
                console.print(json.dumps(child, ensure_ascii=False, indent=2) if child
                              else "No such subagent.", markup=False)
                continue
            if len(parts) == 3 and parts[1] == "wait":
                try:
                    finished = supervisor.wait(parts[2])
                    child = next((item for item in supervisor.list() if item["id"] == parts[2]), None)
                    console.print(json.dumps(child, ensure_ascii=False, indent=2) if finished and child
                                  else "Subagent is still running." if child else "No such subagent.",
                                  markup=False)
                except SubagentError as exc:
                    _write_log("error", f"{exc}")
                continue
            if len(parts) == 3 and parts[1] == "kill":
                stopped = supervisor.kill(parts[2])
                _write_log("warning", "Subagent stopped." if stopped else "No running subagent with that id.")
                continue
            _write_log("warning", "Usage: /agents [run <task> | peek <id> | wait <id> | kill <id>]")
            continue
        if text == "/context":
            console.print(agent.tracker.bar(agent.messages), markup=False)
            for role, tokens in sorted(breakdown(agent.messages).items(), key=lambda kv: -kv[1]):
                console.print(f"  {role:<10} ~{tokens:,} tokens", markup=False)
            continue
        if text == "/audit":
            ok, count, problem = verify(audit.path)
            if ok:
                _write_log("ok", f"audit log OK: {count} entries. chain intact")
            else:
                console.print(f"AUDIT LOG PROBLEM: {problem}", style="bold red", markup=False)
            continue
        if text == "/fullaccess":
            policy.full_access = not policy.full_access
            audit.append("full_access", enabled=policy.full_access)
            if policy.full_access:
                console.print("FULL ACCESS ON: Danica can run ANY command without asking. /fullaccess again to turn it off.", style="bold red", markup=False)
            else:
                _write_log("warning", "full access off")
            continue
        if text.split()[0] in ("/undo", "/rewind"):
            if not checkpoints.available:
                _write_log("warning", "checkpoints are disabled (git not found)")
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
                _write_log("ok", summary)
                if safety:
                    audit.append("restore", command=parts[0], safety=safety)
            except CheckpointError as e:
                _write_log("error", f"{e}")
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
                _write_log("warning", "TODO cleared")
            else:
                console.print(read_todo(ws) or "(no TODO yet)", markup=False)
            continue

        if text == "/prune":
            # Manual prune, keeping only the 2 newest tool results (handy for testing)
            if not agent.prune(keep_recent=2):
                _write_log("warning", "nothing to prune")
            continue

        if text.split()[0] == "/compact":
            # "/compact" uses the normal tail; "/compact 500" keeps only ~500 tokens verbatim
            parts = text.split()
            tail = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
            if not agent.compact(tail):
                _write_log("warning", "nothing worth compacting yet: everything fits in the kept tail. "
                              "Try /compact 500 to keep less.")
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
                _write_log("error", f"{e}")
                continue
            except Exception as e:      # the old session stays active if loading failed
                _write_log("error", f"could not resume: {type(e).__name__}: {e}")
                continue
            ws, audit, checkpoints, policy, agent = parts    # switch over only on success
            session_id = target
            meta = load_session_meta(SESSIONS_DIR / target)
            project = lookup(Path(meta["project"])) if meta.get("project") else None
            _write_log("ok", report)
            continue
        if text == "/taint":
            if ws.web and ws.web.tainted:
                _write_log("warning", "Untrusted web content is in this conversation. Sources:")
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
                _write_log("warning", "taint cleared. The web content is still in the context.")
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
                        _write_log("warning", "not trusted, nothing changed")
                        continue
                    info = register(path)
                    audit.append("project_trust", path=str(path), id=info["id"])
                new_id = datetime.now().strftime("%Y%m%d-%H%M%S")
                new_parts = build_session(new_id, finish, on_event, info)
            except ProjectError as e:
                _write_log("error", f"{e}")
                continue
            ws, audit, checkpoints, policy, agent = new_parts
            session_id, project = new_id, info
            audit.append("session_start", workspace=str(ws.root), mode="project", project=info["id"])
            _write_log("ok", f"Project mode: {info['name']}\n  folder: {ws.root}\n"
                          f"New session {new_id}. The previous session stays available via /sessions and /resume.")
            continue

        if text == "/chat":
            session_id = datetime.now().strftime("%Y%m%d-%H%M%S")
            project = None
            ws, audit, checkpoints, policy, agent = build_session(session_id, finish, on_event)
            audit.append("session_start", workspace=str(ws.root), mode="chat")
            _write_log("ok", f"Normal chat. New session {session_id}.")
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
        if text == "/skills":
            skills, errors = skill_catalog()
            console.print(f"Global skills: {SKILLS_DIR}", markup=False, highlight=False)
            if skills:
                for skill in skills:
                    console.print(f"  {skill.name}: {skill.description}", markup=False, highlight=False)
            else:
                console.print("(no global skills found; add a folder with a SKILL.md)", markup=False)
            for error in errors:
                _write_log("warning", f"WARNING: {error}")
            continue

        if text == "/new":
            session_id = datetime.now().strftime("%Y%m%d-%H%M%S")
            # Same mode as now: a project session reopens the same folder (and asks about
            # DANICA.md only if it changed); a chat session gets a new throwaway workspace
            ws, audit, checkpoints, policy, agent = build_session(session_id, finish, on_event, project)
            audit.append("session_start", workspace=str(ws.root),
                         mode="project" if project else "chat")
            _write_log("ok", f"New session {session_id}.")
            continue

        if text == "/export":
            try:
                out = export_session(APP_DIR / "sessions" / session_id, ws, project)
            except (ValueError, OSError) as e:
                _write_log("error", f"{e}")
                continue
            audit.append("export", folder=str(out))
            _write_log("ok", f"Exported to {out}\nRead conversation.md before sharing it: redaction "
                          f"only catches well-known secret formats.")
            continue

        if text.split()[0] == "/memory":
            handle_memory(text, agent.memory, audit, project is not None, agent)
            continue
        if text == "/imagetest":
            display(test_image(), "test image", console)
            console.print("You should see a framed picture with six coloured bars and a grey gradient.\n"
                          "Garbage text, or nothing at all, means this terminal can't show Sixel images: set\n"
                          '[display] images = "link" in config.toml (inline images need Windows Terminal 1.22+).',
                          markup=False)
            continue
        if text.split()[0] in ("/open", "/save"):
            handle_artifact_command(text, artifacts, audit)
            continue
        if text == "/artifacts":
            show_artifacts(ArtifactList(items=artifacts))
            continue
        if text == "/diagrams":
            for line in diagram_status():
                console.print(line, markup=False, highlight=False)
            continue
        if text.split()[0] == "/plan":
            rest = text[len("/plan"):].strip()
            if not rest:
                plan_next = True
                _write_log("ok", "Plan mode: your next request gets a plan first (read-only drafting, then you approve).")
                continue
            if rest.lower() == "show":
                if agent.last_plan:
                    show_plan(agent.last_plan)
                else:
                    console.print("(no plan yet in this session)", markup=False)
                continue
            if rest.lower().startswith("auto"):
                value = rest[4:].strip().lower()
                if value in ("ask", "off"):
                    plan_auto = value
                    console.print(f"automatic plan offers: {value}", markup=False)
                else:
                    console.print("usage: /plan auto ask | /plan auto off", markup=False)
                continue
            if rest.startswith("/"):
                _write_log("warning", "a slash command can't be planned")
                continue
            text, force_plan = rest, True          # no "continue": it runs as an ordinary request below, with a plan
        # --- plan mode (hybrid): /plan forces it, and big-looking requests are offered one ---
        decision, plan, plan_auto = run_plan_mode(text, force_plan or plan_next, plan_auto, agent, audit)
        plan_next = False
        if decision == "cancel":
            _write_log("warning", "nothing was run")
            console.print()
            continue
        # --- normal request: hand it to the agent ---
        try:
            printer.begin_request()
            result = agent.run(text, plan=plan)
            finish()
            # Normal answers were already streamed to the screen; only the
            # harness's "[stopped: ...]" notices still need printing
            if result.startswith("[stopped]"):
                _write_log("warning", result)
            printer.present_pending(agent.charts_made)    # charts the answer forgot to show
            found = collect(ws, agent.checkpoints, agent.request_snap, agent.touched, printer.presented)
            artifacts = found.items
            show_artifacts(found)
        except KeyboardInterrupt:
            try:
                finish()
                _write_log("warning", "[interrupted]")
            except Exception as e:
                _write_log("error", f"{e}")
        except Exception as e:
            # Most likely cause: llama-server isn't running
            finish()
            # console.print(f"ERROR: {type(e).__name__}: {e}", style="red", markup=False)
            _write_log("error", f"{type(e).__name__}: {e}")

        console.print()  # blank line between turns

    if getattr(ws, "subagent_supervisor", None) is not None:
        try:
            ws.subagent_supervisor.close()
        except SubagentError as exc:
            _write_log("error", f"stopping subagents: {exc}")
    if _KNOWLEDGE_INDEX is not None:
        try:
            _KNOWLEDGE_INDEX.close()
        except KnowledgeError as exc:
            _write_log("error", f"stopping knowledge watchers: {exc}")
    if _EXTENSION_MANAGER is not None:
        try:
            _EXTENSION_MANAGER.close()
        except ExtensionError as exc:
            _write_log("error", f"stopping extensions: {exc}")

if __name__ == "__main__":
    main()