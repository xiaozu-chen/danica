"""
interactive.py - Interactive learning pages Danica can make: a QUIZ (Q&A with a working Submit
button) and FLASH CARDS. Each is ONE self-contained, sealed .html file the user opens from a link.

The model never writes HTML or JavaScript. It writes DATA (a small JSON file), this module checks
it, and the page's code is the fixed script below, so a quiz can run nothing the model chose.

SUBMIT, honestly: an HTML page opened from a link cannot message Danica. So Submit works LOCALLY:
the page grades the answers against the answer key (or, for a survey, just records them), shows the
score and a per-question explanation, and offers "Copy results" and "Download results (.json)" so
the user can paste or attach them in chat ("here are my quiz results, explain what I got wrong").

Quiz data (validate_quiz explains mistakes in plain words):
    {"title": "...", "mode": "graded" | "survey",          (mode is optional: graded when answers are given)
     "questions": [
        {"q": "...", "type": "choice", "options": ["a", "b", "c"], "answer": 1, "explanation": "..."},
        {"q": "...", "type": "multi",  "options": [...], "answer": [0, 2]},
        {"q": "...", "type": "truefalse", "answer": true},
        {"q": "...", "type": "text",   "answer": "Paris"}      (or a list of accepted spellings)
     ]}
    "answer" for choice/multi may be an index (0 = first option) or the option's text.
Flash card data:
    {"title": "...", "cards": [{"front": "...", "back": "...", "hint": "optional"}]}

SAFETY (same rules as slideshow.py and htmlview.py): the page is sealed, and its
Content-Security-Policy allows only this file's own style and script (by hash) and no network.
Everything the model wrote goes into the page as text (textContent), never as HTML.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import unicodedata
from html import escape
from pathlib import Path

from danica.docgen import MARKER
from danica.seal import is_sealed_html, seal_html

MAX_SPEC_BYTES = 200_000
MAX_QUESTIONS, MAX_OPTIONS, MAX_CARDS = 30, 6, 100
MAX_Q, MAX_OPT, MAX_EXPL, MAX_TITLE, MAX_CARD = 300, 150, 400, 100, 400
QUESTION_TYPES = ("choice", "multi", "truefalse", "text")


class InteractiveError(Exception):
    """A problem with the data. The message is safe to show the model."""


def _t(value, limit: int) -> str:
    """Text for the page: one line, invisible characters removed, cut to length."""
    s = " ".join(str(value).split())
    return "".join(ch for ch in s if unicodedata.category(ch) not in ("Cc", "Cf", "Co", "Cn", "Cs"))[:limit]


# ------------------------------------------------------------------ validation

def _index(answer, options: list[str], where: str) -> int:
    """One answer given as an index or as the option's text -> the index."""
    if isinstance(answer, bool):
        raise InteractiveError(f"{where}: the answer must be an option number (0 = first) or the option text")
    if isinstance(answer, int):
        if 0 <= answer < len(options):
            return answer
        raise InteractiveError(f"{where}: answer {answer} is outside the {len(options)} options (0 = first)")
    if isinstance(answer, str):
        wanted = " ".join(answer.split()).lower()
        for i, option in enumerate(options):
            if option.lower() == wanted:
                return i
    raise InteractiveError(f"{where}: the answer must be an option number (0 = first) or exactly one of the options")


