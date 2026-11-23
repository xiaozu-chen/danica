"""
managers.py - what the manager panes (Memories, Knowledge, Skills, Reminders, Web cache) list and what their buttons do.

No Textual in here: a Manager is plain data plus functions, so every action can be tested without a screen. The pane
itself is tui/manager_screen.py, which shows the rows and runs the actions.

Everything here runs on the ENGINE thread (the same thread that runs slash commands), because the notes, the knowledge
index and the web cache sit on SQLite connections that may only be used from that thread. A function raises
ManagerError with a message for the user when something is not allowed; any other exception is shown as an internal error.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable


class ManagerError(Exception):
    """A problem whose message is fit to show the user."""


@dataclass
class Row:
    """One entry in the list: what it is called, a short right-hand hint, and the full text for the detail area."""

    key: str                 # what the actions get back (an id, a folder name, a path...)
    title: str
    detail: str
    hint: str = ""


@dataclass
class Action:
    """One button. `run(app, row, text)` returns a message, or (message, text for the detail area)."""

    id: str
    label: str
    run: Callable[[Any, "Row | None", str], "str | tuple[str, str]"] | None = None
    needs_row: bool = False      # greyed out until a row is selected
    input_label: str = ""        # not empty: the button asks for text first (this is the prompt shown in the box)
    confirm: str = ""            # not empty: asks yes/no first, with this as the question
    command: str = ""            # instead of run: close the pane and run this slash command in the chat (it asks its own questions)


@dataclass
class Manager:
    """One pane: how to load its list and which buttons it has."""

    key: str
    title: str
    empty: str                                           # shown when the list is empty
    load: Callable[[Any], "tuple[list[Row], str]"]       # (rows, one-line summary)
    actions: list[Action] = field(default_factory=list)


def _folder_name(name: str) -> str:
    """A skill folder name that cannot point anywhere else."""
    if not name or name in (".", "..") or Path(name).name != name or "/" in name or "\\" in name:
        raise ManagerError("that is not a valid skill folder name")
    return name


# ------------------------------------------------------------------ memories

def _memory_load(app):
    store = app.agent.memory
    pages = store.list_pages()
    rows = []
    for p in pages:
        detail = (f"[{p.scope}] {p.title}\n\nid {p.id}   source {p.source}   confidence {p.confidence}\n"
                  f"created {p.created}   updated {p.updated}\n\n{p.body}")
        rows.append(Row(p.uuid, f"[{p.scope}] {p.summary}", detail, p.updated[:10]))
    waiting = app.agent.proposals.count() if getattr(app.agent, "proposals", None) else 0
    summary = f"{len(pages)} saved note(s)"
    if waiting:
        summary += f", {waiting} proposed note(s) waiting: use Review proposals"
    if store.skipped:
        summary += f", {store.skipped} file(s) skipped (not valid notes)"
    return rows, summary


def _memory_add(app, row, text):
    from danica.memory import SCOPES, MemoryStoreError
    words = text.strip().split(maxsplit=1)
    if len(words) == 2 and words[0].lower() in SCOPES:            # "global <text>" / "project <text>"
        scope, body = words[0].lower(), words[1]
    else:
        scope, body = ("project" if app.project is not None else "global"), text.strip()
    try:
        page = app.agent.memory.add(scope, body)
    except MemoryStoreError as e:
        raise ManagerError(str(e)) from e
    app.audit.append("memory_add", scope=scope, id=page.id)        # the text itself is never logged
    return f"Saved to {scope} memory (id {page.id})"


def _memory_forget(app, row, text):
    from danica.memory_ui import forget_note
    store = app.agent.memory
    page = store.get(row.key)
    if page is None:
        raise ManagerError("that note is already gone")
    ok = forget_note(store, page, app.ui)                          # prints every check in the chat, as /memory forget does
    app.audit.append("memory_forget", scope=page.scope, id=page.id, ok=ok)
    return f"Forgot note {page.id}" + ("" if ok else " (a check failed: see the chat)")


# ------------------------------------------------------------------ skills

def _skills_load(app):
    from danica.tools.skills import skill_catalog
    skills, errors = skill_catalog()
    rows = []
    for s in skills:
        try:
            text = s.path.read_text(encoding="utf-8-sig")[:6000]
        except OSError:
            text = "(could not read the file)"
        rows.append(Row(s.name, s.name, f"{s.description}\n\nfile: {s.path}\n\n{'-' * 24}\n{text}"))
    for error in errors:
        folder = error.split(":", 1)[0].strip()
        rows.append(Row("!" + folder, f"! {folder}", error + "\n\nThis skill is not loaded. Use Edit to fix its SKILL.md, or Delete.", "problem"))
    return rows, f"{len(skills)} skill(s) loaded" + (f", {len(errors)} with problems" if errors else "")


def _skill_folder(row) -> Path:
    from danica.config import SKILLS_DIR
    folder = SKILLS_DIR / _folder_name(row.key.lstrip("!"))
    if folder.is_symlink() or not folder.is_dir() or folder.resolve().parent != SKILLS_DIR.resolve():
        raise ManagerError("that skill folder is not where Danica keeps skills")
    return folder


def _open_in_system(path: Path) -> None:
    """Open a file or folder with the system's default app."""
    from danica import artifacts
    try:
        artifacts._startfile(str(path))
    except (OSError, AttributeError) as e:                         # AttributeError: not Windows
        raise ManagerError(f"could not open it here ({type(e).__name__}); it is at {path}") from e


