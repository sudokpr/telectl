# AGENTS.md

## Project Purpose

This project runs one Telegram bot process for:

- Codex remote-control commands in a configured Telegram topic.
- Image OCR and Ollama summaries in a separate configured Telegram topic.
- Plain text memory extraction from the same image-summary topic.
- Memory question answering over saved markdown memories.
- Optional local HTTP intake for iOS Shortcuts and other automation.
- Env-controlled image comparison across Tesseract OCR plus one or more Ollama vision models.
- Fuel receipt + odometer extraction with approval before CSV append.

Only one process should poll a given Telegram bot token at a time.

## Setup

- Use `uv` for dependency management.
- Run commands from the repository root.
- Keep secrets in `.env`; never commit real bot tokens, chat IDs, user IDs, LAN IPs, or local absolute paths.
- Use `.env.example` for public configuration shape.

## Verification

Before handing off changes, run:

```bash
uv --cache-dir .uv-cache sync
uv --cache-dir .uv-cache run python -m py_compile bot.py image_summary.py
```

For image-summary changes, also verify `tesseract` is installed and that the configured Ollama endpoint is reachable from the runtime environment.
Preserve `IMAGE_SUMMARY_VISION_MODELS` configurability when changing vision comparison behavior.

For memory extraction changes, verify a sample text message can produce a markdown file under `MEMORY_WORK_DIR`.
For memory query changes, verify `/memory_query ...` or `? ...` retrieves relevant markdown memory files and answers using `MEMORY_QUERY_MODEL`. Interactive query defaults may use a cloud model for speed; keep recurring extraction/summarization defaults local unless the user asks otherwise.

For HTTP intake changes, verify `GET /health` and `POST /memory` locally. Do not expose the endpoint publicly without `HTTP_INTAKE_TOKEN`.

For fuel changes, preserve the approval-before-append flow. Do not append to the fuel CSV without explicit approval. Preserve Fuelio CSV section structure by inserting approved rows into the `## Log` section, not at end of file. Approval must let the user choose full tank, partial fill, correction, or reject. Correction mode must expire after `FUEL_CORRECTION_WINDOW_SECONDS`.

## Operational Notes

- `CODEX_REMOTE_COMMAND` should usually be `codex remote-control` for interactive shells.
- For systemd or cron-like environments, prefer an absolute Codex path, for example:

```text
/home/you/.codex/packages/standalone/current/bin/codex remote-control
```

- The Telegram menu command scope is group-level; topic restrictions are enforced in the handlers.
- Leave unrelated user edits intact. Do not delete runtime files unless the user asks.
