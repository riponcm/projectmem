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
        .replace("{{GRAPH_DATA}}", json.dumps(graph_data))
        .replace("{{PROJECT_MAP}}", json.dumps(project_map_text))
        .replace("{{PROJECT_MAP_GRAPH}}", json.dumps(project_map_graph))
        .replace("{{TIMELINE_DATA}}", json.dumps(timeline_data))
        .replace("{{SCORE_DATA}}", json.dumps(score_data))
        .replace("{{PROJECT_NAME}}", json.dumps(project_name))
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
        .side-ft {
            margin-top: auto; padding: 12px; background: #0E3157; border-radius: 10px;
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
                 transform:translateX(-50%); box-shadow:0 1px 2px rgba(16,47,82,.18); }
        .ov-axis { display:flex; justify-content:space-between; margin:6px 0 0 80px; font:10.5px ui-monospace,Menlo,monospace; color:var(--text-muted); }
        .ov-foot { display:flex; gap:16px; align-items:center; margin-top:12px; font-size:11px; color:var(--text-dim); flex-wrap:wrap; }
        @media (max-width:1080px){ .ov-grid{ grid-template-columns:1fr; } }

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

              <!-- 3. Project Map: mini graph -->
              <section class="ov-card">
                <div class="ov-ph"><span class="ov-tag" style="background:var(--primary)"><svg viewBox="0 0 24 24"><circle cx="12" cy="5" r="2.6"/><circle cx="5" cy="19" r="2.6"/><circle cx="19" cy="19" r="2.6"/><line x1="12" y1="7.5" x2="5.8" y2="16.6"/><line x1="12" y1="7.5" x2="18.2" y2="16.6"/></svg></span>
                  <h2>Project Map</h2><span class="ov-d">structure</span>
                  <span class="ov-jump" data-go="map">open ↗</span></div>
                <div class="ov-psub">Repo structure as a graph — node size = activity, red ring = files with failures.</div>
                <div class="ov-mapwrap"><svg id="ov-map" width="100%" height="232"></svg></div>
              </section>

              <!-- 4. Timeline: swimlanes -->
              <section class="ov-card">
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
        <div class="panel" id="panel-story">
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
                        <button class="so-scn active" data-scene="replay">Story Replay</button>
                        <button class="so-scn" data-scene="orbit">Orbit</button>
                        <button class="so-scn" data-scene="universe">Universe</button>
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
    document.querySelectorAll('.ov-jump').forEach(j => j.addEventListener('click', () => activateTab(j.dataset.go)));

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
            return '<div class="ov-row"><div class="fn" title="'+d.f+'">'+short+'</div>'
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

    // ── 3. Project Map: compact node graph ──
    (function ovMap() {
        const svgEl = document.getElementById('ov-map');
        const W = Math.max(svgEl.getBoundingClientRect().width || 540, 300), H = 232, pad = 26;
        if (!projectMapGraph.nodes.length) {
            svgEl.innerHTML = '<text x="'+(W/2)+'" y="'+(H/2)+'" text-anchor="middle" fill="#8A99AD" font-size="12.5">Run <tspan font-family="monospace" fill="#5A6B82">pjm map</tspan> to generate a project map.</text>';
            return;
        }
        const failByFile = {}; data.nodes.forEach(n => { if (n.type==='file' && n.failures>0) failByFile[n.id]=n.failures; });
        const deg = {}; projectMapGraph.links.forEach(l => { const s=l.source.id||l.source, t=l.target.id||l.target; deg[s]=(deg[s]||0)+1; deg[t]=(deg[t]||0)+1; });
        const cap = [...projectMapGraph.nodes].sort((a,b)=>(deg[b.id]||0)-(deg[a.id]||0)).slice(0,10);
        const capIds = new Set(cap.map(n=>n.id));
        const mlinks = projectMapGraph.links
            .map(l => ({ source:l.source.id||l.source, target:l.target.id||l.target }))
            .filter(l => capIds.has(l.source) && capIds.has(l.target));
        const sim = d3.forceSimulation(cap)
            .force('charge', d3.forceManyBody().strength(-210))
            .force('link', d3.forceLink(mlinks).id(d=>d.id).distance(52))
            .force('center', d3.forceCenter(W/2, H/2))
            .force('collide', d3.forceCollide(24))
            .stop();
        for (let i=0;i<200;i++) sim.tick();
        cap.forEach(n => { n.x=Math.max(pad,Math.min(W-pad,n.x)); n.y=Math.max(pad,Math.min(H-pad,n.y)); });
        const R = n => n.type==='folder' ? 13 : 9;
        let s = '';
        mlinks.forEach(l => { const a=cap.find(n=>n.id===l.source), b=cap.find(n=>n.id===l.target); if(a&&b) s+='<line x1="'+a.x+'" y1="'+a.y+'" x2="'+b.x+'" y2="'+b.y+'" stroke="#CBD7E6" stroke-width="2"/>'; });
        cap.forEach(n => {
            if (failByFile[n.id]) s += '<circle cx="'+n.x+'" cy="'+n.y+'" r="'+(R(n)+5)+'" fill="none" stroke="#E8593B" stroke-width="2.5" stroke-dasharray="3 3"/>';
            s += '<circle cx="'+n.x+'" cy="'+n.y+'" r="'+R(n)+'" fill="'+(n.type==='folder'?'#1F6FEB':'#169F84')+'"/>';
            const lbl = (n.label||'').slice(0,16);
            s += '<text class="ovn-label" x="'+n.x+'" y="'+(n.y+R(n)+13)+'" text-anchor="middle">'+lbl+'</text>';
        });
        svgEl.innerHTML = s;
    })();

    // ── 4. Timeline: swimlanes ──
    (function ovTimeline() {
        const lanes = [
            { key:'issue',    c:'#E8593B' },
            { key:'attempt',  c:'#E8A33B' },
            { key:'fix',      c:'#169F84' },
            { key:'decision', c:'#1F6FEB' },
        ];
        const dated = timelineData.map(e => ({ type:e.type, t:new Date(e.timestamp).getTime() }))
            .filter(e => !isNaN(e.t));
        const wrap = document.getElementById('ov-timeline');
        if (!dated.length) { wrap.innerHTML = '<div class="ov-empty">No dated events yet.</div>'; document.getElementById('ov-foot').textContent=''; return; }
        const tMin = Math.min(...dated.map(e=>e.t)), tMax = Math.max(...dated.map(e=>e.t));
        const span = Math.max(tMax - tMin, 1);
        const xOf = t => 2 + ((t - tMin)/span)*96;
        wrap.innerHTML = lanes.map(L => {
            let pts = dated.filter(e => e.type === L.key).map(e => xOf(e.t));
            if (pts.length > 46) { const step=Math.ceil(pts.length/46); pts = pts.filter((_,i)=>i%step===0); }
            const dots = pts.map(x => '<span class="ov-ev" style="left:'+x+'%;background:'+L.c+'"></span>').join('');
            return '<div class="ov-lane"><div class="ln" style="color:'+L.c+'">'+L.key+'</div><div class="ov-track">'+dots+'</div></div>';
        }).join('');
        // axis: 6 evenly spaced dates
        const fmt = ms => new Date(ms).toLocaleDateString('en-US',{month:'short', day:'numeric'});
        const ticks = []; for (let i=0;i<6;i++) ticks.push(fmt(tMin + span*i/5));
        document.getElementById('ov-axis').innerHTML = ticks.map(t => '<span>'+t+'</span>').join('');
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
            const details = d.summary || d.full_path || d.path || d.id || '';
            const count = d.event_count ? '<br/><span style="color:var(--primary)">'+d.event_count+' attached events</span>' : '';
            const failures = d.failure_count ? '<br/><span style="color:var(--error)">'+d.failure_count+' failed attempts</span>' : '';
            const outcome = d.outcome ? '<br/><span style="color:'+(d.outcome==='failed'?'var(--error)':'var(--success)')+'">Outcome: '+d.outcome+'</span>' : '';
            const loc = d.location ? '<br/><span style="color:var(--accent)">@ '+d.location+'</span>' : '';
            tt.innerHTML = '<strong>'+typeLabel+': '+(d.display_label || d.label || d.id)+'</strong><br/>'+details+count+failures+outcome+loc;
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
            srcLegend.innerHTML += '<div class="roi-donut-item"><div class="roi-donut-dot" style="background:'+(srcColors[key]||'#64748b')+'"></div><div class="roi-donut-name">'+(srcLabels[key]||key)+'</div><div class="roi-donut-val">'+pct+'%</div></div>';
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
            row.innerHTML = '<div class="churn-file" title="'+file+'">'+shortFile+'</div><div class="churn-bar-track"><div class="churn-bar-fill '+severity+'" style="width:0%"></div></div><div class="churn-count">'+count+'</div><div class="churn-severity '+severity+'">'+severity+'</div>';
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
        // Pull fenced ``` blocks out FIRST so their backticks/indentation don't collide
        // with the inline-code and paragraph rules below (they'd render as empty <code>
        // boxes + a stray trailing ``). Restored as <pre> at the end.
        const fences = [];
        md = md.replace(/```[\\w-]*\\n?([\\s\\S]*?)```/g, function (_, code) {
            const esc = code.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
            fences.push('<pre class="md-pre">' + esc.replace(/\\n$/, '') + '</pre>');
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

        const mZoom = d3.zoom().scaleExtent([0.15,4]).on("zoom", e => mG.attr("transform",e.transform));
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
            tt.innerHTML = '<strong>'+d.type.toUpperCase()+'</strong><br/>'+d.full_path
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
            const xs = projectMapGraph.nodes.map(n=>n.x), ys = projectMapGraph.nodes.map(n=>n.y);
            if (!xs.length) return;
            const minx=Math.min(...xs), maxx=Math.max(...xs), miny=Math.min(...ys), maxy=Math.max(...ys);
            const w=(maxx-minx)||1, h=(maxy-miny)||1, pad=64;
            const k=Math.min((mW-2*pad)/w, (mH-2*pad)/h, 1.5);
            const tx=(mW-k*(minx+maxx))/2, ty=(mH-k*(miny+maxy))/2;
            mSvg.transition().duration(450).call(mZoom.transform, d3.zoomIdentity.translate(tx,ty).scale(k));
        }
        mSim.on("end", mFit);
        setTimeout(mFit, 1800);
    }

    // ══════════════════════════════════════════
    // TAB 3b: Project Map — Tree / Dendrogram view
    // ══════════════════════════════════════════
    function buildTreeData() {
        if (!projectMapGraph.nodes.length) return null;
        // Build a 3-level hierarchy: root → top-folder → leaf
        const root = { id: '__root__', label: 'project', children: [] };
        const groups = {};
        const palette = ['#3b82f6','#8b5cf6','#10b981','#f59e0b','#ec4899','#06b6d4','#84cc16','#ef4444','#a78bfa','#22d3ee','#fb7185','#4ade80'];
        let colorIdx = 0;

        projectMapGraph.nodes.forEach(n => {
            const id = n.id;
            const parts = id.split('/').filter(Boolean);
            const top = parts[0] || '(root)';
            if (!groups[top]) {
                groups[top] = { id: 'g_'+top, label: top, color: palette[(colorIdx++) % palette.length], children: [] };
                root.children.push(groups[top]);
            }
            // Only add leaf (file or last segment) — skip if it's the folder itself
            if (n.type !== 'folder' || parts.length > 1) {
                const leafLabel = n.label || parts[parts.length-1];
                groups[top].children.push({ id, label: leafLabel, color: groups[top].color, full: id });
            }
        });
        // Sort & cap each group at 25 leaves to avoid runaway trees
        Object.values(groups).forEach(g => {
            g.children.sort((a,b) => a.label.localeCompare(b.label));
            if (g.children.length > 25) {
                const more = g.children.length - 24;
                g.children = g.children.slice(0, 24);
                g.children.push({ id: 'more_'+g.label, label: `+${more} more`, color: g.color, more: true });
            }
        });
        root.children.sort((a,b) => a.label.localeCompare(b.label));
        return root;
    }

    function renderTree() {
        const treeData = buildTreeData();
        const tSvg = d3.select("#map-tree");
        tSvg.selectAll("*").remove();
        if (!treeData) {
            tSvg.append("text").attr("x","50%").attr("y","50%").attr("text-anchor","middle")
                .attr("fill","var(--text-muted)").attr("font-size","13px")
                .text("PROJECT_MAP.md is empty — no tree to render.");
            return;
        }
        const pane = document.querySelector('.map-graph-pane');
        const W = pane.clientWidth || 800;
        const H = pane.clientHeight || 600;
        const root = d3.hierarchy(treeData);
        const leafCount = root.leaves().length;
        // Vertical space per leaf, fixed min/max
        const innerH = Math.max(H - 40, leafCount * 22);
        const cluster = d3.cluster().size([innerH, W - 260]);
        cluster(root);

        tSvg.attr("viewBox", `0 0 ${W} ${innerH + 40}`)
            .style("cursor","grab");
        const zoomG = tSvg.append("g");
        const g = zoomG.append("g").attr("transform","translate(120,20)");

        // Zoom + pan
        const zoom = d3.zoom()
            .scaleExtent([0.3, 5])
            .on("zoom", (e) => zoomG.attr("transform", e.transform))
            .on("start", () => tSvg.style("cursor","grabbing"))
            .on("end", () => tSvg.style("cursor","grab"));
        tSvg.call(zoom).on("dblclick.zoom", () => tSvg.transition().duration(400).call(zoom.transform, d3.zoomIdentity));

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

        node.append("circle")
            .attr("class","tree-node-circle")
            .attr("r", d => d.depth === 0 ? 5 : d.depth === 1 ? 6 : 4)
            .attr("fill", d => d.data.color || (d.depth === 0 ? '#64748b' : 'var(--primary)'));

        node.append("text")
            .attr("class","tree-node-label")
            .attr("dy","0.32em")
            .attr("x", d => d.children ? -10 : 10)
            .attr("text-anchor", d => d.children ? "end" : "start")
            .attr("fill", d => d.data.more ? "var(--text-muted)" : "var(--text)")
            .style("font-style", d => d.data.more ? "italic" : "normal")
            .text(d => d.depth === 0 ? "" : d.data.label);

        // Tooltip on full paths
        node.on("mouseover", (e,d) => {
            if (!d.data.full) return;
            const tt = document.getElementById("tooltip");
            tt.style.opacity = 1;
            tt.innerHTML = '<strong>'+d.data.label+'</strong><br/>'+d.data.full;
            tt.style.left = (e.pageX + 14) + "px";
            tt.style.top = (e.pageY - 14) + "px";
        }).on("mouseout", () => { document.getElementById("tooltip").style.opacity = 0; });
    }

    // TAB 3c: Project Map — Flow view (layered left-to-right flowchart)
    // Same real data as the Story Map: structure + what happened, flowing into memory.
    // Directory filter selection persists across re-renders (null = show all).
    let flowSelectedDirs = null;

    function renderFlowFilter(host, dirNames, counts) {
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
        const allOn = flowSelectedDirs.size === dirNames.length;
        mkRow('All', total, allOn, () => { flowSelectedDirs = allOn ? new Set() : new Set(dirNames); renderMapFlow(); }, null, true);
        dirNames.forEach(d => {
            const on = flowSelectedDirs.has(d);
            mkRow(d.length > 18 ? '…' + d.slice(-17) : d, counts[d], on,
                () => { on ? flowSelectedDirs.delete(d) : flowSelectedDirs.add(d); renderMapFlow(); },
                () => { flowSelectedDirs = new Set([d]); renderMapFlow(); });
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
        if (allDirNames.length > 1) renderFlowFilter(host, allDirNames, allDirCounts);
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
        // per-file activity chips from the events connected to each file
        const evById = {};
        data.nodes.forEach(n => { if (n.type === 'event') evById[n.id] = n; });
        const chipStats = {};
        fileNodes.forEach(f => { chipStats[f.id] = { failed: 0, fixed: 0, decisions: 0, notes: 0 }; });
        data.links.forEach(l => {
            const s = (l.source && l.source.id) || l.source, t = (l.target && l.target.id) || l.target;
            const ev = evById[s] || evById[t];
            const fid = evById[s] ? t : s;
            if (!ev || !chipStats[fid]) return;
            if (ev.event_type === 'attempt' && ev.outcome === 'failed') chipStats[fid].failed++;
            else if (ev.event_type === 'fix') chipStats[fid].fixed++;
            else if (ev.event_type === 'decision') chipStats[fid].decisions++;
            else if (ev.event_type === 'note') chipStats[fid].notes++;
        });
        // group the visible files by parent directory
        const dirs = {};
        fileNodes.forEach(f => { const d = dirOf(f); (dirs[d] = dirs[d] || []).push(f); });
        const dirNames = Object.keys(dirs).sort();
        const rowH = 62, dirX = 200, fileX = 400, actX = 620, memX = 830, WIDTH = 1010;
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
                link(dirX + 145, yc, fileX, y, hot);
                svg.append('rect').attr('x', fileX).attr('y', y - 21).attr('width', 160).attr('height', 42)
                    .attr('rx', 9).attr('fill', 'var(--surface)')
                    .attr('stroke', hot ? 'var(--error)' : 'var(--border-light)').attr('stroke-width', hot ? 1.8 : 1);
                svg.append('text').attr('x', fileX + 13).attr('y', y - 1)
                    .attr('font-size', 12).attr('font-weight', 600).attr('fill', 'var(--text)')
                    .text((f.label || f.id).length > 19 ? (f.label || f.id).slice(0, 18) + '…' : (f.label || f.id));
                svg.append('text').attr('x', fileX + 13).attr('y', y + 13)
                    .attr('font-size', 10).attr('fill', 'var(--text-dim)').text((f.event_count || 0) + ' events');
                const st = chipStats[f.id];
                const chips = [];
                if (st.failed) chips.push([st.failed + ' failed', '#E8593B']);
                if (st.fixed) chips.push([st.fixed + ' fixed', '#169F84']);
                if (st.decisions) chips.push([st.decisions + ' decisions', '#6366F1']);
                if (st.notes) chips.push([st.notes + ' notes', '#5A6B82']);
                link(fileX + 160, y, actX, y, hot);
                let cx = actX;
                chips.slice(0, 3).forEach(([txt, col]) => {
                    const w = txt.length * 6.2 + 18;
                    svg.append('rect').attr('x', cx).attr('y', y - 12).attr('width', w).attr('height', 24)
                        .attr('rx', 12).attr('fill', col + '15').attr('stroke', col);
                    svg.append('text').attr('x', cx + w / 2).attr('y', y + 4).attr('text-anchor', 'middle')
                        .attr('font-size', 10).attr('font-weight', 700).attr('fill', col).text(txt);
                    cx += w + 7;
                });
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
        btn.innerHTML = type + ' <span class="count">' + (typeCounts[type]||0) + '</span>';
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
                const outcomeLabel = e.outcome?' <span class="'+outcomeClass+'">['+e.outcome+']</span>':'';
                const loc = e.location?'<span style="color:var(--accent)"> @ '+e.location+'</span>':'';
                const iid = e.issue_id?'<span style="color:var(--text-muted)"> #'+e.issue_id+'</span>':'';
                const autoBadge = e.auto_captured?'<span class="tl-auto-badge">AUTO</span>':'';
                const ts = e.timestamp ? new Date(e.timestamp).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}) : '';
                const capSrc = e.auto_captured && e.capture_source ? '<span class="tl-capture-source"> &middot; '+(srcLabels[e.capture_source]||e.capture_source)+'</span>' : '';
                html += '<div class="tl-item"><div class="tl-badge '+e.type+'">'+e.type+'</div><div class="tl-body"><div class="tl-summary">'+e.summary+outcomeLabel+iid+autoBadge+'</div><div class="tl-meta">'+ts+loc+capSrc+'</div></div></div>';
            });
            html += '</div>';
        }
        if (sorted.length === 0) html = '<div style="text-align:center;color:var(--text-muted);padding:40px">No events match current filters</div>';
        listEl.innerHTML = html;
    }
    renderTimeline();

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
        let sceneName = 'replay', playing = true, speed = 1, sel = null, t0 = performance.now();
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
