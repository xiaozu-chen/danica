"""
todo.py - The TODO.md scratchpad tool.

The file lives outside the workspace (ws.todo_path), so the model can only
change it through this tool. The agent loop shows it to the model again on
every call (see Agent._messages_for_model), which is why it survives
pruning and compaction.

Format: free markdown, but checklist lines are normalised to
    1. [ ] text      (open)
    2. [x] text      (done)
so the model can tick items by NUMBER with action="done".
"""

import re

from danica.secret_scan import find_secrets
from danica.tools.registry import tool
from danica.workspace import Workspace

MAX_TODO_CHARS = 3000   # paid for on EVERY model call, so keep it small

# Matches "- [ ] x", "* [x] x", "3. [ ] x" (any bullet style the model may use)
ITEM_RX = re.compile(r"^\s*(?:[-*]|\d+\.)\s*\[( |x|X)\]\s*(.*)$")
NUMBERED_RX = re.compile(r"^(\d+)\. \[( |x)\] ")


def normalize(text: str) -> str:
    """Renumber every checklist line 1, 2, 3... and unify the marker style."""
    out, n = [], 0
    for line in text.replace("\r\n", "\n").splitlines():
        m = ITEM_RX.match(line)
        if m:
            n += 1
            mark = "x" if m.group(1) in ("x", "X") else " "
            out.append(f"{n}. [{mark}] {m.group(2).strip()}")
        else:
            out.append(line.rstrip())
    return "\n".join(out).strip() + "\n"


def counts(text: str) -> tuple[int, int]:
    """Return (total_items, done_items)."""
    total = done = 0
    for line in text.splitlines():
        m = NUMBERED_RX.match(line)
        if m:
            total += 1
            done += m.group(2) == "x"
    return total, done

def open_items(text: str) -> list[int]:
    """Numbers of the checklist items that are not ticked yet."""
    out = []
    for line in text.splitlines():
        m = NUMBERED_RX.match(line)
        if m and m.group(2) == " ":
            out.append(int(m.group(1)))
    return out

def read_todo(ws: Workspace) -> str:
    """The current TODO.md text, or "" if there is none."""
    if ws.todo_path is None or not ws.todo_path.exists():
        return ""
    return ws.todo_path.read_text(encoding="utf-8")


def _save(ws: Workspace, text: str) -> str:
    ws.todo_path.parent.mkdir(parents=True, exist_ok=True)
    with open(ws.todo_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    total, done = counts(text)
    msg = f"OUTPUT: TODO.md updated ({done}/{total} items done)"
    if total and done == total:
        msg += ". Every item is done: verify the result, then give your final summary."
    return msg


@tool(
    "todo",
    "Your scratchpad plan (TODO.md). action 'set' replaces it with markdown: a goal line "
    "plus a checklist of '- [ ] step' lines. action 'done' ticks off checklist items by "
    "number (item '2' or '1,2,3'); tick each step right after you finish it. action 'clear' "
    "deletes the plan. action 'show' prints it. It is shown to you again every turn.",
    {
        "action": {"type": "string", "enum": ["set", "done", "show", "clear"],
                   "description": "What to do"},
        "content": {"type": "string", "description": "For 'set': the full markdown text",
                    "default": ""},
        # A string, so the model can tick several items in one call: "1,2,3"
        "item": {"type": "string", "description": "For 'done': item number(s), like 2 or 1,2,3",
                 "default": ""},
    },
)
def todo(ws: Workspace, action: str, content: str = "", item: str | int = "") -> str:
    """Your scratchpad plan (TODO.md). action 'set' replaces it with markdown: a
        goal line plus a checklist of '- [ ] step' lines. action 'done' ticks off
        checklist items by number (item '2' or '1,2,3'); tick each step right after
        you finish it. action 'clear' deletes the plan. action 'show' prints it.
    """
    if ws.todo_path is None:
        return "ERROR: there is no TODO.md in this session"

    if action == "show":
        return read_todo(ws) or "(TODO.md is empty. Use action 'set' to write a plan.)"

    if action == "clear":
        if ws.todo_path.exists():
            ws.todo_path.unlink()
        return "OUTPUT: TODO.md cleared"

    if action == "set":
        if not content.strip():
            return "ERROR: 'set' needs content (a goal line and a checklist of '- [ ]' steps)"
        text = normalize(content)
        if len(text) > MAX_TODO_CHARS:
            return (f"ERROR: TODO is {len(text)} characters; the limit is {MAX_TODO_CHARS}. "
                    f"Make the plan shorter.")
        if find_secrets(text):
            return "ERROR: refused to save this: it contains what looks like a secret."
        if text.lstrip().startswith("[pruned"):
            return "ERROR: that is a [pruned] placeholder, not a plan. Write the real plan."
        return _save(ws, text)

    if action == "done":
        text = read_todo(ws)
        total, _ = counts(text)
        if total == 0:
            return "ERROR: TODO.md has no checklist items yet. Use action 'set' first."

        # Accept 2, "2", "1,2,3" or "1 2 3"
        try:
            wanted = [int(x) for x in re.split(r"[,\s]+", str(item).strip()) if x]
        except ValueError:
            return "ERROR: 'item' must be a number or a list like 1,2,3"
        if not wanted:
            return "ERROR: 'done' needs the item number(s), for example item '2' or '1,2,3'"
        bad = [n for n in wanted if not 1 <= n <= total]
        if bad:
            return f"ERROR: there is no item {bad[0]}. Items are numbered 1 to {total}."

        lines = text.splitlines()
        newly = 0
        for i, line in enumerate(lines):
            m = NUMBERED_RX.match(line)
            if m and int(m.group(1)) in wanted and m.group(2) == " ":
                lines[i] = line.replace("[ ]", "[x]", 1)
                newly += 1
        if newly == 0:
            return f"Item(s) {', '.join(map(str, wanted))} already done."
        return _save(ws, "\n".join(lines) + "\n")

    return f"ERROR: unknown action '{action}'. Use set, done, show or clear."