"""
permissions.py - Permission tiers and the ask/allow/deny decision.

classify() looks at ONE tool call and says which tier it belongs to.
PermissionPolicy.check() turns that into allow/deny, asking the user when needed.

HONEST LIMIT: the shell checks below are pattern matching. They catch honest
mistakes and lazy attacks, NOT a determined one (obfuscated commands, a script
that deletes things when run). Real containment comes from the sandbox levels
(Job Objects, optional Docker/WSL) later in Phase 2.
"""

import re
from collections.abc import Sequence
from enum import Enum
from pathlib import Path
from typing import AbstractSet, Any, Callable

from danica.openmode import outputs_dir, shell_protected_hit
from danica.tools.registry import TOOLS
from danica.web import host_of


class Tier(Enum):
    """Define the permission levels used for tool actions."""

    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    DESTRUCTIVE = "destructive"
    OUTSIDE = "outside-workspace"
    TAINTED = "untrusted-context"


# Tier for each known tool. Anything registered but missing here falls back to EXECUTE.
TOOL_TIERS = {
    "read_file": Tier.READ,
    "list_dir": Tier.READ,
    "glob": Tier.READ,
    "grep": Tier.READ,
    "write_file": Tier.WRITE,
    "edit_file": Tier.WRITE,
    "run_shell": Tier.EXECUTE,
    "todo": Tier.WRITE,
    "web_fetch": Tier.READ,
    "web_search": Tier.READ,
    "recall": Tier.READ,
    "remember": Tier.READ,   # writes only global memory, and only when [memory] auto_save is on (tools/remember.py)
    "load_skill": Tier.READ,
    "chart": Tier.WRITE,
    "document": Tier.WRITE,
    "diagram": Tier.WRITE,
    "deck": Tier.WRITE,
    "mermaid": Tier.WRITE,
    "dashboard": Tier.WRITE,
    "delegate": Tier.EXECUTE,
    "knowledge_search": Tier.READ,
    "knowledge_synthesize": Tier.READ,
    "web_cache_search": Tier.READ,
    "quiz": Tier.WRITE,            # these three make a file next to their JSON data, like chart
    "flashcards": Tier.WRITE,
    "flowchart": Tier.WRITE,
    "ask_user": Tier.READ,
    "dev_server": Tier.EXECUTE,    # starts a background process: asks like run_shell
    "screenshot": Tier.WRITE,      # saves a picture (out), opens only local pages
    "mail_folders": Tier.READ,     # email is opened read-only (mail.py); the context becomes TAINTED when mail is read
    "mail_list": Tier.READ,
    "mail_read": Tier.READ,
    "mail_scan": Tier.READ,
    "tool_book": Tier.READ,        # only lists tools and switches which tool definitions are sent (toolbook.py)
    "screen_windows": Tier.READ,   # computer use (computer.py): only windows the USER approved with /computer allow can be seen or driven
    "screen_shot": Tier.READ,
    "screen_click": Tier.EXECUTE,  # mouse and keyboard: ask once per session (one shared answer for all screen_* actions)
    "screen_drag": Tier.EXECUTE,
    "screen_type": Tier.EXECUTE,
    "screen_key": Tier.EXECUTE,
    "screen_scroll": Tier.EXECUTE,
    "screen_focus": Tier.EXECUTE,
    "game_run": Tier.EXECUTE,      # runs the game's Python code (in the sandbox, headless): asks like run_shell
    "view_image": Tier.READ,       # only looks at a workspace file (same jail as read_file)         # only shows the user a dialog
}

