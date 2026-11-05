"""
engine_test.py - Checks safe program lookup, the DOT builder, the Mermaid filter, the process
runner, engine selection and the two tools, plus REAL Graphviz and Mermaid renders when installed.
Run from the project root:  python -m scripts.engine_test
Real Mermaid rendering starts a headless browser: the first render can take up to a minute.
"""

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image

import danica.agent as agent_mod
import danica.diagram_engines as eng
from danica.agent import Agent
from danica.charts import ChartError
from danica.config import CONFIG
from danica.diagrams import validate
from danica.pathsearch import find_program
from danica.tools import run_tool
from danica.workspace import Workspace


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


def error_of(fn, *args) -> str:
    try:
        fn(*args)
        return ""
    except ChartError as e:
        return str(e)


PNG = b"\x89PNG\r\n\x1a\n"
TMP = tempfile.gettempdir()


def spec_of(nodes, edges, **extra):
    return validate({"nodes": nodes, "edges": [{"from": a, "to": b} for a, b in edges], **extra})


# --- finding programs without the current directory ---
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    tmp = Path(tmp)
    safe, evil = tmp / "safe", tmp / "evil"
    safe.mkdir()
    evil.mkdir()
    (safe / "goodtool.cmd").write_text("@echo hi\n", encoding="utf-8")
    (safe / "plain.exe").write_bytes(b"MZ")
    (evil / "badtool.cmd").write_text("@echo pwned\n", encoding="utf-8")
    real_path, real_cwd = os.environ["PATH"], os.getcwd()
    os.environ["PATH"] = os.pathsep.join([str(evil), ".", "relative\\dir", str(safe)])
    os.chdir(evil)
    try:
        check("a program in a PATH folder is found (PATHEXT honoured)", find_program("goodtool") == str(safe / "goodtool.cmd"))
        check("an explicit extension works", find_program("plain.exe") == str(safe / "plain.exe"))
        check("a program in the CURRENT directory is never found, even if PATH names it", find_program("badtool") is None)
        check("an unknown program is None", find_program("no-such-program") is None)
    finally:
        os.chdir(real_cwd)
        os.environ["PATH"] = real_path

# --- the DOT builder ---
spec = spec_of([{"id": "a", "label": "Start", "shape": "oval"}, {"id": "b", "label": 'x"]; evil [image="C:\\a.png', "shape": "diamond"},
                {"id": "c", "label": "Rounded $5", "shape": "round"}], [("a", "b"), ("b", "c")], title="My \"title\"", direction="right")
dot = eng.to_dot(spec)
check("DOT has the right envelope and direction", dot.startswith("digraph G {") and dot.endswith("}") and "rankdir=LR" in dot)
check("shapes map to Graphviz shapes", "shape=ellipse" in dot and "shape=diamond" in dot and 'style="rounded,filled"' in dot)
check("the matplotlib dollar escape is removed", "Rounded $5" in dot and "\\$" not in dot)
node_line = next(l for l in dot.split("\n") if l.strip().startswith('"b"') and "label=" in l)
check("a label cannot break out of its quotes (injection stays text)",
      re.sub(r"\\.", "", node_line).count('"') == 10 and 'image=' in node_line)
check("edges are quoted", '"a" -> "b"' in dot)
check("the title is a quoted graph label", 'label="My \\"title\\""' in dot)

# --- the Mermaid filter ---
GOOD = ["flowchart TD\n  A --> B", "sequenceDiagram\n  A->>B: hi", "%% a comment\nclassDiagram\nclass A {\n<<interface>>\n}",
        "stateDiagram-v2\n  [*] --> A", "erDiagram\n  A ||--o{ B : has", "gantt\n  title T", "pie\n  \"a\": 1",
        "flowchart LR\n  A[line one<br/>line two] --> B", "mindmap\n  root((x))", "flowchart TD\n  A --> B\n  linkStyle 0 stroke:red"]
for source in GOOD:
    check(f"allowed: {source.splitlines()[-1][:30]!r}", eng.validate_mermaid(source).strip() != "")
