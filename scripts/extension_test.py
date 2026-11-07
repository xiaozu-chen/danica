"""MCP, Python plugin isolation, hooks, and extension permissions tests."""

import json
import sys
import tempfile
from pathlib import Path

from danica.extensions import ExtensionManager
from danica.permissions import Tier, classify
from danica.tools.registry import tool_schemas
from danica.workspace import Workspace


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    root = Path(tmp)
    plugin_dir = root / "plugins" / "sample"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text(
        'name = "sample"\nmodule = "main.py"\n'
        '[[tools]]\nname = "echo"\ndescription = "Return the supplied value."\n'
        '[tools.parameters.value]\ntype = "string"\n',
        encoding="utf-8",
    )
    (plugin_dir / "main.py").write_text(
        "def run(tool_name, arguments, workspace):\n"
        "    return f'{tool_name}:{arguments.get(\"value\")}:{workspace}'\n",
        encoding="utf-8",
    )
    mcp_server = root / "mcp_server.py"
    mcp_server.write_text(
        "import json,sys\n"
        "for line in sys.stdin:\n"
        " m=json.loads(line); method=m.get('method'); i=m.get('id')\n"
        " if method=='exit': break\n"
        " if i is None: continue\n"
        " if method=='initialize': r={'protocolVersion':'2024-11-05','capabilities':{},'serverInfo':{'name':'fake','version':'1'}}\n"
        " elif method=='tools/list': r={'tools':[{'name':'echo','description':'Echo value','inputSchema':{'type':'object','properties':{'value':{'type':'string'}},'required':['value']}}]}\n"
        " elif method=='tools/call': r={'content':[{'type':'text','text':m['params']['arguments']['value']}]}\n"
        " else: r={}\n"
        " print(json.dumps({'jsonrpc':'2.0','id':i,'result':r}),flush=True)\n",
        encoding="utf-8",
    )
    before_hook = [
        sys.executable, "-c",
        "import json,sys; json.load(sys.stdin); print(json.dumps({'allow': False}))",
    ]
    config = {
        "mcp": {"servers": {"test": {"command": sys.executable, "args": [str(mcp_server)]}}},
        "hooks": {"before": [ {"command": before_hook} ], "after": []},
    }
    manager = ExtensionManager(root, config)
    ws = Workspace(root / "workspace")
    ws.extension_manager = manager

    schemas = {schema["function"]["name"] for schema in tool_schemas()}
    check("user-managed Python plugin tool is registered",
          "plugin_sample_echo" in schemas)
    check("plugin tools require outside-workspace confirmation",
          classify("plugin_sample_echo", {"value": "x"})[0] == Tier.OUTSIDE)
    plugin_result = manager.call_plugin(plugin_dir / "main.py", "echo", {"value": "hello"}, ws)
    check("Python plugin executes in a separate worker process",
          plugin_result.startswith(f"echo:hello:{ws.root}"))

    check("MCP server exposes tools through JSON-RPC",
          "mcp_test_echo" in schemas and manager.call_mcp("test", "echo", {"value": "mcp"})
          == "mcp")
    check("configured before hooks can deny and are not bypassed",
          manager.run_hooks("before", "read_file", {"path": "doc.md"}) is False)
    manager.close()

print("\nAll good.")
