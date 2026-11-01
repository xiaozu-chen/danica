"""
registry.py - Where tools are registered and dispatched.

A tool is a plain Python function: fn(ws, **args) -> str.
The returned string is exactly what the model will see as the tool result.
Errors are returned as strings too (never raised to the loop), so the model
can read the message and correct itself.
"""

from danica.workspace import PathJailError, Workspace

# name -> {"fn": callable, "schema": OpenAI-style tool definition}
TOOLS: dict[str, dict] = {}


def tool(name: str, description: str, parameters: dict):
    """Decorator that registers a function as a model-callable tool."""

    def decorator(fn):
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


def tool_schemas() -> list[dict]:
    """All tool definitions, in the format llama-server's API expects."""
    return [t["schema"] for t in TOOLS.values()]


def run_tool(name: str, args: dict, ws: Workspace) -> str:
    """Execute one tool call and ALWAYS return a string, even on failure."""
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