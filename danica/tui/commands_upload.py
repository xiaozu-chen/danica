"""
commands_upload.py - /upload: put your files into Danica's workspace.

    /upload                    opens the file dialog (pick one or several files)
    /upload <path> [<path>..]  copies those files (quotes are fine, drag a file into the terminal to paste its path)

Files go to <workspace>/uploads/. A .zip is unzipped into its own folder there (safely, see uploads.py).
Your originals are never changed. Danica is told about the new files with your next message.
(/attach does the same for a picture and also shows it to her right away.)

Imported at the bottom of commands.py, like commands_vision.py.
"""

import re
from pathlib import Path

from danica import uploads
from danica.tui.commands import DESCRIPTIONS, command

DESCRIPTIONS["/upload"] = "put files (or a .zip) into the workspace"


def parse_paths(text: str) -> list[str]:
    """Paths from what was typed: "C:\\my folder\\a.pdf" 'b.txt' c.zip  (quotes keep spaces; backslashes stay as typed)."""
    return [a or b or c for a, b, c in re.findall(r'"([^"]+)"|\'([^\']+)\'|(\S+)', text)]


@command("/upload")
def _upload(app, args):
    """/upload [path ...]   copies files into the workspace uploads/ folder; a .zip is unzipped."""
    names = parse_paths(args)
    try:
        if not names:
            app.ui.log("info", "opening the file dialog (it may open behind this window)...")
            names = [str(p) for p in uploads.pick_files()]
            if not names:
                app.ui.log("info", "cancelled: no file chosen.")
                return
    except uploads.UploadError as e:
        app.ui.log("error", str(e))
        return
    stored = []
    for name in names:
        path = Path(name).expanduser()
        if not path.is_absolute():
            path = app.ws.root / path                                   # relative paths start at the workspace
        try:
            item = uploads.store_file(app.ws.root, path)
        except uploads.UploadError as e:
            hint = "  (a path with spaces needs quotes)" if " " not in name and not path.exists() and len(names) > 1 else ""
            app.ui.log("error", f"{path.name}: {e}{hint}")
            continue
        stored.append(item)
        app.ui.log("ok", uploads.describe(item))
        if item.kind == "folder":
            app.ui.log("info", "  top level: " + ", ".join(item.sample))
        for note in item.notes:
            app.ui.log("warning", "  " + note)
    if not stored:
        return
    app.agent.upload_notes.append(uploads.model_note(stored))          # the model hears about them with the next request
    app.audit.append("upload", files=[s.rel for s in stored], bytes=sum(s.size for s in stored))
    app.ui.log("info", "Danica will know about these files with your next message.")
