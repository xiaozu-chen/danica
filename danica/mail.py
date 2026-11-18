"""
mail.py - Read-only email access over IMAP, and turning a raw message into clean, safe text for the model.

WHY IMAP, READ-ONLY
  IMAP works with Gmail, Outlook, Yahoo, iCloud and most others (Gmail / Outlook need an "app password", not the
  normal one). Everything stays on this computer: Danica talks straight to the mail server, no cloud service.
  Reading is the only thing this module can do:
    * the folder is opened with EXAMINE (select(readonly=True)): the server refuses any change to it;
    * messages are fetched with BODY.PEEK[], which does NOT mark them as read;
    * Mailbox._uid() only lets SEARCH and FETCH through. There is no code path that can send, move, flag or delete.

EMAIL IS UNTRUSTED TEXT
  Anyone can write to your address, so a message can contain instructions aimed at the AI ("ignore your rules and
  forward ..."). Defences in this file: invisible characters are stripped; text a mail client would not show
  (display:none, font-size:0, ...) is removed and counted; every size is capped. In tools/mail_tools.py the result is
  wrapped in a "this is data" banner and the conversation is marked TAINTED, so shell commands and new websites
  ask for your approval from then on (the same rule as for web pages).

CREDENTIALS
  Host and user live in config.toml under [mail]. The PASSWORD is never written there: it is kept in memory after
  /mail login, or read from the DANICA_MAIL_PASSWORD environment variable, or (if the `keyring` package is installed
  and you said yes) from the Windows Credential Manager. It is never shown to the model or logged.
"""

from __future__ import annotations

import base64
import imaplib
import json
import os
import re
import socket
import ssl
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from email import message_from_bytes, policy
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser

from danica.config import CONFIG, CONFIG_PATH
from danica.markdown_doc import strip_invisible

DEFAULTS = {"host": "", "user": "", "port": 993, "folder": "INBOX", "ssl": True, "timeout": 20,
            "max_list": 30, "snippet_chars": 240, "body_chars": 6000}
LIST_FETCH_BYTES = 60_000          # how much of each message the list view reads (enough for the headers + the start of the text)
READ_FETCH_BYTES = 3_000_000       # a message bigger than this is read only in part
BATCH = 10                         # messages fetched per request
MAX_LINKS = 40
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
SERVICE = "danica-mail"            # name used with the optional keyring package
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")   # IMAP dates are ALWAYS English


class MailError(Exception):
    """A problem worded for the model and the user."""


# ------------------------------------------------------------------ settings and credentials

def settings() -> dict:
    """[mail] from config.toml over the defaults."""
    return {**DEFAULTS, **{k: v for k, v in CONFIG.get("mail", {}).items() if k in DEFAULTS}}


_SESSION = {"password": None}      # the password lives ONLY here (memory) unless the user chose the credential manager


def password() -> str | None:
    """The password to use: this session's, else the DANICA_MAIL_PASSWORD variable, else the credential manager."""
    if _SESSION["password"]:
        return _SESSION["password"]
    if os.environ.get("DANICA_MAIL_PASSWORD"):
        return os.environ["DANICA_MAIL_PASSWORD"]
    user = settings()["user"]
    if user:
        try:
            import keyring
            return keyring.get_password(SERVICE, user) or None
        except Exception:                                  # not installed, or no backend: that is fine
            return None
    return None


def set_password(value: str | None) -> None:
    _SESSION["password"] = value or None


def keyring_available() -> bool:
    try:
        import keyring  # noqa: F401
        return True
    except Exception:
        return False


def keyring_save(user: str, value: str) -> bool:
    try:
        import keyring
        keyring.set_password(SERVICE, user, value)
        return True
    except Exception:
        return False


def keyring_forget(user: str) -> None:
    try:
        import keyring
        keyring.delete_password(SERVICE, user)
    except Exception:
        pass


def configured() -> tuple[bool, str]:
    """(ready, why not). Ready = a host, a user and a password are known."""
    cfg = settings()
    if not cfg["host"] or not cfg["user"]:
        return False, "no mailbox is set up: run /mail login"
    if not password():
        return False, f"no password for {cfg['user']} in this session: run /mail login"
    return True, ""


