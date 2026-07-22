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
approval flow, OwnTracks digest/map generation, hosted OwnTracks UI render
latency, HTTP intake requests, and process/config gauges. Use
`telegram_control_http_request_duration_seconds{route="/owntracks/stops"}` and
`telegram_control_http_request_duration_seconds{route="/owntracks/dashboard"}`
for full request latency, or
`telegram_control_owntracks_ui_render_duration_seconds{view="stops"}` and
`telegram_control_owntracks_ui_render_duration_seconds{view="dashboard"}` for
server-side page generation latency.

## Local Feature Usage Analytics

Durable usage analytics are enabled by default and stored only in a local
SQLite database:

```text
FEATURE_USAGE_ENABLED=true
FEATURE_USAGE_DB_PATH=./data/usage/usage.sqlite
FEATURE_USAGE_RETENTION_DAYS=365
```

When HTTP intake is enabled, open `/usage?token=...` or use the **Usage** link
in any hosted OwnTracks view. `/usage.json?token=...&days=30` returns the same
report as JSON. The report ranks Telegram commands, automatic message
workflows, HTTP intake features, OwnTracks views, and detailed UI controls. It
also lists registered features with no recorded use.

The browser sends sanitized feature identifiers such as
`owntracks.ui.day.toggle_edges`; it does not send search terms, form values,
coordinates, Telegram message text, user IDs, or tokens into the analytics
database. Command compatibility aliases are counted under their canonical
shortcut. Tracking starts when this version first creates the database, so a
zero count means "not observed since tracking began," not "never used in the
past." Keep a representative window (at least 30 days) before treating an
unused feature as a removal candidate. Analytics can be disabled with
`FEATURE_USAGE_ENABLED=false`.

The same durable database is exported through the existing Prometheus
`/metrics` endpoint. Unlike ordinary in-process counters, these totals survive
bot restarts. Registered features are exported with a value of zero before
their first observed use:

```text
telegram_control_feature_usage_total{feature="telegram.command.otm",surface="telegram",category="OwnTracks commands"}
telegram_control_feature_last_used_timestamp_seconds{feature="telegram.command.otm",surface="telegram",category="OwnTracks commands"}
telegram_control_feature_usage_tracking_started_timestamp_seconds
telegram_control_feature_usage_enabled
```

Useful PromQL queries:

```promql
# Most used features
topk(15, telegram_control_feature_usage_total)

# Registered features never observed
telegram_control_feature_usage_total == 0

# Least used features that have been observed at least once
bottomk(15, telegram_control_feature_usage_total > 0)

# Telegram slash commands only
topk(15, telegram_control_feature_usage_total{feature=~"telegram\\.command\\..+"})

# Used before, but not in the last 30 days
telegram_control_feature_last_used_timestamp_seconds > 0
and telegram_control_feature_last_used_timestamp_seconds < time() - 30 * 24 * 60 * 60
```


### Backup Metrics

The daily DietPi backup job can emit a Prometheus text snapshot and push it to
a Pushgateway-compatible endpoint:

```text
BACKUP_PROMETHEUS_METRICS_ENABLED=true
BACKUP_PROMETHEUS_METRICS_FILE=data/metrics/telegram_control_backup.prom
BACKUP_PROMETHEUS_METRICS_PUSH_URL=http://prometheus-pushgateway:9091/metrics/job/telegram_control_backup
```

Backup run metrics:

- `telegram_control_backup_last_run_timestamp_seconds`
- `telegram_control_backup_last_success_timestamp_seconds`
- `telegram_control_backup_last_failure_timestamp_seconds`
- `telegram_control_backup_last_duration_seconds`
- `telegram_control_backup_last_exit_code`

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
The hosted daily map can also repair missed visits without changing the raw
OwnTracks log. Use **Add missing stop**, tap near the day's trajectory, and
drag the proposed marker; it snaps to the nearest route segment and estimates
the visit time between the surrounding samples. Disable snapping for free
placement. A saved manual stop is inserted into the displayed route using its
arrival and departure times, while the raw OwnTracks samples remain unchanged.
Existing visit markers expose **Adjust location** and can likewise
be dragged with optional trajectory snapping before saving the review.
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

The OwnTracks MQTT listener also sends an immediate Telegram message to
`OWNTRACKS_TOPIC_ID` when a newly received location payload contains a `poi`
field. Set `OWNTRACKS_POI_NOTIFY_TELEGRAM=false` to keep logging POIs without
Telegram notifications.

