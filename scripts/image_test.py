"""
image_test.py - Checks the <present> tag filter, the Sixel encoder (by DECODING its output),
image validation, the display modes, the CLI printer and chart tracking in the agent.
No model needed. Run from the project root:  python -m scripts.image_test
"""

import io
import json
import tempfile
from pathlib import Path

from PIL import Image
from rich.console import Console

import danica.agent as agent_mod
import danica.cli as cli
import danica.imageview as iv
from danica.agent import Agent
from danica.config import CONFIG
from danica.present import PresentFilter
from danica.workspace import Workspace


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


# ------------------------------------------------------------ the tag filter
def run_filter(chunks: list[str]) -> tuple[str, list[str]]:
    f, events = PresentFilter(), []
    for chunk in chunks:
        events += f.feed(chunk)
    events += f.flush()
    return ("".join(v for k, v in events if k == "text"), [v for k, v in events if k == "present"])


TAG = '<present file="charts/a.png"/>'
check("a tag is removed and reported", run_filter([f"Look: {TAG} done"]) == ("Look:  done", ["charts/a.png"]))
check("a tag split into single characters works", run_filter(list(f"Look: {TAG} done")) == ("Look:  done", ["charts/a.png"]))
check("two tags", run_filter([f"{TAG} and {TAG}"])[1] == ["charts/a.png", "charts/a.png"])
check("a tag without the slash works", run_filter(['<present file="x.png">'])[1] == ["x.png"])
check("paths with spaces work", run_filter(['<present file="my charts/a b.png"/>'])[1] == ["my charts/a b.png"])
check("other angle brackets pass through unchanged",
      run_filter(["a < b and <div>x</div> <p"]) == ("a < b and <div>x</div> <p", []))
check("an unfinished tag at the end comes back as text", run_filter(['text <present file="x']) == ('text <present file="x', []))
check("a wrong-shaped tag is not swallowed", run_filter(['<present src="x"/>']) == ('<present src="x"/>', []))
junk = "<present " + "x" * 400 + " after"
check("a never-ending fake tag is not swallowed", run_filter([junk]) == (junk, []))

# ------------------------------------------- the Sixel encoder, checked by decoding
def decode_sixel(seq: str) -> dict[tuple[int, int], tuple[int, int, int]]:
    """A tiny Sixel decoder: returns {(x, y): (r, g, b)} for every pixel that was drawn."""
    body = seq[seq.index("q") + 1: seq.rindex("\x1b\\")]
    palette, pixels = {}, {}
    x = y = 0
    colour = None
    i, n = 0, len(body)

    def number(pos: int) -> tuple[int | None, int]:
        j = pos
        while j < n and body[j].isdigit():
            j += 1
        return (int(body[pos:j]) if j > pos else None), j

    def put(bits: int) -> None:
        nonlocal x
        for k in range(6):
            if bits >> k & 1:
                pixels[(x, y + k)] = palette[colour]
        x += 1

    while i < n:
        ch = body[i]
        if ch == '"':                                   # raster attributes: skip
            i += 1
            while i < n and (body[i].isdigit() or body[i] == ";"):
                i += 1
        elif ch == "#":
            num, i = number(i + 1)
            if i < n and body[i] == ";":                # a palette definition: ;2;r;g;b
                parts = []
                while i < n and body[i] == ";":
                    value, i = number(i + 1)
                    parts.append(value)
                palette[num] = tuple(round(p * 255 / 100) for p in parts[1:4])
            colour = num
        elif ch == "!":
            count, i = number(i + 1)
            bits = ord(body[i]) - 63
            i += 1
            for _ in range(count):
                put(bits)
        elif ch == "$":
            x, i = 0, i + 1
        elif ch == "-":
            x, y, i = 0, y + 6, i + 1
        else:
            put(ord(ch) - 63)
            i += 1
    return pixels


img = Image.new("RGB", (20, 14), (255, 0, 0))                     # red, then a blue right half, then a green bottom
for xx in range(10, 20):
    for yy in range(14):
        img.putpixel((xx, yy), (0, 0, 255))
for xx in range(20):
    for yy in range(9, 14):
        img.putpixel((xx, yy), (0, 160, 0))
seq = iv.encode_sixel(img)
decoded = decode_sixel(seq)
check("the sequence has the right envelope", seq.startswith('\x1bPq"1;1;20;14') and seq.endswith("\x1b\\"))
check("14 rows are 3 bands", seq.count("-") == 3)
check("runs are compressed", "!" in seq)
check("every pixel is drawn", len(decoded) == 20 * 14)
check("decoding rebuilds the same picture (within rounding)",
      all(max(abs(a - b) for a, b in zip(decoded[(xx, yy)], img.getpixel((xx, yy)))) <= 3
          for xx in range(20) for yy in range(14)))
check("the test image encodes", iv.encode_sixel(iv.test_image()).startswith("\x1bPq"))

