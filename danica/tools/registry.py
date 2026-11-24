"""
registry.py - Where tools are registered and dispatched.

A tool is a plain Python function: fn(ws, **args) -> str.
The returned string is exactly what the model will see as the tool result.
Errors are returned as strings too (never raised to the loop), so the model
can read the message and correct itself.
"""

from collections.abc import Callable
from importlib import import_module
import re
from typing import Any

from danica.workspace import PathJailError, Workspace

# name -> {"fn": callable, "schema": OpenAI-style tool definition}
TOOLS: dict[str, dict[str, Any]] = {}

# Keep the original module order so that lazy imports do not change the tool
# schema order sent to the model (which would invalidate its prompt cache).
_MODULE_ORDER = (
    "chart", "computer_tools", "dashboard", "deck", "diagram", "document",
    "fetch", "files", "game_run", "interactive_tools", "knowledge",
    "knowledge_synthesis", "knowledge_write", "mail_tools", "mermaid", "recall",
    "remember", "search", "shell", "dev_server", "screenshot", "skills",
    "vision_tools", "subagents", "tool_book", "todo", "webcache_tool", "websearch",
)

# These modules only register tools listed in danica.toolbook.BOOK. Their code
# is imported when that tool group is exposed or used.
_LAZY_TOOL_MODULES = {
    **{name: "computer_tools" for name in (
        "screen_windows", "screen_shot", "screen_click", "screen_drag", "screen_type",
        "screen_key", "screen_scroll", "screen_focus",
    )},
    **{name: "mail_tools" for name in ("mail_folders", "mail_list", "mail_read", "mail_scan")},
    "game_run": "game_run",
    "deck": "deck",
    "document": "document",
    "chart": "chart",
    "dashboard": "dashboard",
    "diagram": "diagram",
    "mermaid": "mermaid",
    "dev_server": "dev_server",
    "screenshot": "screenshot",
    "knowledge_search": "knowledge",
    "knowledge_synthesize": "knowledge_synthesis",
    "obsidian_write_preview": "knowledge_write",
}

# Modules whose tools are always available. interactive_tools stays eager because
# it also registers ask_user, which is always offered to the model.
_CORE_MODULES = tuple(name for name in _MODULE_ORDER if name not in {
    "chart", "computer_tools", "dashboard", "deck", "diagram", "document",
    "game_run", "knowledge", "knowledge_synthesis", "knowledge_write", "mail_tools",
    "mermaid", "dev_server", "screenshot",
})
BUILTIN_TOOL_NAMES = frozenset(_LAZY_TOOL_MODULES) | frozenset({
    "delegate", "ask_user", "quiz", "flashcards", "flowchart",
    "read_file", "write_file", "edit_file", "list_dir", "glob", "grep",
    "web_fetch", "run_shell", "recall", "remember", "load_skill", "web_search",
    "web_cache_search", "todo", "tool_book", "view_image",
})


def ensure_tools(names=(), *, all_tools: bool = False) -> None:
    """Import core tool definitions and any requested lazy tool modules."""
    modules = set(_CORE_MODULES)
    if all_tools:
        modules.update(_MODULE_ORDER)
    else:
        modules.update(_LAZY_TOOL_MODULES[name] for name in names if name in _LAZY_TOOL_MODULES)
    for name in _MODULE_ORDER:
        if name in modules:
            import_module(f"danica.tools.{name}")


def tool(
    name: str,
    description: str,
    parameters: dict[str, Any],
) -> Callable[[Callable[..., str]], Callable[..., str]]:
    """Decorator that registers a function as a model-callable tool."""

    def decorator(fn: Callable[..., str]) -> Callable[..., str]:
        TOOLS[name] = {
            "fn": fn,
            "schema": {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": {
                        "type": "object",
                        "properties": parameters,
                        # Every parameter without a "default" key is required
                        "required": [k for k, v in parameters.items() if "default" not in v],
                    },
                },
            },
        }
        return fn

    return decorator


def tool_schemas(ws: Workspace | None = None, *, all_tools: bool = False) -> list[dict]:
    """Tool definitions in stable order, importing optional groups only as needed."""
    names = set(getattr(ws, "loaded_tools", ())) if ws is not None else set()
    computer = getattr(ws, "computer", None) if ws is not None else None
    if computer is not None and getattr(computer, "enabled", False):
        names.update(name for name in _LAZY_TOOL_MODULES if name.startswith("screen_"))
    ensure_tools(names, all_tools=all_tools)

    ordered = []
    for module in _MODULE_ORDER:
        ordered.extend(entry["schema"] for entry in TOOLS.values()
                       if entry["fn"].__module__ == f"danica.tools.{module}")
    ordered.extend(entry["schema"] for entry in TOOLS.values()
                   if not entry["fn"].__module__.startswith("danica.tools."))
    return ordered


def register_external_tool(
    name: str,
    description: str,
    schema: dict[str, Any],
    fn: Callable[..., str],
) -> None:
    """Register a namespaced external tool with its already-validated JSON schema."""
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name) or name in TOOLS:
        raise ValueError(f"tool name is empty or already registered: {name}")
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    invalid_schema = (
        len(str(schema)) > 16_000
        or schema.get("type") != "object"
        or not isinstance(properties, dict)
        or not isinstance(required, list)
        or not all(isinstance(item, str) for item in required)
        or len(properties) > 64
        or not set(required) <= set(properties)
        or not all(
            isinstance(key, str) and isinstance(value, dict)
            for key, value in properties.items()
        )
    )
    if invalid_schema:
        raise ValueError(f"tool {name} has an invalid input schema")
    TOOLS[name] = {
        "fn": fn,
        "schema": {
            "type": "function",
            "function": {"name": name, "description": description[:1000],
                         "parameters": schema},
        },
    }


def run_tool(name: str, args: dict, ws: Workspace) -> str:
    """Execute one tool call and ALWAYS return a string, even on failure."""
    ensure_tools((name,))
    entry = TOOLS.get(name)
    if entry is None:
        return f"ERROR: unknown tool '{name}'. Available: {', '.join(TOOLS)}"
    try:
        return entry["fn"](ws, **args)
    except PathJailError as e:
        return f"ERROR: {e}"
    except TypeError as e:
        # Wrong or missing argument names from the model
        return f"ERROR: bad arguments for {name}: {e}"
    except Exception as e:  # last-resort net: the loop must never crash
        return f"ERROR: {name} failed: {type(e).__name__}: {e}"
