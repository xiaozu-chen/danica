"""
present.py - Finds <present file="..."/> tags in streamed answer text.

The answer arrives in small fragments, so a tag can be split across them
("<pre" then "sent file=..."). PresentFilter holds back only text that could still
turn out to be a tag, and lets everything else through immediately.

feed(fragment) / flush() return a list of events:
    ("text", "...")      text to print as it is
    ("present", "path")  an image the answer wants shown (the tag itself is removed)
A malformed or unfinished tag is never swallowed: it comes back as plain text.
"""

import re

OPEN = "<present"
MAX_TAG = 300     # a "tag" longer than this without a closing > is just text
TAG_RX = re.compile(r'<present\s+file\s*=\s*"([^"\n<>]{1,260})"\s*/?>')


class PresentFilter:
    def __init__(self) -> None:
        self.buf = ""

    def feed(self, text: str) -> list[tuple[str, str]]:
        self.buf += text
        out: list[tuple[str, str]] = []
        while True:
            i = self.buf.find("<")
            if i == -1:                                   # no "<": everything is safe text
                if self.buf:
                    out.append(("text", self.buf))
                    self.buf = ""
                return out
            if i > 0:                                     # text before the "<" is safe
                out.append(("text", self.buf[:i]))
                self.buf = self.buf[i:]

            # The buffer now starts with "<"
            if len(self.buf) < len(OPEN) and OPEN.startswith(self.buf):
                return out                                # might still become "<present": wait
            if not self.buf.startswith(OPEN):
                out.append(("text", "<"))                 # some other "<" (a < b, <div>...)
                self.buf = self.buf[1:]
                continue

            end = self.buf.find(">")
            if end == -1:
                if len(self.buf) > MAX_TAG:               # too long to be a real tag
                    out.append(("text", "<"))
                    self.buf = self.buf[1:]
                    continue
                return out                                # the tag is not finished yet: wait
            tag, self.buf = self.buf[:end + 1], self.buf[end + 1:]
            m = TAG_RX.fullmatch(tag)
            out.append(("present", m.group(1).strip()) if m else ("text", tag))

    def flush(self) -> list[tuple[str, str]]:
        """The stream ended: whatever is still held back was never a tag."""
        out = [("text", self.buf)] if self.buf else []
        self.buf = ""
        return out