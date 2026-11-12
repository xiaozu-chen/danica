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
from enum import Enum
from typing import Callable

from danica.tools.registry import TOOLS
from danica.web import host_of


class Tier(Enum):
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

# Text that makes a command too tricky to remember (hidden sub-commands)
COMPLEX_MARKERS = ("$(", "`")
# Shell commands that pull text from the network (used for taint tracking)
NETWORK_RX = re.compile(r"\b(invoke-webrequest|invoke-restmethod|iwr|irm|curl|wget|start-bitstransfer)\b",
                        re.IGNORECASE)


def is_network_command(command: str) -> bool:
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


def classify(tool: str, args: dict) -> tuple[Tier, str, frozenset[str]]:
    """Return (tier, reason, session_keys). session_keys is what 'allow for session' remembers."""
    if tool not in TOOLS:
        # Hallucinated tool name: run_tool will just report "unknown tool", so it's harmless.
        return Tier.READ, "unknown tool", frozenset()

    if tool.startswith(("plugin_", "mcp_")):
        return Tier.OUTSIDE, "executes a user-managed extension", frozenset()

    tier = TOOL_TIERS.get(tool, Tier.EXECUTE)  # unclassified tools fail safe
    if tool != "run_shell":
        return tier, "", frozenset() if tier != Tier.EXECUTE else frozenset({f"tool:{tool}"})

    command = str(args.get("command", ""))
    for pattern, reason in DESTRUCTIVE_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return Tier.DESTRUCTIVE, reason, frozenset()
    for pattern, reason in OUTSIDE_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return Tier.OUTSIDE, reason, frozenset()
    return Tier.EXECUTE, "runs a program", _programs(command)


# ask(tool, summary, tier, reason, can_remember) -> "once" | "session" | "deny"
AskCallback = Callable[[str, str, Tier, str, bool], str]

DENIED_MESSAGE = ("ERROR: the user denied this action. Do not retry it. "
                  "Explain what you wanted to do, or choose a different approach.")


class PermissionPolicy:
    def __init__(self, ask: AskCallback | None = None):
        self.ask = ask                       # None = nobody to ask = deny
        self.full_access = False             # the /fullaccess toggle
        self.session_allowed: set[str] = set()
        self.last_how = ""                  # how the latest check was decided (for the audit log)
        self.project_mode = False            # True inside a trusted project folder (set by the CLI)

    def _taint_applies(self, tool: str, args: dict, known_hosts) -> bool:
        """While untrusted content is in context: which calls need a fresh yes?"""
        if tool == "run_shell":
            return True
        if tool in ("write_file", "edit_file"):
            return self.project_mode      # a page must not choose what lands in a REAL folder
        if tool == "web_fetch":
            host = host_of(str(args.get("url", "")))
            return bool(host) and host not in known_hosts   # a new site might be a page's idea
        return False

    def check(self, tool: str, args: dict, tainted_by=(), known_hosts=frozenset()) -> tuple[bool, str]:
        """Return (allowed, message_for_model_if_denied). Sets self.last_how.
        tainted_by: what put untrusted web content in the context (empty = clean)."""
        tier, reason, keys = classify(tool, args)

        if self.full_access:
            self.last_how = "full-access"
            return True, ""

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
        elif tier in (Tier.READ, Tier.WRITE):
            self.last_how = "free"
            return True, ""

        # Project mode: everyday dev commands are free. Never reached while tainted
        # (the tier above is TAINTED then) and never for destructive/outside commands.
        if (self.project_mode and tool == "run_shell" and tier == Tier.EXECUTE
                and is_project_safe(str(args.get("command", "")))):
            self.last_how = "project-free"
            return True, ""

        # Already approved for this session? (every program in the chain must be)
        if keys and keys <= self.session_allowed:
            self.last_how = "remembered"
            return True, ""

        if self.ask is None:
            self.last_how = "no-asker"
            return False, "ERROR: this action needs the user's permission and none is available."

        # Only plain EXECUTE can be remembered; destructive/outside/tainted always ask.
        can_remember = tier == Tier.EXECUTE and bool(keys)
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