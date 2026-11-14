"""
htmlview.py - An interactive HTML viewer for a picture Danica made (chart, diagram, dashboard).

WHY: the chat used to paint the PNG inline (Sixel). Now it prints a clickable link instead, and
the link opens ONE self-contained .html page:
    chart with a data spec -> an interactive chart redrawn in the browser (hover values, click the
                              legend to hide a series), a Data tab (the numbers as a table) and an
                              Image tab (the PNG with zoom, pan and a download link)
    anything else          -> the Image tab only (zoom, pan, download)
The page is built from the PNG plus the chart's own JSON (the file charts/sales.json next to
charts/sales.png) that the chart tool already validated. Nothing from the model is ever put in the
page as HTML: all text goes in with textContent, and the data sits in a JSON block.

SAFETY (same rules as slideshow.py): the page is SEALED with this install's key, and it carries a
Content-Security-Policy that allows only the exact style and script written here (by hash), no
network and no other scripts. So the page can run nothing but this file's own code, and /open
only hands a viewer to the browser if the seal still verifies.

The page is written next to the picture as <name>.view.html. An existing file by that name is only
replaced when it is an earlier viewer (sealed); anything else is left alone and we report failure.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from html import escape
from pathlib import Path

from danica.charts import ChartError, TYPES, validate
from danica.docgen import MARKER
from danica.seal import is_sealed_html, seal_html

MAX_PNG = 5_000_000            # the same limit as the inline picture (imageview.MAX_BYTES)
MAX_SPEC = 200_000             # the same limit as charts.MAX_SPEC_BYTES
MAX_PAGE = 12_000_000          # a base64 PNG is ~4/3 of its size: refuse an absurd page


class ViewerError(Exception):
    """A problem whose message is safe to show the user."""


CSS = r"""
:root{--bg:#fff;--fg:#1d2330;--mute:#6b7280;--line:#d7dbe3;--card:#f5f6f9;--accent:#0072B2}
@media (prefers-color-scheme:dark){:root{--bg:#14161c;--fg:#e6e8ee;--mute:#9aa1b0;--line:#323746;--card:#1c2029;--accent:#56B4E9}}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,Segoe UI,sans-serif}
body{display:flex;flex-direction:column}
header{display:flex;gap:12px;align-items:center;padding:10px 16px;border-bottom:1px solid var(--line)}
header h1{font-size:15px;margin:0;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
nav{display:flex;gap:4px}
button,a.btn{font:inherit;color:var(--fg);background:var(--card);border:1px solid var(--line);border-radius:6px;padding:4px 10px;cursor:pointer;text-decoration:none}
button:hover,a.btn:hover{border-color:var(--accent)}
button.on{background:var(--accent);border-color:var(--accent);color:#fff}
main{flex:1;min-height:0;position:relative}
section{position:absolute;inset:0;overflow:auto;padding:12px 16px;display:none}
section.on{display:block}
#legend{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 8px}
#legend button{display:flex;align-items:center;gap:6px}
#legend button.off{opacity:.4;text-decoration:line-through}
.sw{width:12px;height:12px;border-radius:3px;display:inline-block}
svg{width:100%;max-width:1100px;height:auto;display:block;margin:0 auto}
svg text{fill:var(--fg);font:12px system-ui,sans-serif}
svg .mute{fill:var(--mute)}
svg .grid{stroke:var(--line);stroke-width:1}
svg .axis{stroke:var(--mute);stroke-width:1}
svg .hit{cursor:default}
svg .hit:hover{opacity:.75;stroke:var(--fg);stroke-width:1.5}
table{border-collapse:collapse;margin:0 auto}
th,td{border:1px solid var(--line);padding:4px 10px;text-align:left}
th{background:var(--card);cursor:pointer;user-select:none}
td.n{text-align:right;font-variant-numeric:tabular-nums}
#tip{position:fixed;pointer-events:none;background:var(--card);border:1px solid var(--line);border-radius:6px;padding:4px 8px;display:none;z-index:9;max-width:320px}
#sec-image{padding:0}
#stage{position:absolute;inset:0;overflow:hidden;cursor:grab;padding:0}
svg .title{font-weight:600;font-size:15px}
#stage.drag{cursor:grabbing}
#pic{transform-origin:0 0;max-width:100%;display:block;user-select:none;-webkit-user-drag:none}
#tools{position:absolute;right:16px;top:12px;display:flex;gap:4px;z-index:2}
"""

JS = r"""
(function(){"use strict";
var $=function(i){return document.getElementById(i)};
var spec=null;try{spec=JSON.parse($("spec").textContent)}catch(e){}
var PAL=["#0072B2","#E69F00","#009E73","#D55E00","#CC79A7","#56B4E9","#F0E442","#999999"];
var NS="http://www.w3.org/2000/svg",W=900,Hh=520,M={l:72,r:24,t:48,b:72},PW=W-M.l-M.r,PH=Hh-M.t-M.b;
function clean(s){return String(s).replace(/\\\$/g,"$")}
function num(v){return v.toLocaleString(undefined,{maximumFractionDigits:4})}
function S(tag,attrs,parent,text){var n=document.createElementNS(NS,tag);for(var k in attrs)n.setAttribute(k,attrs[k]);if(text!==undefined)n.textContent=text;if(parent)parent.appendChild(n);return n}
function E(tag,parent,text,cls){var n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;if(parent)parent.appendChild(n);return n}
var tip=$("tip");
function showTip(e,lines){tip.textContent="";lines.forEach(function(l,i){var d=E("div",tip,l);if(i===0)d.style.fontWeight="600"});tip.style.display="block";moveTip(e)}
function moveTip(e){var x=e.clientX+14,y=e.clientY+14;if(x+tip.offsetWidth>innerWidth)x=e.clientX-tip.offsetWidth-14;if(y+tip.offsetHeight>innerHeight)y=e.clientY-tip.offsetHeight-14;tip.style.left=Math.max(4,x)+"px";tip.style.top=Math.max(4,y)+"px"}
function hover(n,lines){n.setAttribute("class",(n.getAttribute("class")||"")+" hit");n.addEventListener("mouseenter",function(e){showTip(e,lines)});n.addEventListener("mousemove",moveTip);n.addEventListener("mouseleave",function(){tip.style.display="none"})}
function nice(min,max){if(min===max){max=min+1}var span=max-min,raw=span/6,p=Math.pow(10,Math.floor(Math.log10(raw))),r=raw/p,step=p*(r>=7.5?10:r>=3.5?5:r>=1.5?2:1),lo=Math.floor(min/step+1e-9)*step,hi=Math.ceil(max/step-1e-9)*step,t=[];for(var v=lo;v<=hi+step/2;v+=step)t.push(+v.toFixed(10));return{lo:lo,hi:hi,ticks:t}}
var hidden={};
/* ---------- tabs ---------- */
var tabs={};
function show(name){for(var k in tabs){tabs[k].btn.className=k===name?"on":"";tabs[k].sec.className=k===name?"on":""}}
function addTab(name,label){var btn=E("button",$("nav"),label);var sec=$("sec-"+name);tabs[name]={btn:btn,sec:sec};btn.addEventListener("click",function(){show(name)})}
/* ---------- chart ---------- */
function cats(){return spec.labels||[]}
function vis(){return spec.series.map(function(s,i){return{s:s,i:i}}).filter(function(o){return !hidden[o.i]})}
function legend(box){box.textContent="";var items=spec.type==="pie"?cats().map(function(l,i){return{n:l,i:i}}):spec.series.map(function(s,i){return{n:s.name,i:i}});
 if(items.length<2&&spec.type!=="pie")return;
 items.forEach(function(o){var b=E("button",box);b.className=hidden[o.i]?"off":"";var sw=E("span",b,"","sw");sw.style.background=PAL[o.i%PAL.length];E("span",b,clean(o.n));b.addEventListener("click",function(){hidden[o.i]=!hidden[o.i];drawChart()})})}
function axes(svg,y,xlabel,ylabel){
 y.ticks.forEach(function(t){var py=M.t+PH-(t-y.lo)/(y.hi-y.lo)*PH;S("line",{x1:M.l,x2:M.l+PW,y1:py,y2:py,"class":"grid"},svg);S("text",{x:M.l-8,y:py+4,"text-anchor":"end","class":"mute"},svg,num(t))});
 S("line",{x1:M.l,x2:M.l,y1:M.t,y2:M.t+PH,"class":"axis"},svg);S("line",{x1:M.l,x2:M.l+PW,y1:M.t+PH,y2:M.t+PH,"class":"axis"},svg);
 if(xlabel)S("text",{x:M.l+PW/2,y:Hh-10,"text-anchor":"middle"},svg,clean(xlabel));
 if(ylabel){S("text",{transform:"translate(16,"+(M.t+PH/2)+") rotate(-90)","text-anchor":"middle"},svg,clean(ylabel))}}
function xcats(svg){var n=cats().length,band=PW/Math.max(1,n),every=Math.ceil(n/14);cats().forEach(function(l,i){if(i%every)return;S("text",{x:M.l+band*(i+.5),y:M.t+PH+18,"text-anchor":"middle","class":"mute"},svg,clean(l).slice(0,14))});return band}
function drawChart(){
 var box=$("legend"),svg=$("chart");legend(box);svg.textContent="";
 S("text",{x:W/2,y:26,"text-anchor":"middle","class":"title"},svg,clean(spec.title||""));
 var type=spec.type,v=vis(),i,j;
 if(type==="pie"){var vals=spec.series[0].values,tot=0;vals.forEach(function(x,k){if(!hidden[k])tot+=x});
  var cx=W/2,cy=Hh/2+14,r=190,a=-Math.PI/2;
  vals.forEach(function(x,k){if(hidden[k]||tot<=0||x<=0)return;var b=a+x/tot*2*Math.PI,large=b-a>Math.PI?1:0,full=x/tot>0.9999;
   var d=full?"M"+(cx-r)+","+cy+"a"+r+","+r+" 0 1,0 "+2*r+",0a"+r+","+r+" 0 1,0 "+(-2*r)+",0":"M"+cx+","+cy+"L"+(cx+r*Math.cos(a))+","+(cy+r*Math.sin(a))+"A"+r+","+r+" 0 "+large+",1 "+(cx+r*Math.cos(b))+","+(cy+r*Math.sin(b))+"Z";
   var p=S("path",{d:d,fill:PAL[k%PAL.length],stroke:"var(--bg)","stroke-width":2},svg);hover(p,[clean(cats()[k]),num(x)+" ("+(x/tot*100).toFixed(1)+"%)"]);a=b});return}
 if(type==="scatter"){var xs=[],ys=[];v.forEach(function(o){o.s.points.forEach(function(p){xs.push(p[0]);ys.push(p[1])})});if(!xs.length)return;
  var xn=nice(Math.min.apply(null,xs),Math.max.apply(null,xs)),yn=nice(Math.min.apply(null,ys),Math.max.apply(null,ys));axes(svg,yn,spec.x_label,spec.y_label);
  xn.ticks.forEach(function(t){var px=M.l+(t-xn.lo)/(xn.hi-xn.lo)*PW;S("text",{x:px,y:M.t+PH+18,"text-anchor":"middle","class":"mute"},svg,num(t))});
  v.forEach(function(o){o.s.points.forEach(function(p){var c=S("circle",{cx:M.l+(p[0]-xn.lo)/(xn.hi-xn.lo)*PW,cy:M.t+PH-(p[1]-yn.lo)/(yn.hi-yn.lo)*PH,r:5,fill:PAL[o.i%PAL.length],"fill-opacity":.8},svg);hover(c,[clean(o.s.name),"x: "+num(p[0]),"y: "+num(p[1])])})});return}
 /* bar, line, area share categorical x */
 var lo=0,hi=0,stack=type==="area",n=cats().length,cum=[];
 for(j=0;j<n;j++){var pos=0;v.forEach(function(o){var x=o.s.values[j]||0;if(stack){pos+=x;hi=Math.max(hi,pos)}else{hi=Math.max(hi,x);lo=Math.min(lo,x)}})}
 if(type==="line"){lo=Infinity;hi=-Infinity;v.forEach(function(o){o.s.values.forEach(function(x){lo=Math.min(lo,x);hi=Math.max(hi,x)})});if(!isFinite(lo)){lo=0;hi=1}}
 var yn2=nice(lo,hi);axes(svg,yn2,spec.x_label,spec.y_label);var band=xcats(svg);
 function Y(x){return M.t+PH-(x-yn2.lo)/(yn2.hi-yn2.lo)*PH}
 if(type==="bar"){var gw=band*.8,bw=gw/Math.max(1,v.length);v.forEach(function(o,k){o.s.values.forEach(function(x,j2){var y0=Y(0),y1=Y(x);var rc=S("rect",{x:M.l+band*j2+(band-gw)/2+k*bw,y:Math.min(y0,y1),width:Math.max(1,bw-2),height:Math.max(1,Math.abs(y0-y1)),fill:PAL[o.i%PAL.length]},svg);hover(rc,[clean(cats()[j2]),clean(o.s.name)+": "+num(x)])})});return}
 if(type==="line"){v.forEach(function(o){var pts=o.s.values.map(function(x,j2){return(M.l+band*(j2+.5))+","+Y(x)});S("polyline",{points:pts.join(" "),fill:"none",stroke:PAL[o.i%PAL.length],"stroke-width":2.5},svg);
  o.s.values.forEach(function(x,j2){var c=S("circle",{cx:M.l+band*(j2+.5),cy:Y(x),r:4.5,fill:PAL[o.i%PAL.length]},svg);hover(c,[clean(cats()[j2]),clean(o.s.name)+": "+num(x)])})});return}
 /* area (stacked) */
 var base=[];for(j=0;j<n;j++)base.push(0);
 v.forEach(function(o){var top=o.s.values.map(function(x,j2){return base[j2]+(x||0)}),d="M"+(M.l+band*.5)+","+Y(base[0]);
  top.forEach(function(t,j2){d+="L"+(M.l+band*(j2+.5))+","+Y(t)});for(j=n-1;j>=0;j--)d+="L"+(M.l+band*(j+.5))+","+Y(base[j]);d+="Z";
  var p=S("path",{d:d,fill:PAL[o.i%PAL.length],"fill-opacity":.75},svg);
  top.forEach(function(t,j2){var c=S("circle",{cx:M.l+band*(j2+.5),cy:Y(t),r:5,fill:"transparent"},svg);hover(c,[clean(cats()[j2]),clean(o.s.name)+": "+num(o.s.values[j2]||0)])});base=top})}
/* ---------- data table (click a header to sort) ---------- */
function table(host,cols,rows){host.textContent="";var t=E("table",host),hd=E("tr",E("thead",t)),body=E("tbody",t),dir={};
 function fill(rs){body.textContent="";rs.forEach(function(r){var tr=E("tr",body);r.forEach(function(c){var td=E("td",tr,typeof c==="number"?num(c):clean(c));if(typeof c==="number")td.className="n"})})}
 cols.forEach(function(c,k){var th=E("th",hd,clean(c));th.addEventListener("click",function(){dir[k]=!dir[k];rows.sort(function(a,b){var x=a[k],y=b[k],o=typeof x==="number"&&typeof y==="number"?x-y:String(x).localeCompare(String(y),undefined,{numeric:true});return dir[k]?o:-o});fill(rows)})});fill(rows)}
function dataRows(){var cols,rows=[];
 if(spec.type==="scatter"){cols=["Series","x","y"];spec.series.forEach(function(s){s.points.forEach(function(p){rows.push([s.name,p[0],p[1]])})})}
 else{cols=[spec.x_label||"Label"].concat(spec.series.map(function(s){return s.name}));cats().forEach(function(l,j){rows.push([l].concat(spec.series.map(function(s){return s.values[j]})))})}
 return{cols:cols,rows:rows}}
/* ---------- image: zoom + pan ---------- */
var stage=$("stage"),pic=$("pic"),sc=1,tx=0,ty=0;
function apply(){pic.style.transform="translate("+tx+"px,"+ty+"px) scale("+sc+")"}
function zoomAt(px,py,k){var ns=Math.min(20,Math.max(.2,sc*k));tx=px-(px-tx)*ns/sc;ty=py-(py-ty)*ns/sc;sc=ns;apply()}
function center(k){var r=stage.getBoundingClientRect();zoomAt(r.width/2,r.height/2,k)}
function reset(){sc=1;tx=0;ty=0;apply()}
stage.addEventListener("wheel",function(e){e.preventDefault();var r=stage.getBoundingClientRect();zoomAt(e.clientX-r.left,e.clientY-r.top,Math.exp(-e.deltaY*.0015))},{passive:false});
var drag=null;
stage.addEventListener("pointerdown",function(e){drag={x:e.clientX-tx,y:e.clientY-ty};stage.setPointerCapture(e.pointerId);stage.className="drag"});
stage.addEventListener("pointermove",function(e){if(drag){tx=e.clientX-drag.x;ty=e.clientY-drag.y;apply()}});
stage.addEventListener("pointerup",function(){drag=null;stage.className=""});
stage.addEventListener("dblclick",reset);
$("zin").addEventListener("click",function(){center(1.25)});$("zout").addEventListener("click",function(){center(.8)});$("zfit").addEventListener("click",reset);
document.addEventListener("keydown",function(e){if(!tabs.image||tabs.image.sec.className!=="on")return;if(e.key==="+"||e.key==="=")center(1.25);else if(e.key==="-")center(.8);else if(e.key==="0")reset()});
/* ---------- start ---------- */
if(spec){addTab("chart","Chart");
 if(spec.type==="table"){$("legend").style.display="none";$("chart").style.display="none";table($("chart-table"),spec.columns,spec.rows.map(function(r){return r.slice()}))}
 else{drawChart();var d=dataRows();table($("data-table"),d.cols,d.rows);addTab("data","Data")}}
addTab("image","Image");show(spec?"chart":"image");
if(!spec)$("nav").style.display="none";
})();
"""


def _hash(text: str) -> str:
    """The CSP source for an inline element with exactly this text."""
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode("ascii") + "'"


def _load_spec(png: Path):
    """The cleaned chart spec stored next to the picture, or None (diagrams, dashboards, no json)."""
    side = png.with_suffix(".json")
    try:
        if not side.is_file() or side.stat().st_size > MAX_SPEC:
            return None
        data = json.loads(side.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or data.get("type") not in TYPES:
            return None
        return validate(data)                       # the same checks the chart tool ran
    except (OSError, ValueError, ChartError):
        return None


def viewer_path(png: Path) -> Path:
    """Where the viewer for this picture lives."""
    return png.with_name(png.stem + ".view.html")


def build_viewer(png: Path) -> Path:
    """Write (or refresh) the sealed viewer next to `png` and return its path. The caller has
    ALREADY checked the picture (imageview.check_image): inside the workspace, really a PNG/JPEG."""
    png = Path(png)
    out = viewer_path(png)
    if out.exists() and not is_sealed_html(out):
        raise ViewerError(f"{out.name} already exists and was not made by Danica, so it was left alone")
    try:
        raw = png.read_bytes()
    except OSError as exc:
        raise ViewerError(f"could not read the picture ({type(exc).__name__})")
    if len(raw) > MAX_PNG:
        raise ViewerError("the picture is too large for a viewer")
    mime = "image/jpeg" if raw[:3] == b"\xff\xd8\xff" else "image/png"
    uri = f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")
    spec = _load_spec(png)
    spec_json = json.dumps(spec, ensure_ascii=False).replace("<", "\\u003c").replace("&", "\\u0026")
    title = escape(png.stem.replace("_", " ").replace("-", " ")[:80] or "picture")

    csp = ("default-src 'none'; img-src data:; base-uri 'none'; form-action 'none'; "
           f"style-src {_hash(CSS)}; script-src {_hash(JS)}")
    page = (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{csp}">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<meta name="generator" content="{MARKER}">\n'
        '<meta name="danica-seal" content="">\n'
        f"<title>{title}</title>\n<style>{CSS}</style>\n</head><body>\n"
        f'<header><h1>{title}</h1><nav id="nav"></nav>'
        f'<a class="btn" id="dl" download="{escape(png.name)}" href="{uri}">Download image</a></header>\n'
        '<main>\n'
        '<section id="sec-chart"><div id="legend"></div>'
        '<svg id="chart" viewBox="0 0 900 520" role="img"></svg><div id="chart-table"></div></section>\n'
        '<section id="sec-data"><div id="data-table"></div></section>\n'
        '<section id="sec-image"><div id="tools"><button id="zin">+</button>'
        '<button id="zout">&minus;</button><button id="zfit">Fit</button></div>'
        f'<div id="stage"><img id="pic" alt="{title}" src="{uri}"></div></section>\n'
        "</main>\n"
        '<div id="tip"></div>\n'
        f'<script type="application/json" id="spec">{spec_json}</script>\n'
        "<noscript>This viewer needs JavaScript (the picture is still available through Download image).</noscript>\n"
        f"<script>{JS}</script>\n</body></html>\n")
    sealed = seal_html(page)
    if len(sealed.encode("utf-8")) > MAX_PAGE:
        raise ViewerError("the viewer would be too large")

    tmp = out.with_name(out.name + ".tmp")
    try:
        tmp.write_text(sealed, encoding="utf-8")
        os.replace(tmp, out)
    except OSError as exc:
        raise ViewerError(f"could not write the viewer ({type(exc).__name__})")
    finally:
        if tmp.exists():
            tmp.unlink()
    return out
