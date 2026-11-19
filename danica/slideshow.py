"""
slideshow.py - A self-contained HTML slideshow from the same Markdown outline as the .pptx deck.

One file: a 16:9 stage (1280 x 720) scaled to the window; arrow keys, Home/End, click, f = fullscreen,
n = speaker notes; Ctrl+P prints one slide per page. Pictures are embedded as data: addresses.

SAFETY: every piece of text from the outline is HTML-escaped; the page carries a strict
Content-Security-Policy that allows ONLY the exact style and script Danica wrote (by hash) and
data: images, so even a missed escape could not run anything; and the finished file is SEALED
(seal.py) so /open can tell it from a model-written page.
HONEST LIMITS: text fitting is an estimate (the slide scrolls inside its box); at most 25 MB of
pictures; on very narrow windows the stage is letterboxed.
"""

import base64
import hashlib
import html as htmllib
import os
from collections.abc import Callable, Iterable
from pathlib import Path

from danica.deckgen import (
    MAX_CODE_LINES,
    MAX_SLIDES,
    MAX_TABLE_ROWS,
    SIZES,
    SOURCES_PER_SLIDE,
    THEMES,
    fit_size,
    split_slides,
    text_items,
)
from danica.docgen import MARKER
from danica.markdown_doc import DocError, labelled
from danica.seal import seal_html

MAX_EMBED_BYTES = 25_000_000
MAX_FILE_BYTES = 40_000_000


def esc(text: str) -> str:
    """Escape text for safe insertion into HTML."""
    return htmllib.escape(text, quote=True)


def runs_html(runs: Iterable[tuple[str, frozenset]]) -> str:
    """Render styled text runs as escaped HTML elements."""
    out = []
    for text, style in runs:
        t = esc(text)
        if "code" in style:
            t = f"<code>{t}</code>"
        if "b" in style:
            t = f"<strong>{t}</strong>"
        if "i" in style:
            t = f"<em>{t}</em>"
        out.append(t)
    return "".join(out)


CSS = """html,body{margin:0;height:100%;background:#000;overflow:hidden;font-family:@font@,"Segoe UI",Arial,sans-serif}
.stage{position:absolute;left:50%;top:50%;width:1280px;height:720px;transform-origin:center center;transform:translate(-50%,-50%);background:@bg@;overflow:hidden}
.slide{display:none;position:absolute;left:0;top:0;width:1280px;height:720px;box-sizing:border-box;padding:38px 67px;color:@text@;background:@bg@}
.slide.on{display:block}
.slide h2{margin:0;font-size:40px;line-height:1.15;color:@title@;font-weight:700}
.slide h2.long{font-size:30px}
.rule{width:115px;height:7px;background:@accent@;margin:14px 0 26px 5px}
.body{position:absolute;left:67px;right:67px;top:172px;bottom:52px}
.body.cols{display:grid;grid-template-columns:1.25fr 1fr;gap:36px}
.body.tab{display:flex;flex-direction:column;gap:16px;overflow:auto}
.body.tab .text{flex:none;max-height:130px}
.text{overflow:auto;line-height:1.25}
.text p{margin:0 0 .4em}
.text h3{margin:0 0 .4em;color:@accent@;font-size:1.1em}
.text blockquote{margin:0 0 .4em 29px;color:@muted@;font-style:italic}
.text ul{list-style:none;margin:0;padding:0}
.text li{display:flex;gap:.5em;margin:0 0 .3em}
.text li.l1{margin-left:34px}
.text li.l2{margin-left:68px}
.text .m{flex:none;min-width:1.2em;color:@accent@}
.text pre{margin:0 0 .5em;padding:10px 14px;background:@cell@;overflow:auto;font-size:.8em;line-height:1.3}
.text code{font-family:Consolas,"Courier New",monospace;font-size:.9em}
figure{margin:0;display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:0;height:100%}
figure img{max-width:100%;max-height:100%;min-height:0;object-fit:contain}
figcaption{margin-top:8px;font-size:15px;font-style:italic;color:@muted@}
table{border-collapse:collapse;width:100%;font-size:16px}
th{background:@accent@;color:#fff;text-align:left}
th,td{border:1px solid @cell_alt@;padding:4px 10px}
tr:nth-child(odd) td{background:@cell@}
tr:nth-child(even) td{background:@cell_alt@}
.pn{position:absolute;right:60px;bottom:18px;font-size:15px;color:@muted@}
.notes{display:none}
.slide.title h1{position:absolute;left:67px;top:150px;width:1146px;margin:0;font-size:56px;line-height:1.1;color:@title@}
.slide.title h1.long{font-size:42px}
.slide.title .rule{position:absolute;left:72px;top:330px;margin:0}
.slide.title .sub{position:absolute;left:67px;top:362px;width:1146px;font-size:28px;color:@muted@;line-height:1.35}
.slide.title .sub p{margin:0 0 6px}
#bar{position:fixed;left:0;bottom:0;height:4px;background:@accent@;width:0;z-index:5}
#counter{position:fixed;right:12px;bottom:10px;font:13px sans-serif;color:#bbb;z-index:5;background:rgba(0,0,0,.45);padding:2px 8px;border-radius:10px}
#hint{position:fixed;left:12px;bottom:10px;font:12px sans-serif;color:#999;z-index:5}
#notes{display:none;position:fixed;left:0;right:0;bottom:0;max-height:34%;overflow:auto;box-sizing:border-box;padding:14px 26px;background:rgba(0,0,0,.85);color:#eee;font:18px/1.4 sans-serif;z-index:6;white-space:pre-wrap}
#notes.show{display:block}
@page{size:1280px 720px;margin:0}
@media print{html,body{background:#fff;overflow:visible;height:auto}.stage{position:static;transform:none!important;width:auto;height:auto;overflow:visible}.slide{display:block;position:relative;page-break-after:always;break-after:page}#bar,#counter,#hint,#notes{display:none!important}}
"""

