"""
/perf - where the time goes. Run it after a slow message or a slow start and send me the output.

    /perf         start-up phases, the last model calls (time to first word, how much of the prompt the server had to
                  read again), and how many tokens the tool list costs right now
    /perf tools   the same tool list, tool by tool
"""

import json

from danica import perf, toolbook
from danica.context import CHARS_PER_TOKEN
from danica.tools.registry import tool_schemas
from danica.tui.commands import DESCRIPTIONS, command

DESCRIPTIONS["/perf"] = "where the time goes: start-up phases, last model calls, tool-list size"


def _tokens(schemas) -> int:
    return int(len(json.dumps(schemas)) / CHARS_PER_TOKEN)


@command("/perf")
def _perf(app, args):
    every = tool_schemas()
    sent = app.agent.active_tools()
    if args.strip().lower() == "tools":
        rows = sorted(((_tokens([s]), s["function"]["name"]) for s in every), reverse=True)
        hidden = set(toolbook.hidden_names(every, app.ws))
        app.ui.log("info", "tool definitions (tokens), biggest first; (book) = in the tool book, not loaded yet:\n" +
                   "\n".join(f"  {t:5,}  {n}" + ("  (book)" if n in hidden else "") for t, n in rows))
        return
    lines = ["start-up (since Danica's code started loading):"] + (perf.startup_lines() or ["  no marks yet"])
    lines.append("\nlast model calls (first word = how long you waited; 'cache reused' = prompt the server did NOT have to re-read):")
    lines += perf.call_lines() or ["  none yet: send a message first"]
    extra = sorted(toolbook.loaded(app.ws))
    lines.append(f"\ntools sent with each request: {len(sent)} of {len(every)} (~{_tokens(sent):,} tokens; all of them would be "
                 f"~{_tokens(every):,}). Loaded from the tool book: {', '.join(extra) or 'none'}. /perf tools lists them.")
    calls = [c for c in perf.CALLS if c["label"] == "chat" and c.get("prompt_n") is not None]
    if calls and calls[-1]["prompt_n"] > 0.6 * (calls[-1]["prompt_n"] + (calls[-1].get("cache_n") or 0)) and len(calls) > 1:
        lines.append("\nNote: on the last message the server re-read most of the prompt instead of reusing its cache. Something at the "
                     "start of the conversation changed, the server has no free slot for the conversation, or it was started "
                     "without prompt caching.")
    app.ui.log("info", "\n".join(lines))