# (regex, reason) pairs for commands that can destroy things or pull in code.
# False positives are fine: they just cause a prompt. Missed matches are the danger.
DESTRUCTIVE_PATTERNS = [
    (r"\b(invoke-webrequest|invoke-restmethod|iwr|irm|curl|wget)\b[^|;&\n]*"
     r"(localhost|127\.\d|0\.0\.0\.0|\[::1\]|\b10\.\d|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|169\.254\.)",
     "network request to a LOCAL or PRIVATE address (web_fetch blocks these on purpose)"),
    (r"\b(remove-item|rm|rmdir|rd|del|erase|ri)\b", "deletes files"),
    (r"\b(move-item|move|mv)\b", "moves or overwrites files"),
    (r"format-volume|\bformat\s+[a-z]:|clear-disk|\bdiskpart\b", "formats or wipes a disk"),
    (r"\b(stop-process|taskkill|kill)\b", "kills processes"),
    (r"\b(shutdown|stop-computer|restart-computer)\b", "shuts down the machine"),
    (r"\bgit\s+(reset\s+--hard|clean|push|checkout\s+--)", "rewrites or publishes git state"),
    (r"\breg(\.exe)?\s+(add|delete|import)\b|\bset-itemproperty\b", "edits the registry"),
    (r"\bset-executionpolicy\b", "changes the PowerShell security policy"),
    (r"\b(invoke-webrequest|invoke-restmethod|iwr|irm|curl|wget|start-bitstransfer)\b",
     "downloads or sends data over the network"),
    (r"\b(invoke-expression|iex)\b", "runs dynamically built code"),
]

# Patterns that suggest the command reaches outside the workspace folder.
OUTSIDE_PATTERNS = [
    (r"(?<![a-zA-Z])[a-zA-Z]:[\\/]", "mentions an absolute drive path"),  # lookbehind: not "http://"
    (r"\.\.[\\/]|(^|\s)\.\.(\s|$)", "mentions the parent directory (..)"),
    (r"(^|\s)~([\\/]|\s|$)", "mentions the home directory"),
    (r"\$env:|%\w+%", "reads environment variables / system paths"),
    (r"(^|\s)\\\\[\w.]", "mentions a network (UNC) path"),
    (r"""(^|[\s\\/'"])\.env(\.\w+)?\b|\.pem\b|\bid_(rsa|ed25519|dsa|ecdsa)\b|\.ssh\b|\.aws\b|\.npmrc|\.pypirc|\.netrc|\.git-credentials""",
     "mentions a credential file"),
]

# In Open Mode a path outside the workspace is ordinary, so these four stop counting as "outside".
# (The credential-file pattern stays, and shell_protected_hit() adds the protected-area check.)
OPEN_MODE_ORDINARY = {
    "mentions an absolute drive path", "mentions the parent directory (..)",
    "mentions the home directory", "reads environment variables / system paths",
}

# Text that makes a command too tricky to remember (hidden sub-commands)
COMPLEX_MARKERS = ("$(", "`")
# Shell commands that pull text from the network (used for taint tracking)
NETWORK_RX = re.compile(r"\b(invoke-webrequest|invoke-restmethod|iwr|irm|curl|wget|start-bitstransfer)\b",
                        re.IGNORECASE)


def is_network_command(command: str) -> bool:
    """Return whether a command appears to perform network access."""
    return bool(NETWORK_RX.search(command))

# ---------------------------------------------------------------------------
# Project mode: commands that run WITHOUT asking inside a trusted project.
# Everything else still goes through the normal tiers. Matching is by program
# name and first arguments, so it catches honest use, not obfuscation (see the
# HONEST LIMIT at the top of this file).
# ---------------------------------------------------------------------------
READ_ONLY_COMMANDS = {
    "get-content", "gc", "cat", "type", "get-childitem", "gci", "ls", "dir",
    "select-string", "sls", "get-location", "pwd", "echo", "write-output",
    "write-host", "test-path", "get-item", "measure-object", "cd", "set-location", "sl",
}
DEV_TOOLS = {"pytest", "ruff", "mypy", "flake8", "black", "tsc", "eslint", "prettier"}
PYTHON_MODULES = {"pytest", "unittest", "doctest", "ruff", "mypy", "flake8", "black", "compileall"}
GIT_READ_ONLY = {"status", "diff", "log", "show", "ls-files", "rev-parse"}
# Redirects, script blocks and sub-commands can hide what really runs
PROJECT_UNSAFE = ("{", "}", ">", "<", "$(", "`")


