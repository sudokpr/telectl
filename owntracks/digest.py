from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .env import env_int, load_env, project_path
from .tagger import (
    build_geojson,
    build_heatmap_summary,
    build_plan,
    build_sample_heatmap_summary,
    load_user_tags,
    parse_log,
    render_digest,
    render_heatmap_html,
    render_leaflet_map_html,
    render_map_html,
    target_date_from_text,
    target_scope_from_text,
)
from .telegram_send import send_telegram_message


def load_send_env() -> dict[str, str]:
    env = load_env()
    fallback_path = env.get("TELEGRAM_ENV_PATH") or env.get("JOURGRAM_ENV_PATH")
    if not fallback_path:
        return env
    fallback_env = load_env(Path(fallback_path).expanduser())
    merged = fallback_env.copy()
    for key, value in env.items():
        if value or key not in merged:
            merged[key] = value
    return merged


def generate_digest(date_text: str | None = None) -> tuple[dict, str, Path]:
    env = load_env()
    local_tz = ZoneInfo(env.get("OWNTRACKS_TIMEZONE", "Asia/Kolkata"))
    target_date = target_date_from_text(date_text, local_tz)
    log_path = project_path(env.get("OWNTRACKS_LOG_PATH"), "./data/owntracks/mqtt.log")
    derived_dir = project_path(env.get("OWNTRACKS_DERIVED_DIR"), "./data/owntracks/derived")
    tags_path = project_path(env.get("OWNTRACKS_USER_TAGS_PATH"), "./data/owntracks/user_tags.json")

    events = parse_log(log_path, local_tz)
    user_tags = load_user_tags(tags_path)
    plan, track_points = build_plan(events, target_date, user_tags)

    derived_dir.mkdir(parents=True, exist_ok=True)
    plan_path = derived_dir / f"activity-tag-plan-{target_date.isoformat()}.json"
    digest_path = derived_dir / f"activity-summary-{target_date.isoformat()}.txt"
    geojson_path = derived_dir / f"activity-track-{target_date.isoformat()}.geojson"
    map_path = derived_dir / f"activity-map-{target_date.isoformat()}.html"
    map_delivery = env.get("OWNTRACKS_MAP_DELIVERY", "file").strip().lower()
    embed_map_tiles = (
        map_delivery != "hosted"
        and env.get("OWNTRACKS_EMBED_MAP_TILES", "false").strip().lower() in {"1", "true", "yes", "on"}
    )
    digest = render_digest(plan)

    plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    digest_path.write_text(digest + "\n", encoding="utf-8")
    geojson_path.write_text(json.dumps(build_geojson(track_points, plan["named_places"], plan["candidate_stops"]), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tile_cache_dir = derived_dir / "tile-cache" if embed_map_tiles else None
    map_path.write_text(render_map_html(plan, tile_cache_dir), encoding="utf-8")
    return plan, digest, digest_path


def build_plan_for_date(date_text: str | None = None) -> tuple[dict, list]:
    env = load_env()
    local_tz = ZoneInfo(env.get("OWNTRACKS_TIMEZONE", "Asia/Kolkata"))
    target_date = target_date_from_text(date_text, local_tz)
    log_path = project_path(env.get("OWNTRACKS_LOG_PATH"), "./data/owntracks/mqtt.log")
    tags_path = project_path(env.get("OWNTRACKS_USER_TAGS_PATH"), "./data/owntracks/user_tags.json")

    events = parse_log(log_path, local_tz)
    user_tags = load_user_tags(tags_path)
    return build_plan(events, target_date, user_tags)


def generate_hosted_map(date_text: str | None = None) -> tuple[dict, str]:
    env = load_env()
    local_tz = ZoneInfo(env.get("OWNTRACKS_TIMEZONE", "Asia/Kolkata"))
    scope = target_scope_from_text(date_text, local_tz)
    log_path = project_path(env.get("OWNTRACKS_LOG_PATH"), "./data/owntracks/mqtt.log")
    tags_path = project_path(env.get("OWNTRACKS_USER_TAGS_PATH"), "./data/owntracks/user_tags.json")

    events = parse_log(log_path, local_tz)
    user_tags = load_user_tags(tags_path)
    if scope.kind == "day":
        plan, _track_points = build_plan(events, scope.start_date, user_tags)
        return plan, render_leaflet_map_html(plan)
    summary = build_heatmap_summary(events, scope, user_tags)
    return summary, render_heatmap_html(summary)


def generate_sample_heatmap() -> tuple[dict, str]:
    summary = build_sample_heatmap_summary()
    return summary, render_heatmap_html(summary)


def generate_owntracks_visualization(scope_text: str | None = None) -> tuple[dict, str, Path]:
    env = load_env()
    local_tz = ZoneInfo(env.get("OWNTRACKS_TIMEZONE", "Asia/Kolkata"))
    scope = target_scope_from_text(scope_text, local_tz)
    log_path = project_path(env.get("OWNTRACKS_LOG_PATH"), "./data/owntracks/mqtt.log")
    derived_dir = project_path(env.get("OWNTRACKS_DERIVED_DIR"), "./data/owntracks/derived")
    tags_path = project_path(env.get("OWNTRACKS_USER_TAGS_PATH"), "./data/owntracks/user_tags.json")

    events = parse_log(log_path, local_tz)
    user_tags = load_user_tags(tags_path)
    derived_dir.mkdir(parents=True, exist_ok=True)

    if scope.kind == "day":
        plan, digest, digest_path = generate_digest(scope.value)
        map_path = digest_path.with_name(f"activity-map-{scope.value}.html")
        if not map_path.exists():
            map_path.write_text(render_leaflet_map_html(plan), encoding="utf-8")
        summary = {
            **plan,
            "scope": {
                "kind": "day",
                "value": scope.value,
                "start": scope.start_date.isoformat(),
                "end": scope.end_date.isoformat(),
            },
        }
        return summary, render_leaflet_map_html(plan), map_path

    summary = build_heatmap_summary(events, scope, user_tags)
    html = render_heatmap_html(summary)
    map_path = derived_dir / f"activity-map-{scope.value}.html"
    map_path.write_text(html, encoding="utf-8")
    summary_path = derived_dir / f"activity-heatmap-{scope.value}.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return summary, html, map_path


def send_daily(date_text: str | None = None) -> None:
    env = load_send_env()
    _plan, digest, _path = generate_digest(date_text)
    token = env.get("BOT_TOKEN") or env.get("TELEGRAM_BOT_TOKEN")
    chat_id = env.get("TELEGRAM_CHAT_ID") or env.get("TELEGRAM__CHAT_ID")
    topic_id = env_int(env.get("OWNTRACKS_TOPIC_ID"))
    if not token or not chat_id:
        raise RuntimeError("BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env or fallback Telegram env")
    if not topic_id:
        raise RuntimeError("OWNTRACKS_TOPIC_ID must be set in .env before sending the OwnTracks digest")
    send_telegram_message(token, chat_id, digest, topic_id)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate or send OwnTracks daily digest.")
    parser.add_argument("--date", help="YYYY-MM-DD, today, or yesterday. Defaults to today.")
    parser.add_argument("--send", action="store_true", help="Send digest to Telegram after generating it.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.send:
        send_daily(args.date)
    else:
        _plan, digest, path = generate_digest(args.date)
        print(digest)
        print()
        print(f"Wrote {path}")


if __name__ == "__main__":
    main()