JS = """(function(){
var slides=Array.prototype.slice.call(document.querySelectorAll('.slide'));
var stage=document.getElementById('stage'),counter=document.getElementById('counter'),bar=document.getElementById('bar'),notes=document.getElementById('notes');
var cur=0,showNotes=false;
function fit(){var k=Math.min(window.innerWidth/1280,window.innerHeight/720);stage.style.transform='translate(-50%,-50%) scale('+k+')';}
function show(n){
cur=Math.max(0,Math.min(slides.length-1,n));
slides.forEach(function(s,i){s.classList.toggle('on',i===cur);});
counter.textContent=(cur+1)+' / '+slides.length;
bar.style.width=((cur+1)/slides.length*100)+'%';
var a=slides[cur].querySelector('aside');
notes.textContent=a&&a.textContent?a.textContent:'(no notes for this slide)';
try{history.replaceState(null,'','#'+(cur+1));}catch(e){}
}
document.addEventListener('keydown',function(e){
if(e.ctrlKey||e.metaKey||e.altKey)return;
var k=e.key;
if(k==='ArrowRight'||k==='ArrowDown'||k==='PageDown'||k===' '||k==='Enter'){show(cur+1);e.preventDefault();}
else if(k==='ArrowLeft'||k==='ArrowUp'||k==='PageUp'||k==='Backspace'){show(cur-1);e.preventDefault();}
else if(k==='Home'){show(0);}
else if(k==='End'){show(slides.length-1);}
else if(k==='f'||k==='F'){if(document.fullscreenElement){document.exitFullscreen();}else if(document.documentElement.requestFullscreen){document.documentElement.requestFullscreen();}}
else if(k==='n'||k==='N'){showNotes=!showNotes;notes.className=showNotes?'show':'';}
});
document.addEventListener('click',function(e){if(e.target.closest&&e.target.closest('#notes'))return;show(e.clientX<window.innerWidth/3?cur-1:cur+1);});
window.addEventListener('resize',fit);
fit();
var start=parseInt(location.hash.slice(1),10);
show(isNaN(start)?0:start-1);
})();"""


def _css(theme: dict) -> str:
    css = CSS
    for key in ("bg", "title", "text", "accent", "muted", "cell", "cell_alt"):
        css = css.replace(f"@{key}@", "#" + theme[key])
    css = css.replace("@font@", theme["font"])
    return css + "".join(f".s{pt}{{font-size:{round(pt * 96 / 72, 1)}px}}" for pt in SIZES)


