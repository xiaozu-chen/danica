"""
imageview.py - Showing PNG/JPEG files in the terminal.

    sixel  draws the picture inline (Windows Terminal 1.22+ understands Sixel)
    link   prints a clickable file:// link (ctrl+click)
    off    prints only the path
Sixel is encoded here in pure Python from a Pillow image (256 colours).

Files come from the MODEL's <present> tag, so check_image() is strict: the path must be inside
the workspace, the file must really be a PNG or JPEG (checked by its first bytes), and it must
be small enough. HONEST LIMITS: Sixel pictures can disappear when the window is resized or
scrollback is cleared; colours are reduced to 256.
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw
from rich.console import Console
from rich.style import Style
from rich.text import Text

from danica.config import CONFIG
from danica.workspace import PathJailError, Workspace

MAX_BYTES = 5_000_000          # file size limit
MAX_PIXELS = 25_000_000        # checked from the header, BEFORE the image is decoded
MAX_W, MAX_H = 960, 720        # bigger pictures are scaled down to fit (never scaled up)
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"


class ImageError(Exception):
    """A problem whose message is safe to show the user."""


def mode() -> str:
    """How images are shown: sixel, link or off (anything else in config counts as link)."""
    value = str(CONFIG.get("display", {}).get("images", "link")).lower()
    return value if value in ("sixel", "link", "off") else "link"


def _write_raw(text: str) -> None:
    """Send raw bytes (the Sixel data) straight to the terminal. Tests replace this."""
    sys.stdout.write(text)
    sys.stdout.flush()


# --------------------------------------------------------------- validation

def check_image(ws: Workspace, rel: str) -> Path:
    """The real path of an image the model asked to show, or ImageError."""
    try:
        p = ws.resolve(rel)               # raises if it leaves the workspace (symlinks included)
    except PathJailError:
        raise ImageError("that path is outside the workspace")
    if p.suffix.lower() not in (".png", ".jpg", ".jpeg"):
        raise ImageError("only .png and .jpg images can be shown")
    if not p.is_file():
        raise ImageError("no such file")
    if p.stat().st_size > MAX_BYTES:
        raise ImageError(f"the file is too large (limit {MAX_BYTES // 1_000_000} MB)")
    try:
        with open(p, "rb") as f:
            head = f.read(8)
    except OSError:
        raise ImageError("could not read the file")
    if not (head.startswith(PNG_MAGIC) or head.startswith(JPEG_MAGIC)):
        raise ImageError("that is not a real PNG or JPEG file")
    return p


def load_image(path: Path) -> Image.Image:
    """Open, flatten onto white, and shrink to fit the terminal."""
    try:
        img = Image.open(path)
        if img.width * img.height > MAX_PIXELS:       # header only: nothing is decoded yet
            raise ImageError("the image has too many pixels")
        img.load()
    except ImageError:
        raise
    except (OSError, ValueError, Image.DecompressionBombError):
        raise ImageError("could not read the image")
    if img.mode != "RGB":
        rgba = img.convert("RGBA")                    # transparent areas become white
        flat = Image.new("RGB", rgba.size, "white")
        flat.paste(rgba, mask=rgba.getchannel("A"))
        img = flat
    img.thumbnail((MAX_W, MAX_H), Image.Resampling.LANCZOS)
    return img


# ------------------------------------------------------------------- Sixel

def _rle(mask: bytearray) -> str:
    """One colour's row of sixel characters, with runs written as !count+char."""
    out, i, n = [], 0, len(mask)
    while i < n:
        value, j = mask[i], i + 1
        while j < n and mask[j] == value:
            j += 1
        ch, run = chr(63 + value), j - i
        out.append(f"!{run}{ch}" if run > 3 else ch * run)
        i = j
    return "".join(out)


def encode_sixel(img: Image.Image) -> str:
    """A Pillow image as one Sixel escape sequence.
    Format: ESC P q "1;1;W;H  (square pixels), then palette lines  #n;2;r;g;b  (percent),
    then bands of 6 pixel rows. In each band every colour has its own pass:  #n<chars>$
    where each char (63 + 6 bits) says which of the 6 pixels in that column use the colour."""
    q = img.convert("RGB").quantize(colors=256, method=Image.Quantize.MEDIANCUT,
                                    dither=Image.Dither.NONE)
    w, h = q.size
    pixels = q.tobytes()                  # one palette index per pixel
    palette = q.getpalette()
    out = [f'\x1bPq"1;1;{w};{h}']
    for c in sorted(set(pixels)):
        r, g, b = palette[3 * c:3 * c + 3]
        out.append(f"#{c};2;{round(r * 100 / 255)};{round(g * 100 / 255)};{round(b * 100 / 255)}")

    for top in range(0, h, 6):
        masks: dict[int, bytearray] = {}
        for k in range(min(6, h - top)):
            row = pixels[(top + k) * w:(top + k + 1) * w]
            bit = 1 << k
            for x, c in enumerate(row):
                m = masks.get(c)
                if m is None:
                    m = masks[c] = bytearray(w)
                m[x] |= bit
        out.append("$".join(f"#{c}{_rle(m)}" for c, m in masks.items()))
        out.append("-")                   # next band
    out.append("\x1b\\")
    return "".join(out)


# ----------------------------------------------------------------- showing

def display(
    img: Image.Image, label: str, console: Console, uri: str | None = None
) -> None:
    """Show a picture according to the display mode. Never raises."""
    how = mode()
    if how == "sixel":
        try:
            _write_raw(encode_sixel(img) + "\n")
        except Exception as e:            # a picture problem must never break the session
            console.print(f"(could not draw the image: {type(e).__name__})", style="yellow", markup=False)
    elif how == "link" and not uri:
        console.print('(images are in link mode: set [display] images = "sixel" in config.toml, '
                      "which needs Windows Terminal 1.22 or newer)", style="dim", markup=False)

    if uri and how != "off":
        console.print(Text(f"{label}  (ctrl+click to open)", style=Style(link=uri, dim=True)))
    else:
        console.print(label, style="dim", markup=False, highlight=False)


def show_image(path: Path, label: str, console: Console) -> None:
    """Load a validated image file and display it."""
    try:
        img = load_image(path)
    except ImageError as e:
        console.print(f"(could not show {label}: {e})", style="yellow", markup=False)
        return
    display(img, label, console, path.as_uri())


def test_image() -> Image.Image:
    """A framed picture with six coloured bars and a grey gradient, for /imagetest."""
    img = Image.new("RGB", (420, 160), "white")
    d = ImageDraw.Draw(img)
    for i, colour in enumerate(["#e41a1c", "#377eb8", "#4daf4a", "#984ea3", "#ff7f00", "#111111"]):
        x0 = 10 + i * 67
        d.rectangle([x0, 10, x0 + 60, 90], fill=colour)
    for x in range(400):
        shade = int(255 * x / 399)
        d.line([(10 + x, 105), (10 + x, 145)], fill=(shade, shade, shade))
    d.rectangle([0, 0, 419, 159], outline="black")
    return img
