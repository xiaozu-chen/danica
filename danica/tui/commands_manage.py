"""
commands_manage.py - /manage opens a manager pane (the same ones as the sidebar menu).
Imported at the bottom of commands.py.
"""

from danica.managers import MANAGERS, find_manager
from danica.tui.commands import DESCRIPTIONS, command

DESCRIPTIONS["/manage"] = "open a manager pane: " + " | ".join(MANAGERS)


@command("/manage")
def _manage(app, args):
    """/manage <memories | knowledge | skills | reminders | webcache>"""
    manager = find_manager(args)
    if manager is None:
        app.ui.say("usage: /manage <" + " | ".join(MANAGERS) + ">")
        return
    app._call(app.open_manager, manager.key)