The digest lists named geofence events and candidate stops with Google Maps
links, motion, duration, and point count. File delivery writes a self-contained
HTML map attachment. Hosted delivery serves a dynamic Leaflet map from
`/owntracks/map/YYYY-MM-DD`, which gives normal browser tile loading, zoom, and
an OpenStreetMap/Satellite layer picker.
Month and year scopes such as `/owntracks/map/YYYY-MM` and
`/owntracks/map/YYYY` render heatmaps instead of the daily stop map. Background
map tiles are loaded from OpenStreetMap or Esri World Imagery when the hosted
daily map is opened. The heatmap panel can filter locations by motion mode.
`/owntracks/sample` serves a synthetic heatmap with points across countries,
cities, and city areas for visual testing without OwnTracks logs.
`/owntracks/stops` serves a searchable
stop/place index with visit details and links back to each daily map.
`/owntracks/dashboard` serves an activity dashboard for a date range with
home-only days, out-of-home days, travel days, distance, outside-home time, a
daily activity calendar, and most visited places. Its **Home radius (km)**
control temporarily overrides the configured home radius for that dashboard
view, so nearby errands can be classified as home without changing stored data
or `.env`. With no date parameters, the dashboard defaults to month to date.
Hosted daily maps include Previous day, Today, and Next day navigation. For
Telegram iOS, prefer `OWNTRACKS_MAP_DELIVERY=hosted`. In the map, you can
review visits chronologically, click a visit for a popup editor, rename it,
add tags/notes, adjust entry or exit time, keep it reusable for future nearby visits,
attach local media such as outing photos or running certificates, or dismiss
traffic-like visits while keeping raw route samples visible. Long
silent gaps are shown as editable arrival/departure windows instead of precise
interpolated times. Transition/geofence markers are hidden by default behind
the **Show transition points** toggle. Each visit gets a short alias such as
`s1`, `s2`, etc.

Hosted daily maps also include an on-demand **Resolve place** action in visit
popups. It queries Overpass (`OWNTRACKS_PLACE_RESOLVER=overpass`) around the
stop coordinate and shows nearby OpenStreetMap names as suggestions. Suggestions
are not saved automatically; choosing **Use suggestion** writes a normal saved
stop review to `OWNTRACKS_USER_TAGS_PATH`. Set `OWNTRACKS_PLACE_RESOLVER=disabled`
to avoid sending coordinates to the public Overpass endpoint.

On a hosted daily map, every visible route point is clickable. Use **Save
visit** in its popup to persist a short visit that automatic dwell detection
missed. The point becomes a normal visit after the map reloads and can then be
named, tagged, adjusted, and reviewed like detected visits. Hosted maps can
save these edits directly over the authenticated HTTP intake. Saved visits are
reusable for future proximity naming by default; check **Single-use visit only**
when a visit should apply only to that date. Telegram command export remains
available for name/tag/note fallback. Attached HTML maps are view-only for these
actions because they cannot write back to the local service.

Hosted OwnTracks media is HTTP-only. The daily map popup and `/owntracks/stops`
visit cards let you choose an image or PDF, add a caption, upload it, view the
thumbnail/link, and delete mistakes. Files are stored under `OWNTRACKS_MEDIA_DIR`
(default `data/owntracks/media`) and referenced from `OWNTRACKS_USER_TAGS_PATH`;
the raw OwnTracks MQTT log is not modified. Media routes use the same
`HTTP_INTAKE_TOKEN` protection as the hosted OwnTracks pages.

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

Set `IMAGE_SUMMARY_STREAM=true` to show model output as it arrives for Codex
vision, OCR summarization, direct Ollama vision, and the optional comparison
step. Telegram edits are throttled, and only user-facing answer text is
relayed—not private model reasoning.

Plain text messages in the same topic are treated as already-extracted OCR
text. The bot asks the configured text LLM to extract a durable memory record,
saves it as Markdown under `MEMORY_WORK_DIR`, and replies with what was saved.
Codex is preferred when `CODEX_LLM_ENABLED=true`; Ollama is the optional
fallback when `OLLAMA_ENABLED=true`.

HTTP(S) links in text memories are enriched automatically by default. The bot
performs a bounded GET, extracts the page title, description, and readable
text, generates a compact gist for the normal memory-extraction prompt, and
saves retrieval status, final URL, timestamp, content type, SHA-256, and gist
under `Linked Content`. The user's original message remains unchanged under
`Raw Text`. Linked page content is labelled as untrusted data in the LLM prompt
and cannot supply instructions to the extraction workflow.

