"""MCP, user-managed Python plugins, and opt-in tool hooks."""

import json
import os
import re
import subprocess
import sys
import threading
import tomllib
from pathlib import Path

from danica.tools.registry import register_external_tool, tool_schemas

MAX_EXTENSION_OUTPUT = 16_000
MAX_EXTENSION_TOOLS = 64
RPC_TIMEOUT = 30
MAX_SCHEMA_BYTES = 16_000


class ExtensionError(Exception):
    """An extension configuration, process, or protocol error."""


def _safe_name(value: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_]", "_", value).strip("_").lower()
    if not name:
        raise ExtensionError("extension name has no valid characters")
    return name[:40]


def _child_env() -> dict[str, str]:
    allowed = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "APPDATA", "LOCALAPPDATA")
    return {key: os.environ[key] for key in allowed if key in os.environ}


class MCPServer:
    def __init__(self, name: str, command: list[str], cwd: Path | None = None):
        if not command or not all(isinstance(part, str) and part for part in command):
            raise ExtensionError(f"MCP server {name} needs a non-empty command and argument list")
        self.name = name
        self.command = command
        self.cwd = cwd
        self.process: subprocess.Popen | None = None
        self._condition = threading.Condition()
        self._write_lock = threading.Lock()
        self._next_id = 1
        self._responses: dict[int, dict] = {}
        self._failure = ""
        self._reader: threading.Thread | None = None

    def start(self) -> None:
        if self.process is not None:
            return
        try:
            self.process = subprocess.Popen(
                self.command, cwd=str(self.cwd) if self.cwd else None,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                env=_child_env(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
        except OSError as exc:
            raise ExtensionError(f"could not start MCP server {self.name}: {exc}") from exc
        self._reader = threading.Thread(target=self._read_responses,
                                        name=f"danica-mcp-{self.name}", daemon=True)
        self._reader.start()
        self.request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "Danica", "version": "0.1.0"},
        })
        self._notify("notifications/initialized", {})

    def _read_responses(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            while True:
                line = process.stdout.readline(MAX_EXTENSION_OUTPUT * 64 + 1)
                if not line:
                    break
                if len(line) > MAX_EXTENSION_OUTPUT * 64:
                    with self._condition:
                        self._failure = "MCP server response exceeded the protocol size limit"
                        self._condition.notify_all()
                    break
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    with self._condition:
                        self._failure = "MCP server wrote invalid JSON-RPC to stdout"
                        self._condition.notify_all()
                    continue
                if isinstance(message, dict) and isinstance(message.get("id"), int):
                    with self._condition:
                        self._responses[message["id"]] = message
                        self._condition.notify_all()
        except OSError as exc:
            with self._condition:
                self._failure = f"MCP response reader failed: {exc}"
                self._condition.notify_all()
        finally:
            with self._condition:
                if not self._failure:
                    self._failure = "MCP server closed its output"
                self._condition.notify_all()

    def _send(self, message: dict) -> None:
        process = self.process
        if process is None or process.stdin is None or process.poll() is not None:
            raise ExtensionError(self._failure or f"MCP server {self.name} is not running")
        try:
            with self._write_lock:
                process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
                process.stdin.flush()
        except OSError as exc:
            raise ExtensionError(f"could not write to MCP server {self.name}: {exc}") from exc

    def request(self, method: str, params: dict | None = None) -> dict:
        request_id = self._next_id
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method,
                    "params": params or {}})
        with self._condition:
            ready = self._condition.wait_for(
                lambda: request_id in self._responses or bool(self._failure), timeout=RPC_TIMEOUT)
            if not ready:
                raise ExtensionError(f"MCP request {method} timed out")
            response = self._responses.pop(request_id, None)
            if response is None:
                raise ExtensionError(self._failure)
        if "error" in response:
            error = response["error"]
            raise ExtensionError(f"MCP {method} failed: {str(error)[:500]}")
        return response.get("result", {})

    def _notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def call_tool(self, name: str, arguments: dict) -> str:
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        if not isinstance(result, dict):
            raise ExtensionError(f"MCP tool {name} returned an invalid result")
        if result.get("isError"):
            raise ExtensionError(f"MCP tool {name} returned an error")
        content = result.get("content", [])
        if not isinstance(content, list):
            raise ExtensionError(f"MCP tool {name} returned invalid content")
        if any(not isinstance(item, dict) or item.get("type") != "text"
               or not isinstance(item.get("text"), str) for item in content):
            raise ExtensionError(f"MCP tool {name} returned unsupported non-text content")
        rendered = "\n".join(item["text"] for item in content)
        if len(rendered) > MAX_EXTENSION_OUTPUT:
            rendered = rendered[:MAX_EXTENSION_OUTPUT] + "\n[extension result truncated]"
        if not rendered:
            raise ExtensionError(f"MCP tool {name} returned no text content")
        return rendered

    def close(self) -> None:
        process = self.process
        if process is None:
            return
        try:
            if process.poll() is None:
                self.request("shutdown")
                self._notify("exit", {})
                process.wait(timeout=2)
        except (ExtensionError, subprocess.TimeoutExpired):
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        self.process = None


