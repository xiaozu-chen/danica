"""
diagram_engines.py - Graphviz and Mermaid, run as external programs.

SAFETY DESIGN
  * Graphviz: the model never writes DOT. to_dot() builds it from the validated nodes/edges spec
    and escapes every string, so a label can never add attributes (dot's "image=" reads local files).
  * Mermaid: the model writes Mermaid text, so validate_mermaid() allows only known diagram types
    and refuses directives that run or fetch things. mmdc renders in headless Chromium with
    securityLevel "strict" and HTML labels off.
  * Programs are found by find_program() (never the current directory), run WITHOUT a shell with a
    stripped environment, a timeout, and (by default) inside a Job Object that kills the whole
    process tree. Input goes through stdin or fixed file names in a temp folder: nothing the model
    wrote ever reaches a command line.
HONEST LIMITS: the Mermaid filter is a blocklist, not a proof; Chromium inside a Job Object may
fail on some machines ([diagrams] confine = false); output pictures are not editable.
"""

import io
import json
import os
import re
import subprocess
import tempfile
import textwrap
from pathlib import Path

from PIL import Image

from danica.charts import ChartError as DiagramError
from danica.config import CONFIG
from danica.diagrams import COLORS
from danica.diagrams import render as builtin_render
from danica.markdown_doc import strip_invisible
from danica.pathsearch import find_program
from danica.sandbox import Job, SandboxError
from danica.secret_scan import find_secrets

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MAX_PNG_BYTES = 8_000_000
MAX_PIXELS = 20_000_000
MAX_MERMAID_CHARS = 20_000
CREATE_NO_WINDOW = 0x08000000

# The ONLY environment variables a diagram program sees (no API keys, no tokens)
ENV_KEYS = ["SystemRoot", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA",
            "APPDATA", "PROGRAMFILES", "ProgramFiles(x86)", "ProgramW6432", "PROGRAMDATA", "SystemDrive",
            "HOMEDRIVE", "HOMEPATH", "USERNAME", "COMPUTERNAME", "NUMBER_OF_PROCESSORS",
            "PROCESSOR_ARCHITECTURE", "PUPPETEER_EXECUTABLE_PATH", "PUPPETEER_CACHE_DIR"]


def _env() -> dict[str, str]:
    return {k: os.environ[k] for k in ENV_KEYS if k in os.environ}


def _brief(err: str, limit: int = 300) -> str:
    """The useful lines of a program's error output, short enough to show the model."""
    lines = [l.strip() for l in err.splitlines() if l.strip()]
    keep = [l for l in lines if re.search(r"(?i)error|parse|syntax|expecting|warning", l)] or lines
    return " | ".join(keep[:3])[:limit]


# ------------------------------------------------------------ running programs

