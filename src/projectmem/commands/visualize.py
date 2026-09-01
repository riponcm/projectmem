from __future__ import annotations

import json
import re
import webbrowser
from pathlib import Path
from typing import Any

import typer

from projectmem.models import Event, normalize_timestamp
from projectmem.storage import read_events, require_mem_dir, project_map_path
from projectmem.commands.stats import calculate_savings
from projectmem.commands.score import calculate_score


DENSE_FILE_EVENT_THRESHOLD = 10
FAILURE_IMPORTANCE_WEIGHT = 3
ROOT_DIRECTORY_BUCKET = "./"


def json_for_script(value: Any) -> str:
    """JSON for embedding inside an HTML <script> tag.

    `json.dumps` leaves `<` and `>` untouched, so any event text containing
    `</script>` closes the tag early — the rest of the payload is then parsed
    as HTML and the whole dashboard dies with a SyntaxError. Escaping `<`, `>`
    and `&` as \\uXXXX keeps the value a valid JS string while making a tag
    breakout impossible. U+2028/U+2029 are legal in JSON but not in JS source.
    """
    return (
        json.dumps(value)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def run(
    root: Path | None = None,
    output: Path | None = None,
    open_browser: bool = True,
) -> None:
    events = read_events(root)
    mem_dir = require_mem_dir(root)

    # 1. Build the graph data
    project_root = mem_dir.parent
    graph_data = build_graph_data(events, root=project_root)

    # 2. Project Map data. The details panel always shows PROJECT_MAP.md (the
    #    curated memory). The GRAPH prefers the extracted structure.json (deep:
    #    all files + real import relationships), falling back to the curated map
    #    when structure hasn't been built. Structure is derived from code and
    #    never mixed into memory — they only meet here, in the renderer.
    from projectmem.structure import structure_path

    map_path = project_map_path(root)
    project_map_text = map_path.read_text(encoding="utf-8") if map_path.exists() else ""
    project_map_graph = {"nodes": [], "links": []}
    struct_path = structure_path(root)
    if struct_path.exists():
        try:
            project_map_graph = build_structure_graph(
                json.loads(struct_path.read_text(encoding="utf-8"))
            )
        except (json.JSONDecodeError, OSError):
            project_map_graph = {"nodes": [], "links": []}
    if not project_map_graph["nodes"] and project_map_text:
        project_map_graph = build_project_map_graph(project_map_text)

    # THE COMBO: overlay event/failure heat onto the structure nodes, matched
    # by file PATH (not basename — many repos have several __init__.py). Hot
    # files then light up on the real code map. This is the only place
    # structure (from code) and judgment (from events) meet — in the renderer.
    _heat: dict[str, tuple[int, int]] = {}
    for gn in graph_data.get("nodes", []):
        if gn.get("type") == "file":
            fc, ec = gn.get("failure_count", 0), gn.get("event_count", 0)
            for key in (gn.get("full_path"), gn.get("id")):
                if key:
                    _heat.setdefault(key, (fc, ec))
    for sn in project_map_graph.get("nodes", []):
        if sn.get("type") == "file":
            hit = _heat.get(sn.get("full_path")) or _heat.get(sn.get("id"))
            if hit:
                sn["failure_count"], sn["event_count"] = hit

    # 3. Build timeline data for the Timeline tab
    timeline_data = build_timeline_data(events)

    # 4. Full score (grade A+→F, hours/usd/tokens, components) for the
    #    Overview tab's prevention-grade gauge and headline cards. Shares
    #    the single ROI model in score.calculate_score.
    score_data = calculate_score([e.__dict__ for e in events])

    # 5. Project name for the sidebar logo — derived automatically from the
    #    project folder (the parent of .projectmem/), so the dashboard brands
    #    itself with whatever repo it's run in. Falls back to "project".
    project_name = mem_dir.parent.name or "project"

    # 6. Generate the HTML
    html_content = (
        VIZ_TEMPLATE
        .replace("{{GRAPH_DATA}}", json_for_script(graph_data))
        .replace("{{PROJECT_MAP}}", json_for_script(project_map_text))
        .replace("{{PROJECT_MAP_GRAPH}}", json_for_script(project_map_graph))
        .replace("{{TIMELINE_DATA}}", json_for_script(timeline_data))
        .replace("{{SCORE_DATA}}", json_for_script(score_data))
        .replace("{{PROJECT_NAME}}", json_for_script(project_name))
    )

    # 7. Save and (optionally) open
    viz_path = Path(output) if output else (mem_dir / "viz.html")
    viz_path.parent.mkdir(parents=True, exist_ok=True)
    viz_path.write_text(html_content, encoding="utf-8")

    typer.echo(f"Visualization generated at {viz_path}")
    if open_browser:
        webbrowser.open(viz_path.as_uri())

def _location_path_for_graph(
    location: str | None,
    root: Path | None = None,
) -> str | None:
    """Return a project-relative path for Story Map linking, if path-like."""
    if not location:
        return None

    raw = location.strip().strip('"').strip("'")
    if not raw:
        return None

    if ":" in raw:
        head, tail = raw.split(":", 1)
        if tail.strip().split(":", 1)[0].isdigit():
            raw = head

    normalized = raw.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.strip("/")

    if not normalized:
        return None

    root_path = root or Path.cwd()
    candidate = root_path / normalized
    if candidate.is_file():
        return normalized
    if candidate.is_dir():
        return normalized.rstrip("/") + "/"

    name = Path(normalized).name
    is_file_like = "." in name and " " not in normalized
    has_path_separator = "/" in normalized
    if is_file_like and has_path_separator:
        return normalized

    return None


def _file_path_for_graph(path: str) -> str | None:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.strip("/")
    return normalized or None


def _file_graph_metadata(
    path: str,
    event_count: int = 0,
    failure_count: int = 0,
) -> dict[str, Any]:
    normalized = path.replace("\\", "/").strip("/")
    parts = [part for part in normalized.split("/") if part]
    directory_parts = parts[:-1]
    top_directory = f"{directory_parts[0]}/" if directory_parts else ROOT_DIRECTORY_BUCKET
    importance = event_count + failure_count * FAILURE_IMPORTANCE_WEIGHT
    return {
        "path": normalized,
        "directory_parts": directory_parts,
        "top_directory": top_directory,
        "event_count": event_count,
        "failure_count": failure_count,
        "failures": failure_count,
        "importance": importance,
        "dense_event_threshold": DENSE_FILE_EVENT_THRESHOLD,
        "is_dense": event_count >= DENSE_FILE_EVENT_THRESHOLD,
    }


def build_structure_graph(structure: dict[str, Any]) -> dict[str, Any]:
    """Convert structure.json (files + import relationships) into the same
    node/link shape the Project Map renderers consume — so the Tree / Graph
    views show the FULL extracted structure instead of only the curated map.
    """
    nodes: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    node_ids: set[str] = set()

    def add(nid: str, label: str, ntype: str) -> None:
        if nid and nid not in node_ids:
            node_ids.add(nid)
            nodes.append({"id": nid, "label": label, "type": ntype, "full_path": nid})

    files = structure.get("files", [])
    dirs: set[str] = set()
    for f in files:
        parts = f.split("/")
        for i in range(1, len(parts)):
            dirs.add("/".join(parts[:i]) + "/")
    for d in sorted(dirs):
        add(d, d.rstrip("/").split("/")[-1] + "/", "folder")
    for f in files:
        add(f, f.split("/")[-1], "file")

    # Hierarchy: each node → its parent directory.
    for nid in list(node_ids):
        stem = nid.rstrip("/")
        parent = "/".join(stem.split("/")[:-1])
        parent = (parent + "/") if parent else ""
        if parent and parent in node_ids:
            links.append({"source": nid, "target": parent})

    # Import relationships (the real "relations").
    for r in structure.get("relationships", []):
        s, t = r.get("source"), r.get("target")
        if s in node_ids and t in node_ids:
            links.append({"source": s, "target": t, "relation": r.get("kind", "imports")})

    return {"nodes": nodes, "links": links}


def build_project_map_graph(map_text: str) -> dict[str, Any]:
    nodes = []
    links = []
    node_set = set()
    
    path_pattern = re.compile(r'`([^`]+)`')
    
    # Pass 1: Extract all path-like nodes
    for match in path_pattern.finditer(map_text):
        name = match.group(1)
        if name not in node_set and len(name) > 1 and ("/" in name or "." in name):
            node_set.add(name)
            node_type = "folder" if name.endswith("/") else "file"
            label = name.split("/")[-2] + "/" if node_type == "folder" else name.split("/")[-1]
            nodes.append({
                "id": name,
                "label": label,
                "type": node_type,
                "full_path": name
            })
            
    # Pass 2: Extract explicit relationships from bullet points
    lines = map_text.splitlines()
    in_rel = False
    for line in lines:
        if line.startswith("## Relationships"):
            in_rel = True
            continue
        elif in_rel and line.startswith("##"):
            in_rel = False
            
        if in_rel and line.strip().startswith("-"):
            paths = [m.group(1) for m in path_pattern.finditer(line) if m.group(1) in node_set]
            if len(paths) >= 2:
                source = paths[0]
                for target in paths[1:]:
                    links.append({"source": source, "target": target})
                    
    # Pass 3: Implicit hierarchy relationships
    for node in nodes:
        node_id = node["id"]
        for parent in nodes:
            parent_id = parent["id"]
            if parent_id != node_id and parent_id.endswith("/") and node_id.startswith(parent_id):
                # Link if it's a direct child (no extra slashes)
                rel_path = node_id[len(parent_id):]
                if "/" not in rel_path or (rel_path.count("/") == 1 and rel_path.endswith("/")):
                    links.append({"source": node_id, "target": parent_id})
                    
    return {"nodes": nodes, "links": links}


def build_timeline_data(events: list[Event]) -> list[dict[str, Any]]:
    timeline = []
    for event in events:
        # Normalize timestamps before serializing — older events from
        # `pjm backfill` use git's "YYYY-MM-DD HH:MM:SS ±HHMM" format which
        # JS `new Date()` can't parse, producing "INVALID DATE" sections
        # in the Timeline tab (L-024a).
        entry: dict[str, Any] = {
            "type": event.type,
            "summary": event.summary,
            "timestamp": normalize_timestamp(event.timestamp),
            "outcome": event.outcome,
            "location": event.location,
            "issue_id": event.issue_id,
        }
        # Auto-capture fields
        if event.auto_captured:
            entry["auto_captured"] = True
            entry["capture_source"] = event.capture_source
            entry["capture_confidence"] = event.capture_confidence
            entry["git_message"] = event.git_message
        timeline.append(entry)
    return timeline


def build_graph_data(
    events: list[Event],
    root: Path | None = None,
) -> dict[str, Any]:
    nodes = []
    links = []

    # Track nodes to avoid duplicates
    node_ids = set()

    # Counts for heatmap, labels, and collapse decisions
    event_counts: dict[str, int] = {}
    failure_counts: dict[str, int] = {}

    # Helper to add file nodes
    def add_file(path: str):
        if path and path not in node_ids:
            node_ids.add(path)
            nodes.append({
                "id": path,
                "type": "file",
                "label": path.split("/")[-1],
                "full_path": path,
                **_file_graph_metadata(path),
            })

    # First pass: Collect all files and calculate counts
    for event in events:
        explicit_files = [
            normalized_file
            for file_path in event.files
            if (normalized_file := _file_path_for_graph(file_path))
        ]
        linked_files = list(dict.fromkeys(explicit_files))
        location_file = _location_path_for_graph(event.location, root=root)
        if location_file and location_file not in linked_files:
            linked_files.append(location_file)

        for file_path in linked_files:
            add_file(file_path)
            event_counts[file_path] = event_counts.get(file_path, 0) + 1
            if event.outcome == "failed":
                failure_counts[file_path] = failure_counts.get(file_path, 0) + 1

    # Update file metadata in nodes
    for node in nodes:
        if node["type"] != "file":
            continue
        node.update(
            _file_graph_metadata(
                node["id"],
                event_count=event_counts.get(node["id"], 0),
                failure_count=failure_counts.get(node["id"], 0),
            )
        )

    # Second pass: Collect events and links
    for i, event in enumerate(events):
        event_id = event.id or f"evt_{i}"
        node_ids.add(event_id)

        node_data: dict[str, Any] = {
            "id": event_id,
            "type": "event",
            "event_type": event.type,
            "label": event.summary[:30] + ("..." if len(event.summary) > 30 else ""),
            "summary": event.summary,
            "timestamp": event.timestamp,
            "outcome": event.outcome,
            "location": event.location,
        }
        if event.auto_captured:
            node_data["auto_captured"] = True
            node_data["capture_source"] = event.capture_source
        nodes.append(node_data)

        explicit_files = [
            normalized_file
            for file_path in event.files
            if (normalized_file := _file_path_for_graph(file_path))
        ]
        linked_files = list(dict.fromkeys(explicit_files))

        # Link event to its explicit files
        for file_path in linked_files:
            links.append({"source": event_id, "target": file_path, "type": "mention"})

        # Link event to its location file when explicit files do not already do so
        location_file = _location_path_for_graph(event.location, root=root)
        if location_file and location_file not in linked_files:
            links.append({"source": event_id, "target": location_file, "type": "at"})

    # 3. Calculate ROI stats
    raw_events = [e.__dict__ for e in events]
    stats = calculate_savings(raw_events)

    return {"nodes": nodes, "links": links, "stats": stats}


VIZ_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>projectmem Dashboard</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
    <script src="https://d3js.org/d3.v7.min.js"></script>
    <style>
        :root {
            /* projectmem light "product" theme — matches the poster/brand */
            --bg: #EEF3F9;
            --bg-glow: radial-gradient(circle at 50% -10%, rgba(31,111,235,0.06), transparent 55%);
            --surface: #FFFFFF;
            --surface2: #F1F5FA;
            --surface3: #E7EEF6;
            --border: rgba(11,42,74,0.10);
            --border-light: rgba(11,42,74,0.18);
            --text: #13233A;
            --text-dim: #5A6B82;
            --text-muted: #8A99AD;
            --navy: #0B2A4A;
            --primary: #1F6FEB;
            --primary-glow: rgba(31,111,235,0.14);
            --success: #169F84;
            --error: #E8593B;
            --warning: #E8A33B;
            --accent: #6366F1;
        }
        * { margin:0; padding:0; box-sizing:border-box; }
        body {
            background-color: var(--bg);
            background-image: var(--bg-glow);
            color: var(--text);
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            height: 100vh;
            overflow: hidden;
            -webkit-font-smoothing: antialiased;
        }

        /* ── Header ── */
        .header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 0 24px;
            height: 56px;
            background: var(--navy);
            border-bottom: 1px solid rgba(255,255,255,0.06);
            z-index: 10;
            position: relative;
        }
        .header-brand {
            display: flex;
            align-items: center;
            gap: 10px;
            font-weight: 800;
            font-size: 16px;
            letter-spacing: -0.3px;
            color: #fff;
        }
        .pulse-dot {
            width: 8px; height: 8px;
            background: #3FE0B0;
            border-radius: 50%;
            box-shadow: 0 0 8px #3FE0B0;
            animation: pulse 2.5s infinite;
        }
        @keyframes pulse {
            0% { transform:scale(0.95); box-shadow:0 0 0 0 rgba(96,165,250,0.5); }
            70% { transform:scale(1); box-shadow:0 0 0 6px rgba(96,165,250,0); }
            100% { transform:scale(0.95); box-shadow:0 0 0 0 rgba(96,165,250,0); }
        }
        .header-stats {
            display: flex;
            gap: 20px;
            font-size: 12px;
            color: rgba(255,255,255,0.60);
            font-weight: 500;
        }
        .header-stats .val {
            color: #fff;
            font-weight: 700;
            font-size: 13px;
        }

        /* ── Tabs ── */
        .tabs {
            display: flex;
            gap: 0;
            padding: 0 24px;
            background: var(--surface);
            border-bottom: 1px solid var(--border);
        }
        .tab {
            padding: 10px 20px;
            font-size: 13px;
            font-weight: 500;
            color: var(--text-dim);
            cursor: pointer;
            border-bottom: 2px solid transparent;
            transition: all 0.2s;
            user-select: none;
            display: flex;
            align-items: center;
            gap: 6px;
        }
        .tab:hover { color: var(--text); }
        .tab.active { color: var(--primary); border-bottom-color: var(--primary); }
        .tab svg { width:14px; height:14px; opacity:0.7; }
        .tab.active svg { opacity:1; }

        /* ── App shell (sidebar + main) ── */
        .app { display: flex; height: 100vh; }
        .main-area { flex: 1; height: 100vh; position: relative; overflow: hidden; min-width: 0; }
        .side {
            width: 232px; flex-shrink: 0; height: 100vh; overflow-y: auto;
            background: var(--navy); color: #fff; padding: 20px 14px;
            display: flex; flex-direction: column;
        }
        .brand { display: flex; align-items: center; gap: 10px; padding: 2px 6px 4px; margin-bottom: 18px; }
        .logo-mark { display: flex; flex-shrink: 0; }
        .brand-name {
            font-weight: 800; font-size: 18px; letter-spacing: -0.2px; color: #fff;
            white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
        }
        .navlbl { font-size: 10px; font-weight: 700; letter-spacing: 1.4px; color: #6E8BAE; margin: 14px 9px 7px; }
        .nav {
            display: flex; align-items: center; gap: 11px; width: 100%;
            padding: 9px 11px; margin-bottom: 2px; border: none; border-radius: 9px;
            background: transparent; color: #C5D6EC; font-size: 13.5px; font-weight: 500;
            font-family: inherit; cursor: pointer; text-align: left; transition: background 0.15s, color 0.15s;
        }
        .nav:hover { background: rgba(255,255,255,0.06); color: #fff; }
        .nav.active { background: var(--primary); color: #fff; font-weight: 600; }
        .nic { width: 17px; height: 17px; flex-shrink: 0; fill: none; stroke: currentColor;
               stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; opacity: 0.95; }
        .ws-stats { display: flex; gap: 7px; padding: 2px 6px; }
        .ws-stat { flex: 1; background: #0E3157; border-radius: 9px; padding: 9px 6px; text-align: center; }
        .ws-stat .wv { display: block; font-size: 18px; font-weight: 800; color: #fff; line-height: 1; }
        .ws-stat .wl { display: block; font-size: 9.5px; color: #8FA8C6; margin-top: 4px; font-weight: 600; }
        .side-star {
            margin-top: auto; display: flex; align-items: center; gap: 10px;
            padding: 11px 12px; border-radius: 11px; text-decoration: none;
            background: linear-gradient(135deg, #14406E, #0F3159 60%, #14406E);
            border: 1px solid #26568C; color: #EAF2FD; margin-bottom: 8px;
            transition: transform .13s, border-color .13s, box-shadow .13s;
        }
        .side-star:hover { transform: translateY(-1px); border-color: #4C90F0;
            box-shadow: 0 6px 18px rgba(31,111,235,.28); }
        .side-star .ss-star { font-size: 16px; color: #FFC64D; line-height: 1; }
        .side-star .ss-txt { display: flex; flex-direction: column; font-size: 12.5px; font-weight: 700; }
        .side-star .ss-sub { font-size: 9.5px; font-weight: 500; color: #9FB6D2; margin-top: 2px; }
        .side-star .ss-go { margin-left: auto; color: #7FB2F2; font-size: 12px; }
        .side-ft {
            padding: 12px; background: #0E3157; border-radius: 10px;
            font-size: 11px; color: #9FB6D2; line-height: 1.55;
        }
        .side-ft b { color: #fff; font-weight: 600; }

        /* ── Panels ── */
        .panels { height: 100%; position: relative; }
        .panel {
            position: absolute; inset: 0;
            opacity: 0; pointer-events: none;
            transition: opacity 0.35s ease;
        }
        .panel.active { opacity: 1; pointer-events: auto; }

        /* ── Shared ── */
        .map-tooltip {
            position: absolute;
            background: rgba(255,255,255,0.97);
            backdrop-filter: blur(16px);
            padding: 12px 16px;
            border-radius: 10px;
            border: 1px solid var(--border-light);
            font-size: 12px;
            pointer-events: none;
            opacity: 0;
            transition: opacity 0.15s;
            max-width: 340px;
            z-index: 100;
            box-shadow: 0 8px 28px rgba(11,42,74,0.18);
            color: var(--text);
            line-height: 1.5;
        }
        .map-tooltip strong { color: var(--primary); }
        .map-legend {
            position: absolute;
            top: 16px; right: 16px;
            display: flex; flex-direction: column; gap: 8px;
            background: rgba(255,255,255,0.94);
            backdrop-filter: blur(16px);
            padding: 14px 18px;
            border-radius: 12px;
            border: 1px solid var(--border);
            font-size: 11px;
            box-shadow: 0 4px 20px rgba(11,42,74,0.12);
        }
        .map-legend-item { display:flex; align-items:center; gap:8px; color: var(--text-dim); }
        .dot { width:8px; height:8px; border-radius:50%; }

        /* ═══ Story Map ═══ */
        #canvas { width:100%; height:100%; }
        /* view modes: Treemap (default) · Lanes · Graph (the original force map) */
        #panel-story .sm-toggle { top:16px; left:16px; z-index:7; }
        #panel-story .story-controls { top:60px; }
        #panel-story:not(.sm-graph) #canvas,
        #panel-story:not(.sm-graph) .story-controls,
        #panel-story:not(.sm-graph) .map-legend { display:none; }
        .sm-view { position:absolute; inset:0; overflow:auto; padding:62px 22px 24px; display:none; }
        #panel-story.sm-tree #sm-tree { display:block; }
        #panel-story.sm-lanes #sm-lanes { display:block; }
        .sm-head { display:flex; align-items:baseline; gap:10px; margin-bottom:12px; flex-wrap:wrap; }
        .sm-head h3 { font-size:14.5px; font-weight:700; color:var(--navy); }
        .sm-head .d { font-size:11.5px; color:var(--text-muted); }
        .sm-head .r { margin-left:auto; display:flex; align-items:center; gap:6px; }
        .sm-chip { border:1px solid var(--border); background:var(--surface); color:var(--text-dim);
                   border-radius:7px; padding:4px 10px; font:600 11px 'JetBrains Mono',monospace; cursor:pointer; }
        .sm-chip.on { background:var(--primary-glow); color:var(--primary); border-color:transparent; }
        .sm-chip[disabled] { opacity:.35; cursor:default; }
        /* treemap */
        .tm-wrap { position:relative; width:100%; height:calc(100% - 84px); min-height:320px;
                   background:var(--surface2); border:1px solid var(--border); border-radius:12px; overflow:hidden; }
        .tm-tile { position:absolute; border:2px solid var(--surface); border-radius:8px; cursor:pointer;
                   overflow:hidden; padding:7px 9px; transition:filter .13s, box-shadow .13s; }
        .tm-tile:hover { filter:brightness(1.06); z-index:5; box-shadow:0 6px 20px rgba(11,42,74,.22); }
        .tm-fn { font-size:11.5px; font-weight:600; line-height:1.25; word-break:break-all; }
        .tm-mt { font:600 10px 'JetBrains Mono',monospace; opacity:.82; margin-top:3px; }
        .sm-legend { display:flex; gap:15px; align-items:center; margin-top:11px; font-size:11px;
                     color:var(--text-dim); flex-wrap:wrap; }
        .sm-sw { width:20px; height:11px; border-radius:3px; display:inline-block; vertical-align:middle; margin-right:5px; }
        .sm-sw.rnd { width:11px; height:11px; border-radius:50%; }
        /* lanes */
        .sl-row { display:grid; grid-template-columns:220px 1fr 104px; align-items:center; gap:10px;
                  padding:3px 6px; border-radius:8px; cursor:pointer; }
        .sl-row:hover { background:var(--surface2); }
        .sl-fp { font:11.5px 'JetBrains Mono',monospace; color:#33455E; text-align:right; direction:rtl;
                 white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
        .sl-track { position:relative; height:26px; border-bottom:1px dashed var(--border); }
        .sl-pt { position:absolute; top:7px; width:12px; height:12px; border-radius:50%; border:2px solid var(--surface);
                 transform:translateX(-50%); box-shadow:0 1px 3px rgba(16,47,82,.2); transition:transform .12s; }
        .sl-pt:hover { transform:translateX(-50%) scale(1.4); }
        .sl-rt { font-size:11px; color:var(--text-dim); font-weight:600; }
        .sl-rt b { color:var(--error); }
        .sl-axis { display:flex; justify-content:space-between; margin:6px 0 0 230px;
                   font:10.5px 'JetBrains Mono',monospace; color:var(--text-muted); }
        /* file dossier banner (reuses the case-modal shell) */
        .cm-warn { margin:14px 20px 0; background:#FFF6E9; border:1px solid rgba(232,163,59,.4);
                   border-radius:10px; padding:11px 13px; font-size:12.5px; line-height:1.5; }
        .cm-warn b { color:#9A6B12; }
        .story-link { stroke-opacity:0.35; stroke-width:1px; }
        .story-node { cursor:pointer; transition: filter 0.2s; }
        .story-node:hover { filter: brightness(1.3); }
        .story-controls {
            position:absolute; top:16px; left:16px; z-index:6;
            display:flex; flex-wrap:wrap; align-items:center; gap:8px;
            max-width:calc(100% - 260px);
        }
        .story-control-btn {
            border:1px solid var(--border);
            background:rgba(255,255,255,0.94);
            color:var(--text-dim);
            border-radius:8px;
            padding:7px 10px;
            font-size:11px;
            font-weight:700;
            cursor:pointer;
            box-shadow:0 4px 14px rgba(11,42,74,0.08);
        }
        .story-control-btn:hover { color:var(--text); border-color:var(--border-light); }
        .story-control-btn.active {
            background:var(--primary-glow);
            color:var(--primary);
            border-color:rgba(31,111,235,0.32);
        }
        .story-label {
            pointer-events:none;
            font-size:10px;
            fill:#475569;
            paint-order:stroke;
            stroke:rgba(255,255,255,0.9);
            stroke-width:3px;
            stroke-linejoin:round;
        }
        .story-node.dimmed,
        .story-link.dimmed,
        .story-label.dimmed,
        .story-bubble-label.dimmed { opacity:0.14; }
        .story-node.focused,
        .story-link.focused,
        .story-label.focused,
        .story-bubble-label.focused { opacity:1; }
        .story-bubble-label {
            pointer-events:none;
            font-size:11px;
            font-weight:700;
            fill:#1e3a5f;
            paint-order:stroke;
            stroke:rgba(255,255,255,0.94);
            stroke-width:4px;
        }

        /* ═══ ROI Dashboard ═══ */
        .roi-scroll { overflow-y:auto; height:100%; padding:28px 24px; }
        .roi-container { max-width:960px; margin:0 auto; }
        .roi-top { display:grid; grid-template-columns:repeat(4,1fr); gap:14px; margin-bottom:20px; }
        .roi-stat {
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 14px;
            padding: 20px;
            position: relative;
            overflow: hidden;
        }
        .roi-stat::before {
            content:'';
            position:absolute;
            top:0; left:0; right:0;
            height:2px;
        }
        .roi-stat.green::before { background: linear-gradient(90deg, var(--success), #34d399); }
        .roi-stat.blue::before { background: linear-gradient(90deg, var(--primary), #60a5fa); }
        .roi-stat.purple::before { background: linear-gradient(90deg, var(--accent), #a78bfa); }
        .roi-stat.amber::before { background: linear-gradient(90deg, var(--warning), #fbbf24); }
        .roi-stat-label {
            font-size:11px; font-weight:600;
            text-transform:uppercase; letter-spacing:0.6px;
            color: var(--text-dim);
            margin-bottom:8px;
        }
        .roi-stat-value {
            font-size:36px; font-weight:800;
            line-height:1; letter-spacing:-1.5px;
        }
        .roi-stat.green .roi-stat-value { color: var(--success); }
        .roi-stat.blue .roi-stat-value { color: var(--primary); }
        .roi-stat.purple .roi-stat-value { color: var(--accent); }
        .roi-stat.amber .roi-stat-value { color: var(--warning); }
        .roi-stat-sub { font-size:12px; color:var(--text-dim); margin-top:4px; }

        .roi-charts { display:grid; grid-template-columns:1.2fr 0.8fr; gap:14px; margin-bottom:20px; }
        .roi-card {
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 14px;
            padding: 20px;
        }
        .roi-card-title {
            font-size:11px; font-weight:600;
            text-transform:uppercase; letter-spacing:0.6px;
            color:var(--text-dim);
            margin-bottom:16px;
        }
        .roi-bar-row { display:flex; align-items:center; gap:10px; padding:7px 0; font-size:12px; }
        .roi-bar-label { width:110px; color:var(--text-dim); flex-shrink:0; font-weight:500; }
        .roi-bar-track { flex:1; height:8px; background:var(--surface2); border-radius:4px; overflow:hidden; }
        .roi-bar-fill { height:100%; border-radius:4px; transition:width 0.8s cubic-bezier(0.4,0,0.2,1); }
        .roi-bar-val { width:60px; text-align:right; color:var(--text); font-weight:600; font-size:12px; }

        /* Donut Chart */
        .roi-donut-wrap { display:flex; flex-direction:column; align-items:center; gap:16px; }
        .roi-donut-legend { width:100%; }
        .roi-donut-item { display:flex; align-items:center; gap:8px; padding:4px 0; font-size:12px; }
        .roi-donut-dot { width:10px; height:10px; border-radius:3px; flex-shrink:0; }
        .roi-donut-name { flex:1; color:var(--text-dim); }
        .roi-donut-val { font-weight:600; color:var(--text); }

        /* Area Chart */
        .roi-area-card { grid-column:1/-1; }
        .roi-area-chart { width:100%; height:140px; }

        /* ═══ Project Map ═══ */
        .map-split { display:flex; height:100%; }
        .map-graph-pane { flex:1.5; position:relative; background:var(--bg); border-right:1px solid var(--border); }
        .map-text-pane {
            flex:1; padding:24px 32px; overflow-y:auto;
            background:var(--surface); line-height:1.7;
        }
        #map-canvas { width:100%; height:100%; }
        #map-tree { width:100%; height:100%; display:none; }
        .map-graph-pane.tree-mode #map-canvas { display:none; }
        .map-graph-pane.tree-mode #map-tree { display:block; }
        #tree-filter-host { display:none; }
        .map-graph-pane.tree-mode #tree-filter-host { display:block; }
        .tree-legend { position:absolute; right:14px; bottom:14px; z-index:6; display:flex; gap:12px;
            align-items:center; background:var(--surface); border:1px solid var(--border);
            border-radius:9px; padding:7px 11px; font-size:10.5px; color:var(--text-dim); }
        #map-flow { position:absolute; inset:0; overflow:hidden; display:none; }
        .flow-filter::-webkit-scrollbar { width:6px; } .flow-filter::-webkit-scrollbar-thumb { background:var(--border-light); border-radius:3px; }
        .flow-filter .ff-row:hover { background:var(--surface2); }
        .flow-zoom button:hover { background:var(--surface2); }
        #map-content .md-pre { background:var(--surface2); border:1px solid var(--border-light); border-radius:8px; padding:10px 12px; margin:8px 0; font-family:ui-monospace,Menlo,monospace; font-size:11.5px; line-height:1.55; white-space:pre-wrap; overflow-wrap:anywhere; color:var(--text-dim); }
        .map-graph-pane.flow-mode #map-canvas, .map-graph-pane.flow-mode #map-tree { display:none; }
        .map-graph-pane.flow-mode #map-flow { display:block; }
        .map-graph-pane.flow-mode .map-legend { display:none; }
        .flow-empty { padding:64px 24px; color:var(--text-dim); font-size:13px; }
        .map-details-toggle { position:absolute; top:14px; right:14px; z-index:5; padding:3px;
            background:var(--surface); border:1px solid var(--border); border-radius:8px; }
        .map-split.details-collapsed .map-text-pane { display:none; }
        .map-split.details-collapsed .map-graph-pane { border-right:none; }

        /* ═══ Timeline — "Time Spine" view ═══ */
        .tl-toggle { position:absolute; top:14px; left:14px; z-index:6;
            display:flex; gap:0; padding:3px;
            background:var(--surface); border:1px solid var(--border); border-radius:8px; }
        #tl-spine { position:absolute; inset:0; overflow-y:auto; padding:56px 20px 60px; }
        #panel-timeline.list-mode #tl-spine { display:none; }
        #panel-timeline .timeline-view { display:none; }
        #panel-timeline.list-mode .timeline-view { display:block; padding-top:56px; }
        .tsp-colhead { display:flex; justify-content:space-between; max-width:1000px; margin:0 auto 14px;
            font-size:11px; font-weight:700; letter-spacing:1.2px; color:var(--text-muted); }
        .tsp-colhead span { width:46%; text-align:center; }
        .tsp-wrap { position:relative; max-width:1000px; margin:0 auto; }
        .tsp-spine { position:absolute; left:50%; top:0; bottom:0; width:2px;
            background:linear-gradient(#C7D6E8,#9FB4CE); transform:translateX(-1px); }
        .tsp-day { position:relative; text-align:center; margin:24px 0 16px; z-index:2; }
        .tsp-day b { background:var(--navy); color:#fff; font-size:11.5px; padding:5px 15px; border-radius:999px; }
        .tsp-gap { position:relative; text-align:center; margin:8px 0; z-index:2; }
        .tsp-gap span { background:var(--bg); color:var(--text-muted); font-size:10.5px;
            padding:2px 10px; border:1px dashed #C7D6E8; border-radius:999px; }
        .tsp-row { position:relative; display:flex; margin:11px 0; min-height:48px; }
        .tsp-dot { position:absolute; left:50%; top:22px; width:11px; height:11px; border-radius:50%;
            transform:translate(-50%,-50%); border:2.5px solid var(--surface); box-shadow:0 0 0 1.5px #C7D6E8; z-index:3; }
        .tsp-tick { position:absolute; left:50%; top:22px; height:1.5px; width:5%; background:#C7D6E8; z-index:1; }
        .tsp-row.tsp-L .tsp-tick { transform:translate(-100%,-50%); }
        .tsp-row.tsp-R .tsp-tick { transform:translate(0,-50%); }
        .tsp-card { width:44%; background:var(--surface); border:1px solid var(--border); border-radius:11px;
            padding:9px 13px; box-shadow:0 1px 3px rgba(11,42,74,.05); transition:opacity .15s, box-shadow .15s; }
        .tsp-row.tsp-L { justify-content:flex-start; }
        .tsp-row.tsp-R { justify-content:flex-end; }
        .tsp-row.tsp-L .tsp-card { border-right:3px solid var(--ac); }
        .tsp-row.tsp-R .tsp-card { border-left:3px solid var(--ac); }
        .tsp-k { font-size:10.5px; font-weight:800; letter-spacing:.4px; color:var(--ac); }
        .tsp-k .tsp-t { float:right; color:var(--text-muted); font-weight:600; }
        .tsp-s { font-size:12.5px; line-height:1.45; margin-top:3px; color:var(--text); }
        .tsp-m { font-size:10.5px; color:var(--text-muted); margin-top:4px; font-family:'JetBrains Mono', ui-monospace, monospace; }
        .tsp-m .tsp-iss { color:var(--primary); font-weight:700; margin-right:8px; }
        #tl-spine.tsp-hl .tsp-card { opacity:.22; }
        #tl-spine.tsp-hl .tsp-card.tsp-on { opacity:1; box-shadow:0 3px 14px rgba(31,111,235,.20); }
        .map-view-toggle {
            position:absolute; top:14px; left:14px; z-index:5;
            display:flex; gap:0; padding:3px;
            background:var(--surface); border:1px solid var(--border); border-radius:8px;
        }
        .map-view-btn {
            padding:5px 14px; font-size:11px; font-weight:600;
            font-family:'JetBrains Mono', monospace;
            background:transparent; color:var(--text-dim); border:none; cursor:pointer;
            border-radius:6px; transition:all 0.15s;
        }
        .map-view-btn:hover { color:var(--text); }
        .map-view-btn.active { background:var(--primary-glow); color:var(--primary); }
        .tree-link { fill:none; stroke:var(--border-light); stroke-width:1.2; stroke-opacity:0.5; }
        .tree-node-label { font-size:11px; font-family:'JetBrains Mono', monospace; fill:var(--text); }
        .tree-node-circle { stroke:var(--surface); stroke-width:2; cursor:pointer; transition:r 0.15s; }
        .tree-node-circle:hover { stroke:var(--text); }
        .map-text-pane h1 { font-size:20px; font-weight:700; margin-bottom:4px; color:var(--primary); }
        .map-text-pane h2 {
            font-size:12px; font-weight:600;
            text-transform:uppercase; letter-spacing:0.6px;
            color:var(--accent); margin-top:20px; margin-bottom:8px;
            padding-bottom:4px; border-bottom:1px solid var(--border);
        }
        .map-text-pane p,.map-text-pane li { font-size:13px; color:var(--text-dim); }
        .map-text-pane code { background:var(--surface2); padding:2px 6px; border-radius:4px; font-size:12px; color:var(--accent); }
        .map-text-pane ul { padding-left:20px; }
        .map-text-pane li { margin-bottom:3px; }
        .arch-node { cursor:pointer; transition:filter 0.2s; }
        .arch-node:hover { filter:brightness(1.3) drop-shadow(0 0 12px rgba(255,255,255,0.2)); }
        .arch-link { stroke:#475569; stroke-opacity:0.35; stroke-width:1.5px; }

        /* ═══ Timeline ═══ */
        .timeline-view { padding:24px; max-width:800px; margin:0 auto; overflow-y:auto; height:100%; }
        .tl-header { margin-bottom:20px; }
        .tl-activity { display:flex; gap:2px; align-items:flex-end; height:40px; margin-bottom:16px; padding:8px 0; }
        .tl-activity-bar {
            flex:1; min-width:3px; border-radius:2px 2px 0 0;
            background:var(--primary); opacity:0.6; transition:opacity 0.15s, height 0.3s;
        }
        .tl-activity-bar:hover { opacity:1; }
        .tl-filters { display:flex; gap:6px; flex-wrap:wrap; }
        .tl-filter {
            padding:5px 14px; font-size:11px; font-weight:500;
            border-radius:20px; cursor:pointer;
            border:1px solid var(--border); background:var(--surface);
            color:var(--text-dim); transition:all 0.2s;
        }
        .tl-filter.active { background:var(--primary-glow); border-color:var(--primary); color:var(--primary); }
        .tl-filter .count {
            display:inline-block;
            margin-left:4px; padding:1px 6px;
            background:rgba(255,255,255,0.06); border-radius:10px;
            font-size:10px; font-weight:600;
        }
        .tl-filter.active .count { background:rgba(59,130,246,0.2); }

        .tl-date-group { margin-top:20px; }
        .tl-date-label {
            font-size:11px; font-weight:600;
            color:var(--text-muted); text-transform:uppercase;
            letter-spacing:0.5px; padding-bottom:8px;
            border-bottom:1px solid var(--border);
            margin-bottom:4px;
        }
        .tl-item {
            display:flex; gap:12px; padding:10px 0;
            border-bottom:1px solid rgba(255,255,255,0.03);
            font-size:13px; transition:background 0.15s;
        }
        .tl-item:hover { background:rgba(255,255,255,0.015); margin:0 -8px; padding:10px 8px; border-radius:6px; }
        .tl-item:last-child { border-bottom:none; }
        .tl-badge {
            flex-shrink:0; width:70px; text-align:center;
            font-size:10px; font-weight:600; text-transform:uppercase;
            padding:4px 0; border-radius:4px; line-height:1.2; height:fit-content;
        }
        .tl-badge.issue { background:rgba(59,130,246,0.12); color:var(--primary); }
        .tl-badge.attempt { background:rgba(251,191,36,0.12); color:var(--warning); }
        .tl-badge.fix { background:rgba(16,185,129,0.12); color:var(--success); }
        .tl-badge.decision { background:rgba(129,140,248,0.12); color:var(--accent); }
        .tl-badge.note { background:rgba(100,116,139,0.1); color:var(--text-dim); }
        .tl-badge.backfill { background:rgba(100,116,139,0.08); color:var(--text-muted); }
        .tl-body { flex:1; }
        .tl-summary { color:var(--text); line-height:1.45; }
        .tl-meta { font-size:11px; color:var(--text-muted); margin-top:3px; }
        .tl-outcome-failed { color:var(--error); font-weight:600; }
        .tl-outcome-worked { color:var(--success); font-weight:600; }

        /* ── Auto-capture badge (Timeline) ── */
        .tl-auto-badge {
            display:inline-block; font-size:9px; font-weight:700;
            padding:1px 6px; border-radius:3px; margin-left:6px;
            background:rgba(99,102,241,0.12); color:#818cf8;
            text-transform:uppercase; letter-spacing:0.3px; vertical-align:middle;
        }
        .tl-capture-source { color:var(--text-muted); font-size:10px; font-style:italic; }

        /* ── Auto-capture stats (ROI) ── */
        .roi-capture-stats {
            display:grid; grid-template-columns:repeat(4,1fr); gap:12px;
            margin-bottom:20px;
        }
        .roi-capture-stat {
            background:var(--surface); border:1px solid var(--border);
            border-radius:10px; padding:14px 16px; text-align:center;
            border-top:3px solid var(--accent);
        }
        .roi-capture-stat.green { border-top-color:var(--success); }
        .roi-capture-stat.amber { border-top-color:var(--warning); }
        .roi-capture-stat.purple { border-top-color:var(--accent); }
        .roi-capture-stat.blue { border-top-color:var(--primary); }
        .roi-capture-stat-value { font-size:24px; font-weight:800; color:var(--text); }
        .roi-capture-stat-label { font-size:11px; color:var(--text-dim); margin-top:4px; font-weight:500; }

        /* ── Churn heatmap (ROI) ── */
        .churn-row {
            display:flex; align-items:center; gap:10px; padding:6px 0;
            border-bottom:1px solid rgba(255,255,255,0.03);
        }
        .churn-row:last-child { border-bottom:none; }
        .churn-file { flex:0 0 180px; font-size:11px; color:var(--text-dim); font-family:monospace; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
        .churn-bar-track { flex:1; height:16px; background:var(--surface2); border-radius:3px; overflow:hidden; }
        .churn-bar-fill { height:100%; border-radius:3px; transition:width 0.6s ease; }
        .churn-bar-fill.high { background:var(--error); }
        .churn-bar-fill.medium { background:var(--warning); }
        .churn-bar-fill.low { background:var(--success); }
        .churn-count { flex:0 0 60px; font-size:11px; color:var(--text-dim); text-align:right; font-weight:600; }
        .churn-severity {
            flex:0 0 50px; font-size:9px; font-weight:700; text-align:center;
            padding:2px 6px; border-radius:3px; text-transform:uppercase;
        }
        .churn-severity.high { background:rgba(239,68,68,0.12); color:var(--error); }
        .churn-severity.medium { background:rgba(245,158,11,0.12); color:var(--warning); }
        .churn-severity.low { background:rgba(16,185,129,0.12); color:var(--success); }
        .churn-empty { color:var(--text-muted); font-size:12px; padding:16px; text-align:center; }

        /* ── Animated counter ── */
        @keyframes fadeInUp { from { opacity:0; transform:translateY(8px); } to { opacity:1; transform:translateY(0); } }
        .animate-in { animation: fadeInUp 0.4s ease forwards; }

        /* ═══ Overview (landing) ═══ */
        .ov-scroll { height:100%; overflow-y:auto; padding:22px 26px 30px; }
        .ov-head { display:flex; align-items:flex-start; justify-content:space-between; margin-bottom:16px; max-width:1320px; }
        .ov-title { font-size:20px; font-weight:800; color:var(--navy); letter-spacing:-0.3px; }
        .ov-sub { font-size:12.5px; color:var(--text-dim); margin-top:3px; }
        .ov-pill { display:inline-flex; align-items:center; gap:7px; background:#fff; border:1px solid var(--border);
                   border-radius:20px; padding:6px 13px; font-size:11.5px; color:var(--text-dim); font-weight:600; white-space:nowrap; }
        .ov-g { width:8px; height:8px; border-radius:50%; background:var(--success); box-shadow:0 0 7px var(--success); }
        .ov-grid { display:grid; grid-template-columns:1fr 1fr; gap:16px; max-width:1320px; }
        .ov-card { background:var(--surface); border:1px solid var(--border); border-radius:14px;
                   padding:16px 18px; box-shadow:0 1px 0 rgba(16,47,82,.03); }
        .ov-ph { display:flex; align-items:center; gap:9px; margin-bottom:3px; }
        .ov-tag { width:26px; height:26px; border-radius:8px; display:flex; align-items:center; justify-content:center; }
        .ov-tag svg { width:15px; height:15px; fill:none; stroke:#fff; stroke-width:2; stroke-linecap:round; stroke-linejoin:round; }
        .ov-ph h2 { font-size:14.5px; font-weight:700; color:var(--navy); }
        .ov-d { font-size:11.5px; color:var(--text-muted); }
        .ov-jump { margin-left:auto; font-size:11px; font-weight:700; color:var(--primary); cursor:pointer; opacity:.8; }
        .ov-jump:hover { opacity:1; text-decoration:underline; }
        .ov-psub { font-size:11.5px; color:var(--text-dim); margin:0 0 12px 35px; }
        /* heatmap rows */
        .ov-row { display:flex; align-items:center; gap:10px; margin:8px 0; }
        .ov-row .fn { width:150px; font:12px ui-monospace,Menlo,monospace; color:#33455E; text-align:right;
                      white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
        .ov-bar { height:19px; border-radius:5px; position:relative; flex:1; background:var(--surface2); overflow:hidden; }
        .ov-bar i { position:absolute; left:0; top:0; bottom:0; border-radius:5px; display:block; transition:width .7s cubic-bezier(.4,0,.2,1); }
        .ov-row .n { width:78px; font-size:11px; color:var(--text-dim); font-weight:600; }
        .ov-row .n b { color:var(--error); }
        .ov-empty { color:var(--text-muted); font-size:12px; padding:18px 4px; }
        .ov-legend { display:flex; gap:14px; align-items:center; margin-top:11px; font-size:11px; color:var(--text-dim); }
        .ov-sw { display:inline-block; width:26px; height:9px; border-radius:3px; vertical-align:middle; margin-right:5px; }
        /* roi */
        .ov-roi { display:grid; grid-template-columns:1fr 132px; grid-auto-rows:auto; gap:12px; }
        .ov-stat { background:var(--surface2); border:1px solid var(--border); border-radius:11px; padding:12px 14px; }
        .ov-stat .k { font-size:10.5px; color:var(--text-dim); font-weight:700; letter-spacing:.3px; }
        .ov-stat .v { font-size:25px; font-weight:800; color:var(--navy); margin-top:5px; line-height:1; }
        .ov-stat .v small { font-size:13px; font-weight:700; color:var(--text-muted); }
        .ov-stat .t { font-size:10px; color:var(--success); font-weight:700; margin-top:6px; }
        .ov-gauge { grid-row:1/3; background:var(--surface2); border:1px solid var(--border); border-radius:11px;
                    display:flex; flex-direction:column; align-items:center; justify-content:center; padding:6px; }
        .ov-gauge .lbl { font-size:10.5px; color:var(--text-dim); font-weight:700; margin-bottom:2px; }
        .ov-gauge .gr { font-size:12.5px; color:var(--text-dim); font-weight:700; margin-top:2px; }
        /* mini map */
        .ov-mapwrap { display:flex; justify-content:center; }
        #ov-map .ovn-label { font:11px ui-monospace,Menlo,monospace; fill:#33455E; }
        /* timeline swimlanes */
        .ov-lane { display:flex; align-items:center; gap:8px; margin:9px 0; }
        .ov-lane .ln { width:72px; font-size:11.5px; font-weight:700; text-align:right; }
        .ov-track { flex:1; height:22px; position:relative; border-bottom:1px dashed var(--border-light); }
        .ov-ev { position:absolute; top:3px; width:14px; height:14px; border-radius:50%; border:2px solid #fff;
                 transform:translateX(-50%); box-shadow:0 1px 2px rgba(16,47,82,.18); cursor:pointer; transition:transform .12s; }
        .ov-ev:hover { transform:translateX(-50%) scale(1.45); box-shadow:0 2px 7px rgba(16,47,82,.30); z-index:2; }
        .ov-axis { display:flex; justify-content:space-between; margin:6px 0 0 80px; font:10.5px ui-monospace,Menlo,monospace; color:var(--text-muted); }
        .ov-foot { display:flex; gap:16px; align-items:center; margin-top:12px; font-size:11px; color:var(--text-dim); flex-wrap:wrap; }
        /* full-width rows inside the 2-col overview grid */
        .ov-wide { grid-column:1 / -1; }
        /* ── Memory Card (shareable hero) ── */
        .mc-wrap { display:grid; grid-template-columns:1.3fr .82fr; gap:18px; align-items:stretch; }
        .mc-card { background:linear-gradient(150deg,#0B2A4A,#123B66 55%,#0E2F52); border-radius:15px;
                   padding:24px 26px; color:#EAF2FD; position:relative; overflow:hidden; }
        .mc-card:after { content:""; position:absolute; inset:0; pointer-events:none;
                   background:radial-gradient(circle at 88% 6%, rgba(31,111,235,.42), transparent 52%); }
        .mc-top { display:flex; justify-content:space-between; align-items:flex-start; position:relative; z-index:1; gap:14px; }
        .mc-proj { font-size:19px; font-weight:800; letter-spacing:-.2px; }
        .mc-sub { font-size:11.5px; color:#9DBBE0; margin-top:3px; font-family:ui-monospace,Menlo,monospace; }
        .mc-grade { width:74px; height:74px; border-radius:50%; border:3px solid var(--success); flex-shrink:0;
                    display:flex; flex-direction:column; align-items:center; justify-content:center; background:rgba(22,159,132,.12); }
        .mc-grade b { font-size:29px; font-weight:800; line-height:1; color:#4FD2B4; }
        .mc-grade span { font-size:9px; color:#9DBBE0; margin-top:2px; letter-spacing:.5px; }
        .mc-stats { display:grid; grid-template-columns:repeat(3,1fr); gap:14px; margin-top:20px; position:relative; z-index:1; }
        .mc-stat .v { font-size:25px; font-weight:800; letter-spacing:-.4px; }
        .mc-stat .k { font-size:9.5px; color:#9DBBE0; letter-spacing:1px; text-transform:uppercase; margin-top:2px; font-weight:700; }
        .mc-line { margin-top:18px; padding-top:13px; border-top:1px solid rgba(255,255,255,.13);
                   display:flex; justify-content:space-between; align-items:center; gap:12px; position:relative; z-index:1; }
        .mc-quote { font-size:12px; color:#C9DDF5; line-height:1.5; }
        .mc-mark { font-size:10.5px; font-weight:700; color:#7FB2F2; font-family:ui-monospace,Menlo,monospace; white-space:nowrap; }
        .mc-acts { display:flex; flex-direction:column; gap:8px; }
        .mc-btn { display:flex; align-items:center; gap:9px; background:var(--surface); border:1px solid var(--border-light);
                  border-radius:10px; padding:10px 13px; font-size:12.5px; font-weight:600; cursor:pointer;
                  color:var(--text); font-family:inherit; text-align:left; transition:.15s; text-decoration:none; }
        .mc-btn:hover { border-color:var(--primary); color:var(--primary); }
        .mc-btn.primary { background:var(--primary); color:#fff; border-color:var(--primary); }
        .mc-btn.primary:hover { background:#1A5FCC; color:#fff; }
        .mc-btn .ic { width:18px; text-align:center; flex-shrink:0; }
        .mc-btn.star { background:var(--navy); color:#EAF2FD; border-color:var(--navy); }
        .mc-btn.star:hover { background:#123B66; color:#fff; }
        .mc-note { font-size:10.5px; color:var(--text-muted); line-height:1.5; margin-top:2px; }
        /* ── Case files ── */
        .cw-grid { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
        .cw { background:var(--surface2); border:1px solid var(--border); border-radius:12px; padding:13px 15px;
              cursor:pointer; position:relative; transition:.15s; }
        .cw:hover { border-color:var(--primary); background:#fff; box-shadow:0 6px 20px rgba(31,111,235,.10); transform:translateY(-1px); }
        .cw .cid { font:10px ui-monospace,Menlo,monospace; color:var(--text-muted); font-weight:700; letter-spacing:.5px; }
        .cw .prob { font-size:12.5px; font-weight:600; line-height:1.42; margin:5px 0 9px; color:var(--navy);
                    display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
        .cw .chain { display:flex; align-items:center; gap:6px; flex-wrap:wrap; }
        .cpill { padding:2px 8px; border-radius:20px; font-size:9.5px; font-weight:800; letter-spacing:.4px; }
        .cp-issue { background:rgba(232,89,59,.12); color:var(--error); }
        .cp-try { background:rgba(232,163,59,.16); color:#9A6B12; }
        .cp-fix { background:rgba(22,159,132,.13); color:var(--success); }
        .cp-open { background:rgba(31,111,235,.10); color:var(--primary); }
        .cw .arw { color:var(--text-muted); font-size:11px; }
        .cw .file { font:10.5px ui-monospace,Menlo,monospace; color:var(--text-dim); margin-top:9px;
                    padding-top:9px; border-top:1px dashed var(--border); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
        .cw .go { position:absolute; top:12px; right:14px; font-size:10px; color:var(--primary); font-weight:700; opacity:0; transition:.15s; }
        .cw:hover .go { opacity:1; }
        /* ── Case modal ── */
        .cm-back { position:fixed; inset:0; background:rgba(11,42,74,.42); backdrop-filter:blur(2px);
                   display:none; align-items:center; justify-content:center; z-index:80; padding:26px; }
        .cm-back.on { display:flex; }
        .cm { background:var(--surface); border:1px solid var(--border-light); border-radius:15px; width:min(680px,100%);
              max-height:86vh; display:flex; flex-direction:column; box-shadow:0 24px 60px rgba(11,42,74,.28); overflow:hidden; }
        .cm-h { padding:15px 20px; border-bottom:1px solid var(--border); background:var(--surface2);
                display:flex; justify-content:space-between; align-items:center; gap:12px; }
        .cm-h .t { font-size:14px; font-weight:700; color:var(--navy); }
        .cm-h .d { font-size:11px; color:var(--text-muted); white-space:nowrap; }
        .cm-x { cursor:pointer; border:none; background:none; font-size:19px; color:var(--text-muted); line-height:1; font-family:inherit; }
        .cm-b { padding:18px 20px; overflow-y:auto; }
        .cm-step { display:flex; gap:12px; padding-bottom:15px; position:relative; }
        .cm-step:not(:last-child):before { content:""; position:absolute; left:11px; top:26px; bottom:1px; width:2px; background:var(--border-light); }
        .cm-dot { width:24px; height:24px; border-radius:50%; flex-shrink:0; display:flex; align-items:center;
                  justify-content:center; font-size:11px; font-weight:800; color:#fff; z-index:1; }
        .cm-k { font-size:9.5px; font-weight:800; letter-spacing:.8px; text-transform:uppercase; margin-bottom:3px; }
        .cm-s { font-size:12.5px; line-height:1.55; color:var(--text); }
        .cm-tag { display:inline-block; background:var(--surface3); border-radius:5px; padding:1px 6px;
                  font:10.5px ui-monospace,Menlo,monospace; color:var(--text-dim); }
        .cm-f { padding:11px 20px; border-top:1px solid var(--border); background:var(--surface2);
                display:flex; gap:10px; align-items:center; font-size:11.5px; color:var(--text-dim); }
        .tl-item.clickable { cursor:pointer; }
        .tl-item.clickable:hover { background:var(--surface2); }
        @media (max-width:1080px){ .ov-grid{ grid-template-columns:1fr; } .mc-wrap{ grid-template-columns:1fr; } .cw-grid{ grid-template-columns:1fr; } }

        /* ═══ Showoff — dark cinema stage ═══ */
        .so-wrap { display:flex; flex-direction:column; height:100%; background:#070c16; }
        .so-bar { display:flex; align-items:center; gap:10px; padding:10px 14px; border-bottom:1px solid #1c2942; flex-wrap:wrap; }
        .so-scenes { display:flex; gap:6px; }
        .so-scn, .so-btn, .so-spd { cursor:pointer; border:1px solid #1c2942; background:#10203a; color:#cdd9ec;
            border-radius:8px; padding:6px 12px; font-size:12px; font-weight:600; font-family:inherit; }
        .so-scn:hover, .so-btn:hover, .so-spd:hover { border-color:#1F6FEB; }
        .so-scn.active, .so-spd.active, .so-btn.active { background:#1F6FEB; border-color:#1F6FEB; color:#fff; }
        .so-btn.rec { color:#ff8a70; }
        .so-btn.rec.on { background:#E8593B; border-color:#E8593B; color:#fff; }
        .so-speed { display:flex; gap:4px; }
        .so-flex { flex:1; }
        #so-scrub { width:180px; accent-color:#3FE0B0; }
        .so-stage { flex:1; position:relative; min-height:0; }
        #so-canvas { position:absolute; inset:0; cursor:crosshair; }
        #so-card { position:absolute; right:14px; top:14px; width:290px; background:rgba(8,14,26,0.94);
            border:1px solid #20304e; border-radius:12px; padding:14px; display:none; color:#e6edf7; z-index:4; }
        #so-card h3 { margin:0 0 6px; font-size:14px; color:#e6edf7; }
        #so-card .so-row { font-size:12px; color:#9fb0c8; margin-top:5px; line-height:1.5; word-break:break-word; }
        #so-card .so-row b { color:#cdd9ec; }
        #so-card .so-dim { color:#6b7a92; font-size:11px; margin-top:10px; }
        .so-hint { position:absolute; left:14px; bottom:12px; color:#6b7a92; font-size:11px; pointer-events:none; z-index:3; }
        .so-foot { padding:8px 14px; font-size:11px; color:#6b7a92; border-top:1px solid #1c2942; }
    </style>
</head>
<body>

    <div class="app">

    <!-- Sidebar -->
    <aside class="side">
        <div class="brand">
            <span class="logo-mark">
                <svg width="30" height="30" viewBox="0 0 32 32" fill="none">
                    <defs><linearGradient id="lm" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#2D7DF6"/><stop offset="1" stop-color="#169F84"/></linearGradient></defs>
                    <rect width="32" height="32" rx="9" fill="url(#lm)"/>
                    <path d="M16 7 L24 16 L16 25 L8 16 Z" stroke="#fff" stroke-width="2" fill="none" stroke-linejoin="round"/>
                    <circle cx="16" cy="16" r="2.6" fill="#fff"/>
                </svg>
            </span>
            <span class="brand-name" id="brand-name" title="project">project</span>
        </div>

        <div class="navlbl">VISUALIZE</div>
        <button class="nav active" data-panel="overview">
            <svg class="nic" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>
            <span>Overview</span></button>
        <button class="nav" data-panel="story">
            <svg class="nic" viewBox="0 0 24 24"><path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.07-2.14-.22-4.05 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.15.43-2.29 1-3a2.5 2.5 0 0 0 2.5 2.5z"/></svg>
            <span>Story Map</span></button>
        <button class="nav" data-panel="roi">
            <svg class="nic" viewBox="0 0 24 24"><polyline points="22 7 13.5 15.5 8.5 10.5 2 17"/><polyline points="16 7 22 7 22 13"/></svg>
            <span>ROI Dashboard</span></button>
        <button class="nav" data-panel="map">
            <svg class="nic" viewBox="0 0 24 24"><circle cx="12" cy="5" r="2.6"/><circle cx="5" cy="19" r="2.6"/><circle cx="19" cy="19" r="2.6"/><line x1="12" y1="7.5" x2="5.8" y2="16.6"/><line x1="12" y1="7.5" x2="18.2" y2="16.6"/></svg>
            <span>Project Map</span></button>
        <button class="nav" data-panel="timeline">
            <svg class="nic" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15 14"/></svg>
            <span>Timeline</span></button>
        <button class="nav" data-panel="showoff">
            <svg class="nic" viewBox="0 0 24 24"><polygon points="6 4 20 12 6 20 6 4"/></svg>
            <span>Showoff</span></button>

        <div class="navlbl">WORKSPACE</div>
        <div class="ws-stats">
            <div class="ws-stat"><span class="wv" id="ws-events">0</span><span class="wl">events</span></div>
            <div class="ws-stat"><span class="wv" id="ws-fixes">0</span><span class="wl">fixes</span></div>
            <div class="ws-stat"><span class="wv" id="ws-grade">—</span><span class="wl">grade</span></div>
        </div>

        <a class="side-star" href="https://github.com/riponcm/projectmem" target="_blank" rel="noopener">
            <span class="ss-star">★</span>
            <span class="ss-txt">Star projectmem<span class="ss-sub">it's free — stars help it spread</span></span>
            <span class="ss-go">↗</span>
        </a>

        <div class="side-ft">
            Generated from<br><b>.projectmem/events.jsonl</b><br>
            <span style="opacity:.75">100% local · no telemetry</span><br>
            <b style="color:#7FB2F2">$ pjm visualize</b>
        </div>
    </aside>

    <!-- Main -->
    <div class="main-area">

    <!-- Panels -->
    <div class="panels">

        <!-- Overview — all four at a glance -->
        <div class="panel active" id="panel-overview">
          <div class="ov-scroll">
            <div class="ov-head">
              <div>
                <h1 class="ov-title">Workspace overview</h1>
                <div class="ov-sub">A single live view of what your project has learned — failures, ROI, structure, and history.</div>
              </div>
              <span class="ov-pill"><span class="ov-g"></span> live · regenerated on every event</span>
            </div>
            <div class="ov-grid">

              <!-- 0. Memory Card — the shareable hero -->
              <section class="ov-card ov-wide">
                <div class="ov-ph"><span class="ov-tag" style="background:var(--navy)"><svg viewBox="0 0 24 24"><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H19v18H6.5A2.5 2.5 0 0 1 4 18.5z"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="9" y1="12" x2="15" y2="12"/></svg></span>
                  <h2>Memory Card</h2><span class="ov-d">what this project has saved</span></div>
                <div class="ov-psub">Rendered locally from your own events — nothing uploads. Share it, or keep it.</div>
                <div class="mc-wrap">
                  <div class="mc-card" id="mc-card">
                    <div class="mc-top">
                      <div><div class="mc-proj" id="mc-proj">project</div><div class="mc-sub" id="mc-sub">0 events</div></div>
                      <div class="mc-grade" id="mc-grade-ring"><b id="mc-grade">—</b><span id="mc-score">0/100</span></div>
                    </div>
                    <div class="mc-stats">
                      <div class="mc-stat"><div class="v" id="mc-tok">0</div><div class="k">Tokens saved</div></div>
                      <div class="mc-stat"><div class="v" id="mc-hrs">0h</div><div class="k">Hours saved</div></div>
                      <div class="mc-stat"><div class="v" id="mc-cases">0</div><div class="k">Cases closed</div></div>
                    </div>
                    <div class="mc-line"><div class="mc-quote" id="mc-quote"></div><div class="mc-mark">projectmem.dev</div></div>
                  </div>
                  <div class="mc-acts">
                    <button class="mc-btn primary" id="mc-png"><span class="ic">↓</span> Download card (PNG)</button>
                    <button class="mc-btn" id="mc-badge"><span class="ic">⌘</span> Copy README badge</button>
                    <a class="mc-btn star" id="mc-star" href="https://github.com/riponcm/projectmem" target="_blank" rel="noopener"><span class="ic">★</span> Star projectmem on GitHub</a>
                    <a class="mc-btn" id="mc-x" href="#" target="_blank" rel="noopener"><span class="ic">𝕏</span> Post to X</a>
                    <div class="mc-note">The card is drawn on your machine from <span class="cm-tag">.projectmem/events.jsonl</span>. No account, no upload, no telemetry.</div>
                  </div>
                </div>
              </section>

              <!-- 1. Story Map: failure heatmap -->
              <section class="ov-card">
                <div class="ov-ph"><span class="ov-tag" style="background:var(--error)"><svg viewBox="0 0 24 24"><path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.07-2.14-.22-4.05 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.15.43-2.29 1-3a2.5 2.5 0 0 0 2.5 2.5z"/></svg></span>
                  <h2>Story Map</h2><span class="ov-d">failure heatmap</span>
                  <span class="ov-jump" data-go="story">open ↗</span></div>
                <div class="ov-psub">Which files burned the most effort — length = effort, color = failure intensity.</div>
                <div id="ov-story"></div>
              </section>

              <!-- 2. ROI Dashboard: cards + grade gauge -->
              <section class="ov-card">
                <div class="ov-ph"><span class="ov-tag" style="background:var(--success)"><svg viewBox="0 0 24 24"><polyline points="22 7 13.5 15.5 8.5 10.5 2 17"/><polyline points="16 7 22 7 22 13"/></svg></span>
                  <h2>ROI Dashboard</h2><span class="ov-d">value captured</span>
                  <span class="ov-jump" data-go="roi">open ↗</span></div>
                <div class="ov-psub">Tokens, hours and dollars saved — plus a prevention grade from A+ to F.</div>
                <div class="ov-roi">
                  <div class="ov-stat"><div class="k">TOKENS SAVED</div><div class="v" id="ov-tok">0</div><div class="t" id="ov-tok-t">▲ since project start</div></div>
                  <div class="ov-gauge">
                    <div class="lbl">Prevention grade</div>
                    <svg width="120" height="94" viewBox="0 0 120 94"><g id="ov-gauge-g"></g></svg>
                    <div class="gr" id="ov-grade-sub">0 / 100</div>
                  </div>
                  <div class="ov-stat"><div class="k">HOURS SAVED</div><div class="v" id="ov-hrs">0<small> h</small></div><div class="t">▲ repeat-fix time avoided</div></div>
                  <div class="ov-stat"><div class="k">USD SAVED <span style="color:var(--text-muted)">(API)</span></div><div class="v" id="ov-usd">$0</div><div class="t">▲ compounds each session</div></div>
                </div>
              </section>

              <!-- 3. Case files: issue -> attempts -> fix -->
              <section class="ov-card ov-wide">
                <div class="ov-ph"><span class="ov-tag" style="background:var(--primary)"><svg viewBox="0 0 24 24"><path d="M4 6.5A2.5 2.5 0 0 1 6.5 4h4l2 2.5h5A2.5 2.5 0 0 1 20 9v8.5a2.5 2.5 0 0 1-2.5 2.5h-11A2.5 2.5 0 0 1 4 17.5z"/></svg></span>
                  <h2>Case files</h2><span class="ov-d" id="cw-count">no cases yet</span>
                  <span class="ov-jump" data-go="timeline">open ↗</span></div>
                <div class="ov-psub">Every problem with its full chain — what broke, what was tried, what worked. Click a case to read it.</div>
                <div class="cw-grid" id="cw-grid"></div>
                <div class="ov-foot" id="cw-foot"></div>
              </section>

              <!-- 4. Timeline: swimlanes -->
              <section class="ov-card ov-wide">
                <div class="ov-ph"><span class="ov-tag" style="background:var(--warning)"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15 14"/></svg></span>
                  <h2>Timeline</h2><span class="ov-d">project history</span>
                  <span class="ov-jump" data-go="timeline">open ↗</span></div>
                <div class="ov-psub">issues → attempts → fixes → decisions, laid out over time.</div>
                <div id="ov-timeline"></div>
                <div class="ov-axis" id="ov-axis"></div>
                <div class="ov-foot" id="ov-foot"></div>
              </section>

            </div>
          </div>
        </div>

        <!-- Story Map -->
        <div class="panel sm-tree" id="panel-story">
            <div class="map-view-toggle sm-toggle">
                <button class="map-view-btn active" data-smview="tree">Treemap</button>
                <button class="map-view-btn" data-smview="lanes">Lanes</button>
                <button class="map-view-btn" data-smview="graph">Graph</button>
            </div>
            <div class="sm-view" id="sm-tree">
                <div class="sm-head"><h3>Effort treemap</h3>
                    <span class="d">area = events on the file · colour = issues and failed attempts</span>
                    <span class="d r" id="tm-foot"></span></div>
                <div class="tm-wrap" id="tm-wrap"></div>
                <div class="sm-legend">
                    <span><i class="sm-sw" style="background:#DCE6F1"></i>quiet</span>
                    <span><i class="sm-sw" style="background:rgba(232,89,59,.24)"></i>1</span>
                    <span><i class="sm-sw" style="background:rgba(232,89,59,.55)"></i>2</span>
                    <span><i class="sm-sw" style="background:#E8593B"></i>3+ issues / failed attempts</span>
                    <span style="margin-left:auto">Click a file for its dossier</span>
                </div>
            </div>
            <div class="sm-view" id="sm-lanes">
                <div class="sm-head"><h3>Story lanes</h3>
                    <span class="d">one lane per file, left to right in time</span>
                    <span class="r" id="sl-range"></span></div>
                <div id="sl-rows"></div>
                <div class="sl-axis" id="sl-axis"></div>
                <div class="sm-legend" id="sl-legend"></div>
            </div>
            <div class="story-controls">
                <button class="story-control-btn" id="story-file-collapse">Collapse dense files</button>
                <button class="story-control-btn" id="story-directory-collapse">Collapse directories</button>
                <button class="story-control-btn" id="story-expand-all">Expand all</button>
                <button class="story-control-btn" id="story-reset-focus">Reset focus</button>
            </div>
            <svg id="canvas"></svg>
            <div class="map-legend">
                <div class="map-legend-item"><div class="dot" style="background:var(--primary)"></div> Issue / Event</div>
                <div class="map-legend-item"><div class="dot" style="background:var(--success)"></div> Fix</div>
                <div class="map-legend-item"><div class="dot" style="background:var(--error)"></div> Failed Attempt</div>
                <div class="map-legend-item"><div class="dot" style="background:var(--warning)"></div> File (warm = more failures)</div>
                <div class="map-legend-item"><div class="dot" style="background:var(--accent)"></div> Decision</div>
                <div class="map-legend-item"><div class="dot" style="background:#dbeafe;border:2px solid #2563eb"></div> Collapsed file</div>
                <div class="map-legend-item"><div class="dot" style="background:#ecfeff;border:2px solid #0891b2"></div> Directory bubble</div>
                <div class="map-legend-item" style="margin-top:4px;font-size:10px;color:var(--text-muted)">Scroll to zoom &middot; Drag to pan &middot; Click files to focus</div>
            </div>
            <div class="map-tooltip" id="tooltip"></div>
        </div>

        <!-- ROI Dashboard -->
        <div class="panel" id="panel-roi">
            <div class="roi-scroll">
                <div class="roi-container">
                    <div class="roi-top" id="roi-top"></div>
                    <!-- Auto-Capture Stats -->
                    <div class="roi-capture-stats" id="roi-capture-stats"></div>
                    <div class="roi-charts">
                        <div class="roi-card">
                            <div class="roi-card-title">Token Savings Breakdown</div>
                            <div id="roi-bars"></div>
                        </div>
                        <div class="roi-card">
                            <div class="roi-card-title">Distribution</div>
                            <div class="roi-donut-wrap">
                                <svg id="roi-donut" width="160" height="160"></svg>
                                <div class="roi-donut-legend" id="roi-donut-legend"></div>
                            </div>
                        </div>
                    </div>
                    <!-- Capture Source Donut -->
                    <div class="roi-charts">
                        <div class="roi-card">
                            <div class="roi-card-title">Capture Sources</div>
                            <div class="roi-donut-wrap">
                                <svg id="roi-source-donut" width="160" height="160"></svg>
                                <div class="roi-donut-legend" id="roi-source-legend"></div>
                            </div>
                        </div>
                        <div class="roi-card">
                            <div class="roi-card-title">File Churn (Top 10)</div>
                            <div id="roi-churn"></div>
                        </div>
                    </div>
                    <div class="roi-card roi-area-card">
                        <div class="roi-card-title">Cumulative Savings Over Time</div>
                        <svg class="roi-area-chart" id="roi-area"></svg>
                    </div>
                </div>
            </div>
        </div>

        <!-- Project Map -->
        <div class="panel" id="panel-map">
            <div class="map-split">
                <div class="map-graph-pane">
                    <div class="map-view-toggle">
                        <button class="map-view-btn active" data-view="flow">Flow</button>
                        <button class="map-view-btn" data-view="tree">Tree</button>
                        <button class="map-view-btn" data-view="graph">Graph</button>
                    </div>
                    <div class="map-details-toggle">
                        <button class="map-view-btn" id="map-details-btn" title="show / hide the PROJECT_MAP.md pane">Hide details</button>
                    </div>
                    <svg id="map-canvas"></svg>
                    <svg id="map-tree"></svg>
                    <div id="tree-filter-host"></div>
                    <div id="map-flow"></div>
                    <div class="map-legend">
                        <div class="map-legend-item"><div class="dot" style="background:var(--accent)"></div> Folder</div>
                        <div class="map-legend-item"><div class="dot" style="background:var(--primary)"></div> File / Module</div>
                        <div class="map-legend-item" style="color:var(--text-muted);margin-top:4px;font-size:10px;">Hover for full paths</div>
                    </div>
                </div>
                <div class="map-text-pane" id="map-content"></div>
            </div>
        </div>

        <!-- Timeline -->
        <div class="panel" id="panel-timeline">
            <div class="map-view-toggle tl-toggle">
                <button class="map-view-btn active" data-tlview="spine">Spine</button>
                <button class="map-view-btn" data-tlview="list">Details</button>
            </div>
            <div id="tl-spine">
                <div class="tsp-colhead"><span>PROBLEMS</span><span>KNOWLEDGE</span></div>
                <div class="tsp-wrap"><div class="tsp-spine"></div><div id="tsp-body"></div></div>
            </div>
            <div class="timeline-view" id="tl-listwrap">
                <div class="tl-header">
                    <div class="tl-activity" id="tl-activity"></div>
                    <div class="tl-filters" id="tl-filters"></div>
                </div>
                <div id="tl-list"></div>
            </div>
        </div>

        <!-- Showoff — animated story scenes + recorder -->
        <div class="panel" id="panel-showoff">
            <div class="so-wrap">
                <div class="so-bar">
                    <div class="so-scenes">
                        <button class="so-scn active" data-scene="universe">Universe</button>
                        <button class="so-scn" data-scene="orbit">Orbit</button>
                        <button class="so-scn" data-scene="replay">Story Replay</button>
                    </div>
                    <button id="so-play" class="so-btn">Pause</button>
                    <input id="so-scrub" type="range" min="0" max="100" value="0">
                    <div class="so-speed">
                        <button class="so-spd" data-s="0.5">0.5x</button>
                        <button class="so-spd active" data-s="1">1x</button>
                        <button class="so-spd" data-s="2">2x</button>
                    </div>
                    <span class="so-flex"></span>
                    <button id="so-wm" class="so-btn active" title="draw a projectmem badge on the video">badge</button>
                    <select id="so-reclen" class="so-btn" title="recording length (max 60s)">
                        <option value="10">10s</option>
                        <option value="20">20s</option>
                        <option value="30" selected>30s</option>
                        <option value="45">45s</option>
                        <option value="60">60s (max)</option>
                    </select>
                    <button id="so-rec" class="so-btn rec" title="record the stage and download a video">REC</button>
                </div>
                <div class="so-stage">
                    <canvas id="so-canvas"></canvas>
                    <div id="so-card"></div>
                    <div class="so-hint">click a node for details - click it again to release</div>
                </div>
                <div class="so-foot">Record downloads a .webm video (100% local). Most platforms accept it; X/Twitter prefers mp4 - convert or screen-record if needed.</div>
            </div>
        </div>
    </div>
    </div><!-- /main-area -->
    </div><!-- /app -->

    <script>
    // ── Data Injection ──
    const data = {{GRAPH_DATA}};
    const projectMap = {{PROJECT_MAP}};
    const projectMapGraph = {{PROJECT_MAP_GRAPH}};
    const timelineData = {{TIMELINE_DATA}};
    const score = {{SCORE_DATA}};
    const projectName = {{PROJECT_NAME}};

    // Every string below comes from events.jsonl / PROJECT_MAP.md — i.e. from
    // git commit messages, CI output and AI agents. None of it is trusted, and
    // most of it lands in innerHTML. Escape at the sink, always.
    function pmEsc(v) {
        return String(v == null ? '' : v)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    // Compact number formatting so big stats fit the cards: 10k+ -> K, 1M+ -> M,
    // 1B+ -> B. Below 10,000 keep the exact comma-grouped value (and decimals,
    // e.g. USD). One decimal, trailing .0 dropped: 70000->70K, 12500->12.5K,
    // 1500000->1.5M, 280000->280K.
    function fmtNum(n) {
        const abs = Math.abs(n);
        const one = v => (Math.round(v * 10) / 10).toString();
        if (abs >= 1e9) return one(n / 1e9) + 'B';
        if (abs >= 1e6) return one(n / 1e6) + 'M';
        if (abs >= 1e4) return one(n / 1e3) + 'K';
        return n.toLocaleString();
    }

    // ── Animated Counter ──
    function animateValue(el, end, prefix='', suffix='', duration=800) {
        let start = 0;
        const startTime = performance.now();
        function step(now) {
            const progress = Math.min((now - startTime) / duration, 1);
            const ease = 1 - Math.pow(1 - progress, 3);
            const current = Math.floor(ease * end);
            el.textContent = prefix + fmtNum(current) + suffix;
            if (progress < 1) requestAnimationFrame(step);
            else el.textContent = prefix + fmtNum(end) + suffix;
        }
        requestAnimationFrame(step);
    }

    // ── Sidebar nav + dynamic branding ──
    function activateTab(name) {
        document.querySelectorAll('.nav').forEach(t => t.classList.toggle('active', t.dataset.panel === name));
        document.querySelectorAll('.panel').forEach(p => p.classList.toggle('active', p.id === 'panel-' + name));
        if (name === 'story' && typeof smRender === 'function') requestAnimationFrame(smRender);
    }
    document.querySelectorAll('.nav').forEach(n => n.addEventListener('click', () => activateTab(n.dataset.panel)));
    (function brand() {
        const bn = document.getElementById('brand-name');
        if (bn && projectName) { bn.textContent = projectName; bn.title = projectName; }
        document.title = (projectName || 'projectmem') + ' · visualize';
    })();

    // ── Workspace stats (sidebar) ──
    const resolvedCount = timelineData.filter(e => e.type === 'fix').length;
    const autoCount = timelineData.filter(e => e.auto_captured).length;
    const manualCount = timelineData.length - autoCount;
    animateValue(document.getElementById('ws-events'), timelineData.length);
    animateValue(document.getElementById('ws-fixes'), resolvedCount);
    (function(){ const g = document.getElementById('ws-grade'); if (g) g.textContent = score.grade || '—'; })();

    // ══════════════════════════════════════════
    // TAB 0: OVERVIEW — all four at a glance
    // ══════════════════════════════════════════
    // Delegated: some .ov-jump links (the Case-files footer) are rendered later.
    document.addEventListener('click', ev => {
        const j = ev.target.closest('.ov-jump[data-go]');
        if (j) activateTab(j.dataset.go);
    });

    const OV_NOISE = /__pycache__|\\.pyc$|\\.DS_Store|\\.egg-info/;

    // ── 1. Story Map: failure heatmap (top files by effort) ──
    (function ovStory() {
        const mentions = {};
        data.links.forEach(l => { const t = typeof l.target === 'object' ? l.target.id : l.target; mentions[t] = (mentions[t]||0)+1; });
        const ranked = data.nodes
            .filter(n => n.type === 'file' && !OV_NOISE.test(n.id))
            .map(n => ({ f:n.id, fails:n.failures||0, effort:(n.failures||0)*3 + (mentions[n.id]||0) }))
            .sort((a,b) => b.effort - a.effort || b.fails - a.fails)
            .slice(0, 6);
        const el = document.getElementById('ov-story');
        if (!ranked.length) { el.innerHTML = '<div class="ov-empty">No file activity tracked yet — log an issue or attempt against a file to see it here.</div>'; return; }
        const maxEffort = Math.max(...ranked.map(d => d.effort), 1);
        const heat = f => ['#DCE6F1','#FBD9CF','#F1956F','#E8593B'][Math.min(f,3)];
        el.innerHTML = ranked.map((d,i) => {
            const short = d.f.split('/').slice(-2).join('/');
            const w = Math.max((d.effort/maxEffort)*100, 6);
            return '<div class="ov-row"><div class="fn" title="'+pmEsc(d.f)+'">'+pmEsc(short)+'</div>'
                + '<div class="ov-bar"><i id="ovb-'+i+'" style="width:0%;background:'+heat(d.fails)+'"></i></div>'
                + '<div class="n">'+(d.fails>0?'<b>'+d.fails+'</b> failed':'active')+'</div></div>';
        }).join('') + '<div class="ov-legend"><span><span class="ov-sw" style="background:linear-gradient(90deg,#FBD9CF,#E8593B)"></span>more failed attempts</span>'
            + '<span style="margin-left:auto">'+score.components.failed_approaches+' failed · '+score.components.fixes_with_context+' fixes recorded</span></div>';
        ranked.forEach((d,i) => setTimeout(() => { const b=document.getElementById('ovb-'+i); if(b) b.style.width=Math.max((d.effort/maxEffort)*100,6)+'%'; }, 120+i*70));
    })();

    // ── 2. ROI Dashboard: cards + prevention-grade gauge ──
    (function ovRoi() {
        const v = score.value || {};
        animateValue(document.getElementById('ov-tok'), v.tokens_saved||0, '', '', 1000);
        document.getElementById('ov-hrs').innerHTML = '~'+(v.debugging_hours_saved||0).toFixed(1)+'<small> h</small>';
        document.getElementById('ov-usd').textContent = '$'+(v.usd_saved||0).toFixed(2);
        document.getElementById('ov-grade-sub').textContent = (score.score||0)+' / 100';
        // gauge
        const cx=60, cy=80, r=47, val=Math.max(0, Math.min(100, score.score||0));
        const col = val>=85 ? '#169F84' : val>=70 ? '#1F6FEB' : val>=50 ? '#E8A33B' : '#E8593B';
        const pol = a => { const rad=(180-a*1.8)*Math.PI/180; return [cx+r*Math.cos(rad), cy-r*Math.sin(rad)]; };
        const arc = (a0,a1,c,w) => { const [x0,y0]=pol(a0),[x1,y1]=pol(a1); const big=(a1-a0)>100?1:0;
            return '<path d="M'+x0+' '+y0+' A'+r+' '+r+' 0 '+big+' 1 '+x1+' '+y1+'" fill="none" stroke="'+c+'" stroke-width="'+w+'" stroke-linecap="round"/>'; };
        let s = arc(0,100,'#E7EEF6',11) + arc(0.5,Math.max(val,0.6),col,11);
        const [nx,ny] = pol(val);
        s += '<circle cx="'+nx+'" cy="'+ny+'" r="5.5" fill="#0B2A4A"/>';
        s += '<text x="60" y="64" text-anchor="middle" font-size="30" font-weight="800" fill="#0B2A4A">'+(score.grade||'—')+'</text>';
        document.getElementById('ov-gauge-g').innerHTML = s;
    })();

    // ── 3. Case files: issue -> attempts -> fix ──
    // The one screen no rules file or chat-history tool can draw: a problem,
    // the approaches that did NOT work, and the one that did.
    function pmCases() {
        const byId = {};
        timelineData.forEach(e => { if (e.issue_id) { (byId[e.issue_id] = byId[e.issue_id] || []).push(e); } });
        return Object.keys(byId).map(id => {
            const evs = byId[id].slice().sort((a,b) => new Date(a.timestamp) - new Date(b.timestamp));
            const issue = evs.filter(e => e.type === 'issue')[0];
            const fixes = evs.filter(e => e.type === 'fix');
            const attempts = evs.filter(e => e.type === 'attempt');
            const knowledge = evs.filter(e => e.type === 'decision' || e.type === 'note');
            const located = evs.filter(e => e.location)[0];
            return {
                id: id, evs: evs, issue: issue, fix: fixes[fixes.length - 1],
                attempts: attempts, knowledge: knowledge,
                failed: attempts.filter(a => a.outcome === 'failed').length,
                loc: located ? located.location : '',
                t0: new Date(evs[0].timestamp), t1: new Date(evs[evs.length - 1].timestamp)
            };
        }).sort((a,b) => b.t1 - a.t1);
    }
    const CASES = pmCases();
    const CASE_BY_ID = {};
    CASES.forEach(c => { CASE_BY_ID[c.id] = c; });

    function pmDate(d) {
        return isNaN(d) ? '' : d.toLocaleDateString('en-US', { month:'short', day:'numeric' });
    }

    (function ovCases() {
        const grid = document.getElementById('cw-grid');
        const closed = CASES.filter(c => c.fix).length;
        document.getElementById('cw-count').textContent =
            CASES.length ? closed + ' closed · ' + CASES.length + ' total' : 'no cases yet';
        if (!CASES.length) {
            grid.innerHTML = '<div class="ov-empty">No cases yet — run <span class="cm-tag">pjm log "what broke"</span> ' +
                'or let your agent call <span class="cm-tag">log_issue</span> to open the first one.</div>';
            return;
        }
        grid.innerHTML = CASES.slice(0, 6).map(c => {
            const chain = ['<span class="cpill cp-issue">ISSUE</span>'];
            if (c.attempts.length) {
                chain.push('<span class="arw">→</span><span class="cpill cp-try">' + c.attempts.length +
                           (c.attempts.length === 1 ? ' TRIED' : ' TRIED') + '</span>');
            }
            chain.push('<span class="arw">→</span>' + (c.fix
                ? '<span class="cpill cp-fix">FIXED</span>'
                : '<span class="cpill cp-open">OPEN</span>'));
            const head = c.issue ? c.issue.summary : (c.evs[0] ? c.evs[0].summary : '');
            return '<div class="cw" data-case="' + pmEsc(c.id) + '"><span class="go">open ↗</span>' +
                '<div class="cid">CASE ' + pmEsc(c.id) + '</div>' +
                '<div class="prob">' + pmEsc(head) + '</div>' +
                '<div class="chain">' + chain.join('') + '</div>' +
                (c.loc ? '<div class="file">' + pmEsc(c.loc) + '</div>' : '') +
                '</div>';
        }).join('');
        const ruled = CASES.reduce((n,c) => n + c.failed, 0);
        document.getElementById('cw-foot').innerHTML =
            '<span><b>' + closed + '</b> cases closed</span>' +
            (ruled
                ? '<span><b>' + ruled + '</b> approaches ruled out</span>'
                : '<span><b>' + CASES.reduce((n,c) => n + c.attempts.length, 0) + '</b> approaches tried</span>') +
            '<span><b>' + timelineData.filter(e => e.type === 'decision').length + '</b> decisions</span>' +
            '<span><b>' + timelineData.filter(e => e.type === 'note').length + '</b> gotchas</span>' +
            (CASES.length > 6 ? '<span class="ov-jump" data-go="timeline" style="margin-left:auto">all ' + CASES.length + ' in Timeline ↗</span>' : '');
        grid.addEventListener('click', ev => {
            const card = ev.target.closest('.cw');
            if (card) openCase(card.dataset.case);
        });
    })();

    // ── Case modal — mounted once, opened from Overview AND Timeline ──
    const cmBack = document.createElement('div');
    cmBack.className = 'cm-back';
    cmBack.innerHTML = '<div class="cm"><div class="cm-h"><div class="t" id="cm-t"></div>' +
        '<div style="display:flex;align-items:center;gap:12px"><div class="d" id="cm-d"></div>' +
        '<button class="cm-x" id="cm-x" title="Close">✕</button></div></div>' +
        '<div id="cm-warn"></div><div class="cm-b" id="cm-b"></div><div class="cm-f" id="cm-f"></div></div>';
    document.body.appendChild(cmBack);
    document.getElementById('cm-x').addEventListener('click', closeCase);
    cmBack.addEventListener('click', ev => {
        const more = ev.target.closest('[data-openfile]');
        if (more) openFile(more.dataset.openfile);
    });
    cmBack.addEventListener('click', e => { if (e.target === cmBack) closeCase(); });
    document.addEventListener('keydown', e => { if (e.key === 'Escape') closeCase(); });
    function closeCase() { cmBack.classList.remove('on'); }

    const PM_STYLE = {
        issue:    { c:'var(--error)',   g:'!', k:'Issue' },
        attempt:  { c:'var(--warning)', g:'↻', k:'Attempt' },
        fix:      { c:'var(--success)', g:'✓', k:'Fix' },
        decision: { c:'var(--primary)', g:'◆', k:'Decision' },
        note:     { c:'var(--accent)',  g:'●', k:'Gotcha' }
    };

    function pmSteps(evs) {
        return evs.map(e => {
            const st = PM_STYLE[e.type] || { c:'var(--text-muted)', g:'·', k:e.type };
            const worked = e.outcome === 'worked', failed = e.outcome === 'failed';
            const label = st.k + (failed ? ' — failed' : worked ? ' — worked' : '');
            let glyph = st.g, col = st.c;
            if (e.type === 'attempt') {
                glyph = worked ? '✓' : failed ? '✕' : '↻';
                col = worked ? 'var(--success)' : failed ? 'var(--warning)' : 'var(--text-muted)';
            }
            return '<div class="cm-step"><div class="cm-dot" style="background:' + col + '">' + glyph + '</div>' +
                '<div><div class="cm-k" style="color:' + col + '">' + pmEsc(label) + '</div>' +
                '<div class="cm-s">' + pmEsc(e.summary) +
                (e.location ? ' <span class="cm-tag">' + pmEsc(e.location) + '</span>' : '') + '</div></div></div>';
        }).join('');
    }

    function pmShow(title, meta, stepsHtml, footHtml, bannerHtml) {
        document.getElementById('cm-t').textContent = title;
        document.getElementById('cm-d').textContent = meta;
        document.getElementById('cm-warn').innerHTML = bannerHtml || '';
        document.getElementById('cm-b').innerHTML = stepsHtml;
        document.getElementById('cm-f').innerHTML = footHtml;
        cmBack.classList.add('on');
    }

    function openCase(id) {
        const c = CASE_BY_ID[id];
        if (!c) return;
        const where = c.loc ? c.loc.split('/').slice(-1)[0].split(' ')[0] : '';
        const span = pmDate(c.t0) + (pmDate(c.t1) !== pmDate(c.t0) ? ' → ' + pmDate(c.t1) : '');
        const foot = c.failed
            ? '<b style="color:var(--error)">⚠ ' + c.failed + ' failed approach' + (c.failed === 1 ? '' : 'es') +
              ' on record</b><span>— your agent is warned before trying ' + (c.failed === 1 ? 'it' : 'either') + ' again.</span>'
            : (c.fix ? '<b style="color:var(--success)">✓ Closed</b><span>— the fix is in memory; the next session starts from it.</span>'
                     : '<b style="color:var(--warning)">◷ Still open</b><span>— no fix recorded yet.</span>');
        pmShow('Case ' + id + (where ? ' · ' + where : ''),
               span + ' · ' + c.evs.length + ' events', pmSteps(c.evs), foot);
    }

    // A standalone event (a decision or gotcha with no case) opens on its own.
    function openEvent(e) {
        const st = PM_STYLE[e.type] || { k:e.type };
        const d = new Date(e.timestamp);
        const when = pmDate(d) + (isNaN(d) ? '' : ' · ' + d.toLocaleTimeString([], { hour:'2-digit', minute:'2-digit' }));
        pmShow(st.k, when, pmSteps([e]),
            '<span>Standalone ' + st.k.toLowerCase() + ' — not attached to a case.</span>');
    }

    // ══════════════════════════════════════════
    // TAB 1: STORY MAP — Treemap (default) · Lanes · Graph
    // ══════════════════════════════════════════
    // One file's whole history, grouped from event locations. This is what
    // precheck_file answers on the CLI; here it is, drawn.
    function pmFileKey(loc) {
        if (!loc) return null;
        // Locations are free text ("src/a.ts:12 (and b)", "src/a.ts, src/b.ts"),
        // so trim the separators off or the same file splits into several tiles.
        let p = String(loc).split(' ')[0].split(':')[0].trim();
        while (p.length && ',.;)]'.indexOf(p.slice(-1)) > -1) p = p.slice(0, -1);
        return (p.indexOf('/') > -1 || p.indexOf('.') > -1) ? p : null;
    }
    const FILE_STORIES = (function () {
        const m = {};
        timelineData.forEach(e => {
            const k = pmFileKey(e.location);
            if (k) { (m[k] = m[k] || []).push(e); }
        });
        return Object.keys(m).map(k => {
            const evs = m[k].slice().sort((a,b) => new Date(a.timestamp) - new Date(b.timestamp));
            const c = {};
            evs.forEach(e => { c[e.type] = (c[e.type] || 0) + 1; });
            return { path:k, n:evs.length, c:c, evs:evs,
                     friction:(c.issue || 0) + (c.attempt || 0),
                     failed:evs.filter(e => e.outcome === 'failed').length };
        }).sort((a,b) => b.n - a.n || b.friction - a.friction);
    })();
    const FILE_BY_PATH = {};
    FILE_STORIES.forEach(f => { FILE_BY_PATH[f.path] = f; });
    // Tints of the issue red, so the map reads against the event legend.
    const smHeat = f => f.friction >= 3 ? '#E8593B'
                      : f.friction === 2 ? 'rgba(232,89,59,.55)'
                      : f.friction === 1 ? 'rgba(232,89,59,.24)' : '#DCE6F1';
    const smInk = f => f.friction >= 2 ? '#FFFFFF' : '#13233A';

    function smSplit(items, x, y, w, h, out) {
        if (!items.length) return out;
        if (items.length === 1) { out.push({ f:items[0], x:x, y:y, w:w, h:h }); return out; }
        const tot = items.reduce((n,i) => n + i.n, 0);
        let acc = 0, k = 0;
        for (; k < items.length - 1; k++) { acc += items[k].n; if (acc >= tot / 2) break; }
        const ratio = acc / tot;
        const a = items.slice(0, k + 1), b = items.slice(k + 1);
        if (w > h) { smSplit(a, x, y, w * ratio, h, out); smSplit(b, x + w * ratio, y, w * (1 - ratio), h, out); }
        else { smSplit(a, x, y, w, h * ratio, out); smSplit(b, x, y + h * ratio, w, h * (1 - ratio), out); }
        return out;
    }

    function renderTreemap() {
        const el = document.getElementById('tm-wrap');
        const W = el.clientWidth, H = el.clientHeight;
        if (!W || !H) return;                       // hidden pane: nothing to lay out yet
        if (!FILE_STORIES.length) {
            el.innerHTML = '<div class="ov-empty" style="padding:40px 24px">No file activity yet — log an issue ' +
                'against a file (or let your agent call <span class="cm-tag">log_issue</span>) and it appears here.</div>';
            return;
        }
        const items = FILE_STORIES.slice(0, 24);
        el.innerHTML = smSplit(items, 0, 0, W, H, []).map(r => {
            const short = r.f.path.split('/').slice(-1)[0] || r.f.path;
            const dir = r.f.path.split('/').slice(0, -1).slice(-1)[0] || '';
            const tight = r.w < 96 || r.h < 52;
            return '<div class="tm-tile" data-path="' + pmEsc(r.f.path) + '" title="' + pmEsc(r.f.path) + '" ' +
                'style="left:' + r.x + 'px;top:' + r.y + 'px;width:' + Math.max(r.w - 3, 0) + 'px;height:' +
                Math.max(r.h - 3, 0) + 'px;background:' + smHeat(r.f) + ';color:' + smInk(r.f) + '">' +
                '<div class="tm-fn">' + pmEsc(short) + '</div>' +
                (tight ? '' : '<div class="tm-mt">' + r.f.n + ' events' + (dir ? ' · ' + pmEsc(dir) : '') + '</div>') +
                '</div>';
        }).join('');
        const attached = FILE_STORIES.reduce((n,f) => n + f.n, 0);
        document.getElementById('tm-foot').textContent =
            items.length + ' of ' + FILE_STORIES.length + ' files · ' + attached + ' of ' +
            timelineData.length + ' events are attached to a file';
    }

    // Lanes: default to the whole history, but page through it when a project
    // has years of memory rather than squashing it all into one strip.
    const SL_TIMES = timelineData.map(e => new Date(e.timestamp).getTime()).filter(t => !isNaN(t));
    const SL_MIN = SL_TIMES.length ? Math.min(...SL_TIMES) : 0;
    const SL_MAX = SL_TIMES.length ? Math.max(...SL_TIMES) : 0;
    const DAY = 86400000;
    let slRange = (SL_MAX - SL_MIN) > 365 * DAY ? 365 * DAY : null;   // null = all
    let slEnd = SL_MAX;

    function renderLanes() {
        const rows = document.getElementById('sl-rows');
        if (!FILE_STORIES.length) {
            rows.innerHTML = '<div class="ov-empty">No file activity yet.</div>';
            document.getElementById('sl-axis').innerHTML = '';
            return;
        }
        const t1 = slRange ? slEnd : SL_MAX;
        const t0 = slRange ? t1 - slRange : SL_MIN;
        const span = Math.max(t1 - t0, 1);
        const inWin = e => { const t = new Date(e.timestamp).getTime(); return !isNaN(t) && t >= t0 && t <= t1; };
        const files = FILE_STORIES.filter(f => f.evs.some(inWin)).slice(0, 14);
        rows.innerHTML = files.map(f => {
            const dots = f.evs.filter(inWin).map(e => {
                const t = new Date(e.timestamp).getTime();
                return '<span class="sl-pt" title="' + pmEsc(e.type + ' · ' + (e.summary || '').slice(0, 100)) +
                    '" style="left:' + (2 + ((t - t0) / span) * 96) + '%;background:' + (PM_STYLE[e.type] || {}).c + '"></span>';
            }).join('');
            const iss = f.c.issue || 0;
            return '<div class="sl-row" data-path="' + pmEsc(f.path) + '"><div class="sl-fp">' + pmEsc(f.path) + '</div>' +
                '<div class="sl-track">' + dots + '</div><div class="sl-rt">' + f.n + ' events' +
                (iss ? ' · <b>' + iss + ' issue' + (iss === 1 ? '' : 's') + '</b>' : '') + '</div></div>';
        }).join('') || '<div class="ov-empty">Nothing in this window — widen the range.</div>';
        const fmt = ms => new Date(ms).toLocaleDateString('en-US', { month:'short', day:'numeric', year:'2-digit' });
        const ticks = []; for (let i = 0; i < 6; i++) ticks.push(fmt(t0 + span * i / 5));
        document.getElementById('sl-axis').innerHTML = ticks.map(t => '<span>' + t + '</span>').join('');
        const shown = FILE_STORIES.reduce((n,f) => n + f.evs.filter(inWin).length, 0);
        const total = FILE_STORIES.reduce((n,f) => n + f.n, 0);
        document.getElementById('sl-legend').innerHTML =
            ['issue','attempt','fix','decision','note'].map(k =>
                '<span><i class="sm-sw rnd" style="background:' + PM_STYLE[k].c + '"></i>' +
                (k === 'note' ? 'gotcha' : k) + '</span>').join('') +
            '<span style="margin-left:auto">' + files.length + ' files · ' + shown +
            (shown < total ? ' of ' + total : '') + ' events in view</span>';
        // range controls
        const opts = [[30 * DAY,'30d'], [90 * DAY,'90d'], [365 * DAY,'12m']]
            .filter(o => o[0] < (SL_MAX - SL_MIN));
        const atStart = slRange ? (slEnd - slRange) <= SL_MIN : true;
        const atEnd = slRange ? slEnd >= SL_MAX : true;
        document.getElementById('sl-range').innerHTML =
            (slRange ? '<button class="sm-chip" data-nav="-1"' + (atStart ? ' disabled' : '') + '>◀</button>' : '') +
            opts.map(o => '<button class="sm-chip' + (slRange === o[0] ? ' on' : '') + '" data-range="' + o[0] + '">' + o[1] + '</button>').join('') +
            '<button class="sm-chip' + (slRange ? '' : ' on') + '" data-range="0">All</button>' +
            (slRange ? '<button class="sm-chip" data-nav="1"' + (atEnd ? ' disabled' : '') + '>▶</button>' : '');
    }

    document.getElementById('sl-range').addEventListener('click', ev => {
        const b = ev.target.closest('.sm-chip'); if (!b || b.disabled) return;
        if (b.dataset.range !== undefined) { slRange = +b.dataset.range || null; slEnd = SL_MAX; }
        else if (b.dataset.nav) { slEnd = Math.min(SL_MAX, Math.max(SL_MIN + (slRange || 0), slEnd + (+b.dataset.nav) * (slRange || 0) / 2)); }
        renderLanes();
    });
    document.getElementById('tm-wrap').addEventListener('click', ev => {
        const t = ev.target.closest('.tm-tile'); if (t) openFile(t.dataset.path);
    });
    document.getElementById('sl-rows').addEventListener('click', ev => {
        const r = ev.target.closest('.sl-row'); if (r) openFile(r.dataset.path);
    });

    // The dossier — precheck_file, rendered.
    const FILE_KINDS = {
        failed:    { t:'failed attempts', f:e => e.type === 'attempt' && e.outcome === 'failed' },
        attempts:  { t:'attempts',        f:e => e.type === 'attempt' },
        fixed:     { t:'fixes',           f:e => e.type === 'fix' },
        issues:    { t:'issues',          f:e => e.type === 'issue' },
        decisions: { t:'decisions',       f:e => e.type === 'decision' },
        notes:     { t:'gotchas',         f:e => e.type === 'note' }
    };
    // Resolve a graph node to the file story built from event locations.
    function fileKeyFor(n) {
        return [n.full_path, n.path, n.id].find(k => k && FILE_BY_PATH[k]) || null;
    }

    function openFile(path, kind) {
        const f = FILE_BY_PATH[path];
        if (!f) return;
        const K = kind && FILE_KINDS[kind];
        const evs = K ? f.evs.filter(K.f) : f.evs;
        if (!evs.length) return;
        if (K) {
            pmShow(path, evs.length + ' ' + K.t + ' · of ' + f.n + ' events on this file',
                pmSteps(evs),
                '<span>Showing ' + K.t + ' only.</span>' +
                '<span style="margin-left:auto;color:var(--primary);font-weight:700;cursor:pointer" ' +
                'data-openfile="' + pmEsc(path) + '">Full file history ↗</span>');
            return;
        }
        const first = new Date(f.evs[0].timestamp), last = new Date(f.evs[f.evs.length - 1].timestamp);
        const weeks = Math.max(1, Math.round((last - first) / (7 * DAY)));
        const iss = f.c.issue || 0, notes = f.c.note || 0;
        const banner = (iss || f.failed || notes)
            ? '<div class="cm-warn"><b>Before you touch this file:</b> ' +
              (f.failed ? f.failed + ' approach' + (f.failed === 1 ? '' : 'es') + ' already failed here. ' : '') +
              (iss ? iss + ' issue' + (iss === 1 ? '' : 's') + ' opened against it. ' : '') +
              (notes ? notes + ' gotcha' + (notes === 1 ? '' : 's') + ' recorded. ' : '') +
              'Your agent gets this same brief from <span class="cm-tag">precheck_file</span>.</div>'
            : '';
        pmShow(path,
            f.n + ' events · ' + pmDate(first) + ' → ' + pmDate(last) + ' · ' + weeks + ' week' + (weeks === 1 ? '' : 's') + ' of history',
            pmSteps(f.evs),
            '<span>Every event recorded against this file, oldest first.</span>',
            banner);
    }

    // View switching — treemap and lanes need a visible pane to measure.
    let smView = 'tree';
    function smRender() {
        if (smView === 'tree') renderTreemap();
        else if (smView === 'lanes') renderLanes();
    }
    document.querySelectorAll('.sm-toggle .map-view-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.sm-toggle .map-view-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            smView = btn.dataset.smview;
            const panel = document.getElementById('panel-story');
            panel.classList.remove('sm-tree', 'sm-lanes', 'sm-graph');
            panel.classList.add('sm-' + smView);
            smRender();
        });
    });
    let smResize;
    window.addEventListener('resize', () => { clearTimeout(smResize); smResize = setTimeout(smRender, 180); });

    // ── Memory Card: the shareable hero ──
    (function ovShare() {
        const v = score.value || {};
        const closed = CASES.filter(c => c.fix).length;
        const ruled = CASES.reduce((n,c) => n + c.failed, 0);
        const times = timelineData.map(e => new Date(e.timestamp).getTime()).filter(t => !isNaN(t));
        const days = times.length ? Math.max(1, Math.round((Math.max(...times) - Math.min(...times)) / 86400000)) : 0;
        const tokens = fmtNum(v.tokens_saved || 0);
        const hours = '~' + (v.debugging_hours_saved || 0).toFixed(0) + 'h';
        const grade = score.grade || '—';
        const gcol = (score.score || 0) >= 85 ? '#169F84' : (score.score || 0) >= 70 ? '#1F6FEB'
                   : (score.score || 0) >= 50 ? '#E8A33B' : '#E8593B';

        document.getElementById('mc-proj').textContent = projectName;
        document.getElementById('mc-sub').textContent = timelineData.length + ' events · ' + days + ' days of memory';
        document.getElementById('mc-grade').textContent = grade;
        document.getElementById('mc-score').textContent = (score.score || 0) + '/100';
        document.getElementById('mc-grade-ring').style.borderColor = gcol;
        document.getElementById('mc-tok').textContent = tokens;
        document.getElementById('mc-hrs').textContent = hours;
        document.getElementById('mc-cases').textContent = closed;
        const quote = ruled
            ? '“' + ruled + ' failed approach' + (ruled === 1 ? '' : 'es') + ' on record — my agent gets warned before it repeats any of them.”'
            : closed
            ? '“' + closed + ' case' + (closed === 1 ? '' : 's') + ' closed and remembered — my next session starts where the last one ended.”'
            : '“' + timelineData.length + ' event' + (timelineData.length === 1 ? '' : 's') +
              ' of project memory — my agent opens every session with the context instead of a blank slate.”';
        document.getElementById('mc-quote').textContent = quote;

        // Post to X — the user clicks it; nothing is sent from here.
        const tweet = 'projectmem has saved ' + tokens + ' tokens and ~' + (v.debugging_hours_saved || 0).toFixed(0) +
            'h on ' + projectName + ' — local-first memory for AI coding agents. Prevention grade ' + grade + '.';
        document.getElementById('mc-x').href =
            'https://x.com/intent/post?text=' + encodeURIComponent(tweet) + '&url=' + encodeURIComponent('https://projectmem.dev');

        // README badge — static shields values, generated from these numbers.
        document.getElementById('mc-badge').addEventListener('click', function () {
            const md = '[![projectmem](https://img.shields.io/badge/memory-projectmem-1F6FEB)]' +
                '(https://github.com/riponcm/projectmem) ' +
                '[![prevention ' + grade + '](https://img.shields.io/badge/prevention-' +
                encodeURIComponent(grade + ' (' + (score.score || 0) + '/100)') + '-' + gcol.replace('#','') + ')]' +
                '(https://projectmem.dev)';
            const done = () => { this.innerHTML = '<span class="ic">✓</span> Copied to clipboard'; setTimeout(() => {
                this.innerHTML = '<span class="ic">⌘</span> Copy README badge'; }, 1800); };
            if (navigator.clipboard) { navigator.clipboard.writeText(md).then(done, done); }
            else { done(); }
        });

        // PNG — drawn on a canvas at social size. No upload, no third party.
        document.getElementById('mc-png').addEventListener('click', function () {
            const W = 1200, H = 630, cv = document.createElement('canvas');
            cv.width = W; cv.height = H;
            const x = cv.getContext('2d');
            const bg = x.createLinearGradient(0, 0, W, H);
            bg.addColorStop(0, '#0B2A4A'); bg.addColorStop(0.55, '#123B66'); bg.addColorStop(1, '#0E2F52');
            x.fillStyle = bg; x.fillRect(0, 0, W, H);
            const glow = x.createRadialGradient(W * 0.88, 40, 10, W * 0.88, 40, 520);
            glow.addColorStop(0, 'rgba(31,111,235,0.42)'); glow.addColorStop(1, 'rgba(31,111,235,0)');
            x.fillStyle = glow; x.fillRect(0, 0, W, H);
            const F = 'Inter, -apple-system, Helvetica, sans-serif';
            x.fillStyle = '#EAF2FD'; x.font = '800 62px ' + F; x.textBaseline = 'alphabetic';
            x.fillText(projectName, 78, 156);
            x.fillStyle = '#9DBBE0'; x.font = '500 25px ui-monospace, Menlo, monospace';
            x.fillText(timelineData.length + ' events · ' + days + ' days of memory', 78, 200);
            // grade ring
            const gx = W - 178, gy = 150, gr = 74;
            x.beginPath(); x.arc(gx, gy, gr, 0, Math.PI * 2);
            x.fillStyle = 'rgba(22,159,132,0.12)'; x.fill();
            x.lineWidth = 7; x.strokeStyle = gcol; x.stroke();
            x.fillStyle = gcol; x.font = '800 62px ' + F; x.textAlign = 'center';
            x.fillText(grade, gx, gy + 16);
            x.fillStyle = '#9DBBE0'; x.font = '600 20px ' + F;
            x.fillText((score.score || 0) + '/100', gx, gy + 48);
            x.textAlign = 'left';
            // stats
            const stats = [[tokens, 'TOKENS SAVED'], [hours, 'HOURS SAVED'], [String(closed), 'CASES CLOSED']];
            stats.forEach((st, i) => {
                const sx = 78 + i * 300;
                x.fillStyle = '#EAF2FD'; x.font = '800 78px ' + F; x.fillText(st[0], sx, 366);
                x.fillStyle = '#9DBBE0'; x.font = '700 20px ' + F; x.fillText(st[1], sx, 402);
            });
            // rule + quote
            x.strokeStyle = 'rgba(255,255,255,0.14)'; x.lineWidth = 2;
            x.beginPath(); x.moveTo(78, 470); x.lineTo(W - 78, 470); x.stroke();
            x.fillStyle = '#C9DDF5'; x.font = '500 26px ' + F;
            const words = quote.split(' ');
            let line = '', ly = 522;
            words.forEach(w => {
                if (x.measureText(line + w + ' ').width > W - 380) { x.fillText(line, 78, ly); line = w + ' '; ly += 38; }
                else { line += w + ' '; }
            });
            x.fillText(line, 78, ly);
            x.fillStyle = '#7FB2F2'; x.font = '700 24px ui-monospace, Menlo, monospace';
            x.textAlign = 'right'; x.fillText('projectmem.dev', W - 78, 560); x.textAlign = 'left';
            const a = document.createElement('a');
            a.download = projectName + '-projectmem-card.png';
            a.href = cv.toDataURL('image/png');
            a.click();
        });
    })();

    // ── 4. Timeline: swimlanes ──
    (function ovTimeline() {
        const lanes = [
            { key:'issue',    c:'#E8593B' },
            { key:'attempt',  c:'#E8A33B' },
            { key:'fix',      c:'#169F84' },
            { key:'decision', c:'#1F6FEB' },
        ];
        const dated = timelineData.map((e,i) => ({ type:e.type, t:new Date(e.timestamp).getTime(), i:i }))
            .filter(e => !isNaN(e.t));
        const wrap = document.getElementById('ov-timeline');
        if (!dated.length) { wrap.innerHTML = '<div class="ov-empty">No dated events yet.</div>'; document.getElementById('ov-foot').textContent=''; return; }
        const tMin = Math.min(...dated.map(e=>e.t)), tMax = Math.max(...dated.map(e=>e.t));
        const span = Math.max(tMax - tMin, 1);
        const xOf = t => 2 + ((t - tMin)/span)*96;
        wrap.innerHTML = lanes.map(L => {
            let pts = dated.filter(e => e.type === L.key).map(e => ({ x:xOf(e.t), i:e.i }));
            if (pts.length > 46) { const step=Math.ceil(pts.length/46); pts = pts.filter((_,i)=>i%step===0); }
            const dots = pts.map(p => {
                const e = timelineData[p.i];
                const cased = e.issue_id && CASE_BY_ID[e.issue_id];
                return '<span class="ov-ev" data-ev="'+p.i+'" title="'+pmEsc((e.summary||'').slice(0,110))+'"'
                    + ' style="left:'+p.x+'%;background:'+L.c+'"></span>';
            }).join('');
            return '<div class="ov-lane"><div class="ln" style="color:'+L.c+'">'+L.key+'</div><div class="ov-track">'+dots+'</div></div>';
        }).join('');
        // axis: 6 evenly spaced dates
        const fmt = ms => new Date(ms).toLocaleDateString('en-US',{month:'short', day:'numeric'});
        const ticks = []; for (let i=0;i<6;i++) ticks.push(fmt(tMin + span*i/5));
        document.getElementById('ov-axis').innerHTML = ticks.map(t => '<span>'+t+'</span>').join('');
        // A dot is the event: open its case if it has one, else the event itself.
        wrap.addEventListener('click', ev => {
            const dot = ev.target.closest('.ov-ev[data-ev]');
            if (!dot) return;
            const e = timelineData[+dot.dataset.ev];
            if (!e) return;
            if (e.issue_id && CASE_BY_ID[e.issue_id]) openCase(e.issue_id);
            else openEvent(e);
        });
        document.getElementById('ov-foot').innerHTML = lanes.map(L =>
            '<span><span class="ov-sw" style="width:11px;height:11px;border-radius:50%;background:'+L.c+'"></span>'+L.key+'</span>').join('')
            + '<span style="margin-left:auto">'+timelineData.length+' events · '+fmt(tMin)+' – '+fmt(tMax)+'</span>';
    })();

    // ══════════════════════════════════════════
    // TAB 1: Story Map — View-state graph
    // ══════════════════════════════════════════
    (function renderStoryMap() {
        const NOISE = /__pycache__|\\.pyc$|\\.DS_Store|\\.egg-info/;
        const DENSE_FILE_EVENT_THRESHOLD = 10;
        const ROOT_DIRECTORY_BUCKET = './';

        const canonicalNodes = data.nodes.filter(n => !NOISE.test(n.id));
        const canonicalNodeIds = new Set(canonicalNodes.map(n => n.id));
        const canonicalLinks = data.links.filter(l => {
            const s = typeof l.source === 'object' ? l.source.id : l.source;
            const t = typeof l.target === 'object' ? l.target.id : l.target;
            return canonicalNodeIds.has(s) && canonicalNodeIds.has(t);
        });

        const state = {
            fileCollapse: false,
            directoryCollapse: false,
            expandedDirectories: new Set(),
            expandedFiles: new Set(),
            focusedFileId: null,
            selectedNodeId: null,
            previousFileCollapse: false,
        };

        const byId = new Map(canonicalNodes.map(n => [n.id, n]));
        const linksByFile = new Map();
        canonicalLinks.forEach(link => {
            const s = sourceId(link);
            const t = targetId(link);
            const target = byId.get(t);
            if (target && target.type === 'file') {
                if (!linksByFile.has(t)) linksByFile.set(t, []);
                linksByFile.get(t).push({ ...link, source: s, target: t });
            }
        });

        const svg = d3.select("#canvas");
        const width = window.innerWidth;
        const height = window.innerHeight - 94;
        const g = svg.append("g");
        let sim = null;

        const defs = svg.append("defs");
        const glow = defs.append("filter").attr("id","glow");
        glow.append("feGaussianBlur").attr("stdDeviation","3").attr("result","blur");
        glow.append("feMerge").selectAll("feMergeNode")
            .data(["blur","SourceGraphic"]).enter()
            .append("feMergeNode").attr("in", d=>d);

        svg.call(d3.zoom().scaleExtent([0.3,5]).on("zoom", e => g.attr("transform", e.transform)));

        function sourceId(link) { return typeof link.source === 'object' ? link.source.id : link.source; }
        function targetId(link) { return typeof link.target === 'object' ? link.target.id : link.target; }
        function isDenseFile(node) { return node.type === 'file' && (node.event_count || 0) >= DENSE_FILE_EVENT_THRESHOLD; }
        function fileLabel(node) { return node.label || (node.id || '').split('/').pop(); }

        function fullGraph() {
            return {
                nodes: canonicalNodes.map(n => ({ ...n })),
                links: canonicalLinks.map(l => ({ ...l, source: sourceId(l), target: targetId(l) })),
            };
        }

        function makeFileBubble(fileNode) {
            return {
                ...fileNode,
                id: 'file-bubble:' + fileNode.id,
                file_id: fileNode.id,
                type: 'file',
                synthetic_type: 'file_bubble',
                label: fileLabel(fileNode),
                display_label: fileLabel(fileNode) + ' · ' + (fileNode.event_count || 0) + ' events',
                event_count: fileNode.event_count || 0,
                failure_count: fileNode.failure_count || fileNode.failures || 0,
                importance: fileNode.importance || 0,
            };
        }

        function directoryPathForParts(parts) {
            if (!parts || !parts.length) return ROOT_DIRECTORY_BUCKET;
            return parts.join('/') + '/';
        }

        function childDirectoryPath(fileNode) {
            const parts = fileNode.directory_parts || [];
            for (let depth = parts.length; depth >= 0; depth--) {
                const parent = directoryPathForParts(parts.slice(0, depth));
                if (state.expandedDirectories.has(parent)) {
                    if (depth >= parts.length) return fileNode.id;
                    return directoryPathForParts(parts.slice(0, depth + 1));
                }
            }
            return fileNode.top_directory || ROOT_DIRECTORY_BUCKET;
        }

        function makeDirectoryBubble(directoryPath, children) {
            const eventCount = children.reduce((sum, child) => sum + (child.event_count || 0), 0);
            const failureCount = children.reduce((sum, child) => sum + (child.failure_count || child.failures || 0), 0);
            return {
                id: 'dir-bubble:' + directoryPath,
                type: 'directory',
                synthetic_type: 'directory_bubble',
                directory_path: directoryPath,
                label: directoryPath,
                display_label: directoryPath + ' · ' + eventCount + ' events',
                event_count: eventCount,
                failure_count: failureCount,
                importance: eventCount + failureCount * 3,
            };
        }

        function deriveVisibleGraph() {
            if (state.directoryCollapse) return deriveDirectoryGraph();
            if (state.fileCollapse) return deriveFileCollapsedGraph();
            return fullGraph();
        }

        function deriveFileCollapsedGraph() {
            const visibleNodes = [];
            const visibleLinks = [];
            const hiddenEventIds = new Set();
            const replacementByFile = new Map();
            const linksByEvent = new Map();

            canonicalNodes.forEach(node => {
                if (node.type === 'file' && isDenseFile(node) && !state.expandedFiles.has(node.id)) {
                    const bubble = makeFileBubble(node);
                    visibleNodes.push(bubble);
                    replacementByFile.set(node.id, bubble.id);
                    return;
                }
                visibleNodes.push({ ...node });
            });

            canonicalLinks.forEach(link => {
                const s = sourceId(link);
                const t = targetId(link);
                const normalized = { ...link, source: s, target: t };
                if (!linksByEvent.has(s)) linksByEvent.set(s, []);
                linksByEvent.get(s).push(normalized);
            });

            linksByEvent.forEach((eventLinks, eventId) => {
                const fileLinks = eventLinks.filter(link => {
                    const target = byId.get(link.target);
                    return target && target.type === 'file';
                });
                if (fileLinks.length && fileLinks.every(link => replacementByFile.has(link.target))) {
                    hiddenEventIds.add(eventId);
                }
            });

            const emittedPairs = new Set();
            canonicalLinks.forEach(link => {
                const s = sourceId(link);
                const t = targetId(link);
                if (hiddenEventIds.has(s)) return;
                const rewrittenTarget = replacementByFile.get(t) || t;
                const pairKey = s + '\u0000' + rewrittenTarget;
                if (emittedPairs.has(pairKey)) return;
                emittedPairs.add(pairKey);
                visibleLinks.push({
                    ...link,
                    source: s,
                    target: rewrittenTarget,
                });
            });

            return {
                nodes: visibleNodes.filter(node => !(node.type === 'event' && hiddenEventIds.has(node.id))),
                links: visibleLinks,
            };
        }

        function deriveDirectoryGraph() {
            const fileNodes = canonicalNodes.filter(n => n.type === 'file');
            const eventNodesById = new Map(canonicalNodes.filter(n => n.type === 'event').map(n => [n.id, n]));
            const groupChildren = new Map();
            const filePassthrough = new Set();

            fileNodes.forEach(fileNode => {
                const childPath = childDirectoryPath(fileNode);
                if (childPath === fileNode.id) {
                    filePassthrough.add(fileNode.id);
                    return;
                }
                if (!groupChildren.has(childPath)) groupChildren.set(childPath, []);
                groupChildren.get(childPath).push(fileNode);
            });

            const directoryNodes = [...groupChildren.entries()].map(([directoryPath, children]) =>
                makeDirectoryBubble(directoryPath, children)
            );
            const replacementByFile = new Map();
            groupChildren.forEach((children, directoryPath) => {
                children.forEach(fileNode => replacementByFile.set(fileNode.id, 'dir-bubble:' + directoryPath));
            });

            const linksByEvent = new Map();
            canonicalLinks.forEach(link => {
                const s = sourceId(link);
                const t = targetId(link);
                const target = byId.get(t);
                if (!target || target.type !== 'file') return;
                if (!linksByEvent.has(s)) linksByEvent.set(s, []);
                linksByEvent.get(s).push({ ...link, source: s, target: t });
            });

            const visibleEventIds = new Set();
            const visibleLinks = [];
            const emittedPairs = new Set();

            linksByEvent.forEach((eventLinks, eventId) => {
                const visibleTargets = [...new Set(eventLinks.map(link => replacementByFile.get(link.target) || link.target))];
                if (visibleTargets.length <= 1 && visibleTargets[0] && visibleTargets[0].startsWith('dir-bubble:')) {
                    return;
                }
                if (visibleTargets.length === 0) return;
                visibleEventIds.add(eventId);
                visibleTargets.forEach(target => {
                    const pairKey = eventId + '\u0000' + target;
                    if (emittedPairs.has(pairKey)) return;
                    emittedPairs.add(pairKey);
                    visibleLinks.push({ source: eventId, target: target });
                });
            });

            const visibleNodes = [
                ...directoryNodes,
                ...fileNodes.filter(n => filePassthrough.has(n.id)).map(n => ({ ...n })),
                ...[...visibleEventIds]
                    .map(id => eventNodesById.get(id))
                    .filter(Boolean)
                    .map(n => ({ ...n })),
            ];

            return { nodes: visibleNodes, links: visibleLinks };
        }

        function restart() {
            const visible = deriveVisibleGraph();
            drawVisibleGraph(visible.nodes, visible.links);
            updateButtons();
        }

        function updateButtons() {
            document.getElementById('story-file-collapse').classList.toggle('active', state.fileCollapse);
            document.getElementById('story-directory-collapse').classList.toggle('active', state.directoryCollapse);
        }

        document.getElementById('story-file-collapse').addEventListener('click', () => {
            if (state.directoryCollapse) return;
            state.fileCollapse = !state.fileCollapse;
            restart();
        });
        document.getElementById('story-directory-collapse').addEventListener('click', () => {
            if (!state.directoryCollapse) {
                state.previousFileCollapse = state.fileCollapse;
                state.directoryCollapse = true;
                state.fileCollapse = false;
            } else {
                state.directoryCollapse = false;
                state.fileCollapse = state.previousFileCollapse;
                state.expandedDirectories.clear();
            }
            restart();
        });
        document.getElementById('story-expand-all').addEventListener('click', () => {
            state.fileCollapse = false;
            state.directoryCollapse = false;
            state.expandedDirectories.clear();
            state.expandedFiles.clear();
            state.focusedFileId = null;
            state.selectedNodeId = null;
            restart();
        });
        document.getElementById('story-reset-focus').addEventListener('click', () => {
            state.focusedFileId = null;
            state.selectedNodeId = null;
            restart();
        });

        function nodeFill(d) {
            if (d.synthetic_type === 'directory_bubble') return '#ecfeff';
            if (d.synthetic_type === 'file_bubble') return '#dbeafe';
            if (d.type === 'file') {
                const heat = Math.min((d.failure_count || d.failures || 0) / 5, 1);
                return d3.interpolate("#334155","#f87171")(heat);
            }
            if (d.event_type === 'fix') return '#10b981';
            if (d.outcome === 'failed') return '#ef4444';
            if (d.event_type === 'decision') return '#818cf8';
            if (d.event_type === 'note') return '#64748b';
            return '#3b82f6';
        }

        function nodeStroke(d) {
            if (d.synthetic_type === 'directory_bubble') return '#0891b2';
            if (d.synthetic_type === 'file_bubble') return '#2563eb';
            return d.type === 'event' ? nodeFill(d) : '#0f172a';
        }

        function nodeRadius(d) {
            if (d.synthetic_type === 'directory_bubble') return 18 + Math.min(Math.sqrt(d.event_count || 1) * 3, 28);
            if (d.synthetic_type === 'file_bubble') return 15 + Math.min(Math.sqrt(d.event_count || 1) * 2.5, 22);
            if (d.type === 'event') {
                if (d.auto_captured) return 4.5;
                if (d.event_type === 'fix' || d.outcome === 'failed') return 8;
                return 6;
            }
            return 7 + Math.min((d.failure_count || d.failures || 0), 5);
        }

        function linkColor(d) {
            const source = byId.get(sourceId(d));
            if (source && source.outcome === 'failed') return '#ef444480';
            if (source && source.event_type === 'fix') return '#10b98160';
            return '#33415580';
        }

        function linkDistance(d) {
            const source = byId.get(sourceId(d));
            const target = byId.get(targetId(d));
            if ((source && source.synthetic_type) || (target && target.synthetic_type)) return 120;
            if (source && source.type === 'event') return 86;
            return 110;
        }

        function shouldShowLabel(d) {
            if (d.synthetic_type) return true;
            if (d.id === state.selectedNodeId || d.id === state.focusedFileId) return true;
            if (state.focusedFileId && isAttachedToFocusedFile(d)) return true;
            if (d.type === 'file' && (d.importance || 0) >= 10) return true;
            if (d.type === 'event' && (d.outcome === 'failed' || d.event_type === 'fix')) return true;
            return false;
        }

        function isAttachedToFocusedFile(d) {
            if (!state.focusedFileId) return false;
            if (d.id === state.focusedFileId || d.file_id === state.focusedFileId) return true;
            const focusedLinks = linksByFile.get(state.focusedFileId) || [];
            return focusedLinks.some(link => sourceId(link) === d.id || targetId(link) === d.id);
        }

        function focusClassForNode(d) {
            if (state.directoryCollapse) return "";
            if (!state.focusedFileId) return "";
            return isAttachedToFocusedFile(d) ? "focused" : "dimmed";
        }

        function focusClassForLink(d) {
            if (state.directoryCollapse) return "";
            if (!state.focusedFileId) return "";
            const s = sourceId(d);
            const t = targetId(d);
            const focusedBubbleId = 'file-bubble:' + state.focusedFileId;
            return s === state.focusedFileId || t === state.focusedFileId || s === focusedBubbleId || t === focusedBubbleId ? "focused" : "dimmed";
        }

        function showStoryTooltip(event, d) {
            const tt = document.getElementById("tooltip");
            tt.style.opacity = 1;
            const typeLabel = d.synthetic_type
                ? (d.synthetic_type === 'directory_bubble' ? 'DIRECTORY' : 'FILE')
                : (d.event_type ? d.event_type.toUpperCase() : (d.type || '').toUpperCase());
            const details = pmEsc(d.summary || d.full_path || d.path || d.id || '');
            const count = d.event_count ? '<br/><span style="color:var(--primary)">'+d.event_count+' attached events</span>' : '';
            const failures = d.failure_count ? '<br/><span style="color:var(--error)">'+d.failure_count+' failed attempts</span>' : '';
            const outcome = d.outcome ? '<br/><span style="color:'+(d.outcome==='failed'?'var(--error)':'var(--success)')+'">Outcome: '+pmEsc(d.outcome)+'</span>' : '';
            const loc = d.location ? '<br/><span style="color:var(--accent)">@ '+pmEsc(d.location)+'</span>' : '';
            tt.innerHTML = '<strong>'+pmEsc(typeLabel)+': '+pmEsc(d.display_label || d.label || d.id)+'</strong><br/>'+details+count+failures+outcome+loc;
            tt.style.left = (event.pageX + 14) + "px";
            tt.style.top = (event.pageY - 14) + "px";
        }

        function handleNodeClick(d) {
            state.selectedNodeId = d.id;
            if (d.synthetic_type === 'directory_bubble') {
                state.expandedDirectories.add(d.directory_path);
                restart();
                return;
            }
            if (d.synthetic_type === 'file_bubble') {
                state.expandedFiles.add(d.file_id);
                state.focusedFileId = d.file_id;
                restart();
                return;
            }
            if (d.type === 'file') {
                state.focusedFileId = d.id;
                restart();
                return;
            }
            restart();
        }

        function drawVisibleGraph(nodes, links) {
            if (sim) sim.stop();
            g.selectAll("*").remove();
            sim = d3.forceSimulation(nodes)
                .force("link", d3.forceLink(links).id(d=>d.id).distance(linkDistance))
                .force("charge", d3.forceManyBody().strength(-250))
                .force("center", d3.forceCenter(width/2, height/2))
                .force("collision", d3.forceCollide(d => nodeRadius(d) + 3));
            drawLayers(nodes, links);
        }

        function drawLayers(nodes, links) {
            const link = g.append("g").selectAll("line")
                .data(links).enter().append("line")
                .attr("class", d => "story-link " + focusClassForLink(d))
                .attr("stroke", linkColor);

            const node = g.append("g").selectAll("circle")
                .data(nodes).enter().append("circle")
                .attr("class", d => "story-node " + focusClassForNode(d))
                .attr("r", nodeRadius)
                .attr("fill", nodeFill)
                .attr("stroke", nodeStroke)
                .attr("stroke-width", d => d.synthetic_type ? 2 : (d.type === 'event' ? 2 : 1))
                .attr("stroke-opacity", d => d.type === 'event' ? 0.3 : 1)
                .attr("stroke-dasharray", d => d.auto_captured ? "3,2" : null)
                .attr("filter", d => (d.type === 'event' && (d.event_type === 'fix' || d.outcome === 'failed')) ? "url(#glow)" : null)
                .call(d3.drag()
                    .on("start", e => { if(!e.active) sim.alphaTarget(0.3).restart(); e.subject.fx=e.subject.x; e.subject.fy=e.subject.y; })
                    .on("drag", e => { e.subject.fx=e.x; e.subject.fy=e.y; })
                    .on("end", e => { if(!e.active) sim.alphaTarget(0); e.subject.fx=null; e.subject.fy=null; }));

            node.on("click", (event, d) => handleNodeClick(d));
            node.on("mouseover", (event,d) => showStoryTooltip(event, d));
            node.on("mouseout", () => { document.getElementById("tooltip").style.opacity=0; });

            const labels = g.append("g").selectAll("text")
                .data(nodes.filter(shouldShowLabel))
                .enter().append("text")
                .attr("class", d => (d.synthetic_type ? "story-bubble-label " : "story-label ") + focusClassForNode(d))
                .attr("dx", d => nodeRadius(d) + 6)
                .attr("dy",".35em")
                .text(d => d.display_label || d.label);

            sim.on("tick", () => {
                link.attr("x1",d=>d.source.x).attr("y1",d=>d.source.y).attr("x2",d=>d.target.x).attr("y2",d=>d.target.y);
                node.attr("cx",d=>d.x).attr("cy",d=>d.y);
                labels.attr("x",d=>d.x).attr("y",d=>d.y);
            });
        }

        restart();
    })();

    // ══════════════════════════════════════════
    // TAB 2: ROI Dashboard — Full overhaul
    // ══════════════════════════════════════════
    const stats = data.stats;
    const evtCount = timelineData.length;
    const sessionsEstimate = Math.max(Math.ceil(evtCount / 4), 1);
    const avgPerSession = evtCount > 0 ? Math.round(stats.total_tokens / sessionsEstimate) : 0;
    const monthlyProjected = stats.total_tokens * 4;

    // Top stat cards
    const topCards = [
        { label:'Total Tokens Saved', value:stats.total_tokens, prefix:'', suffix:'', color:'green' },
        { label:'Estimated USD Saved', value:stats.usd_saved, prefix:'$', suffix:'', color:'purple', decimal:true },
        { label:'Memory Events', value:evtCount, prefix:'', suffix:'', color:'blue' },
        { label:'Monthly Projection', value:monthlyProjected, prefix:'', suffix:'', color:'amber' },
    ];
    const topEl = document.getElementById('roi-top');
    topCards.forEach((c,i) => {
        const card = document.createElement('div');
        card.className = 'roi-stat ' + c.color + ' animate-in';
        card.style.animationDelay = (i*0.08)+'s';
        card.innerHTML = '<div class="roi-stat-label">'+c.label+'</div><div class="roi-stat-value" id="roi-v-'+i+'">0</div><div class="roi-stat-sub">'+(i===3?'Projected at current pace':'Since project start')+'</div>';
        topEl.appendChild(card);
        setTimeout(() => {
            const el = document.getElementById('roi-v-'+i);
            if (c.decimal) animateValue(el, Math.round(c.value*100)/100, c.prefix, '', 1000);
            else animateValue(el, c.value, c.prefix, c.suffix, 1000);
        }, 200 + i*100);
    });
    // Fix USD display
    setTimeout(() => {
        const usdEl = document.getElementById('roi-v-1');
        if (usdEl) usdEl.textContent = '$' + stats.usd_saved.toFixed(2);
    }, 1400);

    // ── Auto-Capture Stats Row ──
    const captureRate = timelineData.length > 0 ? Math.round(autoCount / timelineData.length * 100) : 0;
    const captureCards = [
        { label:'Manual Events', value:manualCount, color:'blue' },
        { label:'Auto-captured', value:autoCount, color:'purple' },
        { label:'Would Be Lost', value:autoCount, color:'amber' },
        { label:'Auto-capture Rate', value:captureRate, color:'green', suffix:'%' },
    ];
    const captureEl = document.getElementById('roi-capture-stats');
    captureCards.forEach((c,i) => {
        const card = document.createElement('div');
        card.className = 'roi-capture-stat ' + c.color + ' animate-in';
        card.style.animationDelay = (i*0.08)+'s';
        card.innerHTML = '<div class="roi-capture-stat-value" id="cap-v-'+i+'">0</div><div class="roi-capture-stat-label">'+c.label+'</div>';
        captureEl.appendChild(card);
        setTimeout(() => {
            animateValue(document.getElementById('cap-v-'+i), c.value, '', c.suffix||'', 800);
        }, 300+i*100);
    });

    // ── Capture Source Donut ──
    const sourceMap = {};
    timelineData.forEach(e => {
        const src = e.auto_captured ? (e.capture_source || 'auto_unknown') : 'manual';
        sourceMap[src] = (sourceMap[src]||0) + 1;
    });
    const srcData = Object.entries(sourceMap).filter(([,v])=>v>0);
    const srcColors = { manual:'#3b82f6', git_post_commit:'#818cf8', git_post_revert:'#ef4444', git_post_merge:'#10b981', churn_detector:'#f59e0b', ci_parser:'#ec4899', auto_unknown:'#64748b' };
    const srcTotal = srcData.reduce((s,[,v])=>s+v, 0);
    if (srcData.length > 0) {
        const srcPie = d3.pie().value(d=>d[1]).sort(null).padAngle(0.03);
        const srcArc = d3.arc().innerRadius(50).outerRadius(72);
        const srcSvg = d3.select("#roi-source-donut")
            .attr("viewBox","0 0 160 160")
            .append("g").attr("transform","translate(80,80)");
        srcSvg.selectAll("path")
            .data(srcPie(srcData)).enter().append("path")
            .attr("d", srcArc)
            .attr("fill",(d)=>srcColors[d.data[0]]||'#64748b')
            .attr("stroke","var(--surface)").attr("stroke-width",2)
            .style("opacity",0)
            .transition().duration(600).delay((d,i)=>i*100)
            .style("opacity",1)
            .attrTween("d", function(d) {
                const interp = d3.interpolate({startAngle:d.startAngle,endAngle:d.startAngle}, d);
                return t => srcArc(interp(t));
            });
        srcSvg.append("text").attr("text-anchor","middle").attr("dy","-0.2em")
            .attr("fill","var(--text)").attr("font-size","18px").attr("font-weight","800")
            .text(srcTotal);
        srcSvg.append("text").attr("text-anchor","middle").attr("dy","1.2em")
            .attr("fill","var(--text-dim)").attr("font-size","10px").text("events");
        const srcLegend = document.getElementById('roi-source-legend');
        const srcLabels = { manual:'Manual', git_post_commit:'Git Commits', git_post_revert:'Git Reverts', git_post_merge:'Git Merges', churn_detector:'Churn Alerts', ci_parser:'CI Results', auto_unknown:'Auto (other)' };
        srcData.forEach(([key,val]) => {
            const pct = ((val/srcTotal)*100).toFixed(0);
            srcLegend.innerHTML += '<div class="roi-donut-item"><div class="roi-donut-dot" style="background:'+(srcColors[key]||'#64748b')+'"></div><div class="roi-donut-name">'+pmEsc(srcLabels[key]||key)+'</div><div class="roi-donut-val">'+pct+'%</div></div>';
        });
    }

    // ── File Churn Heatmap ──
    const fileChanges = {};
    timelineData.forEach(e => {
        if (e.location) {
            const f = e.location.split(':')[0];
            fileChanges[f] = (fileChanges[f]||0) + 1;
        }
    });
    // Also count from graph data file nodes
    data.nodes.forEach(n => {
        if (n.type === 'file' && n.failures > 0) {
            fileChanges[n.id] = (fileChanges[n.id]||0) + n.failures;
        }
    });
    const churnEl = document.getElementById('roi-churn');
    const churnEntries = Object.entries(fileChanges).sort((a,b)=>b[1]-a[1]).slice(0,10);
    if (churnEntries.length === 0) {
        churnEl.innerHTML = '<div class="churn-empty">No file activity tracked yet</div>';
    } else {
        const maxChurn = Math.max(...churnEntries.map(d=>d[1]), 1);
        churnEntries.forEach(([file,count],i) => {
            const severity = count >= 8 ? 'high' : count >= 4 ? 'medium' : 'low';
            const pct = (count/maxChurn*100).toFixed(0);
            const shortFile = file.split('/').slice(-2).join('/');
            const row = document.createElement('div');
            row.className = 'churn-row animate-in';
            row.style.animationDelay = (0.2+i*0.05)+'s';
            row.innerHTML = '<div class="churn-file" title="'+pmEsc(file)+'">'+pmEsc(shortFile)+'</div><div class="churn-bar-track"><div class="churn-bar-fill '+severity+'" style="width:0%"></div></div><div class="churn-count">'+count+'</div><div class="churn-severity '+severity+'">'+severity+'</div>';
            churnEl.appendChild(row);
            setTimeout(() => { row.querySelector('.churn-bar-fill').style.width = pct+'%'; }, 400+i*60);
        });
    }

    // Bar chart
    const barColors = {
        issue:'#3b82f6', attempt_failed:'#ef4444', attempt_worked:'#10b981',
        fix:'#10b981', decision:'#818cf8', note:'#64748b', backfill:'#475569'
    };
    const barsEl = document.getElementById('roi-bars');
    const maxTokens = Math.max(...Object.values(stats.breakdown), 1);
    Object.entries(stats.breakdown).forEach(([key,val],i) => {
        if (val===0) return;
        const pct = (val/maxTokens*100).toFixed(1);
        const row = document.createElement('div');
        row.className = 'roi-bar-row animate-in';
        row.style.animationDelay = (0.3+i*0.06)+'s';
        row.innerHTML = '<div class="roi-bar-label">'+key.replace(/_/g,' ')+'</div><div class="roi-bar-track"><div class="roi-bar-fill" style="width:0%;background:'+(barColors[key]||'#3b82f6')+'"></div></div><div class="roi-bar-val">'+fmtNum(val)+'</div>';
        barsEl.appendChild(row);
        setTimeout(() => { row.querySelector('.roi-bar-fill').style.width = pct+'%'; }, 400+i*80);
    });

    // Donut chart
    const donutData = Object.entries(stats.breakdown).filter(([,v])=>v>0);
    const donutColors = donutData.map(([k])=>barColors[k]||'#3b82f6');
    const total = donutData.reduce((s,[,v])=>s+v, 0);
    const pie = d3.pie().value(d=>d[1]).sort(null).padAngle(0.03);
    const arc = d3.arc().innerRadius(50).outerRadius(72);
    const donutSvg = d3.select("#roi-donut")
        .attr("viewBox","0 0 160 160")
        .append("g").attr("transform","translate(80,80)");
    donutSvg.selectAll("path")
        .data(pie(donutData)).enter().append("path")
        .attr("d", arc)
        .attr("fill", (d,i)=>donutColors[i])
        .attr("stroke","var(--surface)").attr("stroke-width",2)
        .style("opacity",0)
        .transition().duration(600).delay((d,i)=>i*100)
        .style("opacity",1)
        .attrTween("d", function(d) {
            const interp = d3.interpolate({startAngle:d.startAngle,endAngle:d.startAngle}, d);
            return t => arc(interp(t));
        });
    // Center label
    donutSvg.append("text").attr("text-anchor","middle").attr("dy","-0.2em")
        .attr("fill","var(--text)").attr("font-size","18px").attr("font-weight","800")
        .text(fmtNum(total));
    donutSvg.append("text").attr("text-anchor","middle").attr("dy","1.2em")
        .attr("fill","var(--text-dim)").attr("font-size","10px")
        .text("tokens");

    // Donut legend
    const legendEl = document.getElementById('roi-donut-legend');
    donutData.forEach(([key,val],i) => {
        const pct = ((val/total)*100).toFixed(0);
        legendEl.innerHTML += '<div class="roi-donut-item"><div class="roi-donut-dot" style="background:'+donutColors[i]+'"></div><div class="roi-donut-name">'+key.replace(/_/g,' ')+'</div><div class="roi-donut-val">'+pct+'%</div></div>';
    });

    // Area chart — cumulative savings over time
    const areaSvg = d3.select("#roi-area");
    const aRect = document.getElementById('roi-area').getBoundingClientRect();
    const aW = aRect.width || 900, aH = 140;
    areaSvg.attr("viewBox", "0 0 "+aW+" "+aH);

    const sortedEvents = [...timelineData].sort((a,b) => new Date(a.timestamp)-new Date(b.timestamp));
    const tokenMap = { issue:2000, attempt:2000, fix:4000, decision:3000, note:1000, backfill:500 };
    let cumulative = 0;
    const areaData = sortedEvents.map((e,i) => {
        cumulative += (tokenMap[e.type]||1000);
        return { x:i, y:cumulative, type:e.type };
    });
    if (areaData.length > 0) {
        const xScale = d3.scaleLinear().domain([0,areaData.length-1]).range([40,aW-16]);
        const yScale = d3.scaleLinear().domain([0,d3.max(areaData,d=>d.y)*1.1]).range([aH-24,8]);

        // Grid lines
        const yTicks = yScale.ticks(4);
        yTicks.forEach(t => {
            areaSvg.append("line").attr("x1",40).attr("x2",aW-16).attr("y1",yScale(t)).attr("y2",yScale(t))
                .attr("stroke","var(--border)").attr("stroke-dasharray","2,4");
            areaSvg.append("text").attr("x",4).attr("y",yScale(t)+4)
                .attr("fill","var(--text-muted)").attr("font-size","9px").text((t/1000).toFixed(0)+'K');
        });

        // Area
        const area = d3.area().x(d=>xScale(d.x)).y0(aH-24).y1(d=>yScale(d.y)).curve(d3.curveMonotoneX);
        const grad = areaSvg.append("defs").append("linearGradient").attr("id","aGrad").attr("x1",0).attr("y1",0).attr("x2",0).attr("y2",1);
        grad.append("stop").attr("offset","0%").attr("stop-color","var(--primary)").attr("stop-opacity",0.3);
        grad.append("stop").attr("offset","100%").attr("stop-color","var(--primary)").attr("stop-opacity",0.02);
        areaSvg.append("path").datum(areaData).attr("fill","url(#aGrad)").attr("d",area);

        // Line
        const line = d3.line().x(d=>xScale(d.x)).y(d=>yScale(d.y)).curve(d3.curveMonotoneX);
        const path = areaSvg.append("path").datum(areaData)
            .attr("fill","none").attr("stroke","var(--primary)").attr("stroke-width",2).attr("d",line);
        const pathLen = path.node().getTotalLength();
        path.attr("stroke-dasharray",pathLen).attr("stroke-dashoffset",pathLen)
            .transition().duration(1200).ease(d3.easeCubicOut).attr("stroke-dashoffset",0);

        // Dots for key events
        areaSvg.selectAll(".area-dot")
            .data(areaData.filter(d=>d.type==='fix'||d.type==='issue'))
            .enter().append("circle")
            .attr("cx",d=>xScale(d.x)).attr("cy",d=>yScale(d.y))
            .attr("r",3)
            .attr("fill",d=>d.type==='fix'?'var(--success)':'var(--primary)')
            .style("opacity",0)
            .transition().delay(1200).duration(300).style("opacity",1);
    }

    // ══════════════════════════════════════════
    // TAB 3: Project Map
    // ══════════════════════════════════════════
    function renderMarkdown(md) {
        // PROJECT_MAP.md is written by AI agents from repo content — escape the
        // whole document up front so only the tags this function emits can ever
        // reach innerHTML.
        md = pmEsc(md);
        // Pull fenced ``` blocks out FIRST so their backticks/indentation don't collide
        // with the inline-code and paragraph rules below (they'd render as empty <code>
        // boxes + a stray trailing ``). Restored as <pre> at the end.
        const fences = [];
        md = md.replace(/```[\\w-]*\\n?([\\s\\S]*?)```/g, function (_, code) {
            // md was escaped on entry, so `code` is already HTML-safe here.
            fences.push('<pre class="md-pre">' + code.replace(/\\n$/, '') + '</pre>');
            return '@@FENCE' + (fences.length - 1) + '@@';
        });
        let html = md
            .replace(/^### (.+)$/gm, '<h3>$1</h3>')
            .replace(/^## (.+)$/gm, '<h2>$1</h2>')
            .replace(/^# (.+)$/gm, '<h1>$1</h1>')
            .replace(/`([^`]+)`/g, '<code>$1</code>')
            .replace(/\\*\\*([^*]+)\\*\\*/g, '<strong>$1</strong>')
            .replace(/^- (.+)$/gm, '<li>$1</li>')
            .replace(/(<li>.*<\\/li>)/s, '<ul>$1</ul>')
            .replace(/^(?!<[hul]|@@FENCE)(\\S.*)$/gm, '<p>$1</p>')
            .replace(/\\n\\n/g, '')
            .replace(/((?:<li>[^]*?<\\/li>\\s*)+)/g, '<ul>$1</ul>');
        return html.replace(/@@FENCE(\\d+)@@/g, function (_, i) { return fences[+i]; });
    }
    document.getElementById('map-content').innerHTML = renderMarkdown(projectMap);

    if (projectMapGraph.nodes.length > 0) {
        const mSvg = d3.select("#map-canvas");
        const mW = window.innerWidth * 0.6;
        const mH = window.innerHeight - 56;
        const mG = mSvg.append("g");

        // Pin the zoom extent to mW/mH. The Project Map tab is hidden at load,
        // so #map-canvas measures 0x0 and d3's default extent is degenerate —
        // interpolateZoom over a zero-width view yields translate(NaN,NaN).
        const mZoom = d3.zoom().extent([[0,0],[mW,mH]]).scaleExtent([0.15,4])
            .on("zoom", e => mG.attr("transform",e.transform));
        mSvg.call(mZoom);

        // Arrow marker
        const mDefs = mSvg.append("defs");
        mDefs.append("marker").attr("id","arrow").attr("viewBox","0 -5 10 10")
            .attr("refX",18).attr("refY",0).attr("markerWidth",6).attr("markerHeight",6).attr("orient","auto")
            .append("path").attr("fill","#475569").attr("d","M0,-5L10,0L0,5");
        // Glow
        const mGlow = mDefs.append("filter").attr("id","mglow");
        mGlow.append("feGaussianBlur").attr("stdDeviation","2.5").attr("result","blur");
        mGlow.append("feMerge").selectAll("feMergeNode").data(["blur","SourceGraphic"]).enter().append("feMergeNode").attr("in",d=>d);

        const mSim = d3.forceSimulation(projectMapGraph.nodes)
            .force("link", d3.forceLink(projectMapGraph.links).id(d=>d.id).distance(130))
            .force("charge", d3.forceManyBody().strength(-500))
            .force("center", d3.forceCenter(mW/2, mH/2));

        const mLink = mG.append("g").selectAll("line")
            .data(projectMapGraph.links).enter().append("line")
            .attr("class","arch-link").attr("marker-end","url(#arrow)");

        // Combo: colour/size structure nodes by failure heat; hot files (3+
        // failed attempts) get a red dashed ring — judgment painted on structure.
        const mHeatFill = d => d.type==='folder' ? 'var(--accent)'
            : ((d.failure_count||0)>=3 ? '#E8593B' : ((d.failure_count||0)>=1 ? '#E8A33B' : 'var(--primary)'));
        const mHeatR = d => d.type==='folder' ? 12 : 7 + Math.min(d.failure_count||0, 5);
        const mHot = mG.append("g").selectAll("circle")
            .data(projectMapGraph.nodes.filter(d => (d.failure_count||0) >= 3)).enter().append("circle")
            .attr("fill","none").attr("stroke","#E8593B").attr("stroke-width",2.5)
            .attr("stroke-dasharray","3 3").attr("r", d => mHeatR(d)+5);
        const mNode = mG.append("g").selectAll("circle")
            .data(projectMapGraph.nodes).enter().append("circle")
            .attr("class","arch-node")
            .attr("r", mHeatR)
            .attr("fill", mHeatFill)
            .attr("stroke", d => d.type==='folder' ? 'var(--accent)' : ((d.failure_count||0)>0 ? mHeatFill(d) : 'var(--primary)'))
            .attr("stroke-width", d=>d.type==='folder'?3:1.5)
            .attr("stroke-opacity",0.25)
            .attr("filter","url(#mglow)")
            .call(d3.drag()
                .on("start", e => { if(!e.active) mSim.alphaTarget(0.3).restart(); e.subject.fx=e.subject.x; e.subject.fy=e.subject.y; })
                .on("drag", e => { e.subject.fx=e.x; e.subject.fy=e.y; })
                .on("end", e => { if(!e.active) mSim.alphaTarget(0); e.subject.fx=null; e.subject.fy=null; }));

        const mLabels = mG.append("g").selectAll("text")
            .data(projectMapGraph.nodes).enter().append("text")
            .attr("font-size", d=>d.type==='folder'?'12px':'11px')
            .attr("font-weight", d=>d.type==='folder'?'600':'400')
            .attr("fill", d=>d.type==='folder'?'#4F46E5':'#33455E')
            .attr("dx",16).attr("dy",".35em")
            .text(d=>d.label);

        mNode.on("mouseover", (event,d) => {
            const tt = document.getElementById("tooltip");
            tt.style.opacity=1;
            tt.innerHTML = '<strong>'+pmEsc(d.type).toUpperCase()+'</strong><br/>'+pmEsc(d.full_path)
                + ((d.failure_count||0) ? '<br/><span style="color:#E8593B">'+d.failure_count+' failed attempts</span>' : '');
            tt.style.left=(event.pageX+14)+"px";
            tt.style.top=(event.pageY-14)+"px";
        }).on("mouseout", () => { document.getElementById("tooltip").style.opacity=0; });

        mSim.on("tick", () => {
            mLink.attr("x1",d=>d.source.x).attr("y1",d=>d.source.y).attr("x2",d=>d.target.x).attr("y2",d=>d.target.y);
            mHot.attr("cx",d=>d.x).attr("cy",d=>d.y);
            mNode.attr("cx",d=>d.x).attr("cy",d=>d.y);
            mLabels.attr("x",d=>d.x).attr("y",d=>d.y);
        });

        // Fit the whole structure graph into view once the layout settles, so
        // nodes (incl. the hot files) never spread off-screen.
        function mFit() {
            // Only nodes the simulation has already positioned: a single
            // undefined x turns Math.min into NaN, and the zoom transition then
            // writes translate(NaN,NaN) scale(NaN) on every frame.
            const xs = projectMapGraph.nodes.map(n=>n.x).filter(Number.isFinite);
            const ys = projectMapGraph.nodes.map(n=>n.y).filter(Number.isFinite);
            if (!xs.length || !ys.length) return;
            const minx=Math.min(...xs), maxx=Math.max(...xs), miny=Math.min(...ys), maxy=Math.max(...ys);
            const w=(maxx-minx)||1, h=(maxy-miny)||1, pad=64;
            const k=Math.min((mW-2*pad)/w, (mH-2*pad)/h, 1.5);
            const tx=(mW-k*(minx+maxx))/2, ty=(mH-k*(miny+maxy))/2;
            if (!Number.isFinite(k) || !Number.isFinite(tx) || !Number.isFinite(ty)) return;
            mSvg.transition().duration(450).call(mZoom.transform, d3.zoomIdentity.translate(tx,ty).scale(k));
        }
        mSim.on("end", mFit);
        setTimeout(mFit, 1800);
    }

    // ══════════════════════════════════════════
    // TAB 3b: Project Map — Tree / Dendrogram view
    // ══════════════════════════════════════════
    let treeSelectedDirs = null;
    const treeDirOf = id => {
        const p = String(id).split('/').filter(Boolean);
        return p.length > 1 ? p.slice(0, -1).join('/') + '/' : '(root)';
    };

    function buildTreeData() {
        if (!projectMapGraph.nodes.length) return null;
        // Full nested hierarchy — every path segment becomes a level, so a
        // directory like api/ keeps its subtree instead of collapsing to a leaf.
        const palette = ['#3b82f6','#8b5cf6','#10b981','#f59e0b','#ec4899','#06b6d4','#84cc16','#ef4444','#a78bfa','#22d3ee','#fb7185','#4ade80'];
        const root = { id:'__root__', label:projectName || 'project', children:[], _kids:{} };
        function child(parent, seg, path, isDir) {
            if (!parent._kids[seg]) {
                const node = { id:path, full:path, label:seg, dir:isDir, children:[], _kids:{} };
                parent._kids[seg] = node;
                parent.children.push(node);
            }
            const n = parent._kids[seg];
            if (isDir) n.dir = true;
            return n;
        }
        projectMapGraph.nodes.forEach(n => {
            const raw = String(n.id);
            const parts = raw.split('/').filter(Boolean);
            if (!parts.length) return;
            if (treeSelectedDirs && !treeSelectedDirs.has(treeDirOf(raw))) return;
            let cur = root, path = '';
            parts.forEach((seg, i) => {
                path = path ? path + '/' + seg : seg;
                const last = i === parts.length - 1;
                cur = child(cur, seg, path, !last || n.type === 'folder' || raw.slice(-1) === '/');
            });
        });
        const MAX_KIDS = 16;
        (function walk(node, color, depth) {
            delete node._kids;
            if (!node.children.length) { delete node.children; node.color = color; return; }
            node.color = color;
            node.children.sort((a,b) =>
                (b.children.length > 0) - (a.children.length > 0) || a.label.localeCompare(b.label));
            node.children.forEach((c, i) => {
                walk(c, depth === 0 ? palette[i % palette.length] : color, depth + 1);
            });
            if (node.children.length > MAX_KIDS) {
                const more = node.children.length - (MAX_KIDS - 1);
                node.children = node.children.slice(0, MAX_KIDS - 1);
                node.children.push({ id:'more_' + node.id, label:'+' + more + ' more', color:color, more:true });
            }
        })(root, '#3b82f6', 0);
        // Label directories with a trailing slash once the walk is done.
        (function label(n) {
            if (n.dir && n.label.slice(-1) !== '/') n.label += '/';
            (n.children || []).forEach(label);
        })(root);
        return root;
    }

    function renderTree() {
        // directory checklist — same control the Flow view uses
        const fHost = d3.select('#tree-filter-host');
        fHost.selectAll('*').remove();
        const fileIds = projectMapGraph.nodes.map(n => String(n.id)).filter(Boolean);
        const dirCounts = {};
        fileIds.forEach(id => { const d = treeDirOf(id); dirCounts[d] = (dirCounts[d] || 0) + 1; });
        const dirNames = Object.keys(dirCounts).sort();
        if (treeSelectedDirs === null) treeSelectedDirs = new Set(dirNames);
        [...treeSelectedDirs].forEach(d => { if (!dirCounts[d]) treeSelectedDirs.delete(d); });
        if (dirNames.length > 1) {
            renderDirFilter(fHost, dirNames, dirCounts, treeSelectedDirs,
                next => { treeSelectedDirs = next; renderTree(); });
        }

        const treeData = buildTreeData();
        const tSvg = d3.select("#map-tree");
        tSvg.selectAll("*").remove();
        if (!treeData || !(treeData.children || []).length) {
            tSvg.append("text").attr("x","50%").attr("y","50%").attr("text-anchor","middle")
                .attr("fill","var(--text-muted)").attr("font-size","13px")
                .text(projectMapGraph.nodes.length
                    ? "All directories hidden — enable one in the filter (top-left)."
                    : "No project structure yet — run pjm map.");
            return;
        }
        const pane = document.querySelector('.map-graph-pane');
        const W = pane.clientWidth || 800;
        const H = pane.clientHeight || 600;
        const root = d3.hierarchy(treeData);
        // d3.tree + nodeSize (not cluster + size): the hierarchy is now as deep
        // as the repo, so each level needs its own column and each row a fixed
        // slot — otherwise labels from different depths land on top of each other.
        const ROW = 19, COL = 172;
        d3.tree().nodeSize([ROW, COL]).separation((a, b) => a.parent === b.parent ? 1 : 1.35)(root);
        let xMin = Infinity, xMax = -Infinity;
        root.each(d => { if (d.x < xMin) xMin = d.x; if (d.x > xMax) xMax = d.x; });
        const innerH = Math.max(H - 40, (xMax - xMin) + 60);
        const innerW = Math.max(W, (root.height + 1) * COL + 220);

        // Keep the viewBox at pane size and open at a readable scale — scaling a
        // 1,400px-tall tree down to fit makes every label unreadable. Pan/zoom
        // covers the rest.
        tSvg.attr("viewBox", `0 0 ${W} ${H}`).style("cursor","grab");
        const zoomG = tSvg.append("g");
        const rootLabel = projectName || 'project';
        const leftPad = Math.max(150, rootLabel.length * 7.5 + 34);
        const g = zoomG.append("g").attr("transform", `translate(${leftPad},${30 - xMin})`);

        // Zoom + pan
        const zoom = d3.zoom()
            .extent([[0, 0], [W, H]])
            .scaleExtent([0.25, 5])
            .on("zoom", (e) => zoomG.attr("transform", e.transform))
            .on("start", () => tSvg.style("cursor","grabbing"))
            .on("end", () => tSvg.style("cursor","grab"));
        const fitK = Math.min(1, Math.max(0.55, Math.min(W / innerW, H / (innerH + 40))));
        // Start clear of the directory panel, or the project root label hides under it.
        const panelW = document.querySelector('#tree-filter-host .flow-filter');
        const clearX = panelW ? panelW.getBoundingClientRect().width + 26 : 0;
        const fitT = d3.zoomIdentity
            .translate(Math.max(clearX, (W - innerW * fitK) / 2), 0).scale(fitK);
        tSvg.call(zoom).call(zoom.transform, fitT)
            .on("dblclick.zoom", () => tSvg.transition().duration(400).call(zoom.transform, fitT));

        // Links — bezier curves
        const linkGen = d3.linkHorizontal().x(d=>d.y).y(d=>d.x);
        g.append("g").selectAll("path")
            .data(root.links()).enter().append("path")
            .attr("class","tree-link")
            .attr("d", linkGen);

        // Nodes
        const node = g.append("g").selectAll("g")
            .data(root.descendants()).enter().append("g")
            .attr("transform", d => `translate(${d.y},${d.x})`);

        // A file the project has memory about is drawn from the event palette —
        // bigger, and in the same issue-red family as the Story Map treemap.
        const storyOf = d => (d.data.full && FILE_BY_PATH[d.data.full]) || null;
        node.append("circle")
            .attr("class","tree-node-circle")
            .attr("r", d => {
                const st = storyOf(d);
                if (st) return 4.5 + Math.min(st.n, 5) * 0.9;
                return d.depth === 0 ? 5 : d.depth === 1 ? 6 : 3.5;
            })
            .attr("fill", d => {
                const st = storyOf(d);
                // blue = the project remembers this file; warm = it fought with it
                if (st) return st.friction >= 3 ? '#E8593B' : st.friction === 2 ? '#EC6B47'
                                : st.friction === 1 ? '#F1956F' : '#1F6FEB';
                if (d.depth === 0) return 'var(--navy)';            // the project itself
                if (d.children) return d.data.color || '#64748b';   // directories keep branch colour
                return '#C7D6E8';                                    // file with no memory: muted
            })
            .attr("fill-opacity", d => storyOf(d) || d.children ? 1 : 0.85)
            .attr("stroke", d => {
                const st = storyOf(d);
                return st && st.friction >= 3 ? '#E8593B' : 'var(--surface)';
            })
            .attr("stroke-width", d => {
                const st = storyOf(d);
                return st && st.friction >= 3 ? 2.4 : 2;
            });

        node.append("text")
            .attr("class","tree-node-label")
            .attr("dy","0.32em")
            .attr("x", d => d.children ? -10 : 10)
            .attr("text-anchor", d => d.children ? "end" : "start")
            .style("font-style", d => d.data.more ? "italic" : "normal")
            .text(d => {
                // The root is the project, not an unnamed dot.
                if (d.depth === 0) return rootLabel;
                const story = d.data.full && FILE_BY_PATH[d.data.full];
                return d.data.label + (story ? '  · ' + story.n : '');
            })
            .style("font-weight", d => d.depth === 0 ? "700" : null)
            .style("font-size", d => d.depth === 0 ? "13px" : null)
            .attr("fill", d => d.depth === 0 ? "var(--navy)"
                             : d.data.more ? "var(--text-muted)" : "var(--text)");

        // A file with memory opens the same dossier as the Story Map.
        node.filter(d => d.data.full && FILE_BY_PATH[d.data.full])
            .style("cursor", "pointer")
            .on("click", (e, d) => openFile(d.data.full));

        fHost.append('div').attr('class', 'tree-legend').html(
            '<span><i style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#C7D6E8;margin-right:5px;vertical-align:middle"></i>no memory</span>' +
            '<span><i style="display:inline-block;width:11px;height:11px;border-radius:50%;background:#1F6FEB;margin-right:5px;vertical-align:middle"></i>has memory</span>' +
            '<span><i style="display:inline-block;width:12px;height:12px;border-radius:50%;background:#F1956F;margin-right:5px;vertical-align:middle"></i>1 issue</span>' +
            '<span><i style="display:inline-block;width:14px;height:14px;border-radius:50%;background:#E8593B;margin-right:5px;vertical-align:middle"></i>3+</span>' +
            '<span style="color:var(--text-muted)">size = events · click a file for its dossier</span>');

        // Tooltip on full paths
        node.on("mouseover", (e,d) => {
            if (!d.data.full) return;
            const tt = document.getElementById("tooltip");
            tt.style.opacity = 1;
            tt.innerHTML = '<strong>'+pmEsc(d.data.label)+'</strong><br/>'+pmEsc(d.data.full);
            tt.style.left = (e.pageX + 14) + "px";
            tt.style.top = (e.pageY - 14) + "px";
        }).on("mouseout", () => { document.getElementById("tooltip").style.opacity = 0; });
    }

    // TAB 3c: Project Map — Flow view (layered left-to-right flowchart)
    // Same real data as the Story Map: structure + what happened, flowing into memory.
    // Directory filter selection persists across re-renders (null = show all).
    let flowSelectedDirs = null;

    // Shared by Flow and Tree: a directory checklist that narrows the view.
    function renderDirFilter(host, dirNames, counts, sel, apply) {
        const total = dirNames.reduce((a, d) => a + counts[d], 0);
        const panel = host.append('div').attr('class', 'flow-filter')
            .style('position', 'absolute').style('left', '12px').style('top', '54px').style('z-index', '6')
            .style('background', 'var(--surface)').style('border', '1px solid var(--border-light)')
            .style('border-radius', '10px').style('padding', '8px 10px').style('max-height', 'calc(100% - 70px)')
            .style('overflow-y', 'auto').style('box-shadow', '0 4px 16px rgba(20,35,58,0.10)')
            .style('font-size', '11px').style('min-width', '148px');
        panel.append('div').text('DIRECTORIES')
            .style('font-size', '9.5px').style('font-weight', '700').style('letter-spacing', '0.8px')
            .style('color', 'var(--text-muted)').style('margin-bottom', '5px');
        const mkRow = (label, count, on, onToggle, onSolo, bold) => {
            const row = panel.append('div').attr('class', 'ff-row')
                .style('display', 'flex').style('align-items', 'center').style('gap', '7px')
                .style('padding', '3px 4px').style('cursor', 'pointer').style('border-radius', '5px');
            row.append('span')
                .style('width', '13px').style('height', '13px').style('border-radius', '3px').style('flex', '0 0 auto')
                .style('border', '1.5px solid ' + (on ? 'var(--accent, #1F6FEB)' : 'var(--border-light)'))
                .style('background', on ? 'var(--accent, #1F6FEB)' : 'transparent')
                .style('color', '#fff').style('font-size', '10px').style('text-align', 'center').style('line-height', '13px')
                .text(on ? '✓' : '');
            const lab = row.append('span').style('flex', '1').style('color', 'var(--text)')
                .style('font-weight', bold ? '700' : '500').text(label);
            if (count != null) row.append('span').style('color', 'var(--text-dim)').text(count);
            row.on('click', onToggle);
            if (onSolo) lab.style('text-decoration-line', 'underline').style('text-decoration-style', 'dotted')
                .style('text-underline-offset', '2px').on('click', ev => { ev.stopPropagation(); onSolo(); });
            return row;
        };
        const allOn = sel.size === dirNames.length;
        mkRow('All', total, allOn, () => apply(allOn ? new Set() : new Set(dirNames)), null, true);
        dirNames.forEach(d => {
            const on = sel.has(d);
            mkRow(d.length > 18 ? '…' + d.slice(-17) : d, counts[d], on,
                () => { const next = new Set(sel); on ? next.delete(d) : next.add(d); apply(next); },
                () => apply(new Set([d])));
        });
        panel.append('div').text('box = toggle · name = only')
            .style('font-size', '9px').style('color', 'var(--text-dim)').style('margin-top', '5px');
    }

    function renderMapFlow() {
        const host = d3.select('#map-flow');
        host.selectAll('*').remove();
        // Skip dependency / build noise so the flow shows YOUR code, not site-packages.
        const FLOW_IGNORE = /(^|\\/)(__pycache__|node_modules|\\.venv|venv|site-packages|dist-info|\\.egg-info|\\.git|build|dist)(\\/|$)|\\.(pyc|pyo)$|\\.cpython-/;
        const allFileNodes = data.nodes.filter(n => n.type === 'file');
        const srcFiles = allFileNodes.filter(n => !FLOW_IGNORE.test(n.path || n.id));
        const hiddenCount = allFileNodes.length - srcFiles.length;
        const dirOf = f => { const p = (f.path || f.id).split('/'); return p.length > 1 ? p.slice(0, -1).join('/') + '/' : '(root)'; };
        const allDirCounts = {};
        srcFiles.forEach(f => { const d = dirOf(f); allDirCounts[d] = (allDirCounts[d] || 0) + 1; });
        const allDirNames = Object.keys(allDirCounts).sort();
        // init / reconcile the persistent directory selection
        if (flowSelectedDirs === null) flowSelectedDirs = new Set(allDirNames);
        [...flowSelectedDirs].forEach(d => { if (!allDirCounts[d]) flowSelectedDirs.delete(d); });
        // filter panel (only when there's more than one directory to choose between)
        if (allDirNames.length > 1) {
            renderDirFilter(host, allDirNames, allDirCounts, flowSelectedDirs,
                next => { flowSelectedDirs = next; renderMapFlow(); });
        }
        // apply the directory filter, then rank by activity and cap the long tail
        let visFiles = srcFiles.filter(f => flowSelectedDirs.has(dirOf(f)));
        visFiles.sort((a, b) => ((b.failure_count||0)*100 + (b.event_count||0)) - ((a.failure_count||0)*100 + (a.event_count||0)));
        const FLOW_CAP = 40;
        const cappedCount = Math.max(0, visFiles.length - FLOW_CAP);
        let fileNodes = cappedCount > 0 ? visFiles.slice(0, FLOW_CAP) : visFiles;
        if (!fileNodes.length) {
            host.append('div').attr('class', 'flow-empty')
                .text(srcFiles.length ? 'All directories hidden — enable one in the filter (top-left).'
                                      : 'No file activity yet — log an issue or attempt against a file to see the flow.');
            return;
        }
        // Per-file activity, read from the same file stories the Story Map uses,
        // so the chips here and the dossier there can never disagree — and every
        // event type shows, not just failures and fixes.
        const CHIP_SPEC = [
            ['issue',    e => e.type === 'issue',                                 '#E8593B', 'issues'],
            ['failed',   e => e.type === 'attempt' && e.outcome === 'failed',     '#E8593B', 'failed'],
            ['tried',    e => e.type === 'attempt' && e.outcome !== 'failed',     '#E8A33B', 'attempts'],
            ['fixed',    e => e.type === 'fix',                                   '#169F84', 'fixed'],
            ['decision', e => e.type === 'decision',                              '#1F6FEB', 'decisions'],
            ['gotcha',   e => e.type === 'note',                                  '#6366F1', 'notes']
        ];
        const PLURAL = { issue:'issues', failed:'failed', tried:'tried', fixed:'fixed',
                         decision:'decisions', gotcha:'gotchas' };
        function chipsFor(f) {
            const story = FILE_BY_PATH[fileKeyFor(f) || ''];
            if (!story) return [];
            return CHIP_SPEC.map(spec => {
                const n = story.evs.filter(spec[1]).length;
                return n ? [n + ' ' + (n === 1 ? spec[0] : PLURAL[spec[0]]), spec[2], spec[3]] : null;
            }).filter(Boolean);
        }
        // group the visible files by parent directory
        const dirs = {};
        fileNodes.forEach(f => { const d = dirOf(f); (dirs[d] = dirs[d] || []).push(f); });
        const dirNames = Object.keys(dirs).sort();
        // wider action lane: up to four event-type chips per file
        const rowH = 62, dirX = 200, fileX = 400, actX = 620, memX = 1010, WIDTH = 1190;
        const HEIGHT = Math.max(420, fileNodes.length * rowH + 130);
        const paneW = host.node().clientWidth || 800, paneH = host.node().clientHeight || 600;
        // "showing N of M" note (floating pill, centered)
        if (hiddenCount || cappedCount) {
            const parts = [];
            if (cappedCount) parts.push('showing top ' + fileNodes.length + ' of ' + (fileNodes.length + cappedCount) + ' files');
            if (hiddenCount) parts.push(hiddenCount + ' dependency/build file' + (hiddenCount > 1 ? 's' : '') + ' hidden');
            host.append('div').attr('class', 'flow-note')
                .style('position', 'absolute').style('right', '12px').style('top', '54px')
                .style('z-index', '4')
                .style('padding', '4px 12px').style('font-size', '11px').style('font-weight', '600')
                .style('color', 'var(--text-muted)').style('background', 'var(--surface)')
                .style('border', '1px solid var(--border-light)').style('border-radius', '20px')
                .text(parts.join('  ·  ') + (cappedCount ? '  (ranked by activity)' : ''));
        }
        // Open at a width-fit (top-anchored), then zoom/pan takes over:
        // wheel = zoom to cursor, drag = pan. Fixes the old fit-to-both ribbon AND
        // restores interactive zoom.
        const fitK = Math.min(paneW / (WIDTH + 40), 1);
        const outer = host.append('svg').attr('width', paneW).attr('height', paneH).style('cursor', 'grab');
        outer.append('defs').append('marker').attr('id', 'flowarr').attr('viewBox', '0 0 10 10')
            .attr('refX', 9).attr('refY', 5).attr('markerWidth', 7).attr('markerHeight', 7).attr('orient', 'auto')
            .append('path').attr('d', 'M0,0 L10,5 L0,10 z').attr('fill', '#8FA8C8');
        const svg = outer.append('g');
        const flowZoom = d3.zoom().scaleExtent([0.2, 4]).on('zoom', ev => svg.attr('transform', ev.transform));
        outer.call(flowZoom).on('dblclick.zoom', null);
        const initT = d3.zoomIdentity.translate(Math.max(8, (paneW - WIDTH * fitK) / 2), 20).scale(fitK);
        outer.call(flowZoom.transform, initT);
        // zoom controls (bottom-right)
        const zc = host.append('div').attr('class', 'flow-zoom')
            .style('position', 'absolute').style('right', '12px').style('bottom', '12px').style('z-index', '4')
            .style('display', 'flex').style('gap', '4px');
        const zbtn = (label, wide, fn) => zc.append('button').text(label)
            .style('height', '30px').style('min-width', '30px').style('padding', wide ? '0 10px' : '0')
            .style('border', '1px solid var(--border-light)').style('border-radius', '7px')
            .style('background', 'var(--surface)').style('color', 'var(--text)')
            .style('font-size', wide ? '11px' : '15px').style('font-weight', '700')
            .style('cursor', 'pointer').style('line-height', '1').on('click', fn);
        zbtn('+', false, () => outer.transition().duration(180).call(flowZoom.scaleBy, 1.3));
        zbtn('−', false, () => outer.transition().duration(180).call(flowZoom.scaleBy, 1 / 1.3));
        zbtn('Fit', true, () => outer.transition().duration(180).call(flowZoom.transform, initT));
        const link = (x1, y1, x2, y2, hot) => svg.append('path')
            .attr('fill', 'none').attr('stroke', hot ? '#E8593B' : '#8FA8C8')
            .attr('stroke-width', hot ? 2 : 1.5).attr('marker-end', 'url(#flowarr)')
            .attr('d', `M${x1},${y1} C${(x1 + x2) / 2},${y1} ${(x1 + x2) / 2},${y2} ${x2 - 4},${y2}`);
        // row positions
        let fy = 84; const fpos = {}, dpos = {};
        dirNames.forEach(d => {
            dirs[d].forEach(f => { fpos[f.id] = fy; fy += rowH; });
            const ys = dirs[d].map(f => fpos[f.id]);
            dpos[d] = ys.reduce((a, b) => a + b, 0) / ys.length;
        });
        const projY = Object.values(dpos).reduce((a, b) => a + b, 0) / Math.max(1, dirNames.length);
        // column headers
        [['PROJECT', 100], ['DIRECTORIES', dirX + 72], ['FILES', fileX + 80], ['WHAT HAPPENED', actX + 66], ['MEMORY', memX + 68]]
            .forEach(([txt, x]) => svg.append('text').attr('x', x).attr('y', 42).attr('text-anchor', 'middle')
                .attr('font-size', 10.5).attr('font-weight', 700).attr('letter-spacing', 1)
                .attr('fill', 'var(--text-muted)').text(txt));
        // project box
        svg.append('rect').attr('x', 26).attr('y', projY - 25).attr('width', 148).attr('height', 50)
            .attr('rx', 12).attr('fill', 'var(--navy)');
        svg.append('text').attr('x', 100).attr('y', projY - 2).attr('text-anchor', 'middle')
            .attr('fill', '#fff').attr('font-weight', 700).attr('font-size', 13.5).text(projectName || 'project');
        svg.append('text').attr('x', 100).attr('y', projY + 15).attr('text-anchor', 'middle')
            .attr('fill', '#9DB5D0').attr('font-size', 10).text(timelineData.length + ' events captured');
        // memory cylinder — everything flows into the append-only log
        const my = projY, cyl = svg.append('g');
        cyl.append('rect').attr('x', memX).attr('y', my - 36).attr('width', 136).attr('height', 72)
            .attr('fill', '#FFF6D9').attr('stroke', '#D8C27A');
        cyl.append('ellipse').attr('cx', memX + 68).attr('cy', my - 36).attr('rx', 68).attr('ry', 12)
            .attr('fill', '#FFEFB8').attr('stroke', '#D8C27A');
        cyl.append('ellipse').attr('cx', memX + 68).attr('cy', my + 36).attr('rx', 68).attr('ry', 12)
            .attr('fill', '#FFF6D9').attr('stroke', '#D8C27A');
        cyl.append('text').attr('x', memX + 68).attr('y', my - 1).attr('text-anchor', 'middle')
            .attr('font-size', 11.5).attr('font-weight', 700).attr('fill', '#7A6420').text('events.jsonl');
        cyl.append('text').attr('x', memX + 68).attr('y', my + 15).attr('text-anchor', 'middle')
            .attr('font-size', 9.5).attr('fill', '#7A6420').text('append-only memory');
        // directories, files, chips
        dirNames.forEach(d => {
            const yc = dpos[d];
            link(174, projY, dirX, yc);
            svg.append('rect').attr('x', dirX).attr('y', yc - 21).attr('width', 145).attr('height', 42)
                .attr('rx', 9).attr('fill', 'var(--surface2)').attr('stroke', 'var(--border-light)');
            svg.append('text').attr('x', dirX + 72).attr('y', yc - 1).attr('text-anchor', 'middle')
                .attr('font-size', 12).attr('font-weight', 600).attr('fill', 'var(--text)')
                .text(d.length > 22 ? '…' + d.slice(-21) : d);
            svg.append('text').attr('x', dirX + 72).attr('y', yc + 13).attr('text-anchor', 'middle')
                .attr('font-size', 10).attr('fill', 'var(--text-dim)')
                .text(dirs[d].length + (dirs[d].length > 1 ? ' files' : ' file'));
            dirs[d].forEach(f => {
                const y = fpos[f.id], hot = (f.failure_count || 0) >= 3;
                const fkey = fileKeyFor(f);
                link(dirX + 145, yc, fileX, y, hot);
                svg.append('rect').attr('x', fileX).attr('y', y - 21).attr('width', 160).attr('height', 42)
                    .attr('rx', 9).attr('fill', 'var(--surface)')
                    .attr('stroke', hot ? 'var(--error)' : 'var(--border-light)').attr('stroke-width', hot ? 1.8 : 1)
                    .style('cursor', fkey ? 'pointer' : 'default')
                    .on('click', () => { if (fkey) openFile(fkey); });
                svg.append('text').attr('x', fileX + 13).attr('y', y - 1)
                    .attr('font-size', 12).attr('font-weight', 600).attr('fill', 'var(--text)')
                    .text((f.label || f.id).length > 19 ? (f.label || f.id).slice(0, 18) + '…' : (f.label || f.id));
                svg.append('text').attr('x', fileX + 13).attr('y', y + 13)
                    .attr('font-size', 10).attr('fill', 'var(--text-dim)').text((f.event_count || 0) + ' events');
                const chips = chipsFor(f);
                link(fileX + 160, y, actX, y, hot);
                let cx = actX;
                const SHOWN = 4;
                chips.slice(0, SHOWN).forEach(([txt, col, kind]) => {
                    const w = txt.length * 6.2 + 18;
                    // Each chip opens the events it counts — same popup as everywhere else.
                    const chip = svg.append('g')
                        .style('cursor', fkey ? 'pointer' : 'default')
                        .on('click', () => { if (fkey) openFile(fkey, kind); });
                    chip.append('rect').attr('x', cx).attr('y', y - 12).attr('width', w).attr('height', 24)
                        .attr('rx', 12).attr('fill', col + '15').attr('stroke', col);
                    chip.append('text').attr('x', cx + w / 2).attr('y', y + 4).attr('text-anchor', 'middle')
                        .attr('font-size', 10).attr('font-weight', 700).attr('fill', col).text(txt);
                    if (fkey) chip.append('title').text('Open the ' + txt.replace(/^\\d+ /, '') + ' on this file');
                    cx += w + 7;
                });
                if (chips.length > SHOWN) {
                    const extra = chips.length - SHOWN, w = 42;
                    const more = svg.append('g').style('cursor', fkey ? 'pointer' : 'default')
                        .on('click', () => { if (fkey) openFile(fkey); });
                    more.append('rect').attr('x', cx).attr('y', y - 12).attr('width', w).attr('height', 24)
                        .attr('rx', 12).attr('fill', 'var(--surface2)').attr('stroke', 'var(--border-light)');
                    more.append('text').attr('x', cx + w / 2).attr('y', y + 4).attr('text-anchor', 'middle')
                        .attr('font-size', 10).attr('font-weight', 700).attr('fill', 'var(--text-dim)').text('+' + extra);
                    more.append('title').text('Open the full history for this file');
                    cx += w + 7;
                }
                if (!chips.length) {
                    svg.append('text').attr('x', actX).attr('y', y + 4)
                        .attr('font-size', 10).attr('fill', 'var(--text-dim)').text('activity logged');
                    cx = actX + 86;
                }
                link(Math.min(cx, memX - 22), y, memX, my + (y > my ? 28 : y < my ? -28 : 0));
            });
        });
    }

    // View toggle — Flow is the default view
    const mapPane = document.querySelector('.map-graph-pane');
    let treeRendered = false;
    mapPane.classList.add('flow-mode');
    renderMapFlow();
    document.querySelectorAll('.map-view-toggle:not(.tl-toggle) .map-view-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.map-view-toggle:not(.tl-toggle) .map-view-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            mapPane.classList.remove('tree-mode', 'flow-mode');
            if (btn.dataset.view === 'tree') {
                mapPane.classList.add('tree-mode');
                if (!treeRendered) { renderTree(); treeRendered = true; }
            } else if (btn.dataset.view === 'flow') {
                mapPane.classList.add('flow-mode');
                renderMapFlow();
            }
        });
    });
    // Details pane collapse — gives every map view the full width
    const mapSplit = document.querySelector('.map-split');
    document.getElementById('map-details-btn').addEventListener('click', function () {
        mapSplit.classList.toggle('details-collapsed');
        this.textContent = mapSplit.classList.contains('details-collapsed') ? 'Show details' : 'Hide details';
        if (mapPane.classList.contains('tree-mode')) renderTree();
        else if (mapPane.classList.contains('flow-mode')) renderMapFlow();
    });
    // Responsive — re-fit the active map view when the window resizes.
    let mapResizeTimer;
    window.addEventListener('resize', () => {
        clearTimeout(mapResizeTimer);
        mapResizeTimer = setTimeout(() => {
            if (mapPane.classList.contains('flow-mode')) renderMapFlow();
            else if (mapPane.classList.contains('tree-mode')) renderTree();
        }, 160);
    });
    window.addEventListener('resize', () => {
        if (mapPane.classList.contains('tree-mode')) renderTree();
        else if (mapPane.classList.contains('flow-mode')) renderMapFlow();
    });

    // ══════════════════════════════════════════
    // TAB 4: Timeline — Grouped + Activity Chart
    // ══════════════════════════════════════════
    const eventTypes = [...new Set(timelineData.map(e=>e.type))];
    const activeFilters = new Set([...eventTypes, '_all']);
    let captureFilter = 'all'; // 'all', 'manual', 'auto'
    const filtersEl = document.getElementById('tl-filters');
    const listEl = document.getElementById('tl-list');

    // Count per type
    const typeCounts = {};
    timelineData.forEach(e => { typeCounts[e.type] = (typeCounts[e.type]||0)+1; });

    // Source filter pills (Manual / Auto)
    if (autoCount > 0) {
        const sourceFilters = [
            { key:'all', label:'All', count:timelineData.length },
            { key:'manual', label:'Manual', count:manualCount },
            { key:'auto', label:'Auto-captured', count:autoCount },
        ];
        sourceFilters.forEach(sf => {
            const btn = document.createElement('div');
            btn.className = 'tl-filter' + (sf.key==='all'?' active':'');
            btn.style.borderColor = sf.key==='auto'?'#818cf8':sf.key==='manual'?'#3b82f6':'';
            btn.innerHTML = sf.label + ' <span class="count">' + sf.count + '</span>';
            btn.addEventListener('click', () => {
                captureFilter = sf.key;
                filtersEl.querySelectorAll('.tl-filter-source').forEach(b=>b.classList.remove('active'));
                btn.classList.add('active');
                renderTimeline();
            });
            btn.classList.add('tl-filter-source');
            if (sf.key==='all') btn.classList.add('active');
            filtersEl.appendChild(btn);
        });
        // Separator
        const sep = document.createElement('div');
        sep.style.cssText = 'width:1px;height:20px;background:var(--border);margin:0 4px;';
        filtersEl.appendChild(sep);
    }

    eventTypes.forEach(type => {
        const btn = document.createElement('div');
        btn.className = 'tl-filter active';
        btn.innerHTML = pmEsc(type) + ' <span class="count">' + (typeCounts[type]||0) + '</span>';
        btn.addEventListener('click', () => {
            if (activeFilters.has(type)) { activeFilters.delete(type); btn.classList.remove('active'); }
            else { activeFilters.add(type); btn.classList.add('active'); }
            renderTimeline();
        });
        filtersEl.appendChild(btn);
    });

    // Activity mini-chart
    function buildActivityChart() {
        const actEl = document.getElementById('tl-activity');
        const sorted = [...timelineData].sort((a,b)=>new Date(a.timestamp)-new Date(b.timestamp));
        const dayMap = {};
        sorted.forEach(e => {
            const day = new Date(e.timestamp).toDateString();
            dayMap[day] = (dayMap[day]||0)+1;
        });
        const days = Object.entries(dayMap);
        const maxCount = Math.max(...days.map(d=>d[1]), 1);
        days.forEach(([day,count]) => {
            const bar = document.createElement('div');
            bar.className = 'tl-activity-bar';
            bar.style.height = Math.max((count/maxCount)*100, 8)+'%';
            bar.title = day+': '+count+' events';
            actEl.appendChild(bar);
        });
    }
    buildActivityChart();

    function renderTimeline() {
        let filtered = [...timelineData].filter(e=>activeFilters.has(e.type));
        // Apply capture source filter
        if (captureFilter === 'manual') filtered = filtered.filter(e => !e.auto_captured);
        else if (captureFilter === 'auto') filtered = filtered.filter(e => e.auto_captured);
        const sorted = filtered.sort((a,b)=>new Date(b.timestamp)-new Date(a.timestamp));

        // Group by date
        const groups = {};
        sorted.forEach(e => {
            const d = new Date(e.timestamp);
            const key = d.toLocaleDateString('en-US', {weekday:'short', month:'short', day:'numeric', year:'numeric'});
            if (!groups[key]) groups[key]=[];
            groups[key].push(e);
        });

        const srcLabels = { git_post_commit:'git commit', git_post_revert:'git revert', git_post_merge:'git merge', churn_detector:'churn detected', ci_parser:'CI result' };
        let html = '';
        for (const [date, events] of Object.entries(groups)) {
            html += '<div class="tl-date-group"><div class="tl-date-label">'+date+' &middot; '+events.length+' events</div>';
            events.forEach(e => {
                const outcomeClass = e.outcome==='failed'?'tl-outcome-failed':e.outcome==='worked'?'tl-outcome-worked':'';
                const outcomeLabel = e.outcome?' <span class="'+outcomeClass+'">['+pmEsc(e.outcome)+']</span>':'';
                const loc = e.location?'<span style="color:var(--accent)"> @ '+pmEsc(e.location)+'</span>':'';
                const iid = e.issue_id?'<span style="color:var(--text-muted)"> #'+pmEsc(e.issue_id)+'</span>':'';
                const autoBadge = e.auto_captured?'<span class="tl-auto-badge">AUTO</span>':'';
                const ts = e.timestamp ? new Date(e.timestamp).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}) : '';
                const capSrc = e.auto_captured && e.capture_source ? '<span class="tl-capture-source"> &middot; '+pmEsc(srcLabels[e.capture_source]||e.capture_source)+'</span>' : '';
                const caseAttr = e.issue_id && CASE_BY_ID[e.issue_id] ? ' data-case="'+pmEsc(e.issue_id)+'"' : '';
                html += '<div class="tl-item'+(caseAttr?' clickable':'')+'"'+caseAttr+'><div class="tl-badge '+pmEsc(e.type)+'">'+pmEsc(e.type)+'</div><div class="tl-body"><div class="tl-summary">'+pmEsc(e.summary)+outcomeLabel+iid+autoBadge+'</div><div class="tl-meta">'+ts+loc+capSrc+'</div></div></div>';
            });
            html += '</div>';
        }
        if (sorted.length === 0) html = '<div style="text-align:center;color:var(--text-muted);padding:40px">No events match current filters</div>';
        listEl.innerHTML = html;
    }
    renderTimeline();
    // Any timeline row that belongs to a case opens the full chain.
    listEl.addEventListener('click', ev => {
        const row = ev.target.closest('.tl-item[data-case]');
        if (row) openCase(row.dataset.case);
    });

    // TAB 4b: Timeline — "Time Spine" view (default)
    // Central real-time axis; problems branch left, knowledge branches right.
    function renderTimelineSpine() {
        const body = document.getElementById('tsp-body');
        if (!body) return;
        const tspEsc = s => String(s == null ? '' : s).replace(/[&<>]/g, c => c === '&' ? '&amp;' : c === '<' ? '&lt;' : '&gt;');
        function tspMeta(e) {
            if (e.type === 'attempt') {
                if (e.outcome === 'failed') return ['#E8593B', 'ATTEMPT — FAILED', 'L'];
                if (e.outcome === 'worked') return ['#169F84', 'ATTEMPT — WORKED', 'L'];
                return ['#E8A33B', 'ATTEMPT — PARTIAL', 'L'];
            }
            if (e.type === 'issue') return ['#1F6FEB', 'ISSUE OPENED', 'L'];
            if (e.type === 'fix') return ['#169F84', 'FIX', 'R'];
            if (e.type === 'decision') return ['#6366F1', 'DECISION', 'R'];
            if (e.type === 'note') return ['#5A6B82', 'NOTE', 'R'];
            return ['#8A99AD', (e.type || 'EVENT').toUpperCase(), 'R'];
        }
        const sorted = [...timelineData].sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp));
        let html = '', lastDay = null, lastT = null;
        sorted.forEach(e => {
            const d = new Date(e.timestamp);
            const ok = !isNaN(d.getTime());
            const day = ok ? d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' }) : 'undated';
            const hm = ok ? d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' }) : '';
            if (day !== lastDay) { html += '<div class="tsp-day"><b>' + day + '</b></div>'; lastDay = day; lastT = null; }
            else if (ok && lastT && (d.getTime() - lastT) > 3 * 3600 * 1000) {
                html += '<div class="tsp-gap"><span>' + Math.round((d.getTime() - lastT) / 3600000) + 'h quiet</span></div>';
            }
            if (ok) lastT = d.getTime();
            const [col, kind, side] = tspMeta(e);
            const iss = e.issue_id ? '<span class="tsp-iss">#' + tspEsc(e.issue_id) + '</span>' : '';
            html += '<div class="tsp-row tsp-' + side + '" data-issue="' + tspEsc(e.issue_id || '') + '">'
                + '<div class="tsp-tick"></div><div class="tsp-dot" style="background:' + col + '"></div>'
                + '<div class="tsp-card" style="--ac:' + col + '">'
                + '<div class="tsp-k">' + kind + '<span class="tsp-t">' + hm + '</span></div>'
                + '<div class="tsp-s">' + tspEsc(e.summary || '') + '</div>'
                + '<div class="tsp-m">' + iss + tspEsc(e.location || '') + '</div>'
                + '</div></div>';
        });
        body.innerHTML = html || '<div class="flow-empty">No events yet — start logging to build your timeline.</div>';
        const spineEl = document.getElementById('tl-spine');
        body.querySelectorAll('.tsp-row').forEach(r => {
            r.addEventListener('mouseenter', () => {
                const iss = r.dataset.issue; if (!iss) return;
                spineEl.classList.add('tsp-hl');
                body.querySelectorAll('.tsp-row').forEach(x =>
                    x.querySelector('.tsp-card').classList.toggle('tsp-on', x.dataset.issue === iss));
            });
            r.addEventListener('mouseleave', () => {
                spineEl.classList.remove('tsp-hl');
                body.querySelectorAll('.tsp-card').forEach(c => c.classList.remove('tsp-on'));
            });
            // Same case chain the Overview opens — the spine is the default view,
            // so this is where most clicks land.
            if (r.dataset.issue && CASE_BY_ID[r.dataset.issue]) {
                r.style.cursor = 'pointer';
                r.addEventListener('click', () => openCase(r.dataset.issue));
            }
        });
    }
    renderTimelineSpine();
    document.querySelectorAll('.tl-toggle .map-view-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.tl-toggle .map-view-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            document.getElementById('panel-timeline').classList.toggle('list-mode', btn.dataset.tlview === 'list');
        });
    });

    // ══════════════════════════════════════════
    // TAB 5: SHOWOFF — animated story scenes + recorder
    // Zero new dependencies: canvas 2D + d3-force (already loaded).
    // ══════════════════════════════════════════
    (function () {
        const panel = document.getElementById('panel-showoff');
        const cv = document.getElementById('so-canvas');
        if (!panel || !cv) return;
        const ctx = cv.getContext('2d');
        let W = 0, H = 0;

        function resize() {
            const r = cv.parentElement.getBoundingClientRect();
            const dpr = window.devicePixelRatio || 1;
            W = Math.max(200, r.width); H = Math.max(200, r.height);
            cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
            cv.style.width = W + 'px'; cv.style.height = H + 'px';
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        }
        window.addEventListener('resize', function () {
            if (isActive()) { resize(); const sc = scenes[sceneName]; if (sc && sc.resize) sc.resize(); }
        });

        // ── palette (poster/brand) ──
        const PC = { issue:'#4a90f4', fix:'#2FD6A5', decision:'#818cf8', note:'#5A6B82',
                     failed:'#E8593B', worked:'#169F84', partial:'#E8A33B',
                     file:'#E8A33B', hotfile:'#E8593B', root:'#cfe0ff' };
        function nodeColor(n) {
            if (n.type === 'file') return (n.failure_count || 0) >= 3 ? PC.hotfile : PC.file;
            if (n.type === 'root') return PC.root;
            if (n.event_type === 'attempt') return PC[n.outcome] || PC.partial;
            return PC[n.event_type] || '#9aa7bd';
        }
        function nodeTitle(n) {
            if (n.type === 'file') return 'File: ' + (n.label || n.id);
            if (n.type === 'root') return projectName || 'project';
            const kind = n.event_type === 'attempt'
                ? (n.outcome === 'failed' ? 'Failed attempt' : n.outcome === 'worked' ? 'Attempt worked' : 'Attempt (partial)')
                : (n.event_type || 'event');
            return kind.charAt(0).toUpperCase() + kind.slice(1);
        }

        // ── shared real-data graph (same nodes/links as the Story Map) ──
        const ROOT = { id: '__so_root', type: 'root', label: projectName || 'project' };
        const files = data.nodes.filter(function (n) { return n.type === 'file'; });
        const events = data.nodes.filter(function (n) { return n.type === 'event'; })
            .slice().sort(function (a, b) { return String(a.timestamp || '').localeCompare(String(b.timestamp || '')); });
        const links = data.links.map(function (l) {
            return { s: (l.source && l.source.id) || l.source, t: (l.target && l.target.id) || l.target };
        });
        const adj = {};
        links.forEach(function (l) {
            (adj[l.s] = adj[l.s] || []).push(l.t);
            (adj[l.t] = adj[l.t] || []).push(l.s);
        });
        const byId = {}; data.nodes.forEach(function (n) { byId[n.id] = n; });

        // glow sprite cache (fast canvas glow without shadowBlur)
        const sprites = {};
        function sprite(col) {
            if (sprites[col]) return sprites[col];
            const s = document.createElement('canvas'); s.width = s.height = 64;
            const c = s.getContext('2d');
            const g = c.createRadialGradient(32, 32, 2, 32, 32, 32);
            g.addColorStop(0, col); g.addColorStop(0.4, col); g.addColorStop(1, 'rgba(7,12,22,0)');
            c.fillStyle = g; c.beginPath(); c.arc(32, 32, 32, 0, 6.2832); c.fill();
            sprites[col] = s; return s;
        }
        function glowDot(x, y, r, col, a) {
            ctx.globalAlpha = a == null ? 1 : a;
            ctx.drawImage(sprite(col), x - r * 2.2, y - r * 2.2, r * 4.4, r * 4.4);
            ctx.globalAlpha = a == null ? 1 : a;
            ctx.fillStyle = col; ctx.beginPath(); ctx.arc(x, y, r, 0, 6.2832); ctx.fill();
            ctx.globalAlpha = 1;
        }

        // ── state ──
        let sceneName = 'universe', playing = true, speed = 1, sel = null, t0 = performance.now();
        let raf = null, last = 0, inited = false;
        function isActive() { return panel.classList.contains('active'); }
        function speedNow() { return speed * (sel ? 0.2 : 1); }

        // ── detail card ──
        const card = document.getElementById('so-card');
        function esc(s) { return String(s == null ? '' : s).replace(/[&<>]/g, function (c) { return c === '&' ? '&amp;' : c === '<' ? '&lt;' : '&gt;'; }); }
        function showCard(n) {
            let h = '<h3>' + esc(nodeTitle(n)) + '</h3>';
            if (n.type === 'event') {
                h += '<div class="so-row">' + esc(n.summary || n.label || '') + '</div>';
                if (n.location) h += '<div class="so-row"><b>where:</b> ' + esc(n.location) + '</div>';
                if (n.issue_id) h += '<div class="so-row"><b>issue:</b> #' + esc(n.issue_id) + '</div>';
                if (n.timestamp) h += '<div class="so-row"><b>when:</b> ' + esc(String(n.timestamp).slice(0, 10)) + '</div>';
            } else if (n.type === 'file') {
                h += '<div class="so-row">' + esc(n.path || n.id) + '</div>';
                h += '<div class="so-row"><b>events:</b> ' + (n.event_count || 0) + ' - <b>failures:</b> ' + (n.failure_count || 0) + '</div>';
            } else {
                h += '<div class="so-row">project root - ' + events.length + ' events - ' + files.length + ' files</div>';
            }
            h += '<div class="so-dim">highlighted - click it again (or empty space) to release</div>';
            card.innerHTML = h; card.style.display = 'block';
        }
        function clearSel() { sel = null; card.style.display = 'none'; }

        // ══ SCENE: Story Replay (d3-force + canvas) ══
        const replay = (function () {
            const nodes = [ROOT].concat(files.map(clone)).concat(events.map(clone));
            function clone(n) { const c = {}; for (const k in n) c[k] = n[k]; return c; }
            const nById = {}; nodes.forEach(function (n) { nById[n.id] = n; });
            // birth order: events by time; a file is born with its first event; root at -1
            let order = 0;
            ROOT.born = -1;
            events.forEach(function (ev) {
                const mine = (adj[ev.id] || []).filter(function (o) { return nById[o] && nById[o].type === 'file'; });
                mine.forEach(function (f) { const fn = nById[f]; if (fn.born === undefined) { fn.born = order; order += 1; } });
                nById[ev.id].born = order; order += 1;
            });
            files.forEach(function (f) { if (nById[f.id].born === undefined) { nById[f.id].born = order; order += 1; } });
            const STEPS = order;
            const simLinks = links
                .filter(function (l) { return nById[l.s] && nById[l.t]; })
                .map(function (l) { return { source: l.s, target: l.t }; })
                .concat(files.map(function (f) { return { source: ROOT.id, target: f.id }; }));
            let reveal = 1, acc = 0, sim = null;
            function visible(n) { return (n.born === undefined ? 0 : n.born) < reveal; }
            function rebuild() {
                ROOT.fx = W / 2; ROOT.fy = H / 2;   // project stays anchored at centre
                const vn = nodes.filter(visible);
                const vset = {}; vn.forEach(function (n) { vset[n.id] = 1; });
                const vl = simLinks.filter(function (l) {
                    const s = l.source.id || l.source, t = l.target.id || l.target;
                    return vset[s] && vset[t];
                });
                // seed new nodes near a visible neighbour (or centre) so they bloom in place
                vn.forEach(function (n) {
                    if (n.x !== undefined) return;
                    let px = W / 2, py = H / 2;
                    (adj[n.id] || []).some(function (o) {
                        const m = nById[o];
                        if (m && vset[o] && m.x !== undefined) { px = m.x; py = m.y; return true; }
                        return false;
                    });
                    n.x = px + (Math.random() - 0.5) * 60;
                    n.y = py + (Math.random() - 0.5) * 60;
                });
                if (!sim) {
                    sim = d3.forceSimulation(vn)
                        .force('charge', d3.forceManyBody().strength(-160))
                        .force('link', d3.forceLink(vl).id(function (d) { return d.id; }).distance(function (l) {
                            return (l.source.id === ROOT.id || l.target.id === ROOT.id) ? 95 : 42; }))
                        .force('center', d3.forceCenter(W / 2, H / 2))
                        .force('collide', d3.forceCollide(14));
                } else {
                    sim.nodes(vn);
                    sim.force('link').links(vl);
                    sim.force('center', d3.forceCenter(W / 2, H / 2));
                }
                sim.alpha(0.5).restart();
            }
            let doneHold = 0;
            return {
                scrub: true,
                init: function () { reveal = Math.max(1, reveal); ROOT.x = W / 2; ROOT.y = H / 2; rebuild(); },
                resize: function () { rebuild(); },
                setScrub: function (v) { reveal = Math.max(1, Math.round(v * STEPS)); if (reveal > STEPS) reveal = STEPS; doneHold = 0; rebuild(); },
                getScrub: function () { return STEPS ? reveal / STEPS : 1; },
                step: function (dt) {
                    acc += dt * speedNow();
                    if (acc > 0.8) {
                        acc = 0;
                        if (reveal < STEPS) { reveal += 1; rebuild(); }
                        else { doneHold += 1; if (doneHold > 5) { doneHold = 0; reveal = 1; rebuild(); } }
                    }
                },
                draw: function (t) {
                    const vn = (sim ? sim.nodes() : []);
                    const dimOn = !!sel;
                    ctx.strokeStyle = 'rgba(120,150,200,0.18)'; ctx.lineWidth = 1;
                    (sim ? sim.force('link').links() : []).forEach(function (l) {
                        ctx.globalAlpha = dimOn ? 0.05 : 1;
                        ctx.beginPath(); ctx.moveTo(l.source.x, l.source.y); ctx.lineTo(l.target.x, l.target.y); ctx.stroke();
                    });
                    ctx.globalAlpha = 1;
                    let latest = null;
                    vn.forEach(function (n) {
                        if (n.born === reveal - 1 && n.type === 'event') latest = n;
                        const col = nodeColor(n);
                        const base = n.type === 'root' ? 9 : n.type === 'file' ? 5 + Math.sqrt(n.event_count || 1) : 4;
                        const isNew = n.born !== undefined && n.born >= reveal - 2;
                        const pulse = isNew ? (1 + 0.25 * Math.abs(Math.sin(t * 4))) : 1;
                        const dim = dimOn && sel.id !== n.id && (adj[sel.id] || []).indexOf(n.id) < 0 && n.id !== ROOT.id;
                        glowDot(n.x, n.y, base * pulse, col, dim ? 0.08 : 1);
                        if (!dim && (n.type === 'file' || n.type === 'root')) {
                            ctx.fillStyle = 'rgba(205,217,236,0.85)'; ctx.font = '11px Inter, sans-serif';
                            ctx.textAlign = 'center'; ctx.fillText(n.label || '', n.x, n.y + base + 14);
                        }
                    });
                    if (sel && sel.x !== undefined) haloAndLinks(sel, vn);
                    if (latest && !dimOn) caption((latest.event_type || '') + ': ' + (latest.summary || latest.label || ''));
                    hud(reveal + ' / ' + STEPS + ' events');
                },
                pick: function (x, y) { return nearest(sim ? sim.nodes() : [], x, y); }
            };
        })();

        // ══ SCENE: Orbit (pure canvas) ══
        const orbit = (function () {
            let ang = 0, pos = [];
            const byFile = {};
            events.forEach(function (ev) {
                const fs = (adj[ev.id] || []).filter(function (o) { return byId[o] && byId[o].type === 'file'; });
                const key = fs.length ? fs[0] : '__none';
                (byFile[key] = byFile[key] || []).push(ev);
            });
            return {
                scrub: false,
                init: function () { pos = []; },
                step: function (dt) { ang += dt * 0.15 * speedNow(); },
                draw: function (t) {
                    pos = [];
                    const cx = W / 2, cy = H / 2, R1 = Math.min(W, H) * 0.30;
                    const dimOn = !!sel;
                    files.forEach(function (f, i) {
                        const a = ang + (i / Math.max(1, files.length)) * 6.2832;
                        const fx = cx + Math.cos(a) * R1, fy = cy + Math.sin(a) * R1;
                        const evs = byFile[f.id] || [];
                        const r2 = 26 + evs.length * 2.2;
                        ctx.globalAlpha = dimOn ? 0.05 : 1;
                        ctx.strokeStyle = 'rgba(120,150,200,0.10)';
                        ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(fx, fy); ctx.stroke();
                        ctx.strokeStyle = 'rgba(120,150,200,0.07)';
                        ctx.beginPath(); ctx.arc(fx, fy, r2, 0, 6.2832); ctx.stroke();
                        ctx.globalAlpha = 1;
                        evs.forEach(function (ev, j) {
                            const ma = ang * 2.1 + (j / Math.max(1, evs.length)) * 6.2832;
                            const mx = fx + Math.cos(ma) * r2, my = fy + Math.sin(ma) * r2;
                            const dim = dimOn && sel.id !== ev.id && (adj[sel.id] || []).indexOf(ev.id) < 0;
                            glowDot(mx, my, ev.event_type === 'fix' ? 4.4 : 3.2, nodeColor(ev), dim ? 0.06 : 1);
                            pos.push({ n: ev, x: mx, y: my });
                        });
                        const fdim = dimOn && sel.id !== f.id && (adj[sel.id] || []).indexOf(f.id) < 0;
                        glowDot(fx, fy, 5 + Math.sqrt(f.event_count || 1), nodeColor(f), fdim ? 0.08 : 1);
                        if (!fdim) {
                            ctx.fillStyle = 'rgba(205,217,236,0.85)'; ctx.font = '11px Inter, sans-serif';
                            ctx.textAlign = 'center'; ctx.fillText(f.label || '', fx, fy - r2 - 8);
                        }
                        pos.push({ n: f, x: fx, y: fy });
                    });
                    const none = byFile.__none || [];
                    none.forEach(function (ev, j) {
                        const ma = 0 - ang * 1.5 + (j / Math.max(1, none.length)) * 6.2832;
                        const mx = cx + Math.cos(ma) * R1 * 0.4, my = cy + Math.sin(ma) * R1 * 0.4;
                        glowDot(mx, my, 3, nodeColor(ev), dimOn ? 0.06 : 1);
                        pos.push({ n: ev, x: mx, y: my });
                    });
                    glowDot(cx, cy, 9, PC.root, 1);
                    ctx.fillStyle = '#e6edf7'; ctx.font = 'bold 13px Inter, sans-serif'; ctx.textAlign = 'center';
                    ctx.fillText(projectName || 'project', cx, cy - 18);
                    if (sel) { const p = findPos(pos, sel); if (p) haloAt(p.x, p.y, nodeColor(sel)); }
                },
                pick: function (x, y) { return nearest(pos, x, y, true); }
            };
        })();

        // ══ SCENE: Universe (pure canvas galaxy) ══
        const universe = (function () {
            let ang = 0, stars = [], real = [], inited2 = false;
            function seed(n) { let s = (n * 9301 + 49297) % 233280; return function () { s = (s * 9301 + 49297) % 233280; return s / 233280; }; }
            function build() {
                stars = []; real = [];
                const rnd = seed(7), Rmax = Math.min(W, H) * 0.42, arms = 3, twist = 2.2;
                for (let i = 0; i < 420; i++) { const r = Math.abs(rnd() - rnd()) * Rmax * 0.18; stars.push({ r: r, a: rnd() * 6.2832, c: '#ffe9c4', s: 1.2 + rnd() * 1.6, al: 0.5 }); }
                for (let i = 0; i < 1050; i++) {
                    const r = Math.pow(rnd(), 0.62) * Rmax, arm = i % arms;
                    const a = arm * 6.2832 / arms + (r / Rmax) * twist * 6.2832 + (rnd() - 0.5) * 0.5;
                    stars.push({ r: r, a: a, c: r / Rmax < 0.5 ? '#6f8fe0' : '#8f76d8', s: 1 + rnd() * 1.5, al: 0.32 });
                }
                for (let i = 0; i < 260; i++) { stars.push({ r: rnd() * Rmax * 1.05, a: rnd() * 6.2832, c: '#c2cde6', s: 0.8 + rnd(), al: 0.18 }); }
                const rn = files.concat(events);
                rn.forEach(function (n, i) {
                    const rr = (0.25 + (i / Math.max(1, rn.length)) * 0.65) * Rmax;
                    const arm = i % arms;
                    const a = arm * 6.2832 / arms + (rr / Rmax) * twist * 6.2832 + (seed(i + 3)() - 0.5) * 0.35;
                    real.push({ n: n, r: rr, a: a });
                });
                inited2 = true;
            }
            return {
                scrub: false,
                init: function () { if (!inited2 || !stars.length) build(); },
                step: function (dt) { ang += dt * 0.05 * speedNow(); },
                draw: function (t) {
                    const cx = W / 2, cy = H / 2, Rmax = Math.min(W, H) * 0.42;
                    const dimOn = !!sel;
                    stars.forEach(function (s) {
                        const w = 0.4 + 0.9 * (1 - s.r / Rmax);
                        const a = s.a + ang * w;
                        glowDot(cx + Math.cos(a) * s.r, cy + Math.sin(a) * s.r, s.s, s.c, dimOn ? s.al * 0.25 : s.al);
                    });
                    const P = {};
                    real.forEach(function (rp) {
                        const a = rp.a + ang * 0.55;
                        P[rp.n.id] = { x: cx + Math.cos(a) * rp.r, y: cy + Math.sin(a) * rp.r };
                    });
                    ctx.strokeStyle = 'rgba(130,165,230,0.18)'; ctx.lineWidth = 1;
                    links.forEach(function (l) {
                        const a = P[l.s], b = P[l.t]; if (!a || !b) return;
                        ctx.globalAlpha = dimOn ? 0.04 : 1;
                        ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
                    });
                    ctx.globalAlpha = 1;
                    real.forEach(function (rp, k) {
                        const p = P[rp.n.id];
                        const blink = 0.7 + 0.5 * Math.abs(Math.sin(t * 2 + k * 0.7));
                        const base = rp.n.type === 'file' ? 4.5 + Math.sqrt(rp.n.event_count || 1) : 3.6;
                        const dim = dimOn && sel.id !== rp.n.id && (adj[sel.id] || []).indexOf(rp.n.id) < 0;
                        glowDot(p.x, p.y, base * (sel && sel.id === rp.n.id ? 1.6 : blink), nodeColor(rp.n), dim ? 0.07 : 1);
                        if (!dim && rp.n.type === 'file') {
                            ctx.fillStyle = 'rgba(205,217,236,0.8)'; ctx.font = '10px Inter, sans-serif';
                            ctx.textAlign = 'center'; ctx.fillText(rp.n.label || '', p.x, p.y - base - 8);
                        }
                    });
                    glowDot(cx, cy, 7, PC.root, 1);
                    ctx.fillStyle = '#e6edf7'; ctx.font = 'bold 12px Inter, sans-serif'; ctx.textAlign = 'center';
                    ctx.fillText(projectName || 'project', cx, cy - 16);
                    if (sel && P[sel.id]) {
                        haloAt(P[sel.id].x, P[sel.id].y, nodeColor(sel));
                        (adj[sel.id] || []).forEach(function (o) {
                            const b = P[o]; if (!b) return;
                            ctx.strokeStyle = nodeColor(sel); ctx.lineWidth = 1.6;
                            ctx.setLineDash([5, 6]); ctx.lineDashOffset = -(t * 40) % 22;
                            ctx.globalAlpha = 0.9; ctx.beginPath(); ctx.moveTo(P[sel.id].x, P[sel.id].y); ctx.lineTo(b.x, b.y); ctx.stroke();
                            ctx.setLineDash([]); ctx.globalAlpha = 1;
                        });
                    }
                    this._pos = Object.keys(P).map(function (id) { return { n: byId[id], x: P[id].x, y: P[id].y }; });
                },
                pick: function (x, y) { return nearest(this._pos || [], x, y, true); }
            };
        })();

        const scenes = { replay: replay, orbit: orbit, universe: universe };

        // ── shared helpers ──
        function nearest(arr, x, y, wrapped) {
            let best = null, bd = 20 * 20;
            arr.forEach(function (it) {
                const nx = wrapped ? it.x : it.x, ny = wrapped ? it.y : it.y;
                const n = wrapped ? it.n : it;
                if (nx === undefined) return;
                const d = (nx - x) * (nx - x) + (ny - y) * (ny - y);
                if (d < bd) { bd = d; best = n; }
            });
            return best;
        }
        function findPos(arr, n) { for (let i = 0; i < arr.length; i++) if (arr[i].n && arr[i].n.id === n.id) return arr[i]; return null; }
        function haloAt(x, y, col) {
            const t = (performance.now() - t0) / 1000;
            const rr = 14 + 7 * Math.abs(Math.sin(t * 3.2));
            ctx.strokeStyle = col; ctx.lineWidth = 2; ctx.globalAlpha = 0.9 - 0.5 * Math.abs(Math.sin(t * 3.2));
            ctx.beginPath(); ctx.arc(x, y, rr, 0, 6.2832); ctx.stroke(); ctx.globalAlpha = 1;
        }
        function haloAndLinks(n, vn) {
            if (n.x === undefined) return;
            haloAt(n.x, n.y, nodeColor(n));
            const t = (performance.now() - t0) / 1000;
            (adj[n.id] || []).forEach(function (o) {
                const b = vn.find(function (m) { return m.id === o; }); if (!b) return;
                ctx.strokeStyle = nodeColor(n); ctx.lineWidth = 1.6;
                ctx.setLineDash([5, 6]); ctx.lineDashOffset = -(t * 40) % 22;
                ctx.globalAlpha = 0.9; ctx.beginPath(); ctx.moveTo(n.x, n.y); ctx.lineTo(b.x, b.y); ctx.stroke();
                ctx.setLineDash([]); ctx.globalAlpha = 1;
            });
        }
        function caption(txt) {
            if (!txt) return;
            ctx.font = '12px Inter, sans-serif'; ctx.textAlign = 'center';
            const s = txt.length > 90 ? txt.slice(0, 90) + '...' : txt;
            ctx.fillStyle = 'rgba(159,176,200,0.9)';
            ctx.fillText(s, W / 2, 26);
        }
        function hud(txt) {
            ctx.font = '11px ui-monospace, Menlo, monospace'; ctx.textAlign = 'right';
            ctx.fillStyle = 'rgba(107,122,146,0.9)'; ctx.fillText(txt, W - 12, H - 12);
        }
        function watermark() {
            if (!wmOn) return;
            const txt = 'made with projectmem';
            ctx.font = 'bold 16px Inter, sans-serif';
            const tw = ctx.measureText(txt).width;
            const bh = 36, bx = 14, by = 12, bw = tw + 52;   // top-left: always in view
            ctx.fillStyle = 'rgba(18,32,58,0.90)';           // navy pill, lifted off the bg
            if (ctx.roundRect) { ctx.beginPath(); ctx.roundRect(bx, by, bw, bh, 18); ctx.fill(); }
            else ctx.fillRect(bx, by, bw, bh);
            ctx.strokeStyle = 'rgba(90,155,255,0.85)'; ctx.lineWidth = 1.4;
            if (ctx.roundRect) { ctx.beginPath(); ctx.roundRect(bx, by, bw, bh, 18); ctx.stroke(); }
            glowDot(bx + 20, by + bh / 2, 5, '#2FD6A5', 1);
            ctx.fillStyle = 'rgba(255,255,255,0.97)'; ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
            ctx.fillText(txt, bx + 35, by + bh / 2 + 1);
            ctx.textBaseline = 'alphabetic';
        }

        // ── recorder (MediaRecorder on the canvas — zero deps, 100% local) ──
        let rec = null, recChunks = [], recUntil = 0, recBtn = null, wmOn = true;
        function startRec(sec, btn) {
            if (rec) return;
            sec = Math.min(60, Math.max(3, sec));   // custom length, hard cap 1 minute
            if (!cv.captureStream || typeof MediaRecorder === 'undefined') {
                alert('Recording is not supported in this browser. Try Chrome, Edge or Firefox - or screen-record.');
                return;
            }
            let mime = 'video/webm;codecs=vp9';
            if (!MediaRecorder.isTypeSupported(mime)) mime = 'video/webm';
            try {
                rec = new MediaRecorder(cv.captureStream(30), { mimeType: mime, videoBitsPerSecond: 6000000 });
            } catch (e) { alert('Recording failed to start: ' + e.message); rec = null; return; }
            recChunks = [];
            rec.ondataavailable = function (e) { if (e.data && e.data.size) recChunks.push(e.data); };
            rec.onstop = function () {
                const b = new Blob(recChunks, { type: 'video/webm' });
                const a = document.createElement('a');
                a.href = URL.createObjectURL(b);
                a.download = (projectName || 'projectmem') + '-showoff-' + sceneName + '.webm';
                a.click();
                setTimeout(function () { URL.revokeObjectURL(a.href); }, 8000);
                if (recBtn) { recBtn.classList.remove('on'); recBtn.textContent = recBtn.dataset.label; }
                rec = null; recBtn = null;
            };
            rec.start(250);
            recUntil = performance.now() + sec * 1000;
            recBtn = btn; btn.dataset.label = btn.textContent; btn.classList.add('on');
        }
        function recTick(now) {
            if (!rec) return;
            const left = Math.max(0, Math.ceil((recUntil - now) / 1000));
            if (recBtn) recBtn.textContent = 'REC ' + left + 's';
            glowDot(W - 24, 26, 4.5, '#E8593B', 0.6 + 0.4 * Math.abs(Math.sin(now / 300)));
            if (now >= recUntil && rec.state !== 'inactive') rec.stop();
        }

        // ── main loop (runs only while the Showoff tab is active) ──
        function loop(now) {
            raf = null;
            if (!isActive()) { if (rec && rec.state !== 'inactive') rec.stop(); return; }
            const dt = Math.min(0.05, (now - last) / 1000) || 0.016;
            last = now;
            const t = (now - t0) / 1000;
            const sc = scenes[sceneName];
            if (playing) sc.step(dt);
            ctx.clearRect(0, 0, W, H);
            ctx.fillStyle = '#070c16'; ctx.fillRect(0, 0, W, H);
            sc.draw(t);
            watermark();
            recTick(now);
            if (sc.scrub) {
                const s = document.getElementById('so-scrub');
                if (s && document.activeElement !== s) s.value = Math.round(sc.getScrub() * 100);
            }
            raf = requestAnimationFrame(loop);
        }
        function ensureLoop() {
            resize();
            scenes[sceneName].init();
            last = performance.now();
            if (!raf) raf = requestAnimationFrame(loop);
        }
        document.querySelectorAll('.nav').forEach(function (n) {
            n.addEventListener('click', function () { if (n.dataset.panel === 'showoff') ensureLoop(); });
        });

        // ── controls ──
        document.querySelectorAll('.so-scn').forEach(function (b) {
            b.addEventListener('click', function () {
                document.querySelectorAll('.so-scn').forEach(function (x) { x.classList.remove('active'); });
                b.classList.add('active');
                sceneName = b.dataset.scene; clearSel();
                document.getElementById('so-scrub').style.display = scenes[sceneName].scrub ? '' : 'none';
                scenes[sceneName].init();
            });
        });
        document.getElementById('so-play').addEventListener('click', function () {
            playing = !playing; this.textContent = playing ? 'Pause' : 'Play';
        });
        document.querySelectorAll('.so-spd').forEach(function (b) {
            b.addEventListener('click', function () {
                document.querySelectorAll('.so-spd').forEach(function (x) { x.classList.remove('active'); });
                b.classList.add('active'); speed = parseFloat(b.dataset.s);
            });
        });
        document.getElementById('so-scrub').addEventListener('input', function () {
            if (scenes[sceneName].setScrub) scenes[sceneName].setScrub(this.value / 100);
        });
        document.getElementById('so-wm').addEventListener('click', function () {
            wmOn = !wmOn; this.classList.toggle('active', wmOn);
        });
        document.getElementById('so-rec').addEventListener('click', function () {
            const len = parseInt(document.getElementById('so-reclen').value, 10) || 30;
            startRec(len, this);
        });
        cv.addEventListener('click', function (e) {
            const r = cv.getBoundingClientRect();
            const n = scenes[sceneName].pick(e.clientX - r.left, e.clientY - r.top);
            if (!n || (sel && sel.id === n.id)) { clearSel(); return; }
            sel = n; showCard(n);
        });
        document.getElementById('so-scrub').style.display = scenes[sceneName].scrub ? '' : 'none';
    })();
    </script>
</body>
</html>
"""