BAD = {"an init directive": "%%{init: {\"securityLevel\": \"loose\"}}%%\nflowchart TD\nA-->B",
       "a click directive": "flowchart TD\nA-->B\nclick A href \"http://x\"",
       "a script tag": "flowchart TD\nA[<script>alert(1)</script>]-->B",
       "an image tag": "flowchart TD\nA[<img src=x>]-->B",
       "a javascript address": "flowchart TD\nA[javascript:alert(1)]-->B",
       "an unknown diagram type": "madeUpDiagram\nA-->B",
       "front matter": "---\ntitle: x\n---\nflowchart TD\nA-->B",
       "an empty file": "  \n",
       "text that is too long": "flowchart TD\n" + "A-->B\n" * 5000,
       "a secret": "flowchart TD\nA[" + "AKIA" + "IOSFODNN7EXAMPLE" + "]-->B"}
for label, source in BAD.items():
    check(f"refused: {label}", error_of(eng.validate_mermaid, source) != "")

# --- running programs (both with and without the Job Object) ---
for confine in (True, False):
    tag = "confined" if confine else "plain"
    code, out, err = eng.run([sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"], TMP,
                             stdin=b"abc", timeout=60, confine=confine)
    check(f"{tag}: stdin goes in and stdout comes out", code == 0 and out == b"ABC")
    code, _, err = eng.run([sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"], TMP,
                           timeout=60, confine=confine)
    check(f"{tag}: exit codes and error text come back", code == 3 and "boom" in err)
    started = time.time()
    message = error_of(eng.run, [sys.executable, "-c", "import time; time.sleep(30)"], TMP, None, 2, confine)
    check(f"{tag}: a runaway program is killed at the timeout", "timed out" in message and time.time() - started < 20)
    check(f"{tag}: a missing program is a clean error",
          "could not start" in error_of(eng.run, ["definitely-not-a-program-xyz"], TMP, None, 5, confine))

os.environ["DANICA_FAKE_SECRET"] = "hunter2"
try:
    _, out, _ = eng.run([sys.executable, "-c", "import os; print(os.environ.get('DANICA_FAKE_SECRET'))"], TMP, timeout=60)
finally:
    del os.environ["DANICA_FAKE_SECRET"]
check("programs run with a stripped environment (no secrets)", out.strip() == b"None")

check("a non-PNG output is refused", "PNG" in error_of(eng.check_png, b"hello"))
check("a PNG that is too big is refused", "too big" in error_of(eng.check_png, PNG + b"x" * (eng.MAX_PNG_BYTES + 1)))

# --- engine selection ---
real_gv, real_engine = eng.graphviz_png, CONFIG["diagrams"]["engine"]
small = spec_of([{"id": "a"}, {"id": "b"}], [("a", "b")])


def forbidden(_spec):
    raise AssertionError("Graphviz must not be called")


def broken(_spec):
    raise ChartError("Graphviz was not found")


try:
    CONFIG["diagrams"]["engine"] = "builtin"
    eng.graphviz_png = forbidden
    png, note = eng.render_diagram(small)
    check("builtin never calls Graphviz", png.startswith(PNG) and note == "")
    CONFIG["diagrams"]["engine"] = "auto"
    eng.graphviz_png = broken
    png, note = eng.render_diagram(small)
    check("auto falls back to the built-in engine and says so", png.startswith(PNG) and "built-in engine" in note)
    CONFIG["diagrams"]["engine"] = "graphviz"
    check("graphviz (explicit) reports the failure instead", "not found" in error_of(eng.render_diagram, small))
finally:
    eng.graphviz_png, CONFIG["diagrams"]["engine"] = real_gv, real_engine

# --- the tools ---
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    ws = Workspace(Path(tmp) / "ws")
    (ws.root / "diagrams").mkdir()
    (ws.root / "diagrams" / "flow.json").write_text(json.dumps(
        {"nodes": [{"id": "a", "label": "Start"}, {"id": "b", "label": "End"}], "edges": [{"from": "a", "to": "b"}]}), encoding="utf-8")
    CONFIG["diagrams"]["engine"] = "builtin"
    try:
        out = run_tool("diagram", {"file": "diagrams/flow.json"}, ws)
    finally:
        CONFIG["diagrams"]["engine"] = real_engine
    check("the diagram tool still works", out.startswith("OK: saved diagrams/flow.png")
          and (ws.root / "diagrams" / "flow.png").read_bytes().startswith(PNG))

    def mmd(name: str, text: str) -> str:
        (ws.root / "diagrams" / name).write_text(text, encoding="utf-8")
        return run_tool("mermaid", {"file": f"diagrams/{name}"}, ws)

    check("a non-.mmd file is refused", run_tool("mermaid", {"file": "diagrams/flow.json"}, ws).startswith("ERROR"))
    check("a missing file says to write it first", "write_file" in run_tool("mermaid", {"file": "diagrams/no.mmd"}, ws))
    check("a path outside the workspace is refused", "outside the workspace" in run_tool("mermaid", {"file": "..\\x.mmd"}, ws))
    check("a blocked directive is refused before anything runs", "not allowed" in mmd("bad.mmd", "flowchart TD\nA-->B\nclick A href \"x\""))
    (ws.root / "credentials.json").write_text("{}", encoding="utf-8")
    check("credential files are blocked", "blocked" in run_tool("diagram", {"file": "credentials.json"}, ws))
    (ws.root / ".husky").mkdir()
    (ws.root / ".husky" / "m.mmd").write_text("flowchart TD\nA-->B", encoding="utf-8")
    check("hook folders are never written to", "blocked" in run_tool("mermaid", {"file": ".husky/m.mmd"}, ws)
          and not (ws.root / ".husky" / "m.png").exists())

    real_find = eng.find_mmdc
    eng.find_mmdc = lambda: None
    try:
        check("no mmdc: the message says how to install it", "npm install -g @mermaid-js/mermaid-cli" in mmd("ok.mmd", "flowchart TD\nA-->B"))
    finally:
        eng.find_mmdc = real_find

    # --- the agent tracks mermaid pictures like charts ---
    real_stream, real_run_tool = agent_mod.stream_chat, agent_mod.run_tool
    turn = {"n": 0}

    def stream(messages, **kwargs):
        turn["n"] += 1
        if turn["n"] == 1:
            yield {"type": "tool_calls", "calls": [{"id": "c1", "name": "mermaid", "arguments": json.dumps({"file": "diagrams/s.mmd"})}]}
        else:
            yield {"type": "content", "text": "done"}

    agent_mod.stream_chat = stream
    agent_mod.run_tool = lambda name, args, w: "OK: saved diagrams/s.png (4 KB). Show it ..."
    try:
        agent = Agent(Workspace(Path(tmp) / "ws2"))
        agent.run("draw it")
        check("the agent remembers mermaid pictures for the artifact list and auto-display",
              agent.charts_made == ["diagrams/s.png"] and agent.touched == ["diagrams/s.png"])
    finally:
        agent_mod.stream_chat, agent_mod.run_tool = real_stream, real_run_tool

# --- REAL Graphviz (only if installed) ---
if eng.find_dot():
    img = Image.open(__import__("io").BytesIO(eng.graphviz_png(small)))
    check("real Graphviz draws a PNG", img.width > 50 and img.height > 50)
    cycle = spec_of([{"id": "a"}, {"id": "b"}, {"id": "c", "shape": "diamond", "label": "ok? $5"}],
                    [("a", "b"), ("b", "c"), ("c", "a")], title="Loop")
    check("real Graphviz handles a cycle, shapes and a title", eng.graphviz_png(cycle).startswith(PNG))
    check("real Graphviz survives an injection attempt", eng.graphviz_png(spec).startswith(PNG))
else:
    print("SKIP  real Graphviz tests (dot not found)")

# --- REAL Mermaid (only if installed; the first render can take up to a minute) ---
if eng.find_mmdc():
    check("real Mermaid draws a sequence diagram", eng.mermaid_png("sequenceDiagram\n  Alice->>Bob: Hello\n  Bob-->>Alice: Hi").startswith(PNG))
    check("real Mermaid draws a flowchart with <br/>", eng.mermaid_png("flowchart LR\n  A[one<br/>two] --> B").startswith(PNG))
    message = error_of(eng.mermaid_png, "flowchart TD\n  A --> --> B ((")
    check("a Mermaid syntax error comes back as a readable message", "could not draw" in message)
else:
    print("SKIP  real Mermaid tests (mmdc not found)")

print("\nAll good.")