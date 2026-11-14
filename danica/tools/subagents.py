"""Subagent delegation tool."""

from danica.subagents import SubagentError
from danica.tools.registry import tool


@tool("delegate", "Delegate one bounded research or analysis subtask and receive a concise structured summary.",
      {"task": {"type": "string", "description": "A self-contained subtask brief"}})
def delegate(ws, task: str) -> str:
    supervisor = getattr(ws, "subagent_supervisor", None)
    if supervisor is None:
        return "ERROR: subagent delegation is unavailable in this session."
    try:
        return "OUTPUT: " + supervisor.run(task)
    except SubagentError as exc:
        return f"ERROR: delegation failed: {type(exc).__name__}: {exc}"