class ExtensionManager:
    def __init__(self, app_dir: Path, config: dict, audit=None, enable_mcp: bool = True,
                 enable_hooks: bool = True):
        self.app_dir = Path(app_dir)
        self.config = config
        self.audit = audit
        self.enable_hooks = enable_hooks
        self.plugins: dict[str, tuple[Path, str]] = {}
        self.servers: dict[str, MCPServer] = {}
        self.errors: list[str] = []
        self._load_plugins()
        if enable_mcp:
            self._load_mcp()

    def has_hooks(self) -> bool:
        hooks = self.config.get("hooks", {})
        if not isinstance(hooks, dict):
            return self.enable_hooks
        return self.enable_hooks and any(hooks.get(phase, []) for phase in ("before", "after"))

    def _log(self, event: str, **data) -> None:
        if self.audit:
            self.audit.append(event, **data)

    def _load_plugins(self) -> None:
        root = self.app_dir / "plugins"
        if not root.is_dir():
            return
        for manifest in sorted(root.glob("*/plugin.toml")):
            try:
                self._register_plugin(manifest)
            except (OSError, ValueError, tomllib.TOMLDecodeError, ExtensionError) as exc:
                self.errors.append(f"{manifest.parent.name}: {exc}")

    def _register_plugin(self, manifest: Path) -> None:
        root = manifest.parent.resolve()
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
        name = _safe_name(str(data.get("name", root.name)))
        module = (root / str(data.get("module", ""))).resolve()
        if not module.is_relative_to(root) or module.suffix != ".py" or not module.is_file():
            raise ExtensionError("module must be a Python file inside the plugin folder")
        tools = data.get("tools", [])
        if not isinstance(tools, list) or len(tools) > MAX_EXTENSION_TOOLS:
            raise ExtensionError("tools must be an array of tables")
        for item in tools:
            if not isinstance(item, dict):
                raise ExtensionError("plugin tool entry must be a table")
            tool_name = _safe_name(str(item.get("name", "")))
            full_name = f"plugin_{name}_{tool_name}"
            parameters = item.get("parameters", {})
            if not isinstance(parameters, dict):
                raise ExtensionError(f"{full_name} parameters must be an object")

            def invoke(ws, _module=module, _name=tool_name, **arguments):
                manager = getattr(ws, "extension_manager", None)
                if manager is None:
                    return "ERROR: plugin manager is unavailable."
                return manager.call_plugin(_module, _name, arguments, ws)

            register_external_tool(full_name, str(item.get("description", "User-managed Python plugin tool")),
                                   {"type": "object", "properties": parameters,
                                    "required": item.get("required", []),
                                    "additionalProperties": False}, invoke)
            self.plugins[full_name] = (module, tool_name)

    def _load_mcp(self) -> None:
        configured = self.config.get("mcp", {}).get("servers", {})
        if not isinstance(configured, dict):
            self.errors.append("mcp.servers must be a table of server configurations")
            return
        for server_name, entry in configured.items():
            server = None
            try:
                if not isinstance(entry, dict):
                    raise ExtensionError("server configuration must be a table")
                name = _safe_name(str(server_name))
                arguments = entry.get("args", [])
                if not isinstance(arguments, list):
                    raise ExtensionError("args must be an array of strings")
                if not all(isinstance(item, str) for item in arguments):
                    raise ExtensionError("args must be an array of strings")
                command = [str(entry.get("command", ""))] + arguments
                server = MCPServer(name, command,
                                   Path(entry["cwd"]).expanduser().resolve() if entry.get("cwd") else None)
                server.start()
                result = server.request("tools/list")
                tools = result.get("tools", [])
                if not isinstance(tools, list) or len(tools) > MAX_EXTENSION_TOOLS:
                    raise ExtensionError("tools/list did not return a tool list")
                registrations = []
                for item in tools:
                    if not isinstance(item, dict):
                        raise ExtensionError("tools/list contained a malformed tool entry")
                    original = _safe_name(str(item.get("name", "")))
                    full_name = f"mcp_{name}_{original}"
                    schema = item.get("inputSchema", {"type": "object", "properties": {}})
                    if not isinstance(schema, dict) or schema.get("type") != "object":
                        raise ExtensionError(f"MCP tool {original} has an invalid inputSchema")
                    if len(json.dumps(schema, ensure_ascii=False)) > MAX_SCHEMA_BYTES:
                        raise ExtensionError(f"MCP tool {original} schema exceeds the size limit")
                    schema = dict(schema)
                    schema.setdefault("additionalProperties", False)

                    def invoke(ws, _server=name, _tool=str(item.get("name")), **arguments):
                        manager = getattr(ws, "extension_manager", None)
                        if manager is None:
                            return "ERROR: MCP manager is unavailable."
                        return manager.call_mcp(_server, _tool, arguments)

                    registrations.append((full_name, str(item.get("description", "MCP tool")),
                                          schema, invoke))
                existing = {item["function"]["name"] for item in tool_schemas()}
                names = [entry[0] for entry in registrations]
                if len(names) != len(set(names)) or existing.intersection(names):
                    raise ExtensionError("MCP tool name conflicts with an existing tool")
                for full_name, description, schema, invoke in registrations:
                    register_external_tool(full_name, description, schema, invoke)
                self.servers[name] = server
                self._log("mcp_server_start", server=name, tools=len(tools))
            except (OSError, ValueError, KeyError, TypeError, ExtensionError) as exc:
                if server is not None:
                    try:
                        server.close()
                    except (OSError, ExtensionError, subprocess.TimeoutExpired) as close_exc:
                        self.errors.append(f"MCP {server_name} cleanup: {close_exc}")
                self.errors.append(f"MCP {server_name}: {exc}")

    def call_mcp(self, server_name: str, tool_name: str, arguments: dict) -> str:
        server = self.servers.get(server_name)
        if server is None:
            raise ExtensionError(f"MCP server {server_name} is unavailable")
        result = server.call_tool(tool_name, arguments)
        self._log("mcp_tool_result", server=server_name, tool=tool_name,
                  result_chars=len(result))
        return result

    def call_plugin(self, module: Path, tool_name: str, arguments: dict, ws) -> str:
        command = [sys.executable, "-m", "danica.extension_worker",
                   "--module", str(module), "--tool", tool_name]
        payload = json.dumps({"arguments": arguments, "workspace": str(ws.root)}).encode("utf-8")
        if len(payload) > 32_000:
            raise ExtensionError("plugin input exceeds the 32 KiB limit")
        try:
            result = subprocess.run(
                command, input=payload, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                cwd=str(module.parent), env=_child_env(), timeout=RPC_TIMEOUT, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ExtensionError(f"plugin {module.parent.name} failed: {exc}") from exc
        if result.returncode != 0:
            raise ExtensionError(f"plugin process exited with status {result.returncode}")
        try:
            response = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExtensionError("plugin returned invalid JSON") from exc
        if isinstance(response, dict) and "error" in response:
            raise ExtensionError(f"plugin failed: {str(response['error'])[:500]}")
        if not isinstance(response, dict) or "result" not in response:
            raise ExtensionError("plugin response must contain result")
        rendered = str(response["result"])
        if len(rendered) > MAX_EXTENSION_OUTPUT:
            rendered = rendered[:MAX_EXTENSION_OUTPUT] + "\n[plugin result truncated]"
        self._log("plugin_tool_result", plugin=module.parent.name, tool=tool_name,
                  result_chars=len(rendered))
        return rendered

    def run_hooks(self, phase: str, tool_name: str, arguments: dict,
                  result: str = "") -> bool:
        if not self.enable_hooks:
            return True
        hooks = self.config.get("hooks", {}).get(phase, [])
        if not isinstance(hooks, list):
            raise ExtensionError(f"hooks.{phase} must be an array of command tables")
        from danica.secret_scan import redact

        def safe_value(value):
            if isinstance(value, str):
                return redact(value[:4000])[0]
            if isinstance(value, list):
                return [safe_value(item) for item in value[:100]]
            if isinstance(value, dict):
                return {str(key)[:100]: safe_value(item)
                        for key, item in list(value.items())[:100]}
            if value is None or isinstance(value, (bool, int, float)):
                return value
            return str(type(value).__name__)

        safe_args = safe_value(arguments)
        safe_result, _ = redact(result[:MAX_EXTENSION_OUTPUT])
        payload = json.dumps({"phase": phase, "tool": tool_name,
                              "arguments": safe_args, "result": safe_result}).encode("utf-8")
        if len(payload) > 32_000:
            raise ExtensionError("hook input exceeds the 32 KiB limit")
        for hook in hooks:
            if not isinstance(hook, dict):
                raise ExtensionError(f"hooks.{phase} entries must be tables")
            command = hook.get("command")
            if not isinstance(command, list) or not command or not all(
                    isinstance(item, str) and item for item in command):
                raise ExtensionError(f"hooks.{phase} needs an argument-array command")
            try:
                completed = subprocess.run(
                    command, input=payload, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    timeout=RPC_TIMEOUT, check=False, env=_child_env(),
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ExtensionError(f"{phase} hook failed: {exc}") from exc
            if completed.returncode != 0:
                raise ExtensionError(f"{phase} hook exited with status {completed.returncode}")
            try:
                response = json.loads(completed.stdout.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ExtensionError(f"{phase} hook returned invalid JSON") from exc
            if not isinstance(response, dict):
                raise ExtensionError(f"{phase} hook response must be an object")
            self._log("extension_hook", phase=phase, tool=tool_name,
                      hook=Path(command[0]).name, allowed=response.get("allow", True))
            if phase == "before" and response.get("allow") is False:
                return False
        return True

    def close(self) -> None:
        failures = []
        for server in self.servers.values():
            try:
                server.close()
            except (OSError, ExtensionError) as exc:
                failures.append(str(exc))
        self.servers.clear()
        if failures:
            raise ExtensionError("; ".join(failures))