def run(cmd: list[str], cwd: str, stdin: bytes | None = None, timeout: int = 60,
        confine: bool | None = None) -> tuple[int, bytes, str]:
    """Run a program. Returns (exit code, stdout, stderr text). Raises DiagramError if it can't be
    started or runs past the timeout (the whole process tree is killed). No shell is ever used."""
    if confine is None:
        confine = bool(CONFIG["diagrams"]["confine"])
    kwargs = dict(cwd=cwd, env=_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                  stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                  creationflags=CREATE_NO_WINDOW)

    if confine:
        limits = CONFIG["sandbox"]       # a headless browser needs more room than an ordinary shell command
        try:
            job = Job(memory_mb=max(limits["memory_mb"], 3072), max_processes=max(limits["max_processes"], 128))
        except SandboxError as e:
            raise DiagramError(f"the sandbox could not start, so the program was NOT run: {e}")
        with job:
            try:
                proc = job.popen(cmd, **kwargs)
            except SandboxError as e:
                raise DiagramError(f"could not confine the program, so it was NOT run: {e}")
            except OSError as e:
                raise DiagramError(f"could not start the program: {e}")
            try:
                out, err = proc.communicate(stdin, timeout=timeout)
            except subprocess.TimeoutExpired:
                job.kill()               # the whole tree, browser children included
                proc.communicate()
                raise DiagramError(f"timed out after {timeout}s")
    else:
        try:
            proc = subprocess.Popen(cmd, **kwargs)
        except OSError as e:
            raise DiagramError(f"could not start the program: {e}")
        try:
            out, err = proc.communicate(stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
            proc.communicate()
            raise DiagramError(f"timed out after {timeout}s")
    return proc.returncode, out, err.decode("utf-8", errors="replace")


def check_png(data: bytes) -> None:
    """Refuse anything that is not a reasonable PNG (a program's output is not trusted blindly)."""
    if not data.startswith(PNG_MAGIC):
        raise DiagramError("the program did not produce a PNG picture")
    if len(data) > MAX_PNG_BYTES:
        raise DiagramError("the picture is too big: simplify the diagram")
    try:
        width, height = Image.open(io.BytesIO(data)).size          # reads the header only
    except Exception:
        raise DiagramError("the program's picture could not be read")
    if width * height > MAX_PIXELS:
        raise DiagramError("the picture has too many pixels: simplify the diagram")


# ----------------------------------------------------------------- finding them

def find_dot() -> str | None:
    configured = str(CONFIG["diagrams"]["dot_path"]).strip()
    if configured:
        return configured if Path(configured).is_file() else None
    found = find_program("dot")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if base:
            hits = sorted(Path(base).glob("Graphviz*/bin/dot.exe"), reverse=True)
            if hits:
                return str(hits[0])
    return None


def find_mmdc() -> str | None:
    configured = str(CONFIG["diagrams"]["mmdc_path"]).strip()
    if configured:
        return configured if Path(configured).is_file() else None
    return find_program("mmdc")


# --------------------------------------------------------------------- Graphviz

SHAPE_DOT = {"box": ("box", "filled"), "round": ("box", "rounded,filled"),
             "diamond": ("diamond", "filled"), "oval": ("ellipse", "filled")}


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _label(text: str, width: int = 22) -> str:
    """A quoted DOT string: raw text (the matplotlib "$" escape removed), wrapped, with every
    special character escaped. A label can never end its own quotes or add attributes."""
    raw = text.replace("\\$", "$")
    lines = textwrap.wrap(raw, width, break_long_words=False) or [""]
    return '"' + "\\n".join(_esc(l) for l in lines) + '"'          # \n is DOT's own line break


def to_dot(spec: dict) -> str:
    """DOT text for a VALIDATED spec (see diagrams.validate)."""
    rank = "TB" if spec["direction"] == "down" else "LR"
    graph = (f'  graph [rankdir={rank}, bgcolor="white", pad=0.3, nodesep=0.5, ranksep=0.7, '
             f'fontname="Arial", fontsize=18, labelloc="t"')
    if spec["title"]:
        graph += f", label={_label(spec['title'], 60)}"
    lines = ["digraph G {", graph + "];",
             '  node [fontname="Arial", fontsize=12, penwidth=1.6, margin="0.2,0.1"];',
             '  edge [fontname="Arial", fontsize=10, color="#444444", arrowsize=0.9];']
    for n in spec["nodes"]:
        shape, style = SHAPE_DOT[n["shape"]]
        fill, edge = COLORS[n["color"]]
        lines.append(f'  "{_esc(n["id"])}" [label={_label(n["label"])}, shape={shape}, style="{style}", '
                     f'fillcolor="{fill}", color="{edge}"];')
    for e in spec["edges"]:
        label = f' [label={_label(e["label"], 20)}]' if e["label"] else ""
        lines.append(f'  "{_esc(e["from"])}" -> "{_esc(e["to"])}"{label};')
    lines.append("}")
    return "\n".join(lines)


def graphviz_png(spec: dict) -> bytes:
    dot = find_dot()
    if not dot:
        raise DiagramError("Graphviz (dot) was not found. Install it, or set [diagrams] dot_path in config.toml")
    code, out, err = run([dot, "-Tpng", "-Gdpi=110"], tempfile.gettempdir(),
                         stdin=to_dot(spec).encode("utf-8"), timeout=30)
    if code != 0 or not out:
        raise DiagramError(f"Graphviz failed: {_brief(err) or 'exit code ' + str(code)}")
    check_png(out)
    return out


def render_diagram(spec: dict) -> tuple[bytes, str]:
    """(PNG bytes, a note for the model). Picks the engine from [diagrams] engine."""
    engine = str(CONFIG["diagrams"]["engine"]).lower()
    if engine == "builtin":
        return builtin_render(spec), ""
    try:
        return graphviz_png(spec), ""
    except DiagramError as e:
        if engine == "graphviz":
            raise                                   # the user asked for Graphviz specifically
        return builtin_render(spec), f"Graphviz was not used ({e}); drew it with the built-in engine"


# ---------------------------------------------------------------------- Mermaid

MERMAID_TYPES = ("flowchart", "graph", "sequenceDiagram", "classDiagram", "stateDiagram", "stateDiagram-v2",
                 "erDiagram", "gantt", "pie", "journey", "mindmap", "timeline", "gitGraph", "quadrantChart")
BAD_MERMAID = [
    (re.compile(r"%%\s*\{"), "init directives (%%{ ... }%%) are not allowed"),
    (re.compile(r"(?im)^\s*(click|callback|link|links)\b"), "click and link directives are not allowed"),
    (re.compile(r"(?i)javascript:|vbscript:|data:\w+/"), "script and data addresses are not allowed"),
]
MERMAID_CONFIG = {"securityLevel": "strict", "theme": "default", "htmlLabels": False,
                  "flowchart": {"htmlLabels": False}}


def validate_mermaid(source: str) -> str:
    """The cleaned Mermaid text, or DiagramError saying what to change."""
    text = strip_invisible(source).strip()
    if not text:
        raise DiagramError("the Mermaid file is empty")
    if len(text) > MAX_MERMAID_CHARS:
        raise DiagramError(f"the Mermaid text is too long (limit {MAX_MERMAID_CHARS:,} characters)")
    if find_secrets(text):
        raise DiagramError("refused: the text contains what looks like a secret (key, token or password)")
    for rx, message in BAD_MERMAID:
        if rx.search(text):
            raise DiagramError(message)
    stripped = re.sub(r"<<[^<>\n]*>>", "", text)                  # class-diagram stereotypes are fine
    for m in re.finditer(r"<\s*/?\s*([A-Za-z][\w:-]*)", stripped):
        if m.group(1).lower() != "br":
            raise DiagramError(f"HTML tags are not allowed (found <{m.group(1)}>); only <br/> works")
    first = next((l.strip() for l in text.split("\n") if l.strip() and not l.strip().startswith("%%")), "")
    kind = re.match(r"[A-Za-z][\w-]*", first)
    if not kind or kind.group(0) not in MERMAID_TYPES:
        raise DiagramError("the first line must be the diagram type, one of: " + ", ".join(MERMAID_TYPES)
                           + " (front matter is not supported)")
    return text


def mermaid_png(source: str) -> bytes:
    text = validate_mermaid(source)
    mmdc = find_mmdc()
    if not mmdc:
        raise DiagramError("Mermaid is not installed. Tell the user to run: npm install -g @mermaid-js/mermaid-cli "
                           "(needs Node.js), or set [diagrams] mmdc_path in config.toml")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        folder = Path(tmp)
        (folder / "in.mmd").write_text(text, encoding="utf-8")
        (folder / "config.json").write_text(json.dumps(MERMAID_CONFIG), encoding="utf-8")
        # Fixed relative file names: nothing the model wrote ever reaches the command line
        code, _, err = run([mmdc, "-i", "in.mmd", "-o", "out.png", "-c", "config.json", "-b", "white",
                            "-s", "2", "-q"], str(folder), timeout=120)
        out = folder / "out.png"
        if code != 0 or not out.is_file():
            raise DiagramError(f"Mermaid could not draw it: {_brief(err) or 'exit code ' + str(code)}. Check the syntax.")
        if out.stat().st_size > MAX_PNG_BYTES:
            raise DiagramError("the picture is too big: simplify the diagram")
        data = out.read_bytes()
    check_png(data)
    return data


# ----------------------------------------------------------------------- status

def status_lines() -> list[str]:
    cfg = CONFIG["diagrams"]
    lines = [f"diagram engine setting: {cfg['engine']} (auto = Graphviz if found, else the built-in drawing)",
             f"confinement (Job Object): {'on' if cfg['confine'] else 'off'}"]
    for label, finder, flag in (("Graphviz", find_dot, "-V"), ("Mermaid (mmdc)", find_mmdc, "--version")):
        path = finder()
        if not path:
            lines.append(f"{label}: not found")
            continue
        try:
            code, out, err = run([path, flag], tempfile.gettempdir(), timeout=60)
            version = (err.strip() or out.decode("utf-8", errors="replace").strip()).splitlines()[0][:70]
            lines.append(f"{label}: {path} ({version})")
        except DiagramError as e:
            lines.append(f"{label}: found at {path} but could not run it ({e})")
    return lines