def _skill_new(app, row, text):
    from danica.config import SKILLS_DIR
    from danica.tools.skills import MAX_DESCRIPTION_CHARS, NAME_RX
    words = text.strip().split(maxsplit=1)
    if not words:
        raise ManagerError("type a name for the skill, e.g.  meeting-notes  Turn a meeting transcript into notes")
    name = words[0]
    description = " ".join(words[1].split()) if len(words) > 1 else "Describe here when Danica should use this skill."
    if not NAME_RX.fullmatch(name):
        raise ManagerError("use lowercase letters, digits, - . _ for the name (it is also the folder name)")
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise ManagerError(f"the description may be at most {MAX_DESCRIPTION_CHARS} characters")
    folder = SKILLS_DIR / name
    if folder.exists():
        raise ManagerError(f"a skill called {name} already exists")
    folder.mkdir(parents=True)
    path = folder / "SKILL.md"
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n"
                    "Write the steps Danica should follow here.\n", encoding="utf-8")
    app.audit.append("skill_create", name=name)
    try:
        _open_in_system(path)
        opened = " and opened it for editing"
    except ManagerError:
        opened = f" at {path}"
    return f"Created skill {name}{opened}. A new chat picks it up."


def _skill_edit(app, row, text):
    _open_in_system(_skill_folder(row) / "SKILL.md")
    return f"opened {row.key.lstrip('!')}/SKILL.md for editing. A new chat picks up changes."


def _skill_open_folder(app, row, text):
    _open_in_system(_skill_folder(row))
    return f"opened the folder of {row.key.lstrip('!')}"


def _skill_delete(app, row, text):
    folder = _skill_folder(row)
    shutil.rmtree(folder)
    app.audit.append("skill_delete", name=folder.name)
    return f"Deleted skill {folder.name}. A new chat no longer has it."


# ------------------------------------------------------------------ knowledge

def _knowledge_load(app):
    from danica.app_core import knowledge_index
    index = knowledge_index()
    state, roots = index.status(), index.roots()
    watching = [Path(w) for w in state["watching"]]
    recent: dict[str, list[str]] = {}
    for root, path, _ in index.recent_changes(7, 100):
        recent.setdefault(str(Path(root)), []).append(path)
    rows = []
    for root in roots:
        files = recent.get(str(root), [])
        detail = (f"{root}\n\n{'watching for changes while Danica is open' if root in watching else 'not being watched'}\n"
                  + (f"\nwatcher problem: {state['watch_errors'][str(root)]}\n" if str(root) in state["watch_errors"] else "")
                  + (f"\nchanged in the last 7 days ({len(files)}):\n" + "\n".join("  " + f for f in files[:12])
                     + ("\n  ..." if len(files) > 12 else "") if files else "\nnothing changed in the last 7 days"))
        rows.append(Row(str(root), str(root), detail, "watching" if root in watching else ""))
    summary = f"{state['files']} file(s), {state['chunks']} passage(s) in {len(roots)} folder(s)"
    if state["embedding_note"]:
        summary += f". Meaning search is off: {state['embedding_note']}"
    return rows, summary


