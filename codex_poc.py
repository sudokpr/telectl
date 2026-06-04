from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from codex_llm import ask_codex_image, ask_codex_text, build_codex_llm_config


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="POC for querying the local Codex app server through the Python SDK.")
    parser.add_argument("question", nargs="?", default="Say hello in one sentence.")
    parser.add_argument(
        "--image",
        action="append",
        default=[],
        help="Local image path to include. Can be passed more than once.",
    )
    parser.add_argument("--no-env", action="store_true", help="Do not load .env before running.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.no_env:
        load_dotenv()

    cfg = build_codex_llm_config()
    try:
        if args.image:
            response = ask_codex_image(args.question, args.image, cfg)
        else:
            response = ask_codex_text(args.question, cfg)
    except Exception as exc:
        print(f"codex_poc failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(response)


if __name__ == "__main__":
    main()
