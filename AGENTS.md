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
- OwnTracks hosted stop/place index with deterministic alias-expanded search.
- Optional Codex-generated OwnTracks search alias refresh, used only to update local JSON aliases and not during live search.

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
For OwnTracks hosted UI changes, verify `GET /owntracks/stops` and `GET /owntracks/dashboard` locally when HTTP intake is enabled. These views should render dynamically from the raw OwnTracks log and saved stop reviews, not from a stale precomputed index.
For OwnTracks search alias changes, verify the deterministic search path without an LLM request. Codex may be used to refresh `data/owntracks/search_aliases.generated.json`, but live stop-index search must use local merged aliases only.

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
- OwnTracks embedded HTML map attachments should embed first-view OpenStreetMap tiles so Telegram does not need to fetch tiles when the file is opened. Tile downloads happen during generation and disclose location-derived tile coordinates to OpenStreetMap.
- OwnTracks map delivery is controlled by `OWNTRACKS_MAP_DELIVERY=file|hosted`. Preserve both modes: file mode sends the self-contained HTML attachment, hosted mode sends a local HTTP URL served from `/owntracks/map/YYYY-MM-DD` and renders the Leaflet map dynamically from the OwnTracks log without relying on a saved per-day HTML file.
- OwnTracks map scope supports `today`, `yesterday`, `DD`, `MM-DD`, `YYYY-MM-DD`, `YYYY-MM`, and `YYYY`. Day scopes render the labeled stop map; month/year scopes render an aggregated heatmap at `/owntracks/map/YYYY-MM` or `/owntracks/map/YYYY`.
- OwnTracks heatmaps support client-side filtering by motion mode. Keep the `all`, `stationary`, `walking`, `cycling`, `automotive`, and `moving` modes in sync between the panel, heat layer, and any motion summaries. `/owntracks/sample` serves a synthetic heatmap for visual testing without real OwnTracks logs.
- OwnTracks hosted stop index is served from `/owntracks/stops`. It should build on request from raw OwnTracks logs plus `OWNTRACKS_USER_TAGS_PATH`, include detected stop visits, OwnTracks waypoint records and waypoint-proximity visits, and OwnTracks transition/geofence visits, group visits by reviewed stop/place name first, fall back to coordinate buckets for unnamed stops, and link visit rows back to daily maps.
- OwnTracks activity dashboard is served from `/owntracks/dashboard`. It should remain deterministic and interactive, with date-range controls, home-only/out-of-home/travel day counts, distance and outside-home time summaries, daily calendar/table views, most visited places, and links back to daily maps or the stop index.
- OwnTracks points will often be received in bulk because the MQTT/server endpoint is local and the phone may only push buffered points after returning home. Treat payload timestamps (`tst`/`created_at`) as the source of truth for visit timing; do not infer visit timing from MQTT receive timestamps, and be careful when associating POIs/SMS automations with nearby route points after a bulk upload.
- OwnTracks stop-index search must remain deterministic at query time. Do not call an LLM while rendering search results or while the user types. Search aliases are merged from built-in defaults, `OWNTRACKS_SEARCH_ALIASES_GENERATED_PATH`, and `OWNTRACKS_SEARCH_ALIASES_LOCAL_PATH`.
- Codex-generated OwnTracks search aliases are refreshed on demand from the stop-index UI through `/owntracks/search-aliases` or by running `python -m owntracks.digest --generate-search-aliases`. This writes `data/owntracks/search_aliases.generated.json` by default. Keep this refresh path optional, inspectable, and separate from the live search path.
- The stop-index UI should show alias metadata, including active category/term counts and the generated alias file's last sync time when available.
- OwnTracks stop-jitter and home filtering are visualization-only. Preserve raw MQTT logs, saved stop review data, digest stop detection, and heatmap stop semantics when changing `OWNTRACKS_STOP_JITTER_FILTER_ENABLED`, `OWNTRACKS_STOP_JITTER_RADIUS_METERS`, `OWNTRACKS_STOP_JITTER_MIN_DWELL_MINUTES`, `OWNTRACKS_HOME_FILTER_ENABLED`, `OWNTRACKS_HOME_REGION_NAMES`, or `OWNTRACKS_HOME_FILTER_RADIUS_METERS`.
- OwnTracks stop-jitter filtering must preserve route connector points for stop boundaries and OwnTracks transition points (`t="c"`), so repeated trips such as office -> lunch -> office -> snacks -> office do not collapse into a single edge. Keep `tests/test_owntracks_stop_jitter.py` aligned with this behavior.
- Preserve compatibility with legacy OwnTracks saved stop IDs such as `unnamed-stop-17` when current generated stop IDs include line ranges such as `unnamed-stop-17-547-551`.
- OwnTracks saved stop reviews include coordinates when available. Future stops within about 150 meters may inherit saved names and tags by proximity, but notes are visit/date-specific and should not be inherited automatically.
- OwnTracks date arguments for `/otd` still support `today`, `yesterday`, `DD`, `MM-DD`, and `YYYY-MM-DD`. `DD` uses the current month and year; `MM-DD` uses the current year. Keep bot validation and help text aligned with `target_date_from_text`.
- Use the repo `Makefile` for common local operations:
  - `make start` enables/starts the persistent user systemd service from `systemd/telegram-control.service`.
  - `make stop`, `make restart`, and `make status` manage `telegram-control.service`.
  - `make logs` and `make logs-follow` read the user journal.
  - `make check` runs dependency sync, Python compilation, and pytest.
  - Explicit `make service-*` targets are also available for service operations.
- In Codex sandboxed runs, avoid retry churn for commands known to require host access:
  - Use escalation directly for git commands that write `.git` or contact remotes, such as `git add`, `git commit`, and `git push`; `.git` may be mounted read-only in the sandbox.
  - Use escalation directly for user-systemd operations, such as `make start`, `make stop`, `make restart`, `make status`, `make logs`, and `make logs-follow`; the sandbox may not have access to the user systemd bus or journal.
  - Normal read-only inspection commands, source edits, `uv --cache-dir .uv-cache run pytest`, and `make check` should run without escalation unless they fail for an environment-specific reason.
- Restart `telegram-control.service` after changing bot command registration or handlers so Telegram picks up the new behavior.
- Leave unrelated user edits intact. Do not delete runtime files unless the user asks.