def _knowledge_call(fn, *args):
    from danica.knowledge import KnowledgeError
    try:
        return fn(*args)
    except (KnowledgeError, OSError, ValueError) as e:
        raise ManagerError(str(e)) from e


def _index_message(result: dict) -> str:
    return (f"Indexed {result['root']}: {result['files_changed']} changed, {result['files_unchanged']} unchanged, "
            f"{result['files_removed']} removed, {result['chunks_added']} passages.")


def _knowledge_index(app, row, text):
    from danica.app_core import knowledge_index
    path = Path(text.strip().strip('"')).expanduser()
    if not path.is_dir():
        raise ManagerError(f"{path} is not a folder")
    result = _knowledge_call(knowledge_index().index_folder, path)
    app.audit.append("knowledge_index", root=result["root"], files_changed=result["files_changed"],
                     chunks_added=result["chunks_added"])
    return _index_message(result)


def _knowledge_reindex(app, row, text):
    return _knowledge_index(app, row, row.key)


def _knowledge_watch(app, row, text):
    from danica.app_core import knowledge_index
    root = _knowledge_call(knowledge_index().start_watching, Path(row.key))
    app.audit.append("knowledge_watch", root=root, action="start")
    return f"Watching {root} while Danica is open."


def _knowledge_unwatch(app, row, text):
    from danica.app_core import knowledge_index
    stopped = _knowledge_call(knowledge_index().stop_watching, Path(row.key))
    app.audit.append("knowledge_watch", path=row.key, action="stop", stopped=stopped)
    return "Watcher stopped." if stopped else "That folder was not being watched."


def _knowledge_search(app, row, text):
    from danica.app_core import knowledge_index
    from danica.knowledge import render_hits
    hits = _knowledge_call(knowledge_index().search, text.strip())
    app.audit.append("knowledge_search", hits=len(hits), query_chars=len(text))
    return f"{len(hits)} passage(s) found", render_hits(hits) if hits else "Nothing in the index matches."


# ------------------------------------------------------------------ reminders

_REL = re.compile(r"^in\s+(\d+)\s*(m|min|mins|minutes?|h|hrs?|hours?|d|days?)\s+(.+)$", re.I | re.S)
_ABS = re.compile(r"^(\d{4}-\d{2}-\d{2}|today|tomorrow)[ T](\d{1,2}:\d{2})\s+(.+)$", re.I | re.S)


def parse_when(text: str, now: datetime | None = None) -> tuple[str, str]:
    """'in 30m call mom' / 'tomorrow 09:00 stand-up' / '2026-12-01 14:30 pay rent' -> (ISO local time, message)."""
    now = now or datetime.now()
    text = text.strip()
    if m := _REL.match(text):
        amount, unit = int(m.group(1)), m.group(2).lower()[0]
        due = now + timedelta(**{"minutes": amount} if unit == "m" else {"hours": amount} if unit == "h" else {"days": amount})
        return due.replace(second=0, microsecond=0).isoformat(timespec="minutes"), m.group(3).strip()
    if m := _ABS.match(text):
        day = m.group(1).lower()
        base = now.date() if day == "today" else (now + timedelta(days=1)).date() if day == "tomorrow" else None
        try:
            hour, minute = (int(p) for p in m.group(2).split(":"))
            date = base or datetime.strptime(day, "%Y-%m-%d").date()
            due = datetime(date.year, date.month, date.day, hour, minute)
        except ValueError as e:
            raise ManagerError("that date or time does not exist") from e
        return due.isoformat(timespec="minutes"), m.group(3).strip()
    raise ManagerError("write when, then what:  in 30m call mom   |   tomorrow 09:00 stand-up   |   2026-12-01 14:30 pay rent")


def _reminders_load(app):
    from danica.app_core import knowledge_index
    rows = []
    for rid, due, message in knowledge_index().list_reminders():
        when = datetime.fromtimestamp(due).astimezone()
        rows.append(Row(rid, message, f"{message}\n\ndue {when:%A %d %B %Y, %H:%M}\nid {rid}", f"{when:%d %b %H:%M}"))
    return rows, f"{len(rows)} pending reminder(s)"


