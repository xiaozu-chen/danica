"""
/computer - let Danica look at and operate windows YOU approve (rules and limits: computer.py).

    /computer              status: on or off, approved windows, limits
    /computer on           turn it on for this session (needs a model that can see pictures)
    /computer off          turn it off (Esc, pressed anywhere in Windows, does this at once)
    /computer allow [text] approve a window: pick it from the list of open windows (text filters the list)
    /computer deny [n|all] take approval back (n = number in the status list)
    /computer smooth on|off  glide the mouse instead of jumping
    /computer log          the last things she did

Only YOU approve windows. Danica sees only those windows and never types passwords. It always starts OFF in a new session.
"""

from danica import screenctl, vision
from danica.computer import ComputerError, blocked_reason, clean_title, computer_for
from danica.tui.commands import COMMANDS, DESCRIPTIONS, command

DESCRIPTIONS["/computer"] = "let Danica operate windows you approve: on | off | allow | deny | smooth | log"


def _status(app, comp) -> None:
    lines = [f"computer use is {'ON' if comp.enabled else 'OFF'}  (Esc stops it at once)  "
             f"mouse: {'smooth' if comp.smooth else 'jumps'}  limit: {comp.max_actions} actions per message"]
    wins = comp.approved_windows() if comp.enabled else []
    if comp.approvals:
        for n, a in enumerate(comp.approvals.values(), start=1):
            alive = any(w.hwnd == a.hwnd for w in wins) or not comp.enabled
            lines.append(f"  {n}. {a.title}  ({a.process})" + ("" if alive else "  - closed"))
    else:
        lines.append("  no approved windows: /computer allow")
    app.ui.log("info", "\n".join(lines))


def _allow(app, comp, text: str) -> None:
    try:
        be = comp.backend
        wins = [w for w in be.list_windows() if not blocked_reason(w) and w.rect[2] > w.rect[0]]
    except (screenctl.ScreenError, ComputerError) as exc:
        app.ui.log("error", str(exc))
        return
    wins = [w for w in wins if w.hwnd not in comp.approvals]
    if text:
        wins = [w for w in wins if text.lower() in w.title.lower() or text.lower() in w.process]
    if not wins:
        app.ui.log("info", "no matching window is open (windows that look like a sign-in, shells and system tools are never listed)")
        return
    if len(wins) > 1:
        app.ui.log("info", "open windows:\n" + "\n".join(f"  {n}. {clean_title(w.title)}  ({w.process})" for n, w in enumerate(wins, start=1)))
        answer = app.ui.ask_text("Which window? (a number, or several like 1,3)")
        if not answer:
            app.ui.log("info", "nothing approved")
            return
        try:
            picks = sorted({int(p) for p in answer.replace(",", " ").split()})
            chosen = [wins[n - 1] for n in picks if 1 <= n <= len(wins)]
        except ValueError:
            chosen = []
        if not chosen:
            app.ui.log("error", "that is not a number from the list")
            return
    else:
        chosen = wins
    body = "\n".join(f"{clean_title(w.title)}  ({w.process})" for w in chosen)
    if not app.ui.confirm("Let Danica see and operate these windows?", body,
                          "She can look at them, click, drag, scroll and type in them. Never passwords. Esc stops her.",
                          yes="Yes, allow", no="No"):
        app.ui.log("info", "nothing approved")
        return
    for w in chosen:
        try:
            comp.approve(w)
            app.ui.log("ok", f"approved: {clean_title(w.title)}")
        except ComputerError as exc:
            app.ui.log("error", str(exc))
    app.audit.append("computer_allow", windows=[f"{w.process}: {clean_title(w.title)}" for w in chosen])


@command("/computer")
def _computer(app, args):
    comp = computer_for(app.ws)
    words = args.split(maxsplit=1)
    sub = words[0].lower() if words else ""
    rest = words[1].strip() if len(words) > 1 else ""
    if sub == "":
        _status(app, comp)
    elif sub == "on":
        if not vision.available():
            app.ui.log("error", "this model cannot see pictures, and computer use needs that (see /vision)")
            return
        try:
            comp.enable()
        except ComputerError as exc:
            app.ui.log("error", str(exc))
            return
        app.audit.append("computer_on")
        app.ui.log("ok", "computer use is ON. Now approve a window: /computer allow. Press Esc at any moment to stop her.")
    elif sub in ("off", "stop"):
        comp.disable("/computer off")
        app.audit.append("computer_off")
        app.ui.log("ok", "computer use is OFF")
    elif sub == "allow":
        if not comp.enabled:
            app.ui.log("error", "turn it on first: /computer on")
            return
        _allow(app, comp, rest)
    elif sub in ("deny", "revoke"):
        items = list(comp.approvals)
        if not rest or rest.lower() == "all":
            n = comp.revoke()
        elif rest.isdigit() and 1 <= int(rest) <= len(items):
            n = comp.revoke(items[int(rest) - 1])
        else:
            app.ui.log("error", "use /computer deny <number from /computer> or /computer deny all")
            return
        app.ui.log("ok", f"took approval back from {n} window(s)")
    elif sub == "smooth":
        if rest.lower() not in ("on", "off"):
            app.ui.log("info", f"mouse movement is {'smooth' if comp.smooth else 'instant'}: /computer smooth on|off")
            return
        comp.smooth = rest.lower() == "on"
        app.ui.log("ok", f"mouse movement is now {'smooth' if comp.smooth else 'instant'}")
    elif sub == "log":
        app.ui.log("info", "\n".join(comp.log[-20:]) or "nothing yet")
    else:
        app.ui.log("error", "use /computer on | off | allow | deny | smooth | log")
