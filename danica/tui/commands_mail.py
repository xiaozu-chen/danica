"""
commands_mail.py - /mail: connect Danica to your mailbox (read-only).

    /mail            status: which mailbox, and whether a password is known
    /mail login      email address, server, password; tests the connection before keeping anything
    /mail test       connect again and show the folders and the unread count
    /mail logout     forget the password (also removes the Windows Credential Manager copy)

The password is typed into a masked box, kept in MEMORY only (or, if you say yes and the `keyring` package is
installed, in the Windows Credential Manager). It never goes into config.toml, the chat, or the model's context.
Reading mail itself is done by the mail_* tools (see tools/mail_tools.py): ask Danica "what's new in my inbox?".

Imported at the bottom of commands.py, like commands_vision.py.
"""

from danica import mail
from danica.tui.commands import DESCRIPTIONS, command

DESCRIPTIONS["/mail"] = "connect your mailbox (read-only): login | test | logout"

# Where the IMAP server usually is, by the domain of the address
KNOWN_HOSTS = {"gmail.com": "imap.gmail.com", "googlemail.com": "imap.gmail.com",
               "outlook.com": "outlook.office365.com", "hotmail.com": "outlook.office365.com", "live.com": "outlook.office365.com",
               "office365.com": "outlook.office365.com", "yahoo.com": "imap.mail.yahoo.com", "ymail.com": "imap.mail.yahoo.com",
               "icloud.com": "imap.mail.me.com", "me.com": "imap.mail.me.com", "aol.com": "imap.aol.com",
               "zoho.com": "imap.zoho.com"}
APP_PASSWORD_HELP = {"imap.gmail.com": "Gmail: turn on 2-step verification, then create an App Password at myaccount.google.com/apppasswords.",
                     "outlook.office365.com": "Outlook / Hotmail: IMAP must be enabled, and accounts with 2-step verification need an app password "
                                              "(account.microsoft.com > Security).",
                     "imap.mail.yahoo.com": "Yahoo: create an app password under Account Security.",
                     "imap.mail.me.com": "iCloud: create an app-specific password at appleid.apple.com."}


def _test(app) -> bool:
    """Connect, open the default folder read-only, report the unread count. True on success."""
    try:
        with mail.Mailbox() as box:
            folders = box.folders()
            where = box.select()
            unread = len(box.search(["UNSEEN"]))
    except mail.MailError as e:
        app.ui.log("error", str(e))
        return False
    app.ui.log("ok", f"connected to {mail.settings()['host']} (read-only). {where}: {unread} unread. "
                     f"Folders: {', '.join(folders[:12])}{' ...' if len(folders) > 12 else ''}")
    return True


@command("/mail")
def _mail(app, args):
    """/mail [login | test | logout]"""
    word = args.strip().lower()
    cfg = mail.settings()
    if word == "logout":
        mail.set_password(None)
        if cfg["user"]:
            mail.keyring_forget(cfg["user"])
        app.ui.log("info", "the mail password was forgotten. Reading mail needs /mail login again.")
        return
    if word == "test":
        ready, why = mail.configured()
        if not ready:
            app.ui.log("warning", why)
            return
        _test(app)
        return
    if word == "login":
        user = (app.ui.ask_text("Your email address", default=cfg["user"]) or "").strip()
        if not user or "@" not in user:
            app.ui.log("info", "cancelled: no email address given.")
            return
        guess = cfg["host"] if (cfg["host"] and cfg["user"] == user) else KNOWN_HOSTS.get(user.split("@")[1].lower(), "imap." + user.split("@")[1])
        host = (app.ui.ask_text("IMAP server (change it only if the guess is wrong)", default=guess) or "").strip()
        if not host:
            app.ui.log("info", "cancelled: no server given.")
            return
        help_line = APP_PASSWORD_HELP.get(host.lower(), "")
        secret = app.ui.ask_text(f"Password for {user}" + (f"\n{help_line}" if help_line else "") + "\n(kept in memory only; Esc cancels)", secret=True)
        if not secret:
            app.ui.log("info", "cancelled: no password entered.")
            return
        # Try it BEFORE saving anything: a wrong password must not replace a working setup
        before = (dict(cfg), mail._SESSION["password"])
        mail.CONFIG.setdefault("mail", {}).update(host=host, user=user)
        mail.set_password(secret)
        if not _test(app):
            mail.CONFIG["mail"].update(host=before[0]["host"], user=before[0]["user"])
            mail.set_password(before[1])
            return
        mail.save_account(host, user, int(cfg["port"]), cfg["folder"])
        if mail.keyring_available() and app.ui.confirm("Remember the password?", "Store it in the Windows Credential Manager so you "
                                                       "don't have to type it each session? (No = memory only.)", "", "Remember", "Memory only"):
            if mail.keyring_save(user, secret):
                app.ui.log("info", "the password was saved in the Windows Credential Manager (/mail logout removes it).")
        app.ui.log("info", "Danica can now read your mail (read-only: she cannot send, move or delete anything). "
                           "Try: \"what's new in my inbox?\"")
        return
    ready, why = mail.configured()
    state = f"{cfg['user']} on {cfg['host']}" if cfg["user"] else "no mailbox set up"
    app.ui.log("info" if ready else "warning",
               f"mail: {state}. " + ("Ready (read-only)." if ready else why) +
               "\n  /mail login   connect   /mail test   check the connection   /mail logout   forget the password")