def _reminder_add(app, row, text):
    from danica.app_core import knowledge_index
    due, message = parse_when(text)
    reminder_id = _knowledge_call(knowledge_index().add_reminder, due, message)
    app.audit.append("reminder_add", id=reminder_id)
    return f"Reminder set for {due.replace('T', ' ')}"


def _reminder_delete(app, row, text):
    from danica.app_core import knowledge_index
    removed = knowledge_index().delete_reminder(row.key)
    app.audit.append("reminder_delete", id=row.key, removed=removed)
    return "Reminder removed." if removed else "That reminder was already gone."


# ------------------------------------------------------------------ web cache

def _cache():
    from danica.webcache import get_web_cache
    cache = get_web_cache()
    if cache is None:
        raise ManagerError("the web cache is switched off ([webcache] enabled = false in config.toml, or Settings)")
    return cache


def _webcache_load(app):
    from danica.webcache import age_text
    cache = _cache()
    state = cache.status()
    rows = [Row(url, title or url, f"{title or '(no title)'}\n{url}\n\nstored {age_text(age)} ago"
                + ("  (past its freshness window)" if stale else ""), age_text(age) + (" old" if stale else ""))
            for url, title, age, stale in cache.recent(200)]
    summary = (f"{state['pages']} page(s) ({state['stale_pages']} stale), {state['searches']} search(es)"
               + (f". Meaning search is off: {state['embedding_note']}" if state["embedding_note"] else ""))
    return rows, summary


def _webcache_search(app, row, text):
    hits = _cache().search(text.strip(), 8)
    lines = [f"{h.score:.4f}  {h.title[:60] or '(no title)'}\n        {h.url}  ({h.via})" for h in hits]
    return f"{len(hits)} stored page(s) match", "\n".join(lines) or "Nothing stored matches."


def _webcache_reindex(app, row, text):
    result = _cache().reindex()
    return "Rebuilt the web cache index" + (f" ({result})" if isinstance(result, dict) and result else "")


def _webcache_clear(app, row, text):
    count = _cache().clear()
    app.audit.append("webcache_clear", pages=count)
    return f"Cleared the web cache ({count} page(s))."


# ------------------------------------------------------------------ sessions

def _sessions_load(app):
    """The saved sessions, newest first. The one you are in is marked "current"."""
    from danica import sessions
    rows = []
    for info in sessions.list_sessions(sessions.SESSIONS_DIR):
        title = info.get("title") or info["preview"] or "(untitled)"
        here = info["id"] == app.session_id
        detail = (f"{title}\n\nid {info['id']}\nlast active {info['modified']:%d %b %Y %H:%M}\n"
                  f"{info['requests']} request(s), {info['compactions']} compaction(s)\n\nfirst message: {info['preview']}")
        rows.append(Row(info["id"], title, detail, "current" if here else f"{info['modified']:%d %b %H:%M}"))
    return rows, f"{len(rows)} saved session(s). Resume opens one; Delete removes it for good."


def _sessions_delete(app, row, text):
    from danica import sessions
    from danica.uploads import human
    if row.key == app.session_id:
        raise ManagerError("this is the session you are in: start a New chat first, then delete it")
    size = sessions.describe_session(sessions.SESSIONS_DIR, row.key)["size"]
    try:
        gone = sessions.delete_session(sessions.SESSIONS_DIR, row.key)       # only ever a folder named like a session id
    except (ValueError, OSError) as e:
        raise ManagerError(f"could not delete it: {e}") from e
    if not gone:
        raise ManagerError("could not fully delete it (a file may be open in another program)")
    app.audit.append("sessions_deleted", ids=[row.key])
    return f"Deleted session {row.key} (freed {human(size)})"


# ------------------------------------------------------------------ context

# What each kind of message in the conversation is, for the detail area
ROLE_NOTES = {
    "system": "Danica's standing instructions, the tool list and any project notes. Sent with every request.",
    "user": "What you typed (and any attached pictures).",
    "assistant": "Danica's replies and the tool calls she made.",
    "tool": "What the tools returned: file contents, command output, web pages. Usually the biggest part; "
            "Prune drops the old ones.",
}


