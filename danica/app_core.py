"""
app_core.py - Session setup that has no terminal code in it.

Ported from cli.py: build_session, open_resumed, apply_instructions, ask_trust and the
shared knowledge-index / extension-manager singletons. Every place that used to call
console.print() or prompt() now goes through a `ui` object (see danica/ui.py), so the
Textual TUI and the classic CLI can share this file.

cli.py is NOT changed yet; it keeps its own copies until Phase 6 is finished, so the
old frontend stays a working baseline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from danica.agent import Agent
from danica.audit import AuditLog
from danica.capture import ProposalQueue
from danica.checkpoints import Checkpoints
from danica.config import APP_DIR, CONFIG
from danica.embeddings import get_embedder
from danica.extensions import ExtensionError, ExtensionManager
from danica.knowledge import KnowledgeError, KnowledgeIndex
from danica.memory import MemoryStore
from danica.memory_index import FACTS_DB, MemoryIndex
from danica.openmode import anchor_dir, ensure_anchor, open_mode_note, start_folder_problem
from danica.judge import judge_command
from danica.permissions import PermissionPolicy
from danica.projects import (approve_instructions, load_session_meta, lookup,
                             read_instructions, save_session_meta, scan)
from danica.sessions import SESSIONS_DIR, load_records
from danica.subagents import SubagentError, SubagentSupervisor
from danica.ui import UI
from danica.workspace import Workspace

_KNOWLEDGE_INDEX: KnowledgeIndex | None = None
_EXTENSION_MANAGER: ExtensionManager | None = None

class ResumeCancelled(ValueError):
    """"""

def short_path(root) -> str:
    """The workspace as a short label. (cli.py calls relative_to(APP_DIR/"sessions") directly,
    which raises ValueError in project mode because the root is then your own folder.)"""
    root = Path(root)
    try:
        return str(root.relative_to(APP_DIR / "sessions"))
    except ValueError:
        return root.name or str(root)


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


def make_ask(ui: UI):
    """The permission callback for PermissionPolicy. It runs on whatever thread the agent
    runs on; ui.permission() blocks that thread until the user answers."""

    def ask(tool: str, summary: str, tier, reason: str, can_remember: bool) -> str:
        try:
            answer = ui.permission(tool, summary, tier.value, reason, can_remember)
        except Exception:
            return "deny"                     # a broken dialog must never mean "allow"
        return answer if answer in ("once", "session") else "deny"
    return ask


def apply_instructions(project: dict, agent, audit, ui: UI) -> None:
    """Load the project's DANICA.md. New or changed text must be approved first."""
    found = read_instructions(project["path"])
    if found is None:
        return
    text, sha, cut = found
    approved_now = False
    if project.get("instructions_sha") != sha:
        label = "changed" if project.get("instructions_sha") else "new"
        detail = "Longer than the limit: only the text above would be loaded." if cut else ""
        ok = ui.confirm(
            f"This project has a {label} DANICA.md",
            body=text,
            detail=("Danica would follow it as instructions (it cannot override safety rules or "
                    "permission prompts). " + detail).strip(),
            yes="Load it", no="Skip")
        if not ok:
            ui.log("warning", "  DANICA.md not loaded.")
            return
        approve_instructions(project, sha)
        approved_now = True
    agent.set_project_instructions(text)
    audit.append("instructions", sha=sha[:12], approved_now=approved_now)
    ui.log("ok", f"  Loaded DANICA.md ({len(text)} characters.)")


def make_asker(ui):
    """How the ask_user tool reaches the user: a dialog on the TUI. None when this UI has no dialogs
    (the tool then tells the model to carry on with sensible defaults). Blocks the worker thread
    until the user answers; returns [{"question", "answers", "other"}, ...] or None if skipped."""
    show = getattr(ui, "ask_screen", None)
    if show is None:
        return None

    def ask(questions: list[dict]):
        from danica.tui.ask_screen import AskScreen
        return show(AskScreen(questions))
    return ask


