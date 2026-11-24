"""
toolbook.py - the tool book: a short directory of the tools that are not sent with every request.

Danica has about 40 tools. Sending every definition with every request costs ~9,000 tokens, which a local model has to
read again whenever its cache misses. So the everyday tools (files, shell, search, web, todo, memory...) are always sent
in full, and the rest are described ONE LINE EACH in the description of the always-present `tool_book` tool. The model
therefore always sees that they exist and what each is for (and which ones to prefer over run_shell). To use one:

    tool_book(tools=["deck"])      loads it and prints its full usage; it is in the tool list from the next step on
    tool_book(query="slides")      searches the book when the model is not sure which tool fits
    tool_book()                    prints the whole book

A loaded tool STAYS loaded for the rest of the session, and a tool called without loading it is loaded on the spot.
The tool list is the start of the prompt: changing it makes the server re-read the conversation, so it only ever grows,
and only when something new is loaded.

[tools] lazy = false in config.toml sends every tool every time (no book).
"""

import difflib
from collections.abc import Iterable
from typing import Any

from danica.config import CONFIG
from danica.workspace import Workspace

# Each line: the tools that load together, and what they are for. Written for the model: say WHEN to use it and what
# NOT to use instead. Tools that are not listed here (files, shell, search, web, todo, memory...) are always sent.
BOOK: list[tuple[tuple[str, ...], str]] = [
    (("screen_windows", "screen_shot", "screen_click", "screen_drag", "screen_type", "screen_key", "screen_scroll", "screen_focus"),
     "operate a desktop window the user approved with /computer: look at it, click, drag, type, press keys. Not for websites"),
    (("mail_folders", "mail_list", "mail_read", "mail_scan"),
     "read the user's email (read-only) and check it for phishing"),
    (("game_run",),
     "test a pygame game HEADLESS and see it. Use it instead of run_shell for any pygame file (`python game.py` opens a "
     "window and never exits)"),
    (("deck",), "make a PowerPoint (.pptx) deck or HTML slideshow from a Markdown outline"),
    (("document",), "make a Word (.docx) or PDF document from a Markdown file"),
    (("quiz", "flashcards"), "make an interactive quiz page or flash cards from a JSON file"),
    (("chart", "dashboard"), "draw a chart, table or one-page dashboard as a PNG image from a JSON file"),
    (("flowchart", "diagram", "mermaid"), "draw a flowchart or a boxes-and-arrows / Mermaid diagram as a PNG image"),
    (("dev_server", "screenshot"),
     "run a web project's dev server in the background (never run_shell for `npm run dev`) and take a screenshot of a page "
     "or app you built"),
    (("knowledge_search", "knowledge_synthesize", "obsidian_write_preview"),
     "search the user's indexed notes and Obsidian vault, answer from them with citations, or preview a new note"),
]
PAGE = {name: i for i, (names, _) in enumerate(BOOK) for name in names}       # tool name -> its line in the book
SCREEN = BOOK[0][0]                                                           # loaded by /computer on


def enabled() -> bool:
    """Return whether the tool book is in use (otherwise every tool is sent every time)."""
    return bool((CONFIG.get("tools") or {}).get("lazy", True))


def catalog() -> str:
    """The book as text, one line per entry. This goes into the description of the tool_book tool."""
    return "\n".join(f"- {', '.join(names)}: {about}" for names, about in BOOK)


def loaded(ws: Workspace) -> set[str]:
    """The book tools switched on for this session (kept on the workspace, so a new session starts with none)."""
    names = getattr(ws, "loaded_tools", None)
    if names is None:
        names = ws.loaded_tools = set()
    comp = getattr(ws, "computer", None)
    if comp is not None and getattr(comp, "enabled", False):          # /computer on: the screen tools are needed
        names.update(SCREEN)
    return names


def load(ws: Workspace, tools: Iterable[str]) -> list[str]:
    """Load tools (a tool brings the others on its line: mail_list brings mail_read). Returns the names that were new."""
    on = loaded(ws)
    wanted = {n for t in tools if t in PAGE for n in BOOK[PAGE[t]][0]}
    new = [n for names, _ in BOOK for n in names if n in wanted and n not in on]      # book order, so the list is stable
    on.update(new)
    # Tool-book modules are imported only when their group is activated.
    from danica.tools.registry import ensure_tools
    ensure_tools(new)
    return new


def from_tool(ws: Workspace, name: str) -> None:
    """The model called a book tool that was not loaded (it knew it from the book or a skill): load it from now on."""
    load(ws, [name])
    from danica.tools.registry import ensure_tools
    ensure_tools((name,))


def from_history(ws: Workspace, messages: Iterable[dict[str, Any]]) -> None:
    """A resumed conversation: the tools it used stay loaded, so the start of the prompt matches what was cached."""
    for m in messages:
        for call in m.get("tool_calls") or []:
            from_tool(ws, (call.get("function") or {}).get("name", ""))


def select(schemas: list[dict[str, Any]], ws: Workspace) -> list[dict[str, Any]]:
    """The definitions to send: everything not in the book, plus the book tools that are loaded. Same order every time."""
    if not enabled():
        return schemas
    on = loaded(ws)
    return [s for s in schemas if s["function"]["name"] not in PAGE or s["function"]["name"] in on]


def hidden_names(schemas: list[dict[str, Any]], ws: Workspace) -> list[str]:
    """Names of the tools that are in the book and not loaded right now."""
    on = loaded(ws)
    return [s["function"]["name"] for s in schemas if s["function"]["name"] in PAGE and s["function"]["name"] not in on]


def search(query: str) -> list[int]:
    """Lines of the book that match the words of `query` (tool names count too), best first."""
    words = [w for w in query.lower().replace(",", " ").split() if len(w) > 1]
    scored = []
    for i, (names, about) in enumerate(BOOK):
        text = (" ".join(names) + " " + about).lower()
        hits = sum(w in text for w in words)
        if hits:
            scored.append((-hits, i))
    return [i for _, i in sorted(scored)]


def suggest(name: str) -> list[str]:
    """Names in the book that look like a misspelt `name`."""
    return difflib.get_close_matches(name, list(PAGE), n=3, cutoff=0.5)


def usage(schema: dict[str, Any]) -> str:
    """One tool's full instructions: what it does and every argument (the model needs this in the SAME step it loads the
    tool, because the definition only joins the tool list on the next step)."""
    fn = schema["function"]
    params = fn["parameters"].get("properties", {})
    required = set(fn["parameters"].get("required", []))
    lines = [f"## {fn['name']}", fn["description"], "arguments:"]
    for key, spec in params.items():
        kind = spec.get("type", "any")
        extra = f", default {spec['default']!r}" if "default" in spec else ""
        lines.append(f"  - {key} ({kind}{', required' if key in required else ''}{extra}): {spec.get('description', '')}")
    return "\n".join(lines) if params else "\n".join(lines[:2])
