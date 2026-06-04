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

## Run

```bash
uv --cache-dir .uv-cache run python bot.py
```

## Run As A Linux User Service

For a persistent local service, run it with user systemd:

```bash
systemd-run --user \
  --unit=telegram-control \
  --working-directory=/path/to/telegram_control \
  uv --cache-dir .uv-cache run python bot.py
```

Check status:

```bash
systemctl --user status telegram-control.service --no-pager
```

View logs:

```bash
journalctl --user -u telegram-control.service -f
tail -f data/image-summary/worker.log
```

Restart:

```bash
systemctl --user restart telegram-control.service
```

Stop:

```bash
systemctl --user stop telegram-control.service
```

If HTTP intake is enabled, verify it with:

```bash
curl http://127.0.0.1:8787/health
```

The bot starts `codex remote-control` by default. If the bot runs under a
service manager with a minimal `PATH`, set `CODEX_REMOTE_COMMAND` to an
absolute Codex binary path such as:

```text
/home/you/.codex/packages/standalone/current/bin/codex remote-control
```

The bot registers these Telegram menu commands:

- `/codex_start` - stop any running `codex remote-control` process and start it again
- `/codex_status` - show whether the tracked process is running
- `/codex_stop` - stop the tracked process

Commands are only honored in `TELEGRAM_CHAT_ID` and `TELEGRAM_TOPIC_ID`.
Output is written to `logs/codex-remote-control.log`.

## Image Summaries

Images posted in `IMAGE_SUMMARY_TOPIC_ID` are acknowledged immediately,
downloaded to `data/image-summary/images`, and summarized with:

- OCR through `tesseract`
- OCR text summary through Codex when `CODEX_LLM_ENABLED=true`, falling back to
  Ollama `IMAGE_SUMMARY_OCR_LLM_MODEL`
- direct vision summaries through `IMAGE_SUMMARY_VISION_MODELS`

In `IMAGE_SUMMARY_MODE=compare`, the bot compares Tesseract OCR + text LLM
against each configured vision model, for example:

```env
IMAGE_SUMMARY_VISION_MODELS=gemma4:e2b,minicpm-v:latest
```

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

The bot uses `FUEL_MODEL` through Ollama to extract:

- odometer reading
- fuel volume
- fuel rate
- total amount
- station/date/time/receipt number when visible

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
