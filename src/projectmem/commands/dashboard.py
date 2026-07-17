"""pjm dashboard — the cross-project GLOBAL view.

Reads every registered project's own `.projectmem/` at render time, aggregates
grades / issues / fixes / savings, and writes a single global HTML plus one
freshly-generated per-project dashboard each. Clicking a card opens that repo's
own dashboard — generated live from its current files, never a stale snapshot.

Design invariant (mirrors plan.md): GLOBAL VIEW, NOT GLOBAL STORE. Nothing is
copied into a central database — projects stay in their own folders; this only
reads them. Discovery is the opt-in registry (`~/.projectmem/projects.json`),
never a filesystem crawl.
"""
from __future__ import annotations

import json
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

import typer

from projectmem.models import Event
from projectmem.storage import read_events, registered_projects
from projectmem.commands.score import calculate_score
from projectmem.commands import visualize as visualize_command

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_GRADE_POINTS = {"A+": 12, "A": 11, "A-": 10, "B+": 9, "B": 8, "B-": 7,
                 "C+": 6, "C": 5, "C-": 4, "D+": 3, "D": 2, "D-": 1, "F": 0}
_POINTS_GRADE = {v: k for k, v in _GRADE_POINTS.items()}


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _relative(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    if secs < 3600:
        return f"{max(1, int(secs // 60))}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    if secs < 86400 * 7:
        return f"{int(secs // 86400)}d ago"
    if secs < 86400 * 30:
        return f"{int(secs // 86400 // 7)}w ago"
    return f"{int(secs // 86400 // 30)}mo ago"


def _spark(times: list[datetime], n: int = 8) -> list[int]:
    if not times:
        return [0] * n
    lo, hi = times[0], times[-1]
    span = (hi - lo).total_seconds() or 1.0
    buckets = [0] * n
    for t in times:
        idx = min(n - 1, int((t - lo).total_seconds() / span * n))
        buckets[idx] += 1
    return buckets


def _project_stats(proj: Path) -> dict | None:
    try:
        events: list[Event] = read_events(proj)
    except Exception:
        return None
    score = calculate_score([e.__dict__ for e in events])
    opened, closed = set(), set()
    fixes = 0
    times: list[datetime] = []
    for e in events:
        if e.type == "issue" and getattr(e, "issue_id", None):
            opened.add(e.issue_id)
        elif e.type == "fix":
            fixes += 1
            if getattr(e, "issue_id", None):
                closed.add(e.issue_id)
        t = _parse_ts(getattr(e, "timestamp", None))
        if t is not None:
            times.append(t)
    times.sort()
    open_issues = len(opened - closed)
    return {
        "name": proj.name,
        "path": str(proj),
        "grade": score.get("grade", "F"),
        "events": len(events),
        "fixes": fixes,
        "open": open_issues,
        "tokens": score.get("value", {}).get("tokens_saved", 0),
        "usd": score.get("value", {}).get("usd_saved", 0),
        "hours": score.get("value", {}).get("debugging_hours_saved", 0),
        "failed": max(0, score.get("components", {}).get("failed_approaches", 0)),
        "last": _relative(times[-1] if times else None),
        "spark": _spark(times),
        "hot": open_issues >= 2,
        "_times": times,
    }


def _monthly_series(all_times: list[datetime], n: int = 8) -> dict:
    now = datetime.now(timezone.utc)
    months: list[tuple[int, int]] = []
    for i in range(n - 1, -1, -1):
        yy, mm = now.year, now.month - i
        while mm <= 0:
            mm += 12
            yy -= 1
        months.append((yy, mm))
    counts = {ym: 0 for ym in months}
    for t in all_times:
        key = (t.year, t.month)
        if key in counts:
            counts[key] += 1
    return {
        "labels": [_MONTHS[mm - 1] for (_, mm) in months],
        "values": [counts[ym] for ym in months],
    }


def _avg_grade(stats: list[dict]) -> str:
    pts = [_GRADE_POINTS.get(s["grade"], 0) for s in stats]
    if not pts:
        return "—"
    avg = round(sum(pts) / len(pts))
    return _POINTS_GRADE.get(avg, "C")


# Injected into every per-project dashboard so users can click back to the
# global view. Prepends a link to the top of the sidebar nav; falls back to a
# floating pill if the sidebar structure isn't found.
_BACK_SCRIPT = (
    "<script>(function(){var f=document.querySelector('[data-panel]');"
    "var a=document.createElement('a');a.href='index.html';a.textContent='\\u2190 All projects';"
    "if(f&&f.parentNode){a.style.cssText='display:block;color:#9DB6D8;text-decoration:none;"
    "font:600 12px system-ui;padding:9px 18px;margin-bottom:4px;border-bottom:1px solid rgba(255,255,255,.08)';"
    "f.parentNode.insertBefore(a,f);}else{a.style.cssText='position:fixed;top:11px;left:12px;z-index:99999;"
    "background:#0C1A34;color:#fff;padding:8px 13px;border-radius:9px;font:600 12px system-ui;"
    "text-decoration:none;box-shadow:0 4px 14px rgba(0,0,0,.25)';document.body.appendChild(a);}})();</script>"
)


def _back_linked(html: str) -> str:
    return html.replace("</body>", _BACK_SCRIPT + "</body>", 1) if "</body>" in html else html


def _inject_back_link(path: Path) -> None:
    try:
        html = path.read_text(encoding="utf-8")
    except OSError:
        return
    path.write_text(_back_linked(html), encoding="utf-8")


def _aggregate(stats: list[dict], all_times: list[datetime], live: bool = False) -> dict:
    """Build the global payload from per-project stats. `_times` is stripped so
    the JSON stays small; card hrefs are already set by the caller. `live` flips
    the page between the honest snapshot label (static) and the live-server one."""
    return {
        "live": live,
        "generated": datetime.now(timezone.utc).isoformat(),
        "agg": {
            "projects": len(stats),
            "events": sum(s["events"] for s in stats),
            "fixes": sum(s["fixes"] for s in stats),
            "open": sum(s["open"] for s in stats),
            "tokens": sum(s["tokens"] for s in stats),
            "usd": round(sum(s["usd"] for s in stats), 2),
            "prevented": sum(s["failed"] for s in stats),
            "avg_grade": _avg_grade(stats),
        },
        "series": _monthly_series(all_times),
        "projects": [{k: v for k, v in s.items() if k != "_times"} for s in stats],
    }


def _global_html(live: bool = False) -> tuple[str, int]:
    """Build the global index HTML from the CURRENT registry, read live.

    Cheap: only aggregates each project's memory — it does NOT generate the
    per-project dashboards (those are written once in static mode, or rendered
    on demand when a card is clicked in --serve mode). Card hrefs point at
    p<i>.html where i is the registry index, so both modes line up."""
    stats: list[dict] = []
    all_times: list[datetime] = []
    for i, proj in enumerate(registered_projects()):
        s = _project_stats(proj)
        if s is None:
            continue
        all_times.extend(s.get("_times", []))
        s["href"] = f"p{i}.html"
        stats.append(s)
    payload = _aggregate(stats, all_times, live=live)
    return GLOBAL_TEMPLATE.replace("{{DASH_DATA}}", json.dumps(payload)), len(stats)


def run(
    output: Path | None = None,
    open_browser: bool = True,
    serve: bool = False,
    port: int = 8787,
) -> None:
    if not registered_projects():
        typer.echo(
            "No projects registered yet. Run `pjm init` in a repo first — "
            "each init adds the project to ~/.projectmem/projects.json."
        )
        raise typer.Exit(0)

    if serve:
        _serve(port, open_browser)
        return

    # ── static (serverless) mode: write a self-contained snapshot to disk ──
    out_dir = Path(output) if output else (Path.home() / ".projectmem" / "dashboard")
    out_dir.mkdir(parents=True, exist_ok=True)
    for i, proj in enumerate(registered_projects()):
        if _project_stats(proj) is None:
            continue
        try:
            p_file = out_dir / f"p{i}.html"
            visualize_command.run(root=proj, output=p_file, open_browser=False)
            _inject_back_link(p_file)
        except Exception:
            pass  # card still shows in the index; just won't open

    html, count = _global_html()
    if count == 0:
        typer.echo("Registered projects have no readable memory yet.")
        raise typer.Exit(0)
    index = out_dir / "index.html"
    index.write_text(html, encoding="utf-8")
    typer.echo(f"Global dashboard: {index}  ({count} projects)")
    if open_browser:
        webbrowser.open(index.resolve().as_uri())


def _serve(port: int, open_browser: bool) -> None:
    """Ephemeral live mode. A tiny local HTTP server renders the global view
    and each project's dashboard fresh on every request, so Refresh re-reads
    your files. No daemon, no state — it exists only while this command runs,
    and Ctrl+C ends it cleanly."""
    import re
    import signal
    import tempfile
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    proj_re = re.compile(r"^/p(\d+)\.html$")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the terminal quiet
            pass

        def _send(self, html: str, code: int = 200):
            body = html.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")  # always fresh
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                html, _ = _global_html(live=True)  # every project re-read, live
                self._send(html)
                return
            m = proj_re.match(path)
            if m:
                projects = registered_projects()
                idx = int(m.group(1))
                if 0 <= idx < len(projects):
                    try:
                        with tempfile.TemporaryDirectory() as td:
                            tmp = Path(td) / "p.html"
                            visualize_command.run(
                                root=projects[idx], output=tmp, open_browser=False
                            )
                            self._send(_back_linked(tmp.read_text(encoding="utf-8")))
                    except Exception:
                        self._send("<h1>Could not render this project.</h1>", 500)
                    return
            self._send("<h1>404</h1>", 404)

    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        typer.echo(
            f"Port {port} is busy. Pick another with "
            "`pjm dashboard --serve --port <N>`."
        )
        raise typer.Exit(1)
    server.daemon_threads = True  # in-flight renders never block shutdown

    # Explicitly convert Ctrl+C (SIGINT) and `kill` (SIGTERM) into a clean stop.
    # Setting SIGINT here also overrides the SIG_IGN a shell hands to background
    # jobs, so the server is always interruptible.
    def _stop(*_):
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)
    except (ValueError, OSError):
        pass  # not the main thread (e.g. under a test harness) — Ctrl+C still works

    url = f"http://127.0.0.1:{port}"
    typer.echo("")
    typer.echo(f"  ● Live global dashboard  →  {url}")
    typer.echo("    Reads every project's files fresh on each load; the Refresh")
    typer.echo("    button pulls the latest. Ephemeral — no background daemon.")
    typer.echo("    Press Ctrl+C to stop.")
    typer.echo("")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        typer.echo("\n  Stopped. Nothing left running.")
    finally:
        server.shutdown()
        server.server_close()


