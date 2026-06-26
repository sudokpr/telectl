# Telegram Codex Remote Control

Small Telegram bot for starting `codex remote-control` from a specific
supergroup topic, and for summarizing images posted to a separate topic.

## Setup

```bash
uv --cache-dir .uv-cache sync
cp .env.example .env
```

Edit `.env` and set:

- `BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `TELEGRAM_TOPIC_ID`
- `IMAGE_SUMMARY_TOPIC_ID`
- `IMAGE_SUMMARY_OLLAMA_URL`, if Ollama is not running on `localhost`

If Telegram is temporarily unavailable but hosted OwnTracks maps, HTTP intake,
or metrics should keep running, set:

```text
TELEGRAM_BOT_ENABLED=false
HTTP_INTAKE_ENABLED=true
HTTP_INTAKE_NOTIFY_TELEGRAM=false
```

In this mode the process does not poll Telegram and does not require Telegram
credentials, but it still serves configured local HTTP endpoints.

## Run

```bash
uv --cache-dir .uv-cache run python bot.py
```

## Run As A Linux User Service

For a persistent local service, run it with user systemd:

```bash
make start
```

This enables and starts the user unit at `systemd/telegram-control.service`.
With user lingering enabled, the bot starts again after reboot.

Check status:

```bash
make status
```

View logs:

```bash
make logs-follow
make logs-image
```

Restart:

```bash
make restart
```

Stop:

```bash
make stop
```

If HTTP intake is enabled, verify it with:

```bash
make health
```

## Prometheus Metrics

Prometheus metrics are opt-in:

```text
PROMETHEUS_METRICS_ENABLED=true
PROMETHEUS_METRICS_HOST=127.0.0.1
PROMETHEUS_METRICS_PORT=8788
```

When `HTTP_INTAKE_ENABLED=false`, the bot starts a local scrape endpoint at
`http://PROMETHEUS_METRICS_HOST:PROMETHEUS_METRICS_PORT/metrics`. When HTTP
intake is enabled, metrics are served by the intake server at `/metrics`; if
`HTTP_INTAKE_TOKEN` is set, the same token is required for metrics requests.

Metrics cover Telegram update volume, handler latency/errors, image summary
jobs, Ollama latency, OCR output size, memory extraction/query outcomes, fuel
approval flow, OwnTracks digest/map generation, HTTP intake requests, and
process/config gauges. Codex remote-control commands are intentionally not
instrumented.

For iOS map viewing, `/otm` can send either an HTML attachment or a hosted
local URL. Attachment mode is the default:

```text
OWNTRACKS_MAP_DELIVERY=file
```

Hosted mode serves the generated map through the bot's HTTP server:

```text
HTTP_INTAKE_ENABLED=true
HTTP_INTAKE_HOST=0.0.0.0
HTTP_INTAKE_PORT=8787
HTTP_INTAKE_TOKEN=some-long-token
OWNTRACKS_MAP_DELIVERY=hosted
OWNTRACKS_MAP_BASE_URL=http://192.168.1.50:8787
```

In hosted mode, `/otm` replies with a link like
`/owntracks/map/YYYY-MM-DD?token=...` instead of attaching the HTML file. The
hosted route renders the map dynamically from the OwnTracks log on each
request, so there is no per-day HTML file to regenerate for map UI changes.
The hosted stop index at `/owntracks/stops?token=...` is also rendered on
request from the raw OwnTracks log plus saved stop reviews. It groups visits by
reviewed stop/place name when available, falls back to coordinate buckets for
unnamed stops, and lets you search names, tags, notes, and visit dates without
using an LLM. Search expansion is deterministic: built-in aliases are merged
with Codex-generated aliases from
`data/owntracks/search_aliases.generated.json` and optional manual aliases from
`data/owntracks/search_aliases.local.json`.
Use `/otme` to force a self-contained embedded HTML attachment even when hosted
delivery is configured. Embedded attachments download OpenStreetMap tiles during
generation and store them as data URLs, so Telegram does not need to fetch map
tiles when the file is opened.

The bot starts `codex remote-control` by default. If the bot runs under a
service manager with a minimal `PATH`, set `CODEX_REMOTE_COMMAND` to an
absolute Codex binary path such as:

```text
/home/you/.codex/packages/standalone/current/bin/codex remote-control
```