def save_account(host: str, user: str, port: int = 993, folder: str = "INBOX") -> None:
    """Write host / user / port / folder under [mail] in config.toml (everything else stays as it is). NO password."""
    values = {"host": host, "user": user, "port": port, "folder": folder}
    text = CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.exists() else ""
    lines = [f"{key} = {json.dumps(value)}" for key, value in values.items()]    # JSON strings are valid TOML strings
    section = re.search(r"(?ms)^\[mail\][ \t]*\n(.*?)(?=^\[|\Z)", text)
    block = "\n".join(lines) + "\n"
    if section:
        body = section.group(1)
        for key in values:                                 # drop the old copies of the keys we rewrite, keep the rest
            body = re.sub(rf"(?m)^[ \t]*{key}[ \t]*=.*\n?", "", body)
        text = text[:section.start(1)] + block + body + text[section.end(1):]
    else:
        text = text.rstrip() + ("\n\n" if text.strip() else "") + "[mail]\n" + block
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(text, encoding="utf-8")
    CONFIG.setdefault("mail", {}).update(values)


# ------------------------------------------------------------------ IMAP, read-only

# Indirection so tests can swap in a fake server
IMAP_SSL, IMAP_PLAIN = imaplib.IMAP4_SSL, imaplib.IMAP4


def _quote(value: str) -> str:
    """An IMAP quoted string. CR / LF / NUL are refused: they would let text break out of the command."""
    if re.search(r"[\r\n\x00]", value):
        raise MailError("a search value contains a line break or control character")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _utf7_decode(name: str) -> str:
    """IMAP folder names use modified UTF-7 for non-ASCII: '&AOk-' is 'é'."""
    out, i = [], 0
    while i < len(name):
        if name[i] != "&":
            out.append(name[i])
            i += 1
            continue
        end = name.find("-", i)
        if end == -1:
            out.append(name[i:])
            break
        chunk = name[i + 1:end]
        if not chunk:
            out.append("&")
        else:
            b64 = chunk.replace(",", "/")
            try:
                out.append(base64.b64decode(b64 + "=" * (-len(b64) % 4)).decode("utf-16-be"))
            except Exception:
                out.append(name[i:end + 1])
        i = end + 1
    return "".join(out)


def imap_date(day: date) -> str:
    return f"{day.day:02d}-{MONTHS[day.month - 1]}-{day.year}"


