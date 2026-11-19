"""
toolgroups.py - send the model only the tools it is likely to need.

Every request carries the definition of every tool (about 9,000 tokens for 40 tools). A local model has to read all of
that again whenever its cache misses, and it takes a quarter of a 32k window before you have typed a word. So rarely
used tools live in GROUPS and are left out until they are needed:

    * you mention them (the request says "slides", "email", "pygame"...), or
    * the model asks for them with the always-present enable_tools tool, or it calls one anyway, or
    * the feature is switched on (/computer on), or the resumed conversation already used them.

A group, once on, STAYS on for the session. The tool list sits at the very start of the prompt, so changing it makes the
server re-read the whole conversation; growing it only ever happens once per group.
Core tools (files, shell, search, web, todo, memory, ask_user, skills, delegate, view_image) are always sent.
[tools] lazy = false in config.toml sends everything every time (the old behaviour).
"""

import re

from danica.config import CONFIG

GROUPS: dict[str, dict] = {
    "screen": {"tools": ["screen_windows", "screen_shot", "screen_click", "screen_drag", "screen_type", "screen_key",
                         "screen_scroll", "screen_focus"],
               "about": "look at and operate a window the user approved (computer use)",
               "words": r"\b(click|computer use|screen control|operate (the )?(app|window)|notepad|paint|desktop app|gui)\b"},
    "mail": {"tools": ["mail_folders", "mail_list", "mail_read", "mail_scan"],
             "about": "read and scan the user's email (read-only)",
             "words": r"\b(e-?mails?|inbox|mailbox|phishing|unread|gmail|outlook)\b"},
    "game": {"tools": ["game_run"],
             "about": "run a pygame game headless and look at it",
             "words": r"\b(pygame|game|sprite|platformer)\b"},
    "docs": {"tools": ["deck", "document", "quiz", "flashcards"],
             "about": "make slide decks, documents, quizzes, flashcards",
             "words": r"\b(slides?|deck|presentation|powerpoint|pptx|docx|document|report|quiz|flashcards?|word file)\b"},
    "viz": {"tools": ["chart", "dashboard", "flowchart", "diagram", "mermaid"],
            "about": "charts, dashboards, diagrams, flowcharts",
            "words": r"\b(chart|graph|plot|dashboard|diagram|flow ?chart|mermaid|visuali[sz]e|bar|pie)\b"},
    "web_ui": {"tools": ["screenshot", "dev_server"],
               "about": "start a dev server and take screenshots of a web page or app",
               "words": r"\b(screenshot|dev server|vite|npm|localhost|react|frontend|website|web ?page|html|css|ui)\b"},
    "notes": {"tools": ["knowledge_search", "knowledge_synthesize", "obsidian_write_preview"],
              "about": "search and write the user's notes (Obsidian / knowledge base)",
              "words": r"\b(obsidian|vault|knowledge|my notes|zettel)\b"},
}
OWNER = {name: group for group, spec in GROUPS.items() for name in spec["tools"]}       # tool name -> group
WORDS = {group: re.compile(spec["words"], re.I) for group, spec in GROUPS.items()}


def enabled() -> bool:
    return bool((CONFIG.get("tools") or {}).get("lazy", True))


def active(ws) -> set:
    """The groups switched on for this session (stored on the workspace, so a new session starts with none)."""
    groups = getattr(ws, "tool_groups", None)
    if groups is None:
        groups = ws.tool_groups = set()
    comp = getattr(ws, "computer", None)
    if comp is not None and getattr(comp, "enabled", False):          # /computer on: the screen tools are needed
        groups.add("screen")
    return groups


def activate(ws, groups) -> list[str]:
    """Switch groups on. Returns the names that were new."""
    on = active(ws)
    new = [g for g in groups if g in GROUPS and g not in on]
    on.update(new)
    return new


def from_text(ws, text: str) -> list[str]:
    """Switch on the groups a request talks about. Returns the new ones."""
    return activate(ws, [g for g, rx in WORDS.items() if rx.search(text or "")])


def from_tool(ws, name: str) -> None:
    """The model used a tool of a group that was off (it knew it from a skill): switch the group on."""
    group = OWNER.get(name)
    if group:
        activate(ws, [group])


def from_history(ws, messages) -> None:
    """A resumed conversation: groups its tool calls used stay on, so the start of the prompt matches what was cached."""
    for m in messages:
        for call in m.get("tool_calls") or []:
            from_tool(ws, (call.get("function") or {}).get("name", ""))


def select(schemas: list[dict], ws) -> list[dict]:
    """The schemas to send: everything outside a group, plus the groups that are on. Same order every time."""
    if not enabled():
        return schemas
    on = active(ws)
    return [s for s in schemas if OWNER.get(s["function"]["name"]) in (None, *on)]


def hidden_names(schemas: list[dict], ws) -> list[str]:
    on = active(ws)
    return [s["function"]["name"] for s in schemas if OWNER.get(s["function"]["name"]) not in (None, *on)]


def describe() -> str:
    return "; ".join(f"{g} ({spec['about']})" for g, spec in GROUPS.items())
