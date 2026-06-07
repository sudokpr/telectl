from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .env import env_int, load_env, project_path
from .tagger import build_geojson, build_plan, load_user_tags, parse_log, render_digest, target_date_from_text
from .telegram_send import send_telegram_message


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
    digest = render_digest(plan)

    plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    digest_path.write_text(digest + "\n", encoding="utf-8")
    geojson_path.write_text(json.dumps(build_geojson(track_points, plan["named_places"], plan["candidate_stops"]), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return plan, digest, digest_path


def send_daily(date_text: str | None = None) -> None:
    env = load_env()
    _plan, digest, _path = generate_digest(date_text)
    token = env.get("BOT_TOKEN") or env.get("TELEGRAM_BOT_TOKEN")
    chat_id = env.get("TELEGRAM_CHAT_ID") or env.get("TELEGRAM__CHAT_ID")
    topic_id = env_int(env.get("OWNTRACKS_TOPIC_ID"))
    if not token or not chat_id:
        raise RuntimeError("BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env")
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