def validate_quiz(data) -> dict:
    """Check a quiz spec and return a cleaned copy. Raises InteractiveError with a fix-it message."""
    if not isinstance(data, dict):
        raise InteractiveError('the file must hold one JSON object: {"title": "...", "questions": [...]}')
    questions = data.get("questions")
    if not isinstance(questions, list) or not 1 <= len(questions) <= MAX_QUESTIONS:
        raise InteractiveError(f'"questions" must be a list of 1 to {MAX_QUESTIONS} questions')
    out, with_answer = [], 0
    for n, q in enumerate(questions, start=1):
        where = f"question {n}"
        if not isinstance(q, dict) or not str(q.get("q", "")).strip():
            raise InteractiveError(f'{where} must be an object with a "q" (the question text)')
        kind = q.get("type") or ("truefalse" if isinstance(q.get("answer"), bool) else
                                 "multi" if isinstance(q.get("answer"), list) and q.get("options") else
                                 "choice" if q.get("options") else "text")
        if kind not in QUESTION_TYPES:
            raise InteractiveError(f'{where}: "type" must be one of ' + ", ".join(QUESTION_TYPES))
        item = {"q": _t(q["q"], MAX_Q), "type": kind, "explanation": _t(q.get("explanation", ""), MAX_EXPL)}
        raw = q.get("answer")
        if kind in ("choice", "multi"):
            options = q.get("options")
            if not isinstance(options, list) or not 2 <= len(options) <= MAX_OPTIONS or any(
                    isinstance(o, (dict, list)) or not str(o).strip() for o in options):
                raise InteractiveError(f'{where} needs "options": 2 to {MAX_OPTIONS} non-empty texts')
            item["options"] = [_t(o, MAX_OPT) for o in options]
            if len({o.lower() for o in item["options"]}) != len(item["options"]):
                raise InteractiveError(f"{where}: two options are the same")
            if raw is not None:
                if kind == "choice":
                    item["answer"] = _index(raw, item["options"], where)
                else:
                    picks = raw if isinstance(raw, list) else [raw]
                    if not picks:
                        raise InteractiveError(f"{where}: a multi question needs at least one correct option")
                    item["answer"] = sorted({_index(a, item["options"], where) for a in picks})
        elif kind == "truefalse":
            item["options"] = ["True", "False"]
            if raw is not None:
                if isinstance(raw, str) and raw.strip().lower() in ("true", "false"):
                    raw = raw.strip().lower() == "true"
                if not isinstance(raw, bool):
                    raise InteractiveError(f"{where}: a truefalse answer must be true or false")
                item["answer"] = 0 if raw else 1
        else:                                                   # text
            if raw is not None:
                accepted = raw if isinstance(raw, list) else [raw]
                accepted = [_t(a, MAX_OPT) for a in accepted if not isinstance(a, (dict, list)) and str(a).strip()]
                if not accepted:
                    raise InteractiveError(f'{where}: a text answer must be a text (or a list of accepted texts)')
                item["answer"] = accepted
        if "answer" in item:
            with_answer += 1
        out.append(item)

    mode = data.get("mode")
    if mode not in (None, "graded", "survey"):
        raise InteractiveError('"mode" must be "graded" or "survey"')
    mode = mode or ("graded" if with_answer else "survey")
    if mode == "graded" and with_answer != len(out):
        raise InteractiveError('a graded quiz needs an "answer" on every question (use "mode": "survey" for no grading)')
    return {"kind": "quiz", "title": _t(data.get("title", "Quiz"), MAX_TITLE) or "Quiz", "mode": mode, "questions": out}


def validate_cards(data) -> dict:
    """Check a flash card spec and return a cleaned copy."""
    if not isinstance(data, dict):
        raise InteractiveError('the file must hold one JSON object: {"title": "...", "cards": [...]}')
    cards = data.get("cards")
    if not isinstance(cards, list) or not 1 <= len(cards) <= MAX_CARDS:
        raise InteractiveError(f'"cards" must be a list of 1 to {MAX_CARDS} cards')
    out = []
    for n, c in enumerate(cards, start=1):
        if not isinstance(c, dict) or not str(c.get("front", "")).strip() or not str(c.get("back", "")).strip():
            raise InteractiveError(f'card {n} must be an object with a "front" and a "back"')
        out.append({"front": _t(c["front"], MAX_CARD), "back": _t(c["back"], MAX_CARD),
                    "hint": _t(c.get("hint", ""), MAX_OPT)})
    return {"kind": "flashcards", "title": _t(data.get("title", "Flash cards"), MAX_TITLE) or "Flash cards", "cards": out}


# ------------------------------------------------------------------ the pages