GLOBAL_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>projectmem — Global Dashboard</title>
<style>
:root{--ink:#182740;--muted:#7488A3;--line:#E1E8F3;--card:#fff}
*{box-sizing:border-box}
body{margin:0;font-family:'Inter',system-ui,-apple-system,sans-serif;color:var(--ink);
  background:linear-gradient(180deg,#EEF3FB,#E6EDF7);padding:22px}
.wrap{max-width:1180px;margin:0 auto}
code{font-family:ui-monospace,Menlo,monospace;background:#DFE7F3;padding:1px 5px;border-radius:4px;font-size:.85em}
.top{display:flex;justify-content:space-between;align-items:center;gap:14px;flex-wrap:wrap;
  background:linear-gradient(120deg,#0C1A34,#152A52);color:#fff;padding:16px 20px;border-radius:13px;
  box-shadow:0 8px 24px rgba(12,26,52,.18)}
.brand{display:flex;align-items:center;gap:13px}
.logo{font-size:24px;color:#5AA0FF;filter:drop-shadow(0 0 10px rgba(90,160,255,.5))}
.title{font-size:18px;font-weight:800}
.chip{font-size:10.5px;font-weight:700;background:linear-gradient(90deg,#2E7BF6,#17B0A0);
  padding:2px 10px;border-radius:20px;margin-left:7px;text-transform:uppercase}
.tag{font-size:12px;color:#A9C2E6;margin-top:3px}
.tr{display:flex;flex-direction:column;align-items:flex-end;gap:6px}
.live{display:flex;align-items:center;gap:6px;font-size:10.5px;color:#7FE3C8;
  background:rgba(23,176,160,.14);border:1px solid rgba(23,176,160,.35);padding:3px 10px;border-radius:20px;font-weight:600}
.dot{width:7px;height:7px;border-radius:50%;background:#28E0B8;box-shadow:0 0 8px #28E0B8}
.cmd{font-family:ui-monospace,Menlo,monospace;font-size:12px;color:#8FBEF2;background:#08132A;
  padding:5px 12px;border-radius:8px;border:1px solid #23406B}
.trb{display:flex;align-items:center;gap:8px}
.refresh{font:600 12px system-ui;color:#fff;background:#1F6FEB;border:0;padding:6px 13px;border-radius:8px;cursor:pointer}
.refresh:hover{background:#2E7BF6}
.gen{font-size:10px;color:#7E97BC}
.stats{display:grid;grid-template-columns:repeat(6,1fr);gap:11px;margin:16px 0}
.stat{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:13px 15px 14px;
  position:relative;overflow:hidden;box-shadow:0 2px 8px rgba(20,40,80,.05)}
.stat i{position:absolute;top:0;left:0;right:0;height:3px}
.stat .k{font-size:9.5px;font-weight:700;letter-spacing:.7px;color:var(--muted);text-transform:uppercase}
.stat .v{font-size:27px;font-weight:800;margin-top:5px;line-height:1}
.stat .t{font-size:10.5px;color:#93A2B8;margin-top:4px}
.charts{display:grid;grid-template-columns:1.6fr 1fr;gap:13px;margin-bottom:16px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:13px;padding:15px 17px;box-shadow:0 2px 10px rgba(20,40,80,.05)}
.h{font-size:13px;font-weight:700;color:#243B5A;margin-bottom:11px}
.h2{margin:6px 0 11px;font-size:14.5px;color:#152A4A}
.sub{font-size:10.5px;font-weight:500;color:#93A2B8}
#line{width:100%;height:150px;display:block}
.grades{display:flex;flex-direction:column;gap:8px}
.grow{display:flex;align-items:center;gap:9px;font-size:12px}
.glabel{width:24px;font-weight:800;text-align:center;border-radius:6px;color:#fff;font-size:11px;padding:3px 0}
.gbar{height:13px;border-radius:7px}.gcount{color:var(--muted);font-size:11px}
.attn{margin-top:13px;padding-top:11px;border-top:1px dashed var(--line);font-size:11px;color:#C0492B}
.attn b{font-weight:700}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:13px;padding:15px 14px 13px;
  cursor:pointer;transition:transform .12s,box-shadow .12s;position:relative;overflow:hidden;text-decoration:none;color:inherit;display:block}
.card i{position:absolute;top:0;left:0;right:0;height:3px}
.card:hover{transform:translateY(-3px);box-shadow:0 10px 24px rgba(20,40,80,.13)}
.card.hot{border-color:#F0C4B7}
.card.dead{cursor:default;opacity:.75}
.open{position:absolute;bottom:11px;right:13px;font-size:10px;font-weight:700;color:#2E7BF6;opacity:0;transition:opacity .12s;background:#EAF2FD;padding:2px 7px;border-radius:12px}
.card:hover .open{opacity:1}
.crow{display:flex;justify-content:space-between;align-items:flex-start;gap:8px}
.cname{font-weight:700;font-size:13px;color:#17263C;word-break:break-word;line-height:1.25;padding-right:6px}
.badge{flex:0 0 auto;width:32px;height:27px;border-radius:8px;color:#fff;font-weight:800;font-size:12.5px;
  display:flex;align-items:center;justify-content:center;box-shadow:0 2px 6px rgba(0,0,0,.12)}
.cmeta{display:flex;gap:13px;margin-top:10px;font-size:11px;color:var(--muted)}
.cmeta b{color:#26374F}.oi{color:#E0562F;font-weight:700}
.spark{margin-top:9px;display:block}.last{font-size:10px;color:#A2B0C4;margin-top:8px}
.gotcha{margin-bottom:13px}.got{display:flex;gap:9px;font-size:12px;color:#3A4A63;align-items:baseline;margin-bottom:7px}
.got .lib{font-family:ui-monospace,Menlo,monospace;font-size:11px;background:#E7F0FE;color:#2E7BF6;padding:1.5px 7px;border-radius:5px;flex:0 0 auto;font-weight:600}
.foot{font-size:11px;color:var(--muted);text-align:center}.foot a{color:#2E7BF6;text-decoration:none;font-weight:600}.foot a:hover{text-decoration:underline}
@media(max-width:900px){.stats{grid-template-columns:repeat(3,1fr)}.charts{grid-template-columns:1fr}.grid{grid-template-columns:repeat(2,1fr)}}
</style></head>
<body><div class="wrap">
  <div class="top">
    <div class="brand"><span class="logo">&#9672;</span><div>
      <div class="title">projectmem <span class="chip">Global</span></div>
      <div class="tag">Every project in one view — none of them ever leave their folder.</div>
    </div></div>
    <div class="tr"><span class="live" id="pillwrap"><span class="dot" id="dot"></span><span id="pill">snapshot</span></span>
      <div class="trb"><button class="refresh" id="refresh" onclick="location.reload()" title="re-read your projects">&#8635; Refresh</button><span class="cmd" id="cmd">$ pjm dashboard</span></div>
      <span class="gen" id="gen"></span></div>
  </div>
  <div class="stats" id="stats"></div>
  <div class="charts">
    <div class="panel"><div class="h">Issues captured over time <span class="sub">all projects</span></div>
      <svg id="line" viewBox="0 0 520 170" preserveAspectRatio="none"></svg></div>
    <div class="panel"><div class="h">Grade distribution</div><div id="grades" class="grades"></div>
      <div class="attn" id="attn"></div></div>
  </div>
  <div class="h h2">Projects <span class="sub" id="count"></span></div>
  <div class="grid" id="cards"></div>
  <div class="foot">100% local · aggregated at read-time from each repo's <code>.projectmem/</code> · nothing ever leaves your machine · <a href="https://github.com/riponcm/projectmem" target="_blank" rel="noopener">★ GitHub</a> · <a href="https://projectmem.dev/guide" target="_blank" rel="noopener">Docs</a></div>
</div>
<script>
var DATA={{DASH_DATA}};
var GC={'A+':'#0E9C88','A':'#12A594','A-':'#12A594','B+':'#2E7BF6','B':'#3B87EA','B-':'#3B87EA','C+':'#E0A32E','C':'#E0A32E','C-':'#E0A32E','D+':'#E8683B','D':'#E8683B','D-':'#E8683B','F':'#D64545'};
var ACC=['#12A594','#2E7BF6','#7C5CFC','#E0A32E','#E8683B','#17B0A0'];
function gc(g){return GC[g]||'#93A2B8';}
function fmt(n){n=+n||0;var a=Math.abs(n);if(a>=1e9)return(Math.round(n/1e8)/10)+'B';if(a>=1e6)return(Math.round(n/1e5)/10)+'M';if(a>=1e4)return(Math.round(n/1e2)/10)+'K';return n.toLocaleString();}
var A=DATA.agg,P=DATA.projects;
(function(){var g=new Date(DATA.generated),s=Math.max(0,(Date.now()-g)/1000),r;
  if(s<90)r='just now';else if(s<5400)r=Math.round(s/60)+'m ago';else if(s<86400)r=Math.round(s/3600)+'h ago';else r=Math.round(s/86400)+'d ago';
  if(DATA.live){
    // Live server: green "live" pill + a Refresh that genuinely re-reads files.
    document.getElementById('pill').textContent='live · reads your files on every load';
    document.getElementById('cmd').textContent='$ pjm dashboard --serve';
    document.getElementById('gen').textContent='rendered '+r+' · Refresh re-reads live';
  }else{
    // Static snapshot: nothing to re-read, so drop the "live" cues entirely —
    // neutralize the dot/pill and hide the Refresh button (it would only reload
    // the same frozen file). Refreshing means re-running the command.
    document.getElementById('pill').textContent='snapshot · re-run to refresh';
    document.getElementById('cmd').textContent='$ pjm dashboard';
    document.getElementById('gen').textContent='snapshot '+r+' · re-run pjm dashboard to refresh';
    var pw=document.getElementById('pillwrap');
    pw.style.background='rgba(148,162,184,.12)';pw.style.borderColor='rgba(148,162,184,.32)';pw.style.color='#9DB2C9';
    var dot=document.getElementById('dot');dot.style.background='#8798AE';dot.style.boxShadow='none';
    document.getElementById('refresh').style.display='none';
  }})();
var stats=[
  {k:'Projects',v:A.projects,t:'registered'},
  {k:'Issues Captured',v:fmt(A.events),t:'all projects'},
  {k:'Fixes Confirmed',v:A.fixes,t:'with context'},
  {k:'Dead-ends Prevented',v:A.prevented,t:'pre-commit warns'},
  {k:'Tokens Saved',v:fmt(A.tokens),t:'est. · $'+A.usd},
  {k:'Avg Grade',v:A.avg_grade,t:'A+ → F'}
];
document.getElementById('stats').innerHTML=stats.map(function(s,i){
  return '<div class="stat"><i style="background:'+ACC[i]+'"></i><div class="k">'+s.k+'</div><div class="v" style="color:'+ACC[i]+'">'+s.v+'</div><div class="t">'+s.t+'</div></div>';}).join('');
document.getElementById('count').textContent='· '+P.length+' registered · click a card → opens that repo, freshly generated';

var lab=DATA.series.labels,ser=DATA.series.values;
var W=520,H=170,pad=24,mx=Math.max.apply(0,ser.concat([1]))*1.12;
function X(i){return pad+i/(ser.length-1)*(W-2*pad);}
function Y(v){return H-26-v/mx*(H-46);}
var pts=ser.map(function(v,i){return X(i)+','+Y(v);}).join(' ');
var area='M'+X(0)+','+(H-26)+' L'+ser.map(function(v,i){return X(i)+','+Y(v);}).join(' L')+' L'+X(ser.length-1)+','+(H-26)+' Z';
document.getElementById('line').innerHTML='<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#2E7BF6" stop-opacity="0.24"/><stop offset="1" stop-color="#2E7BF6" stop-opacity="0"/></linearGradient></defs><path d="'+area+'" fill="url(#g)"/><polyline points="'+pts+'" fill="none" stroke="#2E7BF6" stroke-width="2.6" stroke-linejoin="round"/>'+ser.map(function(v,i){return '<circle cx="'+X(i)+'" cy="'+Y(v)+'" r="3.2" fill="#fff" stroke="#2E7BF6" stroke-width="2"/>';}).join('')+lab.map(function(m,i){return '<text x="'+X(i)+'" y="'+(H-8)+'" text-anchor="middle" font-size="9" fill="#A2B0C4">'+m+'</text>';}).join('');

var order=['A','B','C','D'],counts={A:0,B:0,C:0,D:0};
P.forEach(function(p){var b=(p.grade||'F')[0];if(counts[b]!=null)counts[b]++;});
var gmax=Math.max.apply(0,order.map(function(o){return counts[o];}).concat([1]));
document.getElementById('grades').innerHTML=order.map(function(o){var c=gc(o);
  return '<div class="grow"><span class="glabel" style="background:'+c+'">'+o+'</span><span class="gbar" style="width:'+(22+counts[o]/gmax*140)+'px;background:'+c+'"></span><span class="gcount">'+counts[o]+' project'+(counts[o]===1?'':'s')+'</span></div>';}).join('');
var needy=P.filter(function(p){return p.open>0;}).length;
document.getElementById('attn').innerHTML=needy?('⚠ <b>'+needy+' project'+(needy===1?'':'s')+'</b> need attention · <b>'+A.open+' open issues</b> across the portfolio'):'✓ no open issues across the portfolio';

document.getElementById('cards').innerHTML=P.map(function(p){var c=gc(p.grade);var sp=p.spark,m=Math.max.apply(0,sp.concat([1])),w=124,h=26;
  var pl=sp.map(function(v,i){return (i/(sp.length-1)*w)+','+(h-v/m*(h-2)-1);}).join(' ');
  var tag=p.href?'a href="'+p.href+'"':'div';var cls='card'+(p.hot?' hot':'')+(p.href?'':' dead');
  return '<'+tag+' class="'+cls+'" title="'+p.path+'"><i style="background:'+c+'"></i>'+(p.href?'<span class="open">open ↗</span>':'')+
    '<div class="crow"><div class="cname">'+p.name+'</div><div class="badge" style="background:'+c+'">'+p.grade+'</div></div>'+
    '<div class="cmeta"><span><b>'+p.events+'</b> ev</span><span><b>'+p.fixes+'</b> fix</span>'+(p.open?'<span class="oi">'+p.open+' open</span>':'<span>0 open</span>')+'</div>'+
    '<svg class="spark" width="'+w+'" height="'+h+'" viewBox="0 0 '+w+' '+h+'"><polyline points="'+pl+'" fill="none" stroke="'+c+'" stroke-width="1.8" stroke-linejoin="round"/></svg>'+
    '<div class="last">updated '+p.last+'</div></'+(p.href?'a':'div')+'>';}).join('');
</script></body></html>"""
