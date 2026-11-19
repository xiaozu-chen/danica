"""
Importing this package imports files.py, whose @tool decorators
fill the registry. Later tool modules get imported here too.
"""

from danica.tools import chart, computer_tools, dashboard, deck, diagram, document, fetch, files, game_run, interactive_tools, knowledge, knowledge_synthesis, knowledge_write, mail_tools, mermaid, recall, search, shell, dev_server, screenshot, skills, vision_tools, subagents, todo, webcache_tool, websearch  # noqa: F401  (imported for their @tool side effects)
from danica.tools.registry import run_tool, tool_schemas  # noqa: F401