CSS = r"""
:root{--bg:#fff;--fg:#1d2330;--mute:#6b7280;--line:#d7dbe3;--card:#f5f6f9;--accent:#0072B2;--ok:#0a7d4b;--bad:#c2410c}
@media (prefers-color-scheme:dark){:root{--bg:#14161c;--fg:#e6e8ee;--mute:#9aa1b0;--line:#323746;--card:#1c2029;--accent:#56B4E9;--ok:#4ade80;--bad:#fb923c}}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,Segoe UI,sans-serif}
main{max-width:760px;margin:0 auto;padding:20px 16px 60px}
h1{font-size:22px;margin:0 0 4px}
.sub{color:var(--mute);margin:0 0 18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:0 0 14px}
.card h3{font-size:16px;margin:0 0 10px}
label.opt{display:flex;gap:8px;align-items:flex-start;padding:6px 8px;border-radius:6px;border:1px solid transparent;cursor:pointer}
label.opt:hover{border-color:var(--line)}
label.opt.ok{border-color:var(--ok);background:rgba(10,125,75,.12)}
label.opt.bad{border-color:var(--bad);background:rgba(194,65,12,.12)}
input[type=text]{width:100%;font:inherit;padding:8px 10px;color:var(--fg);background:var(--bg);border:1px solid var(--line);border-radius:6px}
button{font:inherit;color:var(--fg);background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 16px;cursor:pointer}
button:hover:not(:disabled){border-color:var(--accent)}
button:disabled{opacity:.5;cursor:default}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
.row{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}
.fb{margin-top:8px;font-size:14px}
.fb.ok{color:var(--ok)}.fb.bad{color:var(--bad)}
.fb .why{color:var(--mute);margin-top:2px}
#warn{color:var(--bad);min-height:1.4em}
#result{display:none}
#result h2{margin:0 0 6px}
.big{font-size:34px;font-weight:700}
.bar{height:8px;background:var(--line);border-radius:4px;overflow:hidden;margin:6px 0 12px}
.bar i{display:block;height:100%;background:var(--accent);width:0}
#stage{perspective:900px;margin:12px 0}
#flip{min-height:240px;display:flex;align-items:center;justify-content:center;text-align:center;font-size:22px;padding:24px;background:var(--card);border:1px solid var(--line);border-radius:14px;cursor:pointer;user-select:none}
#flip.back{border-color:var(--accent)}
#flip small{display:block;color:var(--mute);font-size:12px;letter-spacing:.06em;text-transform:uppercase;margin-bottom:10px}
#hint{color:var(--mute);min-height:1.4em;text-align:center}
kbd{border:1px solid var(--line);border-radius:4px;padding:0 5px;font-size:12px;background:var(--card)}
"""

