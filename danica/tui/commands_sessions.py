"""
/sessions delete - remove saved sessions (the list itself is still /sessions, see commands.py).

    /sessions delete 3          row 3 of /sessions        /sessions delete 2-5       rows 2 to 5
    /sessions delete 20261102-013000    an id or a unique start of one
    /sessions delete empty      sessions with no requests  /sessions delete all       every session except this one

It asks first (starting on "No") and shows what goes: id, date, title, requests and size. A chat session's own
workspace goes with it. A PROJECT session never deletes the project folder: only Danica's saved conversation of it.
The current session cannot be deleted (use /new first).
"""

from danica import sessions
from danica.tui.commands import COMMANDS, DESCRIPTIONS

_list_sessions = COMMANDS["/sessions"]              # the original handler still shows the list


def _human(n: int) -> str:
    from danica.uploads import human
    return human(n)


def _delete(app, words: list[str]) -> None:
    if not words:
        app.ui.log("info", "usage: /sessions delete <number | 2-5 | id | empty | all>   (see /sessions for the numbers)")
        return
    root = sessions.SESSIONS_DIR
    ids, problems = sessions.select_sessions(root, words, current=app.session_id)
    for p in problems:
        app.ui.log("error", p)
    if not ids:
        if not problems:
            app.ui.log("info", "nothing to delete")
        return
    infos = [sessions.describe_session(root, sid) for sid in ids]
    lines = []
    for i in infos[:12]:
        when = f"{i['modified']:%d %b %H:%M}" if i["modified"] else "no transcript"
        where = "project (its folder is NOT touched)" if i["project"] else ("chat (its workspace files go too)" if i["has_files"] else "chat")
        lines.append(f"{i['id']}  {when}  {i['requests']} request(s)  {_human(i['size'])}  {where}\n    {i['title'] or '(untitled)'}")
    if len(infos) > 12:
        lines.append(f"... and {len(infos) - 12} more")
    total = sum(i["size"] for i in infos)
    body = "\n".join(lines)
    if not app.ui.confirm(f"Delete {len(infos)} session(s)?", body, f"{_human(total)} will be freed. This cannot be undone.",
                          yes="Yes, delete", no="No, keep them"):
        app.ui.log("info", "nothing deleted")
        return
    gone, failed = [], []
    for sid in ids:
        try:
            (gone if sessions.delete_session(root, sid) else failed).append(sid)
        except (ValueError, OSError) as exc:
            failed.append(sid)
            app.ui.log("error", f"{sid}: {exc}")
    if gone:
        app.ui.log("info", f"deleted {len(gone)} session(s), freed about {_human(total)}")
        app.audit.append("sessions_deleted", ids=gone)
    if failed:
        app.ui.log("error", f"could not fully delete: {', '.join(failed)} (a file may be open in another program)")


def _sessions(app, args):
    words = args.split()
    if words and words[0].lower() in ("delete", "rm", "remove"):
        _delete(app, words[1:])
    else:
        _list_sessions(app, args)


COMMANDS["/sessions"] = _sessions
DESCRIPTIONS["/sessions"] = "list saved sessions; /sessions delete <n|2-5|id|empty|all> removes them"


def _sidebar(app, args):
    """/sidebar - show or hide the session list on the left (same as Ctrl+B)."""
    app._call(app.action_toggle_sessions)


COMMANDS["/sidebar"] = _sidebar
DESCRIPTIONS["/sidebar"] = "show or hide the session list panel on the left (Ctrl+B)"
