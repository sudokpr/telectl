from __future__ import annotations

import html
import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class UsageConfig:
    enabled: bool
    db_path: Path
    retention_days: int


@dataclass(frozen=True)
class FeatureDefinition:
    key: str
    surface: str
    category: str
    label: str
    description: str


def _feature(key: str, surface: str, category: str, label: str, description: str) -> FeatureDefinition:
    return FeatureDefinition(key, surface, category, label, description)


COMMAND_ALIASES: dict[str, str] = {
    "cmd": "cmd", "commands": "cmd", "help": "cmd",
    "cxr": "cxr", "codex_start": "cxr",
    "cxs": "cxs", "codex_status": "cxs",
    "cxq": "cxq", "codex_stop": "cxq",
    "memq": "memq", "memory_query": "memq",
    "spq": "spq", "spending_query": "spq",
    "spi": "spi", "spending_index": "spi",
    "sph": "sph", "spending_help": "sph",
    "oth": "oth", "owntracks": "oth", "owntracks_help": "oth", "ot_help": "oth",
    "otd": "otd", "ot": "otd", "owntracks_digest": "otd", "ot_digest": "otd",
    "otm": "otm", "ot_map": "otm", "owntracks_map": "otm",
    "otme": "otme", "ot_map_embed": "otme", "owntracks_map_embed": "otme",
    "otb": "otb", "ot_names": "otb", "owntracks_names": "otb",
    "ott": "ott", "tag": "ott", "owntracks_tag": "ott", "ot_tag": "ott",
    "otn": "otn", "name": "otn", "owntracks_name": "otn", "ot_name": "otn",
    "oto": "oto", "note": "oto", "owntracks_note": "oto", "ot_note": "oto",
}


