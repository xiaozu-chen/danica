"""
commands_vision.py - /attach, /detach and /vision.

    /attach [picture]   choose or name a picture to send with your NEXT message (drag a file into the terminal to paste its path).
                        A copy is kept in the workspace (uploads/), so Danica can look at it again with view_image.
    /detach             forget the pictures you attached
    /vision             does the server see pictures? (and how to turn it on)

Imported at the bottom of commands.py, like commands_view.py.
"""

from pathlib import Path

from danica import uploads, vision
from danica.tui.commands import DESCRIPTIONS, command

DESCRIPTIONS.update({
    "/attach": "choose or name a picture to send with your next message",
    "/detach": "forget attached pictures",
    "/vision": "does the model see pictures?",
})

HOW_TO = ("Start llama-server with the vision projector, for example:\n"
          "  llama-server -m model.gguf --mmproj mmproj.gguf -c 65536 --jinja --port 8080\n"
          "then run /vision again.")


@command("/vision")
def _vision(app, args):
    """/vision   re-checks the server and says whether pictures can be sent."""
    if vision.refresh():
        app.ui.log("info", "vision is ON: Danica can look at pictures (/attach, and the view_image tool).")
    else:
        app.ui.log("warning", "vision is OFF: the server was started without a vision projector.\n" + HOW_TO)


@command("/attach")
def _attach(app, args):
    """/attach [path]   choose a picture or provide its path (png / jpg / webp / gif / bmp)."""
    raw = args.strip().strip('"').strip("'")
    if not raw:
        try:
            paths = uploads.pick_files(images_only=True)
        except uploads.UploadError as e:
            app.ui.log("error", str(e))
            return
        if not paths:
            app.ui.log("info", "cancelled: no picture chosen.")
            return
        raw = str(paths[0])
    if not vision.available() and not vision.refresh():
        app.ui.log("warning", "the server has no vision, so the picture would be ignored.\n" + HOW_TO)
        return
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = app.ws.root / path                    # relative paths start at the workspace
    try:
        vision.check_file(path)
        vision.load_data_uri(path)                  # a real test now, not a surprise on the next message
    except vision.VisionError as e:
        app.ui.log("error", str(e))
        return
    try:
        stored = uploads.store_picture(app.ws.root, path)       # a copy in the workspace (uploads/): she can look again later
    except uploads.UploadError as e:
        app.ui.log("error", str(e))
        return
    app.agent.next_images.append(stored)                        # the WORKSPACE path is what the history keeps
    app.ui.log("info", f"attached {path.name} ({len(app.agent.next_images)} waiting), kept in the workspace as {stored} "
                       "so Danica can look at it again later. Type your message; it goes with the next request.")


@command("/detach")
def _detach(app, args):
    """/detach   drops the pictures waiting for the next message."""
    n = len(app.agent.next_images)
    app.agent.next_images = []
    app.ui.log("info", f"removed {n} attached picture(s)." if n else "nothing was attached.")
