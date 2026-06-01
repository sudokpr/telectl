# AGENTS.md

## Project Purpose

This project runs one Telegram bot process for:

- Codex remote-control commands in a configured Telegram topic.
- Image OCR and Ollama summaries in a separate configured Telegram topic.

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

## Operational Notes

- `CODEX_REMOTE_COMMAND` should usually be `codex remote-control` for interactive shells.
- For systemd or cron-like environments, prefer an absolute Codex path, for example:

```text
/home/you/.codex/packages/standalone/current/bin/codex remote-control
```

- The Telegram menu command scope is group-level; topic restrictions are enforced in the handlers.
- Leave unrelated user edits intact. Do not delete runtime files unless the user asks.