class Mailbox:
    """One short IMAP session:  with Mailbox() as box: box.folders(); box.search(...); box.fetch(...).
    Only SEARCH and FETCH are ever sent after login (see _uid)."""

    def __init__(self) -> None:
        self.cfg = settings()
        self.conn = None
        self.folder: str | None = None
        self._names: dict[str, str] | None = None            # display name -> raw server name

    # -- connection
    def __enter__(self) -> "Mailbox":
        ok, why = configured()
        if not ok:
            raise MailError(why)
        cfg = self.cfg
        try:
            if cfg["ssl"]:
                self.conn = IMAP_SSL(cfg["host"], int(cfg["port"]), ssl_context=ssl.create_default_context(), timeout=cfg["timeout"])
            elif cfg["host"] in LOOPBACK:                    # unencrypted is only ever allowed on THIS computer (tests)
                self.conn = IMAP_PLAIN(cfg["host"], int(cfg["port"]), timeout=cfg["timeout"])
            else:
                raise MailError("refusing an unencrypted connection to a remote server: set ssl = true under [mail]")
            self.conn.login(cfg["user"], password())
        except MailError:
            raise
        except imaplib.IMAP4.error:
            self.close()
            raise MailError("the server refused the login. Gmail and Outlook need an APP PASSWORD (not your normal password) "
                            "and IMAP switched on in the account settings. Run /mail login to try again.") from None
        except (OSError, ssl.SSLError, socket.timeout) as exc:
            self.close()
            raise MailError(f"could not connect to {cfg['host']}:{cfg['port']} ({type(exc).__name__}). "
                            "Check the host name and your internet connection.") from None
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        conn, self.conn = self.conn, None
        if conn is not None:
            try:
                conn.logout()
            except Exception:
                pass

    # -- the only two commands that are ever sent after login
    def _uid(self, command: str, *args):
        if command.upper() not in ("SEARCH", "FETCH"):        # the read-only guarantee, enforced in one place
            raise MailError(f"internal error: the command {command} is not allowed")
        try:
            kind, data = self.conn.uid(command, *args)
        except (imaplib.IMAP4.error, OSError, socket.timeout) as exc:
            raise MailError(f"the mail server did not answer properly ({type(exc).__name__})") from None
        if kind != "OK":
            raise MailError("the mail server refused the request")
        return data

    # -- folders
    def folders(self) -> list[str]:
        """Display names of the folders you can read."""
        if self._names is None:
            try:
                kind, data = self.conn.list()
            except (imaplib.IMAP4.error, OSError, socket.timeout) as exc:
                raise MailError(f"could not list folders ({type(exc).__name__})") from None
            names: dict[str, str] = {}
            for item in data or []:
                if not isinstance(item, (bytes, bytearray)):
                    continue
                m = re.match(rb'\((?P<flags>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+)$', bytes(item))
                if not m or b"\\noselect" in m.group("flags").lower():
                    continue
                raw = m.group("name").decode("utf-8", "replace").strip()
                if raw.startswith('"') and raw.endswith('"'):
                    raw = raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
                names[_utf7_decode(raw)] = raw
            self._names = names
        return sorted(self._names, key=lambda n: (n.upper() != "INBOX", n.lower()))

    def select(self, folder: str = "") -> str:
        """Open a folder READ-ONLY. The name must be one the server listed (the model's text never reaches the command)."""
        folder = (folder or self.cfg["folder"] or "INBOX").strip()
        listed = self.folders()
        match = next((n for n in listed if n.lower() == folder.lower()), None)
        if match is None:
            raise MailError(f"there is no folder '{folder[:60]}'. Folders: {', '.join(listed[:30])}")
        try:
            kind, _ = self.conn.select(_quote(self._names[match]), readonly=True)      # readonly=True = EXAMINE
        except (imaplib.IMAP4.error, OSError, socket.timeout) as exc:
            raise MailError(f"could not open '{match}' ({type(exc).__name__})") from None
        if kind != "OK":
            raise MailError(f"could not open '{match}'")
        self.folder = match
        return match

    # -- searching
    def search(self, criteria: list[str]) -> list[int]:
        """UIDs matching the criteria, oldest first."""
        data = self._uid("SEARCH", *(criteria or ["ALL"]))
        return sorted(int(x) for x in (data[0] or b"").split() if x.isdigit())

    # -- fetching
    def fetch(self, uids: list[int], max_bytes: int = LIST_FETCH_BYTES) -> list["Parsed"]:
        """Parse these messages (headers + the first max_bytes of each). BODY.PEEK: nothing is marked as read."""
        results: list[Parsed] = []
        for start in range(0, len(uids), BATCH):
            batch = [int(u) for u in uids[start:start + BATCH]]
            data = self._uid("FETCH", ",".join(map(str, batch)), f"(UID FLAGS RFC822.SIZE BODY.PEEK[]<0.{int(max_bytes)}>)")
            for i, item in enumerate(data or []):
                if not isinstance(item, tuple):
                    continue
                head = bytes(item[0]) + (bytes(data[i + 1]) if i + 1 < len(data) and isinstance(data[i + 1], (bytes, bytearray)) else b"")
                uid = re.search(rb"UID (\d+)", head)
                flags = re.search(rb"FLAGS \(([^)]*)\)", head)
                size = re.search(rb"RFC822\.SIZE (\d+)", head)
                parsed = parse_message(bytes(item[1]), settings()["body_chars"])
                parsed.uid = int(uid.group(1)) if uid else 0
                parsed.unread = b"\\seen" not in (flags.group(1).lower() if flags else b"")
                parsed.size = int(size.group(1)) if size else len(item[1])
                parsed.truncated = parsed.size > len(item[1])
                results.append(parsed)
        return results


