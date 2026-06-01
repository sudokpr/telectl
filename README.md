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
- OCR text summary through Ollama `llama3.1:8b`
- direct vision summary through Ollama `gemma4:e2b`

The bot keeps refreshing Telegram `typing` while processing runs. Debug
logging for all received updates is enabled by default and written to
`data/image-summary/worker.log`; set `IMAGE_SUMMARY_DEBUG_UPDATES=false` after
delivery behavior is confirmed.

## Public Repo Hygiene

Do not commit `.env`, `data/`, `logs/`, or runtime output. Use `.env.example`
for shareable configuration names only.
