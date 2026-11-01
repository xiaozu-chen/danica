"""
sandbox_test.py - Checks that the Job Object really enforces its limits.
Starts real processes (python, ping). Run:  python -m scripts.sandbox_test
"""

import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

from danica.config import CONFIG
from danica.sandbox import Job
from danica.tools import run_tool
from danica.workspace import Workspace


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


def run_py(code: str, memory_mb: int, max_processes: int = 64) -> str:
    """Run python code inside a fresh job and return its combined output."""
    with Job(memory_mb=memory_mb, max_processes=max_processes) as job:
        proc = job.popen([sys.executable, "-c", code],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out, _ = proc.communicate(timeout=60)
    return out.decode("utf-8", errors="replace")


# --- memory cap: the same 300 MB allocation passes under 1 GB and fails under 150 MB ---
ALLOC = "x = bytearray(300 * 1024 * 1024); print('ALLOCATED')"
check("control: 300 MB fits under a 1024 MB cap", "ALLOCATED" in run_py(ALLOC, 1024))
out = run_py(ALLOC, 150)
check("300 MB is refused under a 150 MB cap", "ALLOCATED" not in out and "MemoryError" in out)

# --- process cap: ten sleeping children, but only a few may live ---
SPAWN = textwrap.dedent("""
    import subprocess, time
    kids = []
    for _ in range(10):
        try:
            kids.append(subprocess.Popen(["ping", "-n", "6", "127.0.0.1"], stdout=subprocess.DEVNULL))
        except OSError:
            pass   # refused outright: also fine
    time.sleep(1.5)   # a child that was killed on arrival has exited by now
    print("ALIVE", sum(1 for k in kids if k.poll() is None))
""")


def alive_count(max_processes: int) -> int:
    out = run_py(SPAWN, 1024, max_processes)
    return int(out.split("ALIVE")[1].split()[0])


check("control: 10 children live under a cap of 64", alive_count(64) == 10)
# The parent python takes 1-2 slots (a venv adds a launcher), so a cap of 6 leaves room for 4 at most
check("process cap limits children", alive_count(6) <= 4)

# --- kill takes the whole tree, grandchildren included ---
TREE = textwrap.dedent("""
    import subprocess, time
    for _ in range(3):
        subprocess.Popen(["ping", "-n", "30", "127.0.0.1"], stdout=subprocess.DEVNULL)
    print("READY", flush=True)
    time.sleep(30)
""")
with Job(memory_mb=1024, max_processes=64) as job:
    proc = job.popen([sys.executable, "-c", TREE], stdout=subprocess.PIPE)
    proc.stdout.readline()   # blocks until the child printed READY
    check("job sees parent + grandchildren", job.active_processes() >= 4)
    job.kill()
    deadline = time.time() + 5
    while job.active_processes() > 0 and time.time() < deadline:
        time.sleep(0.1)
    check("kill() empties the whole job", job.active_processes() == 0)
    proc.stdout.close()

# --- end to end through the real run_shell tool ---
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    ws = Workspace(Path(tmp) / "ws")
    original = CONFIG["sandbox"]["memory_mb"]
    CONFIG["sandbox"]["memory_mb"] = 150
    try:
        out = run_tool("run_shell",
                       {"command": 'python -c "x = bytearray(300*1024*1024); print(\'ALLOCATED\')"'}, ws)
    finally:
        CONFIG["sandbox"]["memory_mb"] = original   # always restore the shared config
    check("run_shell enforces the memory cap", "ALLOCATED" not in out)
    check("run_shell explains the memory limit to the model", "memory limit" in out)

    out = run_tool("run_shell", {"command": "echo still-works"}, ws)
    check("normal commands still work inside the job", "still-works" in out and "[exit code 0]" in out)

print("\nAll good.")