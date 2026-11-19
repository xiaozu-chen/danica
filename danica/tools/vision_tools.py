"""
vision_tools.py - view_image: lets the model LOOK at a picture file (a screenshot of a page it built,
a chart it made, a picture you put in the workspace).

The tool itself only checks the file and queues it. The agent loop then attaches the picture to the
next model call as a harness message (see vision.py), because a tool RESULT can only carry text.
"""

from danica.secret_scan import is_sensitive_path
from danica.tools.registry import tool
from danica.vision import VisionError, available, load_data_uri
from danica.workspace import Workspace

MAX_PER_CALL = 2            # pictures queued by one model step (each costs context)


@tool(
    "view_image",
    "Look at a picture file (png, jpg, webp, gif) in the workspace: a screenshot of a page or UI you built, a "
    "chart or diagram you made, or a picture the user mentioned. The picture is shown to you on your next step, "
    "then you can describe it, review it, or fix what you see. Call it once per picture; do not call it again "
    "for the same file unless you changed the file.",
    {"path": {"type": "string", "description": "Path of the picture, for example shots/home.png"}},
)
def view_image(ws: Workspace, path: str) -> str:
    """Look at a picture file (png, jpg, webp, gif) in the workspace: a screenshot
        of a page or UI you built, a chart or diagram you made, or a picture the
        user mentioned.
    """
    if not available():
        return ("ERROR: this model cannot see pictures right now (the server has no vision projector). "
                "Tell the user to start llama-server with --mmproj, or work without looking at the picture.")
    p = ws.resolve(path)
    if is_sensitive_path(ws.rel(p)):
        return "ERROR: that file is blocked: it looks like a credentials file"
    try:
        load_data_uri(p)               # really decode it now: a corrupt file is an error NOW, not a surprise later
    except VisionError as e:
        return f"ERROR: {e}"
    queue = getattr(ws, "pending_images", None)
    if queue is None:
        queue = ws.pending_images = []
    shown = ws.display(p)
    if len(queue) >= MAX_PER_CALL:
        return f"ERROR: look at at most {MAX_PER_CALL} pictures per step; call view_image for '{shown}' on the next step"
    if shown not in queue:
        queue.append(shown)
    return f"OUTPUT: '{shown}' is attached to your next step. Look at it and continue; do not call view_image for it again."
