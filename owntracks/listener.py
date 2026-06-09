from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime

from .env import load_env, project_path


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
            print(line, end="")
            sys.stdout.flush()
    return proc.wait()


def main() -> None:
    raise SystemExit(run_listener())


if __name__ == "__main__":
    main()
