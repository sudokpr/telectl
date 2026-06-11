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
For memory query changes, verify `/memq ...` or `? ...` retrieves relevant markdown memory files and answers using `MEMORY_QUERY_MODEL`. Interactive query defaults may use a cloud model for speed; keep recurring extraction/summarization defaults local unless the user asks otherwise.

For HTTP intake changes, verify `GET /health` and `POST /memory` locally. Do not expose the endpoint publicly without `HTTP_INTAKE_TOKEN`.

For fuel changes, preserve the approval-before-append flow. Do not append to the fuel CSV without explicit approval. Preserve Fuelio CSV section structure by inserting approved rows into the `## Log` section, not at end of file. Approval must let the user choose full tank, partial fill, correction, or reject. Correction mode must expire after `FUEL_CORRECTION_WINDOW_SECONDS`.

## Operational Notes

- `CODEX_REMOTE_COMMAND` should usually be `codex remote-control` for interactive shells.
- `CODEX_REMOTE_DETACHED=true` should remain the default. `/cxr` starts Codex remote-control through `systemd-run --user --scope` so the Codex daemon is outside `telegram-control.service` and bot restarts do not kill the active Codex session.
- For systemd or cron-like environments, prefer an absolute Codex path, for example:

```text
/home/you/.codex/packages/standalone/current/bin/codex remote-control
```

- The Telegram menu command scope is group-level; topic restrictions are enforced in the handlers.
- Telegram menu commands should avoid underscores because they are hard to type on mobile. Prefer unique 3-4 character commands such as `cxr`, `cxs`, `cxq`, `memq`, `otd`, `otm`, `otb`, `ott`, `otn`, `oto`, and `oth`. Older long or underscore commands may remain as hidden compatibility aliases.
- When adding a new bot command, update the central command shortcut list, Telegram menu registration, `/cmd` shortcut help, README command docs, and any topic-specific help command in the same change.
- OwnTracks map tile embedding is controlled by `OWNTRACKS_EMBED_MAP_TILES` because tile downloads disclose location-derived tile coordinates to OpenStreetMap. Keep embedding opt-in unless the user explicitly asks to enable it.
- OwnTracks map delivery is controlled by `OWNTRACKS_MAP_DELIVERY=file|hosted`. Preserve both modes: file mode sends the self-contained HTML attachment, hosted mode sends a local HTTP URL served from `/owntracks/map/YYYY-MM-DD` and renders the Leaflet map dynamically from the OwnTracks log without relying on a saved per-day HTML file.
- OwnTracks map scope supports `today`, `yesterday`, `DD`, `MM-DD`, `YYYY-MM-DD`, `YYYY-MM`, and `YYYY`. Day scopes render the labeled stop map; month/year scopes render an aggregated heatmap at `/owntracks/map/YYYY-MM` or `/owntracks/map/YYYY`.
- OwnTracks heatmaps support client-side filtering by motion mode. Keep the `all`, `stationary`, `walking`, `cycling`, `automotive`, and `moving` modes in sync between the panel, heat layer, and any motion summaries. `/owntracks/sample` serves a synthetic heatmap for visual testing without real OwnTracks logs.
- Preserve compatibility with legacy OwnTracks saved stop IDs such as `unnamed-stop-17` when current generated stop IDs include line ranges such as `unnamed-stop-17-547-551`.
- OwnTracks saved stop reviews include coordinates when available. Future stops within about 150 meters may inherit saved names and tags by proximity, but notes are visit/date-specific and should not be inherited automatically.
- OwnTracks date arguments for `/otd` still support `today`, `yesterday`, `DD`, `MM-DD`, and `YYYY-MM-DD`. `DD` uses the current month and year; `MM-DD` uses the current year. Keep bot validation and help text aligned with `target_date_from_text`.
- Use the repo `Makefile` for common local operations:
  - `make start` creates/starts the transient user systemd service with `systemd-run`.
  - `make stop`, `make restart`, and `make status` manage `telegram-control.service`.
  - `make logs` and `make logs-follow` read the user journal.
  - `make check` runs dependency sync and Python compilation.
  - Explicit `make service-*` targets are also available for service operations.
- Restart `telegram-control.service` after changing bot command registration or handlers so Telegram picks up the new behavior.
- Leave unrelated user edits intact. Do not delete runtime files unless the user asks.