def build_session(session_id: str, ui: UI, on_event, project: dict | None = None,
                  headless: bool = False, workspace_root: Path | None = None,
                  open_root: Path | None = None):
    """Create everything one session needs.
    project=None  -> normal chat: a throwaway workspace inside the session folder
    project=info  -> project mode: the workspace IS the user's trusted folder
    open_root=dir -> OPEN MODE: `dir` is only the START folder; the whole disk is reachable except
                     the protected areas (openmode.py). The caller has ALREADY shown the warning.
    Returns (ws, audit, checkpoints, policy, agent), same as cli.build_session."""
    session_dir = APP_DIR / "sessions" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    if open_root is not None:
        problem = start_folder_problem(Path(open_root))
        if problem:
            raise ValueError(f"Open Mode can't start there: {problem}")
    root = (Path(open_root) if open_root is not None else Path(project["path"]) if project else (
        Path(workspace_root) if headless and workspace_root else session_dir / "workspace"))
    ws = Workspace(root, todo_path=session_dir / "TODO.md",
                   sources_path=session_dir / "sources.json", open_mode=open_root is not None)
    ws.knowledge_index = knowledge_index()
    save_session_meta(session_dir, project["path"] if project else None,
                      str(ws.root) if open_root is not None else None)

    audit = AuditLog(APP_DIR / "logs" / "audit.jsonl", session=session_id)

    git_dir = Path(project["dir"]) / "git" if project else session_dir / "checkpoints" / "git"
    # Open Mode: /undo can only cover the START folder, and only if it is a sensible size of folder
    # (the same rules as a project). Otherwise snapshots are switched off and we say so.
    snapshots_off = open_root is not None and _too_broad_for_snapshots(ws.root)
    checkpoints = Checkpoints(ws, git_dir, disabled=snapshots_off)
    if snapshots_off:
        ui.log("warning", "  /undo and /rewind are OFF: the start folder is too broad to snapshot. "
                          "Start Open Mode from a project folder to get them back.")
    elif not checkpoints.available:
        ui.log("warning", "  `git` was not found, so /undo and /rewind are disabled. "
                          "(Install it with: winget install Git.Git)")

    policy = PermissionPolicy(ask=None if headless else make_ask(ui))
    policy.project_mode = project is not None
    policy.open_root = ws.root if open_root is not None else None      # switches Open Mode rules on
    policy.judge = None if headless else judge_command                 # the main model explains / judges shell commands (judge.py)
    supervisor = SubagentSupervisor(ws, policy, audit, session_id)
    ws.subagent_supervisor = supervisor
    ws.ask_user = None if headless else make_asker(ui)       # the ask_user tool's way to reach the user
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
    store = MemoryStore(APP_DIR / "memory",
                        Path(project["dir"]) / "memory" if project else None,
                        session_dir / "memory")
    store.index = MemoryIndex(FACTS_DB, embedder=get_embedder(),
                              min_similarity=CONFIG["embeddings"]["min_similarity"])
    ws.memory = store
    agent.memory = store
    agent.proposals = ProposalQueue(session_dir / "proposals.json")
    agent.plans_dir = session_dir / "plans"
    if project:
        apply_instructions(project, agent, audit, ui)
    if open_root is not None:
        try:
            ensure_anchor()          # ~/.danica/outputs, where generated files go
        except OSError:
            pass                     # not fatal: writes there just ask like any other outside path
        agent.set_mode_note(open_mode_note(ws.root, snapshots_off))

    report = agent.context_report()
    if report.status == "too_small":
        ui.log("warning", f"  The context window ({report.window:,} tokens) is too small for Danica: "
                          f"the system prompt and tools alone need about {report.fixed:,}. "
                          f"Restart llama-server with -c {report.recommended_window:,} or more.")
    return ws, audit, checkpoints, policy, agent


def open_resumed(target: str, ui: UI, on_event):
    """Build a session around an existing folder and load its history.
    Returns (the 5 session objects, a text report for the user).
    Raises ValueError (message shown to the user) if it can't or shouldn't be resumed.
    Blocking: it may ask the user and call the model, so call it from a worker thread."""
    records, bad = load_records(SESSIONS_DIR / target / "transcript.jsonl")
    project = None
    meta = load_session_meta(SESSIONS_DIR / target)
    if meta.get("project"):
        project = lookup(Path(meta["project"]))
        if project is None or not Path(project["path"]).is_dir():
            raise ValueError(f"that session worked in {meta['project']}, which is no longer "
                             f"trusted or no longer exists. Use /project <path> to trust it again.")
    open_root = None
    if meta.get("mode") == "open" and meta.get("open_root"):
        # Open Mode never switches itself back on: the warning is shown again, every time
        open_root = Path(meta["open_root"])
        if not open_root.is_dir():
            raise ValueError(f"that session was in Open Mode, but its start folder {open_root} "
                             f"no longer exists.")
        if not confirm_open_mode(open_root, ui):
            raise ValueError("Open Mode was not confirmed, so the session was not resumed. "
                             "(You can /resume it again and confirm, or work in Chat Mode.)")
    parts = build_session(target, ui, on_event, project, open_root=open_root)
    ws, audit, checkpoints, policy, agent = parts

    try:
        fixed = agent.load_history(records)

        report = agent.context_report()
        if report.status == "too_small":
            raise ValueError(f"The context window ({report.window:,} tokens) is too small: the "
                             f"system prompt and tools alone need about {report.fixed:,}. Restart "
                             f"llama-server with -c {report.recommended_window:,} or more.")
        if report.status == "over":
            if not ui.confirm("This session doesn't fit the context window",
                              f"It is about {report.used:,} tokens, but the window is {report.window:,}. "
                              f"Danica can summarize the older part so it fits. The full transcript "
                              f"stays on disk, but the compaction is saved, so resuming later with a "
                              f"bigger window will also start from the summary.",
                              yes="Compact and resume", no="Cancel"):
                raise ValueError("Resume cancelled.")
            report = agent.fit_to_window()
            if report.status != "ok":
                raise ValueError(f"Could not make this session fit {report.window:,} tokens. "
                                 f"Restart llama-server with a larger -c to resume it.")
            ui.log("info", f"Session compacted to about {report.used:,} tokens.")
    except BaseException:
        try:
            shutdown(ws)            # release what build_session started (git, subagents, extensions)
        except Exception:
            pass
        raise

    audit.append("session_resume", workspace=str(ws.root), records=len(records),
                 unreadable_lines=bad, repaired=fixed)

    where = (f"OPEN MODE (start folder {open_root})" if open_root is not None
             else f"project {project['name']}" if project else "chat")
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