FEATURES: tuple[FeatureDefinition, ...] = (
    _feature("telegram.command.cmd", "telegram", "Commands", "/cmd", "List bot command shortcuts."),
    _feature("telegram.command.cxr", "telegram", "Codex remote control", "/cxr", "Restart Codex remote control."),
    _feature("telegram.command.cxs", "telegram", "Codex remote control", "/cxs", "Show Codex remote-control status."),
    _feature("telegram.command.cxq", "telegram", "Codex remote control", "/cxq", "Stop Codex remote control."),
    _feature("telegram.command.memq", "telegram", "Memory", "/memq", "Ask a question over saved memories."),
    _feature("telegram.command.spq", "telegram", "Spending", "/spq", "Ask the spending and receipt price index."),
    _feature("telegram.command.spi", "telegram", "Spending", "/spi", "Resync POI memories and spending correlations."),
    _feature("telegram.command.sph", "telegram", "Spending", "/sph", "Show spending and POI help."),
    _feature("telegram.command.otd", "telegram", "OwnTracks commands", "/otd", "Generate an OwnTracks activity digest."),
    _feature("telegram.command.otm", "telegram", "OwnTracks commands", "/otm", "Deliver the configured interactive OwnTracks map."),
    _feature("telegram.command.otme", "telegram", "OwnTracks commands", "/otme", "Generate a self-contained embedded OwnTracks map."),
    _feature("telegram.command.otb", "telegram", "OwnTracks commands", "/otb", "Bulk-save OwnTracks stop reviews."),
    _feature("telegram.command.ott", "telegram", "OwnTracks commands", "/ott", "Tag an OwnTracks stop."),
    _feature("telegram.command.otn", "telegram", "OwnTracks commands", "/otn", "Name an OwnTracks stop."),
    _feature("telegram.command.oto", "telegram", "OwnTracks commands", "/oto", "Add a note to an OwnTracks stop."),
    _feature("telegram.command.oth", "telegram", "OwnTracks commands", "/oth", "Show OwnTracks command help."),
    _feature("telegram.workflow.image_summary", "telegram", "Automatic workflows", "Image summary", "OCR and summarize an image posted in the image topic."),
    _feature("telegram.workflow.text_memory", "telegram", "Automatic workflows", "Text memory extraction", "Extract and save a memory from plain text."),
    _feature("telegram.workflow.memory_query_text", "telegram", "Automatic workflows", "Quick memory query", "Ask memory using ?, q:, or query: text syntax."),
    _feature("telegram.workflow.fuel_image", "telegram", "Fuel workflow", "Fuel image intake", "Extract a fuel receipt and odometer image set."),
    _feature("telegram.workflow.fuel_approve_full", "telegram", "Fuel workflow", "Approve full tank", "Approve and append a full-tank fuel row."),
    _feature("telegram.workflow.fuel_approve_partial", "telegram", "Fuel workflow", "Approve partial fill", "Approve and append a partial-fill fuel row."),
    _feature("telegram.workflow.fuel_correction", "telegram", "Fuel workflow", "Correct fuel entry", "Enter correction mode or submit corrected fields."),
    _feature("telegram.workflow.fuel_reject", "telegram", "Fuel workflow", "Reject fuel entry", "Reject a pending fuel extraction."),
    _feature("http.memory_intake", "http", "HTTP intake", "Memory intake", "Save memory text through POST /memory."),
    _feature("http.spending_index", "http", "HTTP intake", "Spending index API", "Refresh correlations through POST /spending/index."),
    _feature("http.spending_query", "http", "HTTP intake", "Spending query API", "Query spending through POST /spending/query."),
    _feature("owntracks.view.day_map", "owntracks", "Views", "Day map", "Open a hosted daily OwnTracks map."),
    _feature("owntracks.view.heat_map", "owntracks", "Views", "Heat map", "Open a monthly or yearly OwnTracks heatmap."),
    _feature("owntracks.view.stops", "owntracks", "Views", "Stop index", "Open the searchable stop and place index."),
    _feature("owntracks.view.trips", "owntracks", "Views", "Trip explorer", "Open point-to-point trip analysis."),
    _feature("owntracks.view.dashboard", "owntracks", "Views", "Activity dashboard", "Open the activity dashboard."),
    _feature("owntracks.view.sample", "owntracks", "Views", "Synthetic sample", "Open the synthetic heatmap test page."),
    _feature("owntracks.view.usage", "owntracks", "Views", "Feature usage", "Open this local feature-usage report."),
    _feature("owntracks.ui.navigation.day_map", "owntracks", "Navigation", "Navigate to day map", "Use the OwnTracks navigation bar to open a day map."),
    _feature("owntracks.ui.navigation.heat_map", "owntracks", "Navigation", "Navigate to heat map", "Use the OwnTracks navigation bar to open a heatmap."),
    _feature("owntracks.ui.navigation.stops", "owntracks", "Navigation", "Navigate to stops", "Use the OwnTracks navigation bar to open stops."),
    _feature("owntracks.ui.navigation.trips", "owntracks", "Navigation", "Navigate to trips", "Use the OwnTracks navigation bar to open trips."),
    _feature("owntracks.ui.navigation.dashboard", "owntracks", "Navigation", "Navigate to dashboard", "Use the OwnTracks navigation bar to open the dashboard."),
    _feature("owntracks.ui.navigation.usage", "owntracks", "Navigation", "Navigate to usage", "Use the OwnTracks navigation bar to open usage analytics."),
    _feature("owntracks.ui.day.prev_day", "owntracks", "Day map controls", "Previous day", "Move the daily map to the previous day."),
    _feature("owntracks.ui.day.today", "owntracks", "Day map controls", "Today", "Move the daily map to today."),
    _feature("owntracks.ui.day.next_day", "owntracks", "Day map controls", "Next day", "Move the daily map to the next day."),
    _feature("owntracks.ui.day.select_all", "owntracks", "Day map controls", "Select all stops", "Select all daily stops."),
    _feature("owntracks.ui.day.clear_selection", "owntracks", "Day map controls", "Clear stop selection", "Clear selected daily stops."),
    _feature("owntracks.ui.day.fit_all", "owntracks", "Day map controls", "Fit all", "Fit all map content in view."),
    _feature("owntracks.ui.day.center_selected", "owntracks", "Day map controls", "Center selected", "Center the map on selected stops."),
    _feature("owntracks.ui.day.toggle_tools", "owntracks", "Day map controls", "Toggle tools panel", "Show or hide the daily map tool panel."),
    _feature("owntracks.ui.day.toggle_edges", "owntracks", "Map layers", "Toggle route edges", "Show or hide route edges."),
    _feature("owntracks.ui.day.toggle_arrows", "owntracks", "Map layers", "Toggle arrows", "Show or hide route direction arrows."),
    _feature("owntracks.ui.day.toggle_travel_times", "owntracks", "Map layers", "Toggle travel times", "Show or hide travel-time labels."),
    _feature("owntracks.ui.day.toggle_stop_labels", "owntracks", "Map layers", "Toggle stop labels", "Show or hide stop labels."),
    _feature("owntracks.ui.day.toggle_place_labels", "owntracks", "Map layers", "Toggle transition points", "Show or hide OwnTracks transitions."),
    _feature("owntracks.ui.day.toggle_filtered_points", "owntracks", "Map layers", "Toggle filtered points", "Show or hide visualization-filtered route points."),
    _feature("owntracks.ui.day.toggle_pois", "owntracks", "Map layers", "Toggle POIs", "Show or hide captured POIs."),
    _feature("owntracks.ui.day.toggle_possible_stops", "owntracks", "Map layers", "Toggle possible stops", "Show or hide possible missed stops."),
    _feature("owntracks.ui.day.poi_position_source", "owntracks", "Map layers", "Change POI positioning", "Choose how captured POIs are positioned on the route."),
    _feature("owntracks.ui.day.add_missing_stop", "owntracks", "Stop review", "Add missing stop", "Start adding a missing stop."),
    _feature("owntracks.ui.day.save_changes", "owntracks", "Stop review", "Save visit changes", "Save edited stop reviews."),
    _feature("owntracks.ui.day.apply_name", "owntracks", "Stop review", "Apply name", "Apply a name to selected stops."),
    _feature("owntracks.ui.day.select_nearby", "owntracks", "Stop review", "Select nearby", "Select stops near the active stop."),
    _feature("owntracks.ui.day.group_selected", "owntracks", "Stop review", "Group selected", "Group selected visits into one place."),
    _feature("owntracks.ui.day.copy_commands", "owntracks", "Stop review", "Copy Telegram commands", "Copy generated stop-review commands."),
    _feature("owntracks.ui.day.upload_media", "owntracks", "Media", "Attach map media", "Attach media from the daily map."),
    _feature("owntracks.ui.day.delete_media", "owntracks", "Media", "Delete map media", "Delete an attachment from the daily map."),
    _feature("owntracks.ui.day.save_stop", "owntracks", "Stop review", "Save stop popup", "Save a stop from its map popup."),
    _feature("owntracks.ui.day.dismiss_stop", "owntracks", "Stop review", "Dismiss visit", "Dismiss a candidate stop visit."),
    _feature("owntracks.ui.day.resolve_stop", "owntracks", "Stop review", "Resolve place", "Request place suggestions for a stop."),
    _feature("owntracks.ui.day.adjust_stop_location", "owntracks", "Stop review", "Adjust stop location", "Adjust the saved location of a stop."),
    _feature("owntracks.ui.day.mark_manual_stop", "owntracks", "Stop review", "Save manual visit", "Save a route point as a manual visit."),
    _feature("owntracks.ui.day.save_manual_placement", "owntracks", "Stop review", "Save placed visit", "Save a manually placed visit."),
    _feature("owntracks.ui.day.use_place_candidate", "owntracks", "Stop review", "Use place suggestion", "Apply a resolved place suggestion."),
    _feature("owntracks.ui.day.duration_start", "owntracks", "Route analysis", "Set duration start", "Choose a point as the elapsed-time start."),
    _feature("owntracks.ui.day.duration_end", "owntracks", "Route analysis", "Set duration end", "Choose a point as the elapsed-time end."),
    _feature("owntracks.ui.day.duration_clear", "owntracks", "Route analysis", "Clear duration", "Clear selected elapsed-time anchors."),
    _feature("owntracks.ui.day.possible_stop", "owntracks", "Route analysis", "Open possible stop", "Open a possible missed-stop suggestion."),
    _feature("owntracks.ui.day.segment_index", "owntracks", "Route analysis", "Open ride segment", "Open a detected ride segment."),
    _feature("owntracks.ui.day.route_anim_play", "owntracks", "Route analysis", "Play route animation", "Animate the daily route."),
    _feature("owntracks.ui.day.route_anim_reset", "owntracks", "Route analysis", "Reset route animation", "Reset the daily route animation."),
    _feature("owntracks.ui.day.route_motion", "owntracks", "Route analysis", "Motion colors", "Color the route by motion mode."),
    _feature("owntracks.ui.day.route_speed", "owntracks", "Route analysis", "Speed colors", "Color the route by speed."),
    _feature("owntracks.ui.day.route_elevation_bands", "owntracks", "Route analysis", "Elevation bands", "Color the route using elevation bands."),
    _feature("owntracks.ui.day.route_elevation_slope", "owntracks", "Route analysis", "Ascent/descent", "Color the route by ascent and descent."),
    _feature("owntracks.ui.day.profile_distance", "owntracks", "Route analysis", "Distance profile", "Display the elevation profile by distance."),
    _feature("owntracks.ui.day.profile_time", "owntracks", "Route analysis", "Time profile", "Display the elevation profile by time."),
    _feature("owntracks.ui.stops.apply_range", "owntracks", "Stop index", "Apply stop range", "Apply date filters in the stop index."),
    _feature("owntracks.ui.stops.refresh_aliases", "owntracks", "Stop index", "Refresh search aliases", "Generate deterministic search aliases with Codex."),
    _feature("owntracks.ui.stops.search", "owntracks", "Stop index", "Search stops", "Use local deterministic stop search (query text is not recorded)."),
    _feature("owntracks.ui.stops.select_place", "owntracks", "Stop index", "Select place", "Open a place and its visits."),
    _feature("owntracks.ui.stops.day_map", "owntracks", "Stop index", "Open visit day map", "Open a visit on its daily map."),
    _feature("owntracks.ui.stops.attach_media", "owntracks", "Media", "Attach media", "Attach media to a stop visit."),
    _feature("owntracks.ui.stops.delete_media", "owntracks", "Media", "Delete media", "Delete a stop attachment."),
    _feature("owntracks.ui.dashboard.apply_range", "owntracks", "Dashboard", "Apply dashboard range", "Apply dashboard date and home-radius filters."),
    _feature("owntracks.ui.dashboard.filter_toggle", "owntracks", "Dashboard", "Toggle dashboard filters", "Show or hide dashboard filters on mobile."),
    _feature("owntracks.ui.dashboard.range_preset", "owntracks", "Dashboard", "Select range preset", "Choose a dashboard date-range preset."),
    _feature("owntracks.ui.dashboard.metric", "owntracks", "Dashboard", "Change calendar metric", "Switch dashboard calendar values among time, distance, and visits."),
    _feature("owntracks.ui.dashboard.travel_split", "owntracks", "Dashboard", "Toggle travel split", "Separate travel days in dashboard summaries."),
    _feature("owntracks.ui.dashboard.day_map", "owntracks", "Dashboard", "Open dashboard day", "Open a dashboard day on the map."),
    _feature("owntracks.ui.trips.run", "owntracks", "Trips", "Run trip search", "Run point-to-point trip analysis."),
    _feature("owntracks.ui.trips.open_day_map", "owntracks", "Trips", "Open trip day map", "Open the selected trip date on the day map."),
    _feature("owntracks.ui.heat.heat_metric", "owntracks", "Heatmap", "Change heat metric", "Switch among time spent, visits, and raw points."),
    _feature("owntracks.ui.heat.motion_mode", "owntracks", "Heatmap", "Filter heatmap motion", "Filter heatmap points by motion mode."),
    _feature("owntracks.ui.heat.toggle_points", "owntracks", "Heatmap", "Toggle raw points", "Show or hide individual heatmap points."),
    _feature("owntracks.ui.heat.toggle_panel", "owntracks", "Heatmap", "Toggle heatmap panel", "Show or hide heatmap controls."),
    _feature("owntracks.ui.day.motion_mode", "owntracks", "Day map controls", "Filter route motion", "Filter the daily route by motion mode."),
)


