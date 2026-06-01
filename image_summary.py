from __future__ import annotations

import base64
import datetime as dt
import json
import shutil
import subprocess
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests


IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
VISION_PROMPT = """
Read the uploaded image carefully and summarize only what is visibly present.

Focus on useful content: visible text, document purpose, numbers, dates, names, tasks, and decisions.
Do not invent people, places, events, or facts that are not visible in the image.
If the image is too blurry or unreadable, say that briefly instead of guessing.
""".strip()


@dataclass(frozen=True)
class ImageSummaryConfig:
    chat_id: int
    topic_id: int
    max_reply_chars: int
    summary_mode: str
    work_dir: Path
    log_file: Path
    debug_updates: bool
    ocr_command: str
    ocr_lang: str
    ocr_psm: str
    ocr_llm_model: str
    vision_llm_model: str
    vision_llm_models: tuple[str, ...]
    ollama_url: str
    ollama_timeout_seconds: int
    memory_dir: Path
    memory_llm_model: str


def env_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(value: str | None, default: int) -> int:
    if not value:
        return default
    return int(value)


def env_list(value: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if not value:
        return default
    return tuple(part.strip() for part in value.split(",") if part.strip())


def build_config(env: dict[str, str], fallback_chat_id: int) -> ImageSummaryConfig:
    mode = env.get("IMAGE_SUMMARY_MODE", "compare").strip().lower()
    if mode not in {"compare", "ocr", "vision"}:
        raise ValueError("IMAGE_SUMMARY_MODE must be one of: compare, ocr, vision")

    work_dir = Path(
        env.get(
            "IMAGE_SUMMARY_WORK_DIR",
            "./data/image-summary",
        )
    ).expanduser()

    vision_model = env.get("IMAGE_SUMMARY_VISION_LLM_MODEL", "gemma4:e2b")
    return ImageSummaryConfig(
        chat_id=env_int(env.get("IMAGE_SUMMARY_CHAT_ID"), fallback_chat_id),
        topic_id=env_int(env.get("IMAGE_SUMMARY_TOPIC_ID"), 145),
        max_reply_chars=env_int(env.get("IMAGE_SUMMARY_MAX_REPLY_CHARS"), 3600),
        summary_mode=mode,
        work_dir=work_dir,
        log_file=Path(env.get("IMAGE_SUMMARY_LOG_FILE", str(work_dir / "worker.log"))).expanduser(),
        debug_updates=env_bool(env.get("IMAGE_SUMMARY_DEBUG_UPDATES"), True),
        ocr_command=env.get("IMAGE_SUMMARY_OCR_COMMAND", "tesseract"),
        ocr_lang=env.get("IMAGE_SUMMARY_OCR_LANG", "eng"),
        ocr_psm=env.get("IMAGE_SUMMARY_OCR_PSM", "6"),
        ocr_llm_model=env.get("IMAGE_SUMMARY_OCR_LLM_MODEL", "llama3.1:8b"),
        vision_llm_model=vision_model,
        vision_llm_models=env_list(env.get("IMAGE_SUMMARY_VISION_MODELS"), (vision_model,)),
        ollama_url=env.get("IMAGE_SUMMARY_OLLAMA_URL", "http://localhost:11434").rstrip("/"),
        ollama_timeout_seconds=env_int(env.get("IMAGE_SUMMARY_OLLAMA_TIMEOUT_SECONDS"), 600),
        memory_dir=Path(env.get("MEMORY_WORK_DIR", "./data/memories")).expanduser(),
        memory_llm_model=env.get("MEMORY_LLM_MODEL", env.get("IMAGE_SUMMARY_OCR_LLM_MODEL", "llama3.1:8b")),
    )


def log(cfg: ImageSummaryConfig, message: str) -> None:
    cfg.log_file.parent.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(IST).isoformat(timespec="seconds")
    with cfg.log_file.open("a", encoding="utf-8") as f:
        f.write(f"{stamp} {message}\n")


def split_message(text: str, max_chars: int) -> list[str]:
    text = text.strip()
    if not text:
        return []
    parts: list[str] = []
    while len(text) > max_chars:
        cut = text.rfind("\n\n", 0, max_chars)
        if cut < max_chars * 0.5:
            cut = text.rfind("\n", 0, max_chars)
        if cut < max_chars * 0.5:
            cut = max_chars
        parts.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        parts.append(text)
    return parts


def run_ocr(image_path: Path, cfg: ImageSummaryConfig) -> str:
    if not shutil.which(cfg.ocr_command):
        raise RuntimeError(f"OCR command not found: {cfg.ocr_command}")
    cmd = [
        cfg.ocr_command,
        str(image_path),
        "stdout",
        "-l",
        cfg.ocr_lang,
        "--psm",
        cfg.ocr_psm,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "OCR failed")
    return result.stdout.strip()


def ollama_chat(
    cfg: ImageSummaryConfig,
    model: str,
    prompt: str,
    images: list[Path] | None = None,
) -> str:
    message: dict[str, Any] = {"role": "user", "content": prompt}
    if images:
        message["images"] = [base64.b64encode(path.read_bytes()).decode("ascii") for path in images]
    resp = requests.post(
        f"{cfg.ollama_url}/api/chat",
        json={"model": model, "messages": [message], "stream": False},
        timeout=cfg.ollama_timeout_seconds,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Ollama {model} failed: HTTP {resp.status_code}: {resp.text[:500]}")
    return resp.json().get("message", {}).get("content", "").strip()


def summarize_ocr(image_path: Path, cfg: ImageSummaryConfig) -> tuple[str, str]:
    text = run_ocr(image_path, cfg)
    if not text:
        return "", "OCR found no text."
    prompt = f"""
You are summarizing text extracted from an image uploaded to a private Telegram topic.

Write a concise, useful summary. Preserve important numbers, dates, names, tasks, and decisions.
If the OCR text is noisy, infer cautiously and mention uncertainty only when it matters.

OCR TEXT:
{text}
""".strip()
    summary = ollama_chat(cfg, cfg.ocr_llm_model, prompt)
    return text, summary


def summarize_vision(image_path: Path, cfg: ImageSummaryConfig) -> str:
    return ollama_chat(cfg, cfg.vision_llm_model, VISION_PROMPT, images=[image_path])


def timed_call(label: str, fn: Any) -> dict[str, Any]:
    start = time.monotonic()
    try:
        value = fn()
        return {"label": label, "ok": True, "value": value, "seconds": time.monotonic() - start}
    except Exception as exc:
        return {"label": label, "ok": False, "error": str(exc), "seconds": time.monotonic() - start}


def build_reply(results: list[dict[str, Any]], mode: str) -> str:
    lines = [f"Image summary ({mode})"]
    for result in results:
        seconds = f"{result['seconds']:.1f}s"
        lines.append("")
        lines.append(f"{result['label']} [{seconds}]")
        if not result["ok"]:
            lines.append(f"Failed: {result['error']}")
            continue
        value = result["value"]
        if isinstance(value, tuple):
            ocr_text, summary = value
            lines.append(summary.strip() or "No summary returned.")
            if ocr_text:
                preview = textwrap.shorten(" ".join(ocr_text.split()), width=700, placeholder=" ...")
                lines.append("")
                lines.append("OCR preview:")
                lines.append(preview)
        else:
            lines.append(str(value).strip() or "No summary returned.")
    return "\n".join(lines).strip()


def build_result_reply(result: dict[str, Any], mode: str) -> str:
    return build_reply([result], mode)


def image_result_jobs(image_path: Path, cfg: ImageSummaryConfig) -> list[tuple[str, Callable[[], dict[str, Any]]]]:
    jobs: list[tuple[str, Callable[[], dict[str, Any]]]] = []
    if cfg.summary_mode in {"compare", "ocr"}:
        label = f"OCR + LLM ({cfg.ocr_llm_model})"
        jobs.append((label, lambda label=label: timed_call(label, lambda: summarize_ocr(image_path, cfg))))
    if cfg.summary_mode in {"compare", "vision"}:
        for model in cfg.vision_llm_models:
            label = f"Direct vision LLM ({model})"
            jobs.append(
                (
                    label,
                    lambda label=label, model=model: timed_call(
                        label,
                        lambda: ollama_chat(cfg, model, VISION_PROMPT, images=[image_path]),
                    ),
                )
            )
    return jobs


def process_image(image_path: Path, cfg: ImageSummaryConfig) -> str:
    results = [job() for _, job in image_result_jobs(image_path, cfg)]
    return build_reply(results, cfg.summary_mode)