Link fetching accepts HTML and plain text, follows at most five redirects,
revalidates every redirect destination, and defaults to three URLs, 2 MiB per
response, and a ten-second request timeout. URLs with credentials, private or
non-public addresses, and nonstandard ports are blocked. Sensitive query values
such as tokens and signatures are redacted from saved link metadata. Configure
the behavior with:

```env
MEMORY_LINK_ENRICHMENT_ENABLED=true
MEMORY_LINK_TIMEOUT_SECONDS=10
MEMORY_LINK_MAX_BYTES=2097152
MEMORY_LINK_MAX_URLS=3
MEMORY_LINK_ALLOWED_HOSTS=
```

Use `MEMORY_LINK_ALLOWED_HOSTS=notes.internal,*.trusted.example` only for
explicitly trusted exceptional destinations. An allowlisted hostname may
resolve to a private address or use a nonstandard port, so this is the approval
mechanism for internal URLs and should be kept narrow.

PDF documents sent to the memory topic are downloaded and hashed before
processing. The extractor first reads embedded PDF text with `pypdf`. If the
document has no usable text layer, pages are rendered locally with PDFium and
passed through Tesseract OCR. The resulting text then goes through the same
configured memory LLM used for plain-text structuring; the LLM does not parse
the PDF bytes or replace the deterministic/OCR extraction step. The saved
source metadata records the SHA-256, extraction method, heuristic quality,
page count, processed pages, and extracted character count.

Uploading the same PDF again without a new caption creates no memory. Uploading
the same PDF with a new caption updates the existing hash-linked memory. PDF
limits are configurable:

```env
MEMORY_PDF_MAX_BYTES=20971520
MEMORY_PDF_MAX_PAGES=10
MEMORY_PDF_RENDER_SCALE=2.5
```

Successful image extraction is also saved automatically as Markdown under
`MEMORY_WORK_DIR`. The memory records the source image filename and SHA-256 so
the same saved image can be backfilled or retried without creating duplicates.
Codex benchmark text is preferred, followed by raw Tesseract OCR and then a
successful Ollama vision response. Set `OLLAMA_ENABLED=false` to skip all local
Ollama image jobs and text fallbacks; when Codex is enabled it is then used for
both image extraction and durable memory structuring. If every configured image
job returns no usable text, memory saving makes one final raw Tesseract attempt.

An image caption is passed to the vision model as trusted user context. Use a
caption to correct ambiguous or misspelled receipt text, describe the image's
purpose, or add facts that are not visually obvious. The saved memory preserves
the caption as `user_comment` metadata and in the Raw Text section. If an image
already has a hash-linked memory, uploading the same image with a new caption
updates that memory instead of silently dropping the correction as a duplicate.

To update an older image without sharing it again, reply directly to that image
with plain text. The bot downloads the replied-to Telegram image by file ID,
matches its SHA-256, replaces the active comment, preserves comment history,
and rebuilds the existing memory from its previously extracted Raw Text without
rerunning image OCR or vision.

The same reply workflow supports PDFs. Reply to an older PDF with the new
caption; the bot downloads the replied-to document by Telegram file ID, hashes
it, extracts embedded text or uses the scanned-page OCR fallback, and updates
the existing PDF memory. Legacy caption-only PDF memories are upgraded in place
by their original Telegram message ID, so the first successful reply adds the
missing PDF hash instead of creating another memory.

The iOS/Telegram workflow that sends replacement text and then immediately
shares the old image is also supported. A text message is held briefly; if the
very next message ID is an image from the same user, chat, and topic, that text
becomes the image's replacement caption and overrides the old forwarded caption.
Otherwise it is saved normally as a text memory. Configure the short hold with:

```env
MEMORY_CAPTION_PAIR_GRACE_SECONDS=3
```

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

Set `MEMORY_QUERY_SHOW_RETRIEVAL=true` to replace the initial search
acknowledgement with the normalized query terms, ranked keyword matches, their
deterministic scores, and any source reused from recent query history. This is
retrieval evidence, not private model chain-of-thought. Set
`MEMORY_QUERY_STREAM=true` to stream the user-facing answer by periodically
editing a Telegram message. Both the Codex SDK and the Ollama fallback support
this stream; the internal `USED_MEMORY_FILES` source-validation footer is never
shown and the final message is replaced with the validated answer and sources.