By default, `/cxr` starts Codex remote-control through `systemd-run --user
--scope`, controlled by `CODEX_REMOTE_DETACHED=true`. This places the Codex
daemon outside `telegram-control.service`, so restarting the Telegram bot does
not kill the active Codex session. Set `CODEX_REMOTE_DETACHED=false` only if
you explicitly want the old in-service start behavior.

The bot registers these Telegram menu commands:

- `/cmd` - list bot command shortcuts
- `/cxr` - stop any running `codex remote-control` process and start it again
- `/cxs` - show whether the tracked process is running
- `/cxq` - stop the tracked process
- `/memq` - ask saved memories
- `/otd` - show an OwnTracks daily activity digest
- `/otm` - send an interactive labeled OwnTracks stop map
- `/otme` - send an embedded OwnTracks stop map attachment
- `/otb` - bulk-save stop names, tags, and notes exported by the map
- `/ott` - tag a stop from the latest OwnTracks digest
- `/otn` - name a stop from the latest OwnTracks digest
- `/oto` - add a note to a stop from the latest OwnTracks digest
- `/oth` - show OwnTracks command help

Older underscore commands remain as compatibility aliases, but new menu
commands intentionally avoid underscores for mobile typing.

Commands are only honored in `TELEGRAM_CHAT_ID` and `TELEGRAM_TOPIC_ID`.
Output is written to `logs/codex-remote-control.log`.

## OwnTracks Activity Review

OwnTracks MQTT events are logged to `data/owntracks/mqtt.log`. The daily
digest runs at 21:00 IST and posts to `OWNTRACKS_TOPIC_ID`. It is generic:
use it for cycle rides, errands, saloon visits, government office visits,
tax payments, work visits, or any other activity inferred from location
stops.

The digest lists named geofence events and candidate stops with Google Maps
links, motion, duration, and point count. File delivery writes a self-contained
HTML map attachment. Hosted delivery serves a dynamic Leaflet map from
`/owntracks/map/YYYY-MM-DD`, which gives normal browser tile loading and zoom.
Month and year scopes such as `/owntracks/map/YYYY-MM` and
`/owntracks/map/YYYY` render heatmaps instead of the daily stop map. Background
map tiles are loaded from OpenStreetMap when the hosted map is opened. The
heatmap panel can filter locations by motion mode. `/owntracks/sample` serves a
synthetic heatmap with points across countries, cities, and city areas for
visual testing without OwnTracks logs. `/owntracks/stops` serves a searchable
stop/place index with visit details and links back to each daily map.
`/owntracks/dashboard` serves an activity dashboard for a date range with
home-only days, out-of-home days, travel days, distance, outside-home time, a
daily activity calendar, and most visited places. For
Telegram iOS, prefer `OWNTRACKS_MAP_DELIVERY=hosted`. In the map, you can
select stops, click a stop for a popup editor, rename stops locally, add
tags/notes, and copy a generated `/otb` command back into Telegram to save
those reviews. Each stop gets a short alias such as `s1`, `s2`, etc.

On a hosted daily map, every visible route point is clickable. Use **Mark as
stop** in its popup to persist a short visit that automatic dwell detection
missed. The point becomes a normal stop after the map reloads and can then be
named, tagged, and reviewed like detected stops. Name, tag, and note edits on
hosted maps can be saved directly over the authenticated HTTP intake; Telegram
command export remains available as a fallback. Attached HTML maps are
view-only for these actions because they cannot write back to the local service.

To refresh generated stop-index search aliases with Codex:

```bash
uv --cache-dir .uv-cache run python -m owntracks.digest --generate-search-aliases
```

You can also open `/owntracks/stops` and use **Refresh search aliases**. That
runs the same Codex generation pipeline for the currently selected date range,
writes `search_aliases.generated.json`, and reloads the index.

Optional bounds limit the evidence window:

```bash
uv --cache-dir .uv-cache run python -m owntracks.digest --generate-search-aliases --start 2026-06-01 --end 2026-06-30
```

The generated file is active immediately on the next `/owntracks/stops` page
load. Local aliases in `search_aliases.local.json` are also active and are
merged after generated aliases. The weekly timer template is
`systemd/my-owntracks-search-aliases.timer`; enable it only after confirming
the Codex SDK settings work in the service environment.

Short commands in the OwnTracks topic:

```text
/otd [today|yesterday|DD|MM-DD|YYYY-MM-DD]
/otm [today|yesterday|DD|MM-DD|YYYY-MM-DD|YYYY-MM|YYYY]
/otme [today|yesterday|DD|MM-DD|YYYY-MM-DD|YYYY-MM|YYYY]
/otb 2026-06-06
s1 Local saloon | tags: haircut saloon | note: paid by UPI
s2 Local saloon
/ott s1 haircut saloon
/otn s1 Local saloon
/oto s1 haircut, paid by UPI
```

Run `/otd` first. The bot remembers the last reviewed date for you in that
topic, so `/ott s1 ...` does not need the date. You can still include the
date explicitly. `16` means the 16th of the current month/year, and `06-16`
means June 16 of the current year:

```text
/otm 16
/otme 16
/otm 06-16
/otm 2026-06
/otm 2026
/ott 2026-06-06 s1 property-tax govt-office
```

Saved review data is stored in `OWNTRACKS_USER_TAGS_PATH`, defaulting to
`data/owntracks/user_tags.json`; the raw MQTT log is not modified. Saved stop
coordinates let future visits within about 150 meters reuse names and tags
automatically. Notes stay tied to the specific visit/date.

To hide dense significant-change jitter around stops from daily route maps,
enable the visualization filter:

```env
OWNTRACKS_STOP_JITTER_FILTER_ENABLED=true
OWNTRACKS_STOP_JITTER_RADIUS_METERS=150
OWNTRACKS_STOP_JITTER_MIN_DWELL_MINUTES=10
OWNTRACKS_STOP_JITTER_INCLUDE_GEOFENCES=true
OWNTRACKS_STOP_JITTER_INCLUDE_CANDIDATE_STOPS=true
```

The filter infers anchors from OwnTracks geofence transition events and the
bot's candidate stop clusters. It removes route points within the configured
radius from daily map visualization, including points that no longer carry
`inregions`, but it does not modify the raw MQTT log, saved stop review data,
or stop detection. Each filtered jitter run keeps a boundary connector point
when there is route data before or after the stop, so the route still visibly
connects to stop markers. Month/year heatmaps keep stop points by default so
they still show where time was spent. The older `OWNTRACKS_HOME_FILTER_*`
settings remain available for home-only heatmap suppression.

The sample OwnTracks systemd units in `systemd/` use `/path/to/telectl` as
an install-time placeholder. Replace it with this checkout path before
installing the units.

## Image Summaries

Images posted in `IMAGE_SUMMARY_TOPIC_ID` are acknowledged immediately,
downloaded to `data/image-summary/images`, and summarized with:

- direct Codex vision when `CODEX_LLM_ENABLED=true`
- direct vision summaries through `IMAGE_SUMMARY_VISION_MODELS`
- optional OCR through `tesseract` when `IMAGE_SUMMARY_OCR_ENABLED=true`
- optional OCR text summary through Codex when `CODEX_LLM_ENABLED=true`,
  falling back to Ollama `IMAGE_SUMMARY_OCR_LLM_MODEL`

In `IMAGE_SUMMARY_MODE=compare`, the bot compares direct Codex vision, when
enabled, against each configured Ollama vision model. For example:

```env
CODEX_LLM_ENABLED=true
IMAGE_SUMMARY_OCR_ENABLED=false
IMAGE_SUMMARY_VISION_MODELS=minicpm-v:latest
```

Set `IMAGE_SUMMARY_OCR_ENABLED=true` to add the old `OCR + LLM` result back
into compare mode. `IMAGE_SUMMARY_MODE=ocr` also requires OCR to be enabled.

When compare mode has both a Codex benchmark and one or more Ollama vision
responses, the bot sends a follow-up Codex evaluation comparing the local
response against Codex-extracted image text. The report uses Telegram-friendly
bullets instead of tables and scores factual coverage, omissions, unsupported
claims, text/number fidelity, and practical usefulness.

The bot keeps refreshing Telegram `typing` while processing runs. Debug
logging for all received updates is enabled by default and written to
`data/image-summary/worker.log`; set `IMAGE_SUMMARY_DEBUG_UPDATES=false` after
delivery behavior is confirmed.

Plain text messages in the same topic are treated as already-extracted OCR
text. The bot asks Ollama to extract a durable memory record, saves it as
Markdown under `MEMORY_WORK_DIR`, and replies with what was saved. This is the
first step toward later command/keyword-specific handling, such as fuel receipt
field extraction.