def build_usage_config(env: dict[str, str], base_dir: Path) -> UsageConfig:
    enabled = env.get("FEATURE_USAGE_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    raw_path = Path(env.get("FEATURE_USAGE_DB_PATH", "./data/usage/usage.sqlite")).expanduser()
    path = raw_path if raw_path.is_absolute() else base_dir / raw_path
    retention = max(1, int(env.get("FEATURE_USAGE_RETENTION_DAYS", "365")))
    return UsageConfig(enabled=enabled, db_path=path, retention_days=retention)


def canonical_command(raw_text: str) -> str | None:
    match = re.match(r"^/([A-Za-z0-9_]+)(?:@[A-Za-z0-9_]+)?(?:\s|$)", raw_text.strip())
    return COMMAND_ALIASES.get(match.group(1).lower()) if match else None


class UsageAnalytics:
    def __init__(self, config: UsageConfig):
        self.config = config
        self._init_lock = threading.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        self.config.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.config.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        if not self.config.enabled or self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            with self._connect() as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("CREATE TABLE IF NOT EXISTS usage_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                db.execute(
                    "CREATE TABLE IF NOT EXISTS usage_events ("
                    "id INTEGER PRIMARY KEY, occurred_at TEXT NOT NULL, feature TEXT NOT NULL, "
                    "surface TEXT NOT NULL, action TEXT NOT NULL)"
                )
                db.execute("CREATE INDEX IF NOT EXISTS usage_events_time ON usage_events(occurred_at)")
                db.execute("CREATE INDEX IF NOT EXISTS usage_events_feature ON usage_events(feature)")
                db.execute(
                    "CREATE TABLE IF NOT EXISTS usage_totals ("
                    "feature TEXT PRIMARY KEY, surface TEXT NOT NULL, count INTEGER NOT NULL, "
                    "first_used TEXT NOT NULL, last_used TEXT NOT NULL)"
                )
                db.execute(
                    "INSERT OR IGNORE INTO usage_totals(feature, surface, count, first_used, last_used) "
                    "SELECT feature, MIN(surface), COUNT(*), MIN(occurred_at), MAX(occurred_at) "
                    "FROM usage_events GROUP BY feature"
                )
                db.execute(
                    "INSERT OR IGNORE INTO usage_meta(key, value) VALUES ('tracking_started_at', ?)",
                    (datetime.now(timezone.utc).isoformat(),),
                )
            self._initialized = True

    def record(self, feature: str, surface: str, action: str = "use") -> bool:
        if not self.config.enabled or not re.fullmatch(r"[a-z0-9_.:-]{1,160}", feature):
            return False
        self._initialize()
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=self.config.retention_days)
        with self._connect() as db:
            db.execute(
                "INSERT INTO usage_events(occurred_at, feature, surface, action) VALUES (?, ?, ?, ?)",
                (now.isoformat(), feature, surface[:32], action[:32]),
            )
            db.execute(
                "INSERT INTO usage_totals(feature, surface, count, first_used, last_used) VALUES (?, ?, 1, ?, ?) "
                "ON CONFLICT(feature) DO UPDATE SET count=count+1, last_used=excluded.last_used",
                (feature, surface[:32], now.isoformat(), now.isoformat()),
            )
            db.execute("DELETE FROM usage_events WHERE occurred_at < ?", (cutoff.isoformat(),))
        return True

    def prometheus_snapshot(self, definitions: Iterable[FeatureDefinition] = FEATURES) -> dict[str, Any]:
        """Return lifetime counters plus registered zero-use features for a scrape."""
        if not self.config.enabled:
            return {"enabled": False, "tracking_started_at": None, "features": []}
        self._initialize()
        with self._connect() as db:
            started_row = db.execute("SELECT value FROM usage_meta WHERE key='tracking_started_at'").fetchone()
            rows = db.execute(
                "SELECT feature, surface, count, first_used, last_used FROM usage_totals"
            ).fetchall()
        recorded = {row["feature"]: dict(row) for row in rows}
        definitions_by_key = {item.key: item for item in definitions}
        unknown = [
            FeatureDefinition(key, str(row["surface"]), "Other recorded controls", key, "Automatically discovered UI interaction.")
            for key, row in recorded.items() if key not in definitions_by_key
        ]
        features = []
        for definition in (*tuple(definitions), *unknown):
            row = recorded.get(definition.key, {})
            features.append({
                "feature": definition.key,
                "surface": definition.surface,
                "category": definition.category,
                "count": int(row.get("count") or 0),
                "first_used": row.get("first_used"),
                "last_used": row.get("last_used"),
            })
        return {
            "enabled": True,
            "tracking_started_at": started_row["value"] if started_row else None,
            "features": features,
        }

    def summary(self, days: int = 30, definitions: Iterable[FeatureDefinition] = FEATURES) -> dict[str, Any]:
        days = min(3650, max(1, days))
        if not self.config.enabled:
            return {"enabled": False, "days": days, "features": [], "totals": {"events": 0, "used": 0, "never_used": 0}}
        self._initialize()
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=days)
        with self._connect() as db:
            started_row = db.execute("SELECT value FROM usage_meta WHERE key='tracking_started_at'").fetchone()
            rows = db.execute(
                "SELECT feature, surface, COUNT(*) count, MIN(occurred_at) first_used, MAX(occurred_at) last_used "
                "FROM usage_events WHERE occurred_at >= ? GROUP BY feature, surface",
                (cutoff.isoformat(),),
            ).fetchall()
        recorded = {row["feature"]: dict(row) for row in rows}
        definitions_by_key = {item.key: item for item in definitions}
        unknown = [
            FeatureDefinition(key, str(row["surface"]), "Other recorded controls", key.rsplit(".", 1)[-1].replace("_", " ").title(), "Automatically discovered UI interaction.")
            for key, row in recorded.items() if key not in definitions_by_key
        ]
        items = []
        for definition in (*tuple(definitions), *unknown):
            row = recorded.get(definition.key, {})
            count = int(row.get("count") or 0)
            items.append({
                "key": definition.key, "surface": definition.surface, "category": definition.category,
                "label": definition.label, "description": definition.description, "count": count,
                "first_used": row.get("first_used"), "last_used": row.get("last_used"),
                "status": "never" if count == 0 else ("often" if count >= 10 else "used"),
            })
        items.sort(key=lambda item: (-item["count"], item["surface"], item["category"], item["label"]))
        started = datetime.fromisoformat(started_row["value"]) if started_row else now
        tracking_days = max(0.0, (now - started).total_seconds() / 86400)
        return {
            "enabled": True, "days": days, "tracking_started_at": started.isoformat(),
            "tracking_days": round(tracking_days, 1), "retention_days": self.config.retention_days,
            "features": items,
            "totals": {"events": sum(item["count"] for item in items), "used": sum(item["count"] > 0 for item in items), "never_used": sum(item["count"] == 0 for item in items)},
        }


