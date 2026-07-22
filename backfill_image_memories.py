from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from bot import load_config
from image_summary import image_result_jobs
from memory_processor import has_image_memory, save_image_memory


def image_paths(images_dir: Path) -> list[Path]:
    return sorted(path for path in images_dir.iterdir() if path.is_file()) if images_dir.exists() else []


def backfill(
    paths: Sequence[Path],
    limit: int | None = None,
    user_comment: str | None = None,
) -> int:
    cfg = load_config().image_summary
    pending = list(paths) if user_comment else [path for path in paths if not has_image_memory(path, cfg)]
    if limit is not None:
        pending = pending[:limit]
    print(f"Found {len(paths)} saved images; {len(pending)} need image memories.", flush=True)
    failures = 0
    for index, path in enumerate(pending, start=1):
        print(f"[{index}/{len(pending)}] Extracting {path.name}", flush=True)
        results = [job() for _label, job in image_result_jobs(path, cfg, user_comment)]
        saved = save_image_memory(
            path,
            results,
            cfg,
            {"backfilled": True, "user_comment": user_comment},
        )
        if saved:
            print(f"[{index}/{len(pending)}] Saved {saved.path.name}", flush=True)
        elif has_image_memory(path, cfg):
            print(f"[{index}/{len(pending)}] Skipped duplicate image content", flush=True)
        else:
            failures += 1
            errors = "; ".join(str(result.get("error", "no usable text")) for result in results if not result.get("ok"))
            print(f"[{index}/{len(pending)}] FAILED {path.name}: {errors or 'no usable extraction'}", flush=True)
    print(f"Backfill complete: saved={len(pending) - failures} failed={failures}", flush=True)
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Create missing Markdown memories for saved image-summary files.")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--image", help="Process only this saved image filename")
    parser.add_argument("--comment", help="User context or correction to apply while reprocessing --image")
    args = parser.parse_args()
    cfg = load_config().image_summary
    paths = image_paths(cfg.work_dir / "images")
    if args.image:
        paths = [path for path in paths if path.name == args.image]
        if not paths:
            parser.error(f"saved image not found: {args.image}")
    if args.comment and not args.image:
        parser.error("--comment requires --image")
    return backfill(paths, args.limit, args.comment)


if __name__ == "__main__":
    raise SystemExit(main())
