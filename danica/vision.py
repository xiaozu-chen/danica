"""
vision.py - Letting the model SEE pictures (your screenshots, charts Danica made, UI it built).

HOW IT WORKS
  * A message that carries a picture is stored as plain text plus an "images" list of FILE PATHS:
        {"role": "user", "content": "what is wrong with this layout?", "images": ["shots/home.png"]}
    The history, the session transcript, pruning and compaction therefore never see picture data
    (nothing huge is stored, nothing needs to learn about lists or base64).
  * Only at the moment of a model call does prepare() turn those paths into real pictures
    ("image_url" data URIs). Pictures are shrunk first (long side <= MAX_SIDE) to save context.
  * Only the newest KEEP_IMAGES pictures are sent; older ones become a one-line text note.
    (When the oldest picture drops out, the server re-reads from that point: a one-off cost.)
  * If the server has no vision (it was started without --mmproj), pictures are never sent. They
    are replaced by a note, so the model is never confused and the server never errors.

START THE SERVER WITH VISION, for example:
    llama-server -m model-Q4_K_M.gguf --mmproj mmproj-F16.gguf -c 65536 --jinja --port 8080
"""

from __future__ import annotations

import base64
import io
from pathlib import Path

from danica.config import CONFIG
from danica.workspace import Workspace

MAX_SIDE = 1024                # longest side after shrinking (pixels)
MAX_FILE_BYTES = 12_000_000    # refuse bigger files outright
KEEP_IMAGES = 2                # how many pictures the model can still see at once
IMAGE_TOKENS = 700             # rough context cost of one picture (only used for estimates)
SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

# What the server can do, filled in by refresh(). "known" is False until the first successful probe.
STATE = {"vision": False, "known": False}


class VisionError(Exception):
    """A picture problem worded so it can be shown to the user or the model as is."""


# ------------------------------------------------------------------ does the server see?

def probe_vision(server_url: str, timeout: float = 2.0) -> bool:
    """True if llama-server was started with a vision projector (--mmproj). Never raises."""
    import requests                       # imported here: slow to load, and only this check needs it
    try:
        data = requests.get(f"{server_url}/props", timeout=timeout).json()
    except (requests.RequestException, ValueError):
        return False
    mods = data.get("modalities") if isinstance(data, dict) else None
    return bool(isinstance(mods, dict) and mods.get("vision"))


def refresh() -> bool:
    """Ask the server again (call after connecting / reconnecting). Returns the new answer."""
    STATE["vision"] = probe_vision(CONFIG["server"]["base_url"])
    STATE["known"] = True
    return STATE["vision"]


def available() -> bool:
    """Return whether the configured model server supports images."""
    return bool(STATE["vision"])


# ------------------------------------------------------------------ loading a picture

def check_file(path: Path) -> None:
    """Raise VisionError unless path is a real, reasonably small picture (checked by content, not by name)."""
    if path.suffix.lower() not in SUFFIXES:
        raise VisionError(f"'{path.name}' is not a picture (png, jpg, webp, gif, bmp)")
    if not path.is_file():
        raise VisionError(f"'{path.name}' is not a file")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise VisionError(f"'{path.name}' is too big ({path.stat().st_size // 1_000_000} MB, limit {MAX_FILE_BYTES // 1_000_000} MB)")


def load_data_uri(path: Path, max_side: int | None = None) -> tuple[str, int, int]:
    """(data URI, width, height) of the shrunk picture. Raises VisionError."""
    check_file(path)
    from PIL import Image

    limit = max_side or int(CONFIG.get("vision", {}).get("max_side", MAX_SIDE))
    try:
        with Image.open(path) as im:
            im.load()
            if im.mode in ("RGBA", "LA", "P"):                 # flatten transparency onto white
                im = im.convert("RGBA")
                flat = Image.new("RGB", im.size, "white")
                flat.paste(im, mask=im.split()[-1])
                im = flat
            else:
                im = im.convert("RGB")
            if max(im.size) > limit:
                im.thumbnail((limit, limit), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "PNG", optimize=True)                  # PNG keeps small UI text crisp
            if buf.tell() > 900_000:                            # a photo-like picture: JPEG is far smaller
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=88)
                mime = "image/jpeg"
            else:
                mime = "image/png"
            width, height = im.size
    except (OSError, ValueError, Image.DecompressionBombError) as e:
        raise VisionError(f"'{path.name}' could not be read as a picture ({type(e).__name__})") from None
    return f"data:{mime};base64," + base64.b64encode(buf.getvalue()).decode("ascii"), width, height


# ------------------------------------------------------------------ at the moment of a model call

def _count_images(messages: list[dict]) -> int:
    return sum(len(m.get("images") or []) for m in messages)


def prepare(messages: list[dict], root: Path | None = None) -> list[dict]:
    """The messages as the SERVER should see them. Messages with an "images" list become
    multi-part messages (text + pictures); the stored history is never changed.
    No pictures anywhere = the very same list comes back (nothing copied)."""
    if not _count_images(messages):
        return messages
    out: list[dict] = []
    left_to_show = KEEP_IMAGES if available() else 0
    # Walk newest -> oldest so the NEWEST pictures are the ones kept
    shown_for: dict[int, set[int]] = {}
    for i in range(len(messages) - 1, -1, -1):
        imgs = messages[i].get("images") or []
        keep = set()
        for k in range(len(imgs) - 1, -1, -1):
            if left_to_show > 0:
                keep.add(k)
                left_to_show -= 1
        shown_for[i] = keep
    for i, m in enumerate(messages):
        imgs = m.get("images")
        if not imgs:
            out.append(m)
            continue
        text = str(m.get("content") or "")
        parts: list[dict] = []
        notes: list[str] = []
        labels: list[str] = []
        for k, rel in enumerate(imgs):
            if k in shown_for[i]:
                try:
                    path = Path(rel)
                    if not path.is_absolute() and root is not None:
                        path = root / rel
                    uri, _, _ = load_data_uri(path)
                    parts.append({"type": "image_url", "image_url": {"url": uri}})
                    # Say so in words: a small model otherwise goes looking for the file with its tools
                    where = f" (saved in the workspace as {rel})" if not Path(rel).is_absolute() else ""
                    labels.append(f"[attached picture: {path.name}{where}. It is shown below; you can already see it, so do not "
                                  "search for it or call view_image for it now.]")
                    continue
                except VisionError as e:
                    notes.append(f"[picture {rel} could not be loaded: {e}]")
                    continue
            if not available():
                notes.append(f"[picture {rel}: not shown, this server has no vision (start llama-server with --mmproj)]")
            else:
                again = f" To look at it again, call view_image with path {rel}." if not Path(rel).is_absolute() else ""
                notes.append(f"[picture {rel}: shown earlier, no longer attached.{again}]")
        clean = {k: v for k, v in m.items() if k != "images"}
        clean["content"] = ([{"type": "text", "text": "\n".join([text] + labels + notes).strip()}] + parts) if parts \
            else "\n".join([text] + notes).strip()
        out.append(clean)
    return out


def extra_tokens(messages: list[dict]) -> int:
    """Rough extra context for pictures (used by the token estimate, replaced by exact counts later)."""
    return _count_images(messages) * IMAGE_TOKENS if available() else 0


def take_pending(ws: Workspace) -> list[str]:
    """The pictures view_image queued during this step (and empty the queue)."""
    queue = list(getattr(ws, "pending_images", None) or [])
    ws.pending_images = []
    return queue