# --------------------------------------------------------- image validation
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    tmp = Path(tmp)
    ws = Workspace(tmp / "ws")
    (ws.root / "charts").mkdir()
    Image.new("RGB", (30, 20), "red").save(ws.root / "charts" / "a.png")
    Image.new("RGB", (30, 20), "blue").save(ws.root / "charts" / "b.png")
    (ws.root / "fake.png").write_text("this is text, not a picture", encoding="utf-8")
    (ws.root / "notes.txt").write_text("x", encoding="utf-8")

    def refused(rel: str) -> str:
        try:
            iv.check_image(ws, rel)
            return ""
        except iv.ImageError as e:
            return str(e)

    check("a real PNG is accepted", iv.check_image(ws, "charts/a.png") == (ws.root / "charts" / "a.png").resolve())
    check("a missing file is refused", "no such file" in refused("charts/zzz.png"))
    check("a path outside the workspace is refused", "outside" in refused("..\\..\\x.png"))
    check("a text file pretending to be a PNG is refused", "not a real" in refused("fake.png"))
    check("other file types are refused", "only .png" in refused("notes.txt"))
    iv.MAX_BYTES = 98
    try:
        check("a huge file is refused", "too large" in refused("charts/a.png"))
    finally:
        iv.MAX_BYTES = 5_000_000
    iv.MAX_PIXELS = 100
    try:
        try:
            iv.load_image(ws.root / "charts" / "a.png")          # 30 x 20 = 600 pixels
            pixel_error = ""
        except iv.ImageError as e:
            pixel_error = str(e)
        check("too many pixels is refused before decoding", "too many pixels" in pixel_error)
    finally:
        iv.MAX_PIXELS = 25_000_000

    Image.new("RGBA", (10, 10), (0, 0, 0, 0)).save(ws.root / "charts" / "clear.png")
    check("transparent areas become white", iv.load_image(ws.root / "charts" / "clear.png").getpixel((5, 5)) == (255, 255, 255))
    Image.new("RGB", (3000, 2000), "red").save(ws.root / "charts" / "big.png")
    loaded = iv.load_image(ws.root / "charts" / "big.png")
    check("big pictures are scaled down to fit", loaded.width <= iv.MAX_W and loaded.height <= iv.MAX_H)

    # ------------------------------------------------------- display modes
    def shown(mode_value: str, encoder=None):
        buf = io.StringIO()
        con = Console(file=buf, width=100, force_terminal=False)
        raw: list[str] = []
        saved = (iv._write_raw, iv.mode, iv.encode_sixel)
        iv._write_raw, iv.mode = raw.append, (lambda: mode_value)
        if encoder:
            iv.encode_sixel = encoder
        try:
            iv.display(Image.new("RGB", (30, 20), "red"), "charts/a.png", con, "file:///x/a.png")
        finally:
            iv._write_raw, iv.mode, iv.encode_sixel = saved
        return buf.getvalue(), raw

    text, raw = shown("sixel")
    check("sixel mode writes the picture and a caption", len(raw) == 1 and raw[0].startswith("\x1bPq") and "charts/a.png" in text)
    text, raw = shown("link")
    check("link mode writes no picture, only the caption", raw == [] and "charts/a.png" in text)
    text, raw = shown("off")
    check("off mode prints only the label", raw == [] and "charts/a.png" in text)

    def boom(_img):
        raise RuntimeError("x")

    text, raw = shown("sixel", encoder=boom)
    check("an encoder failure is a notice, not a crash", "could not draw" in text and raw == [])
    CONFIG["display"]["images"] = "weird"
    try:
        check("an unknown mode counts as link", iv.mode() == "link")
    finally:
        CONFIG["display"]["images"] = "link"

    # -------------------------------------------------------- the CLI printer
    real_console, real_show = cli.console, cli.show_image
    buf = io.StringIO()
    cli.console = Console(file=buf, width=100, force_terminal=False)
    drawn: list[str] = []
    cli.show_image = lambda path, label, console: drawn.append(label)
    try:
        printer = cli.EventPrinter(lambda: ws)
        printer.begin_request()
        for piece in ["Here it is: <pre", 'sent file="charts/a.png"/> and that is all.']:
            printer("content", piece)
        printer.finish()
        out = buf.getvalue()
        check("the tag is not printed but the text around it is",
              "<present" not in out and "Here it is:" in out and "that is all." in out)
        check("the picture was shown once", drawn == ["charts/a.png"])

        printer.present_pending(["charts/a.png", "charts/b.png"])
        check("pending shows only pictures not shown yet", drawn == ["charts/a.png", "charts/b.png"])

        printer.begin_request()
        printer("content", '<present file="../../x.png"/>')
        printer.finish()
        check("an outside path is a notice, not a crash", "could not show" in buf.getvalue() and len(drawn) == 2)

        printer("content", 'half <present file="c')
        printer("tool_call", ("read_file", {"path": "x"}))
        check("held-back text is flushed before the next event", 'half <present file="c' in buf.getvalue())
    finally:
        cli.console, cli.show_image = real_console, real_show

    # ----------------------------------------------- the agent tracks charts
    real_stream, real_run_tool = agent_mod.stream_chat, agent_mod.run_tool
    turn = {"n": 0}

    def stream(messages, **kwargs):
        turn["n"] += 1
        if turn["n"] == 1:
            yield {"type": "tool_calls", "calls": [{"id": "c1", "name": "chart",
                                                    "arguments": json.dumps({"file": "charts/a.json"})}]}
        else:
            yield {"type": "content", "text": "done"}

    agent_mod.stream_chat = stream
    agent_mod.run_tool = lambda name, args, w: "OK: saved charts/a.png (3 KB). Show it to the user ..."
    try:
        agent = Agent(Workspace(tmp / "ws2"))
        agent.run("chart it")
        check("the agent remembers charts made during the request", agent.charts_made == ["charts/a.png"])
        turn["n"] = 99                                   # this request makes no tool call
        agent.run("thanks")
        check("the list resets for every request", agent.charts_made == [])
    finally:
        agent_mod.stream_chat, agent_mod.run_tool = real_stream, real_run_tool

print("\nAll good.")