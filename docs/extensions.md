# Extensions and headless operation

All extension tools are namespaced and pass through Danica's tool permission
check and audit log. MCP and plugin calls are classified as
outside-workspace actions and require approval each time. This approval layer
is not an operating-system sandbox.

## MCP servers

Configure local stdio servers in `%APPDATA%\Danica\config.toml`:

```toml
[mcp.servers.example]
command = "python"
args = ["C:\\path\\to\\server.py"]
# cwd = "C:\\path\\to\\server"
```

Danica starts configured MCP servers as child processes, negotiates the
JSON-RPC protocol, and exposes their tools as `mcp_<server>_<tool>`. Extension
output is untrusted. Only configure servers you trust; a server can perform
actions under the current Windows user account after a tool call is approved.
The child process receives a small environment allowlist rather than all
environment variables.

## Python plugins

Put user-managed plugins under `%APPDATA%\Danica\plugins\<plugin>\`. Each
folder has a `plugin.toml` manifest:

```toml
name = "example"
module = "main.py"

[[tools]]
name = "hello"
description = "Return a greeting."
required = ["name"]

[tools.parameters.name]
type = "string"
```

The module defines `run(tool_name, arguments, workspace)` and returns a value
that can be represented as text. Danica invokes it in a separate Python
process. Plugin code still runs with the current user's operating-system
privileges; install only code you trust. It is not safe to treat subprocess
separation as a filesystem jail.

## Hooks, headless mode, and export

Optional `hooks.before` and `hooks.after` entries in `config.toml` are
argument-array commands. They receive one JSON object on stdin with the phase,
tool name, redacted arguments, and (after the call) redacted result. A before
hook may return `{"allow": false}` to deny the tool. Hook activity is audited.
Hooks run only when explicitly configured and are not enabled in headless mode.
Hook commands also run with the current user's privileges; configure only
trusted commands.

`danica -p "request"` runs one request without an interactive approval callback.
Actions requiring approval fail closed; this does not enable full access.
`danica --export-bundle output.zip` creates a workspace ZIP with a README and
eligible UTF-8 text files, excluding sensitive paths, detected secrets,
binaries, and large files.