def build_criteria(unread: bool | None = None, days: int | None = None, since: str = "", before: str = "",
                   sender: str = "", subject: str = "", text: str = "") -> tuple[list[str], dict]:
    """(IMAP SEARCH tokens, client-side filters). Every text value is quoted; text with non-ASCII letters can't be
    sent to the server safely, so it is applied here after fetching instead."""
    tokens: list[str] = []
    later: dict[str, str] = {}
    if unread is True:
        tokens.append("UNSEEN")
    elif unread is False:
        tokens.append("SEEN")
    if days:
        tokens += ["SINCE", imap_date(date.today() - timedelta(days=max(0, int(days) - 1)))]
    for key, token, value in (("since", "SINCE", since), ("before", "BEFORE", before)):
        if value:
            try:
                tokens += [token, imap_date(date.fromisoformat(value.strip()))]
            except ValueError:
                raise MailError(f"{key} must be a date like 2026-11-30") from None
    for token, value in (("FROM", sender), ("SUBJECT", subject), ("TEXT", text)):
        value = (value or "").strip()
        if not value:
            continue
        if len(value) > 100:
            raise MailError("a search text is too long (100 characters at most)")
        if value.isascii():
            tokens += [token, _quote(value)]
        else:
            later[token.lower()] = value.lower()
    return tokens or ["ALL"], later


def apply_later(items: list["Parsed"], later: dict) -> list["Parsed"]:
    """The client-side half of build_criteria (non-ASCII search text)."""
    def keep(p: "Parsed") -> bool:
        return ((not later.get("from") or later["from"] in f"{p.from_name} {p.from_addr}".lower())
                and (not later.get("subject") or later["subject"] in p.subject.lower())
                and (not later.get("text") or later["text"] in (p.subject + " " + p.text).lower()))
    return [p for p in items if keep(p)] if later else items


# ------------------------------------------------------------------ a message, cleaned

@dataclass
class Parsed:
    uid: int = 0
    unread: bool = False
    size: int = 0
    truncated: bool = False
    subject: str = ""
    from_name: str = ""
    from_addr: str = ""
    reply_to: str = ""
    return_path: str = ""
    to_count: int = 0
    date: datetime | None = None
    message_id: str = ""
    bulk: bool = False                                        # a newsletter / mailing list (List-Unsubscribe, Precedence: bulk)
    auth: dict = field(default_factory=dict)                  # {"spf": "pass", "dkim": "pass", "dmarc": "fail"} as the server reported
    text: str = ""                                            # the readable body, cleaned
    links: list = field(default_factory=list)                 # [(shown text, address)]
    attachments: list = field(default_factory=list)           # [{"name", "type", "size"}]
    hidden_chars: int = 0                                     # characters of invisible text that were removed
    hidden_text: str = ""                                     # the first part of it (kept only so it can be scanned, never shown)
    html: bool = False
    tricks: list = field(default_factory=list)                # e.g. "right-to-left override character in the subject"


# Elements whose text a mail client never shows
SKIP_TAGS = {"script", "style", "head", "title", "noscript", "template", "svg"}
VOID_TAGS = {"br", "img", "hr", "meta", "link", "input", "area", "base", "col", "embed", "source", "track", "wbr"}
BLOCK_TAGS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol", "blockquote",
              "section", "article", "header", "footer", "hr"}
HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?![.\d])|opacity\s*:\s*0(?![.\d])"
                          r"|max-height\s*:\s*0(?![.\d])|mso-hide\s*:\s*all|width\s*:\s*0(?:px)?\s*;[^\"']*height\s*:\s*0", re.I)
URL_RX = re.compile(r"https?://[^\s<>\"'\)\]]+", re.I)
BIDI = "‪‫‬‭‮⁦⁧⁨⁩"      # characters that make text display in a different order than it is stored