def _program(word: str) -> str:
    name = word.strip("'\"").lower()
    return name[:-4] if name.endswith(".exe") else name


def _segment_ok(words: list[str]) -> bool:
    """Is ONE command (a program and its arguments) free inside a project?"""
    prog = _program(words[0])
    if "/" in prog or "\\" in prog:
        return False                      # only bare names: a model-written file can't pose as one
    rest = [w.strip("'\"") for w in words[1:]]

    if prog in READ_ONLY_COMMANDS or prog in DEV_TOOLS:
        return True
    if prog in ("python", "python3", "py"):
        if not rest:
            return False                  # a bare interpreter is a REPL
        if rest[0] in ("--version", "-V"):
            return True
        if rest[0] == "-m":
            return len(rest) > 1 and rest[1].lower() in PYTHON_MODULES
        return rest[0].lower().endswith(".py")        # NOT -c: inline code asks
    if prog == "node":
        return bool(rest) and rest[0].lower().endswith((".js", ".mjs", ".cjs"))   # NOT -e
    if prog in ("npm", "pnpm", "yarn"):
        return bool(rest) and rest[0].lower() in ("test", "t", "run", "run-script")   # NOT install
    if prog == "git":
        return bool(rest) and rest[0].lower() in GIT_READ_ONLY
    return False


def is_project_safe(command: str) -> bool:
    """True if EVERY part of the command is on the free list."""
    if not command.strip() or any(m in command for m in PROJECT_UNSAFE):
        return False
    for segment in re.split(r"[;|&\n]+", command):     # a; b | c && d
        words = segment.strip().split()
        if words and not _segment_ok(words):
            return False
    return True

def _norm_dir(path: Path) -> str:
    """One spelling of a folder for the 'allow for session' key (Windows is case-insensitive)."""
    import os
    return os.path.normcase(str(path))


def _programs(command: str) -> frozenset[str]:
    """First word of every segment in a chained command: 'a; b | c' -> {a, b, c}."""
    if any(m in command for m in COMPLEX_MARKERS):
        return frozenset()  # empty = "can't be remembered, ask every time"
    names = set()
    for segment in re.split(r"[;|&\n]+", command):
        words = segment.strip().split(None, 1)
        if not words:
            continue
        name = words[0].strip("'\"").lower()
        names.add(name[:-4] if name.endswith(".exe") else name)
    return frozenset(names)


def is_read_only(command: str) -> bool:
    """True if EVERY part of the command only looks at things (cat, ls, git status-style reads).
    Used by Open Mode, where these run without asking."""
    if not command.strip() or any(m in command for m in PROJECT_UNSAFE):
        return False
    for segment in re.split(r"[;|&\n]+", command):
        words = segment.strip().split()
        if words and (_program(words[0]) not in READ_ONLY_COMMANDS or "/" in _program(words[0])
                      or "\\" in _program(words[0])):
            return False
    return True


# Tools that WRITE a file, and the argument that names it (Open Mode asks when it is outside the start folder)
WRITE_PATH_ARGS = {"write_file": "path", "edit_file": "path", "chart": "file", "dashboard": "file",
                   "diagram": "file", "mermaid": "file", "document": "source", "deck": "source",
                   "quiz": "file", "flashcards": "file", "flowchart": "file", "screenshot": "out", "game_run": "out"}


