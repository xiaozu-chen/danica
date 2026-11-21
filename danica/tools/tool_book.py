"""
tool_book - the directory of tools that are not sent with every request (see toolbook.py).

Always present. Its description IS the book: one line per tool group, so the model always knows what exists. Calling it
loads tools and prints how to use them.
"""

from danica import toolbook
from danica.tools.registry import TOOLS, tool
from danica.workspace import Workspace


@tool("tool_book",
      "Tool book: the extra tools that are not in your tool list right now. Check this list BEFORE you improvise with "
      "run_shell or write_file: if a tool below fits the job, load it and use it.\n" + toolbook.catalog() + "\n"
      "To use one, call tool_book with tools=[\"name\"]: it loads the tool and prints its full usage, and it stays in your "
      "tool list for the rest of the session. Not sure which fits? tool_book with query=\"words\" searches the book; "
      "with nothing, it prints the whole book.",
      {"tools": {"type": "array", "items": {"type": "string"}, "description": "Tool names to load, e.g. [\"deck\"]", "default": []},
       "query": {"type": "string", "description": "Words to search the book for, e.g. \"slides\"", "default": ""}})
def tool_book(ws: Workspace, tools: list | None = None, query: str = "") -> str:
    """Load tools from the tool book, search it, or print it."""
    tools = tools or []
    if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
        return "ERROR: tools must be a list of names such as [\"deck\"]. Call tool_book with no arguments to see the book."
    if not tools and not query.strip():
        return "OUTPUT: tool book (load with tools=[\"name\"]):\n" + toolbook.catalog()
    out: list[str] = []
    if query.strip():
        found = toolbook.search(query)
        out.append("OUTPUT: matches for " + repr(query.strip()) + ":\n" + "\n".join(
            f"- {', '.join(toolbook.BOOK[i][0])}: {toolbook.BOOK[i][1]}" for i in found)
            if found else f"OUTPUT: nothing in the book matches {query.strip()!r}. Call tool_book with no arguments to see it all.")
    if tools:
        unknown = [t for t in tools if t not in toolbook.PAGE]
        toolbook.load(ws, tools)
        shown: list[str] = []
        for t in tools:
            if t in toolbook.PAGE:
                for name in toolbook.BOOK[toolbook.PAGE[t]][0]:
                    if name in TOOLS and name not in shown:
                        shown.append(name)
        out.append(f"OUTPUT: loaded {', '.join(shown) or 'nothing'}. They are in your tool list from your next step. Usage:\n\n"
                   + "\n\n".join(toolbook.usage(TOOLS[n]["schema"]) for n in shown))
        for t in unknown:
            hint = toolbook.suggest(t)
            out.append(f"ERROR: {t!r} is not in the tool book" + (f" (did you mean {', '.join(hint)}?)" if hint else "")
                       + ". Tools such as read_file, run_shell and web_search are always available and need no loading.")
    return "\n\n".join(out)