Ask saved memories from the same topic with:

```text
/memq how much was the service labour for polo in 2026
```

or:

```text
? how much was the service labour for polo in 2026
```

The query path retrieves relevant markdown files from `MEMORY_WORK_DIR`, asks
Codex when `CODEX_LLM_ENABLED=true`, falls back to `MEMORY_QUERY_MODEL` through
Ollama on Codex errors, and replies with the answer plus source file names. Use
a fast fallback model such as `gemma4:31b-cloud` for interactive Q&A, while
keeping `MEMORY_LLM_MODEL` on a local model for recurring extraction/summarization work.
`MEMORY_QUERY_TOP_K` defaults to `1` for precise receipt lookups; increase it
for aggregate questions that need multiple memories.

## Codex SDK LLM POC

This repo includes a small proof of concept for using the local Codex app
server through the official Python SDK instead of waiting on local Ollama
models. It reuses your existing Codex authentication.

This uses `openai-codex>=0.1.0b3`, which includes a runtime package with a
compatible `manylinux aarch64` wheel for Raspberry Pi / Debian ARM64. Install
the normal project dependencies:

```bash
uv --cache-dir .uv-cache sync
```

```bash
uv --cache-dir .uv-cache run python codex_poc.py "Say hello in one sentence"
```

Ask about a local image by passing one or more `--image` paths:

```bash
uv --cache-dir .uv-cache run python codex_poc.py \
  --image ./data/image-summary/images/example.jpg \
  "What text and important numbers are visible in this image?"
```

Optional settings:

```env
CODEX_LLM_CWD=.
CODEX_LLM_ENABLED=true
CODEX_LLM_MODEL=gpt-5.4-mini
CODEX_LLM_SANDBOX=read_only
CODEX_LLM_EPHEMERAL=true
CODEX_LLM_BASE_INSTRUCTIONS=
```

## HTTP Intake

Set `HTTP_INTAKE_ENABLED=true` to accept memory text from iOS Shortcuts or other
local automation:

```text
POST http://<host>:8787/memory
Content-Type: application/json
X-Intake-Token: <optional token>
```

```json
{
  "text": "extracted receipt or note text",
  "source": "ios_shortcuts"
}
```

Use `HTTP_INTAKE_HOST=0.0.0.0` if another device on your LAN needs to reach the
server. Set `HTTP_INTAKE_TOKEN` before exposing this beyond a trusted local
network.

## Fuel Tracking

Fuel receipt processing listens in `FUEL_TOPIC_ID`. Upload a fuel receipt
screenshot and an odometer photo together as a Telegram media group. If they are
sent separately, the bot groups fuel images received within
`FUEL_PENDING_WINDOW_SECONDS`.

The bot uses Codex by default to extract:

- odometer reading
- fuel volume
- fuel rate
- total amount
- station/date/time/receipt number when visible

Fuel extraction reuses the `CODEX_LLM_*` SDK settings. To use the old local
Ollama vision path, set `FUEL_LLM_PROVIDER=ollama`; in that mode `FUEL_MODEL`
selects the local vision model.

It sends an approval message with inline buttons for full tank, partial fill,
correction, or reject. The normal case is full tank; partial fill writes
`Full=0`. The bot leaves `km/l` empty and lets Fuelio recalculate derived
consumption. The CSV is updated only after approval, and the new row is inserted
into the Fuelio `## Log` section before the next export section. Dates are
written in Fuelio's `yyyy-MM-dd` import format.

Before approving, tap `Correction` and send the corrected values within
`FUEL_CORRECTION_WINDOW_SECONDS`:

```text
odo=71234,vol=43,rate=112.0,amt=4533.20
```

Supported correction keys include `odo`, `volume`/`vol`, `rate`,
`amount`/`amt`, `date`, `station`, and `notes`. If two receipt values among
amount, rate, and volume are present, the bot recalculates the third; amount
plus rate is preferred when the extracted volume disagrees. Irrelevant text is
ignored after the correction window expires.

Export the local CSV with:

```text
GET http://<host>:8787/fuel.csv
```

## Public Repo Hygiene

Do not commit `.env`, `data/`, `logs/`, or runtime output. Use `.env.example`
for shareable configuration names only.
