"""
phase7_exit.py - Phase 7 exit test: from ONE prompt, a cited report as a Word file plus a chart
you can open from the CLI.

Offline: a scripted fake model and a local test web page. Everything else is real (web_fetch with
taint tracking, the chart and document tools, citation checking, the shadow-git artifact list,
the CLI printer and its <present> filter).
Run from the project root:  python -m scripts.phase7_exit
"""

import io
import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from docx import Document
from rich.console import Console

import danica.agent as agent_mod
import danica._cli as _cli
import danica.seal as seal
import danica.tools.fetch as fetch
from danica.agent import Agent
from danica.artifacts import collect, is_openable
from danica.checkpoints import Checkpoints
from danica.docgen import is_danica_file
from danica.permissions import PermissionPolicy
from danica.tools import run_tool
from danica.workspace import Workspace


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


PNG = b"\x89PNG\r\n\x1a\n"
PAGE = (b"<html><head><title>Pelican Diet</title></head><body><h1>Diet</h1>"
        b"<p>Pelicans mostly eat fish such as herring and anchovies.</p>"
        b"<p>Some species also eat crustaceans.</p></body></html>")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)


script: list[dict] = []
counter = {"n": 0}


def call(name: str, **args) -> None:
    """Queue one tool call for the fake model."""
    counter["n"] += 1
    script.append({"type": "tool_calls", "calls": [{"id": f"c{counter['n']}", "name": name, "arguments": json.dumps(args)}]})


def say(text: str) -> None:
    """Queue a plain-text answer. It streams in 7-character pieces, so the <present> tag arrives split."""
    script.append({"type": "content", "text": text})


def fake_stream(messages, **kwargs):
    item = script.pop(0) if script else {"type": "content", "text": "done"}
    if item["type"] == "content":
        text = item["text"]
        for i in range(0, len(text), 7):
            yield {"type": "content", "text": text[i:i + 7]}
    else:
        yield item


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
fetch.TEST_ALLOW.add(("127.0.0.1", port))
url = f"http://127.0.0.1:{port}/diet"

real_stream, real_console, real_show = agent_mod.stream_chat, _cli.console, _cli.show_image
agent_mod.stream_chat = fake_stream
buf = io.StringIO()
_cli.console = Console(file=buf, width=100, force_terminal=False)
drawn: list[str] = []
_cli.show_image = lambda path, label, console: drawn.append(label)
try:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp = Path(tmp)
        seal.KEY_PATH = tmp / "seal.key"
        ws = Workspace(tmp / "ws")
        asked: list[str] = []
        printer = _cli.EventPrinter(lambda: ws)
        agent = Agent(ws, on_event=printer, policy=PermissionPolicy(ask=lambda *a: asked.append(a[0]) or "deny"),
                      checkpoints=Checkpoints(ws, tmp / "git"), max_steps=20)

        # ============ ONE prompt: research, chart, cited report, Word file ============
        call("web_fetch", url=url)
        call("write_file", path="charts/fish.json", content=json.dumps(
            {"type": "bar", "title": "Fish eaten (illustrative)", "labels": ["Herring", "Anchovy", "Mullet"],
             "series": [{"name": "Count", "values": [12, 9, 5]}]}))
        call("chart", file="charts/fish.json")
        call("write_file", path="reports/pelicans.md", content=(
            "# Pelican diet\n\nPelicans mostly eat fish [S1.1].\n\n![Fish eaten (illustrative)](charts/fish.png)\n"))
        call("document", source="reports/pelicans.md")
        say('Done. Here is the chart:\n<present file="charts/fish.png"/>\nThe report is in reports/pelicans.docx.')

        printer.begin_request()
        agent.run(f"Research what pelicans eat from {url}, chart it, and write me a cited report as a Word document.")
        printer.finish()
        printer.present_pending(agent.charts_made)
        found = collect(ws, agent.checkpoints, agent.request_snap, agent.touched, printer.presented)

        docx = ws.root / "reports" / "pelicans.docx"
        doc = Document(str(docx))
        texts = [p.text for p in doc.paragraphs]
        check("the Word file exists and is Danica's own", docx.exists() and is_danica_file(docx))
        check("it embeds the chart", len(doc.inline_shapes) == 1)
        check("it cites the page it read", any("[S1.1]" in t for t in texts))
        check("the harness added a Sources section with the real address", "Sources" in texts and any(url in t for t in texts))
        check("the chart is a real PNG on disk", (ws.root / "charts" / "fish.png").read_bytes().startswith(PNG))

        out = buf.getvalue()
        answer_start = out.find("Done. Here is the chart:")
        answer = out[answer_start:] if answer_start >= 0 else ""
        check("the <present> tag never reached the screen, though it arrived split",
              answer_start >= 0 and "<present" not in answer)
        check("the chart was shown in the terminal exactly once", drawn == ["charts/fish.png"])

        rels = {a.rel for a in found.items}
        check("the artifact list names the report and its source", {"reports/pelicans.docx", "reports/pelicans.md"} <= rels)
        check("it leaves out the chart picture (already shown) and its data file", not {"charts/fish.png", "charts/fish.json"} & rels)
        check("the report can be opened from the CLI", is_openable(docx))
        check("no permission prompt was needed", not asked)
        check("reading the web tainted the session (the defences were active)", ws.web is not None and ws.web.tainted)

        # ============ the same flow with a made-up citation must refuse ============
        before = docx.read_bytes()
        call("write_file", path="reports/pelicans.md", content="# Pelican diet\n\nA made-up claim [S9.9].\n")
        call("document", source="reports/pelicans.md")
        say("I could not build it.")
        printer.begin_request()
        agent.run("Rebuild the report.")
        printer.finish()
        last = [m["content"] for m in agent.messages if m["role"] == "tool"][-1]
        check("a citation that points to nothing refuses the build", last.startswith("ERROR") and "point to nothing" in last)
        check("the earlier report was not damaged", docx.read_bytes() == before)

        # ============ the rest of Phase 7: dashboard and slideshow from the same session ============
        (ws.root / "dashboards").mkdir()
        (ws.root / "dashboards" / "d.json").write_text(json.dumps({
            "title": "Pelican overview", "kpis": [{"label": "Species", "value": "8", "note": "+1"}],
            "panels": [{"type": "bar", "labels": ["a", "b"], "series": [{"values": [1, 2]}]},
                       {"type": "pie", "labels": ["x", "y"], "series": [{"values": [3, 1]}]}]}), encoding="utf-8")
        check("a dashboard is one PNG", run_tool("dashboard", {"file": "dashboards/d.json"}, ws).startswith("OK: saved dashboards/d.png"))
        (ws.root / "decks").mkdir()
        (ws.root / "decks" / "t.md").write_text(
            "# Pelicans\n\n## Diet\n\n- Fish [S1.1]\n\n## Chart\n\n![Fish](../charts/fish.png)\n", encoding="utf-8")
        run_tool("deck", {"source": "decks/t.md", "format": "html"}, ws)
        slides = ws.root / "decks" / "t.html"
        check("a slideshow is sealed and may be opened", seal.is_sealed_html(slides) and is_openable(slides))
        slides.write_text(slides.read_text(encoding="utf-8") + "<!-- edited -->", encoding="utf-8")
        check("an edited slideshow may not", not is_openable(slides))
finally:
    agent_mod.stream_chat, _cli.console, _cli.show_image = real_stream, real_console, real_show
    server.shutdown()
    fetch.TEST_ALLOW.clear()

print("\nAll good. Phase 7 exit test passed.")