def render_usage_html(summary: dict[str, Any]) -> str:
    features = summary.get("features") or []
    rows = "".join(
        "<tr>"
        f"<td><strong>{html.escape(str(item['label']))}</strong><small>{html.escape(str(item['description']))}</small></td>"
        f"<td>{html.escape(str(item['surface']).title())}</td><td>{html.escape(str(item['category']))}</td>"
        f"<td class='number'>{item['count']}</td><td>{html.escape(str(item['last_used'] or 'Never')[:19].replace('T', ' '))}</td>"
        f"<td><span class='status {item['status']}'>{'Never used' if item['status'] == 'never' else item['status'].title()}</span></td>"
        "</tr>" for item in features
    )
    totals = summary.get("totals") or {}
    days = int(summary.get("days") or 30)
    started = str(summary.get("tracking_started_at") or "")[:10]
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Feature usage</title><style>
:root {{ color-scheme:light dark; font-family:system-ui,sans-serif; }} body {{ margin:0; background:#f3f5f7; color:#17202a; }}
main {{ max-width:1180px; margin:auto; padding:20px; }} h1 {{ margin-bottom:4px; }} .muted, small {{ color:#637083; }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin:18px 0; }}
.card, .panel {{ background:white; border:1px solid #dce2e8; border-radius:12px; padding:14px; }} .card strong {{ display:block; font-size:1.7rem; }}
form {{ display:flex; gap:8px; align-items:end; flex-wrap:wrap; margin:12px 0; }} input,button {{ padding:9px; border:1px solid #aeb8c2; border-radius:8px; }} button {{ background:#1769aa; color:white; }}
.table-wrap {{ overflow:auto; }} table {{ width:100%; border-collapse:collapse; }} th,td {{ padding:10px; border-bottom:1px solid #e2e7ec; text-align:left; vertical-align:top; }} th {{ position:sticky; top:0; background:white; }} small {{ display:block; max-width:440px; margin-top:3px; }} .number {{ font-weight:700; text-align:right; }}
.status {{ border-radius:999px; padding:3px 8px; background:#dcefe2; white-space:nowrap; }} .status.never {{ background:#f4d9d6; }} .status.often {{ background:#d9e8fa; }}
nav a {{ margin-right:12px; }} @media(prefers-color-scheme:dark) {{ body {{ background:#111820; color:#e5edf5; }} .card,.panel,th {{ background:#1b2530; border-color:#344250; }} th,td {{ border-color:#344250; }} .muted,small {{ color:#aab8c6; }} }}
</style></head><body><main>
<nav><a href="/owntracks/map/today">Day map</a><a href="/owntracks/stops">Stops</a><a href="/owntracks/trips">Trips</a><a href="/owntracks/dashboard">Dashboard</a></nav>
<h1>Feature usage</h1><p class="muted">Local, content-free analytics since {html.escape(started)}. Counts cover the selected {days}-day window.</p>
<div class="cards"><div class="card"><span>Interactions</span><strong>{totals.get('events', 0)}</strong></div><div class="card"><span>Features used</span><strong>{totals.get('used', 0)}</strong></div><div class="card"><span>Never used</span><strong>{totals.get('never_used', 0)}</strong></div><div class="card"><span>Tracking age</span><strong>{summary.get('tracking_days', 0)}d</strong></div></div>
<div class="panel"><strong>Removal guidance</strong><p class="muted">Treat zero-use features as review candidates only after at least 30 representative days. Before removal, consider seasonal use, emergency/admin value, and whether the feature is reached automatically rather than clicked.</p>
<form method="get"><label>Window (days)<br><input name="days" type="number" min="1" max="3650" value="{days}"></label><button type="submit">Apply</button></form>
<div class="table-wrap"><table><thead><tr><th>Feature</th><th>Surface</th><th>Category</th><th>Uses</th><th>Last used (UTC)</th><th>Status</th></tr></thead><tbody>{rows}</tbody></table></div></div>
</main></body></html>"""


def inject_usage_tracker(document: str, view: str) -> str:
    if "</body>" not in document.lower():
        return document
    safe_view = re.sub(r"[^a-z0-9_]+", "_", view.lower()).strip("_") or "page"
    script = f"""<script data-feature-usage>
(() => {{
  const view = {json.dumps(safe_view)};
  const token = new URLSearchParams(location.search).get("token") || "";
  const endpoint = "/usage/events" + (token ? "?token=" + encodeURIComponent(token) : "");
  if (token) {{
    document.querySelectorAll('a[href^="/owntracks/"],a[href^="/usage"]').forEach((link) => {{
      const url = new URL(link.getAttribute("href"), location.origin); url.searchParams.set("token", token);
      link.setAttribute("href", url.pathname + url.search + url.hash);
    }});
    document.querySelectorAll('form[method="get"],form:not([method])').forEach((form) => {{
      if (!form.querySelector('input[name="token"]')) {{
        const input = document.createElement("input"); input.type = "hidden"; input.name = "token"; input.value = token; form.appendChild(input);
      }}
    }});
  }}
  const snake = (value) => String(value || "").replace(/([a-z0-9])([A-Z])/g, "$1_$2").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 70);
  const aliases = {{rangeForm:"apply_range", tripForm:"run", toggleHeatmapPoints:"toggle_points", toggleHeatmapPanel:"toggle_panel", key:"select_place"}};
  const send = (feature, action="click") => {{
    if (!feature) return;
    const body = JSON.stringify({{feature, surface:"owntracks", action}});
    if (navigator.sendBeacon) {{ const blob = new Blob([body], {{type:"application/json"}}); navigator.sendBeacon(endpoint, blob); }}
    else fetch(endpoint, {{method:"POST", headers:{{"Content-Type":"application/json"}}, body, keepalive:true}}).catch(() => {{}});
  }};
  const navFeature = (href) => {{
    const path = new URL(href, location.href).pathname;
    if (path.includes("/usage")) return "usage";
    if (path.includes("/stops")) return "stops";
    if (path.includes("/trips")) return "trips";
    if (path.includes("/dashboard")) return "dashboard";
    if (path.includes("/map/")) return /^\\/owntracks\\/map\\/\\d{{4}}(?:-\\d{{1,2}})?$/.test(path) ? "heat_map" : "day_map";
    return "";
  }};
  document.addEventListener("click", (event) => {{
    const el = event.target.closest("button,a,input[type=button],input[type=submit]"); if (!el) return;
    if (el.closest(".ot-nav") || (el.tagName === "A" && el.getAttribute("href")?.startsWith("/owntracks/"))) {{
      const target = navFeature(el.href || el.getAttribute("href")); if (target) return send(`owntracks.ui.navigation.${{target}}`);
    }}
    let control = el.dataset.usageFeature || el.id;
    const dataKeys = ["heatMetric","motionMode","mode","uploadMedia","deleteMedia","saveStop","dismissStop","resolveStop","adjustStopLocation","possibleStop","markManualStop","saveManualPlacement","usePlaceCandidate","durationStart","durationEnd","durationClear","segmentIndex","key"];
    if (!control) {{ const key = dataKeys.find((item) => el.dataset[item] !== undefined); if (key) control = key; }}
    if (!control && el.type === "submit") control = el.form?.id || "apply_range";
    if (!control) control = `${{el.tagName.toLowerCase()}}_${{el.type || "link"}}`;
    send(`owntracks.ui.${{view}}.${{aliases[control] || snake(control)}}`);
  }}, true);
  let searchSent = false;
  document.addEventListener("input", (event) => {{
    if (view === "stops" && event.target.matches('input[type="search"],#search')) {{
      if (!searchSent) {{ searchSent = true; send("owntracks.ui.stops.search", "input"); }}
    }}
  }}, true);
  document.addEventListener("change", (event) => {{
    const control = event.target?.id || event.target?.name;
    if (control && !(view === "stops" && control === "search")) send(`owntracks.ui.${{view}}.${{aliases[control] || snake(control)}}`, "change");
  }}, true);
}})();</script>"""
    return re.sub(r"</body>", lambda _match: script + "</body>", document, count=1, flags=re.IGNORECASE)