def _too_broad_for_snapshots(path: Path) -> bool:
    """A start folder /undo can't sensibly snapshot: a drive root, your home folder (or above it)."""
    from danica.projects import ProjectError, check_allowed
    try:
        check_allowed(path)
    except ProjectError:
        return True
    return False


def resolve_open_start(requested: str, launch: Path) -> tuple[Path | None, str, str]:
    """Pick the Open Mode start folder. Returns (folder, notice, error); folder is None on error.
    - a folder you NAMED is used as given (or refused with the reason: you asked for it)
    - otherwise the launch folder is used, unless it is protected or too broad to snapshot, in which
      case we fall back to the anchor (~/.danica) and say why."""
    if requested.strip():
        start = Path(requested.strip().strip('"')).expanduser()
        problem = start_folder_problem(start)
        return (None, "", problem) if problem else (start.resolve(), "", "")
    problem = start_folder_problem(launch) or ("too broad to snapshot" if _too_broad_for_snapshots(launch.resolve()) else "")
    if not problem:
        return launch.resolve(), "", ""
    try:
        anchor = ensure_anchor()
    except OSError as exc:
        return None, "", f"the launch folder can't be used ({problem}) and {anchor_dir()} can't be created: {exc}"
    return anchor, f"Launch folder not used ({problem}); starting in the anchor folder {anchor}.", ""


def confirm_open_mode(start: Path, ui: UI) -> bool:
    """The Open Mode warning. Returns True only on an explicit yes. Shown every time."""
    body = (f"Start folder: {start}\n\n"
            "Open Mode lets Danica read your whole disk and write anywhere you approve, instead of "
            "staying in one folder.")
    detail = ("STILL BLOCKED: Windows and Program Files, AppData (browser profiles, saved logins), other "
              "users' profiles, credential folders (.ssh, .aws...), network paths and Danica's own data. "
              "STILL ASKS: writing outside the start folder, and every shell command except look-only ones. "
              "NOT PROTECTED: everything else on this computer, including documents and photos. "
              "/undo only covers the start folder, so changes elsewhere cannot be rolled back. "
              "Anything Danica reads from a web page could try to steer it: keep Open Mode for tasks you "
              "are watching. It ends with /chat, /project or /new and is never switched on automatically.")
    return ui.confirm("\u26a0  Enable OPEN MODE (full disk access)?", body=body, detail=detail,
                      yes="Enable Open Mode", no="Cancel")


def ask_trust(path: Path, ui: UI) -> bool:
    """The trust dialog. Returns True only on an explicit yes."""
    files, size, capped, has_git = scan(path)
    count = f"{files:,}+" if capped else f"{files:,}"
    body = (f"{path}\n\n{count} files, {size / 1_000_000:,.0f} MB (ignoring .git, .venv, node_modules)"
            + ("; it is a git repository" if has_git else ""))
    detail = ("Trusting means Danica can read and change files here. Commands still ask for "
              "permission, and every request is snapshotted so /undo can roll it back. "
              "Snapshots live outside your folder and never touch your own .git.")
    if capped or size > 500_000_000:
        detail += " This folder is big: the first snapshot may take a while."
    return ui.confirm("Trust this folder as a Danica project?", body=body, detail=detail,
                      yes="Trust", no="Cancel")


def shutdown(ws, log: Callable[[str, str], None] | None = None) -> None:
    """Stop subagents, knowledge watchers and extensions (what cli.py does after its loop)."""
    log = log or (lambda level, text: None)
    supervisor = getattr(ws, "subagent_supervisor", None) if ws is not None else None
    if supervisor is not None:
        try:
            supervisor.close()
        except SubagentError as exc:
            log("error", f"stopping subagents: {exc}")
    if _KNOWLEDGE_INDEX is not None:
        try:
            _KNOWLEDGE_INDEX.close()
        except KnowledgeError as exc:
            log("error", f"stopping knowledge watchers: {exc}")
    if _EXTENSION_MANAGER is not None:
        try:
            _EXTENSION_MANAGER.close()
        except ExtensionError as exc:
            log("error", f"stopping extensions: {exc}")