QUIZ_JS = r"""
(function(){"use strict";
var Q=JSON.parse(document.getElementById("spec").textContent),$=function(i){return document.getElementById(i)};
function E(tag,parent,text,cls){var n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;if(parent)parent.appendChild(n);return n}
function norm(s){return String(s).toLowerCase().replace(/\s+/g," ").replace(/^[\s.,;:!?'"]+|[\s.,;:!?'"]+$/g,"")}
var graded=Q.mode==="graded",form=$("form"),sub=$("submit"),warn=$("warn"),done=false,nudged=false,cards=[];
$("title").textContent=Q.title;
$("sub").textContent=Q.questions.length+" question"+(Q.questions.length===1?"":"s")+(graded?" · graded":" · survey (no grading)");
Q.questions.forEach(function(q,i){var c=E("section",form,"","card");E("h3",c,(i+1)+". "+q.q);var inputs=[];
 if(q.type==="text"){var t=E("input",c);t.type="text";t.autocomplete="off";t.placeholder="Your answer";inputs.push(t)}
 else q.options.forEach(function(o,k){var lab=E("label",c,"","opt"),inp=E("input",lab);inp.type=q.type==="multi"?"checkbox":"radio";inp.name="q"+i;inp.value=k;inputs.push(inp);E("span",lab,o)});
 cards.push({card:c,inputs:inputs,fb:E("div",c,"","fb"),q:q})});
function given(c){if(c.q.type==="text")return c.inputs[0].value.trim();var g=[];c.inputs.forEach(function(inp,k){if(inp.checked)g.push(k)});return g}
function empty(c){var g=given(c);return c.q.type==="text"?g==="":g.length===0}
function isRight(c,g){var a=c.q.answer;if(c.q.type==="text")return a.some(function(x){return norm(x)===norm(g)});
 if(c.q.type==="multi")return g.length===a.length&&g.every(function(v,k){return v===a[k]});return g.length===1&&g[0]===a}
function show(c,k){return c.q.type==="text"?String(k):k.map(function(i){return c.q.options[i]}).join(", ")}
function answerText(c){var a=c.q.answer;return c.q.type==="text"?a[0]:c.q.type==="multi"?a.map(function(i){return c.q.options[i]}).join(", "):c.q.options[a]}
function lock(on){cards.forEach(function(c){c.inputs.forEach(function(i){i.disabled=on})})}
var results=null;
sub.addEventListener("click",function(){
 var blank=cards.filter(empty).length;
 if(blank&&!nudged){nudged=true;warn.textContent=blank+" question"+(blank===1?" is":"s are")+" not answered. Click Submit again to submit anyway.";return}
 warn.textContent="";done=true;lock(true);sub.disabled=true;var score=0,rows=[];
 cards.forEach(function(c,i){var g=given(c),has=!empty(c),ok=graded&&has&&isRight(c,g);if(ok)score++;
  var row={question:c.q.q,your_answer:has?show(c,g):"(no answer)"};
  if(graded){row.correct_answer=answerText(c);row.correct=ok}rows.push(row);
  if(graded){c.fb.className="fb "+(ok?"ok":"bad");c.fb.textContent="";E("div",c.fb,ok?"Correct":"Not quite. Answer: "+answerText(c));
   if(c.q.explanation)E("div",c.fb,c.q.explanation,"why");
   if(c.q.type!=="text")c.inputs.forEach(function(inp,k){var right=c.q.type==="multi"?c.q.answer.indexOf(k)>=0:c.q.answer===k;
    if(right)inp.parentNode.className="opt ok";else if(inp.checked)inp.parentNode.className="opt bad"})}
  else if(c.q.explanation){c.fb.textContent=c.q.explanation}});
 results={quiz:Q.title,mode:Q.mode,answered:cards.filter(function(c){return !empty(c)}).length,total:cards.length,answers:rows};
 if(graded)results.score=score;
 var r=$("result");r.style.display="block";
 $("score").textContent=graded?score+" / "+cards.length:"Thanks!";
 $("pct").textContent=graded?Math.round(score/cards.length*100)+"% correct":results.answered+" of "+cards.length+" answered";
 $("fill").style.width=(graded?score/cards.length*100:results.answered/cards.length*100)+"%";
 r.scrollIntoView({behavior:"smooth",block:"start"})});
function copyText(text){if(navigator.clipboard&&window.isSecureContext){return navigator.clipboard.writeText(text).then(function(){return true},function(){return legacy(text)})}return Promise.resolve(legacy(text))}
function legacy(text){var t=document.createElement("textarea");t.value=text;t.setAttribute("readonly","");t.className="offscreen";document.body.appendChild(t);t.select();var ok=false;try{ok=document.execCommand("copy")}catch(e){}document.body.removeChild(t);return ok}
$("copy").addEventListener("click",function(){if(!results)return;copyText(JSON.stringify(results,null,2)).then(function(ok){$("copied").textContent=ok?"Copied. Paste it into the chat.":"Could not copy: use Download instead."})});
$("dl").addEventListener("click",function(){if(!results)return;var b=new Blob([JSON.stringify(results,null,2)],{type:"application/json"}),a=document.createElement("a");a.href=URL.createObjectURL(b);a.download="quiz-results.json";document.body.appendChild(a);a.click();document.body.removeChild(a);setTimeout(function(){URL.revokeObjectURL(a.href)},1000)});
$("again").addEventListener("click",function(){done=false;nudged=false;results=null;lock(false);sub.disabled=false;$("result").style.display="none";$("copied").textContent="";
 cards.forEach(function(c){c.inputs.forEach(function(i){i.checked=false;if(i.type==="text")i.value="";if(i.parentNode.className.indexOf("opt")===0)i.parentNode.className="opt"});c.fb.textContent="";c.fb.className="fb"});window.scrollTo(0,0)});
})();
"""