Memory queries retain the last `MEMORY_QUERY_HISTORY_TURNS` successful query
turns, defaulting to `3`, for the same Telegram user, chat, and topic. Each turn
keeps the question, answer, and selected source paths so follow-ups such as
`what else did I buy on the same day?` can reuse the prior receipt context.
There is no time expiry. History is held in process memory and resets when the
bot service restarts. Both `/memq ...` and `? ...` participate in this history.

Memory answers distinguish document-derived facts from correlated location
evidence. A merchant address printed on a receipt is labelled as a printed
address; a POI location is labelled as the capture/association location and is
not treated as proof of where the purchase occurred. When multiple purchases
match, the answer distinguishes them instead of silently collapsing them. The
model returns exact supporting basenames through an internal footer that the
bot validates and strips; Telegram renders the final `Sources` list itself, so
the model cannot invent local paths or source links.

Every OwnTracks record containing POI text is also saved automatically as a
deterministic Markdown memory under `MEMORY_WORK_DIR`. This includes ordinary
place notes, trail observations, viewpoints, POIs with images, and POIs that
contain transaction evidence. POI memory ingestion is local and deterministic;
it does not call an LLM or require the text to resemble a receipt. The memory
preserves the capture timestamp, coordinates, map URL, nearby stop or route
context, OwnTracks line, optional image metadata, and original POI text.

Ask POI, receipt, price, and location questions through the same memory command:

```text
/memq What POIs did I capture yesterday?
/memq Where was Rs.450 spent on 7th July 2026?
/memq What did I note at Savandurga viewpoint?
```

## OwnTracks Spending POI Index

OwnTracks POIs can be used as spending evidence when iOS automation pushes bank
SMS text or receipt text/images into the POI field. The bot keeps the raw
OwnTracks MQTT log unchanged, then a background indexer scans new POIs into a
local SQLite database:

The preferred iOS automation value for the OwnTracks `poi` field is a JSON
string containing the capture context. The capture time and coordinates are
authoritative for spending indexing; the enclosing OwnTracks `tst`, `lat`, and
`lon` remain unchanged in the raw log and act as fallback values:

```json
{"time ":"2026-07-17T11:48:36+05:30","lat":12.95900953072699,"lon":77.5007669503874,"poi":"Bank or receipt text"}
```

The parser trims whitespace from JSON keys, so both `time ` (the current iOS
Shortcut output) and `time` are accepted. Plain-text legacy POIs remain
supported.

```env
SPENDING_INDEX_ENABLED=true
SPENDING_DB_PATH=./data/spending/spending.sqlite
SPENDING_INDEX_POLL_SECONDS=60
SPENDING_INDEX_IMAGES=true
```

The index stores unreviewed extracted transactions, receipt line items, evidence
paths, OwnTracks line numbers, and nearest location context. Embedded POI images
are decoded under `SPENDING_EVIDENCE_DIR`; OCR runs only when
`IMAGE_SUMMARY_OCR_ENABLED=true`.

### Memory and POI correlation

Each spending-index pass also refreshes deterministic links between Markdown
memories and indexed POI events in the SQLite `memory_poi_links` table. Matching
uses the first unambiguous method in this order:

1. shared `capture_id` (`confidence=1.0`)
2. exact image SHA-256 (`confidence=1.0`)
3. merchant + amount + transaction date (`confidence=0.9`)
4. a unique amount + transaction date match (`confidence=0.8`)

Live `/memq` queries do not ask an LLM to match records. They retrieve memories,
read already-saved links locally, and add the linked POI time, place, distance,
map URL, transaction, OwnTracks line, match method, and confidence to the answer
context. This supports questions such as `/memq Where did I buy broccoli?`.

For new iOS Shortcuts, generate one UUID and include it in both the structured
OwnTracks POI JSON and the Telegram image caption:

```json
{"capture_id":"b3adca44-2f72-4e42-8729-d262fc55df77","time":"2026-07-20T15:03:00+05:30","lat":12.95,"lon":77.50,"poi":"GO GREEN receipt ₹511.50"}
```

```text
capture_id: b3adca44-2f72-4e42-8729-d262fc55df77
Broccoli is misspelt as brookly.
```

Payload `time` remains authoritative; MQTT receive time is not used for the
transaction time or correlation.

The hidden compatibility command `/spi` can be used for manual POI-memory
backfills and correlation retries:

```text
/spi today
/spi 2026-07-07
/spi 2026-07
```

Telegram spending and price questions use `/memq`; there is no separate
`/spq` command.

When HTTP intake is enabled, the same feature is available locally:

```text
POST /spending/index
POST /spending/query
GET /spending/events
```

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
