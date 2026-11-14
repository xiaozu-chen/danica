"""
loopguard.py - Catches tool-call loops that the old "same call 5 times in a row" check missed.

WHY THE OLD CHECK FAILED
  1. It only compared a step with the step right before it, so  A -> B -> A -> B  never matched.
  2. It looked at the arguments only. `pytest -q` run again after the goal was already met is a
     loop even though nothing "new" is wrong with the call: it returns the SAME OUTPUT every time.
  3. The model runs at a low temperature, so an unchanged context gives an unchanged answer.
     Penalties alone cannot break that: the CONTEXT has to change (a reminder), or the call has to
     stop working (a block), or the sampling has to loosen (a warmer temperature for a few turns).

WHAT THIS DOES
  It remembers a window of recent calls as (call, output) pairs and escalates:
    level 1  the same call returned the same output 3 times  -> a NOTE is appended to the result
    level 2  4 times, or a call sequence repeating            -> a "[harness reminder] are you
                                                                  done?" message + warmer sampling
    level 3  5 times                                           -> that exact call is BLOCKED (it is
                                                                  not run; the model gets an error)
    level 4  the model keeps hitting blocked calls             -> the request stops, with a reason
  Real progress resets everything: a NEW successful write/edit means the situation changed, so
  blocks are lifted and the counters start over. (Running pytest, editing a file, running pytest
  again is debugging, not a loop.)

Nothing here stores text from the conversation beyond short hashes and one-line call summaries.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

WINDOW = 40                 # calls remembered
NOTE_AT = 3                 # level 1: identical call + identical output this many times
REMIND_AT = 4               # level 2
BLOCK_AT = 5                # level 3
CYCLE_MAX_PERIOD = 6        # longest call sequence looked for  (A,B,C,A,B,C ...)
CYCLE_REPS = 3              # level 2: a sequence of calls (period >= 2) with the same outputs, repeated this many times
SAME_OUTPUT_MIN_CHARS = 200 # level 2/4: same tool, DIFFERENT args, same long output
SAME_OUTPUT_REMIND_AT = 4
SAME_OUTPUT_STOP_AT = 7
MAX_BLOCKED_HITS = 3        # level 4: blocked calls attempted this many times -> stop the request
HOT_TURNS = 3               # warmer sampling for this many model turns after a level-2 reminder
HOT_TEMPERATURE = 0.8
REMIND_COOLDOWN = 6         # calls that must pass before another reminder (no nagging every step)
# Same FILE written again and again with slightly different content each time. Every such write looks
# "new" (a new call signature), so without a per-file count it would reset the guard forever.
PATH_REMIND_AT = {"write": 5, "edit": 9}    # writes to one path before the "are you done?" reminder
PATH_BLOCK_AT = {"write": 8, "edit": 14}    # ...and before further writes to that path are refused

# Tools whose successful, NEW call counts as real progress (it changes files/artifacts)
PROGRESS_TOOLS = frozenset({"write_file", "edit_file", "chart", "document", "diagram", "deck",
                            "mermaid", "dashboard"})

# Parts of a tool output that change every run without meaning anything changed
_VOLATILE = [
    (re.compile(r"\b\d+(?:\.\d+)?\s?(?:ms|s|sec|secs|seconds|min)\b", re.I), "<t>"),    # "in 0.52s"
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?Z?\b"), "<ts>"),  # timestamps
    (re.compile(r"0x[0-9a-fA-F]+"), "<addr>"),
]


def _norm_output(text: str) -> str:
    for rx, repl in _VOLATILE:
        text = rx.sub(repl, text)
    return text


def _canonical_args(arguments: str) -> str:
    """Same arguments written differently (key order, spacing in a command) compare equal."""
    try:
        data = json.loads(arguments or "{}")
    except ValueError:
        return " ".join(str(arguments).split())

    def clean(value):
        if isinstance(value, str):
            return " ".join(value.split())
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items()}
        if isinstance(value, list):
            return [clean(v) for v in value]
        return value
    return json.dumps(clean(data), sort_keys=True, ensure_ascii=False)


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]


def describe(name: str, arguments: str) -> str:
    """One short line naming a call, for messages: run_shell(pytest -q)."""
    args = _canonical_args(arguments)
    try:
        data = json.loads(args)
        first = next(iter(data.values()), "") if isinstance(data, dict) else args
    except ValueError:
        first = args
    text = " ".join(str(first).split())
    return f"{name}({text[:70] + '…' if len(text) > 70 else text})"


@dataclass
class _Record:
    sig: str            # name + canonical args
    name: str
    out: str            # hash of the normalised output
    out_len: int
    label: str          # one-line description of the call


@dataclass
class Verdict:
    note: str = ""          # append to THIS call's result (level 1)
    reminder: str = ""      # send as a harness reminder before the next model turn (level 2)
    stop: str = ""          # end the request with this message (level 4)


@dataclass
class LoopGuard:
    history: list[_Record] = field(default_factory=list)
    blocked: dict[str, str] = field(default_factory=dict)       # sig -> label of a blocked call
    fired: set[str] = field(default_factory=set)                # reminders already sent
    seen_progress: set[str] = field(default_factory=set)        # progress calls done this request
    blocked_hits: int = 0
    hot_turns: int = 0
    path_writes: dict[str, int] = field(default_factory=dict)   # (kind, path) -> successful writes this request
    blocked_paths: dict[str, str] = field(default_factory=dict) # (kind, path) -> label of a path that stopped taking writes
    total: int = 0              # calls recorded this request
    last_remind: int = -999     # value of `total` when the last reminder was sent

    # ---------------------------------------------------------------- before a call runs

    def check_blocked(self, call: dict) -> tuple[str, str]:
        """(error text, stop message). The error text is non-empty when the call must NOT run.
        The stop message is non-empty when the model has hit blocked calls too many times."""
        sig = self._sig(call)
        pkey = self._path_key(call)
        if pkey in self.blocked_paths:                    # the same file rewritten too many times
            self.blocked_hits += 1
            label = self.blocked_paths[pkey]
            if self.blocked_hits >= MAX_BLOCKED_HITS:
                return ("BLOCKED: loop guard", f"[stopped: Danica kept rewriting {label}. "
                        "Tell it what to do next, or say 'continue' after you changed something.]")
            return (f"BLOCKED: {label} was already written {PATH_BLOCK_AT[pkey.split(':', 1)[0]]} times in this "
                    "request, so this write was NOT done. The file is probably fine as it is. If the task "
                    "is finished, write your final summary now with no tool call. Otherwise do something "
                    "different (use the file, check a different one, or ask the user).", "")
        if sig not in self.blocked:
            return "", ""
        self.blocked_hits += 1
        label = self.blocked[sig]
        if self.blocked_hits >= MAX_BLOCKED_HITS:
            return ("BLOCKED: loop guard", f"[stopped: Danica was stuck repeating {label}. "
                    "Tell it what to do next, or say 'continue' after you changed something.]")
        return (f"BLOCKED: {label} already returned the same output {BLOCK_AT} times, so it was NOT "
                "run again. If the task is finished, write your final summary now with no tool call. "
                "Otherwise change something first (edit a file, use different arguments, or take a "
                "different approach).", "")

    # ---------------------------------------------------------------- after a call ran

    def record(self, call: dict, result: str) -> Verdict:
        """Log one finished call and return what (if anything) to tell the model."""
        name = call["name"]
        sig = self._sig(call)
        failed = result.startswith("ERROR")

        # Real progress: a NEW, successful change to a file/artifact -> fresh start
        pkey = self._path_key(call)
        path_reminder = ""
        if name in PROGRESS_TOOLS and not failed and pkey:
            n = self.path_writes[pkey] = self.path_writes.get(pkey, 0) + 1
            kind = pkey.split(":", 1)[0]
            label = describe(name, call["arguments"])
            if n >= PATH_BLOCK_AT[kind]:
                self.blocked_paths[pkey] = pkey.split(":", 1)[1]
            elif n >= PATH_REMIND_AT[kind]:
                path_reminder = self._remind(f"{pkey.split(':', 1)[1]} has been written {n} times in this request "
                                             "(the content changes a little each time)", "path:" + pkey, str(n))
            if n > PATH_REMIND_AT[kind] - 1:              # a file rewritten this often is NOT progress
                self.total += 1
                self.history.append(_Record(sig, name, _hash(_norm_output(result)), len(result), label))
                del self.history[:-WINDOW]
                return Verdict(reminder=path_reminder)
        if name in PROGRESS_TOOLS and not failed and sig not in self.seen_progress:
            self.seen_progress.add(sig)
            self.history.clear()
            self.blocked.clear()
            self.fired.clear()
            self.blocked_hits = 0
            self.hot_turns = 0
            return Verdict()

        self.total += 1
        rec = _Record(sig, name, _hash(_norm_output(result)), len(result), describe(name, call["arguments"]))
        self.history.append(rec)
        del self.history[:-WINDOW]
        verdict = Verdict()

        # 1) the same call, the same output, again and again (also catches A,B,A,B: each call
        #    still keeps returning the same thing, even with other calls in between)
        same = sum(1 for r in self.history if r.sig == sig and r.out == rec.out)
        if same >= BLOCK_AT:
            self.blocked[sig] = rec.label
        elif same >= REMIND_AT:
            verdict.reminder = self._remind(f"{rec.label} returned identical output {same} times", sig, rec.out)
        elif same >= NOTE_AT:
            verdict.note = (f"NOTE: this exact call has now returned the same output {same} times. "
                            "Repeating it will not change anything. If your goal is met, stop and "
                            "write the final summary.")

        # 2) a sequence of calls going round and round (A,B,A,B,A,B,A,B / A,B,C,A,B,C ...)
        if not verdict.reminder:
            period = self._cycle_period()
            if period:
                names = " -> ".join(r.label for r in self.history[-period:])
                verdict.reminder = self._remind(f"the call sequence [{names}] repeated {CYCLE_REPS} times",
                                                "cycle:" + "|".join(r.sig for r in self.history[-period:]), "")

        # 3) different arguments, same long output (e.g. pytest with other flags, same failure)
        if not verdict.reminder and rec.out_len >= SAME_OUTPUT_MIN_CHARS:
            alike = sum(1 for r in self.history if r.name == name and r.out == rec.out)
            if alike >= SAME_OUTPUT_STOP_AT:
                verdict.stop = (f"[stopped: Danica kept calling {name} and getting the same output "
                                f"({alike} times). Tell it what to do next.]")
            elif alike >= SAME_OUTPUT_REMIND_AT:
                verdict.reminder = self._remind(f"{name} returned the same long output {alike} times "
                                                "(with different arguments)", f"out:{name}:{rec.out}", "")
        return verdict

    # ---------------------------------------------------------------- sampling

    def temperature(self, normal: float) -> float:
        """Warmer sampling for a few turns after a reminder: an unchanged context at a low
        temperature gives an unchanged answer, so loosen it until the model is out of the rut."""
        if self.hot_turns > 0:
            self.hot_turns -= 1
            return max(normal, HOT_TEMPERATURE)
        return normal

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _sig(call: dict) -> str:
        return call["name"] + ":" + _canonical_args(call.get("arguments") or "{}")

    @staticmethod
    def _path_key(call: dict) -> str:
        """Returns "write:charts/scores.json" for write_file, "edit:..." for edit_file, "" for anything else.
        Chart/diagram/deck tools are not counted: they only read the data file, and re-drawing a
        chart a few times while tweaking it is normal."""
        name = call["name"]
        if name not in ("write_file", "edit_file"):       # the chart/deck/... tools only READ that file
            return ""
        try:
            data = json.loads(call.get("arguments") or "{}")
        except ValueError:
            return ""
        path = data.get("path") if isinstance(data, dict) else None
        if not isinstance(path, str) or not path.strip():
            return ""
        return ("edit:" if name == "edit_file" else "write:") + path.strip().replace("\\", "/").lower()

    def _cycle_period(self) -> int:
        """Smallest p >= 2 such that the last p calls AND their outputs repeated CYCLE_REPS times in
        a row. Outputs must match too, otherwise a legitimate edit/test/edit/test routine whose
        results change would look like a loop."""
        sigs = [(r.sig, r.out) for r in self.history]
        for p in range(2, CYCLE_MAX_PERIOD + 1):
            need = p * CYCLE_REPS
            if len(sigs) < need:
                break
            tail = sigs[-need:]
            if len(set(tail[:p])) > 1 and all(tail[i] == tail[i % p] for i in range(need)):
                return p
        return 0

    def _remind(self, what: str, key: str, out: str) -> str:
        """The "are you done?" message. Sent once per distinct loop, and it warms the sampling."""
        if key + out in self.fired or self.total - self.last_remind < REMIND_COOLDOWN:
            return ""
        self.fired.add(key + out)
        self.last_remind = self.total
        self.hot_turns = HOT_TURNS
        return ("[harness reminder, not from the user] You are going in circles: " + what + ". "
                "Before anything else, decide: is the user's task ALREADY finished? "
                "If YES (for example the tests pass or the file was written), stop calling tools and "
                "write your final summary now. "
                "If NO, say in one sentence what is different about the next step you will take. "
                "Calls that keep returning the same output will be refused.")