def _context_load(app):
    """Where the context window is going: one row per kind of message, then the compaction summary and the TODO list."""
    from danica.compaction import is_summary
    from danica.context import breakdown
    from danica.tools.todo import counts, read_todo
    agent = app.agent
    if agent is None:
        return [], "Danica is still starting"
    messages = agent.messages
    used = breakdown(messages)
    total = sum(used.values()) or 1
    rows = []
    for role, tokens in sorted(used.items(), key=lambda item: -item[1]):
        n = sum(1 for m in messages if m.get("role") == role)
        detail = f"{role}: {n} message(s), about {tokens:,} tokens ({tokens * 100 // total}% of the conversation)\n\n" \
                 + ROLE_NOTES.get(role, "")
        rows.append(Row("role:" + role, f"{role:<10} ~{tokens:,} tokens", detail, f"{tokens * 100 // total}%"))
    if len(messages) > 1 and is_summary(messages[1]):
        rows.append(Row("summary", "Compaction summary", messages[1]["content"], "yes"))
    else:
        rows.append(Row("summary", "Compaction summary", "(no compaction has happened yet)", "none"))
    todo = read_todo(app.ws)
    if todo:
        count, done = counts(todo)
        rows.append(Row("todo", "TODO list", todo, f"{done}/{count} done"))
    else:
        rows.append(Row("todo", "TODO list", "(no TODO yet)", "none"))
    return rows, agent.tracker.bar(messages) + f"   {len(messages)} messages"


def _context_prune(app, row, text):
    if not app.agent.prune(keep_recent=2):                 # keeps only the 2 newest tool results
        raise ManagerError("nothing to prune")
    return "Dropped the old tool results (kept the 2 newest)"


def _context_clear_todo(app, row, text):
    path = app.ws.todo_path
    if path is None or not path.exists():
        raise ManagerError("there is no TODO list")
    path.unlink()
    return "TODO cleared"


# ------------------------------------------------------------------ safety

def _safety_load(app):
    """The switches that decide what Danica may do without asking, and the log that records it."""
    from danica.config import CONFIG
    policy, web = app.policy, app.ws.web
    full = bool(policy.full_access)
    tainted = bool(web and web.tainted)
    sb = CONFIG["sandbox"]
    rows = [
        Row("access", "Permissions", (
            "FULL ACCESS is ON: Danica runs ANY command without asking." if full else
            "Ask first: Danica asks before commands that could change things, and before anything outside the workspace.")
            + "\n\nAllow everything = full access.  Ask when needed = the normal mode.", "FULL ACCESS" if full else "ask first"),
        Row("taint", "Untrusted web content", (
            "Web content is in this conversation, from:\n" + "\n".join("  " + src for src in web.taint_sources)
            + "\n\nWhile tainted, shell commands and fetches from new sites always ask.\n"
              "Clear taint lowers the flag; it does not remove the content from the conversation." if tainted else
            "Nothing untrusted is in the conversation, so no extra caution is needed."), "TAINTED" if tainted else "clean"),
        Row("sandbox", "Shell sandbox", (
            "level 1: workspace path jail for file tools, stripped environment variables, timeout, output cap, permission prompts\n"
            f"level 2: Job Object: {sb['memory_mb']} MB memory cap, {sb['max_processes']} process, the whole process tree killed on "
            "timeout/exit/crash, no clipboard/desktop access\nlevel 3: Docker/WSL isolation: not built yet\n\n"
            "What this does NOT stop: a command reading or writing files outside the workspace (only the permission prompts guard "
            "that), network access, CPU use."), "level 2"),
        Row("audit", "Audit log", f"Every sensitive action is written to a hash-chained log:\n{app.audit.path}\n\n"
            "Verify walks the whole chain and reports if any entry was changed or removed.", ""),
    ]
    return rows, f"Permissions: {'FULL ACCESS' if full else 'ask first'}   Web content: {'TAINTED' if tainted else 'clean'}"


def _safety_full_on(app, row, text):
    app.policy.full_access = True
    app.audit.append("full_access", enabled=True)
    return "FULL ACCESS ON: Danica runs any command without asking"


