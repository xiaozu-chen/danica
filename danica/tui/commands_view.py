"""
commands_view.py - /open (split-screen viewer), /copy, /close and /save (alias /download).
Imported at the bottom of commands.py: registering a command name again replaces the old handler,
so this file overrides the earlier /open and /save without editing them.
"""

from pathlib import Path

from danica.tui.commands import DESCRIPTIONS, command

DESCRIPTIONS.update({
    "/open": "view a file (split screen) from the last request",
    "/copy": "copy a text file's contents to the clipboard",
    "/close": "hide the file viewer",
    "/download": "same as /save",
})


@command("/open", "/save", "/download")
def _open_save(app, args):
    """/open <n>      text and code files open in the split-screen viewer (read-only, highlighted);
                      anything else (pictures, .pptx, .docx...) goes to its default app as before
       /open <n> ext  force the default app / Explorer
       /save <n> [folder] (alias /download)  copy a file out, default Downloads"""
    from danica.artifacts import (
        ArtifactError,
        downloads_dir,
        open_artifact,
        pick,
        read_text,
        save_copy,
        tidy_for_view,
        view_mode,
    )
    from danica.tui.viewer_widgets import view_body
    parts = args.split(maxsplit=1)               # the number, then the rest (a folder path, or "ext")
    arg = parts[0] if parts else ""
    rest = parts[1] if len(parts) > 1 else ""
    try:
        item = pick(arg, app.artifacts)
        if app.current_command == "/open":
            if rest.strip().lower() != "ext":
                try:                              # text first: the viewer
                    text, cut = read_text(item.path)
                    shown, left_out = tidy_for_view(text)
                    mode = view_mode(shown)
                    # The slow part (syntax colours) is built HERE, on the worker thread, so the screen stays alive.
                    # "too_large" builds nothing: the pane just says so (viewer_widgets.show_file).
                    body = None if mode == "too_large" else view_body(shown, item.path, mode)
                    app._call(
                        app.view_artifact, item.rel, item.path, shown, cut, left_out, body
                    )
                    app.audit.append("artifact_view", path=item.rel, mode=mode)
                    return
                except ArtifactError:
                    pass                          # not text: fall through to the default app
            app.ui.log("ok", open_artifact(item))
            app.audit.append("artifact_open", path=item.rel)
        else:
            dest = Path(rest.strip().strip('"')) if rest.strip() else downloads_dir()
            saved = save_copy(item.path, dest)
            app.audit.append("artifact_save", path=item.rel, to=str(saved.parent))
            app.ui.log("ok", f"saved a copy to {saved}")
    except ArtifactError as e:
        app.ui.log("error", f"{e}")


@command("/copy")
def _copy(app, args):
    """/copy <n>: put a text file's contents on the clipboard (code, markdown, csv...)."""
    from danica.artifacts import ArtifactError, copy_contents, pick
    try:
        item = pick(args.split()[0] if args.split() else "", app.artifacts)
        chars = copy_contents(item)
        app.audit.append("artifact_copy", path=item.rel, chars=chars)
        app.ui.log("ok", f"copied {item.rel} ({chars:,} characters) to the clipboard")
    except ArtifactError as e:
        app.ui.log("error", f"{e}")


@command("/close")
def _close(app, args):
    """Hide the split-screen file viewer."""
    app._call(app.close_viewer)