CARDS_JS = r"""
(function(){"use strict";
var D=JSON.parse(document.getElementById("spec").textContent),$=function(i){return document.getElementById(i)};
$("title").textContent=D.title;
var all=D.cards.map(function(c,i){return{i:i,front:c.front,back:c.back,hint:c.hint}}),deck=all.slice(),pos=0,back=false,known={},missed={},hintShown=false;
function shuffle(a){for(var i=a.length-1;i>0;i--){var j=Math.floor(Math.random()*(i+1)),t=a[i];a[i]=a[j];a[j]=t}return a}
function count(o){return Object.keys(o).length}
function render(){var f=$("flip");f.textContent="";
 if(pos>=deck.length){finish();return}
 $("end").style.display="none";$("stage").style.display="block";$("ctl").style.display="flex";
 var c=deck[pos];f.className=back?"back":"";var s=document.createElement("small");s.textContent=back?"Answer":"Question";f.appendChild(s);f.appendChild(document.createTextNode(back?c.back:c.front));
 $("hint").textContent=(!back&&hintShown&&c.hint)?"Hint: "+c.hint:"";$("hintbtn").style.display=(!back&&c.hint)?"inline-block":"none";
 $("count").textContent="Card "+(pos+1)+" of "+deck.length+" · known "+count(known)+" · to review "+count(missed);
 $("fill").style.width=(pos/deck.length*100)+"%"}
function flip(){if(pos<deck.length){back=!back;hintShown=false;render()}}
function mark(ok){if(pos>=deck.length)return;var c=deck[pos];if(ok){known[c.i]=1;delete missed[c.i]}else{missed[c.i]=1;delete known[c.i]}pos++;back=false;hintShown=false;render()}
function move(d){var n=pos+d;if(n>=0&&n<deck.length){pos=n;back=false;hintShown=false;render()}}
function finish(){$("stage").style.display="none";$("ctl").style.display="none";$("hint").textContent="";$("fill").style.width="100%";var e=$("end");e.style.display="block";
 $("endscore").textContent=count(known)+" / "+all.length+" known";$("redo").style.display=count(missed)?"inline-block":"none";
 $("count").textContent="Finished · known "+count(known)+" · to review "+count(missed)}
function results(){return{deck:D.title,total:all.length,known:all.filter(function(c){return known[c.i]}).map(function(c){return c.front}),to_review:all.filter(function(c){return missed[c.i]}).map(function(c){return c.front}),not_seen:all.filter(function(c){return !known[c.i]&&!missed[c.i]}).map(function(c){return c.front})}}
$("flip").addEventListener("click",flip);$("flipbtn").addEventListener("click",flip);
$("hintbtn").addEventListener("click",function(){hintShown=true;render()});
$("again").addEventListener("click",function(){mark(false)});$("got").addEventListener("click",function(){mark(true)});
$("prev").addEventListener("click",function(){move(-1)});$("next").addEventListener("click",function(){move(1)});
$("shuf").addEventListener("click",function(){deck=shuffle(deck.slice());pos=0;back=false;render()});
$("redo").addEventListener("click",function(){deck=all.filter(function(c){return missed[c.i]});missed={};pos=0;back=false;render()});
$("restart").addEventListener("click",function(){deck=all.slice();known={};missed={};pos=0;back=false;render()});
function legacy(text){var t=document.createElement("textarea");t.value=text;t.setAttribute("readonly","");t.className="offscreen";document.body.appendChild(t);t.select();var ok=false;try{ok=document.execCommand("copy")}catch(e){}document.body.removeChild(t);return ok}
$("copy").addEventListener("click",function(){var text=JSON.stringify(results(),null,2),done=function(ok){$("copied").textContent=ok?"Copied. Paste it into the chat.":"Could not copy: use Download instead."};
 if(navigator.clipboard&&window.isSecureContext)navigator.clipboard.writeText(text).then(function(){done(true)},function(){done(legacy(text))});else done(legacy(text))});
$("dl").addEventListener("click",function(){var b=new Blob([JSON.stringify(results(),null,2)],{type:"application/json"}),a=document.createElement("a");a.href=URL.createObjectURL(b);a.download="flashcards-results.json";document.body.appendChild(a);a.click();document.body.removeChild(a);setTimeout(function(){URL.revokeObjectURL(a.href)},1000)});
document.addEventListener("keydown",function(e){if(e.key===" "||e.key==="Enter"){e.preventDefault();flip()}else if(e.key==="ArrowRight")move(1);else if(e.key==="ArrowLeft")move(-1);else if(e.key==="1"||e.key==="x")mark(false);else if(e.key==="2"||e.key==="c")mark(true)});
render();
})();
"""