def _safety_full_off(app, row, text):
    app.policy.full_access = False
    app.audit.append("full_access", enabled=False)
    return "Back to asking when needed"


def _safety_untaint(app, row, text):
    web = app.ws.web
    if not (web and web.tainted):
        raise ManagerError("nothing is tainted")
    web.clear_taint()
    app.audit.append("untaint")
    return "Taint cleared (the web content is still in the conversation)"


def _safety_audit(app, row, text):
    from danica.audit import verify
    ok, count, problem = verify(app.audit.path)
    if not ok:
        raise ManagerError(f"AUDIT LOG PROBLEM: {problem}")
    return f"Audit log OK: {count} entries, chain intact"


# ------------------------------------------------------------------ checkpoints

def _checkpoints_load(app):
    """The snapshots Danica takes of the workspace at the start of every request (and safety copies from restores)."""
    cps = app.checkpoints
    if cps is None or not cps.available:
        return [], "Checkpoints are off (git was not found)"
    rows = []
    for ident, when, label in cps.list_checkpoints():
        rows.append(Row(ident, label or "(no label)", f"checkpoint {ident}\n{when}\n\n{label}\n\n"
                        "Rewind makes the workspace match this snapshot again.", when))
    return rows, f"{len(rows)} checkpoint(s). Undo reverts the last request; Rewind jumps to the selected one."


def _checkpoint_result(app, command, summary, safety):
    """Audit the restore, print the full summary (which files changed, how to undo it) in the chat like /undo does, and
    return only its first line as the panel's message. Returning plain text, not (text, detail), makes the list reload,
    so the new safety copy shows up."""
    if safety:
        app.audit.append("restore", command=command, safety=safety)
    app.ui.log("ok", summary)
    return summary.splitlines()[0]


def _checkpoints_undo(app, row, text):
    from danica.checkpoints import CheckpointError
    try:
        summary, safety = app.checkpoints.undo()
    except CheckpointError as e:
        raise ManagerError(str(e)) from e
    return _checkpoint_result(app, "/undo", summary, safety)


def _checkpoints_rewind(app, row, text):
    from danica.checkpoints import CheckpointError
    try:
        summary, safety = app.checkpoints.rewind(row.key)
    except CheckpointError as e:
        raise ManagerError(str(e)) from e
    return _checkpoint_result(app, "/rewind", summary, safety)


# ------------------------------------------------------------------ web sources

def _sources_load(app):
    """The web pages Danica has read in this session. The row key is the page's address."""
    web = app.ws.web
    sources = list(web.by_id.values()) if web else []
    rows = []
    for s in sources:
        url = s.final_url or s.url
        rows.append(Row(url, s.title[:80] or "(no title)", f"{s.title or '(no title)'}\n\n{url}\n\n{len(s.passages)} passage(s) kept",
                        f"[{s.id}]"))
    return rows, f"{len(rows)} page(s) read in this session"


def _sources_copy(app, row, text):
    from danica import artifacts
    try:
        artifacts._clipboard(row.key)
    except Exception as e:                                   # no PowerShell, clipboard locked...
        raise ManagerError(f"could not copy: {type(e).__name__}") from e
    return "Link copied to the clipboard"


def _sources_open(app, row, text):
    import webbrowser
    if not row.key.lower().startswith(("http://", "https://")):          # never hand a file: or other scheme to the system
        raise ManagerError("that is not a web address")
    webbrowser.open(row.key)
    return "Opened in your browser"


# ------------------------------------------------------------------ the list