def classify(
    tool: str, args: dict, open_root: Path | None = None
) -> tuple[Tier, str, frozenset[str]]:
    """Return (tier, reason, session_keys). session_keys is what 'allow for session' remembers.
    open_root: the Open Mode start folder (None = not in Open Mode)."""
    if tool not in TOOLS:
        # Hallucinated tool name: run_tool will just report "unknown tool", so it's harmless.
        return Tier.READ, "unknown tool", frozenset()

    if tool.startswith(("plugin_", "mcp_")):
        return Tier.OUTSIDE, "executes a user-managed extension", frozenset()

    tier = TOOL_TIERS.get(tool, Tier.EXECUTE)  # unclassified tools fail safe
    if tool.startswith("screen_") and tier == Tier.EXECUTE:
        return tier, "controls the mouse and keyboard of a window you approved", frozenset({"tool:screen_input"})
    if tool != "run_shell":
        return tier, "", frozenset() if tier != Tier.EXECUTE else frozenset({f"tool:{tool}"})

    command = str(args.get("command", ""))
    for pattern, reason in DESTRUCTIVE_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return Tier.DESTRUCTIVE, reason, frozenset()
    if open_root is not None:
        hit = shell_protected_hit(command, open_root)
        if hit:
            return Tier.OUTSIDE, f"mentions a protected area: {hit[0]} ({hit[1]})", frozenset()
    for pattern, reason in OUTSIDE_PATTERNS:
        if open_root is not None and reason in OPEN_MODE_ORDINARY:
            continue
        if re.search(pattern, command, re.IGNORECASE):
            return Tier.OUTSIDE, reason, frozenset()
    return Tier.EXECUTE, "runs a program", _programs(command)


# ask(tool, summary, tier, reason, can_remember) -> "once" | "session" | "deny"
AskCallback = Callable[[str, str, Tier, str, bool], str]

DENIED_MESSAGE = ("ERROR: the user denied this action. Do not retry it. "
                  "Explain what you wanted to do, or choose a different approach.")


