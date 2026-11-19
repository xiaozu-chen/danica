"""
dev_server - start, stop and inspect a project's dev server in the background (see devserver.py).
Pairs with screenshot: start it, take screenshots of its http://localhost address, stop it when finished.
"""

from danica import devserver
from danica.devserver import DevServerError
from danica.secret_scan import is_sensitive_path
from danica.tools.registry import tool
from danica.workspace import Workspace


@tool(
    "dev_server",
    "Run a web project's dev server in the BACKGROUND (never use run_shell for `npm run dev`: it never exits and "
    "hangs). action: start (folder = the project folder with package.json; script = dev or preview), status, logs "
    "(id) or stop (id, or empty for all). start waits until the server answers and returns its address, for example "
    "http://127.0.0.1:5173/. Then use screenshot with that address as target, and edit files: the server reloads by "
    "itself. Stop it when you are done.",
    {
        "action": {"type": "string", "description": "start, status, logs or stop"},
        "folder": {"type": "string", "default": "", "description": "Project folder for start, for example app"},
        "script": {"type": "string", "default": "dev", "description": "dev (live reload) or preview (serves the built dist)"},
        "id": {"type": "string", "default": "", "description": "Server id for logs / stop, for example dev1"},
    },
)
def dev_server(ws: Workspace, action: str, folder: str = "", script: str = "dev", id: str = "") -> str:
    """Run a web project's dev server in the BACKGROUND (never use run_shell for
        `npm run dev`: it never exits and hangs). action: start (folder = the
        project folder with package.json; script = dev or preview), status, logs
        (id) or stop (id, or empty for all). start waits until the server answers
        and returns its address, for example http://127.0.0.1:5173/.
    """
    try:
        if action == "start":
            path = ws.resolve(folder or ".")
            if not path.is_dir():
                return f"ERROR: '{folder}' is not a folder"
            if is_sensitive_path(ws.rel(path)):
                return "ERROR: that folder is blocked: it looks like a credentials folder"
            server = devserver.start(path, script)
            return (f"OUTPUT: {server.id} is running at {server.url} (project {ws.display(path)}). "
                    f"Use screenshot with target={server.url} to look at it. It reloads when you edit files. "
                    f"When finished: dev_server action=stop id={server.id}.")
        if action == "status":
            lines = devserver.describe()
            return "OUTPUT: " + ("\n".join(lines) if lines else "no dev server is running")
        if action == "logs":
            return f"OUTPUT: last output of {id}:\n{devserver.logs(id)}"
        if action == "stop":
            stopped = devserver.stop(id)
            return f"OUTPUT: stopped {', '.join(stopped)}" if stopped else "OUTPUT: no dev server was running"
        return "ERROR: action must be start, status, logs or stop"
    except DevServerError as exc:
        return f"ERROR: {exc}"