MANAGERS: dict[str, Manager] = {m.key: m for m in (
    Manager("memories", "Memories", "No saved notes yet. Use Add note.", _memory_load, [
        Action("add", "Add note", _memory_add, input_label="Note to remember (start with global or project to choose where)"),
        Action("forget", "Forget", _memory_forget, needs_row=True, confirm="Forget this note for good?"),
        Action("review", "Review proposals", command="/memory review"),
        Action("merge", "Merge duplicates", command="/memory merge"),
    ]),
    Manager("knowledge", "Knowledge", "No folders indexed yet. Use Index folder.", _knowledge_load, [
        Action("index", "Index folder", _knowledge_index, input_label="Folder to index (full path)"),
        Action("reindex", "Re-index", _knowledge_reindex, needs_row=True),
        Action("watch", "Watch", _knowledge_watch, needs_row=True),
        Action("unwatch", "Stop watching", _knowledge_unwatch, needs_row=True),
        Action("search", "Search", _knowledge_search, input_label="Words to look for in your indexed notes"),
    ]),
    Manager("skills", "Skills", "No skills yet. Use New skill.", _skills_load, [
        Action("new", "New skill", _skill_new, input_label="name  and an optional one-line description"),
        Action("edit", "Edit", _skill_edit, needs_row=True),
        Action("folder", "Open folder", _skill_open_folder, needs_row=True),
        Action("delete", "Delete", _skill_delete, needs_row=True, confirm="Delete this skill folder?"),
    ]),
    Manager("reminders", "Reminders", "No pending reminders. Use Add reminder.", _reminders_load, [
        Action("add", "Add reminder", _reminder_add, input_label="when, then what:  in 30m call mom  |  tomorrow 09:00 stand-up"),
        Action("delete", "Remove", _reminder_delete, needs_row=True),
    ]),
    Manager("webcache", "Web cache", "The web cache is empty.", _webcache_load, [
        Action("search", "Search", _webcache_search, input_label="Words to look for in stored pages"),
        Action("reindex", "Rebuild index", _webcache_reindex),
        Action("clear", "Clear all", _webcache_clear, confirm="Delete every stored web page?"),
    ]),
    Manager("sessions", "Sessions", "No saved sessions yet.", _sessions_load, [
        Action("resume", "Resume", needs_row=True, command="/resume {key}"),
        Action("export", "Export this chat", command="/export"),
        Action("delete", "Delete", _sessions_delete, needs_row=True,
               confirm="Delete this saved session for good? (Only Danica's saved conversation goes; a project folder is never touched.)"),
    ]),
    Manager("context", "Context", "Nothing in the conversation yet.", _context_load, [
        Action("prune", "Prune", _context_prune, confirm="Drop all but the 2 newest tool results from the conversation?"),
        Action("compact", "Compact", command="/compact"),
        Action("compact_small", "Compact (keep less)", command="/compact 500"),
        Action("cleartodo", "Clear TODO", _context_clear_todo, confirm="Delete the TODO list?"),
    ]),
    Manager("checkpoints", "Checkpoints", "No checkpoints yet. One is saved at the start of every request.", _checkpoints_load, [
        Action("undo", "Undo last request", _checkpoints_undo,
               confirm="Put the files back the way they were before the last request? (a safety copy is saved first)"),
        Action("rewind", "Rewind to this", _checkpoints_rewind, needs_row=True,
               confirm="Make the workspace match this checkpoint? (a safety copy is saved first)"),
    ]),
    Manager("safety", "Safety", "", _safety_load, [
        Action("full_on", "Allow everything", _safety_full_on,
               confirm="Turn FULL ACCESS on? Danica will run ANY command without asking."),
        Action("full_off", "Ask when needed", _safety_full_off),
        Action("untaint", "Clear taint", _safety_untaint,
               confirm="Clear the taint flag? The web content stays in the conversation."),
        Action("audit", "Verify audit log", _safety_audit),
    ]),
    Manager("sources", "Web sources", "No web pages read in this session yet.", _sources_load, [
        Action("copy", "Copy link", _sources_copy, needs_row=True),
        Action("open", "Open in browser", _sources_open, needs_row=True),
    ]),
)}

ALIASES = {"memory": "memories", "notes": "memories", "note": "memories", "skill": "skills", "reminder": "reminders",
           "web": "webcache", "cache": "webcache", "know": "knowledge",
           "session": "sessions", "ctx": "context", "tokens": "context", "todo": "context",
           "checkpoint": "checkpoints", "undo": "checkpoints", "rewind": "checkpoints", "history": "checkpoints",
           "safe": "safety", "security": "safety", "permissions": "safety", "audit": "safety",
           "source": "sources", "links": "sources"}


def find_manager(name: str) -> Manager | None:
    """A manager by its key or a short alias ('memory', 'web')."""
    name = name.strip().lower()
    return MANAGERS.get(ALIASES.get(name, name))