# An off-screen class for the hidden textarea the copy fallback uses
CSS += ".offscreen{position:fixed;left:-9999px;top:0}\n"

QUIZ_BODY = (
    '<main><h1 id="title"></h1><p class="sub" id="sub"></p><div id="form"></div>'
    '<p id="warn"></p><div class="row"><button class="primary" id="submit">Submit</button></div>'
    '<section class="card" id="result"><h2>Results</h2><div class="big" id="score"></div>'
    '<div id="pct"></div><div class="bar"><i id="fill"></i></div>'
    '<div class="row"><button id="copy">Copy results</button><button id="dl">Download results (.json)</button>'
    '<button id="again">Try again</button></div><div id="copied" class="sub"></div></section></main>')

CARDS_BODY = (
    '<main><h1 id="title"></h1><p class="sub" id="count"></p><div class="bar"><i id="fill"></i></div>'
    '<div id="stage"><div id="flip" tabindex="0"></div></div><div id="hint"></div>'
    '<div class="row" id="ctl"><button id="prev">← Back</button><button id="flipbtn">Flip <kbd>Space</kbd></button>'
    '<button id="hintbtn">Hint</button><button id="again">Again <kbd>1</kbd></button>'
    '<button class="primary" id="got">Got it <kbd>2</kbd></button><button id="next">Skip →</button>'
    '<button id="shuf">Shuffle</button></div>'
    '<section class="card" id="end" style="display:none"><h2>Done</h2><div class="big" id="endscore"></div>'
    '<div class="row"><button id="redo">Review the ones I missed</button><button id="restart">Start over</button>'
    '<button id="copy">Copy results</button><button id="dl">Download results (.json)</button></div>'
    '<div id="copied" class="sub"></div></section></main>')


def _hash(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode("ascii") + "'"


def _page(spec: dict, body: str, js: str) -> str:
    """The sealed page for a validated spec."""
    data = json.dumps(spec, ensure_ascii=False).replace("<", "\\u003c").replace("&", "\\u0026")
    title = escape(spec["title"])
    # style="..." attributes are blocked by the style hash, so the one hidden section uses a class
    body = body.replace('style="display:none"', 'hidden')
    csp = ("default-src 'none'; base-uri 'none'; form-action 'none'; "
           f"style-src {_hash(CSS)}; script-src {_hash(js)}")
    page = (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{csp}">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<meta name="generator" content="{MARKER}">\n'
        f'<meta name="danica-kind" content="{spec["kind"]}">\n'
        '<meta name="danica-seal" content="">\n'
        f"<title>{title}</title>\n<style>{CSS}</style>\n</head><body>\n"
        f"{body}\n"
        f'<script type="application/json" id="spec">{data}</script>\n'
        f"<script>{js}</script>\n</body></html>\n")
    return seal_html(page)


def build_page(spec: dict) -> str:
    """HTML text for a quiz or flash card spec returned by validate_quiz / validate_cards."""
    if spec["kind"] == "quiz":
        return _page(spec, QUIZ_BODY, QUIZ_JS)
    return _page(spec, CARDS_BODY, CARDS_JS)


def write_page(spec: dict, out: Path) -> Path:
    """Write the sealed page to `out`. A file of that name is only replaced if Danica made it."""
    out = Path(out)
    if out.exists() and not is_sealed_html(out):
        raise InteractiveError(f"{out.name} already exists and was not made by Danica, so it was left alone")
    html = build_page(spec)
    if len(html.encode("utf-8")) > 2_000_000:
        raise InteractiveError("the page would be too large")
    tmp = out.with_name(out.name + ".tmp")
    try:
        tmp.write_text(html, encoding="utf-8")
        os.replace(tmp, out)
    except OSError as exc:
        raise InteractiveError(f"could not write the page ({type(exc).__name__})")
    finally:
        if tmp.exists():
            tmp.unlink()
    return out


_KIND = re.compile(r'<meta name="danica-kind" content="(quiz|flashcards)">')


def page_kind(path: Path) -> str:
    """"quiz", "flashcards" or "" for a file. Reads only the head of the file."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            m = _KIND.search(f.read(2048))
        return m.group(1) if m else ""
    except OSError:
        return ""