class PermissionPolicy:
    """Apply session and workspace rules to tool permission requests."""

    def __init__(self, ask: AskCallback | None = None):
        self.ask = ask                       # None = nobody to ask = deny
        self.full_access = False             # the /fullaccess toggle
        self.session_allowed: set[str] = set()
        self.last_how = ""                  # how the latest check was decided (for the audit log)
        self.project_mode = False            # True inside a trusted project folder (set by the CLI)
        self.open_root: Path | None = None   # Open Mode: the START folder (None = not in Open Mode)
        self.judge = None                    # shell judge: judge(command, mode) -> Verdict | None (see judge.py)

    def _taint_applies(self, tool: str, args: dict, known_hosts) -> bool:
        """While untrusted content is in context: which calls need a fresh yes?"""
        if tool == "run_shell":
            return True
        if tool in ("write_file", "edit_file"):
            return self.project_mode or self.open_root is not None   # a page must not choose what lands in a REAL folder
        if tool == "web_fetch":
            host = host_of(str(args.get("url", "")))
            return bool(host) and host not in known_hosts   # a new site might be a page's idea
        return False

    def _open_write_outside(self, tool: str, args: dict) -> Path | None:
        """Open Mode: the file a write tool is about to write, if it is OUTSIDE the start folder."""
        arg = WRITE_PATH_ARGS.get(tool)
        if self.open_root is None or arg is None:
            return None
        try:
            target = (self.open_root / str(args.get(arg) or "")).resolve()
        except (OSError, ValueError, RuntimeError):
            return None
        if (
            target.is_relative_to(self.open_root)
            or target.is_relative_to(outputs_dir())
        ):
            return None        # inside the start folder, or in the anchor's outputs folder: no ask
        return target

    def check(
        self,
        tool: str,
        args: dict[str, Any],
        tainted_by: Sequence[str] = (),
        known_hosts: AbstractSet[str] = frozenset(),
    ) -> tuple[bool, str]:
        """Return (allowed, message_for_model_if_denied). Sets self.last_how.
        tainted_by: what put untrusted web content in the context (empty = clean)."""
        tier, reason, keys = classify(tool, args, self.open_root)

        if self.full_access:
            self.last_how = "full-access"
            return True, ""

        # Open Mode: reading anywhere is free, but WRITING outside the start folder asks first
        # ("allow for session" remembers that one folder, nothing wider).
        outside = self._open_write_outside(tool, args)
        if outside is not None:
            tier = Tier.OUTSIDE
            reason = f"writes outside the start folder: {outside}"
            keys = frozenset({"openwrite:" + _norm_dir(outside.parent)})

        if tainted_by and self._taint_applies(tool, args, known_hosts):
            note = f"untrusted web content is in this conversation (from: {tainted_by[-1][:80]})"
            if tier in (Tier.DESTRUCTIVE, Tier.OUTSIDE):
                reason = f"{reason}; also: {note}"       # already asks every time
            else:
                if tool == "run_shell":
                    what = "a page could be steering this command"
                elif tool in ("write_file", "edit_file"):
                    what = "a page could be choosing what gets written into your project"
                else:
                    what = "this site has not appeared in the conversation yet"
                tier, reason = Tier.TAINTED, f"{what}; {note}"
            keys = frozenset()      # earlier "allow for session" approvals do not count now
        elif tier in (Tier.READ, Tier.WRITE) and outside is None:
            self.last_how = "free"
            return True, ""

        # Project mode: everyday dev commands are free. Never reached while tainted
        # (the tier above is TAINTED then) and never for destructive/outside commands.
        if (self.project_mode and tool == "run_shell" and tier == Tier.EXECUTE
                and is_project_safe(str(args.get("command", "")))):
            self.last_how = "project-free"
            return True, ""

        # Open Mode: look-only shell commands run free; everything else asks (below)
        if (self.open_root is not None and tool == "run_shell" and tier == Tier.EXECUTE
                and is_read_only(str(args.get("command", "")))):
            self.last_how = "open-readonly"
            return True, ""

        # Already approved for this session? (every program in the chain must be)
        if keys and keys <= self.session_allowed:
            self.last_how = "remembered"
            return True, ""

        # The model judge (judge.py) only sees plain EXECUTE shell commands that no rule above settled. It can
        # ESCALATE anywhere and explains the command in the prompt; it can wave a command through ONLY in a
        # trusted project. The rules above are never relaxed, and no answer from it means "ask" as before.
        if tool == "run_shell" and tier == Tier.EXECUTE and self.judge is not None:
            mode = "project" if self.project_mode else ("open" if self.open_root is not None else "normal")
            try:
                verdict = self.judge(str(args.get("command", "")), mode)
            except Exception:
                verdict = None                          # a broken judge must never mean "allow"
            if verdict is not None:
                from danica import classifier
                if (verdict.verdict == "safe" and mode == "project" and self.ask is not None
                        and classifier.settings()["judge_can_approve"]):
                    self.last_how = "model-free"
                    return True, ""
                reason = f"{reason}; model: {verdict.reason}" if reason else f"model: {verdict.reason}"
                if verdict.verdict == "dangerous":
                    tier, keys = Tier.DESTRUCTIVE, frozenset()      # asks every time, cannot be remembered

        if self.ask is None:
            self.last_how = "no-asker"
            return False, "ERROR: this action needs the user's permission and none is available."

        # Only plain EXECUTE can be remembered; destructive/outside/tainted always ask.
        can_remember = bool(keys) and (tier == Tier.EXECUTE or (
            tier == Tier.OUTSIDE and all(k.startswith("openwrite:") for k in keys)))
        summary = str(args.get("command")) if tool == "run_shell" else f"{tool} {args}"
        decision = self.ask(tool, summary[:300], tier, reason, can_remember)

        if decision == "session" and can_remember:
            self.session_allowed |= keys
            self.last_how = "asked-session"
            return True, ""
        if decision in ("once", "session"):
            self.last_how = "asked-once"
            return True, ""
        self.last_how = "denied-by-user"
        return False, DENIED_MESSAGE
