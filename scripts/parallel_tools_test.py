"""Verify that only independent read calls use concurrent execution."""

import json
import threading
import time
import tempfile
from pathlib import Path
from unittest.mock import patch

import danica.agent as agent_mod
from danica.agent import Agent
from danica.workspace import Workspace


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    agent = Agent(Workspace(Path(tmp) / "workspace"))
    state = {"active": 0, "peak": 0}
    lock = threading.Lock()

    def fake_run_tool(name, args, ws):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.08)
        with lock:
            state["active"] -= 1
        return f"{name}:{args['path']}"

    calls = [
        {"id": f"read-{i}", "name": "read_file",
         "arguments": json.dumps({"path": f"file-{i}.txt"})}
        for i in range(4)
    ]
    with patch.object(agent_mod, "run_tool", side_effect=fake_run_tool):
        result = agent._execute_parallel_reads(calls)
    check("four independent read calls overlap and preserve model order",
          result is not None and state["peak"] > 1
          and [item[1] for item in result] == [f"read_file:file-{i}.txt" for i in range(4)])
    check("mutating tools are never put on the parallel path",
          agent._execute_parallel_reads(calls + [
              {"id": "write", "name": "write_file", "arguments": '{"path":"x","content":"y"}'}
          ]) is None)

print("\nAll good.")
