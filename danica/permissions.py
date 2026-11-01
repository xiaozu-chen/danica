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


class Tier(Enum):
    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    DESTRUCTIVE = "destructive"
    OUTSIDE = "outside-workspace"


# Tier for each known tool. Anything registered but missing here falls back to EXECUTE.
TOOL_TIERS = {
    "read_file": Tier.READ,
    "list_dir": Tier.READ,
    "glob": Tier.READ,
    "grep": Tier.READ,
    "write_file": Tier.WRITE,
    "edit_file": Tier.WRITE,
    "run_shell": Tier.EXECUTE,
}

# (regex, reason) pairs for commands that can destroy things or pull in code.
# False positives are fine: they just cause a prompt. Missed matches are the danger.
DESTRUCTIVE_PATTERNS = [
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
]

# Text that makes a command too tricky to remember (hidden sub-commands)
COMPLEX_MARKERS = ("$(", "`")


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

    def check(self, tool: str, args: dict) -> tuple[bool, str]:
        """Return (allowed, message_for_model_if_denied)."""
        tier, reason, keys = classify(tool, args)

        if self.full_access or tier in (Tier.READ, Tier.WRITE):
            return True, ""

        # Already approved for this session? (every program in the chain must be)
        if keys and keys <= self.session_allowed:
            return True, ""

        if self.ask is None:
            return False, "ERROR: this action needs the user's permission and none is available."

        # Only plain EXECUTE can be remembered; destructive/outside always ask.
        can_remember = tier == Tier.EXECUTE and bool(keys)
        summary = str(args.get("command")) if tool == "run_shell" else f"{tool} {args}"
        decision = self.ask(tool, summary[:300], tier, reason, can_remember)

        if decision == "session" and can_remember:
            self.session_allowed |= keys
            return True, ""
        if decision in ("once", "session"):
            return True, ""
        return False, DENIED_MESSAGE