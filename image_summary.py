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

from codex_llm import CodexLlmConfig, ask_codex_image, ask_codex_text, build_codex_llm_config
from metrics import OCR_CHARS_TOTAL, OLLAMA_REQUEST_DURATION_SECONDS, OLLAMA_REQUESTS_TOTAL, error_type


IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
VISION_PROMPT = """
Read the uploaded image carefully and summarize only what is visibly present.

Focus on useful content: visible text, document purpose, numbers, dates, names, tasks, and decisions.
Do not invent people, places, events, or facts that are not visible in the image.
If the image is too blurry or unreadable, say that briefly instead of guessing.
""".strip()
CODEX_BENCHMARK_PROMPT = """
Read the uploaded image carefully and extract all visible text plus key facts.

Return a faithful benchmark record that includes:
- every readable line item when the image is a receipt, invoice, list, or table
- visible text, numbers, dates, names, labels, quantities, prices, and totals when readable
- the likely document or image purpose
- important tasks, decisions, or claims visible in the image

Do not shorten or summarize away repeated rows or line items. Preserve the source order.
Do not invent people, places, events, or facts that are not visible in the image.
If a field is blurry or unreadable, say so briefly instead of guessing.
""".strip()


def image_prompt(base_prompt: str, user_comment: str | None = None) -> str:
    comment = (user_comment or "").strip()
    if not comment:
        return base_prompt
    return f"""
{base_prompt}

USER-SUPPLIED IMAGE COMMENT:
{comment}

Use the comment as trusted context for spelling corrections, ambiguous text,
document purpose, and other facts the user explicitly supplies. Preserve both
the visibly printed value and the user's correction when they differ. Do not
claim that comment-only text was visibly printed in the image.
""".strip()
COMPARISON_PROMPT_TEMPLATE = """
You are evaluating image-summary quality for a private Telegram workflow.

Treat the Codex benchmark as the reference text extracted from the image.
Compare each local Ollama vision response against it using only the text below.
Do not add facts that are not present in the benchmark or candidate response.

Return a concise Telegram-friendly Markdown report. Do not use a Markdown table.
Use this shape for each local model:

Benchmark evaluation: <model label>
Verdict: Use local / Borderline / Use Codex
Overall: <score>/5
Scores:
- Factual coverage: <score>/5
- Missing important details: <score>/5
- Unsupported claims risk: <score>/5
- Text and number fidelity: <score>/5
- Practical usefulness: <score>/5
What MiniCPM got right:
- <short bullets>
What MiniCPM missed or distorted:
- <short bullets>
Decision note: <one sentence about whether the local model is good enough>

Scoring guidance:
- factual coverage: higher means more Codex benchmark facts are preserved
- missing important details: higher means fewer important omissions
- unsupported claims risk: higher means fewer claims unsupported by the Codex benchmark
- text and number fidelity: higher means visible text, numbers, dates, and names match better
- practical usefulness: higher means better for deciding whether local Ollama is sufficient

CODEX BENCHMARK:
{benchmark}

OLLAMA RESPONSES:
{candidates}
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
    ocr_enabled: bool
    ocr_command: str
    ocr_lang: str
    ocr_psm: str
    ocr_llm_model: str
    vision_llm_model: str
    vision_llm_models: tuple[str, ...]
    ollama_url: str
    ollama_timeout_seconds: int
    ollama_enabled: bool
    codex_llm_enabled: bool
    codex_llm_model: str
    codex_llm_config: CodexLlmConfig
    memory_dir: Path
    memory_llm_model: str
    memory_query_model: str
    memory_query_top_k: int
    memory_query_max_context_chars: int
    memory_query_history_turns: int
    memory_link_enrichment_enabled: bool
    memory_link_timeout_seconds: int
    memory_link_max_bytes: int
    memory_link_max_urls: int
    memory_link_allowed_hosts: tuple[str, ...]
    memory_pdf_max_bytes: int
    memory_pdf_max_pages: int
    memory_pdf_render_scale: float
    memory_caption_pair_grace_seconds: int


def env_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(value: str | None, default: int) -> int:
    if not value:
        return default
    return int(value)


def env_topic_id(value: str | None, default: int) -> int:
    if value is None:
        return default
    if not value.strip():
        return 0
    return int(value)


def env_list(value: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if not value:
        return default
    return tuple(part.strip() for part in value.split(",") if part.strip())


def build_config(env: dict[str, str], fallback_chat_id: int) -> ImageSummaryConfig:
    mode = env.get("IMAGE_SUMMARY_MODE", "compare").strip().lower()
    if mode not in {"compare", "ocr", "vision"}:
        raise ValueError("IMAGE_SUMMARY_MODE must be one of: compare, ocr, vision")
    ocr_enabled = env_bool(env.get("IMAGE_SUMMARY_OCR_ENABLED"), False)
    if mode == "ocr" and not ocr_enabled:
        raise ValueError("IMAGE_SUMMARY_MODE=ocr requires IMAGE_SUMMARY_OCR_ENABLED=true")

    work_dir = Path(
        env.get(
            "IMAGE_SUMMARY_WORK_DIR",
            "./data/image-summary",
        )
    ).expanduser()

    vision_model = env.get("IMAGE_SUMMARY_VISION_LLM_MODEL", "gemma4:e2b")
    return ImageSummaryConfig(
        chat_id=env_int(env.get("IMAGE_SUMMARY_CHAT_ID"), fallback_chat_id),
        topic_id=env_topic_id(env.get("IMAGE_SUMMARY_TOPIC_ID"), 145),
        max_reply_chars=env_int(env.get("IMAGE_SUMMARY_MAX_REPLY_CHARS"), 3600),
        summary_mode=mode,
        work_dir=work_dir,
        log_file=Path(env.get("IMAGE_SUMMARY_LOG_FILE", str(work_dir / "worker.log"))).expanduser(),
        debug_updates=env_bool(env.get("IMAGE_SUMMARY_DEBUG_UPDATES"), True),
        ocr_enabled=ocr_enabled,
        ocr_command=env.get("IMAGE_SUMMARY_OCR_COMMAND", "tesseract"),
        ocr_lang=env.get("IMAGE_SUMMARY_OCR_LANG", "eng"),
        ocr_psm=env.get("IMAGE_SUMMARY_OCR_PSM", "6"),
        ocr_llm_model=env.get("IMAGE_SUMMARY_OCR_LLM_MODEL", "llama3.1:8b"),
        vision_llm_model=vision_model,
        vision_llm_models=env_list(env.get("IMAGE_SUMMARY_VISION_MODELS"), (vision_model,)),
        ollama_url=env.get("IMAGE_SUMMARY_OLLAMA_URL", "http://localhost:11434").rstrip("/"),
        ollama_timeout_seconds=env_int(env.get("IMAGE_SUMMARY_OLLAMA_TIMEOUT_SECONDS"), 600),
        ollama_enabled=env_bool(env.get("OLLAMA_ENABLED"), True),
        codex_llm_enabled=env_bool(env.get("CODEX_LLM_ENABLED"), False),
        codex_llm_model=env.get("CODEX_LLM_MODEL", "").strip(),
        codex_llm_config=build_codex_llm_config(env),
        memory_dir=Path(env.get("MEMORY_WORK_DIR", "./data/memories")).expanduser(),
        memory_llm_model=env.get("MEMORY_LLM_MODEL", env.get("IMAGE_SUMMARY_OCR_LLM_MODEL", "llama3.1:8b")),
        memory_query_model=env.get("MEMORY_QUERY_MODEL", "gemma4:31b-cloud"),
        memory_query_top_k=env_int(env.get("MEMORY_QUERY_TOP_K"), 1),
        memory_query_max_context_chars=env_int(env.get("MEMORY_QUERY_MAX_CONTEXT_CHARS"), 14000),
        memory_query_history_turns=env_int(env.get("MEMORY_QUERY_HISTORY_TURNS"), 3),
        memory_link_enrichment_enabled=env_bool(env.get("MEMORY_LINK_ENRICHMENT_ENABLED"), True),
        memory_link_timeout_seconds=env_int(env.get("MEMORY_LINK_TIMEOUT_SECONDS"), 10),
        memory_link_max_bytes=env_int(env.get("MEMORY_LINK_MAX_BYTES"), 2 * 1024 * 1024),
        memory_link_max_urls=env_int(env.get("MEMORY_LINK_MAX_URLS"), 3),
        memory_link_allowed_hosts=env_list(env.get("MEMORY_LINK_ALLOWED_HOSTS"), ()),
        memory_pdf_max_bytes=env_int(env.get("MEMORY_PDF_MAX_BYTES"), 20 * 1024 * 1024),
        memory_pdf_max_pages=env_int(env.get("MEMORY_PDF_MAX_PAGES"), 10),
        memory_pdf_render_scale=float(env.get("MEMORY_PDF_RENDER_SCALE") or "2.5"),
        memory_caption_pair_grace_seconds=env_int(env.get("MEMORY_CAPTION_PAIR_GRACE_SECONDS"), 3),
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
    text = result.stdout.strip()
    OCR_CHARS_TOTAL.inc(len(text))
    return text


def ollama_chat(
    cfg: ImageSummaryConfig,
    model: str,
    prompt: str,
    images: list[Path] | None = None,
    purpose: str = "general",
) -> str:
    start = time.monotonic()
    message: dict[str, Any] = {"role": "user", "content": prompt}
    if images:
        message["images"] = [base64.b64encode(path.read_bytes()).decode("ascii") for path in images]
    try:
        resp = requests.post(
            f"{cfg.ollama_url}/api/chat",
            json={"model": model, "messages": [message], "stream": False},
            timeout=cfg.ollama_timeout_seconds,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Ollama {model} failed: HTTP {resp.status_code}: {resp.text[:500]}")
        OLLAMA_REQUESTS_TOTAL.labels(purpose=purpose, model=model, result="success").inc()
        return resp.json().get("message", {}).get("content", "").strip()
    except Exception as exc:
        OLLAMA_REQUESTS_TOTAL.labels(purpose=purpose, model=model, result=error_type(exc)).inc()
        raise
    finally:
        OLLAMA_REQUEST_DURATION_SECONDS.labels(purpose=purpose, model=model).observe(time.monotonic() - start)


def codex_text_chat(cfg: ImageSummaryConfig, prompt: str) -> str:
    return ask_codex_text(prompt, cfg.codex_llm_config)


def codex_image_chat(cfg: ImageSummaryConfig, prompt: str, image_path: Path) -> str:
    return ask_codex_image(prompt, [image_path], cfg.codex_llm_config)


def text_llm_chat(cfg: ImageSummaryConfig, ollama_model: str, prompt: str, purpose: str) -> str:
    if cfg.codex_llm_enabled:
        try:
            return codex_text_chat(cfg, prompt)
        except Exception as exc:
            log(cfg, f"codex_llm_failed purpose={purpose} model={cfg.codex_llm_model or 'default'} error={exc}")
    if not cfg.ollama_enabled:
        raise RuntimeError(f"No text LLM available for {purpose}: Codex failed or is disabled and Ollama is disabled")
    return ollama_chat(cfg, ollama_model, prompt, purpose=purpose)


def summarize_ocr(
    image_path: Path,
    cfg: ImageSummaryConfig,
    user_comment: str | None = None,
) -> tuple[str, str]:
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
    prompt = image_prompt(prompt, user_comment)
    summary = text_llm_chat(cfg, cfg.ocr_llm_model, prompt, "ocr_summary")
    return text, summary


def summarize_vision(image_path: Path, cfg: ImageSummaryConfig, user_comment: str | None = None) -> str:
    return ollama_chat(
        cfg,
        cfg.vision_llm_model,
        image_prompt(VISION_PROMPT, user_comment),
        images=[image_path],
        purpose="vision_summary",
    )


def summarize_codex_vision(
    image_path: Path,
    cfg: ImageSummaryConfig,
    user_comment: str | None = None,
) -> str:
    return codex_image_chat(cfg, image_prompt(CODEX_BENCHMARK_PROMPT, user_comment), image_path)


def compare_ollama_to_codex(results: list[dict[str, Any]], cfg: ImageSummaryConfig) -> dict[str, Any] | None:
    benchmark = next(
        (
            result
            for result in results
            if result.get("ok") and str(result.get("label", "")).startswith("Codex benchmark text")
        ),
        None,
    )
    candidates = [
        result
        for result in results
        if result.get("ok") and str(result.get("label", "")).startswith("Direct vision LLM")
    ]
    if not benchmark or not candidates:
        return None

    candidate_text = "\n\n".join(
        f"{index}. {result['label']}\n{str(result['value']).strip()}"
        for index, result in enumerate(candidates, start=1)
    )
    prompt = COMPARISON_PROMPT_TEMPLATE.format(
        benchmark=str(benchmark["value"]).strip(),
        candidates=candidate_text,
    )
    label = f"MiniCPM-vs-Codex benchmark ({cfg.codex_llm_model or 'default'})"
    return timed_call(label, lambda: codex_text_chat(cfg, prompt))


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


def processing_description(cfg: ImageSummaryConfig) -> str:
    parts: list[str] = []
    if cfg.summary_mode in {"compare", "vision"}:
        if cfg.codex_llm_enabled:
            parts.append("Codex vision")
        if cfg.ollama_enabled:
            parts.append("direct vision")
    if cfg.summary_mode in {"compare", "ocr"} and cfg.ocr_enabled:
        parts.append("OCR")
    return " + ".join(parts) or cfg.summary_mode


def image_result_jobs(
    image_path: Path,
    cfg: ImageSummaryConfig,
    user_comment: str | None = None,
) -> list[tuple[str, Callable[[], dict[str, Any]]]]:
    jobs: list[tuple[str, Callable[[], dict[str, Any]]]] = []
    if cfg.summary_mode in {"compare", "ocr"} and cfg.ocr_enabled:
        model = f"Codex {cfg.codex_llm_model or 'default'} -> Ollama {cfg.ocr_llm_model}" if cfg.codex_llm_enabled else cfg.ocr_llm_model
        label = f"OCR + LLM ({model})"
        jobs.append(
            (
                label,
                lambda label=label: timed_call(
                    label,
                    lambda: summarize_ocr(image_path, cfg, user_comment),
                ),
            )
        )
    if cfg.summary_mode in {"compare", "vision"}:
        if cfg.codex_llm_enabled:
            label = f"Codex benchmark text ({cfg.codex_llm_model or 'default'})"
            jobs.append(
                (
                    label,
                    lambda label=label: timed_call(
                        label,
                        lambda: summarize_codex_vision(image_path, cfg, user_comment),
                    ),
                )
            )
        if cfg.ollama_enabled:
            for model in cfg.vision_llm_models:
                label = f"Direct vision LLM ({model})"
                jobs.append(
                    (
                        label,
                        lambda label=label, model=model: timed_call(
                            label,
                            lambda: ollama_chat(
                                cfg,
                                model,
                                image_prompt(VISION_PROMPT, user_comment),
                                images=[image_path],
                                purpose="vision_summary",
                            ),
                        ),
                    )
                )
    return jobs


def process_image(image_path: Path, cfg: ImageSummaryConfig, user_comment: str | None = None) -> str:
    results = [job() for _, job in image_result_jobs(image_path, cfg, user_comment)]
    comparison = compare_ollama_to_codex(results, cfg)
    replies = [build_reply(results, cfg.summary_mode)]
    if comparison:
        replies.append(build_result_reply(comparison, "comparison"))
    return "\n\n".join(replies)
