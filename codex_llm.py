from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class CodexLlmConfig:
    cwd: Path | None
    model: str | None
    sandbox: str | None
    ephemeral: bool
    base_instructions: str | None


def build_codex_llm_config(env: dict[str, str] | None = None) -> CodexLlmConfig:
    env = env or os.environ
    cwd_value = env.get("CODEX_LLM_CWD", ".").strip()
    model = env.get("CODEX_LLM_MODEL", "").strip() or None
    sandbox = env.get("CODEX_LLM_SANDBOX", "read_only").strip() or None
    ephemeral = _env_bool(env.get("CODEX_LLM_EPHEMERAL"), True)
    base_instructions = env.get("CODEX_LLM_BASE_INSTRUCTIONS", "").strip() or None
    return CodexLlmConfig(
        cwd=Path(cwd_value).expanduser() if cwd_value else None,
        model=model,
        sandbox=sandbox,
        ephemeral=ephemeral,
        base_instructions=base_instructions,
    )


def _sandbox_value(name: str | None):
    if not name:
        return None
    try:
        from openai_codex import Sandbox
    except ModuleNotFoundError as exc:
        raise RuntimeError(_missing_sdk_message()) from exc

    normalized = name.strip().lower().replace("-", "_")
    choices = {
        "read_only": Sandbox.read_only,
        "workspace_write": Sandbox.workspace_write,
        "full_access": Sandbox.full_access,
    }
    if normalized not in choices:
        raise ValueError("CODEX_LLM_SANDBOX must be one of: read_only, workspace_write, full_access")
    return choices[normalized]


def ask_codex_text(prompt: str, cfg: CodexLlmConfig | None = None) -> str:
    return ask_codex(prompt=prompt, image_paths=(), cfg=cfg)


def ask_codex_image(prompt: str, image_paths: Iterable[Path | str], cfg: CodexLlmConfig | None = None) -> str:
    return ask_codex(prompt=prompt, image_paths=image_paths, cfg=cfg)


def ask_codex(
    prompt: str,
    image_paths: Iterable[Path | str] = (),
    cfg: CodexLlmConfig | None = None,
) -> str:
    try:
        from openai_codex import ApprovalMode, Codex, LocalImageInput, TextInput
    except ModuleNotFoundError as exc:
        raise RuntimeError(_missing_sdk_message()) from exc

    cfg = cfg or build_codex_llm_config()
    images = [Path(path).expanduser() for path in image_paths]
    for image in images:
        if not image.exists():
            raise FileNotFoundError(image)

    run_input = [TextInput(text=prompt)]
    run_input.extend(LocalImageInput(path=str(image)) for image in images)

    with Codex() as codex:
        thread = codex.thread_start(
            approval_mode=ApprovalMode.deny_all,
            base_instructions=cfg.base_instructions,
            cwd=str(cfg.cwd) if cfg.cwd else None,
            ephemeral=cfg.ephemeral,
            model=cfg.model,
            sandbox=_sandbox_value(cfg.sandbox),
        )
        result = thread.run(run_input, model=cfg.model)

    if result.final_response is None:
        raise RuntimeError("Codex turn completed without a final response")
    return result.final_response.strip()


def _missing_sdk_message() -> str:
    return (
        "openai_codex is not installed in this Python environment. Install the official "
        "Codex Python SDK with `uv --cache-dir .uv-cache sync --extra codex --prerelease allow` "
        "or make it available to this Python environment."
    )


def _env_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