def _hash(text: str) -> str:
    """The CSP source for an inline element with exactly this text."""
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode("ascii") + "'"


def _body_html(blocks: list[dict]) -> str:
    parts = []
    for b in blocks:
        t = b["t"]
        if t == "p":
            parts.append(f"<p>{runs_html(b['runs'])}</p>")
        elif t == "h":
            parts.append(f"<h3>{runs_html(b['runs'])}</h3>")
        elif t == "quote":
            parts.append(f"<blockquote>{runs_html(b['runs'])}</blockquote>")
        elif t in ("ul", "ol"):
            items = "".join(f'<li class="l{level}"><span class="m">{esc(label)}</span><span>{runs_html(runs)}</span></li>'
                            for level, label, runs in labelled(b))
            parts.append(f"<ul>{items}</ul>")
        elif t == "code":
            parts.append("<pre><code>" + esc("\n".join(b["text"].split("\n")[:MAX_CODE_LINES])) + "</code></pre>")
    return "".join(parts)


def _table_html(block: dict) -> str:
    head = "".join(f"<th>{runs_html(r)}</th>" for r in block["header"])
    rows = "".join("<tr>" + "".join(f"<td>{runs_html(c)}</td>" for c in row) + "</tr>" for row in block["rows"])
    return f"<table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>"


def _image_html(path: Path, alt: str, budget: list[int]) -> str:
    data = Path(path).read_bytes()                     # check_image already limited the size
    budget[0] += len(data)
    if budget[0] > MAX_EMBED_BYTES:
        raise DocError(f"the pictures are too large to embed (limit {MAX_EMBED_BYTES // 1_000_000} MB in total)")
    mime = "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"
    caption = f"<figcaption>{esc(alt)}</figcaption>" if alt else ""
    return (f'<figure><img src="data:{mime};base64,{base64.b64encode(data).decode("ascii")}" '
            f'alt="{esc(alt)}">{caption}</figure>')


def _notes_html(spec: dict) -> str:
    return f'<aside class="notes">{esc(chr(10).join(spec["notes"]))}</aside>' if spec["notes"] else ""


def _title_slide(spec: dict, n: int, warnings: list[str]) -> str:
    title = spec["title"][:100]
    paragraphs = [b for b in spec["body"] if b["t"] == "p"]
    if len(paragraphs) != len(spec["body"]):
        warnings.append(f"title slide '{title[:30]}': only plain text is used under a # title")
    sub = "".join(f"<p>{runs_html(b['runs'])}</p>" for b in paragraphs[:3])
    return (f'<section class="slide title" aria-label="Slide {n}: {esc(title)}">'
            f'<h1 class="{"long" if len(title) > 40 else ""}">{esc(title)}</h1><div class="rule"></div>'
            f'<div class="sub">{sub}</div>{_notes_html(spec)}</section>')


def _content_slide(
    spec: dict, n: int, image_for, warnings: list[str], budget: list[int]
) -> str:
    title = spec["title"][:100]
    label = f"slide {n} ('{title[:30]}')"
    visuals = [b for b in spec["body"] if b["t"] in ("img", "table")]
    text_blocks = [b for b in spec["body"] if b["t"] not in ("img", "table")]
    visual = visuals[0] if visuals else None
    if len(visuals) > 1:
        warnings.append(f"{label}: only the first picture or table is used (give the others their own slide)")
    if visual and visual["t"] == "table" and len(visual["rows"]) > MAX_TABLE_ROWS - 1:
        raise DocError(f"{label}: a table holds at most {MAX_TABLE_ROWS} rows including the header "
                       f"(split it over two slides)")

    if visual and visual["t"] == "img" and text_blocks:
        layout, width_in, height_in = "cols", 6.4, 4.9
    elif visual and visual["t"] == "table" and text_blocks:
        layout, width_in, height_in = "tab", 11.9, 1.35
    elif visual:
        layout, width_in, height_in = "one", 11.9, 4.9
    else:
        layout, width_in, height_in = "one", 11.9, 4.9

    text = ""
    if text_blocks:
        size, fits = fit_size(text_items(text_blocks), width_in, height_in)
        if not fits:
            warnings.append(f"{label} has a lot of text and may overflow: split it into two slides")
        text = f'<div class="text s{size}">{_body_html(text_blocks)}</div>'

    extra = ""
    if visual and visual["t"] == "table":
        extra = _table_html(visual)
    elif visual:
        try:
            extra = _image_html(image_for(visual["path"]), visual["alt"], budget)
        except DocError as e:
            if "too large to embed" in str(e):
                raise
            warnings.append(str(e))
            extra = f'<div class="text s16"><p><em>[image not shown: {esc(visual["alt"] or visual["path"])}]</em></p></div>'
        except Exception as e:
            warnings.append(f"image {visual['path']}: could not be added ({type(e).__name__})")

    return (f'<section class="slide content" aria-label="Slide {n}: {esc(title)}">'
            f'<h2 class="{"long" if len(title) > 50 else ""}">{esc(title)}</h2><div class="rule"></div>'
            f'<div class="body {layout}">{text}{extra}</div>{_notes_html(spec)}<div class="pn">{n}</div></section>')