class _HtmlText(HTMLParser):
    """HTML -> (visible text, links). Skips what a person would not see and counts it."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.hidden_chars = 0
        self.hidden_text = ""
        self._stack: list[tuple[str, bool, bool]] = []         # (tag, hidden, skip)
        self._href: str | None = None
        self._anchor: list[str] = []

    @property
    def _off(self) -> tuple[bool, bool]:
        return (any(h for _, h, _ in self._stack), any(s for _, _, s in self._stack))

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag in BLOCK_TAGS:
            self.parts.append("\n")
        if tag in VOID_TAGS:
            return
        hidden = "hidden" in a or bool(HIDDEN_STYLE.search(a.get("style", "")))
        self._stack.append((tag, hidden, tag in SKIP_TAGS))
        if tag == "a" and a.get("href") and not any(self._off):
            self._href, self._anchor = a["href"].strip(), []

    def handle_startendtag(self, tag, attrs):
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "a" and self._href is not None:
            self.links.append((re.sub(r"\s+", " ", "".join(self._anchor)).strip(), self._href))
            self._href, self._anchor = None, []
        for i in range(len(self._stack) - 1, -1, -1):          # close up to the matching open tag (real mail HTML is sloppy)
            if self._stack[i][0] == tag:
                del self._stack[i:]
                break

    def handle_data(self, data):
        hidden, skip = self._off
        if skip:
            return
        if hidden:
            self.hidden_chars += len(data.strip())
            if len(self.hidden_text) < 600:
                self.hidden_text += " " + data.strip()
            return
        self.parts.append(data)
        if self._href is not None:
            self._anchor.append(data)


def _clean(text: str) -> str:
    """Invisible characters out, blank-line runs collapsed."""
    text = strip_invisible(text)
    text = re.sub(r"[ \t ]+", " ", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def _header(msg, name: str, limit: int = 300) -> str:
    try:
        value = msg.get(name)
        return strip_invisible(" ".join(str(value).split()))[:limit] if value is not None else ""
    except Exception:                                          # a malformed header must not break the list
        return ""


def _text_of(part) -> str:
    try:
        return part.get_content()
    except Exception:
        raw = part.get_payload(decode=True) or b""
        return raw.decode(part.get_content_charset() or "utf-8", "replace")


def parse_message(raw: bytes, body_chars: int = 6000) -> Parsed:
    """A raw RFC 822 message -> Parsed. Never raises on a bad message."""
    p = Parsed()
    msg = message_from_bytes(raw, policy=policy.default)
    raw_subject = str(msg.get("Subject", "") or "")
    p.subject = _header(msg, "Subject", 200) or "(no subject)"
    p.from_name, p.from_addr = (strip_invisible(s) for s in parseaddr(str(msg.get("From", "") or "")))
    p.from_name, p.from_addr = p.from_name[:100], p.from_addr.lower()[:200]
    p.reply_to = parseaddr(str(msg.get("Reply-To", "") or ""))[1].lower()[:200]
    p.return_path = parseaddr(str(msg.get("Return-Path", "") or ""))[1].lower()[:200]
    p.to_count = len(re.findall(r"@", str(msg.get("To", "") or "") + str(msg.get("Cc", "") or "")))
    p.message_id = _header(msg, "Message-ID", 200)
    try:
        p.date = parsedate_to_datetime(str(msg.get("Date"))).astimezone()      # shown in YOUR time zone
    except Exception:
        p.date = None
    p.bulk = bool(msg.get("List-Unsubscribe") or msg.get("List-Id")
                  or _header(msg, "Precedence").lower() in ("bulk", "list", "junk"))
    # What the receiving server says about SPF / DKIM / DMARC (a forged header can lie, so this is a hint, not proof)
    try:
        results = " ".join(str(h) for h in (msg.get_all("Authentication-Results") or [])) + " " + str(msg.get("Received-SPF", "") or "")
    except Exception:
        results = ""
    for kind in ("spf", "dkim", "dmarc"):
        m = re.search(rf"\b{kind}\s*=\s*(\w+)", results, re.I)
        if m:
            p.auth[kind] = m.group(1).lower()
    if "spf" not in p.auth:
        m = re.match(r"\s*(pass|fail|softfail|neutral|none)\b", str(msg.get("Received-SPF", "") or ""), re.I)
        if m:
            p.auth["spf"] = m.group(1).lower()
    if any(c in raw_subject for c in BIDI):
        p.tricks.append("the subject contains a right-to-left / direction-override character (used to disguise text)")

    # The body: the plain part for reading, the HTML part (if any) for links and hidden text
    plain = html = None
    try:
        for part in msg.walk():
            if part.is_multipart() or part.get_content_disposition() == "attachment":
                continue
            kind = part.get_content_type()
            if kind == "text/plain" and plain is None:
                plain = _text_of(part)
            elif kind == "text/html" and html is None:
                html = _text_of(part)
    except Exception:
        pass
    links: list[tuple[str, str]] = []
    if html is not None:
        p.html = True
        parser = _HtmlText()
        try:
            parser.feed(html)
            parser.close()
        except Exception:
            pass
        p.hidden_chars = parser.hidden_chars
        p.hidden_text = parser.hidden_text[:600]
        links = parser.links
        html_text = "".join(parser.parts)
    else:
        html_text = ""
    body = plain if plain and plain.strip() else html_text
    if plain:
        links += [("", u.rstrip(".,;:!?")) for u in URL_RX.findall(plain)]
    p.text = _clean(body or "")[:body_chars]
    seen, unique = set(), []
    for text, href in links:                                   # one entry per address, in order
        key = (text.lower(), href)
        if href and key not in seen:
            seen.add(key)
            unique.append((strip_invisible(text)[:120], strip_invisible(href)[:500]))
    p.links = unique[:MAX_LINKS]

    # Attachments: only described, never saved or opened
    try:
        for part in msg.walk():
            if part.is_multipart():
                continue
            name = part.get_filename()
            if part.get_content_disposition() == "attachment" or (name and not part.get_content_type().startswith("text/plain")):
                name = name or "(unnamed)"
                if any(c in name for c in BIDI):
                    p.tricks.append("an attachment name contains a direction-override character (hides the real file type)")
                p.attachments.append({"name": strip_invisible(name)[:120], "type": part.get_content_type(),
                                      "size": len(part.get_payload(decode=True) or b"")})
    except Exception:
        pass
    return p


def snippet(p: Parsed, chars: int | None = None) -> str:
    """The first words of the message, without quoted replies, on one line."""
    chars = chars or settings()["snippet_chars"]
    lines = [ln.strip() for ln in p.text.splitlines()
             if ln.strip() and not ln.lstrip().startswith(">") and not re.match(r"(?i)^on .{5,80} wrote:?$", ln.strip())]
    return " ".join(" ".join(lines).split())[:chars]


# ------------------------------------------------------------------ hints (rules, no model)

DEADLINE_RX = re.compile(r"\b(?:by|before|until|due|deadline)\b[^.\n]{0,25}\b(?:today|tomorrow|tonight|eod|end of (?:day|week|month)|"
                         r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|\d{1,2}[/-]\d{1,2}|"
                         r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? \d{1,2})|\basap\b|\burgent\b|\bdeadline\b", re.I)
ASK_RX = re.compile(r"\b(?:please|could you|can you|would you|need you to|let me know|action required|kindly|reply|respond)\b", re.I)
MEETING_RX = re.compile(r"\b(?:invite|invitation|meeting|calendar|zoom|teams|google meet|rsvp|schedule)\b", re.I)
MONEY_RX = re.compile(r"\b(?:invoice|payment|receipt|billing|statement|refund|order|subscription)\b", re.I)


def hints(p: Parsed) -> list[str]:
    """Short tags that help sort a mailbox: what kind of mail is this? Rule-based, so they cost nothing."""
    head = (p.subject + " " + p.text[:1500])
    out = []
    if p.bulk:
        out.append("newsletter")
    if "?" in p.text[:800] and not p.bulk:
        out.append("question")
    if ASK_RX.search(head) and not p.bulk:
        out.append("asks something")
    if DEADLINE_RX.search(head):
        out.append("deadline")
    if MEETING_RX.search(head) or any(a["name"].lower().endswith(".ics") for a in p.attachments):
        out.append("meeting")
    if MONEY_RX.search(head):
        out.append("money")
    if p.attachments:
        out.append(f"{len(p.attachments)} attachment" + ("s" if len(p.attachments) > 1 else ""))
    return out[:5]


def defang(url: str, limit: int = 90) -> str:
    """A link written so it cannot be clicked by accident: hxxps://exa[.]mple[.]com/path"""
    m = re.match(r"(?i)^([a-z][a-z0-9+.-]*)://([^/?#]*)(.*)$", url.strip())
    if not m:                                             # a bare host name or address
        return url.strip().replace(".", "[.]")[:limit]
    scheme, host, rest = m.groups()
    scheme = re.sub(r"(?i)^http", "hxxp", scheme)
    return f"{scheme}://{host.replace('.', '[.]')}{rest}"[:limit]
