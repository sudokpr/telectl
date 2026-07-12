from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

from .env import env_int, load_env, project_path
from .telegram_send import send_telegram_message


def env_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_mqtt_log_line(line: str) -> tuple[str, str, dict[str, Any]] | None:
    stamp, sep, rest = line.strip().partition(" ")
    if not sep:
        return None
    topic, sep, payload_text = rest.partition(" ")
    if not sep:
        return None
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return stamp, topic, payload


def owntracks_poi_message(stamp: str, topic: str, payload: dict[str, Any]) -> str | None:
    poi = str(payload.get("poi") or "").strip()
    if not poi:
        return None
    lines = ["OwnTracks POI", f"Time: {stamp}", f"Topic: {topic}", "", poi]
    lat = payload.get("lat")
    lon = payload.get("lon")
    if lat is not None and lon is not None:
        lines.extend(["", f"Maps: https://www.google.com/maps?q={lat},{lon}"])
    return "\n".join(lines)


def notify_poi(env: dict[str, str], line: str) -> None:
    if not env_bool(env.get("OWNTRACKS_POI_NOTIFY_TELEGRAM"), True):
        return
    parsed = parse_mqtt_log_line(line)
    if parsed is None:
        return
    stamp, topic, payload = parsed
    message = owntracks_poi_message(stamp, topic, payload)
    if not message:
        return
    token = env.get("BOT_TOKEN") or env.get("TELEGRAM_BOT_TOKEN")
    chat_id = env.get("TELEGRAM_CHAT_ID") or env.get("TELEGRAM__CHAT_ID")
    topic_id = env_int(env.get("OWNTRACKS_TOPIC_ID"))
    if not token or not chat_id:
        print("OwnTracks POI notification skipped: BOT_TOKEN and TELEGRAM_CHAT_ID are required", file=sys.stderr)
        return
    send_telegram_message(token, chat_id, message, topic_id)


def run_listener() -> int:
    env = load_env()
    log_path = project_path(env.get("OWNTRACKS_LOG_PATH"), "./data/owntracks/mqtt.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "mosquitto_sub",
        "-h",
        env.get("MQTT_HOST", "localhost"),
        "-t",
        env.get("MQTT_TOPIC", "owntracks/#"),
        "-F",
        "%I %t %p",
    ]
    username = env.get("MQTT_USERNAME")
    password = env.get("MQTT_PASSWORD")
    if username:
        cmd.extend(["-u", username])
    if password:
        cmd.extend(["-P", password])
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    assert proc.stdout is not None
    with log_path.open("a", encoding="utf-8") as handle:
        for line in proc.stdout:
            handle.write(line)
            handle.flush()
            try:
                notify_poi(env, line)
            except Exception as exc:  # noqa: BLE001 - logging must not stop MQTT capture.
                print(f"OwnTracks POI notification failed: {exc}", file=sys.stderr)
            print(line, end="")
            sys.stdout.flush()
    return proc.wait()


def main() -> None:
    raise SystemExit(run_listener())


if __name__ == "__main__":
    main()