def build_html(
    blocks: list[dict],
    out: Path,
    *,
    theme: str,
    deck_title: str,
    meta_title: str,
    sources: list[tuple[str, str, str]],
    image_for: Callable[[str], Path],
) -> tuple[int, list[str]]:
    """Write a sealed slideshow. Returns (number of slides, warnings)."""
    if theme not in THEMES:
        raise DocError("theme must be one of: " + ", ".join(THEMES))
    slides = split_slides(blocks)
    if deck_title and not (slides and slides[0]["kind"] == "title"):
        slides.insert(0, {"kind": "title", "title": deck_title, "body": [], "notes": []})
    for k in range(0, len(sources), SOURCES_PER_SLIDE):              # built by the harness, never by the model
        chunk = sources[k:k + SOURCES_PER_SLIDE]
        items = [(0, [(f"[{sid}] {stitle} - {url}", frozenset())]) for sid, stitle, url in chunk]
        slides.append({"kind": "content", "title": "Sources" if k == 0 else f"Sources ({k // SOURCES_PER_SLIDE + 1})",
                       "body": [{"t": "ul", "items": items}], "notes": []})
    if not slides:
        raise DocError("the outline has no slides")
    if len(slides) > MAX_SLIDES:
        raise DocError(f"too many slides ({len(slides)}; the limit is {MAX_SLIDES})")

    warnings: list[str] = []
    budget = [0]
    parts = [_title_slide(s, n, warnings) if s["kind"] == "title" else _content_slide(s, n, image_for, warnings, budget)
             for n, s in enumerate(slides, start=1)]

    css = _css(THEMES[theme])
    csp = ("default-src 'none'; img-src data:; base-uri 'none'; form-action 'none'; "
           f"style-src {_hash(css)}; script-src {_hash(JS)}")
    page = (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{csp}">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<meta name="generator" content="{MARKER}">\n'
        '<meta name="danica-seal" content="">\n'
        f"<title>{esc(meta_title[:120])}</title>\n"
        f"<style>{css}</style>\n</head><body>\n"
        f'<div id="stage" class="stage">\n{chr(10).join(parts)}\n</div>\n'
        '<div id="bar"></div><div id="counter"></div>'
        '<div id="hint">arrows: move &middot; f: fullscreen &middot; n: notes</div><div id="notes"></div>\n'
        "<noscript>This slideshow needs JavaScript.</noscript>\n"
        f"<script>{JS}</script>\n</body></html>\n")
    sealed = seal_html(page)
    if len(sealed.encode("utf-8")) > MAX_FILE_BYTES:
        raise DocError("the slideshow is too large (reduce the number or size of pictures)")

    tmp = Path(out).with_name(Path(out).name + ".tmp")
    try:
        tmp.write_text(sealed, encoding="utf-8")
        os.replace(tmp, out)
    except OSError as e:
        raise DocError(f"could not write the slideshow ({type(e).__name__})")
    finally:
        if tmp.exists():
            tmp.unlink()
    return len(slides), warnings
