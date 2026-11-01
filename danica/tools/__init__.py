"""
Importing this package imports files.py, whose @tool decorators
fill the registry. Later tool modules get imported here too.
"""

from danica.tools import files, search, shell, todo  # noqa: F401  (imported for their @tool side effects)
from danica.tools.registry import run_tool, tool_schemas  # noqa: F401