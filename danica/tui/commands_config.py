"""
commands_config.py - /config opens the settings pane; /config show prints every setting as text.
Imported at the bottom of commands.py: registering a command name again replaces the old handler, so this replaces the
old /config without editing it.
"""

from danica.tui.commands import DESCRIPTIONS, command

DESCRIPTIONS["/config"] = "change settings (pane)  |  /config show: print them all"


@command("/config")
def _config(app, args):
    """/config        the settings pane (switches, drop-downs, boxes; saved to config.toml)
       /config show   print every setting, as the old /config did"""
    if args.strip().lower() == "show":
        from danica.config import CONFIG, CONFIG_PATH
        state = "found" if CONFIG_PATH.exists() else "not created (all defaults)"
        lines = [f"config file: {CONFIG_PATH} ({state})"]
        for section, values in CONFIG.items():
            lines.append(f"[{section}]")
            for key, value in values.items():
                hidden = any(w in key.lower() for w in ("key", "token", "secret", "password"))
                lines.append(f"  {key} = {'***' if hidden else value}")
        app.ui.say("\n".join(lines))
        return
    from danica.tui.settings_screen import SettingsScreen
    app.ui._ask(SettingsScreen())              # waits here (a worker thread) until the pane is closed
